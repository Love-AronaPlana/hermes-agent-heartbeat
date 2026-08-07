# Changelog

## 1.0.0 (2026-08-08)

- Initial release
- `pre_gateway_dispatch` hook — binds to Telegram session on first message
- `asyncio` loop — periodic `deliver_wake` into the same conversation
- `/heartbeat` slash command — manual trigger via `asyncio.Event`
- `[SILENT]` support — skip a cycle by returning this in the prompt
- Hot-reload — config and prompt file re-read every cycle
- Active time window — `active_start`/`active_end` with `utc_offset`
- Idle auto-pause — skip heartbeats when user is away (opt-in)
- Structured logging — every wakeup logged in `gateway.log`