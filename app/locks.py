"""One upload / delete at a time per campaign.

Redis lock (SET NX PX + token-checked release) when REDIS_URL is set, so it works across
several API instances. Without Redis an in-process lock is used: fine for one instance.
A second request for a busy campaign fails fast with CampaignBusy (HTTP 409) instead of waiting.
"""
import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

log = logging.getLogger(__name__)

_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end
"""


class CampaignBusy(RuntimeError):
    """Another upload / delete for this campaign is running. Maps to HTTP 409."""


class Locks:
    def __init__(self, redis=None, ttl_seconds: int = 600, prefix: str = "tc-rag:lock:"):
        self.redis = redis
        self.ttl_ms = ttl_seconds * 1000
        self.prefix = prefix
        self._local: dict[int, asyncio.Lock] = {}

    @asynccontextmanager
    async def hold(self, campaign_id: int):
        if self.redis is None:
            lock = self._local.setdefault(campaign_id, asyncio.Lock())
            if lock.locked():
                raise CampaignBusy(f"campaign {campaign_id} is being updated, try again shortly")
            async with lock:
                yield
            return

        key, token = f"{self.prefix}{campaign_id}", secrets.token_hex(16)
        if not await self.redis.set(key, token, nx=True, px=self.ttl_ms):
            raise CampaignBusy(f"campaign {campaign_id} is being updated, try again shortly")
        try:
            yield
        finally:
            try:
                await self.redis.eval(_RELEASE, 1, key, token)
            except Exception:  # the TTL frees it anyway
                log.warning("lock release failed", extra={"data": {"campaign_id": campaign_id}}, exc_info=True)

    async def ping(self) -> bool:
        if self.redis is None:
            return True
        return bool(await self.redis.ping())
