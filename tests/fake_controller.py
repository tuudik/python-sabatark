"""In-process fake Agatark controller for offline client tests."""

import asyncio
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

FIXTURES = Path(__file__).parent / "fixtures"
EMAIL = "user@example.com"
PASSWORD = "password"
TOKEN = "7a2fd50cc006384e8c694c4a83e04233dca11a13cae0378fd7f7f77035675161"

Responder = Callable[["Seen"], Any]


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def response(data: Any = None, status: int = 200, headers: dict[str, str] | None = None):
    if data is None:
        return web.Response(status=status, headers=headers)
    return web.json_response(data, status=status, headers=headers)


def reply(data: Any = None, status: int = 200, headers: dict[str, str] | None = None) -> Responder:
    return lambda seen: response(data, status, headers)


@dataclass
class Seen:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    @property
    def json(self) -> Any:
        return json.loads(self.body) if self.body else None


class FakeController:
    """Records requests, answers /hello and the own-user probe itself, and serves queued replies."""

    def __init__(self) -> None:
        self.seen: list[Seen] = []
        self.routes: dict[tuple[str, str], list[Responder]] = {}
        self.sessions: set[str] = set()
        self.logins = 0
        self.anonymous = 0
        self.user = fixture("hello")["user"]

    def on(self, method: str, path: str, *responses: Responder) -> None:
        self.routes.setdefault((method, path), []).extend(responses)

    def authed(self, seen: Seen) -> bool:
        return seen.headers.get("Authorization") in self.sessions

    def requests(self, method: str | None = None, path: str | None = None) -> list[Seen]:
        return [
            item
            for item in self.seen
            if (method is None or item.method == method) and (path is None or item.path == path)
        ]

    def expire_sessions(self) -> None:
        self.sessions.clear()

    async def handle(self, request: web.Request) -> web.StreamResponse:
        seen = Seen(request.method, request.raw_path, dict(request.headers), await request.read())
        self.seen.append(seen)
        queue = self.routes.get((seen.method, seen.path))
        if queue:
            responder = queue.pop(0) if len(queue) > 1 else queue[0]
            if responder is DISCONNECT:
                assert request.transport is not None
                request.transport.close()
                await asyncio.sleep(0)
                return web.Response(status=599)
            if responder is STALL:
                await asyncio.sleep(STALL_SECONDS)
                return web.Response(status=599)
            while not isinstance(responder, web.StreamResponse):
                responder = responder(seen)
            return responder
        if seen.path == "/hello" and seen.method == "PUT":
            return self._hello(seen)
        if seen.method == "GET" and seen.path == f"/users?id={self.user['id']}":
            return response([self.user] if self.authed(seen) else [])
        if seen.path == "/hello" and seen.method == "DELETE":
            self.sessions.discard(seen.headers.get("Authorization", ""))
            return web.Response(status=204)
        return web.Response(status=404, text="Not Found")

    def _hello(self, seen: Seen) -> web.Response:
        body = seen.json or {}
        if "token" in body:
            if body.get("email") != EMAIL or body.get("token") != TOKEN:
                return response({"a": "anon", "error": "login.failed"})
            self.logins += 1
            authorization = f"S session-{self.logins:030d}"
            self.sessions.add(authorization)
            return response(
                {
                    "a": f"tab{self.logins}",
                    "authorization": authorization,
                    "user": self.user,
                    "version": "1.0.0-test",
                    "nodeName": "fake",
                    "sites": fixture("sites"),
                    "rooms": fixture("rooms"),
                }
            )
        self.anonymous += 1
        anonymous = {"a": f"anon{self.anonymous}", "authorization": f"S anon-{self.anonymous:033d}"}
        return response(anonymous, status=401)


def DISCONNECT(seen: Seen) -> web.Response:
    raise AssertionError("DISCONNECT is a marker, not a responder")


STALL_SECONDS = 0.3


def STALL(seen: Seen) -> web.Response:
    raise AssertionError("STALL is a marker, not a responder")


@asynccontextmanager
async def controller():
    ctl = FakeController()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", ctl.handle)
    server = TestServer(app)
    await server.start_server()
    try:
        yield ctl, str(server.make_url("")).rstrip("/")
    finally:
        await server.close()


def run(coroutine_function: Callable[[], Any]) -> None:
    asyncio.run(coroutine_function())


def when_authed(ctl: FakeController, data: Any) -> Callable[[Seen], web.Response]:
    return lambda seen: response(data if ctl.authed(seen) else [])
