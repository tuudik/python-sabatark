import logging

import aiohttp
import pytest

from sabatark import AgatarkClient, AgatarkError, UplinkError
from tests.fake_controller import (
    DISCONNECT,
    EMAIL,
    PASSWORD,
    STALL,
    controller,
    fixture,
    reply,
    run,
)


def item(device_id):
    return reply({"events": [{"id": device_id, "status": {"x": device_id}}]})


def busy(status=503):
    return reply({"title": "busy"}, status=status)


class Recorder:
    def __init__(self):
        self.calls = []
        self.seen_state = []
        self.client = None

    def __call__(self, connected):
        self.calls.append(connected)
        self.seen_state.append(self.client.connected if self.client else None)


def fast_client(url, recorder=None, **kwargs):
    client = AgatarkClient(url, EMAIL, PASSWORD, on_connection_change=recorder, **kwargs)
    client.empty_poll_interval = 0
    if recorder is not None:
        recorder.client = client
    return client


async def take(iterator, count):
    result = []
    async for event in iterator:
        result.append(event)
        if len(result) == count:
            break
    await iterator.aclose()
    return result


def test_connected_is_unknown_until_the_first_request_and_initial_success_is_silent():
    async def scenario():
        async with controller() as (ctl, url):
            recorder = Recorder()
            async with fast_client(url, recorder) as client:
                assert client.connected is None
                await client.login()
                assert client.connected is True
            assert recorder.calls == []

    run(scenario)


def test_outage_during_events_reports_one_false_and_one_true():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("POST", "/events", item(1), DISCONNECT, busy(), DISCONNECT, item(2))
            recorder = Recorder()
            async with fast_client(url, recorder) as client:
                events = await take(client.events(backoff_initial=0.01, backoff_max=0.02), 2)
                assert client.connected is True
            assert [event.id for event in events] == [1, 2]
            assert recorder.calls == [False, True]
            assert recorder.seen_state == [False, True]
            assert len(ctl.requests("POST", "/events")) == 5

    run(scenario)


def test_flapping_reports_one_callback_per_transition():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on(
                "POST",
                "/events",
                item(1),
                DISCONNECT,
                item(2),
                busy(500),
                busy(502),
                item(3),
                STALL,
                item(4),
            )
            recorder = Recorder()
            async with fast_client(url, recorder, events_timeout=0.05) as client:
                events = await take(client.events(backoff_initial=0.01, backoff_max=0.02), 4)
            assert [event.id for event in events] == [1, 2, 3, 4]
            assert recorder.calls == [False, True, False, True, False, True]

    run(scenario)


def test_callback_exceptions_do_not_break_the_stream(caplog):
    def broken(connected):
        raise RuntimeError(f"boom {PASSWORD}")

    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("POST", "/events", item(1), DISCONNECT, item(2))
            async with fast_client(url, broken) as client:
                events = await take(client.events(backoff_initial=0.01, backoff_max=0.02), 2)
                assert client.connected is True
            assert [event.id for event in events] == [1, 2]

    with caplog.at_level(logging.WARNING, logger="sabatark.client"):
        run(scenario)
    failures = [r for r in caplog.records if "Connection change callback failed" in r.message]
    assert len(failures) == 2
    assert all("RuntimeError" in r.message for r in failures)
    messages = [record.getMessage() for record in failures]
    assert all(PASSWORD not in m and "session-" not in m for m in messages)
    assert all(r.exc_info is None for r in failures)


def test_async_callback_is_awaited():
    calls = []

    async def callback(connected):
        calls.append(connected)

    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("POST", "/events", DISCONNECT, item(1))
            async with fast_client(url, callback) as client:
                await take(client.events(backoff_initial=0.01, backoff_max=0.02), 1)

    run(scenario)
    assert calls == [False, True]


def test_normal_requests_flip_the_state_too():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("GET", "/devices", busy(), reply(fixture("devices")))
            ctl.on("POST", "/x", DISCONNECT, reply({"ok": True}))
            recorder = Recorder()
            async with fast_client(url, recorder) as client:
                await client.login()
                with pytest.raises(AgatarkError) as error:
                    await client.devices()
                assert error.value.status == 503
                assert client.connected is False
                assert await client.devices()
                assert client.connected is True
                with pytest.raises(aiohttp.ClientError):
                    await client.request("POST", "/x")
                assert client.connected is False
                assert await client.request("POST", "/x") == {"ok": True}
                assert client.connected is True
            assert recorder.calls == [False, True, False, True]

    run(scenario)


def test_client_errors_count_as_connected():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("POST", "/events", DISCONNECT, reply({"title": "Nope"}, status=400))
            recorder = Recorder()
            async with fast_client(url, recorder) as client:
                with pytest.raises(AgatarkError) as error:
                    await take(client.events(backoff_initial=0.01, backoff_max=0.02), 1)
                assert error.value.status == 400
                assert client.connected is True
            assert recorder.calls == [False, True]

    run(scenario)


def test_504_marks_disconnected_and_still_raises():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("POST", "/events", reply({"message": "uplink"}, status=504))
            ctl.on("GET", "/rooms", reply({"message": "uplink"}, status=504), reply([]))
            recorder = Recorder()
            async with fast_client(url, recorder) as client:
                with pytest.raises(UplinkError):
                    await take(client.events(), 1)
                assert client.connected is False
                with pytest.raises(UplinkError):
                    await client.rooms()
                assert client.connected is False
            assert recorder.calls == [False]

    run(scenario)


def test_unreachable_controller_reports_disconnected_on_first_request():
    async def scenario():
        async with controller() as (ctl, url):
            pass
        recorder = Recorder()
        async with fast_client(url, recorder, timeout=2) as client:
            with pytest.raises(aiohttp.ClientError):
                await client.login()
            assert client.connected is False
        assert recorder.calls == [False]

    run(scenario)
