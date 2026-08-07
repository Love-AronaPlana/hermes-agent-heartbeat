"""Persistent heartbeat plugin — periodic wakeups in the current gateway session.

Binds to the ``pre_gateway_dispatch`` hook: when a matching message arrives, it
starts an asyncio loop that periodically injects a prompt into the **same**
gateway session via ``deliver_wake``. The agent remembers the conversation,
and the user can reply between wakeups — context is fully preserved.

License: MIT
"""

from __future__ import annotations

import asyncio
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

# Module state
_tasks: dict[str, asyncio.Task] = {}
_triggers: dict[str, asyncio.Event] = {}  # key -> manual trigger event
_last_interaction: dict[str, float] = {}  # key -> last user message timestamp
_gateway_ref: Any = None  # last seen gateway (for slash command)
_sources: dict[str, Any] = {}  # key -> SessionSource for manual trigger


def _config() -> dict[str, Any]:
    try:
        value = (load_config() or {}).get("agent_heartbeat", {})
    except Exception:
        logger.exception("agent-heartbeat config load failed")
        return {}
    return value if isinstance(value, dict) else {}


# ── helpers ──────────────────────────────────────────────────────────────


def _session_key(source: Any) -> str:
    """Build a unique session key from a message source."""
    platform = getattr(source, "platform", None)
    if platform is Platform.TELEGRAM:
        return f"telegram:{source.chat_id}:{source.thread_id or ''}"
    if platform is not None:
        return f"{platform.value}:{source.chat_id}:{source.thread_id or ''}"
    return "unknown:unknown"


def _target_matches(event: Any, cfg: dict[str, Any]) -> bool:
    source = getattr(event, "source", None)
    if source is None or getattr(source, "platform", None) is not Platform.TELEGRAM:
        return False
    if str(cfg.get("chat_id", "")).strip() != str(getattr(source, "chat_id", "")):
        return False
    configured_thread = str(cfg.get("thread_id", "") or "").strip()
    return not configured_thread or configured_thread == str(
        getattr(source, "thread_id", "") or ""
    )


def _prompt(cfg: dict[str, Any]) -> str:
    prompt_file = str(cfg.get("prompt_file", "") or "").strip()
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
    return str(cfg.get("prompt", "") or "").strip()


def _interval(cfg: dict[str, Any]) -> float:
    try:
        value = float(cfg.get("interval_seconds", _DEFAULT_INTERVAL))
    except (TypeError, ValueError):
        value = _DEFAULT_INTERVAL
    return max(_MIN_INTERVAL, min(_MAX_INTERVAL, value))


def _in_active_window(cfg: dict[str, Any]) -> bool:
    """Check if current local time falls within the configured active window.

    Configure with ``active_start`` / ``active_end`` (24h format, e.g. "08:00").
    When ``start <= end`` it's a same-day window; when ``start > end`` it
    crosses midnight (e.g. 22:00-02:00).  Empty = always active.
    """
    active_start = str(cfg.get("active_start", "") or "").strip()
    active_end = str(cfg.get("active_end", "") or "").strip()
    if not active_start and not active_end:
        return True

    try:
        utc_offset_str = str(cfg.get("utc_offset", "+8") or "+8").strip()
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
        logger.warning("agent-heartbeat: invalid time window config, defaulting to active")
        return True


def _check_idle_pause(cfg: dict[str, Any], key: str) -> bool:
    """Return True if heartbeat should be skipped due to inactivity."""
    if not bool(cfg.get("idle_auto_pause_enabled", False)):
        return False
    try:
        idle_minutes = float(cfg.get("idle_auto_pause_minutes", 120))
        last_ts = _last_interaction.get(key)
        if last_ts is None:
            return False
        elapsed = datetime.now().timestamp() - last_ts
        if elapsed > idle_minutes * 60.0:
            logger.info(
                "agent-heartbeat: idle skip %s (%.0fs idle > %.0fs threshold)",
                key, elapsed, idle_minutes * 60.0,
            )
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
            cfg = _config()
            if not bool(cfg.get("enabled", False)):
                logger.info("agent-heartbeat: %s disabled, stopping loop", key)
                return

            if not _in_active_window(cfg):
                await asyncio.sleep(_interval(cfg))
                continue

            if _check_idle_pause(cfg, key):
                await asyncio.sleep(_interval(cfg))
                continue

            # Wait for interval OR manual trigger, whichever comes first
            is_manual = False
            try:
                await asyncio.wait_for(event.wait(), timeout=_interval(cfg))
                event.clear()
                is_manual = True
                logger.info("agent-heartbeat: %s manual trigger fired", key)
            except asyncio.TimeoutError:
                pass  # Normal interval tick

            prompt = _prompt(cfg)
            if not prompt:
                await asyncio.sleep(_interval(cfg))
                continue

            adapter = gateway.adapters.get(Platform.TELEGRAM)
            running = getattr(gateway, "_running_agents", {})
            if adapter is None:
                logger.warning("agent-heartbeat: %s no telegram adapter available", key)
            elif key in running:
                logger.info("agent-heartbeat: %s agent busy, skip", key)
            else:
                await deliver_wake(adapter, text=prompt, source=source)
                logger.info(
                    "agent-heartbeat: delivered to %s%s",
                    key, " [manual]" if is_manual else "",
                )
    except asyncio.CancelledError:
        logger.info("agent-heartbeat: %s cancelled", key)
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
    """Bind heartbeat to the first matching real message. Also tracks idle."""
    cfg = _config()
    if not bool(cfg.get("enabled", False)):
        return

    source = getattr(event, "source", None)
    if source is None:
        return

    key = _session_key(source)
    _last_interaction[key] = datetime.now().timestamp()

    if not _target_matches(event, cfg):
        return

    task = _tasks.get(key)
    if task is None or task.done():
        _tasks[key] = asyncio.create_task(
            _run(gateway, source, key), name=f"agent-heartbeat:{key}"
        )
        logger.info("agent-heartbeat: bound to gateway session %s", key)


# ── slash command ────────────────────────────────────────────────────────


def _cmd_heartbeat(raw_args: str) -> str | None:
    """/heartbeat — trigger an immediate heartbeat wakeup."""
    for key, task in list(_tasks.items()):
        if task.done():
            continue
        event = _triggers.get(key)
        if event is not None:
            event.set()
            logger.info("agent-heartbeat: manual trigger via /heartbeat for %s", key)
            if raw_args.strip():
                return f"✅ Heartbeat triggered with args: {raw_args.strip()}"
            return "✅ Heartbeat triggered"
    return "❌ No active heartbeat session. Send a message first to bind."


# ── entry point ──────────────────────────────────────────────────────────


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    ctx.register_command(
        name="heartbeat",
        handler=_cmd_heartbeat,
        description="Trigger an immediate heartbeat wakeup in the current session.",
        args_hint="<optional prompt override>",
    )