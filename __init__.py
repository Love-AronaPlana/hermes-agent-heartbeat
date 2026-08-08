"""Persistent heartbeat plugin — periodic wakeups in the current gateway session.

Binds to the ``pre_gateway_dispatch`` hook: when a matching message arrives, it
starts an asyncio loop that periodically injects a prompt into the **same**
gateway session via ``deliver_wake``. The agent remembers the conversation,
and the user can reply between wakeups — context is fully preserved.

Supports multiple sessions (different channels/threads), each with its own
config. Manage via ``/heartbeat set|unset|list|config`` slash commands.

License: MIT
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hermes_cli.config import load_config
from gateway.platforms.base import Platform
from gateway.wake import deliver_wake

logger = logging.getLogger(__name__)

_MIN_INTERVAL = 60.0
_MAX_INTERVAL = 86400.0
_DEFAULT_INTERVAL = 900.0

_SESSIONS_FILE = Path("~/.hermes/heartbeat/sessions.json").expanduser()

# Module state
_tasks: dict[str, asyncio.Task] = {}
_triggers: dict[str, asyncio.Event] = {}  # key -> manual trigger event
_last_interaction: dict[str, float] = {}  # key -> last user message timestamp
_gateway_ref: Any = None  # last seen gateway (for slash command)
_sources: dict[str, Any] = {}  # key -> SessionSource for manual trigger
_last_source: Any = None  # source of the last incoming message


# ── config helpers ────────────────────────────────────────────────────────


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
        "active_start": "",
        "active_end": "",
        "utc_offset": "+8",
        "idle_auto_pause_enabled": False,
        "idle_auto_pause_minutes": 120,
    }


def _get_session_config(key: str) -> dict[str, Any]:
    """Get merged config for a session (sessions.json overrides defaults)."""
    sessions = _load_sessions()
    s = sessions.get(key, {})
    defaults = _session_defaults()
    merged = dict(defaults)
    merged.update(s)
    return merged


# ── helpers ──────────────────────────────────────────────────────────────


def _session_key(source: Any) -> str:
    """Build a unique session key from a message source."""
    platform = getattr(source, "platform", None)
    if platform is Platform.TELEGRAM:
        return f"telegram:{source.chat_id}:{source.thread_id or ''}"
    if platform is not None:
        return f"{platform.value}:{source.chat_id}:{source.thread_id or ''}"
    return "unknown:unknown"


def _is_session_active(session_key: str) -> bool:
    """Check if a session is configured and enabled."""
    g = _global_config()
    if not bool(g.get("enabled", False)):
        return False
    sessions = _load_sessions()
    s = sessions.get(session_key, {})
    return bool(s.get("enabled", False))


def _prompt(sc: dict[str, Any]) -> str:
    """Read prompt from file or inline string."""
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
    return str(sc.get("prompt", "") or "").strip()


def _interval(sc: dict[str, Any]) -> float:
    try:
        value = float(sc.get("interval", _DEFAULT_INTERVAL))
    except (TypeError, ValueError):
        value = _DEFAULT_INTERVAL
    return max(_MIN_INTERVAL, min(_MAX_INTERVAL, value))


def _in_active_window(sc: dict[str, Any]) -> bool:
    """Check if current local time falls within the configured active window."""
    active_start = str(sc.get("active_start", "") or "").strip()
    active_end = str(sc.get("active_end", "") or "").strip()
    if not active_start and not active_end:
        return True

    try:
        utc_offset_str = str(sc.get("utc_offset", "+8") or "+8").strip()
        sign = 1 if utc_offset_str.startswith("+") else -1
        offset_hours = int(utc_offset_str.lstrip("+").lstrip("-"))
        tz = timezone(timedelta(hours=sign * offset_hours))
        now = datetime.now(tz)

        def to_minutes(t: str) -> int:
            parts = t.strip().split(":")
            return int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 else int(parts[0]) * 60

        now_minutes = now.hour * 60 + now.minute
        start_min = to_minutes(active_start)
        end_min = to_minutes(active_end)

        if start_min <= end_min:
            return start_min <= now_minutes <= end_min
        else:
            return now_minutes >= start_min or now_minutes <= end_min
    except (ValueError, TypeError):
        return True


def _check_idle_pause(sc: dict[str, Any], key: str) -> bool:
    if not bool(sc.get("idle_auto_pause_enabled", False)):
        return False
    try:
        idle_minutes = float(sc.get("idle_auto_pause_minutes", 120))
        last_ts = _last_interaction.get(key)
        if last_ts is None:
            return False
        elapsed = datetime.now().timestamp() - last_ts
        if elapsed > idle_minutes * 60.0:
            logger.info("agent-heartbeat: idle skip %s", key)
            return True
    except (TypeError, ValueError):
        pass
    return False


# ── heartbeat loop ───────────────────────────────────────────────────────


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
            g = _global_config()
            if not bool(g.get("enabled", False)):
                logger.info("agent-heartbeat: master disabled, stopping %s", key)
                return

            sc = _get_session_config(key)
            if not sc.get("enabled", False):
                logger.info("agent-heartbeat: %s session disabled, stopping", key)
                return

            if not _in_active_window(sc):
                await asyncio.sleep(_interval(sc))
                continue

            if _check_idle_pause(sc, key):
                await asyncio.sleep(_interval(sc))
                continue

            is_manual = False
            try:
                await asyncio.wait_for(event.wait(), timeout=_interval(sc))
                event.clear()
                is_manual = True
                logger.info("agent-heartbeat: %s manual trigger fired", key)
            except asyncio.TimeoutError:
                pass

            prompt = _prompt(sc)
            if not prompt:
                await asyncio.sleep(_interval(sc))
                continue

            adapter = gateway.adapters.get(Platform.TELEGRAM)
            running = getattr(gateway, "_running_agents", {})
            if adapter is None:
                logger.warning("agent-heartbeat: %s no telegram adapter", key)
            elif key in running:
                logger.info("agent-heartbeat: %s agent busy, skip", key)
            else:
                await deliver_wake(adapter, text=prompt, source=source)
                logger.info("agent-heartbeat: delivered to %s%s", key, " [manual]" if is_manual else "")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("agent-heartbeat: loop failed for %s", key)
    finally:
        _tasks.pop(key, None)
        _sources.pop(key, None)
        _triggers.pop(key, None)
        _last_interaction.pop(key, None)
        logger.info("agent-heartbeat: loop ended for %s", key)


# ── hooks ────────────────────────────────────────────────────────────────


def _on_pre_gateway_dispatch(event: Any, gateway: Any, **_: Any) -> None:
    """Bind heartbeat to matching sessions. Also tracks idle and last source."""
    global _last_source

    source = getattr(event, "source", None)
    if source is None:
        return

    _last_source = source

    g = _global_config()
    if not bool(g.get("enabled", False)):
        return

    key = _session_key(source)
    _last_interaction[key] = datetime.now().timestamp()

    # Check if this session is configured and enabled
    if not _is_session_active(key):
        return

    task = _tasks.get(key)
    if task is None or task.done():
        _tasks[key] = asyncio.create_task(
            _run(gateway, source, key), name=f"agent-heartbeat:{key}"
        )
        logger.info("agent-heartbeat: bound to session %s", key)


# ── slash commands ───────────────────────────────────────────────────────


def _get_current_key() -> str | None:
    """Return the session key for the last incoming message, or None."""
    if _last_source is None:
        return None
    return _session_key(_last_source)


def _format_source(key: str) -> str:
    """Pretty-format a session key for display."""
    parts = key.split(":", 2)
    platform = parts[0] if len(parts) > 0 else "?"
    chat_id = parts[1] if len(parts) > 1 else "?"
    thread = parts[2] if len(parts) > 2 and parts[2] else "DM"
    return f"{platform}/{chat_id}/{thread}"


def _cmd_heartbeat(raw_args: str) -> str | None:
    args = raw_args.strip()

    # ── /heartbeat (no subcommand) — manual trigger ──
    if not args or args.startswith("/heartbeat"):
        for key, task in list(_tasks.items()):
            if task.done():
                continue
            event = _triggers.get(key)
            if event is not None:
                event.set()
                logger.info("agent-heartbeat: manual trigger for %s", key)
                return f"✅ Heartbeat triggered for {_format_source(key)}"
        return "❌ No active heartbeat session. Use `/heartbeat set` to enable one."

    subcmd = args.split()[0].lower()

    # ── /heartbeat list ────────────────────────────────────────────────
    if subcmd == "list":
        sessions = _load_sessions()
        if not sessions:
            return "No sessions configured. Use `/heartbeat set` to add one."

        lines = []
        for key, sc in sorted(sessions.items()):
            enabled = sc.get("enabled", False)
            active = key in _tasks and not _tasks[key].done()
            status = "🟢 Active" if active else ("⚪ Configured" if enabled else "🔴 Disabled")
            interval = sc.get("interval", _DEFAULT_INTERVAL)
            lines.append(f"  {status} {_format_source(key)}")
            lines.append(f"         Interval: {int(interval)}s")
            lines.append(f"         Prompt: {sc.get('prompt_file', '(inline)')}")
            if sc.get("active_start"):
                lines.append(f"         Window: {sc['active_start']}-{sc['active_end']} UTC{sc.get('utc_offset', '+8')}")
        return "**Heartbeat Sessions:**\n" + "\n".join(lines)

    # ── /heartbeat set ─────────────────────────────────────────────────
    if subcmd == "set":
        key = _get_current_key()
        if key is None:
            return "❌ No session context. Send a message first."
        sessions = _load_sessions()
        defaults = _session_defaults()
        if key in sessions:
            sessions[key]["enabled"] = True
        else:
            sessions[key] = dict(defaults)
            sessions[key]["enabled"] = True
        _save_sessions(sessions)
        logger.info("agent-heartbeat: enabled for %s via /heartbeat set", key)
        return f"✅ Heartbeat enabled for {_format_source(key)}.\n   Interval: {int(sessions[key]['interval'])}s\n   Send a message to activate."

    # ── /heartbeat unset ───────────────────────────────────────────────
    if subcmd == "unset":
        key = _get_current_key()
        if key is None:
            return "❌ No session context."
        sessions = _load_sessions()
        if key in sessions:
            sessions[key]["enabled"] = False
            _save_sessions(sessions)
            logger.info("agent-heartbeat: disabled for %s via /heartbeat unset", key)
            return f"✅ Heartbeat disabled for {_format_source(key)}."
        return "❌ Heartbeat not configured for this session."

    # ── /heartbeat config [key] [value] ────────────────────────────────
    if subcmd == "config":
        key = _get_current_key()
        if key is None:
            return "❌ No session context."
        sessions = _load_sessions()
        if key not in sessions:
            return f"❌ Session {_format_source(key)} not configured. Use `/heartbeat set` first."

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
                f"  active_start: {sc.get('active_start', '') or '(none)'}",
                f"  active_end: {sc.get('active_end', '') or '(none)'}",
                f"  utc_offset: {sc.get('utc_offset', '+8')}",
                f"  idle_auto_pause_enabled: {sc.get('idle_auto_pause_enabled', False)}",
                f"  idle_auto_pause_minutes: {sc.get('idle_auto_pause_minutes', 120)}",
            ]
            return "\n".join(lines)

        # Parse key value
        parts = rest.split(None, 1)
        if len(parts) < 2:
            return "❌ Usage: `/heartbeat config <key> <value>`"
        cfg_key, cfg_val = parts[0], parts[1]
        sc = sessions[key]

        # Validate and coerce
        try:
            if cfg_key in ("enabled", "idle_auto_pause_enabled"):
                cfg_val = cfg_val.lower() in ("true", "1", "yes")
            elif cfg_key in ("interval", "idle_auto_pause_minutes"):
                cfg_val = int(cfg_val)
            elif cfg_key in ("active_start", "active_end", "prompt_file", "utc_offset"):
                cfg_val = str(cfg_val)
            else:
                return f"❌ Unknown config key: `{cfg_key}`. Valid keys: enabled, interval, prompt_file, active_start, active_end, utc_offset, idle_auto_pause_enabled, idle_auto_pause_minutes"
            sc[cfg_key] = cfg_val
            _save_sessions(sessions)
            return f"✅ Set `{cfg_key}` = `{cfg_val}` for {_format_source(key)}."
        except (ValueError, TypeError):
            return f"❌ Invalid value for `{cfg_key}`."

    return f"❌ Unknown subcommand: `{subcmd}`. Try: `list`, `set`, `unset`, `config`."


# ── entry point ──────────────────────────────────────────────────────────


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    ctx.register_command(
        name="heartbeat",
        handler=_cmd_heartbeat,
        description="Manage heartbeat: set, unset, list, config. Use /heartbeat (no args) for manual trigger.",
        args_hint="[set|unset|list|config [key] [value]]",
    )