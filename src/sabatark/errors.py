"""Exceptions raised by the Agatark client."""

from typing import Any


class AgatarkError(Exception):
    """Base error for every failure reported by the client."""

    def __init__(self, message: str = "", *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message or (f"HTTP {status}" if status is not None else "Agatark error"))
        self.status = status
        self.body = body


class AuthError(AgatarkError):
    """Login failed, credentials are missing, or the session could not be re-established."""


class PasswordChangeRequired(AuthError):
    """The controller demands a password change (HTTP 454)."""


class ConfirmationRequired(AgatarkError):
    """The controller asks for confirmation (HTTP 406); retry with confirm=<action>."""

    def __init__(self, dialog: dict[str, Any] | None = None, *, status: int = 406) -> None:
        self.dialog: dict[str, Any] = dialog if isinstance(dialog, dict) else {}
        actions = self.dialog.get("actions")
        if not isinstance(actions, list):
            actions = []
        self.actions: list[dict[str, Any]] = [item for item in actions if isinstance(item, dict)]
        self.action: str | None = next(
            (str(item["action"]) for item in self.actions if item.get("action")), None
        )
        title = self.dialog.get("title") or self.dialog.get("message") or "Confirmation required"
        super().__init__(str(title), status=status, body=self.dialog)


class UplinkError(AgatarkError):
    """The controller lost its link to the main controller (HTTP 504)."""
