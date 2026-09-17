"""Fast unit tests for core business rules that do not require external services."""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from jose import JWTError

from app import main as main_module
from app.auth.service import (
    create_access_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.collaboration.presence import PresenceManager, color_for_user
from app.config import Settings, settings
from app.main import _RedactTokenQueryFilter
from app.models import Execution, ExecutionStatus, LanguageEnum


def test_password_hash_round_trip() -> None:
    password = "correct horse battery staple"

    encoded = hash_password(password)

    assert encoded != password
    assert verify_password(password, encoded)
    assert not verify_password("incorrect", encoded)


def test_access_token_round_trip() -> None:
    token = create_access_token(
        user_id="8ea8fb4b-14b1-4b17-915b-012fbb42525e",
        username="ada",
    )

    payload = decode_token(token)

    assert payload["sub"] == "8ea8fb4b-14b1-4b17-915b-012fbb42525e"
    assert payload["username"] == "ada"
    assert "exp" in payload


def test_expired_access_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_expire_minutes", -1)
    token = create_access_token(
        user_id="8ea8fb4b-14b1-4b17-915b-012fbb42525e",
        username="ada",
    )

    with pytest.raises(JWTError):
        decode_token(token)


def test_websocket_tokens_are_redacted_from_log_arguments() -> None:
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "WebSocket %s" [accepted]',
        args=("127.0.0.1:1234", "/ws/room?token=secret-token&mode=test"),
        exc_info=None,
    )

    assert _RedactTokenQueryFilter().filter(record)
    assert "secret-token" not in record.getMessage()
    assert "token=[REDACTED]&mode=test" in record.getMessage()


def test_cors_origins_can_be_configured_from_json_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CORS_ORIGINS",
        '["https://demo.example.com","https://review.example.com"]',
    )

    configured = Settings(_env_file=None)

    assert configured.cors_origins == [
        "https://demo.example.com",
        "https://review.example.com",
    ]


def test_execution_state_machine_accepts_forward_transitions() -> None:
    execution = Execution(
        status=ExecutionStatus.QUEUED,
        session_id="49ab32d9-25ca-420a-b79b-d55b93146ce8",
        triggered_by="8ea8fb4b-14b1-4b17-915b-012fbb42525e",
        code="print('ok')",
        language=LanguageEnum.python,
    )

    execution.transition_to(ExecutionStatus.STARTING)
    execution.transition_to(ExecutionStatus.RUNNING)
    execution.transition_to(ExecutionStatus.COMPLETED)

    assert execution.status is ExecutionStatus.COMPLETED


def test_execution_state_machine_rejects_invalid_transition() -> None:
    execution = Execution(
        status=ExecutionStatus.QUEUED,
        session_id="49ab32d9-25ca-420a-b79b-d55b93146ce8",
        triggered_by="8ea8fb4b-14b1-4b17-915b-012fbb42525e",
        code="print('ok')",
        language=LanguageEnum.python,
    )

    with pytest.raises(ValueError, match="Invalid transition"):
        execution.transition_to(ExecutionStatus.COMPLETED)


def test_presence_join_update_and_leave() -> None:
    manager = PresenceManager()
    session_id = "session-1"
    user_id = "user-1"

    joined = manager.join(session_id, user_id, "Ada")
    manager.update_cursor(
        session_id,
        user_id,
        cursor={"line": 2, "column": 4},
        selection={"startLine": 2, "endLine": 3},
    )

    active = manager.get_all_presence(session_id)
    assert joined["color"] == color_for_user(user_id)
    assert active[0]["cursor"] == {"line": 2, "column": 4}
    assert active[0]["selection"] == {"startLine": 2, "endLine": 3}

    manager.leave(session_id, user_id)
    assert manager.get_all_presence(session_id) == []


@pytest.mark.asyncio
async def test_periodic_flush_drains_in_flight_checkpoint_before_stopping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    allow_finish = asyncio.Event()

    async def checkpoint(_session_id: str) -> None:
        started.set()
        await allow_finish.wait()

    monkeypatch.setattr(settings, "crdt_flush_interval_seconds", 0.001)
    monkeypatch.setattr(
        main_module,
        "doc_manager",
        SimpleNamespace(active_sessions=["room"]),
    )
    monkeypatch.setattr(main_module, "maintain_document_checkpoint", checkpoint)
    stop_event = asyncio.Event()
    task = asyncio.create_task(main_module._periodic_crdt_flush(stop_event))

    await asyncio.wait_for(started.wait(), timeout=1)
    stop_event.set()
    await asyncio.sleep(0)
    assert task.done() is False

    allow_finish.set()
    await asyncio.wait_for(task, timeout=1)
