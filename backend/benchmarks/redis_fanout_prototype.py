"""Opt-in Redis Pub/Sub prototype for cross-worker Yjs update fan-out.

This experiment is intentionally separate from the V1 request path. It proves
the wire format and relay lifecycle without implying production delivery
guarantees from Redis Pub/Sub.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import uuid
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis

CHANNEL_PREFIX = "concord:ydoc:v2:"
MAX_UPDATE_BYTES = 5 * 1024 * 1024
UpdateHandler = Callable[[str, bytes], Awaitable[None]]


class RedisYjsFanout:
    def __init__(self, redis: Redis, origin: str | None = None) -> None:
        self.redis = redis
        self.origin = origin or str(uuid.uuid4())

    @staticmethod
    def channel(session_id: str) -> str:
        return f"{CHANNEL_PREFIX}{uuid.UUID(session_id)}"

    def encode(self, update: bytes) -> str:
        if not update or len(update) > MAX_UPDATE_BYTES:
            raise ValueError("Yjs update is empty or exceeds the prototype limit")
        return json.dumps(
            {
                "version": 1,
                "origin": self.origin,
                "message_id": str(uuid.uuid4()),
                "update": base64.b64encode(update).decode("ascii"),
            },
            separators=(",", ":"),
        )

    def decode(self, payload: str | bytes) -> tuple[str, bytes] | None:
        try:
            event = json.loads(payload)
            if event.get("version") != 1 or event.get("origin") == self.origin:
                return None
            origin = str(uuid.UUID(event["origin"]))
            uuid.UUID(event["message_id"])
            update = base64.b64decode(event["update"], validate=True)
            if not update or len(update) > MAX_UPDATE_BYTES:
                return None
            return origin, update
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    async def publish(self, session_id: str, update: bytes) -> int:
        return int(
            await self.redis.publish(self.channel(session_id), self.encode(update))
        )

    async def relay(self, stop: asyncio.Event, handler: UpdateHandler) -> None:
        pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
        await pubsub.psubscribe(f"{CHANNEL_PREFIX}*")
        try:
            while not stop.is_set():
                message = await pubsub.get_message(timeout=0.25)
                if not message or message.get("type") != "pmessage":
                    continue
                channel = message["channel"]
                if isinstance(channel, bytes):
                    channel = channel.decode()
                try:
                    session_id = str(uuid.UUID(channel.removeprefix(CHANNEL_PREFIX)))
                except (AttributeError, ValueError):
                    continue
                decoded = self.decode(message["data"])
                if decoded is not None:
                    _origin, update = decoded
                    await handler(session_id, update)
        finally:
            await pubsub.aclose()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Publish one prototype Yjs update")
    parser.add_argument("session_id")
    parser.add_argument("--redis-url", default="redis://127.0.0.1:6380/0")
    parser.add_argument("--update", default="prototype-update")
    args = parser.parse_args()
    redis = Redis.from_url(args.redis_url, decode_responses=False)
    try:
        subscribers = await RedisYjsFanout(redis).publish(
            args.session_id, args.update.encode()
        )
        print(f"Published to {subscribers} subscriber(s)")
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
