import logging

from typing import Optional, List

from tracardi.config import tracardi
from tracardi.context import get_context, Context
from tracardi.domain.storage_record import RecordMetadata
from tracardi.exceptions.log_handler import log_handler
from tracardi.service.storage.redis.cache import RedisCache
from tracardi.service.storage.redis.collections import Collection
from tracardi.service.storage.redis_client import RedisClient
from tracardi.service.tracking.cache.prefix import get_cache_prefix
from tracardi.service.tracking.locking import GlobalMutexLock
from tracardi.service.utils.getters import get_entity_id
from tracardi.domain.profile import Profile
from tracardi.service.merging.profile_merger import merge_profiles

logger = logging.getLogger(__name__)
logger.setLevel(tracardi.logging_level)
logger.addHandler(log_handler)
redis_cache = RedisCache(ttl=None)
_redis = RedisClient()


def delete_profile_cache(profile_id: str, context: Context):
    production = context.context_abrv()

    key_namespace = f"{Collection.profile}{production}:{get_cache_prefix(profile_id[0:2])}:"
    redis_cache.delete(
        profile_id,
        key_namespace
    )


def load_profile_cache(profile_id: str, context: Context) -> Optional[Profile]:
    production = context.context_abrv()

    key_namespace = f"{Collection.profile}{production}:{get_cache_prefix(profile_id[0:2])}:"

    if not redis_cache.has(profile_id, key_namespace):
        return None

    _data = redis_cache.get(
        profile_id,
        key_namespace
    )

    try:
        context, profile, profile_changes, profile_metadata = _data
    except Exception:
        return None

    profile = Profile(**profile)
    if profile_metadata:
        profile.set_meta_data(RecordMetadata(**profile_metadata))

    return profile


def save_profile_cache(profile: Optional[Profile]):
    if profile:

        context = get_context()
        key = f"{Collection.profile}{context.context_abrv()}:{get_cache_prefix(profile.id[0:2])}:"

        index = profile.get_meta_data()

        print(f"Caching with profile index {index}")

        try:
            if index is None:
                raise ValueError("Empty profile index.")

            value = (
                    {
                        "production": context.production,
                        "tenant": context.tenant
                    },
                    profile.model_dump(mode="json", exclude_defaults=True),
                    None,
                    index.model_dump(mode="json")
                )

            redis_cache.set(
                profile.id,
                value,
                key
            )

        except ValueError as e:
            logger.error(f"Saving to cache filed: Detail: {str(e)}")




def save_profiles_in_cache(profiles: List[Profile]):
    context = get_context()
    if profiles:

        for profile in profiles:
            if profile:
                value = (
                    {
                        "production": context.production,
                        "tenant": context.tenant
                    },
                    profile.model_dump(mode="json", exclude_defaults=True),
                    profile.get_meta_data().model_dump() if profile.has_meta_data() else None
                )

                collection = f"{Collection.profile}{context.context_abrv()}:{get_cache_prefix(profile.id[0:2])}:"

                redis_cache.set(profile.id, value, collection)


def merge_with_cache_profile(profile: Profile, context: Context) -> Profile:
    # Loads profile form cache and merges it with the current profile

    _cache_profile = load_profile_cache(profile.id, context)

    if not _cache_profile:
        return profile

    return merge_profiles(base_profile=_cache_profile, profile=profile)


async def lock_merge_with_cache_and_save_profile(profile: Profile, context: Context, lock_name=None):
    async with GlobalMutexLock(
            get_entity_id(profile),
            'profile',
            namespace=Collection.lock_tracker,
            redis=_redis,
            name=lock_name
    ):
        profile = merge_with_cache_profile(profile, context)

        return save_profile_cache(profile)
