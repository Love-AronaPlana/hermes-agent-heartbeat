# Changelog

## 0.3.1 (2026-08-28)

### 🐛 Fixed
- **Command replies now actually deliver** — `adapter.send()` is an async
  method but was being called synchronously, which created a coroutine that
  never ran. Command responses (e.g. `/xt status`) were silently dropped.
  Now scheduled via `loop.create_task()` on the running event loop.
- Added a warning log when the adapter lookup returns `None` so silent reply
  failures are visible in gateway logs instead of disappearing.

## 0.3.0 (2026-08-28)

### 🔧 Changed
- **Primary command renamed from `/heartbeat` to `/xt`** — the Hermes core
  ships a built-in `/heartbeat` whose aliases include `/hb`, so a plugin
  command named `/hb`/`/heartbeat` was being swallowed by the built-in handler
  before the plugin hook could run. `/xt` has no collision.
- `/heartbeat` and `/hb` are **also intercepted** and route to the same handler,
  so old muscle memory and the built-in name keep working.
- `/xt` is additionally registered via `register_command()` as a safety net
  (the hook interception remains the primary path).
- `_last_source` is now recorded before command interception, so intercepted
  commands always see the correct conversation context.
- 50 unit tests, all passing.

## 0.2.0 (2026-08-15)

### 🚀 New Features
- `/heartbeat stats` — view wakeup statistics (total, skipped, last error, last wakeup time)
- `/heartbeat stats clear` — reset statistics for the current session
- `/heartbeat test` — dry-run: check all config conditions without actually delivering a wakeup
- `/heartbeat pause [duration]` — temporarily pause heartbeats (`30m`, `2h`, or seconds)
- `/heartbeat resume` — resume paused heartbeats
- **Multi-prompt rotation** — `prompt_files` array, picked randomly each cycle
- **Jitter** — random interval offset (±N%) to avoid predictable beats
- **Persistent stats** — wakeup/skip counts survive gateway restarts
- **Graceful shutdown** — `on_session_end` hook cancels all heartbeat tasks
- **Multi-platform adapter** — dynamic adapter lookup, no hardcoded Telegram dependency

### 🐛 Bug Fixes
- **Hardcoded Telegram adapter** → replaced with `_adapter_for_source()` that dynamically looks up the correct platform adapter
- **`/heartbeat` triggered ALL sessions** → now only triggers the **current session** (via `_get_current_key()`)
- **Race condition on loop creation** → added `asyncio.Lock` (`_start_lock`) around task creation
- **Idle tracking counted agent messages** → `_is_user_message()` check ensures only user-initiated messages reset the idle timer
- **`stats clear` unreachable** → moved before the general `stats` handler so it actually executes

### 🔧 Improvements
- `_to_minutes()` extracted as standalone function (testable)
- `deliver_wake()` has its own try/except block with error tracking
- `_check_paused()` helper extracted for manual pause support
- `_adapter_for_source()` helper for dynamic platform lookup
- `_is_user_message()` helper for accurate idle detection

### 📦 Project Infrastructure
- Added `pyproject.toml` with project metadata and test dependencies
- Added `.gitignore` (Python, pytest, IDE, OS artifacts)
- Added `.github/workflows/test.yml` — CI on push/PR for Python 3.11/3.12
- Added `on_session_end` hook registration to test suite (2 hooks now)
- 34 unit tests (was 26), all passing

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