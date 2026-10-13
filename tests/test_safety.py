import asyncio

import pytest

from sabatark import AgatarkClient, AgatarkError, WriteBlocked, WriteGuard
from tests.fake_controller import EMAIL, PASSWORD, controller, reply, run

ALLOWED = 1001
PREFIX = "sabatark-test"


def guard():
    return WriteGuard({ALLOWED}, PREFIX)


def test_client_checks_output_before_any_http():
    client = AgatarkClient("http://example.invalid", EMAIL, PASSWORD, guard=guard())
    with pytest.raises(WriteBlocked):
        asyncio.run(client.set_output(1002, True))


def test_client_allowed_output_reaches_the_controller():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("PATCH", "/devices?id=1001", reply(status=204))
            async with AgatarkClient(url, EMAIL, PASSWORD, guard=guard()) as client:
                await client.set_output(1001, True)
            assert ctl.requests("PATCH", "/devices?id=1001")[0].json == {
                "id": 1001,
                "status": {"output": True},
            }

    run(scenario)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PATCH", "/devices?id=1002", {"id": 1002}),
        ("DELETE", "/devices?id=1001", None),
        ("PUT", "/devices?id=1001", {"event": "execute"}),
        ("PATCH", "/devices?id=1001", {"id": 1002}),
        ("PUT", "/hello", {"a": "x"}),
        ("DELETE", "/hello", None),
        ("POST", "/sites?id=7/devices", {"name": "sabatark-test-device"}),
        ("POST", "/sites?id=7/users", {"name": "Owner"}),
        ("PATCH", "/sites?id=7", {"id": 7}),
        ("POST", "/pub", None),
    ],
)
def test_guarded_raw_writes_are_blocked_before_http(method, path, body):
    async def scenario():
        async with controller() as (ctl, url):
            async with AgatarkClient(url, EMAIL, PASSWORD, guard=guard()) as client:
                with pytest.raises(WriteBlocked):
                    await client.request(method, path, body)
            assert [item.method for item in ctl.seen] == []

    run(scenario)


def test_guard_allows_sandbox_items_and_blocks_real_ones():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("GET", "/users?id=803", reply([{"id": 803, "name": "sabatark-test-user"}]))
            ctl.on("GET", "/users?id=60", reply([{"id": 60, "name": "Owner"}]))
            ctl.on("GET", "/roles?id=1", reply([{"id": 1, "name": "Admins"}]))
            ctl.on("GET", "/users?id=999", reply([]))
            ctl.on("POST", "/sites?id=7/rules", reply({"id": 802}))
            ctl.on("PATCH", "/users?id=803", reply(status=204))
            ctl.on("DELETE", "/users?id=803", reply(status=204))
            async with AgatarkClient(url, EMAIL, PASSWORD, guard=guard()) as client:
                assert await client.create("rules", {"name": "sabatark-test-rule"}, 7) == 802
                await client.patch("users", 803, {"forceChange": True})
                await client.delete("users", 803)
                with pytest.raises(WriteBlocked):
                    await client.patch("users", 803, {"name": "Owner"})
                with pytest.raises(WriteBlocked):
                    await client.patch("users", 60, {"name": "sabatark-test-user"})
                with pytest.raises(WriteBlocked):
                    await client.delete("roles", 1)
                with pytest.raises(WriteBlocked):
                    await client.create("rules", {"name": "real rule"}, 7)
                with pytest.raises(WriteBlocked):
                    await client.delete("users", 999)
            writes = [
                (item.method, item.path)
                for item in ctl.seen
                if item.method != "GET" and item.path != "/hello"
            ]
            assert writes == [
                ("POST", "/sites?id=7/rules"),
                ("PATCH", "/users?id=803"),
                ("DELETE", "/users?id=803"),
            ]

    run(scenario)


def test_guard_allows_reads_and_event_polls():
    guard = WriteGuard([ALLOWED], PREFIX)
    guard.check("GET", "devices", 1002)
    guard.check("POST", "events")
    guard.check("PATCH", "devices", 1001, data={"id": 1001, "status": {"output": False}})


def test_unguarded_client_allows_any_write():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("PATCH", "/devices?id=1002", reply(status=204))
            async with AgatarkClient(url, EMAIL, PASSWORD) as client:
                await client.set_output(1002, False)
            assert len(ctl.requests("PATCH", "/devices?id=1002")) == 1

    run(scenario)


def test_write_blocked_is_an_agatark_error():
    assert issubclass(WriteBlocked, AgatarkError)


def test_guard_has_no_default_targets():
    with pytest.raises(TypeError):
        WriteGuard()
    with pytest.raises(TypeError):
        WriteGuard({ALLOWED})
    with pytest.raises(ValueError):
        WriteGuard({ALLOWED}, "")


def test_guard_normalises_its_arguments():
    guard = WriteGuard(["1001", 1002], PREFIX, sandbox_collections=["rules"])
    assert guard.devices == frozenset({1001, 1002})
    assert guard.sandbox_collections == frozenset({"rules"})
    assert guard == WriteGuard({1001, 1002}, PREFIX, frozenset({"rules"}))
    assert hash(guard) == hash(WriteGuard((1002, 1001), PREFIX, ("rules",)))
    guard.check("POST", "rules", data={"name": "sabatark-test-rule"})
    with pytest.raises(WriteBlocked):
        guard.check("POST", "users", data={"name": "sabatark-test-user"})


def test_guard_without_sandbox_prefix_allows_only_devices():
    guard = WriteGuard({ALLOWED}, None)
    guard.check("PATCH", "devices", ALLOWED, data={"id": ALLOWED, "status": {"output": True}})
    assert not guard.needs_current("PATCH", "users", 803)
    with pytest.raises(WriteBlocked):
        guard.check("POST", "rules", data={"name": "sabatark-test-rule"})
    with pytest.raises(WriteBlocked):
        guard.check("PATCH", "users", 803, current={"id": 803, "name": "sabatark-test-user"})
    with pytest.raises(WriteBlocked):
        guard.check("PATCH", "devices", 1002, data={"id": 1002})


def test_guard_respects_a_custom_prefix():
    guard = WriteGuard((), "scratch-")
    guard.check("POST", "roles", data={"name": "scratch-role"})
    with pytest.raises(WriteBlocked):
        guard.check("POST", "roles", data={"name": "sabatark-test-role"})
    with pytest.raises(WriteBlocked):
        guard.check("PATCH", "devices", ALLOWED, data={"id": ALLOWED})
