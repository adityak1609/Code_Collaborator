"""Redis Pub/Sub relay for execution events destined for WebSocket rooms."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.collaboration.websocket import broadcast_execution_event
from app.config import settings
from app.execution.queue import EXECUTION_EVENT_CHANNEL_PREFIX

logger = logging.getLogger(__name__)
_EVENT_TYPES = {"execution_status", "execution_output"}


def validate_execution_event(
    channel: str | bytes, payload: str | bytes
) -> tuple[str, dict[str, object]] | None:
    """Decode a worker event and bind it to the session named by its channel."""
    if isinstance(channel, bytes):
        channel = channel.decode(errors="replace")
    if isinstance(payload, bytes):
        payload = payload.decode(errors="replace")
    if not channel.startswith(EXECUTION_EVENT_CHANNEL_PREFIX):
        return None

    session_id = channel.removeprefix(EXECUTION_EVENT_CHANNEL_PREFIX)
    try:
        uuid.UUID(session_id)
        event = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(event, dict) or event.get("type") not in _EVENT_TYPES:
        return None
    if event.get("session_id") != session_id:
        return None
    try:
        uuid.UUID(str(event.get("execution_id")))
    except (TypeError, ValueError):
        return None
    return session_id, event


async def run_execution_event_relay(stop_event: asyncio.Event) -> None:
    """Reconnect as needed and fan worker events out through the WebSocket hub."""
    pattern = f"{EXECUTION_EVENT_CHANNEL_PREFIX}*"
    while not stop_event.is_set():
        redis = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        pubsub = redis.pubsub(ignore_subscribe_messages=True)
        try:
            await pubsub.psubscribe(pattern)
            logger.info("Execution event relay subscribed to %s", pattern)
            while not stop_event.is_set():
                message = await pubsub.get_message(timeout=1)
                if message is None or message.get("type") != "pmessage":
                    continue
                validated = validate_execution_event(
                    message.get("channel", ""), message.get("data", "")
                )
                if validated is None:
                    logger.warning("Ignoring invalid execution event")
                    continue
                session_id, event = validated
                await broadcast_execution_event(session_id, event)
        except (RedisConnectionError, RedisTimeoutError):
            if not stop_event.is_set():
                logger.warning("Execution event relay unavailable; retrying")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=1)
                except TimeoutError:
                    pass
        finally:
            await pubsub.aclose()
            await redis.aclose()
