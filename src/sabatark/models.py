"""Typed views over Agatark API documents."""

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

OUTPUT_TYPES = frozenset({"output", "light", "outputPwm"})


def merge_patch(target: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deep copy of target with patch merged in; a None value deletes the key."""
    result = copy.deepcopy(dict(target))
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = merge_patch(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


@dataclass(frozen=True, slots=True)
class Hello:
    """Reply of PUT /hello: session credential, user and bootstrap lists."""

    authorization: str | None = field(default=None, repr=False)
    a: str | None = None
    user: dict[str, Any] | None = None
    version: str | None = None
    node_name: str | None = None
    sites: list[dict[str, Any]] = field(default_factory=list, repr=False)
    rooms: list[dict[str, Any]] = field(default_factory=list, repr=False)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def user_id(self) -> int | None:
        return _int((self.user or {}).get("id"))

    @property
    def logged_in(self) -> bool:
        return bool(self.user)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Hello:
        user = data.get("user")
        return cls(
            authorization=data.get("authorization") or None,
            a=data.get("a") or None,
            user=dict(user) if isinstance(user, Mapping) and user else None,
            version=data.get("version"),
            node_name=data.get("nodeName"),
            sites=[dict(item) for item in data.get("sites") or [] if isinstance(item, Mapping)],
            rooms=[dict(item) for item in data.get("rooms") or [] if isinstance(item, Mapping)],
            raw=dict(data),
        )


@dataclass(frozen=True, slots=True)
class Device:
    """One entry of GET /devices."""

    id: int
    name: str = ""
    type: str = ""
    enabled: bool | None = None
    room_id: int | None = None
    site_id: int | None = None
    uuid: str | None = None
    usage: dict[str, list[str]] = field(default_factory=dict)
    conf: list[dict[str, Any]] = field(default_factory=list, repr=False)
    status: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Device:
        usage = {
            str(key): [str(item) for item in value] if isinstance(value, list) else []
            for key, value in _dict(data.get("usage")).items()
        }
        conf = data.get("conf")
        return cls(
            id=int(data["id"]),
            name=str(data.get("name") or ""),
            type=str(data.get("type") or ""),
            enabled=data.get("enabled"),
            room_id=_int(data.get("roomId")),
            site_id=_int(data.get("siteId")),
            uuid=data.get("uuid"),
            usage=usage,
            conf=[dict(item) for item in conf if isinstance(item, Mapping)]
            if isinstance(conf, list)
            else [],
            status=_dict(data.get("status")),
            raw=dict(data),
        )

    def apply(self, event: EventDelta) -> Device:
        """Return a copy with an event delta merged in."""
        if event.id != self.id:
            raise ValueError(f"Event for item {event.id} applied to device {self.id}")
        return Device.from_dict(merge_patch(self.raw, event.patch))

    @property
    def is_output(self) -> bool:
        return self.type in OUTPUT_TYPES or (not self.type and "output" in self.status)

    @property
    def is_switch(self) -> bool:
        return self.type == "switch" or "switch" in self.usage

    @property
    def is_motion(self) -> bool:
        return "motion" in self.usage.get("securityDetector", []) or "motion" in self.usage.get(
            "securityDetector24", []
        )


@dataclass(frozen=True, slots=True)
class Site:
    """One entry of GET /sites."""

    id: int
    name: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Site:
        return cls(id=int(data["id"]), name=str(data.get("name") or ""), raw=dict(data))


@dataclass(frozen=True, slots=True)
class Room:
    """One entry of GET /rooms."""

    id: int
    name: str = ""
    site_id: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Room:
        return cls(
            id=int(data["id"]),
            name=str(data.get("name") or ""),
            site_id=_int(data.get("siteId")),
            raw=dict(data),
        )


@dataclass(frozen=True, slots=True)
class EventDelta:
    """Partial item from /events, to deep-merge into the cached item `id`."""

    id: int
    status: dict[str, Any] | None = None
    setup: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def patch(self) -> dict[str, Any]:
        return {key: value for key, value in self.raw.items() if key != "id"}


@dataclass(frozen=True, slots=True)
class EventLog:
    """Gesture log from /events, e.g. singleClick:/status/input on device `id`."""

    id: int
    time: int
    by: int | None
    key: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def gesture(self) -> str:
        return self.key.partition(":")[0]

    @property
    def path(self) -> str:
        return self.key.partition(":")[2]

    @property
    def timestamp(self) -> datetime:
        return datetime.fromtimestamp(self.time / 1000, UTC)


@dataclass(frozen=True, slots=True)
class EventOp:
    """Structural /events item (add, remove, api.reload) or any unrecognised item."""

    op: str
    id: int | None = None
    path: str | None = None
    value: Any = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


Event = EventDelta | EventLog | EventOp


def parse_event(item: Any) -> Event:
    """Map one raw /events item to EventDelta, EventLog or EventOp without ever raising."""
    if not isinstance(item, Mapping):
        return EventOp(op="", value=item, raw={"value": item})
    raw = dict(item)
    op = raw.get("op")
    item_id = _int(raw.get("id"))
    if op is None:
        if item_id is not None:
            status = raw.get("status")
            setup = raw.get("setup")
            return EventDelta(
                id=item_id,
                status=dict(status) if isinstance(status, Mapping) else None,
                setup=dict(setup) if isinstance(setup, Mapping) else None,
                raw=raw,
            )
        return EventOp(op="", raw=raw)
    time = _int(raw.get("time"))
    key = raw.get("key")
    if op == "log" and item_id is not None and time is not None and isinstance(key, str):
        return EventLog(id=item_id, time=time, by=_int(raw.get("by")), key=key, raw=raw)
    path = raw.get("path")
    return EventOp(
        op=str(op),
        id=item_id,
        path=path if isinstance(path, str) else None,
        value=raw.get("value"),
        raw=raw,
    )


@dataclass(frozen=True, slots=True)
class LogEntry:
    """One entry of GET /logs."""

    id: int | None
    time: int
    key: str
    by: int | None = None
    model: int | None = None
    value: Any = None
    meta: Any = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def timestamp(self) -> datetime:
        return datetime.fromtimestamp(self.time / 1000, UTC)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LogEntry:
        return cls(
            id=_int(data.get("id")),
            time=_int(data.get("time")) or 0,
            key=str(data.get("key") or ""),
            by=_int(data.get("by")),
            model=_int(data.get("model")),
            value=data.get("value"),
            meta=data.get("meta"),
            raw=dict(data),
        )
