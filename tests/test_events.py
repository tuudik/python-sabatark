import asyncio

import pytest

from sabatark import (
    AgatarkClient,
    AgatarkError,
    EventDelta,
    EventLog,
    EventOp,
    UplinkError,
    parse_event,
)
from tests.fake_controller import (
    DISCONNECT,
    EMAIL,
    PASSWORD,
    controller,
    fixture,
    reply,
    run,
)

STREAM = fixture("events_stream")
BATCH_SIZES = [1, 2, 5, 3, 30, 1, 7, 25]


def batches(items, sizes):
    result, start = [], 0
    for size in sizes:
        result.append(items[start : start + size])
        start += size
    assert start == len(items)
    return result


async def take(iterator, count):
    result = []
    async for event in iterator:
        result.append(event)
        if len(result) == count:
            break
    return result


def fast_client(url):
    client = AgatarkClient(url, EMAIL, PASSWORD)
    client.empty_poll_interval = 0
    return client


def test_stream_batches_yield_every_item_in_order():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("POST", "/events", *[reply({"events": b}) for b in batches(STREAM, BATCH_SIZES)])
            async with fast_client(url) as client:
                iterator = client.events()
                events = await take(iterator, len(STREAM))
                await iterator.aclose()
            assert [event.raw for event in events] == STREAM
            assert sum(isinstance(e, EventLog) for e in events) == sum(
                item.get("op") == "log" for item in STREAM
            )
            assert all(isinstance(e, EventDelta | EventLog) for e in events)
            polls = ctl.requests("POST", "/events")
            assert len(polls) == len(BATCH_SIZES)
            assert all(p.body == b"" and "Content-Type" not in p.headers for p in polls)
            assert all(p.headers["Authorization"].startswith("S ") for p in polls)

    run(scenario)


def test_batched_edges_of_one_device_are_not_collapsed():
    burst = [
        {"id": 1006, "status": {"active": False}},
        {"id": 1006, "status": {"active": True}},
        {"id": 1006, "status": {"active": False}},
    ]

    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("POST", "/events", reply({"events": burst}))
            async with fast_client(url) as client:
                iterator = client.events()
                events = await take(iterator, 3)
                await iterator.aclose()
            assert [e.status["active"] for e in events] == [False, True, False]

    run(scenario)


def test_bare_array_reply_is_accepted():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("POST", "/events", reply([{"id": 1014, "status": {"current": 42}}]))
            async with fast_client(url) as client:
                iterator = client.events()
                (event,) = await take(iterator, 1)
                await iterator.aclose()
            assert event == EventDelta(
                id=1014, status={"current": 42}, raw={"id": 1014, "status": {"current": 42}}
            )

    run(scenario)


def test_event_shapes_and_unknown_items_never_crash():
    log = parse_event(STREAM[0])
    assert isinstance(log, EventLog)
    assert (log.id, log.by, log.gesture, log.path) == (1005, 50, "longPush", "/status/active")
    assert log.timestamp.year >= 2026
    setup = parse_event({"id": 1001, "setup": {"onTime": "2026-01-01T08:00:00.000Z"}})
    assert isinstance(setup, EventDelta) and setup.status is None
    assert setup.patch == {"setup": {"onTime": "2026-01-01T08:00:00.000Z"}}
    add = parse_event({"op": "add", "path": "/sites?id=1/devices", "value": {"id": 9}})
    assert add == EventOp(op="add", path="/sites?id=1/devices", value={"id": 9}, raw=add.raw)
    assert parse_event({"op": "remove", "id": 9}).id == 9
    assert parse_event({"op": "api.reload"}).op == "api.reload"
    for odd in [{"foo": 1}, 5, None, "x", {"op": "log", "id": 1}, {"id": "7", "status": {}}]:
        assert isinstance(parse_event(odd), EventOp)


def test_unknown_items_are_passed_through_by_the_iterator():
    odd = [{"foo": 1}, 5, {"op": "custom", "id": 3}]

    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("POST", "/events", reply({"events": odd}))
            async with fast_client(url) as client:
                iterator = client.events()
                events = await take(iterator, 3)
                await iterator.aclose()
            assert [type(e) for e in events] == [EventOp, EventOp, EventOp]
            assert events[2].op == "custom"

    run(scenario)


def test_network_errors_back_off_and_resume():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on(
                "POST",
                "/events",
                DISCONNECT,
                reply({"title": "busy"}, status=503),
                reply({"events": [{"id": 1, "status": {"x": 1}}]}),
            )
            async with fast_client(url) as client:
                iterator = client.events(backoff_initial=0.01, backoff_max=0.02)
                (event,) = await take(iterator, 1)
                await iterator.aclose()
            assert event.id == 1
            assert len(ctl.requests("POST", "/events")) == 3

    run(scenario)


def test_504_is_surfaced_through_the_iterator():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("POST", "/events", reply({"message": "uplink"}, status=504))
            async with fast_client(url) as client:
                with pytest.raises(UplinkError):
                    await take(client.events(), 1)

    run(scenario)


def test_second_concurrent_iterator_is_refused():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("POST", "/events", reply({"events": [{"id": 1, "status": {"x": 1}}]}))
            async with fast_client(url) as client:
                first = client.events()
                await anext(first)
                with pytest.raises(AgatarkError, match="separate client"):
                    await anext(client.events())
                await first.aclose()
                again = client.events()
                assert (await anext(again)).id == 1
                await again.aclose()

    run(scenario)


def test_empty_poll_probes_session_and_relogs_in():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on(
                "POST",
                "/events",
                lambda seen: reply(
                    {"events": [{"id": 2, "status": {"time": "08:15"}}] if ctl.authed(seen) else []}
                ),
            )
            async with fast_client(url) as client:
                client.session_probe_interval = 0
                await client.login()
                ctl.expire_sessions()
                (event,) = await take(client.events(), 1)
            assert event.id == 2
            assert ctl.logins == 2

    run(scenario)


def test_empty_polls_are_rate_limited():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("POST", "/events", reply({"events": []}), reply({"events": [{"id": 1, "x": 1}]}))
            async with AgatarkClient(url, EMAIL, PASSWORD) as client:
                client.empty_poll_interval = 0.2
                loop = asyncio.get_running_loop()
                started = loop.time()
                await take(client.events(), 1)
                assert loop.time() - started >= 0.2

    run(scenario)


def test_401_on_events_relogins_and_resumes():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on(
                "POST",
                "/events",
                lambda seen: (
                    reply({"events": [{"id": 7, "status": {"input": False}}]})(seen)
                    if ctl.authed(seen)
                    else reply({"message": "Unauthorized"}, status=401)(seen)
                ),
            )
            async with fast_client(url) as client:
                await client.login()
                ctl.expire_sessions()
                iterator = client.events()
                (event,) = await take(iterator, 1)
                await iterator.aclose()
            assert event.id == 7
            assert ctl.logins == 2
            first, second = ctl.requests("POST", "/events")[:2]
            assert first.headers["Authorization"] != second.headers["Authorization"]

    run(scenario)
