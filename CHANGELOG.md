# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-13

### Added

- `AgatarkClient`: async client for Agatark (EnLife) controllers built on aiohttp, with lazy
  login, "remember me" sessions and one automatic re-login when the controller rejects a
  session (a `401`, or an empty list plus a failed own-user check).
- Session reuse: `SessionState`, `session_state=` and `on_session_change=`, plus file helpers
  in `sabatark.session` that store sessions as `0600` files under `~/.config/sabatark/`.
- Connection-state reporting: `AgatarkClient.connected` and `on_connection_change=`, which
  fires once when a network error, timeout or HTTP 5xx makes the controller unreachable and
  once when it answers again, including outages that `events()` retries internally.
- Typed models for devices, sites, rooms, events and log entries, `parse_event()` and
  `merge_patch()`.
- Collection CRUD with `406` confirmation support, the long-polling `events()` iterator with
  backoff, and paged `logs()`.
- `derive_token()`, matching the web UI's PBKDF2 login token.
- Opt-in `WriteGuard` that limits writes to listed devices and sandbox-named users, rules and
  roles.
- `sabatark` command-line tool: `outputs`, `inputs`, `sensors`, `status`, `watch`, `logs`,
  `dump`, `set` (guarded with `--allow-device`) and `logout`.

[0.1.0]: https://github.com/tuudik/python-sabatark/releases/tag/v0.1.0
