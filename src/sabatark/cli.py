import argparse
import asyncio
import json
import os
import shlex
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from aiohttp import ClientError

from sabatark import (
    COLLECTIONS,
    AgatarkClient,
    AgatarkError,
    AuthError,
    Device,
    EventDelta,
    EventLog,
    WriteBlocked,
    WriteGuard,
)
from sabatark.session import delete_session, load_session, persist_session, session_path


def load_env(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key not in {"SABATARK_URL", "SABATARK_USERNAME", "SABATARK_PASSWORD"}:
            continue
        if key in os.environ:
            continue
        value = value.strip()
        if value.startswith(("'", '"')):
            try:
                parts = shlex.split(value, comments=True)
            except ValueError:
                raise ValueError(f"Invalid quoting for {key} in environment file") from None
            if len(parts) != 1:
                raise ValueError(f"Invalid quoting for {key} in environment file")
            value = parts[0]
        else:
            value = value.partition(" #")[0].rstrip()
        os.environ[key] = value


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Expected a positive integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("Expected a positive integer")
    return number


def device_ids(value: str) -> set[int]:
    try:
        return {positive_int(item.strip()) for item in value.split(",")}
    except argparse.ArgumentTypeError:
        raise argparse.ArgumentTypeError("Expected comma-separated positive device ids") from None


COMMAND_HELP = {
    "outputs": "list output devices",
    "inputs": "list switches, motion detectors and other inputs",
    "sensors": "list climate sensors",
    "status": "show one device",
    "watch": "stream live events",
    "logs": "show the change log of one device",
}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="sabatark",
        description="Command-line client for Agatark (EnLife) controllers. Reads SABATARK_URL, "
        "SABATARK_USERNAME and SABATARK_PASSWORD from the environment or from ./.env.",
    )
    result.add_argument("--url", default=os.environ.get("SABATARK_URL", ""))
    result.add_argument("--json", action="store_true", help="print raw JSON")
    result.add_argument(
        "--no-session-cache",
        action="store_true",
        help="keep the session in memory only and log out when the command finishes",
    )
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("logout", help="end the stored session and delete its file")
    for name, text in COMMAND_HELP.items():
        command = commands.add_parser(name, help=text)
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        if name in {"status", "logs"}:
            command.add_argument("id", type=positive_int)
        if name == "watch":
            command.add_argument("--ids", type=device_ids)
        if name == "logs":
            command.add_argument("--limit", type=positive_int, default=30)
    set_command = commands.add_parser(
        "set",
        help="switch an output on or off (unguarded unless --allow-device is given)",
        description="Switch an output on or off. Writes are unguarded unless --allow-device is "
        "given; with it, only the listed device ids may be written.",
    )
    set_command.add_argument("id", type=positive_int)
    set_command.add_argument("state", choices=("on", "off"))
    set_command.add_argument(
        "--allow-device",
        action="append",
        type=positive_int,
        metavar="ID",
        help="refuse the write unless the device is in this allow-list (repeatable)",
    )
    dump_command = commands.add_parser("dump", help="save every collection as JSON files")
    dump_command.add_argument("destination", type=Path)
    return result


def print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def cell(value: object) -> str:
    if isinstance(value, str):
        return value.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def table(headers: tuple[str, ...], rows: list[tuple[object, ...]]) -> None:
    rendered = [headers, *(tuple(cell(value) for value in row) for row in rows)]
    widths = [max(len(row[index]) for row in rendered) for index in range(len(headers))]
    for row in rendered:
        print(
            "  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)).rstrip()
        )


def has_role(device: Device, role: str) -> bool:
    if role == "outputs":
        return device.is_output
    if role == "inputs":
        return device.is_switch or device.is_motion or device.type == "input"
    return device.type == "climateSensor" or any(
        key in device.status for key in ("temperature", "rh", "humidity", "co2")
    )


def print_devices(devices: list[Device]) -> None:
    table(
        ("ID", "NAME", "TYPE", "STATUS"),
        [
            (
                device.id,
                device.name,
                device.type,
                " ".join(f"{key}={cell(value)}" for key, value in device.status.items()) or "-",
            )
            for device in devices
        ],
    )


async def watch_names(client: AgatarkClient) -> dict[int, str]:
    collections = await asyncio.gather(
        client.devices(),
        client.rooms(),
        client.sites(),
        client.list("users"),
        return_exceptions=True,
    )
    names = {}
    for items in collections:
        if isinstance(items, BaseException):
            continue
        for item in items:
            item_id, name = (
                (item.get("id"), item.get("name"))
                if isinstance(item, dict)
                else (item.id, item.name)
            )
            if isinstance(item_id, int) and isinstance(name, str) and name:
                names[item_id] = name
    return names


def write_guard(args: argparse.Namespace) -> WriteGuard | None:
    """Return a device allow-list guard when --allow-device was given, otherwise None."""
    allowed = getattr(args, "allow_device", None)
    return WriteGuard(allowed, sandbox_prefix=None) if allowed else None


async def run(args: argparse.Namespace) -> None:
    if not args.url:
        raise ValueError("Provide --url or export SABATARK_URL")
    email = os.environ.get("SABATARK_USERNAME")
    path = None if args.no_session_cache or not email else session_path(args.url, email, "cli")
    client = AgatarkClient(
        args.url,
        email=email,
        password=os.environ.get("SABATARK_PASSWORD"),
        guard=write_guard(args),
        session_state=await asyncio.to_thread(load_session, path) if path else None,
        on_session_change=partial(persist_session, path) if path else None,
    )
    try:
        if args.command == "logout":
            await logout(client, path)
        else:
            await command(client, args)
    finally:
        await client.close(logout=path is None)


async def logout(client: AgatarkClient, path: Path | None) -> None:
    """Log the stored session out on the controller and delete its file."""
    try:
        if client.logged_in:
            await client.logout()
    except AuthError:
        pass
    finally:
        if path is not None:
            await asyncio.to_thread(delete_session, path)


async def command(client: AgatarkClient, args: argparse.Namespace) -> None:
    """Run one data command with an open client."""
    if args.command == "set":
        await client.set_output(args.id, args.state == "on")
    elif args.command in {"outputs", "inputs", "sensors"}:
        devices = [device for device in await client.devices() if has_role(device, args.command)]
        if args.json:
            print_json([device.raw for device in devices])
        else:
            print_devices(devices)
    elif args.command == "status":
        device = await client.device(args.id)
        if device is None:
            raise ValueError(f"Device {args.id} was not found")
        if args.json:
            print_json(device.status)
        else:
            print_devices([device])
    elif args.command == "watch":
        names = await watch_names(client)
        async for event in client.events():
            item_id = event.id
            if item_id is None and isinstance(getattr(event, "value", None), dict):
                item_id = event.value.get("id")
            if args.ids is not None and item_id not in args.ids:
                continue
            if args.json:
                print(json.dumps(event.raw, ensure_ascii=False), flush=True)
                continue
            stamp = (
                event.timestamp.isoformat()
                if isinstance(event, EventLog)
                else datetime.now(UTC).isoformat()
            )
            change = (
                cell(event.patch)
                if isinstance(event, EventDelta)
                else event.key
                if isinstance(event, EventLog)
                else cell(event.raw)
            )
            print(f"{stamp}  {cell(names.get(item_id) or str(item_id))}  {change}", flush=True)
    elif args.command == "logs":
        logs = [entry async for entry in client.logs(model=args.id, limit=args.limit)]
        if args.json:
            print_json([entry.raw for entry in logs])
        else:
            table(
                ("TIME", "KEY", "BY", "VALUE"),
                [(entry.timestamp.isoformat(), entry.key, entry.by, entry.value) for entry in logs],
            )
    elif args.command == "dump":
        args.destination.mkdir(parents=True, exist_ok=True)
        for collection in sorted(COLLECTIONS):
            payload = await client.list(collection)
            await asyncio.to_thread(
                (args.destination / f"{collection}.json").write_text,
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )


def main(argv: list[str] | None = None) -> None:
    arguments = parser()
    try:
        load_env()
        args = parser().parse_args(argv)
        asyncio.run(run(args))
    except WriteBlocked as exc:
        arguments.exit(3, f"sabatark: {exc}\n")
    except AgatarkError, ClientError, TimeoutError, OSError, ValueError, NotImplementedError:
        arguments.exit(
            2, "sabatark: Command failed; check configuration and controller availability\n"
        )
    except KeyboardInterrupt:
        return
