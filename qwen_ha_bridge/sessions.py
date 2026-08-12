"""Small bounded session registry for legacy MCP clients."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass


@dataclass(slots=True)
class Session:
    expires_at: float
    initialized: bool = False


class SessionStore:
    def __init__(self, *, ttl_seconds: int, max_sessions: int) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self._sessions: dict[str, Session] = {}

    def create(self) -> str:
        self.prune()
        if len(self._sessions) >= self.max_sessions:
            oldest = min(self._sessions, key=lambda key: self._sessions[key].expires_at)
            del self._sessions[oldest]
        session_id = secrets.token_urlsafe(32)
        self._sessions[session_id] = Session(time.monotonic() + self.ttl_seconds)
        return session_id

    def touch(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        session = self._sessions.get(session_id)
        if session is None:
            return False
        if session.expires_at <= time.monotonic():
            del self._sessions[session_id]
            return False
        session.expires_at = time.monotonic() + self.ttl_seconds
        return True

    def mark_initialized(self, session_id: str | None) -> bool:
        if not session_id or not self.touch(session_id):
            return False
        self._sessions[session_id].initialized = True
        return True

    def is_initialized(self, session_id: str | None) -> bool:
        if not session_id or not self.touch(session_id):
            return False
        session = self._sessions.get(session_id)
        return bool(session and session.initialized)

    def delete(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        return self._sessions.pop(session_id, None) is not None

    def prune(self) -> None:
        now = time.monotonic()
        expired = [key for key, session in self._sessions.items() if session.expires_at <= now]
        for key in expired:
            del self._sessions[key]

    def __len__(self) -> int:
        self.prune()
        return len(self._sessions)
