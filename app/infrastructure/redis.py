import redis.asyncio as redis

from app.infrastructure.config import get_settings


class RedisClient:
    def __init__(self, url: str) -> None:
        self._client = redis.from_url(url, decode_responses=True)

    async def ping(self) -> bool:
        return bool(await self._client.ping())

    async def close(self) -> None:
        await self._client.aclose()


def create_redis_client() -> RedisClient:
    return RedisClient(get_settings().redis_url)
