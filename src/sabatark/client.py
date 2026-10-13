"""Async client for the Agatark/EnLife controller HTTP API."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import aiohttp
from yarl import URL

from .auth import derive_token, normalize_email
from .errors import (
    AgatarkError,
    AuthError,
    ConfirmationRequired,
    PasswordChangeRequired,
    UplinkError,
)
from .models import Device, Event, Hello, LogEntry, Room, Site, parse_event
from .safety import WriteGuard
from .session import SessionState

COLLECTIONS = frozenset({"devices", "sites", "rooms", "rules", "users", "roles", "securityAreas"})

_LOGGER = logging.getLogger(__name__)
_QUERY_SAFE = "/,:@$!*'()[]{}|~"
_LINK_NEXT = re.compile(r'<([^>]*)>\s*;[^,]*\brel="?next"?', re.IGNORECASE)
_SITE_TARGET = re.compile(r"/sites\?id=(\d+)/(\w+)(?:\?id=(\d+))?")
_PLAIN_TARGET = re.compile(r"/(\w+)(?:\?(.*))?")


@dataclass(frozen=True, slots=True)
class _Reply:
    status: int
    data: Any
    next_link: str | None


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple | set | frozenset):
        return ",".join(_format_value(item) for item in value)
    return str(value)


def _query(params: Mapping[str, Any]) -> str:
    pairs = [
        f"{quote(str(key), safe=_QUERY_SAFE)}={quote(_format_value(value), safe=_QUERY_SAFE)}"
        for key, value in params.items()
        if value is not None
    ]
    return "?" + "&".join(pairs) if pairs else ""


def _collection(name: str) -> str:
    if name not in COLLECTIONS:
        raise ValueError(f"Unknown collection {name!r}; expected one of {sorted(COLLECTIONS)}")
    return name


def _target(path: str) -> tuple[str | None, int | None]:
    match = _SITE_TARGET.fullmatch(path)
    if match:
        return match.group(2), int(match.group(3)) if match.group(3) else None
    match = _PLAIN_TARGET.fullmatch(path)
    if not match:
        return None, None
    ids = parse_qs(match.group(2) or "").get("id", [])
    item_id = int(ids[0]) if len(ids) == 1 and ids[0].isdigit() else None
    return match.group(1), item_id


def _relative(link: str) -> str:
    parts = urlsplit(link)
    path = parts.path if parts.path.startswith("/") else "/" + parts.path
    return path + (f"?{parts.query}" if parts.query else "")


def _error_message(data: Any, status: int) -> str:
    if isinstance(data, Mapping):
        for key in ("title", "message", "error"):
            if isinstance(data.get(key), str) and data[key]:
                return f"HTTP {status}: {data[key]}"
    if isinstance(data, str) and data.strip():
        return f"HTTP {status}: {data.strip()[:200]}"
    return f"HTTP {status}"


def _check(reply: _Reply) -> None:
    status = reply.status
    if 200 <= status < 300:
        return
    message = _error_message(reply.data, status)
    if status == 401:
        raise AuthError(message, status=status, body=reply.data)
    if status == 454:
        raise PasswordChangeRequired(message, status=status, body=reply.data)
    if status == 406:
        raise ConfirmationRequired(reply.data if isinstance(reply.data, dict) else None)
    if status == 504:
        raise UplinkError(message, status=status, body=reply.data)
    raise AgatarkError(message, status=status, body=reply.data)


class AgatarkClient:
    """One authenticated session against an Agatark controller."""

    session_probe_interval: float = 300.0
    empty_poll_interval: float = 1.0

    def __init__(
        self,
        base_url: str,
        email: str | None = None,
        password: str | None = None,
        token: str | None = None,
        session: aiohttp.ClientSession | None = None,
        guard: WriteGuard | None = None,
        *,
        timeout: float = 30.0,
        events_timeout: float = 120.0,
        remember: bool = True,
        session_state: SessionState | None = None,
        on_session_change: Callable[[SessionState | None], Awaitable[None] | None] | None = None,
        on_connection_change: Callable[[bool], Awaitable[None] | None] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.guard = guard
        self._email = email
        self._password = password
        self._token = token
        self._remember = remember
        self._on_session_change = on_session_change
        self._on_connection_change = on_connection_change
        self._connected: bool | None = None
        self._session = session
        self._owns_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._events_timeout = aiohttp.ClientTimeout(total=events_timeout)
        self._authorization: str | None = None
        self._a: str | None = None
        self._hello: Hello | None = None
        self._user_id: int | None = None
        self._state: SessionState | None = None
        self._generation = 0
        self._auth_lock = asyncio.Lock()
        self._events_active = False
        self._last_probe = float("-inf")
        if session_state is not None:
            if self._matches(session_state):
                self._authorization = session_state.authorization
                self._a = session_state.a
                self._user_id = session_state.user_id
                self._state = session_state
            else:
                _LOGGER.debug("Ignoring a stored session for another controller or user")

    def __repr__(self) -> str:
        return f"AgatarkClient({self.base_url!r}, logged_in={self.logged_in})"

    async def __aenter__(self) -> AgatarkClient:
        self._http()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self, logout: bool = False) -> None:
        """Close the HTTP session when the client owns it; log out first only if asked."""
        if logout and self._authorization is not None:
            try:
                await self.logout()
            except AgatarkError, aiohttp.ClientError, TimeoutError:
                _LOGGER.debug("Logout during close failed", exc_info=True)
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def logged_in(self) -> bool:
        return self._authorization is not None

    @property
    def last_hello(self) -> Hello | None:
        return self._hello

    @property
    def session_state(self) -> SessionState | None:
        return self._state

    @property
    def connected(self) -> bool | None:
        """None before the first request completes, then whether the controller last answered."""
        return self._connected

    async def login(self) -> Hello:
        """PUT /hello with the derived token; stores the reply's authorization."""
        async with self._auth_lock:
            return await self._login()

    async def logout(self) -> None:
        """DELETE /hello and forget the session."""
        try:
            if self._authorization is not None:
                _check(await self._send("DELETE", "/hello"))
        finally:
            self._forget()
            await self._notify(None)

    async def hello(self) -> Hello:
        """Server bootstrap; on this firmware returns user=None unless called with login
        credentials."""
        reply = await self._send("PUT", "/hello", {"a": self._a} if self._a else None)
        if reply.status != 401:
            _check(reply)
        hello = Hello.from_dict(reply.data if isinstance(reply.data, Mapping) else {})
        return replace(hello, user=None) if reply.status == 401 else hello

    async def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        confirm: str | None = None,
    ) -> Any:
        """Send one API request with session auth, re-login and the write guard applied."""
        if not path.startswith("/"):
            raise ValueError("path must start with '/'")
        verb = method.upper()
        collection, item_id = _target(path)
        await self._guard(verb, collection, item_id, body)
        reply = await self._call(verb, path, body, confirm=confirm, check_empty=verb == "GET")
        return reply.data

    async def list(self, collection: str, **query: Any) -> list[dict[str, Any]]:
        """GET /<collection> with optional query parameters."""
        return await self._list(f"/{_collection(collection)}{_query(query)}")

    async def get(self, collection: str, id: int) -> dict[str, Any] | None:
        """GET /<collection>?id=<id>; None when the item does not exist."""
        items = await self._list(f"/{_collection(collection)}?id={int(id)}")
        return next((item for item in items if item.get("id") == int(id)), None)

    async def create(self, collection: str, data: Mapping[str, Any], site_id: int) -> int:
        """POST /sites?id=<site_id>/<collection>; returns the new item id."""
        body = dict(data)
        await self._guard("POST", _collection(collection), None, body)
        reply = await self._call("POST", f"/sites?id={int(site_id)}/{collection}", body)
        new_id = reply.data.get("id") if isinstance(reply.data, Mapping) else None
        if not isinstance(new_id, int) or isinstance(new_id, bool):
            raise AgatarkError("Create reply carried no id", status=reply.status, body=reply.data)
        return new_id

    async def patch(
        self,
        collection: str,
        id: int,
        data: Mapping[str, Any],
        confirm: str | None = None,
    ) -> None:
        """PATCH /<collection>?id=<id> with {id, **data} as a merge-patch."""
        item_id = int(id)
        body = {"id": item_id, **{key: value for key, value in data.items() if key != "id"}}
        await self._guard("PATCH", _collection(collection), item_id, body)
        await self._call("PATCH", f"/{collection}?id={item_id}", body, confirm=confirm)

    async def delete(self, collection: str, id: int, confirm: str | None = None) -> None:
        """DELETE /<collection>?id=<id>."""
        item_id = int(id)
        await self._guard("DELETE", _collection(collection), item_id, None)
        await self._call("DELETE", f"/{collection}?id={item_id}", confirm=confirm)

    async def devices(self) -> list[Device]:
        return [Device.from_dict(item) for item in await self.list("devices") if "id" in item]

    async def device(self, id: int) -> Device | None:
        item = await self.get("devices", id)
        return Device.from_dict(item) if item is not None else None

    async def set_output(self, device_id: int, on: bool) -> None:
        """PATCH /devices?id=<id> with {id, status: {output: on}}."""
        await self.patch("devices", device_id, {"status": {"output": bool(on)}})

    async def sites(self) -> list[Site]:
        return [Site.from_dict(item) for item in await self.list("sites") if "id" in item]

    async def rooms(self) -> list[Room]:
        return [Room.from_dict(item) for item in await self.list("rooms") if "id" in item]

    async def events(
        self, *, backoff_initial: float = 1.0, backoff_max: float = 30.0
    ) -> AsyncIterator[Event]:
        """Long-poll POST /events, yielding every item in order; one iterator per session."""
        if self._events_active:
            raise AgatarkError(
                "events() is already running on this client; the /events queue is per session, "
                "so use a separate client and login for each consumer"
            )
        self._events_active = True
        try:
            delay = backoff_initial
            while True:
                started = time.monotonic()
                try:
                    reply = await self._call("POST", "/events", client_timeout=self._events_timeout)
                    items = self._event_items(reply.data)
                    if not items:
                        await self._probe_if_due()
                except (aiohttp.ClientError, TimeoutError) as exc:
                    _LOGGER.warning(
                        "Event poll failed (%s); retrying in %.1f s", type(exc).__name__, delay
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, backoff_max)
                    continue
                except AgatarkError as exc:
                    if exc.status is None or exc.status < 500 or isinstance(exc, UplinkError):
                        raise
                    _LOGGER.warning("Event poll got HTTP %s; retrying in %.1f s", exc.status, delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, backoff_max)
                    continue
                delay = backoff_initial
                for item in items:
                    yield parse_event(item)
                if not items:
                    remaining = self.empty_poll_interval - (time.monotonic() - started)
                    if remaining > 0:
                        await asyncio.sleep(remaining)
        finally:
            self._events_active = False

    async def logs(
        self, model: int | None = None, by: int | None = None, limit: int | None = None
    ) -> AsyncIterator[LogEntry]:
        """GET /logs?model=|by=, newest first, following Link rel=next pages."""
        if (model is None) == (by is None):
            raise ValueError("Pass exactly one of model or by")
        if limit is not None and limit <= 0:
            return
        path: str | None = (
            f"/logs?model={int(model)}" if model is not None else f"/logs?by={int(by or 0)}"
        )
        count = 0
        first = True
        while path:
            reply = await self._call("GET", path, check_empty=first)
            first = False
            data = reply.data if isinstance(reply.data, list) else []
            items = [item for item in data if isinstance(item, Mapping)]
            for item in items:
                yield LogEntry.from_dict(item)
                count += 1
                if limit is not None and count >= limit:
                    return
            path = _relative(reply.next_link) if items and reply.next_link else None

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
            self._owns_session = True
        return self._session

    def _forget(self) -> None:
        self._authorization = None
        self._a = None
        self._hello = None
        self._user_id = None
        self._state = None

    @property
    def _can_login(self) -> bool:
        return bool(self._email) and (self._token is not None or self._password is not None)

    @property
    def _can_recover(self) -> bool:
        return self._can_login or self._authorization is not None

    def _matches(self, state: SessionState) -> bool:
        return (
            bool(self._email)
            and state.base_url.rstrip("/") == self.base_url
            and normalize_email(state.email) == normalize_email(self._email or "")
        )

    async def _notify(self, state: SessionState | None) -> None:
        if self._on_session_change is None:
            return
        try:
            result = self._on_session_change(state)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            _LOGGER.warning("Session change callback failed (%s)", type(exc).__name__)

    async def _set_connected(self, connected: bool) -> None:
        previous = self._connected
        self._connected = connected
        if previous is connected or (previous is None and connected):
            return
        if self._on_connection_change is None:
            return
        try:
            result = self._on_connection_change(connected)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            _LOGGER.warning("Connection change callback failed (%s)", type(exc).__name__)

    async def _send(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        confirm: str | None = None,
        client_timeout: aiohttp.ClientTimeout | None = None,
    ) -> _Reply:
        headers: dict[str, str] = {}
        if self._authorization is not None:
            headers["Authorization"] = self._authorization
        if confirm is not None:
            headers["Accept-Confirm"] = confirm
        payload: bytes | None = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body, separators=(",", ":")).encode()
        _LOGGER.debug("%s %s", method, path)
        try:
            async with self._http().request(
                method,
                URL(self.base_url + path, encoded=True),
                headers=headers,
                data=payload,
                skip_auto_headers=() if payload is not None else ("Content-Type",),
                timeout=client_timeout or self._timeout,
            ) as response:
                raw = await response.read()
                data: Any = None
                if response.content_type == "application/json" and raw:
                    try:
                        data = json.loads(raw)
                    except ValueError:
                        data = raw.decode(errors="replace")
                elif raw:
                    data = raw.decode(errors="replace")
                match = _LINK_NEXT.search(response.headers.get("Link", ""))
                reply = _Reply(response.status, data, match.group(1) if match else None)
        except aiohttp.ClientError, TimeoutError:
            await self._set_connected(False)
            raise
        await self._set_connected(reply.status < 500)
        return reply

    async def _login(self) -> Hello:
        if not self._email:
            raise AuthError("No e-mail configured")
        if self._token is not None:
            token = self._token
        elif self._password is not None:
            token = derive_token(self._password, self._email)
        else:
            raise AuthError("No password or token configured")
        self._forget()
        body = {
            "email": normalize_email(self._email),
            "remember": "on" if self._remember else None,
            "token": token,
        }
        reply = await self._send("PUT", "/hello", body)
        _check(reply)
        data = reply.data if isinstance(reply.data, Mapping) else {}
        hello = Hello.from_dict(data)
        if not hello.logged_in or not hello.authorization:
            error = data.get("error")
            safe = {key: value for key, value in data.items() if key != "authorization"}
            raise AuthError(
                f"Login rejected{f': {error}' if error else ''}", status=reply.status, body=safe
            )
        self._authorization = hello.authorization
        self._a = hello.a
        self._hello = hello
        self._user_id = hello.user_id
        self._state = SessionState(
            authorization=hello.authorization,
            user_id=hello.user_id,
            a=hello.a,
            base_url=self.base_url,
            email=normalize_email(self._email),
        )
        self._generation += 1
        self._last_probe = time.monotonic()
        await self._notify(self._state)
        return hello

    async def _ensure_login(self) -> None:
        if self._authorization is not None:
            return
        async with self._auth_lock:
            if self._authorization is None:
                await self._login()

    async def _relogin(self, generation: int) -> None:
        async with self._auth_lock:
            if self._generation == generation:
                if not self._can_login:
                    self._forget()
                    raise AuthError("stored session expired and no credentials to log in again")
                _LOGGER.info("Session expired; logging in again")
                await self._login()

    async def _session_valid(self) -> bool:
        self._last_probe = time.monotonic()
        user_id = self._user_id
        if user_id is None or self._authorization is None:
            return False
        reply = await self._send("GET", f"/users?id={user_id}")
        if reply.status == 401:
            return False
        _check(reply)
        data = reply.data if isinstance(reply.data, list) else []
        return any(isinstance(item, Mapping) and item.get("id") == user_id for item in data)

    async def _probe_if_due(self) -> None:
        if not self._can_recover:
            return
        if time.monotonic() - self._last_probe < self.session_probe_interval:
            return
        generation = self._generation
        if not await self._session_valid():
            await self._relogin(generation)

    async def _call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        confirm: str | None = None,
        client_timeout: aiohttp.ClientTimeout | None = None,
        check_empty: bool = False,
    ) -> _Reply:
        await self._ensure_login()
        generation = self._generation
        reply = await self._send(method, path, body, confirm=confirm, client_timeout=client_timeout)
        if reply.status == 401 and self._can_recover:
            await self._relogin(generation)
            generation = self._generation
            reply = await self._send(
                method, path, body, confirm=confirm, client_timeout=client_timeout
            )
        _check(reply)
        if (
            check_empty
            and reply.data == []
            and self._can_recover
            and not await self._session_valid()
        ):
            await self._relogin(generation)
            reply = await self._send(
                method, path, body, confirm=confirm, client_timeout=client_timeout
            )
            _check(reply)
        return reply

    async def _list(self, path: str) -> list[dict[str, Any]]:
        reply = await self._call("GET", path, check_empty=True)
        data = reply.data
        if data is None:
            return []
        if not isinstance(data, list):
            raise AgatarkError(f"Expected a list from GET {path}", status=reply.status, body=data)
        return [dict(item) for item in data if isinstance(item, Mapping)]

    async def _guard(
        self, method: str, collection: str | None, item_id: int | None, data: Any
    ) -> None:
        if self.guard is None:
            return
        current = None
        if self.guard.needs_current(method, collection, item_id) and collection in COLLECTIONS:
            current = await self.get(collection, item_id)
        self.guard.check(method, collection, item_id, data=data, current=current)

    @staticmethod
    def _event_items(data: Any) -> list[Any]:
        if isinstance(data, Mapping):
            data = data.get("events")
        return list(data) if isinstance(data, list) else []
