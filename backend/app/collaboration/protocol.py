"""Binary helpers for the ``y-websocket`` and Yjs sync protocols.

``y-websocket`` uses lib0 variable-length integers. Each WebSocket frame starts
with an outer message type. Sync and awareness messages then contain their own
length-prefixed payloads. Keeping this codec independent from FastAPI makes the
permission boundary testable before any bytes reach the CRDT document.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

MESSAGE_SYNC = 0
MESSAGE_AWARENESS = 1
MESSAGE_AUTH = 2
MESSAGE_QUERY_AWARENESS = 3
MESSAGE_DOCUMENT_STATUS = 4
MESSAGE_EXECUTION_EVENT = 5

SYNC_STEP1 = 0
SYNC_STEP2 = 1
SYNC_UPDATE = 2

AUTH_PERMISSION_DENIED = 0

MAX_SAFE_INTEGER = (1 << 53) - 1
MAX_MESSAGE_SIZE = 16 * 1024 * 1024
MAX_AWARENESS_ENTRIES = 10_000


class ProtocolError(ValueError):
    """Raised when a binary frame is not valid lib0/y-websocket data."""


@dataclass(frozen=True, slots=True)
class SyncMessage:
    """A Yjs sync submessage and its length-prefixed binary payload."""

    subtype: int
    payload: bytes

    @property
    def modifies_document(self) -> bool:
        """Whether applying this subtype can change the recipient document."""
        return self.subtype in (SYNC_STEP2, SYNC_UPDATE)


@dataclass(frozen=True, slots=True)
class AwarenessEntry:
    """One client state from a Yjs awareness update."""

    client_id: int
    clock: int
    state: object
    encoded_state: str


@dataclass(frozen=True, slots=True)
class AwarenessMessage:
    """A decoded awareness update containing zero or more client states."""

    entries: tuple[AwarenessEntry, ...]


@dataclass(frozen=True, slots=True)
class AwarenessQuery:
    """Request for every awareness state known by the server."""


@dataclass(frozen=True, slots=True)
class AuthMessage:
    """A y-protocols auth message (currently permission-denied only)."""

    subtype: int
    reason: str


type ParsedMessage = SyncMessage | AwarenessMessage | AwarenessQuery | AuthMessage


def encode_var_uint(value: int) -> bytes:
    """Encode a non-negative JavaScript-safe integer using lib0 varUint."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("varUint value must be an integer")
    if value < 0 or value > MAX_SAFE_INTEGER:
        raise ValueError("varUint value is outside the JavaScript-safe range")

    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value //= 128
    encoded.append(value)
    return bytes(encoded)


def decode_var_uint(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Decode one lib0 varUint and return ``(value, next_offset)``."""
    if offset < 0 or offset > len(data):
        raise ProtocolError("varUint offset is outside the frame")

    value = 0
    multiplier = 1
    bytes_read = 0
    while offset < len(data):
        bytes_read += 1
        if bytes_read > 8:
            raise ProtocolError("varUint uses more than 8 bytes")
        byte = data[offset]
        offset += 1
        value += (byte & 0x7F) * multiplier
        if value > MAX_SAFE_INTEGER:
            raise ProtocolError("varUint exceeds the JavaScript-safe range")
        if byte < 0x80:
            return value, offset
        multiplier *= 128

    raise ProtocolError("truncated varUint")


def encode_var_bytes(value: bytes) -> bytes:
    """Encode a byte string with a lib0 varUint length prefix."""
    return encode_var_uint(len(value)) + value


def decode_var_bytes(data: bytes, offset: int) -> tuple[bytes, int]:
    """Decode a lib0 length-prefixed byte string."""
    length, offset = decode_var_uint(data, offset)
    end = offset + length
    if end > len(data):
        raise ProtocolError("length-prefixed payload exceeds the frame")
    return data[offset:end], end


def _encode_var_string(value: str) -> bytes:
    return encode_var_bytes(value.encode("utf-8"))


def _decode_var_string(data: bytes, offset: int) -> tuple[str, int]:
    encoded, offset = decode_var_bytes(data, offset)
    try:
        return encoded.decode("utf-8"), offset
    except UnicodeDecodeError as exc:
        raise ProtocolError("string payload is not valid UTF-8") from exc


def _ensure_finished(data: bytes, offset: int) -> None:
    if offset != len(data):
        raise ProtocolError("unexpected trailing bytes")


def _reject_non_standard_json(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _decode_awareness_entries(payload: bytes) -> tuple[AwarenessEntry, ...]:
    count, offset = decode_var_uint(payload)
    if count > MAX_AWARENESS_ENTRIES:
        raise ProtocolError("awareness update contains too many entries")

    entries: list[AwarenessEntry] = []
    for _ in range(count):
        client_id, offset = decode_var_uint(payload, offset)
        clock, offset = decode_var_uint(payload, offset)
        encoded_state, offset = _decode_var_string(payload, offset)
        try:
            state = json.loads(
                encoded_state,
                parse_constant=_reject_non_standard_json,
            )
        except (RecursionError, ValueError) as exc:
            raise ProtocolError("awareness state is not valid JSON") from exc
        entries.append(
            AwarenessEntry(
                client_id=client_id,
                clock=clock,
                state=state,
                encoded_state=encoded_state,
            )
        )

    _ensure_finished(payload, offset)
    return tuple(entries)


def parse_message(frame: bytes) -> ParsedMessage:
    """Decode and strictly validate one complete y-websocket frame."""
    if not frame:
        raise ProtocolError("empty WebSocket frame")
    if len(frame) > MAX_MESSAGE_SIZE:
        raise ProtocolError("WebSocket frame exceeds the size limit")

    message_type, offset = decode_var_uint(frame)

    if message_type == MESSAGE_SYNC:
        subtype, offset = decode_var_uint(frame, offset)
        if subtype not in (SYNC_STEP1, SYNC_STEP2, SYNC_UPDATE):
            raise ProtocolError(f"unknown Yjs sync subtype: {subtype}")
        payload, offset = decode_var_bytes(frame, offset)
        _ensure_finished(frame, offset)
        return SyncMessage(subtype=subtype, payload=payload)

    if message_type == MESSAGE_AWARENESS:
        payload, offset = decode_var_bytes(frame, offset)
        _ensure_finished(frame, offset)
        return AwarenessMessage(entries=_decode_awareness_entries(payload))

    if message_type == MESSAGE_QUERY_AWARENESS:
        _ensure_finished(frame, offset)
        return AwarenessQuery()

    if message_type == MESSAGE_AUTH:
        subtype, offset = decode_var_uint(frame, offset)
        if subtype != AUTH_PERMISSION_DENIED:
            raise ProtocolError(f"unknown auth subtype: {subtype}")
        reason, offset = _decode_var_string(frame, offset)
        _ensure_finished(frame, offset)
        return AuthMessage(subtype=subtype, reason=reason)

    raise ProtocolError(f"unknown y-websocket message type: {message_type}")


def encode_sync_message(subtype: int, payload: bytes) -> bytes:
    """Build a framed Yjs sync message."""
    if subtype not in (SYNC_STEP1, SYNC_STEP2, SYNC_UPDATE):
        raise ValueError(f"unknown Yjs sync subtype: {subtype}")
    return (
        encode_var_uint(MESSAGE_SYNC)
        + encode_var_uint(subtype)
        + encode_var_bytes(payload)
    )


def encode_sync_step1(state_vector: bytes) -> bytes:
    return encode_sync_message(SYNC_STEP1, state_vector)


def encode_sync_step2(update: bytes) -> bytes:
    return encode_sync_message(SYNC_STEP2, update)


def encode_sync_update(update: bytes) -> bytes:
    return encode_sync_message(SYNC_UPDATE, update)


def encode_awareness_message(entries: tuple[AwarenessEntry, ...]) -> bytes:
    """Build a framed awareness update from validated entries."""
    payload = bytearray(encode_var_uint(len(entries)))
    for entry in entries:
        payload.extend(encode_var_uint(entry.client_id))
        payload.extend(encode_var_uint(entry.clock))
        payload.extend(_encode_var_string(entry.encoded_state))
    return encode_var_uint(MESSAGE_AWARENESS) + encode_var_bytes(bytes(payload))


def encode_permission_denied(reason: str) -> bytes:
    """Build the auth frame understood by y-websocket's warning handler."""
    return (
        encode_var_uint(MESSAGE_AUTH)
        + encode_var_uint(AUTH_PERMISSION_DENIED)
        + _encode_var_string(reason)
    )


def encode_document_saved(
    *,
    saved_at: str | None,
    state_vector: str | None,
    state_hash: str,
    dirty: bool,
    saved_by: str | None,
) -> bytes:
    """Build the custom server-to-client document persistence event.

    The outer message type lives outside the standard y-websocket range while
    the payload still uses lib0's var-string encoding. Clients compare the
    supplied state vector with their current Y.Doc before showing "Saved" so a
    delayed response cannot hide newer collaborative edits.
    """
    payload = json.dumps(
        {
            "type": "document_saved",
            "saved_at": saved_at,
            "state_vector": state_vector,
            "state_hash": state_hash,
            "dirty": dirty,
            "saved_by": saved_by,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return encode_var_uint(MESSAGE_DOCUMENT_STATUS) + _encode_var_string(payload)


def encode_execution_event(event: dict[str, object]) -> bytes:
    """Build a custom server-to-client execution event frame."""
    payload = json.dumps(event, separators=(",", ":"), sort_keys=True)
    return encode_var_uint(MESSAGE_EXECUTION_EVENT) + _encode_var_string(payload)
