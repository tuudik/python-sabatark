import hashlib
import json
from pathlib import Path

import pytest

from sabatark import AgatarkClient, AuthError, Hello, PasswordChangeRequired, derive_token
from tests.fake_controller import EMAIL, PASSWORD, TOKEN, controller, reply, run, when_authed

VECTORS = json.loads((Path(__file__).parent / "vectors" / "pbkdf2.json").read_text())["vectors"]


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["email"])
def test_derive_token_matches_web_ui_vectors(vector):
    assert derive_token(vector["password"], vector["email"]) == vector["token"]


@pytest.mark.parametrize("vector", [v for v in VECTORS if not v["stdlib_matches"]])
def test_quirk_vectors_differ_from_plain_stdlib(vector):
    plain = hashlib.pbkdf2_hmac(
        "sha256", vector["password"].encode(), vector["salt"].encode(), 10000, 32
    ).hex()
    assert plain != vector["token"]
    assert derive_token(vector["password"], vector["email"]) == vector["token"]


def test_login_sends_normalised_email_token_and_json_body():
    async def scenario():
        async with controller() as (ctl, url):
            async with AgatarkClient(url, " User@Example.COM ", PASSWORD) as client:
                hello = await client.login()
            login = ctl.requests("PUT", "/hello")[0]
            assert login.json == {"email": EMAIL, "remember": "on", "token": TOKEN}
            assert login.headers["Content-Type"] == "application/json"
            assert "Authorization" not in login.headers
            assert hello.user_id == ctl.user["id"]
            assert hello.logged_in

    run(scenario)


def test_authorization_header_is_injected_verbatim_without_body_headers():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("GET", "/devices", when_authed(ctl, [{"id": 1001}]))
            async with AgatarkClient(url, EMAIL, PASSWORD) as client:
                await client.list("devices")
                await client.list("devices")
            gets = ctl.requests("GET", "/devices")
            expected = f"S session-{1:030d}"
            assert [item.headers["Authorization"] for item in gets] == [expected] * 2
            assert all("Content-Type" not in item.headers for item in gets)
            assert all("Cookie" not in item.headers for item in gets)
            assert ctl.logins == 1

    run(scenario)


def test_precomputed_token_logs_in_without_password():
    async def scenario():
        async with controller() as (ctl, url):
            async with AgatarkClient(url, EMAIL, token=TOKEN) as client:
                await client.login()
            assert ctl.requests("PUT", "/hello")[0].json["token"] == TOKEN

    run(scenario)


def test_rejected_login_raises_auth_error():
    async def scenario():
        async with controller() as (_, url):
            async with AgatarkClient(url, EMAIL, "wrong") as client:
                with pytest.raises(AuthError) as caught:
                    await client.devices()
            assert "login.failed" in str(caught.value)
            assert not client.logged_in

    run(scenario)


def test_missing_credentials_raise_auth_error():
    async def scenario():
        async with controller() as (ctl, url):
            async with AgatarkClient(url) as client:
                with pytest.raises(AuthError):
                    await client.devices()
            assert ctl.seen == []

    run(scenario)


def test_status_454_raises_password_change_required():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("PUT", "/hello", reply({"message": "change"}, status=454))
            async with AgatarkClient(url, EMAIL, PASSWORD) as client:
                with pytest.raises(PasswordChangeRequired) as caught:
                    await client.login()
            assert caught.value.status == 454
            assert isinstance(caught.value, AuthError)

    run(scenario)


def test_401_triggers_one_relogin_and_retry():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("GET", "/devices", reply(status=401), when_authed(ctl, [{"id": 1001}]))
            async with AgatarkClient(url, EMAIL, PASSWORD) as client:
                devices = await client.devices()
            assert [device.id for device in devices] == [1001]
            assert ctl.logins == 2

    run(scenario)


def test_empty_list_without_user_relogins_and_returns_data():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("GET", "/devices", when_authed(ctl, [{"id": 1001, "name": "Kitchen light"}]))
            async with AgatarkClient(url, EMAIL, PASSWORD) as client:
                await client.login()
                ctl.expire_sessions()
                devices = await client.devices()
            assert [device.id for device in devices] == [1001]
            assert ctl.logins == 2
            trail = [(item.method, item.path, item.json) for item in ctl.seen[1:5]]
            assert trail == [
                ("GET", "/devices", None),
                ("GET", f"/users?id={ctl.user['id']}", None),
                ("PUT", "/hello", {"email": EMAIL, "remember": "on", "token": TOKEN}),
                ("GET", "/devices", None),
            ]

    run(scenario)


def test_empty_list_with_live_session_is_returned_without_relogin():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("GET", "/securityAreas", reply([]))
            async with AgatarkClient(url, EMAIL, PASSWORD) as client:
                assert await client.list("securityAreas") == []
            assert ctl.logins == 1
            assert len(ctl.requests("GET", "/securityAreas")) == 1

    run(scenario)


def test_logout_accepts_204_and_forgets_session():
    async def scenario():
        async with controller() as (ctl, url):
            client = AgatarkClient(url, EMAIL, PASSWORD)
            async with client:
                await client.login()
                await client.logout()
                assert not client.logged_in
                assert client.last_hello is None
            assert ctl.sessions == set()
            assert len(ctl.requests("DELETE", "/hello")) == 1

    run(scenario)


def test_closing_the_client_keeps_the_session():
    async def scenario():
        async with controller() as (ctl, url):
            async with AgatarkClient(url, EMAIL, PASSWORD) as client:
                await client.login()
            assert ctl.requests("DELETE", "/hello") == []
            assert len(ctl.sessions) == 1

    run(scenario)


def test_hello_tolerates_401_and_never_adopts_its_authorization():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("GET", "/devices", when_authed(ctl, [{"id": 1001}]))
            async with AgatarkClient(url, EMAIL, PASSWORD) as client:
                login = await client.login()
                bootstrap = await client.hello()
                assert bootstrap.user is None
                assert bootstrap.authorization != login.authorization
                assert client.last_hello is login
                assert [device.id for device in await client.devices()] == [1001]
            probe, devices = ctl.requests("PUT", "/hello")[1], ctl.requests("GET", "/devices")[0]
            assert probe.json == {"a": login.a}
            assert devices.headers["Authorization"] == login.authorization
            assert ctl.logins == 1

    run(scenario)


def test_failed_login_replies_never_store_their_authorization():
    async def scenario():
        async with controller() as (ctl, url):
            anonymous = {"a": "anon", "authorization": "S anonymous-credential"}
            ctl.on("PUT", "/hello", reply(anonymous, status=401), reply(anonymous))
            async with AgatarkClient(url, EMAIL, PASSWORD) as client:
                with pytest.raises(AuthError):
                    await client.login()
                assert not client.logged_in
                with pytest.raises(AuthError) as caught:
                    await client.login()
                assert not client.logged_in
                assert client.last_hello is None
                assert "anonymous-credential" not in repr(caught.value.body)
            assert ctl.requests("DELETE", "/hello") == []

    run(scenario)


def test_session_probe_uses_own_user_lookup():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("GET", "/rules", reply([]))
            async with AgatarkClient(url, EMAIL, PASSWORD) as client:
                await client.list("rules")
            assert [(item.method, item.path) for item in ctl.seen[1:3]] == [
                ("GET", "/rules"),
                ("GET", f"/users?id={ctl.user['id']}"),
            ]
            assert len(ctl.requests("PUT", "/hello")) == 1

    run(scenario)


def test_secrets_stay_out_of_reprs():
    async def scenario():
        async with controller() as (_, url):
            async with AgatarkClient(url, EMAIL, PASSWORD) as client:
                hello = await client.login()
                assert "session-" not in repr(hello)
                assert "session-" not in repr(client)
                assert PASSWORD not in repr(client)

    run(scenario)
    assert "secret" not in repr(Hello(authorization="secret"))
