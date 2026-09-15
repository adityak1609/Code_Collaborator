"""Redis worker that runs queued code in locked-down Docker containers."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import tarfile
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

import docker
from docker.errors import ImageNotFound
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import select

from app.config import settings
from app.database import async_session_factory
from app.execution.queue import EXECUTION_QUEUE_KEY, ExecutionQueue
from app.models import Execution, ExecutionStatus, LanguageEnum

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
EVENT_CHANNEL_PREFIX = "concord:execution:events:"


@dataclass(frozen=True)
class Runtime:
    image: str
    filename: str
    command: list[str]


RUNTIMES = {
    LanguageEnum.python: Runtime(
        "python:3.12-slim", "main.py", ["python3", "-u", "/code/main.py"]
    ),
    LanguageEnum.javascript: Runtime(
        "node:22-slim", "main.js", ["node", "/code/main.js"]
    ),
    LanguageEnum.cpp: Runtime(
        "gcc:13",
        "main.cpp",
        ["sh", "-c", "g++ /code/main.cpp -o /tmp/a.out && /tmp/a.out"],
    ),
}


def _code_archive(filename: str, code: str) -> bytes:
    payload = code.encode()
    info = tarfile.TarInfo(filename)
    info.size = len(payload)
    info.mode = 0o444
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        tar.addfile(info, io.BytesIO(payload))
    return archive.getvalue()


def _bounded(data: bytes) -> str:
    limit = settings.execution_output_limit_bytes
    suffix = b"\n[output truncated]\n"
    if len(data) > limit:
        data = data[: max(0, limit - len(suffix))] + suffix
    return data.decode(errors="replace")


class ExecutionWorker:
    def __init__(self, redis_client=None, docker_client=None) -> None:
        self.redis = redis_client or Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=None,
        )
        self.docker = docker_client or docker.from_env()
        self.queue = ExecutionQueue(self.redis)

    async def _publish(self, execution: Execution) -> None:
        event = {
            "type": "execution_status",
            "execution_id": str(execution.id),
            "session_id": str(execution.session_id),
            "status": execution.status.value,
            "stdout": execution.stdout,
            "stderr": execution.stderr,
            "exit_code": execution.exit_code,
            "elapsed_ms": execution.elapsed_ms,
        }
        await self.redis.publish(
            f"{EVENT_CHANNEL_PREFIX}{execution.session_id}", json.dumps(event)
        )

    async def _transition(
        self,
        execution_id: uuid.UUID,
        expected: ExecutionStatus,
        target: ExecutionStatus,
    ) -> Execution | None:
        async with async_session_factory() as db:
            result = await db.execute(
                select(Execution).where(Execution.id == execution_id).with_for_update()
            )
            execution = result.scalar_one_or_none()
            if execution is None or execution.status is not expected:
                return None
            execution.transition_to(target)
            await db.commit()
            await db.refresh(execution)
            await self._publish(execution)
            return execution

    async def _finish(
        self,
        execution_id: uuid.UUID,
        target: ExecutionStatus,
        stdout: str,
        stderr: str,
        exit_code: int | None,
        elapsed_ms: int,
    ) -> None:
        async with async_session_factory() as db:
            result = await db.execute(
                select(Execution).where(Execution.id == execution_id).with_for_update()
            )
            execution = result.scalar_one_or_none()
            if execution is None:
                return
            if execution.status is not ExecutionStatus.CANCELLED:
                execution.transition_to(target)
            execution.stdout = stdout
            execution.stderr = stderr
            execution.exit_code = exit_code
            execution.elapsed_ms = elapsed_ms
            execution.finished_at = datetime.now(UTC)
            await db.commit()
            await db.refresh(execution)
            await self._publish(execution)

    async def _ensure_image(self, image: str) -> None:
        try:
            await asyncio.to_thread(self.docker.images.get, image)
        except ImageNotFound:
            logger.info("Pulling sandbox image %s", image)
            await asyncio.to_thread(self.docker.images.pull, image)

    async def _create_container(self, execution: Execution):
        runtime = RUNTIMES[execution.language]
        await self._ensure_image(runtime.image)
        volume = await asyncio.to_thread(
            self.docker.volumes.create,
            name=f"concord-execution-{execution.id}",
        )
        try:
            writer = await asyncio.to_thread(
                self.docker.containers.create,
                runtime.image,
                ["true"],
                network_disabled=True,
                volumes={volume.name: {"bind": "/code", "mode": "rw"}},
            )
            try:
                await asyncio.to_thread(
                    writer.put_archive,
                    "/code",
                    _code_archive(runtime.filename, execution.code),
                )
            finally:
                await asyncio.to_thread(writer.remove, force=True)
        except Exception:
            await asyncio.to_thread(volume.remove, force=True)
            raise
        try:
            container = await asyncio.to_thread(
                self.docker.containers.create,
                runtime.image,
                runtime.command,
                detach=True,
                network_disabled=True,
                network_mode="none",
                mem_limit=settings.execution_memory_limit,
                nano_cpus=settings.execution_cpu_limit * 1_000_000_000,
                pids_limit=settings.execution_pids_limit,
                read_only=True,
                tmpfs={"/tmp": "rw,nosuid,size=64m"},
                user="65534:65534",
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                working_dir="/code",
                volumes={volume.name: {"bind": "/code", "mode": "ro"}},
            )
            return container, volume
        except Exception:
            await asyncio.to_thread(volume.remove, force=True)
            raise

    async def _monitor(self, execution_id: uuid.UUID, container):
        deadline = time.monotonic() + settings.execution_timeout_seconds
        while True:
            if await self.redis.exists(self.queue.cancellation_key(execution_id)):
                await asyncio.to_thread(container.kill)
                return ExecutionStatus.CANCELLED
            await asyncio.to_thread(container.reload)
            if container.status in {"exited", "dead"}:
                return None
            if time.monotonic() >= deadline:
                await asyncio.to_thread(container.kill)
                return ExecutionStatus.TIMEOUT
            await asyncio.sleep(settings.execution_poll_interval_seconds)

    async def process(self, execution_id: uuid.UUID) -> None:
        execution = await self._transition(
            execution_id, ExecutionStatus.QUEUED, ExecutionStatus.STARTING
        )
        if execution is None:
            return
        started = time.monotonic()
        container = volume = None
        try:
            container, volume = await self._create_container(execution)
            await asyncio.to_thread(container.start)
            if (
                await self._transition(
                    execution_id, ExecutionStatus.STARTING, ExecutionStatus.RUNNING
                )
                is None
            ):
                await asyncio.to_thread(container.kill)
                return
            forced = await self._monitor(execution_id, container)
            result = await asyncio.to_thread(container.wait)
            exit_code = int(result.get("StatusCode", 1))
            stdout = _bounded(
                await asyncio.to_thread(container.logs, stdout=True, stderr=False)
            )
            stderr = _bounded(
                await asyncio.to_thread(container.logs, stdout=False, stderr=True)
            )
            target = forced or (
                ExecutionStatus.COMPLETED if exit_code == 0 else ExecutionStatus.FAILED
            )
            await self._finish(
                execution_id,
                target,
                stdout,
                stderr,
                exit_code,
                int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:
            logger.exception("Execution %s failed", execution_id)
            await self._finish(
                execution_id,
                ExecutionStatus.FAILED,
                "",
                str(exc),
                None,
                int((time.monotonic() - started) * 1000),
            )
        finally:
            await self.redis.delete(self.queue.cancellation_key(execution_id))
            if container is not None:
                with suppress(Exception):
                    await asyncio.to_thread(container.remove, force=True)
            if volume is not None:
                with suppress(Exception):
                    await asyncio.to_thread(volume.remove, force=True)

    async def run(self) -> None:
        logger.info("Waiting on %s", EXECUTION_QUEUE_KEY)
        while True:
            try:
                item = await self.redis.blpop(EXECUTION_QUEUE_KEY, timeout=0)
            except (RedisConnectionError, RedisTimeoutError):
                logger.warning("Redis queue unavailable; retrying")
                await asyncio.sleep(1)
                continue
            if item is None:
                continue
            try:
                execution_id = uuid.UUID(item[1])
            except ValueError:
                logger.warning("Ignoring invalid execution queue item")
                continue
            await self.process(execution_id)

    async def close(self) -> None:
        await self.redis.aclose()
        await asyncio.to_thread(self.docker.close)


async def main() -> None:
    worker = ExecutionWorker()
    try:
        await worker.run()
    finally:
        await worker.close()


if __name__ == "__main__":
    asyncio.run(main())
