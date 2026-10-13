import pytest
from aiohttp import web

from sabatark import (
    AgatarkClient,
    AgatarkError,
    ConfirmationRequired,
    Device,
    EventDelta,
    UplinkError,
    merge_patch,
)
from tests.fake_controller import EMAIL, PASSWORD, controller, fixture, reply, run, when_authed

CONFIRM_DIALOG = {
    "title": "Delete role?",
    "body": "Role is in use",
    "actions": [{"title": "Delete anyway", "action": "forceDelete", "key": "d"}],
}


def client_for(url):
    return AgatarkClient(url, EMAIL, PASSWORD)


def test_crud_url_shapes_and_bodies():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("GET", "/rules?siteId=7&type=if-then,calculator", reply([{"id": 9}]))
            ctl.on("GET", "/users?id=7", reply([{"id": 7, "name": "x"}]))
            ctl.on("GET", "/users?id=8", when_authed(ctl, []))
            ctl.on("POST", "/sites?id=7/roles", reply({"id": 801}))
            ctl.on("PATCH", "/rules?id=9", reply(status=200))
            ctl.on("DELETE", "/users?id=7", reply(status=204))
            async with client_for(url) as client:
                rules = await client.list("rules", siteId=7, type=["if-then", "calculator"])
                assert rules == [{"id": 9}]
                assert await client.get("users", 7) == {"id": 7, "name": "x"}
                assert await client.get("users", 8) is None
                assert await client.create("roles", {"name": "r"}, 7) == 801
                await client.patch("rules", 9, {"enabled": False, "id": 1})
                await client.delete("users", 7)
            create = ctl.requests("POST", "/sites?id=7/roles")[0]
            assert create.json == {"name": "r"}
            assert create.headers["Content-Type"] == "application/json"
            assert ctl.requests("PATCH", "/rules?id=9")[0].json == {"id": 9, "enabled": False}
            delete = ctl.requests("DELETE", "/users?id=7")[0]
            assert delete.body == b""
            assert "Content-Type" not in delete.headers

    run(scenario)


def test_unknown_collection_is_rejected():
    async def scenario():
        async with controller() as (ctl, url):
            async with client_for(url) as client:
                with pytest.raises(ValueError):
                    await client.list("output")
            assert ctl.seen == []

    run(scenario)


def test_set_output_patches_status_output_and_accepts_empty_reply():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("PATCH", "/devices?id=1001", reply(status=200))
            async with client_for(url) as client:
                await client.set_output(1001, True)
                await client.set_output(1001, False)
            bodies = [item.json for item in ctl.requests("PATCH", "/devices?id=1001")]
            assert bodies == [
                {"id": 1001, "status": {"output": True}},
                {"id": 1001, "status": {"output": False}},
            ]

    run(scenario)


def test_any_2xx_succeeds_and_non_json_bodies_are_not_parsed():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("DELETE", "/rules?id=5", reply(status=204))
            ctl.on("PATCH", "/rules?id=5", lambda seen: web.Response(text="ok"))
            async with client_for(url) as client:
                await client.patch("rules", 5, {"name": "n"})
                await client.delete("rules", 5)
                assert await client.request("PATCH", "/rules?id=5", {"id": 5}) == "ok"

    run(scenario)


def test_406_raises_confirmation_and_confirm_sends_accept_confirm():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on(
                "DELETE",
                "/roles?id=12",
                lambda seen: (
                    reply(status=204)
                    if seen.headers.get("Accept-Confirm") == "forceDelete"
                    else reply(CONFIRM_DIALOG, status=406)
                ),
            )
            async with client_for(url) as client:
                with pytest.raises(ConfirmationRequired) as caught:
                    await client.delete("roles", 12)
                assert caught.value.action == "forceDelete"
                assert caught.value.dialog["body"] == "Role is in use"
                assert str(caught.value) == "Delete role?"
                await client.delete("roles", 12, confirm=caught.value.action)
            first, second = ctl.requests("DELETE", "/roles?id=12")
            assert "Accept-Confirm" not in first.headers
            assert second.headers["Accept-Confirm"] == "forceDelete"

    run(scenario)


def test_406_on_patch_supports_confirm():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("PATCH", "/rules?id=3", reply(CONFIRM_DIALOG, status=406), reply(status=200))
            async with client_for(url) as client:
                with pytest.raises(ConfirmationRequired):
                    await client.patch("rules", 3, {"enabled": True})
                await client.patch("rules", 3, {"enabled": True}, confirm="forceDelete")
            retry = ctl.requests("PATCH", "/rules?id=3")[1]
            assert retry.headers["Accept-Confirm"] == "forceDelete"

    run(scenario)


def test_504_maps_to_uplink_error_and_other_errors_to_agatark_error():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("GET", "/devices", reply({"message": "uplink"}, status=504))
            ctl.on("GET", "/rooms", reply({"title": "Boom"}, status=500))
            async with client_for(url) as client:
                with pytest.raises(UplinkError) as uplink:
                    await client.devices()
                with pytest.raises(AgatarkError) as other:
                    await client.rooms()
            assert uplink.value.status == 504
            assert other.value.status == 500
            assert "Boom" in str(other.value)

    run(scenario)


def test_create_without_id_in_reply_raises():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("POST", "/sites?id=1/rules", reply(status=204))
            async with client_for(url) as client:
                with pytest.raises(AgatarkError):
                    await client.create("rules", {"name": "x"}, 1)

    run(scenario)


def test_typed_collections_parse_fixtures():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("GET", "/devices", reply(fixture("devices")))
            target = [item for item in fixture("devices") if item["id"] == 1001]
            ctl.on("GET", "/devices?id=1001", reply(target))
            ctl.on("GET", "/sites", reply(fixture("sites")))
            ctl.on("GET", "/rooms", reply(fixture("rooms")))
            async with client_for(url) as client:
                devices = {device.id: device for device in await client.devices()}
                target = await client.device(1001)
                sites = await client.sites()
                rooms = await client.rooms()
            assert target == devices[1001]
            assert target.name == "Kitchen light"
            assert (target.room_id, target.site_id, target.uuid) == (70, 7, "kq7tz2ma")
            assert target.usage == {"light": [""]}
            assert target.conf[0]["path"] == "/status/o1"
            assert target.status["output"] is True
            assert target.raw["id"] == 1001
            assert target.is_output and not target.is_switch and not target.is_motion
            assert devices[1005].is_switch and not devices[1005].is_output
            assert devices[1003].is_output
            assert not devices[1012].is_output
            assert not devices[1013].is_output
            assert devices[1004].is_output and devices[1002].is_output
            assert devices[1006].is_motion and devices[1007].is_motion
            assert [site.id for site in sites] == [item["id"] for item in fixture("sites")]
            assert all(room.site_id is not None for room in rooms)

    run(scenario)


def test_device_helpers_and_apply_delta():
    pir = Device.from_dict({"id": 1006, "usage": {"securityDetector": ["motion"]}, "status": {}})
    assert pir.is_motion
    delta = {"id": 1006, "status": {"active": True}}
    switched = pir.apply(EventDelta(id=1006, status=delta["status"], raw=delta))
    assert switched.status == {"active": True}
    with pytest.raises(ValueError):
        pir.apply(EventDelta(id=1, raw={"id": 1}))


def test_merge_patch_deep_merges_and_deletes_on_null():
    target = {"status": {"a": 1, "b": 2}, "setup": {"onTime": "x"}}
    patched = merge_patch(target, {"status": {"b": None, "c": 3}, "setup": None, "name": "n"})
    assert patched == {"status": {"a": 1, "c": 3}, "name": "n"}
    assert target == {"status": {"a": 1, "b": 2}, "setup": {"onTime": "x"}}


def test_raw_request_passes_through_with_auth():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("GET", "/sites?id=7/users", when_authed(ctl, [{"id": 50}]))
            async with client_for(url) as client:
                assert await client.request("get", "/sites?id=7/users") == [{"id": 50}]
                with pytest.raises(ValueError):
                    await client.request("GET", "devices")

    run(scenario)
