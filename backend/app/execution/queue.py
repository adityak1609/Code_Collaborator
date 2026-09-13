"""Redis transport for execution jobs and cancellation signals."""

from __future__ import annotations

import uuid
from typing import Protocol

from redis.asyncio import Redis

from app.config import settings

EXECUTION_QUEUE_KEY = "concord:execution:queue"


class RedisExecutionClient(Protocol):
    async def rpush(self, key: str, value: str) -> object: ...

    async def set(
        self,
        key: str,
        value: str,
        *,
        ex: int | None = None,
    ) -> object: ...

    async def aclose(self) -> None: ...


class ExecutionQueue:
    """Small Redis boundary shared by the API and the future worker."""

    def __init__(self, redis_client: RedisExecutionClient | None = None) -> None:
        self._redis: RedisExecutionClient = redis_client or Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=5.0,
            socket_timeout=5.0,
            health_check_interval=30,
        )

    @staticmethod
    def cancellation_key(execution_id: uuid.UUID) -> str:
        return f"concord:execution:{execution_id}:cancel"

    async def enqueue(self, execution_id: uuid.UUID) -> None:
        await self._redis.rpush(EXECUTION_QUEUE_KEY, str(execution_id))

    async def request_cancellation(self, execution_id: uuid.UUID) -> None:
        await self._redis.set(
            self.cancellation_key(execution_id),
            "1",
            ex=settings.execution_timeout_seconds + 60,
        )

    async def close(self) -> None:
        await self._redis.aclose()


execution_queue = ExecutionQueue()
