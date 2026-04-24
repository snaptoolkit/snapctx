"""Session management for the sample package."""

from __future__ import annotations

from dataclasses import dataclass

from sample_pkg.utils import hash_token


@dataclass
class Session:
    """An active user session."""

    user_id: str
    token: str


class SessionManager:
    """Creates, refreshes, and invalidates user sessions."""

    def __init__(self, salt: str) -> None:
        self.salt = salt
        self._sessions: dict[str, Session] = {}

    def login(self, user_id: str, password: str) -> Session:
        """Log a user in and return a fresh Session."""
        token = hash_token(password, salt=self.salt)
        session = Session(user_id=user_id, token=token)
        self._sessions[token] = session
        return session

    def refresh(self, token: str, *, force: bool = False) -> Session:
        """Refresh an expired session token.

        Raises KeyError if the token isn't known.
        """
        session = self._sessions[token]
        new_token = hash_token(session.user_id, salt=self.salt + "refresh")
        session.token = new_token
        return session

    def logout(self, token: str) -> None:
        """Invalidate a session token."""
        self._sessions.pop(token, None)
