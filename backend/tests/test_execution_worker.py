import io
import tarfile
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.config import settings
from app.execution.worker import (
    EXECUTION_QUEUE_KEY,
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
async def test_queue_timeout_is_retried(monkeypatch):
    redis = AsyncMock()
    redis.blpop.side_effect = [RedisTimeoutError, (EXECUTION_QUEUE_KEY, "invalid")]
    worker = ExecutionWorker(redis_client=redis, docker_client=AsyncMock())
    sleep = AsyncMock()
    monkeypatch.setattr("app.execution.worker.asyncio.sleep", sleep)

    with pytest.raises(StopAsyncIteration):
        redis.blpop.side_effect = [
            RedisTimeoutError,
            (EXECUTION_QUEUE_KEY, "invalid"),
            StopAsyncIteration,
        ]
        await worker.run()

    sleep.assert_awaited_once_with(1)
