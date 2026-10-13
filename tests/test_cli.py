import json

import pytest

from sabatark import (
    COLLECTIONS,
    AgatarkError,
    Device,
    LogEntry,
    Room,
    Site,
    WriteBlocked,
    WriteGuard,
    cli,
    parse_event,
)

DEVICES = [
    {"id": 1001, "name": "Stair light", "type": "output", "status": {"output": True}},
    {"id": 1002, "name": "Other light", "type": "light", "status": {"output": False}},
    {"id": 1005, "name": "Wall switch", "type": "switch", "status": {"input": True}},
    {
        "id": 1006,
        "name": "Hall motion",
        "type": "input",
        "usage": {"securityDetector": ["motion"]},
        "status": {"active": False},
    },
    {"id": 1008, "name": "Door contact", "type": "input", "status": {"open": False}},
    {
        "id": 1009,
        "name": "Climate",
        "type": "climateSensor",
        "status": {"temperature": 21.5, "rh": 45, "co2": 620},
    },
]
EVENTS = [
    {"id": 1005, "status": {"input": False}},
    {"id": 1005, "status": {"input": True}},
    {"op": "log", "id": 1005, "time": 1700000000000, "by": 50, "key": "singleClick:/status/input"},
    {"id": 1001, "status": {"output": False}},
    {"op": "add", "path": "/devices", "value": {"id": 9999, "name": "New device"}},
    {"op": "api.reload"},
]
LOG = {
    "id": 1,
    "time": 1700000000000,
    "key": "changed:/status/output",
    "by": 50,
    "model": 1001,
    "value": True,
}
USERS = [{"id": 50, "name": "Dashboard user"}]


@pytest.fixture
def fake_client(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in ("SABATARK_URL", "SABATARK_USERNAME", "SABATARK_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SABATARK_URL", "http://example.invalid")
    monkeypatch.setenv("SABATARK_USERNAME", "fake@example.org")
    monkeypatch.setenv("SABATARK_PASSWORD", "synthetic-password")
    instances = []

    class FakeClient:
        failures = {}
        event_items = EVENTS

        def __init__(self, url, email=None, password=None, guard=None, **session):
            self.url, self.email, self.password, self.guard = url, email, password, guard
            self.session = session
            self.calls = []
            self.closed = False
            self.blocked = False
            self.interrupt = False
            instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            await self.close()

        async def close(self, logout=False):
            self.closed = True

        async def devices(self):
            self.calls.append(("devices",))
            if "devices" in self.failures:
                raise self.failures["devices"]
            return [Device.from_dict(value) for value in DEVICES]

        async def rooms(self):
            self.calls.append(("rooms",))
            if "rooms" in self.failures:
                raise self.failures["rooms"]
            return [Room(id=70, name="Dining room")]

        async def sites(self):
            self.calls.append(("sites",))
            if "sites" in self.failures:
                raise self.failures["sites"]
            return [Site(id=7, name="Home site")]

        async def device(self, item_id):
            self.calls.append(("device", item_id))
            return next(
                (Device.from_dict(value) for value in DEVICES if value["id"] == item_id), None
            )

        async def set_output(self, item_id, on):
            self.calls.append(("set_output", item_id, on))
            if self.guard is not None:
                self.guard.check("PATCH", "devices", item_id, data={"status": {"output": on}})

        async def events(self):
            self.calls.append(("events",))
            for value in self.event_items:
                yield parse_event(value)

        async def logs(self, model=None, by=None, limit=None):
            self.calls.append(("logs", model, by, limit))
            yield LogEntry(**LOG, raw=LOG)

        async def list(self, collection, **query):
            self.calls.append(("list", collection, query))
            if collection in self.failures:
                raise self.failures[collection]
            if collection == "users":
                return USERS
            return [{"id": 1, "name": collection}]

    monkeypatch.setattr(cli, "AgatarkClient", FakeClient)
    return instances


@pytest.mark.parametrize(
    ("command", "expected"),
    [("outputs", [1001, 1002]), ("inputs", [1005, 1006, 1008]), ("sensors", [1009])],
)
def test_device_role_tables(fake_client, capsys, command, expected):
    cli.main([command])
    output = capsys.readouterr().out
    assert output.splitlines()[0].split() == ["ID", "NAME", "TYPE", "STATUS"]
    assert [int(line.split()[0]) for line in output.splitlines()[1:]] == expected
    for value in DEVICES:
        if value["id"] in expected:
            assert value["name"] in output
            for key in value["status"]:
                assert f"{key}=" in output
    assert fake_client[0].calls == [("devices",)]
    assert fake_client[0].closed


@pytest.mark.parametrize("command", ["outputs", "inputs", "sensors"])
def test_device_json_is_raw(fake_client, capsys, command):
    cli.main([command, "--json"])
    values = json.loads(capsys.readouterr().out)
    assert values == [value for value in DEVICES if cli.has_role(Device.from_dict(value), command)]
    assert "room_id" not in values[0]


def test_global_json_flag(fake_client, capsys):
    cli.main(["--json", "outputs"])
    assert len(json.loads(capsys.readouterr().out)) == 2


def test_credentials_and_guard(fake_client, capsys):
    cli.main(["outputs"])
    client = fake_client[0]
    assert client.url == "http://example.invalid"
    assert client.email == "fake@example.org"
    assert client.password == "synthetic-password"
    assert client.guard is None
    output = capsys.readouterr()
    assert client.email not in output.out + output.err
    assert client.password not in output.out + output.err


def test_status(fake_client, capsys):
    cli.main(["status", "1001"])
    assert "Stair light" in capsys.readouterr().out
    assert fake_client[0].calls == [("device", 1001)]
    cli.main(["status", "1001", "--json"])
    assert json.loads(capsys.readouterr().out) == {"output": True}


def test_missing_status(fake_client, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["status", "9999"])
    assert exc.value.code == 2
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("state", ["on", "off"])
def test_set_without_allow_list_is_unguarded(fake_client, state):
    cli.main(["set", "1002", state])
    assert fake_client[0].guard is None
    assert fake_client[0].calls == [("set_output", 1002, state == "on")]
    assert fake_client[0].closed


@pytest.mark.parametrize(
    ("arguments", "on"),
    [
        (["set", "1001", "on", "--allow-device", "1001"], True),
        (["set", "--allow-device", "1003", "--allow-device", "1001", "1001", "off"], False),
    ],
)
def test_set_with_allow_list_uses_the_guard(fake_client, arguments, on):
    cli.main(arguments)
    guard = fake_client[0].guard
    assert isinstance(guard, WriteGuard)
    assert 1001 in guard.devices
    assert guard.sandbox_prefix is None
    assert fake_client[0].calls == [("set_output", 1001, on)]


def test_blocked_set_exits_three(fake_client, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["set", "1002", "on", "--allow-device", "1001"])
    assert exc.value.code == 3
    assert capsys.readouterr().err.startswith("sabatark:")
    assert fake_client[0].closed


def test_reads_never_use_a_guard(fake_client):
    cli.main(["status", "1001"])
    assert fake_client[0].guard is None


def test_set_help_says_writes_are_unguarded(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    with pytest.raises(SystemExit) as exc:
        cli.main(["set", "--help"])
    assert exc.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "unguarded unless --allow-device" in text
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "unguarded" in capsys.readouterr().out


def test_watch_preserves_edges_and_filters(fake_client, capsys):
    cli.main(["watch", "--ids", "1005", "--json"])
    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line) for line in lines] == EVENTS[:3]
    assert fake_client[0].calls == [
        ("devices",),
        ("rooms",),
        ("sites",),
        ("list", "users", {}),
        ("events",),
    ]


def test_watch_named_lines(fake_client, capsys):
    cli.main(["watch"])
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == len(EVENTS)
    assert "Wall switch" in lines[0]
    assert '"input":false' in lines[0]
    assert '"input":true' in lines[1]
    assert "singleClick:/status/input" in lines[2]
    assert "2023-11-14T22:13:20+00:00" in lines[2]
    assert "Stair light" in lines[3]
    assert "9999" in lines[4]
    assert "api.reload" in lines[5]


def test_watch_filter_add_id(fake_client, capsys):
    cli.main(["watch", "--ids", "9999", "--json"])
    assert json.loads(capsys.readouterr().out) == EVENTS[4]


def test_watch_ctrl_c_is_clean(fake_client, monkeypatch, capsys):
    def interrupted(coroutine):
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.asyncio, "run", interrupted)
    cli.main(["watch"])
    output = capsys.readouterr()
    assert output.out == output.err == ""


def test_logs(fake_client, capsys):
    cli.main(["logs", "1001", "--limit", "2"])
    output = capsys.readouterr().out
    assert "changed:/status/output" in output
    assert "2023-11-14T22:13:20+00:00" in output
    assert fake_client[0].calls == [("logs", 1001, None, 2)]
    cli.main(["logs", "1001", "--json"])
    assert json.loads(capsys.readouterr().out) == [LOG]


def test_dump_writes_every_collection(fake_client, tmp_path, capsys):
    destination = tmp_path / "dump"
    cli.main(["dump", str(destination)])
    assert {path.stem for path in destination.iterdir()} == set(COLLECTIONS)
    for collection in COLLECTIONS:
        assert json.loads((destination / f"{collection}.json").read_text()) == (
            USERS if collection == "users" else [{"id": 1, "name": collection}]
        )
    assert all(call[0] == "list" for call in fake_client[0].calls)
    assert capsys.readouterr().out == ""


def test_env_file_is_loaded_without_overriding_process_env(
    fake_client, tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("SABATARK_USERNAME")
    monkeypatch.delenv("SABATARK_PASSWORD")
    (tmp_path / ".env").write_text(
        "# synthetic credentials only\n"
        "SABATARK_URL=http://ignored.invalid\n"
        "export SABATARK_USERNAME='file@example.org'\n"
        'SABATARK_PASSWORD="space # dollar$ password" # comment\n'
        "IGNORED_KEY=ignored\n"
    )
    cli.main(["outputs"])
    client = fake_client[0]
    assert client.url == "http://example.invalid"
    assert client.email == "file@example.org"
    assert client.password == "space # dollar$ password"
    assert "IGNORED_KEY" not in cli.os.environ
    assert "file@example.org" not in capsys.readouterr().out


def test_configuration_error_does_not_expose_values(fake_client, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SABATARK_PASSWORD")
    (tmp_path / ".env").write_text('SABATARK_PASSWORD="synthetic-secret-unclosed\n')
    with pytest.raises(SystemExit) as exc:
        cli.main(["outputs"])
    assert exc.value.code == 2
    assert "synthetic-secret" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "arguments", [["watch", "--ids", "a"], ["logs", "1001", "--limit", "0"], ["status", "bad"]]
)
def test_invalid_arguments(fake_client, arguments):
    with pytest.raises(SystemExit) as exc:
        cli.main(arguments)
    assert exc.value.code == 2
    assert not fake_client


def test_blocked_client_error_is_caught(fake_client, monkeypatch):
    def blocked(coroutine):
        coroutine.close()
        raise WriteBlocked("blocked by fake client")

    monkeypatch.setattr(cli.asyncio, "run", blocked)
    with pytest.raises(SystemExit) as exc:
        cli.main(["set", "1002", "on"])
    assert exc.value.code == 3


@pytest.mark.parametrize("failed_collection", [None, "devices", "rooms", "sites", "users", "all"])
def test_watch_resolves_all_collections_and_tolerates_failures(
    fake_client, monkeypatch, capsys, failed_collection
):
    events = [
        {"id": 1001, "status": {"output": True}},
        {"id": 70, "status": {"co2": 869}},
        {"id": 7, "status": {"online": True}},
        {"id": 50, "status": {"dot": "green"}},
        {"id": 9999, "status": {"online": False}},
    ]
    failures = {
        "devices": AgatarkError("Synthetic devices failure"),
        "rooms": TimeoutError("Synthetic rooms failure"),
        "sites": OSError("Synthetic sites failure"),
        "users": NotImplementedError("Synthetic users failure"),
    }
    monkeypatch.setattr(cli.AgatarkClient, "event_items", events)
    monkeypatch.setattr(
        cli.AgatarkClient,
        "failures",
        failures
        if failed_collection == "all"
        else {failed_collection: failures[failed_collection]}
        if failed_collection is not None
        else {},
    )
    cli.main(["watch"])
    output = capsys.readouterr()
    names = [
        ("devices", "Stair light"),
        ("rooms", "Dining room"),
        ("sites", "Home site"),
        ("users", "Dashboard user"),
    ]
    lines = output.out.splitlines()
    assert len(lines) == len(events)
    for line, event, (collection, name) in zip(lines[:-1], events[:-1], names, strict=True):
        assert line.split("  ")[1] == (
            str(event["id"]) if failed_collection in {collection, "all"} else name
        )
    assert lines[-1].split("  ")[1] == "9999"
    assert fake_client[0].calls == [
        ("devices",),
        ("rooms",),
        ("sites",),
        ("list", "users", {}),
        ("events",),
    ]
    assert fake_client[0].closed
    assert output.err == ""
