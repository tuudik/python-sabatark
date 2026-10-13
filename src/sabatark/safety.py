"""Opt-in client-side allow-list for writes."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .errors import AgatarkError

SANDBOX_COLLECTIONS = frozenset({"users", "rules", "roles"})
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class WriteBlocked(AgatarkError):
    """A write was refused by the client-side WriteGuard."""


@dataclass(frozen=True, slots=True, init=False)
class WriteGuard:
    """Client-side allow-list: PATCH on the listed devices, and sandbox-named users/rules/roles."""

    devices: frozenset[int]
    sandbox_prefix: str | None
    sandbox_collections: frozenset[str]

    def __init__(
        self,
        devices: Iterable[int],
        sandbox_prefix: str | None,
        sandbox_collections: Iterable[str] = SANDBOX_COLLECTIONS,
    ) -> None:
        if sandbox_prefix is not None and not sandbox_prefix:
            raise ValueError("sandbox_prefix must be a non-empty string or None")
        object.__setattr__(self, "devices", frozenset(int(item) for item in devices))
        object.__setattr__(self, "sandbox_prefix", sandbox_prefix)
        object.__setattr__(self, "sandbox_collections", frozenset(sandbox_collections))

    def needs_current(self, method: str, collection: str | None, item_id: int | None) -> bool:
        """Whether check() needs the stored item to decide on this write."""
        return (
            self.sandbox_prefix is not None
            and method.upper() not in READ_METHODS
            and collection in self.sandbox_collections
            and item_id is not None
        )

    def check(
        self,
        method: str,
        collection: str | None,
        item_id: int | None = None,
        *,
        data: Any = None,
        current: Mapping[str, Any] | None = None,
    ) -> None:
        """Raise WriteBlocked unless the write targets an allowed device or sandbox item."""
        verb = method.upper()
        if verb in READ_METHODS or (collection == "events" and verb == "POST"):
            return
        if isinstance(data, Mapping) and item_id is not None and data.get("id", item_id) != item_id:
            raise WriteBlocked(f"Body id does not match {collection} {item_id}")
        if collection == "devices" and item_id in self.devices and verb == "PATCH":
            return
        if collection in self.sandbox_collections:
            if item_id is None and verb == "POST" and self._sandboxed(data):
                return
            renamed = isinstance(data, Mapping) and "name" in data
            if (
                item_id is not None
                and self._sandboxed(current)
                and (not renamed or self._sandboxed(data))
            ):
                return
        target = collection if item_id is None else f"{collection} {item_id}"
        raise WriteBlocked(f"{verb} {target} is not an allowed write target")

    def _sandboxed(self, document: Any) -> bool:
        return (
            self.sandbox_prefix is not None
            and isinstance(document, Mapping)
            and isinstance(document.get("name"), str)
            and document["name"].startswith(self.sandbox_prefix)
        )
