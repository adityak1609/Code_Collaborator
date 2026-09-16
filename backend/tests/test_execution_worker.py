import io
import tarfile
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.config import settings
from app.execution.worker import (
    RUNTIMES,
    ExecutionWorker,
    _bounded,
    _code_archive,
)
from app.models import LanguageEnum


def test_all_languages_have_runtime_specs():
    assert set(RUNTIMES) == set(LanguageEnum)
    assert RUNTIMES[LanguageEnum.python].command[0] == "python3"
    assert RUNTIMES[LanguageEnum.javascript].command[0] == "node"
    assert "g++" in RUNTIMES[LanguageEnum.cpp].command[-1]


def test_code_archive_is_read_only_and_exact():
    archive = _code_archive("main.py", "print('safe')")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r") as tar:
        member = tar.getmember("main.py")
        assert member.mode == 0o444
        extracted = tar.extractfile(member)
        assert extracted is not None
        assert extracted.read() == b"print('safe')"


def test_output_is_bounded(monkeypatch):
    monkeypatch.setattr(settings, "execution_output_limit_bytes", 32)
    result = _bounded(b"x" * 100)
    assert len(result.encode()) <= 32
    assert result.endswith("[output truncated]\n")


@pytest.mark.asyncio
async def test_sandbox_container_has_security_and_resource_limits():
    docker_client = Mock()
    volume = Mock(name="volume")
    volume.name = "execution-volume"
    writer = Mock()
    sandbox = Mock()
    docker_client.volumes.create.return_value = volume
    docker_client.containers.create.side_effect = [writer, sandbox]
    worker = ExecutionWorker(redis_client=AsyncMock(), docker_client=docker_client)
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        language=LanguageEnum.python,
        code="print('safe')",
    )

    created, created_volume = await worker._create_container(execution)

    assert created is sandbox
    assert created_volume is volume
    options = docker_client.containers.create.call_args_list[1].kwargs
    assert options["network_disabled"] is True
    assert options["network_mode"] == "none"
    assert options["read_only"] is True
    assert options["user"] == "65534:65534"
    assert options["cap_drop"] == ["ALL"]
    assert options["security_opt"] == ["no-new-privileges:true"]
    assert options["mem_limit"] == settings.execution_memory_limit
    assert options["memswap_limit"] == settings.execution_memory_limit
    assert options["nano_cpus"] == settings.execution_cpu_limit * 1_000_000_000
    assert options["pids_limit"] == settings.execution_pids_limit
    assert options["tmpfs"] == {"/tmp": "rw,exec,nosuid,size=64m"}


@pytest.mark.asyncio
async def test_queue_timeout_is_retried(monkeypatch):
    redis = AsyncMock()
    redis.blmove.side_effect = [RedisTimeoutError, "invalid", StopAsyncIteration]
    worker = ExecutionWorker(redis_client=redis, docker_client=Mock())
    worker._cleanup_orphans = AsyncMock()
    worker._recover_processing = AsyncMock()
    sleep = AsyncMock()
    monkeypatch.setattr("app.execution.worker.asyncio.sleep", sleep)

    with pytest.raises(StopAsyncIteration):
        await worker.run()

    sleep.assert_awaited_once_with(1)
    redis.lrem.assert_awaited_once()
