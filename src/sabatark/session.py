"""Persistable login sessions and their per-consumer session files."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .auth import normalize_email


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionState:
    """A logged-in session that can be stored and restored without logging in again."""

    authorization: str = field(repr=False)
    user_id: int | None
    a: str | None = None
    base_url: str
    email: str
    created: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "authorization": self.authorization,
            "user_id": self.user_id,
            "a": self.a,
            "base_url": self.base_url,
            "email": self.email,
            "created": self.created,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SessionState:
        if not isinstance(data, Mapping):
            raise ValueError("Session state must be a JSON object")
        authorization = data.get("authorization")
        user_id = data.get("user_id")
        a = data.get("a")
        base_url = data.get("base_url")
        email = data.get("email")
        created = data.get("created")
        if (
            not isinstance(authorization, str)
            or not authorization
            or not (user_id is None or (isinstance(user_id, int) and not isinstance(user_id, bool)))
            or not (a is None or isinstance(a, str))
            or not isinstance(base_url, str)
            or not base_url
            or not isinstance(email, str)
            or not email
            or not _number(created)
        ):
            raise ValueError("Invalid session state")
        return cls(
            authorization=authorization,
            user_id=user_id,
            a=a or None,
            base_url=base_url,
            email=email,
            created=created,
        )


def session_path(base_url: str, email: str, consumer: str) -> Path:
    """Return the session file of one consumer, such as "cli"."""
    configured = os.environ.get("XDG_CONFIG_HOME", "")
    root = Path(configured) if os.path.isabs(configured) else Path.home() / ".config"
    key = f"{base_url.rstrip('/')}|{normalize_email(email)}"
    digest = hashlib.sha256(key.encode()).hexdigest()[:12]
    return root / "sabatark" / f"session-{digest}-{consumer}.json"


def load_session(path: Path) -> SessionState | None:
    """Read a session file; None when it is missing, unreadable or invalid."""
    try:
        return SessionState.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except OSError, ValueError:
        return None


def save_session(path: Path, state: SessionState) -> None:
    """Atomically write a session file as 0600 inside a 0700 directory."""
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state.to_dict(), handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def delete_session(path: Path) -> None:
    """Remove a session file if it exists."""
    path.unlink(missing_ok=True)


async def persist_session(path: Path, state: SessionState | None) -> None:
    """Save state to path, or delete the file when state is None."""
    if state is None:
        await asyncio.to_thread(delete_session, path)
    else:
        await asyncio.to_thread(save_session, path, state)
