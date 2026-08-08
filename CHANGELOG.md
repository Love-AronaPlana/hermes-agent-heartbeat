# Changelog

## 0.1.2 (2026-08-08)

- Multi-session heartbeat: independent config per channel/thread
- `/heartbeat set` — enable heartbeat for the current conversation
- `/heartbeat unset` — disable heartbeat for the current conversation
- `/heartbeat list` — view all configured sessions and their status
- `/heartbeat config [key] [value]` — view/set per-session settings
- Per-session config stored in `~/.hermes/heartbeat/sessions.json`
- 26 unit tests, all passing

## 0.1.1 (2026-08-08)

- Add Chinese README (README_zh.md)

## 0.1.0 (2026-08-08)

- Initial release
- `pre_gateway_dispatch` hook — binds to Telegram session on first message
- `asyncio` loop — periodic `deliver_wake` into the same conversation
- `/heartbeat` slash command — manual trigger via `asyncio.Event`
- `[SILENT]` support — skip a cycle by returning this in the prompt
- Hot-reload — config and prompt file re-read every cycle
- Active time window — `active_start`/`active_end` with `utc_offset`
- Idle auto-pause — skip heartbeats when user is away (opt-in)
- Structured logging — every wakeup logged in `gateway.log`