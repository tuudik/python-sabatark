import asyncio
import json
import logging
import stat
from functools import partial

import pytest

from sabatark import AgatarkClient, AuthError, SessionState, cli
from sabatark.session import load_session, persist_session, save_session, session_path
from tests.fake_controller import (
    EMAIL,
    PASSWORD,
    TOKEN,
    controller,
    reply,
    response,
    run,
    when_authed,
)

STORED = "S stored-session-credential-0000000000"
DEVICES = [{"id": 1001, "name": "Kitchen light"}]


def stored_state(ctl, url, *, authorization=STORED, email=EMAIL):
    return SessionState(
        authorization=authorization,
        user_id=ctl.user["id"],
        a="stored-tab",
        base_url=url,
        email=email,
        created=1_700_000_000.0,
    )


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SABATARK_USERNAME", EMAIL)
    monkeypatch.setenv("SABATARK_PASSWORD", PASSWORD)

    def configure(url):
        monkeypatch.setenv("SABATARK_URL", url)
        return session_path(url, EMAIL, "cli")

    return configure


@pytest.mark.parametrize(("remember", "sent"), [(True, "on"), (False, None)])
def test_remember_flag_in_login_body(remember, sent):
    async def scenario():
        async with controller() as (ctl, url):
            async with AgatarkClient(url, EMAIL, PASSWORD, remember=remember) as client:
                await client.login()
            assert ctl.requests("PUT", "/hello")[0].json == {
                "email": EMAIL,
                "remember": sent,
                "token": TOKEN,
            }

    run(scenario)


def test_restored_session_is_used_without_login():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.sessions.add(STORED)
            ctl.on("GET", "/devices", when_authed(ctl, DEVICES))
            changes = []
            state = stored_state(ctl, url)
            async with AgatarkClient(
                url, EMAIL, PASSWORD, session_state=state, on_session_change=changes.append
            ) as client:
                assert client.logged_in
                assert client.session_state is state
                assert [device.id for device in await client.devices()] == [1001]
            assert ctl.requests("PUT", "/hello") == []
            assert ctl.requests("GET", "/devices")[0].headers["Authorization"] == STORED
            assert changes == []

    run(scenario)


def test_restored_invalid_session_probes_then_logs_in_once():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("GET", "/devices", when_authed(ctl, DEVICES))
            changes = []

            async def remember(state):
                changes.append(state)

            async with AgatarkClient(
                url,
                EMAIL,
                PASSWORD,
                session_state=stored_state(ctl, url),
                on_session_change=remember,
            ) as client:
                assert [device.id for device in await client.devices()] == [1001]
                current = client.session_state
            assert [(item.method, item.path) for item in ctl.seen] == [
                ("GET", "/devices"),
                ("GET", f"/users?id={ctl.user['id']}"),
                ("PUT", "/hello"),
                ("GET", "/devices"),
            ]
            assert ctl.seen[1].headers["Authorization"] == STORED
            assert ctl.logins == 1
            assert changes == [current]
            assert current.authorization == f"S session-{1:030d}"
            assert current.authorization in ctl.sessions
            assert (current.user_id, current.base_url, current.email) == (
                ctl.user["id"],
                url,
                EMAIL,
            )

    run(scenario)


def test_restored_session_401_relogins_and_retries():
    async def scenario():
        async with controller() as (ctl, url):

            def events(seen):
                if seen.headers.get("Authorization") == STORED:
                    return response(status=401)
                return response({"events": [{"id": 1001, "status": {"output": True}}]})

            ctl.on("POST", "/events", events)
            async with AgatarkClient(
                url, EMAIL, PASSWORD, session_state=stored_state(ctl, url)
            ) as client:
                events = client.events()
                first = await anext(events)
                await events.aclose()
            assert first.id == 1001
            assert ctl.logins == 1

    run(scenario)


def test_expired_session_without_credentials_raises_auth_error():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("GET", "/devices", when_authed(ctl, DEVICES))
            async with AgatarkClient(url, EMAIL, session_state=stored_state(ctl, url)) as client:
                with pytest.raises(AuthError, match="stored session expired and no credentials"):
                    await client.devices()
                assert not client.logged_in
                assert client.session_state is None
            assert ctl.requests("PUT", "/hello") == []

    run(scenario)


@pytest.mark.parametrize("field", ["base_url", "email"])
def test_mismatched_session_is_ignored(field):
    async def scenario():
        async with controller() as (ctl, url):
            ctl.sessions.add(STORED)
            ctl.on("GET", "/devices", when_authed(ctl, DEVICES))
            state = stored_state(ctl, url)
            other = SessionState(
                **{
                    **state.to_dict(),
                    field: "http://other.invalid" if field == "base_url" else "other@example.com",
                }
            )
            async with AgatarkClient(url, EMAIL, PASSWORD, session_state=other) as client:
                assert not client.logged_in
                assert client.session_state is None
                await client.devices()
            assert ctl.logins == 1
            assert ctl.requests("GET", "/devices")[0].headers["Authorization"] != STORED

    run(scenario)


def test_email_and_trailing_slash_are_normalised_when_matching():
    async def scenario():
        async with controller() as (ctl, url):
            state = stored_state(ctl, url + "/", email=" User@Example.COM ")
            client = AgatarkClient(url, EMAIL.upper(), PASSWORD, session_state=state)
            assert client.session_state is state
            await client.close()

    run(scenario)


def test_close_keeps_session_and_logout_calls_back_none():
    async def scenario():
        async with controller() as (ctl, url):
            changes = []
            client = AgatarkClient(url, EMAIL, PASSWORD, on_session_change=changes.append)
            await client.login()
            await client.close()
            assert ctl.requests("DELETE", "/hello") == []
            assert client.logged_in
            await client.logout()
            await client.close()
            deletes = ctl.requests("DELETE", "/hello")
            assert [item.headers["Authorization"] for item in deletes] == [f"S session-{1:030d}"]
            assert changes[-1] is None
            assert isinstance(changes[0], SessionState)
            assert client.session_state is None

    run(scenario)


def test_close_with_logout_ends_the_session():
    async def scenario():
        async with controller() as (ctl, url):
            client = AgatarkClient(url, EMAIL, PASSWORD)
            await client.login()
            await client.close(logout=True)
            assert len(ctl.requests("DELETE", "/hello")) == 1
            assert ctl.sessions == set()

    run(scenario)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_callback_exception_does_not_break_the_request(asynchronous, caplog):
    def broken(state):
        raise RuntimeError(f"cannot store {state.authorization}")

    async def broken_async(state):
        broken(state)

    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("GET", "/devices", when_authed(ctl, DEVICES))
            async with AgatarkClient(
                url, EMAIL, PASSWORD, on_session_change=broken_async if asynchronous else broken
            ) as client:
                assert [device.id for device in await client.devices()] == [1001]

    with caplog.at_level(logging.WARNING, logger="sabatark.client"):
        run(scenario)
    assert "RuntimeError" in caplog.text
    assert "session-" not in caplog.text


def test_session_state_round_trips_and_hides_authorization():
    state = SessionState(authorization=STORED, user_id=50, base_url="http://x.invalid", email=EMAIL)
    assert STORED not in repr(state)
    assert SessionState.from_dict(json.loads(json.dumps(state.to_dict()))) == state
    assert state.a is None and state.created > 0
    for broken in (
        [],
        {**state.to_dict(), "authorization": ""},
        {**state.to_dict(), "user_id": "50"},
        {**state.to_dict(), "created": None},
    ):
        with pytest.raises(ValueError):
            SessionState.from_dict(broken)


def test_session_path_uses_xdg_and_consumer(isolated_config_home):
    path = session_path("http://x.invalid/", " User@Example.COM ", "cli")
    assert path.parent == isolated_config_home / "sabatark"
    assert path == session_path("http://x.invalid", EMAIL, "cli")
    assert path.name.startswith("session-") and path.name.endswith("-cli.json")
    assert len(path.name.removeprefix("session-").removesuffix("-cli.json")) == 12
    assert session_path("http://x.invalid", EMAIL, "app") != path


def test_cli_stores_and_reuses_a_private_session_file(cli_env, isolated_config_home, capsys):
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("GET", "/devices", when_authed(ctl, [{**DEVICES[0], "type": "output"}]))
            path = cli_env(url)
            await asyncio.to_thread(cli.main, ["outputs"])
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
            text = path.read_text()
            assert PASSWORD not in text and TOKEN not in text
            assert load_session(path).authorization == f"S session-{1:030d}"
            await asyncio.to_thread(cli.main, ["outputs"])
            assert ctl.logins == 1
            assert ctl.requests("DELETE", "/hello") == []
            authorizations = {item.headers["Authorization"] for item in ctl.requests("GET")}
            assert authorizations == {f"S session-{1:030d}"}
            assert list(path.parent.iterdir()) == [path]

    run(scenario)
    assert "Kitchen light" in capsys.readouterr().out


@pytest.mark.parametrize("content", ["{not json", '{"authorization": 1}', "[]"])
def test_cli_ignores_a_corrupt_session_file(cli_env, content):
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("GET", "/devices", when_authed(ctl, DEVICES))
            path = cli_env(url)
            path.parent.mkdir(parents=True)
            path.write_text(content)
            await asyncio.to_thread(cli.main, ["outputs"])
            assert ctl.logins == 1
            assert load_session(path).authorization == f"S session-{1:030d}"

    run(scenario)


def test_cli_logout_ends_the_stored_session_and_deletes_the_file(cli_env):
    async def scenario():
        async with controller() as (ctl, url):
            path = cli_env(url)
            ctl.sessions.add(STORED)
            save_session(path, stored_state(ctl, url))
            await asyncio.to_thread(cli.main, ["logout"])
            assert not path.exists()
            assert [item.headers["Authorization"] for item in ctl.seen] == [STORED]
            assert ctl.seen[0].method == "DELETE"
            assert ctl.sessions == set()
            await asyncio.to_thread(cli.main, ["logout"])
            assert len(ctl.seen) == 1

    run(scenario)


def test_cli_logout_tolerates_an_already_expired_session(cli_env):
    async def scenario():
        async with controller() as (ctl, url):
            path = cli_env(url)
            ctl.on("DELETE", "/hello", reply(status=401))
            save_session(path, stored_state(ctl, url))
            await asyncio.to_thread(cli.main, ["logout"])
            assert not path.exists()

    run(scenario)


def test_cli_without_session_cache_writes_nothing_and_logs_out(cli_env, isolated_config_home):
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("GET", "/devices", when_authed(ctl, DEVICES))
            cli_env(url)
            await asyncio.to_thread(cli.main, ["--no-session-cache", "outputs"])
            assert ctl.logins == 1
            assert len(ctl.requests("DELETE", "/hello")) == 1
            assert list(isolated_config_home.iterdir()) == []

    run(scenario)


def test_each_consumer_keeps_its_own_session_file(cli_env):
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("GET", "/devices", when_authed(ctl, DEVICES))
            cli_path = cli_env(url)
            await asyncio.to_thread(cli.main, ["outputs"])
            app_path = session_path(url, EMAIL, "app")
            for _ in range(2):
                async with AgatarkClient(
                    url,
                    EMAIL,
                    PASSWORD,
                    session_state=load_session(app_path),
                    on_session_change=partial(persist_session, app_path),
                ) as client:
                    assert [device.id for device in await client.devices()] == [1001]
            assert app_path.name.endswith("-app.json")
            assert app_path.parent == cli_path.parent
            cli_state, app_state = load_session(cli_path), load_session(app_path)
            assert cli_state.authorization != app_state.authorization
            assert {cli_state.authorization, app_state.authorization} <= ctl.sessions
            assert stat.S_IMODE(app_path.stat().st_mode) == 0o600
            assert ctl.logins == 2
            assert ctl.requests("DELETE", "/hello") == []
            app_requests = ctl.requests("GET", "/devices")[1:]
            assert {item.headers["Authorization"] for item in app_requests} == {
                app_state.authorization
            }

    run(scenario)
