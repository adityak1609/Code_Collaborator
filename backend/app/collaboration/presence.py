"""Presence and awareness protocol handler.

Manages cursor positions, selections, and online status for
all users in a collaborative session. Uses the Yjs awareness
protocol format for compatibility with y-websocket clients.
"""

import hashlib
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.collaboration.protocol import AwarenessEntry

logger = logging.getLogger(__name__)

# Stale awareness timeout (seconds)
AWARENESS_TIMEOUT = 30

# Deterministic color palette for cursor colors
CURSOR_COLORS = [
    "#3b82f6",  # blue
    "#ef4444",  # red
    "#10b981",  # green
    "#f59e0b",  # amber
    "#8b5cf6",  # violet
    "#ec4899",  # pink
    "#06b6d4",  # cyan
    "#f97316",  # orange
    "#14b8a6",  # teal
    "#6366f1",  # indigo
]


def color_for_user(user_id: str) -> str:
    """Deterministic color assignment from user ID hash."""
    hash_val = int(hashlib.md5(user_id.encode()).hexdigest(), 16)
    return CURSOR_COLORS[hash_val % len(CURSOR_COLORS)]


@dataclass
class UserPresence:
    """Tracks a single user's awareness state in a session."""

    user_id: str
    username: str
    color: str
    cursor: dict | None = None  # {line, column}
    selection: dict | None = None  # {startLine, startColumn, endLine, endColumn}
    last_seen: float = field(default_factory=time.time)


class PresenceManager:
    """Per-session presence tracker.

    Stores the latest awareness state for each connected user
    and provides serialization for broadcast.
    """

    def __init__(self) -> None:
        # session_id -> {user_id -> UserPresence}
        self._sessions: dict[str, dict[str, UserPresence]] = {}

    def join(self, session_id: str, user_id: str, username: str) -> dict:
        """Register a user as present in a session. Returns their awareness state."""
        if session_id not in self._sessions:
            self._sessions[session_id] = {}

        presence = UserPresence(
            user_id=user_id,
            username=username,
            color=color_for_user(user_id),
        )
        self._sessions[session_id][user_id] = presence
        logger.info("User %s joined session %s presence", username, session_id)

        return self._serialize_presence(presence)

    def leave(self, session_id: str, user_id: str) -> None:
        """Remove a user from session presence."""
        if session_id in self._sessions:
            self._sessions[session_id].pop(user_id, None)
            if not self._sessions[session_id]:
                del self._sessions[session_id]

    def update_cursor(
        self,
        session_id: str,
        user_id: str,
        cursor: dict | None = None,
        selection: dict | None = None,
    ) -> None:
        """Update a user's cursor position and/or selection."""
        session_users = self._sessions.get(session_id, {})
        presence = session_users.get(user_id)
        if presence is None:
            return

        if cursor is not None:
            presence.cursor = cursor
        if selection is not None:
            presence.selection = selection
        presence.last_seen = time.time()

    def get_all_presence(self, session_id: str) -> list[dict]:
        """Get all users' presence states for a session."""
        session_users = self._sessions.get(session_id, {})
        now = time.time()
        result = []
        stale = []

        for user_id, presence in session_users.items():
            if now - presence.last_seen > AWARENESS_TIMEOUT:
                stale.append(user_id)
            else:
                result.append(self._serialize_presence(presence))

        # Clean up stale entries
        for user_id in stale:
            del session_users[user_id]
            logger.debug("Removed stale presence for user %s", user_id)

        return result

    def get_member_count(self, session_id: str) -> int:
        """Number of online users in a session."""
        return len(self._sessions.get(session_id, {}))

    @staticmethod
    def _serialize_presence(presence: UserPresence) -> dict:
        return {
            "user_id": presence.user_id,
            "username": presence.username,
            "color": presence.color,
            "cursor": presence.cursor,
            "selection": presence.selection,
        }


# Singleton
presence_manager = PresenceManager()


@dataclass(slots=True)
class _StoredAwareness:
    entry: AwarenessEntry
    owner: object | None
    last_seen: float


class AwarenessStore:
    """Track protocol-native Yjs awareness states for active rooms.

    Awareness clocks are compared using the same rules as ``y-protocols``:
    newer clocks replace older states, while an equal-clock ``null`` state can
    remove an active client. The owner is the WebSocket that most recently
    supplied a state and lets us emit the required removal update on disconnect.
    """

    def __init__(self, stale_timeout: float = AWARENESS_TIMEOUT) -> None:
        self._sessions: dict[str, dict[int, _StoredAwareness]] = {}
        self._stale_timeout = stale_timeout

    def apply_update(
        self,
        session_id: str,
        owner: object,
        entries: Sequence[AwarenessEntry],
        *,
        now: float | None = None,
    ) -> tuple[AwarenessEntry, ...]:
        """Apply entries and return only states accepted by awareness clocks."""
        timestamp = time.monotonic() if now is None else now
        states = self._sessions.setdefault(session_id, {})
        accepted: list[AwarenessEntry] = []

        for entry in entries:
            current = states.get(entry.client_id)
            current_clock = current.entry.clock if current is not None else 0
            current_is_active = current is not None and current.entry.state is not None
            is_removal = entry.state is None
            should_apply = current_clock < entry.clock or (
                current_clock == entry.clock and is_removal and current_is_active
            )
            if not should_apply:
                continue

            states[entry.client_id] = _StoredAwareness(
                entry=entry,
                owner=None if is_removal else owner,
                last_seen=timestamp,
            )
            accepted.append(entry)

        return tuple(accepted)

    def active_entries(self, session_id: str) -> tuple[AwarenessEntry, ...]:
        """Return the room's currently active states in stable client-ID order."""
        states = self._sessions.get(session_id, {})
        return tuple(
            stored.entry
            for _, stored in sorted(states.items())
            if stored.entry.state is not None
        )

    def remove_owner(
        self,
        session_id: str,
        owner: object,
    ) -> tuple[AwarenessEntry, ...]:
        """Remove live client IDs controlled by a disconnecting WebSocket."""
        states = self._sessions.get(session_id, {})
        removed: list[AwarenessEntry] = []
        timestamp = time.monotonic()

        for client_id, stored in states.items():
            if stored.owner is not owner or stored.entry.state is None:
                continue
            removal = AwarenessEntry(
                client_id=client_id,
                clock=stored.entry.clock,
                state=None,
                encoded_state="null",
            )
            stored.entry = removal
            stored.owner = None
            stored.last_seen = timestamp
            removed.append(removal)

        return tuple(removed)

    def prune_stale(
        self,
        session_id: str,
        *,
        now: float | None = None,
    ) -> tuple[AwarenessEntry, ...]:
        """Mark awareness states without a heartbeat as removed."""
        timestamp = time.monotonic() if now is None else now
        states = self._sessions.get(session_id, {})
        removed: list[AwarenessEntry] = []

        for client_id, stored in states.items():
            if (
                stored.entry.state is None
                or timestamp - stored.last_seen <= self._stale_timeout
            ):
                continue
            removal = AwarenessEntry(
                client_id=client_id,
                clock=stored.entry.clock,
                state=None,
                encoded_state="null",
            )
            stored.entry = removal
            stored.owner = None
            stored.last_seen = timestamp
            removed.append(removal)

        return tuple(removed)

    def discard_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


awareness_store = AwarenessStore()
