"""Persistent heartbeat plugin — periodic wakeups in the current gateway session.

Binds to the ``pre_gateway_dispatch`` hook: when a matching message arrives, it
starts an asyncio loop that periodically injects a prompt into the **same**
gateway session via ``deliver_wake``. The agent remembers the conversation,
and the user can reply between wakeups — context is fully preserved.

Supports multiple sessions (different channels/threads), each with its own
config. Manage via ``/hb set|unset|list|config|stats|test|pause|resume``
slash commands.

License: MIT
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hermes_cli.config import load_config
from gateway.platforms.base import Platform
from gateway.wake import deliver_wake

logger = logging.getLogger(__name__)

# ── constants ──────────────────────────────────────────────────────────────────

_MIN_INTERVAL = 60.0
_MAX_INTERVAL = 86400.0
_DEFAULT_INTERVAL = 900.0
_DEFAULT_JITTER = 0.0
_DEFAULT_PAUSE_DURATION = 3600  # 1 hour default pause

_SESSIONS_FILE = Path("~/.hermes/heartbeat/sessions.json").expanduser()
_STATS_FILE = Path("~/.hermes/heartbeat/stats.json").expanduser()

# ── module state ───────────────────────────────────────────────────────────────

_tasks: dict[str, asyncio.Task] = {}
_triggers: dict[str, asyncio.Event] = {}  # key -> manual trigger event
_last_user_message: dict[str, float] = {}  # key -> last USER message timestamp
_gateway_ref: Any = None  # last seen gateway (for slash command)
_sources: dict[str, Any] = {}  # key -> SessionSource for manual trigger
_last_source: Any = None  # source of the last incoming user message
_start_lock: asyncio.Lock = asyncio.Lock()  # prevent race on loop creation

# ── config helpers ─────────────────────────────────────────────────────────────


def _global_config() -> dict[str, Any]:
    """Read the global ``agent_heartbeat`` section from config.yaml."""
    try:
        value = (load_config() or {}).get("agent_heartbeat", {})
    except Exception:
        logger.exception("agent-heartbeat: global config load failed")
        return {}
    return value if isinstance(value, dict) else {}


def _load_sessions() -> dict[str, dict[str, Any]]:
    """Load per-session config from ``sessions.json``."""
    try:
        if _SESSIONS_FILE.exists():
            with _SESSIONS_FILE.open(encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        logger.exception("agent-heartbeat: failed to load sessions.json")
    return {}


def _save_sessions(data: dict[str, dict[str, Any]]) -> None:
    """Save per-session config to ``sessions.json``."""
    _SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _SESSIONS_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError:
        logger.exception("agent-heartbeat: failed to save sessions.json")


def _session_defaults() -> dict[str, Any]:
    """Return default values for a session config."""
    g = _global_config()
    return {
        "enabled": True,
        "interval": float(g.get("default_interval", _DEFAULT_INTERVAL)),
        "prompt_file": str(g.get("default_prompt_file", "") or ""),
        "prompt_files": list(g.get("default_prompt_files", []) or []),
        "active_start": "",
        "active_end": "",
        "utc_offset": "+8",
        "idle_auto_pause_enabled": False,
        "idle_auto_pause_minutes": 120,
        "paused_until": "",  # ISO timestamp; empty = not paused
    }


def _get_session_config(key: str) -> dict[str, Any]:
    """Get merged config for a session (sessions.json overrides defaults)."""
    sessions = _load_sessions()
    s = sessions.get(key, {})
    defaults = _session_defaults()
    merged = dict(defaults)
    merged.update(s)
    return merged


# ── helpers ────────────────────────────────────────────────────────────────────


def _session_key(source: Any) -> str:
    """Build a unique session key from a message source."""
    platform = getattr(source, "platform", None)
    if platform is not None:
        return f"{platform.value}:{source.chat_id}:{source.thread_id or ''}"
    return "unknown:unknown"


def _format_source(key: str) -> str:
    """Pretty-format a session key for display."""
    parts = key.split(":", 2)
    platform = parts[0] if len(parts) > 0 else "?"
    chat_id = parts[1] if len(parts) > 1 else "?"
    thread = parts[2] if len(parts) > 2 and parts[2] else "DM"
    return f"{platform}/{chat_id}/{thread}"


def _is_session_active(session_key: str) -> bool:
    """Check if a session is configured and enabled."""
    g = _global_config()
    if not bool(g.get("enabled", False)):
        return False
    sessions = _load_sessions()
    s = sessions.get(session_key, {})
    return bool(s.get("enabled", False))


def _adapter_for_source(gateway: Any, source: Any) -> Any:
    """Get the adapter for the given source's platform."""
    platform = getattr(source, "platform", None)
    if platform is None:
        return None
    return gateway.adapters.get(platform)


def _to_minutes(t: str) -> int:
    """Convert 'HH:MM' or 'HH' to minutes since midnight."""
    parts = t.strip().split(":")
    if not parts or not parts[0]:
        return 0
    try:
        return int(parts[0]) * 60 + (int(parts[1]) if len(parts) == 2 else 0)
    except (ValueError, IndexError):
        return 0


def _prompt(sc: dict[str, Any]) -> str:
    """Read prompt from file, multi-prompt rotation, or inline string."""
    # Try multi-prompt rotation first
    prompt_files = sc.get("prompt_files", []) or []
    if prompt_files and isinstance(prompt_files, list):
        # Filter out empty strings
        valid_files = [p for p in prompt_files if p and str(p).strip()]
        if valid_files:
            choice = random.choice(valid_files)
            try:
                text = Path(str(choice)).expanduser().read_text(encoding="utf-8").strip()
                if text:
                    return text
            except (FileNotFoundError, OSError):
                logger.warning("agent-heartbeat: prompt file missing: %s", choice)

    # Fall back to single prompt_file
    prompt_file = str(sc.get("prompt_file", "") or "").strip()
    if prompt_file:
        try:
            text = Path(prompt_file).expanduser().read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            logger.warning("agent-heartbeat: prompt file missing: %s", prompt_file)
            text = ""
        except OSError:
            logger.exception("agent-heartbeat: prompt read failed: %s", prompt_file)
            text = ""
        if text:
            return text

    # Fall back to inline prompt
    return str(sc.get("prompt", "") or "").strip()


def _interval(sc: dict[str, Any]) -> float:
    """Get the interval in seconds, with optional jitter."""
    try:
        value = float(sc.get("interval", _DEFAULT_INTERVAL))
    except (TypeError, ValueError):
        value = _DEFAULT_INTERVAL
    base = max(_MIN_INTERVAL, min(_MAX_INTERVAL, value))

    # Apply jitter
    g = _global_config()
    jitter_pct = float(g.get("jitter", _DEFAULT_JITTER))
    if jitter_pct > 0:
        jitter_range = base * min(jitter_pct, 0.5)  # cap at 50%
        base += random.uniform(-jitter_range, jitter_range)
        base = max(_MIN_INTERVAL, min(_MAX_INTERVAL, base))

    return base


def _in_active_window(sc: dict[str, Any]) -> bool:
    """Check if current local time falls within the configured active window."""
    active_start = str(sc.get("active_start", "") or "").strip()
    active_end = str(sc.get("active_end", "") or "").strip()
    if not active_start and not active_end:
        return True

    # If start/end contain non-digit, non-colon characters, treat as invalid
    if not active_start.replace(":", "").isdigit() or not active_end.replace(":", "").isdigit():
        return True

    start_min = _to_minutes(active_start)
    end_min = _to_minutes(active_end)

    try:
        utc_offset_str = str(sc.get("utc_offset", "+8") or "+8").strip()
        sign = 1 if utc_offset_str.startswith("+") else -1
        offset_hours = int(utc_offset_str.lstrip("+").lstrip("-"))
        tz = timezone(timedelta(hours=sign * offset_hours))
        now = datetime.now(tz)

        now_minutes = now.hour * 60 + now.minute

        if start_min <= end_min:
            return start_min <= now_minutes <= end_min
        else:
            return now_minutes >= start_min or now_minutes <= end_min
    except (ValueError, TypeError):
        logger.warning("agent-heartbeat: invalid active window config")
        return True


def _check_idle_pause(sc: dict[str, Any], key: str) -> bool:
    """Check if the session should be paused due to user inactivity."""
    if not bool(sc.get("idle_auto_pause_enabled", False)):
        return False
    try:
        idle_minutes = float(sc.get("idle_auto_pause_minutes", 120))
        last_ts = _last_user_message.get(key)
        if last_ts is None:
            return False
        elapsed = datetime.now().timestamp() - last_ts
        if elapsed > idle_minutes * 60.0:
            logger.info("agent-heartbeat: idle skip %s", key)
            return True
    except (TypeError, ValueError):
        pass
    return False


def _check_paused(sc: dict[str, Any]) -> float | None:
    """Check if session is paused. Returns seconds remaining if paused, else None."""
    paused_until = str(sc.get("paused_until", "") or "").strip()
    if not paused_until:
        return None
    try:
        dt = datetime.fromisoformat(paused_until)
        remaining = (dt - datetime.now()).total_seconds()
        if remaining > 0:
            return remaining
    except (ValueError, TypeError):
        pass
    return None


def _is_user_message(event: Any) -> bool:
    """Check if the event is a user-initiated message (not agent/system)."""
    msg = getattr(event, "message", None)
    if msg is None:
        # If no message object, fall back to checking source
        return True  # conservative: assume user message
    role = getattr(msg, "role", None)
    if role == "user":
        return True
    # Also check if it's not an assistant message
    return role != "assistant"


# ── stats persistence ──────────────────────────────────────────────────────────


def _load_stats() -> dict[str, dict[str, Any]]:
    """Load per-session stats from ``stats.json``."""
    try:
        if _STATS_FILE.exists():
            with _STATS_FILE.open(encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        logger.exception("agent-heartbeat: failed to load stats.json")
    return {}


def _save_stats(data: dict[str, dict[str, Any]]) -> None:
    """Save per-session stats to ``stats.json``."""
    _STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _STATS_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError:
        logger.exception("agent-heartbeat: failed to save stats.json")


def _track_stat(key: str, field: str, value: Any = None) -> None:
    """Increment a stat counter or set a value for a session."""
    stats = _load_stats()
    if key not in stats:
        stats[key] = {
            "total_wakeups": 0,
            "total_skipped": 0,
            "last_wakeup_ts": None,
            "last_error": None,
            "created_ts": datetime.now().isoformat(),
            "last_skip_reason": None,
        }
    if field == "wakeup":
        stats[key]["total_wakeups"] = stats[key].get("total_wakeups", 0) + 1
        stats[key]["last_wakeup_ts"] = datetime.now().isoformat()
    elif field == "skip":
        stats[key]["total_skipped"] = stats[key].get("total_skipped", 0) + 1
        stats[key]["last_skip_reason"] = str(value) if value else None
    elif field == "error":
        stats[key]["last_error"] = str(value) if value else None
    elif value is not None:
        stats[key][field] = value
    _save_stats(stats)


def _clear_stats(key: str) -> None:
    """Reset stats for a session."""
    stats = _load_stats()
    stats.pop(key, None)
    _save_stats(stats)


# ── heartbeat loop ─────────────────────────────────────────────────────────────


async def _run(gateway: Any, source: Any, key: str) -> None:
    """Heartbeat loop for a single session. Runs until disabled or cancelled."""
    global _gateway_ref
    _gateway_ref = gateway
    _sources[key] = source

    event = _triggers.get(key)
    if event is None:
        event = asyncio.Event()
        _triggers[key] = event

    logger.info("agent-heartbeat: loop started for %s", key)

    try:
        while True:
            # ── check global master switch ──
            g = _global_config()
            if not bool(g.get("enabled", False)):
                logger.info("agent-heartbeat: master disabled, stopping %s", key)
                return

            # ── check session config ──
            sc = _get_session_config(key)
            if not sc.get("enabled", False):
                logger.info("agent-heartbeat: %s session disabled, stopping", key)
                return

            # ── check active window ──
            if not _in_active_window(sc):
                _track_stat(key, "skip", "outside active window")
                await asyncio.sleep(_interval(sc))
                continue

            # ── check idle pause ──
            if _check_idle_pause(sc, key):
                _track_stat(key, "skip", "idle")
                await asyncio.sleep(_interval(sc))
                continue

            # ── check manual pause ──
            pause_remaining = _check_paused(sc)
            if pause_remaining is not None:
                _track_stat(key, "skip", f"paused ({int(pause_remaining)}s remaining)")
                await asyncio.sleep(min(pause_remaining, _interval(sc)))
                continue

            # ── wait for interval OR manual trigger ──
            is_manual = False
            try:
                await asyncio.wait_for(event.wait(), timeout=_interval(sc))
                event.clear()
                is_manual = True
                logger.info("agent-heartbeat: %s manual trigger fired", key)
            except asyncio.TimeoutError:
                pass

            # ── read prompt ──
            prompt = _prompt(sc)
            if not prompt:
                logger.info("agent-heartbeat: %s empty prompt, skip", key)
                await asyncio.sleep(_interval(sc))
                continue

            # ── deliver wake ──
            adapter = _adapter_for_source(gateway, source)
            running = getattr(gateway, "_running_agents", {})
            if adapter is None:
                logger.warning("agent-heartbeat: %s no adapter for platform", key)
                _track_stat(key, "error", "no adapter")
            elif key in running:
                logger.info("agent-heartbeat: %s agent busy, skip", key)
                _track_stat(key, "skip", "agent busy")
            else:
                try:
                    await deliver_wake(adapter, text=prompt, source=source)
                    _track_stat(key, "wakeup")
                    logger.info("agent-heartbeat: delivered to %s%s", key, " [manual]" if is_manual else "")
                except Exception as exc:
                    logger.exception("agent-heartbeat: deliver_wake failed for %s", key)
                    _track_stat(key, "error", str(exc))

    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("agent-heartbeat: loop failed for %s", key)
    finally:
        _tasks.pop(key, None)
        _sources.pop(key, None)
        _triggers.pop(key, None)
        _last_user_message.pop(key, None)
        logger.info("agent-heartbeat: loop ended for %s", key)


# ── hooks ──────────────────────────────────────────────────────────────────────


def _on_pre_gateway_dispatch(event: Any, gateway: Any, **_: Any) -> dict | None:
    """Intercept /hb and /heartbeat commands, then bind heartbeat to sessions."""
    global _last_source

    source = getattr(event, "source", None)
    if source is None:
        return

    # ── Intercept /hb and /heartbeat commands BEFORE built-in dispatch ──
    text = (getattr(event, "text", "") or "").strip()
    if text.startswith("/hb") or text.startswith("/heartbeat"):
        # Extract args after the command name
        _, _, remainder = text.partition(" ")
        remainder = remainder.strip()
        response_text = _cmd_heartbeat(remainder)
        if response_text:
            # Send reply via adapter, then skip built-in dispatch
            try:
                adapter = _adapter_for_source(gateway, source)
                if adapter:
                    adapter.send(str(source.chat_id), response_text)
            except Exception:
                logger.warning("agent-heartbeat: failed to send command response", exc_info=True)
            return {"action": "skip", "reason": "agent-heartbeat handled command"}

    _last_source = source

    g = _global_config()
    if not bool(g.get("enabled", False)):
        return

    key = _session_key(source)

    # Only track user-initiated messages for idle detection
    if _is_user_message(event):
        _last_user_message[key] = datetime.now().timestamp()

    # Check if this session is configured and enabled
    if not _is_session_active(key):
        return

    # Start the loop if not already running (with lock to prevent races)
    task = _tasks.get(key)
    if task is None or task.done():
        # Use a fire-and-forget task to acquire the lock asynchronously
        # since this hook is synchronous
        async def _start_locked():
            async with _start_lock:
                t = _tasks.get(key)
                if t is None or t.done():
                    _tasks[key] = asyncio.create_task(
                        _run(gateway, source, key), name=f"agent-heartbeat:{key}"
                    )
                    logger.info("agent-heartbeat: bound to session %s", key)

        # Schedule the lock-acquisition coroutine
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_start_locked())
        except RuntimeError:
            logger.warning("agent-heartbeat: no running event loop, skip binding %s", key)


def _on_session_end(event: Any, **_: Any) -> None:
    """Gracefully cancel all heartbeat tasks on session end."""
    if not _tasks:
        return
    count = len(_tasks)
    for key, task in list(_tasks.items()):
        task.cancel()
    _tasks.clear()
    _triggers.clear()
    _sources.clear()
    _last_user_message.clear()
    logger.info("agent-heartbeat: cancelled %d tasks on session end", count)


# ── slash commands ─────────────────────────────────────────────────────────────


def _get_current_key() -> str | None:
    """Return the session key for the last incoming message, or None."""
    if _last_source is None:
        return None
    return _session_key(_last_source)


def _cmd_heartbeat(raw_args: str) -> str | None:
    args = raw_args.strip()

    # ── /heartbeat (no subcommand) — manual trigger for CURRENT session ──
    if not args or args.startswith("/hb"):
        current_key = _get_current_key()
        if current_key is None:
            return "❌ No session context. Send a message first."
        task = _tasks.get(current_key)
        if task is None or task.done():
            return "❌ No active heartbeat for this session. Use `/hb set` first."
        event = _triggers.get(current_key)
        if event is not None:
            event.set()
            logger.info("agent-heartbeat: manual trigger for %s", current_key)
            return f"✅ Heartbeat triggered for {_format_source(current_key)}"
        return "❌ No active heartbeat for this session."

    subcmd = args.split()[0].lower()

    # ── /heartbeat status — show current session heartbeat status ──
    if subcmd == "status":
        current_key = _get_current_key()
        if current_key is None:
            return "❌ No session context."
        sessions = _load_sessions()
        sc = sessions.get(current_key)
        if sc is None or not sc.get("enabled", False):
            return "No heartbeat configured for this session. Use `/hb set` to enable one."
        task = _tasks.get(current_key)
        is_active = task is not None and not task.done()
        paused_until = str(sc.get("paused_until", "") or "").strip()
        if paused_until:
            pause_status = f"⏸️ Paused until {paused_until}"
        elif is_active:
            pause_status = "🟢 Active"
        else:
            pause_status = "⚪ Configured (waiting for message)"
        return f"**Heartbeat Status** — {_format_source(current_key)}\n" \
               f"  Status: {pause_status}\n" \
               f"  Interval: {int(sc.get('interval', 900))}s\n" \
               f"  Window: {sc.get('active_start', '') or 'all day'}–{sc.get('active_end', '') or 'all day'}"

    # ── /heartbeat list ────────────────────────────────────────────────────
    if subcmd == "list":
        sessions = _load_sessions()
        if not sessions:
            return "No sessions configured. Use `/hb set` to add one."

        lines = []
        for key, sc in sorted(sessions.items()):
            enabled = sc.get("enabled", False)
            active = key in _tasks and not _tasks[key].done()
            if active:
                status = "🟢 Active"
            elif enabled:
                # Check if paused
                paused_until = str(sc.get("paused_until", "") or "").strip()
                if paused_until:
                    status = "⏸️ Paused"
                else:
                    status = "⚪ Configured"
            else:
                status = "🔴 Disabled"
            interval = sc.get("interval", _DEFAULT_INTERVAL)
            lines.append(f"  {status} {_format_source(key)}")
            lines.append(f"         Interval: {int(interval)}s")
            lines.append(f"         Prompt: {sc.get('prompt_file', '(inline)')}")
            if sc.get("active_start"):
                lines.append(f"         Window: {sc['active_start']}-{sc['active_end']} UTC{sc.get('utc_offset', '+8')}")
        return "**Heartbeat Sessions:**\n" + "\n".join(lines)

    # ── /hb set ─────────────────────────────────────────────────────
    if subcmd == "set":
        key = _get_current_key()
        if key is None:
            return "❌ No session context. Send a message first."
        sessions = _load_sessions()
        defaults = _session_defaults()
        if key in sessions:
            sessions[key]["enabled"] = True
            # Clear any pause
            sessions[key].pop("paused_until", None)
        else:
            sessions[key] = dict(defaults)
            sessions[key]["enabled"] = True
        _save_sessions(sessions)
        logger.info("agent-heartbeat: enabled for %s via /hb set", key)
        return f"✅ Heartbeat enabled for {_format_source(key)}.\n   Interval: {int(sessions[key]['interval'])}s\n   Send a message to activate."

    # ── /heartbeat unset ───────────────────────────────────────────────────
    if subcmd == "unset":
        key = _get_current_key()
        if key is None:
            return "❌ No session context."
        sessions = _load_sessions()
        if key in sessions:
            sessions[key]["enabled"] = False
            sessions[key].pop("paused_until", None)
            _save_sessions(sessions)
            logger.info("agent-heartbeat: disabled for %s via /heartbeat unset", key)
            return f"✅ Heartbeat disabled for {_format_source(key)}."
        return "❌ Heartbeat not configured for this session."

    # ── /heartbeat pause ───────────────────────────────────────────────────
    if subcmd == "pause":
        key = _get_current_key()
        if key is None:
            return "❌ No session context."
        sessions = _load_sessions()
        if key not in sessions:
            return f"❌ Session {_format_source(key)} not configured. Use `/hb set` first."

        # Parse optional duration (default: 1 hour)
        rest = args[len("pause"):].strip()
        duration = _DEFAULT_PAUSE_DURATION
        if rest:
            try:
                # Support: "30m", "2h", "3600" (seconds)
                rest = rest.lower()
                if rest.endswith("m"):
                    duration = int(rest[:-1]) * 60
                elif rest.endswith("h"):
                    duration = int(rest[:-1]) * 3600
                else:
                    duration = int(rest)
                duration = max(60, min(86400, duration))
            except (ValueError, TypeError):
                return "❌ Invalid duration. Use: `30m` (minutes), `2h` (hours), or `3600` (seconds)."

        paused_until = (datetime.now() + timedelta(seconds=duration)).isoformat()
        sessions[key]["paused_until"] = paused_until
        _save_sessions(sessions)
        logger.info("agent-heartbeat: paused for %s (%ds)", key, duration)
        human = f"{duration//60}m" if duration < 3600 else f"{duration//3600}h"
        return f"⏸️ Heartbeat paused for {_format_source(key)} ({human})."

    # ── /heartbeat resume ──────────────────────────────────────────────────
    if subcmd == "resume":
        key = _get_current_key()
        if key is None:
            return "❌ No session context."
        sessions = _load_sessions()
        if key not in sessions:
            return f"❌ Session {_format_source(key)} not configured. Use `/hb set` first."
        if "paused_until" not in sessions[key] or not sessions[key].get("paused_until"):
            return f"ℹ️ Heartbeat for {_format_source(key)} is not paused."
        sessions[key].pop("paused_until", None)
        _save_sessions(sessions)
        logger.info("agent-heartbeat: resumed for %s", key)
        return f"▶️ Heartbeat resumed for {_format_source(key)}."

    # ── /heartbeat stats clear ────────────────────────────────────────────
    if subcmd == "stats" and len(args.split()) > 1 and args.split()[1].lower() == "clear":
        key = _get_current_key()
        if key is None:
            return "❌ No session context."
        _clear_stats(key)
        return f"✅ Stats cleared for {_format_source(key)}."

    # ── /heartbeat stats ───────────────────────────────────────────────────
    if subcmd == "stats":
        key = _get_current_key()
        if key is None:
            return "❌ No session context."
        stats = _load_stats()
        s = stats.get(key, {})
        task = _tasks.get(key)
        is_active = task is not None and not task.done()

        lines = [f"**Heartbeat Stats for {_format_source(key)}:**"]
        lines.append(f"  Status: {'🟢 Active' if is_active else '⚪ Idle'}")
        lines.append(f"  Total wakeups: {s.get('total_wakeups', 0)}")
        lines.append(f"  Total skipped: {s.get('total_skipped', 0)}")
        last_wakeup = s.get("last_wakeup_ts")
        if last_wakeup:
            try:
                dt = datetime.fromisoformat(last_wakeup)
                elapsed = (datetime.now() - dt).total_seconds()
                if elapsed < 60:
                    lines.append(f"  Last wakeup: {int(elapsed)}s ago")
                elif elapsed < 3600:
                    lines.append(f"  Last wakeup: {int(elapsed // 60)}m ago")
                else:
                    lines.append(f"  Last wakeup: {elapsed / 3600:.1f}h ago")
            except (ValueError, TypeError):
                pass
        last_error = s.get("last_error")
        if last_error:
            lines.append(f"  Last error: `{last_error}`")
        last_skip_reason = s.get("last_skip_reason")
        if last_skip_reason:
            lines.append(f"  Last skip: {last_skip_reason}")
        created = s.get("created_ts")
        if created:
            lines.append(f"  Created: {created}")
        return "\n".join(lines)

    # ── /heartbeat stats clear ─────────────────────────────────────────────
    if subcmd == "stats" and len(args.split()) > 1 and args.split()[1].lower() == "clear":
        key = _get_current_key()
        if key is None:
            return "❌ No session context."
        _clear_stats(key)
        return f"✅ Stats cleared for {_format_source(key)}."

    # ── /heartbeat test — dry run ──────────────────────────────────────────
    if subcmd == "test":
        key = _get_current_key()
        if key is None:
            return "❌ No session context. Send a message first."
        sc = _get_session_config(key)
        g = _global_config()

        lines = [f"**Heartbeat Test for {_format_source(key)}:**"]
        lines.append(f"  Global enabled: {bool(g.get('enabled', False))}")
        lines.append(f"  Session enabled: {bool(sc.get('enabled', False))}")
        lines.append(f"  Interval: {int(sc.get('interval', _DEFAULT_INTERVAL))}s")

        # Check jitter
        jitter_pct = float(g.get("jitter", _DEFAULT_JITTER))
        if jitter_pct > 0:
            lines.append(f"  Jitter: ±{jitter_pct * 100:.0f}%")

        # Check active window
        active_start = str(sc.get("active_start", "") or "").strip()
        active_end = str(sc.get("active_end", "") or "").strip()
        if active_start and active_end:
            try:
                utc_offset_str = str(sc.get("utc_offset", "+8") or "+8").strip()
                sign = 1 if utc_offset_str.startswith("+") else -1
                offset_hours = int(utc_offset_str.lstrip("+").lstrip("-"))
                tz = timezone(timedelta(hours=sign * offset_hours))
                now = datetime.now(tz)
                in_window = _in_active_window(sc)
                lines.append(f"  Window: {active_start}-{active_end} UTC{utc_offset_str}")
                lines.append(f"  Current time: {now.strftime('%H:%M')} ({'✅ in window' if in_window else '❌ outside window'})")
            except (ValueError, TypeError):
                pass
        else:
            lines.append("  Window: always active")

        # Check idle
        idle_enabled = bool(sc.get("idle_auto_pause_enabled", False))
        if idle_enabled:
            last_ts = _last_user_message.get(key)
            if last_ts:
                elapsed = (datetime.now().timestamp() - last_ts) / 60
                idle_minutes = float(sc.get("idle_auto_pause_minutes", 120))
                lines.append(f"  Idle: {elapsed:.0f}m since last message (threshold: {int(idle_minutes)}m) {'✅ active' if elapsed < idle_minutes else '❌ paused'}")
            else:
                lines.append("  Idle: no messages yet (active)")
        else:
            lines.append("  Idle pause: disabled")

        # Check pause
        pause_remaining = _check_paused(sc)
        if pause_remaining is not None:
            lines.append(f"  Paused: {int(pause_remaining)}s remaining")
        else:
            lines.append("  Paused: no")

        # Check prompt
        prompt = _prompt(sc)
        if prompt:
            preview = prompt[:80].replace("\n", "\\n")
            lines.append(f"  Prompt: {preview}...")
            lines.append(f"  Prompt length: {len(prompt)} chars")
        else:
            lines.append("  ❌ No prompt configured! Set prompt_file or prompt.")

        # Check adapter
        adapter = _adapter_for_source(_gateway_ref, _last_source) if _gateway_ref and _last_source else None
        lines.append(f"  Adapter: {'✅ available' if adapter else '❌ not found'}")

        # Check if loop is running
        task = _tasks.get(key)
        if task and not task.done():
            lines.append("  Loop status: 🟢 running")
        else:
            lines.append("  Loop status: ⚪ not started (will start on next message)")

        return "\n".join(lines)

    # ── /hb config [key] [value] ────────────────────────────────────
    if subcmd == "config":
        key = _get_current_key()
        if key is None:
            return "❌ No session context."
        sessions = _load_sessions()
        if key not in sessions:
            return f"❌ Session {_format_source(key)} not configured. Use `/hb set` first."

        # Parse key=value or key value
        rest = args[len("config"):].strip()
        if not rest:
            # Show current config
            sc = _get_session_config(key)
            lines = [
                f"**Heartbeat Config for {_format_source(key)}:**",
                f"  enabled: {sc.get('enabled', False)}",
                f"  interval: {int(sc.get('interval', _DEFAULT_INTERVAL))}s",
                f"  prompt_file: {sc.get('prompt_file', '') or '(none)'}",
                f"  prompt_files: {sc.get('prompt_files', []) or '(none)'}",
                f"  active_start: {sc.get('active_start', '') or '(none)'}",
                f"  active_end: {sc.get('active_end', '') or '(none)'}",
                f"  utc_offset: {sc.get('utc_offset', '+8')}",
                f"  idle_auto_pause_enabled: {sc.get('idle_auto_pause_enabled', False)}",
                f"  idle_auto_pause_minutes: {sc.get('idle_auto_pause_minutes', 120)}",
                f"  paused_until: {sc.get('paused_until', '') or '(none)'}",
            ]
            return "\n".join(lines)

        # Parse key value
        parts = rest.split(None, 1)
        if len(parts) < 2:
            return "❌ Usage: `/hb config <key> <value>`"
        cfg_key, cfg_val = parts[0], parts[1]
        sc = sessions[key]

        # Validate and coerce
        valid_keys = {
            "enabled", "interval", "prompt_file", "prompt_files",
            "active_start", "active_end", "utc_offset",
            "idle_auto_pause_enabled", "idle_auto_pause_minutes",
        }
        if cfg_key not in valid_keys:
            return f"❌ Unknown config key: `{cfg_key}`. Valid keys: {', '.join(sorted(valid_keys))}"

        try:
            if cfg_key in ("enabled", "idle_auto_pause_enabled"):
                cfg_val = cfg_val.lower() in ("true", "1", "yes")
            elif cfg_key in ("interval", "idle_auto_pause_minutes"):
                cfg_val = int(cfg_val)
            elif cfg_key == "prompt_files":
                cfg_val = [p.strip() for p in cfg_val.split(",") if p.strip()]
            elif cfg_key in ("active_start", "active_end", "prompt_file", "utc_offset"):
                cfg_val = str(cfg_val)
            else:
                cfg_val = str(cfg_val)

            sc[cfg_key] = cfg_val
            _save_sessions(sessions)
            return f"✅ Set `{cfg_key}` = `{cfg_val}` for {_format_source(key)}."
        except (ValueError, TypeError):
            return f"❌ Invalid value for `{cfg_key}`."

    return f"❌ Unknown subcommand: `{subcmd}`. Try: `list`, `set`, `unset`, `config`, `stats`, `test`, `pause`, `resume`."


# ── entry point ────────────────────────────────────────────────────────────────


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    ctx.register_hook("on_session_end", _on_session_end)
    # Note: commands are intercepted via the pre_gateway_dispatch hook rather
    # than register_command, because Hermes core has a built-in /heartbeat
    # command that rejects plugin registration of the same name. The hook
    # catches both /hb and /heartbeat prefixes before the built-in dispatch.