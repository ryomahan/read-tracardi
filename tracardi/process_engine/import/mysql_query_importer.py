import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Tuple
from uuid import uuid4

import aiomysql

from tracardi.domain.import_config import ImportConfig
from tracardi.domain.task import Task
from .importer import Importer
from pydantic import field_validator, BaseModel
from tracardi.service.plugin.domain.register import Form, FormGroup, FormField, FormComponent
from tracardi.domain.named_entity import NamedEntity
from tracardi.service.storage.driver.elastic import resource as resource_db
from tracardi.service.storage.driver.elastic import task as task_db
from tracardi.process_engine.action.v1.connectors.mysql.query.model.connection import Connection
from tracardi.service.plugin.plugin_endpoint import PluginEndpoint
from tracardi.worker.celery_worker import run_mysql_query_import_job


class MySQLQueryImportConfig(BaseModel):
    source: NamedEntity
    database_name: NamedEntity
    query: str
    batch: int = 100

    @field_validator('query')
    @classmethod
    def validate_query(cls, value):
        if (not value.lower().startswith("select")) or "limit" in value.lower():
            raise ValueError("Provided query cannot contain LIMIT keyword and has to start with SELECT keyword. "
                             "Limit is used to batch the data during import.")
        return value

    @field_validator("source", "database_name")
    @classmethod
    def validate_named_entities(cls, value):
        if not value.id:
            raise ValueError(f"This field cannot be empty.")
        return value


class DatabaseFetcherConfig(BaseModel):
    source: NamedEntity

    @field_validator("source")
    @classmethod
    def validate_named_entities(cls, value):
        if not value.id:
            raise ValueError(f"This field cannot be empty.")
        return value


class Endpoint(PluginEndpoint):

    @staticmethod
    async def fetch_databases(config: dict):
        config = DatabaseFetcherConfig(**config)
        resource = await resource_db.load(config.source.id)
        credentials = resource.credentials.production
        pool = await Connection(**credentials).connect()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(f"SHOW DATABASES")
                result = await cursor.fetchall()

                await cursor.close()
                conn.close()

                return {
                    "total": len(result),
                    "result": [{"name": list(record.values())[0], "id": list(record.values())[0]} for record in result]
                }


class MySQLQueryImporter(Importer):
    config_model = MySQLQueryImportConfig

    init = {
        "source": {"name": "", "id": ""},
        "database_name": {"name": "", "id": ""},
        "query": "",
        "batch": 100,
    }

    form = Form(groups=[FormGroup(
        fields=[
            FormField(
                name="MySQL resource",
                id="source",
                description="Select MySQL resource you want to connect to. "
                            "Resource must have database credentials defined.",
                component=FormComponent(type="resource", props={"tag": "mysql"})
            ),
            FormField(
                name="Database name",
                id="database_name",
                description="Select database that you want to fetch data from.",
                component=FormComponent(type="autocomplete", props={
                    "label": "Database",
                    "endpoint": {
                        "url": Endpoint.url(__name__, "fetch_databases"),
                        "method": "post"
                    }
                })
            ),
            FormField(
                name="Query",
                id="query",
                description="Type in the query that will filter the data.",
                component=FormComponent(type="sql", props={
                    "label": "Query"
                })
            ),
            FormField(
                name="Batch",
                id="batch",
                description="System will not import the whole data at once. It will break the whole data set "
                            "into small batches. Type a number of records that will be processed in one batch.",
                component=FormComponent(type="text", props={"label": "Batch"})
            )
        ])])

    async def run(self, task_name, import_config: ImportConfig) -> Tuple[str, str]:

        def add_to_celery(import_config: ImportConfig, credentials):
            # todo replace celery
            return run_mysql_query_import_job.delay(
                import_config.model_dump(),
                credentials
            )

        config = MySQLQueryImportConfig(**import_config.config)
        resource = await resource_db.load(config.source.id)
        credentials = resource.credentials.test if self.debug is True else resource.credentials.production

        executor = ThreadPoolExecutor(
            max_workers=1,
        )
        loop = asyncio. get_running_loop()
        blocking_tasks = [loop.run_in_executor(executor, add_to_celery, import_config, credentials)]
        completed, pending = await asyncio.wait(blocking_tasks)
        celery_task = completed.pop().result()

        task = Task(
            timestamp=datetime.utcnow(),
            id=str(uuid4()),
            name=task_name if task_name else import_config.name,
            type="import",
            params=import_config.model_dump(),
            task_id=celery_task.id
        )

        await task_db.upsert_task(task)

        return task.id, celery_task.id
