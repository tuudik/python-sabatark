# python-sabatark

`sabatark` is an unofficial async Python client for Agatark (EnLife) home automation
controllers. It talks to the controller's HTTP API with [aiohttp](https://docs.aiohttp.org/)
and covers login and session reuse, devices, sites and rooms, the generic collections, the
live `/events` stream and the change log. A small `sabatark` command-line tool is included.

The package is typed (`py.typed`) and needs Python 3.14 or newer.

## Install

```sh
pip install sabatark
```

or, in a uv project:

```sh
uv add sabatark
```

## Quick start

```python
import asyncio

from sabatark import AgatarkClient, EventDelta, EventLog


async def main() -> None:
    async with AgatarkClient(
        "http://controller.local",
        email="user@example.com",
        password="your-password",
    ) as client:
        for device in await client.devices():
            if device.is_output:
                print(device.id, device.name, device.status.get("output"))

        await client.set_output(1001, True)

        async for event in client.events():
            if isinstance(event, EventDelta):
                print("changed", event.id, event.patch)
            elif isinstance(event, EventLog):
                print("gesture", event.id, event.gesture, event.path)


asyncio.run(main())
```

The client logs in on its first request. Useful entry points:

- `devices()`, `device(id)`, `sites()`, `rooms()` return typed models (`Device`, `Site`,
  `Room`). Every model keeps the original document in `.raw`.
- `list(collection, **query)`, `get(collection, id)`, `create(collection, data, site_id)`,
  `patch(collection, id, data)` and `delete(collection, id)` work on `devices`, `sites`,
  `rooms`, `rules`, `users`, `roles` and `securityAreas`.
- `request(method, path, body)` sends a raw API request with the session applied.
- `events()` long-polls `/events` and yields `EventDelta`, `EventLog` or `EventOp` items in
  order. Merge a delta into a cached device with `device.apply(delta)`.
- `logs(model=id)` or `logs(by=id)` yields `LogEntry` items, newest first, across pages.

Errors derive from `AgatarkError`. `AuthError` covers rejected logins, `PasswordChangeRequired`
is HTTP 454, `UplinkError` is HTTP 504 and `ConfirmationRequired` is HTTP 406: retry the same
call with `confirm=exc.action` once the user has agreed.

`client.connected` tells whether the controller answered the last request: `None` before the
first request completes, `False` after a network error, timeout or HTTP 5xx (including the
`504` that still raises `UplinkError`), and `True` once a request or event poll gets any other
reply. Pass `on_connection_change=` (a plain or async callable taking a `bool`) to hear about
each change; it fires once per transition, not for the first successful request. `events()`
keeps retrying through outages, so this is how a consumer notices that the controller is
unreachable.

### Write guard

Writes are unrestricted by default. Pass a `WriteGuard` to allow only specific targets; any
other write raises `WriteBlocked` before a request is sent:

```python
from sabatark import AgatarkClient, WriteGuard

guard = WriteGuard(devices={1001, 1002}, sandbox_prefix="test-")
client = AgatarkClient("http://controller.local", "user@example.com", "your-password", guard=guard)
```

The guard allows reads, event polls, `PATCH` on the listed devices, and changes to users,
rules and roles whose name starts with `sandbox_prefix`. Use `sandbox_prefix=None` to allow
devices only.

## Command line

The `sabatark` command reads `SABATARK_URL`, `SABATARK_USERNAME` and `SABATARK_PASSWORD`
from the environment, or from a `.env` file in the working directory. Variables that are
already set win over the file.

```sh
export SABATARK_URL=http://192.0.2.10
export SABATARK_USERNAME=user@example.com
export SABATARK_PASSWORD=your-password

sabatark outputs              # list output devices
sabatark inputs               # switches, motion detectors and other inputs
sabatark sensors              # climate sensors
sabatark status 1001          # one device
sabatark watch --ids 1001     # stream live events; Ctrl+C stops
sabatark logs 1001 --limit 10 # change log of one device
sabatark dump ./backup        # every collection as JSON files
sabatark set 1001 on          # switch an output
sabatark logout               # end the stored session
```

Add `--json` to any listing for raw JSON. `--url` overrides `SABATARK_URL`.

**`sabatark set` is unguarded by default.** Pass `--allow-device ID` (repeatable) to refuse
any write to a device that is not on the list:

```sh
sabatark set 1001 off --allow-device 1001
```

A refused write exits with status 3; other failures exit with status 2.

## Sessions

The client logs in with "remember me" (`"remember": "on"`), so a session stays valid for a
long time. Pass `remember=False` to `AgatarkClient` to turn that off. The client logs in again
only when the controller rejects the session, and closing a client does not log out. Call
`logout()` or `close(logout=True)` to end the session on the controller.

To reuse a session across restarts, give the client the stored `SessionState` and a callback
that saves new ones:

```python
from functools import partial

from sabatark import AgatarkClient
from sabatark.session import load_session, persist_session, session_path

path = session_path("http://controller.local", "user@example.com", "my-app")
client = AgatarkClient(
    "http://controller.local",
    "user@example.com",
    "your-password",
    session_state=load_session(path),
    on_session_change=partial(persist_session, path),
)
```

The CLI does this for you. Session files live in `${XDG_CONFIG_HOME:-~/.config}/sabatark/`
and are named `session-<hash>-<consumer>.json`, for example `session-<hash>-cli.json`. The
hash covers the controller URL and the e-mail address. The directory is `0700` and each file
is `0600`. Files are written atomically. They never contain the password or the login token.
A corrupt or unreadable file is ignored and replaced on the next login.

**A session file is password-equivalent.** Anyone who can read it can use the controller as
you until that session is logged out.

Every consumer should keep its own session. The controller has one `/events` queue per
session, so two consumers that share a session split the events between them and each misses
changes. For the same reason a client allows only one `events()` iterator at a time.

`sabatark logout` ends the CLI's stored session on the controller and deletes its file.
`--no-session-cache` keeps the session in memory only and logs out when the command finishes.

## Protocol notes

- **Remember me.** Login is `PUT /hello` with the e-mail (trimmed, lower-cased), a PBKDF2
  token derived from the password, and `"remember": "on"`. `derive_token()` reproduces the
  web UI's derivation, including its quirks for long passwords.
- **One event queue per session.** `POST /events` is a long poll on a queue that belongs to
  the session. Items arrive in order and are yielded one by one, never collapsed.
- **An expired session can look like an empty list.** List reads answer `200 []` instead of
  `401` when the session is no longer valid. When a list comes back empty the client checks
  its own user with `GET /users?id=<me>`, logs in again if that fails, and retries. A `401`
  on any request, including `/events`, triggers one re-login and a retry.
- Requests without a body carry no `Content-Type`; the controller rejects unexpected media
  types.

## Development

```sh
uv sync
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

The tests run offline against an in-process fake controller with synthetic fixtures.

## Disclaimer

This project is unofficial. It is not affiliated with, endorsed by or supported by Agatark,
EnLife or any related company. Product names are used only to describe compatibility. Use it
at your own risk; writes change real devices in your home.

## License

MIT. See [LICENSE](LICENSE).
