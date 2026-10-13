import pytest

from sabatark import AgatarkClient, LogEntry
from tests.fake_controller import EMAIL, PASSWORD, controller, reply, run


def entries(start, count, model=1001):
    return [
        {
            "id": start - n,
            "time": 1_791_908_000_000 - (start - n) * 1000,
            "key": "changed:/status/output",
            "by": 50,
            "model": model,
            "value": True,
            "meta": None,
        }
        for n in range(count)
    ]


PAGE1 = entries(100, 3)
PAGE2 = entries(97, 2)
PAGE3 = entries(95, 2)


def paged(ctl):
    ctl.on(
        "GET",
        "/logs?model=1001",
        reply(PAGE1, headers={"Link": '</logs?model=1001&$next=1791907903000>; rel="next"'}),
    )
    ctl.on(
        "GET",
        "/logs?model=1001&$next=1791907903000",
        reply(PAGE2, headers={"Link": "</logs?model=1001&$next=1791907905000>; rel=next"}),
    )
    ctl.on("GET", "/logs?model=1001&$next=1791907905000", reply(PAGE3))


async def collect(iterator):
    return [entry async for entry in iterator]


def test_logs_follow_link_next_until_exhausted():
    async def scenario():
        async with controller() as (ctl, url):
            paged(ctl)
            async with AgatarkClient(url, EMAIL, PASSWORD) as client:
                logs = await collect(client.logs(model=1001))
            assert [entry.raw for entry in logs] == PAGE1 + PAGE2 + PAGE3
            assert [entry.id for entry in logs] == [100, 99, 98, 97, 96, 95, 94]
            assert len(ctl.requests("GET")) == 3

    run(scenario)


def test_logs_limit_stops_paging():
    async def scenario():
        async with controller() as (ctl, url):
            paged(ctl)
            async with AgatarkClient(url, EMAIL, PASSWORD) as client:
                assert len(await collect(client.logs(model=1001, limit=2))) == 2
                assert len(ctl.requests("GET")) == 1
                assert len(await collect(client.logs(model=1001, limit=4))) == 4
                assert len(ctl.requests("GET")) == 3
                assert await collect(client.logs(model=1001, limit=0)) == []

    run(scenario)


def test_logs_by_source_and_entry_fields():
    async def scenario():
        async with controller() as (ctl, url):
            ctl.on("GET", "/logs?by=50", reply(entries(5, 1)))
            async with AgatarkClient(url, EMAIL, PASSWORD) as client:
                (entry,) = await collect(client.logs(by=50))
            assert entry == LogEntry.from_dict(entries(5, 1)[0])
            assert (entry.id, entry.key, entry.by, entry.model, entry.value) == (
                5,
                "changed:/status/output",
                50,
                1001,
                True,
            )
            assert entry.timestamp.year >= 2026

    run(scenario)


@pytest.mark.parametrize("kwargs", [{}, {"model": 1, "by": 2}])
def test_logs_need_exactly_one_filter(kwargs):
    async def scenario():
        client = AgatarkClient("http://example.invalid", EMAIL, PASSWORD)
        with pytest.raises(ValueError):
            await collect(client.logs(**kwargs))

    run(scenario)
