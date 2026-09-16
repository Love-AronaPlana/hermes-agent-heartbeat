"""Persistent heartbeat plugin — periodic wakeups in the current gateway session.

Binds to the ``pre_gateway_dispatch`` hook: when a matching message arrives, it
starts an asyncio loop that periodically injects a prompt into the **same**
gateway session via ``deliver_wake``. The agent remembers the conversation,
and the user can reply between wakeups — context is fully preserved.

Supports multiple sessions (different channels/threads), each with its own
config. Manage via ``/xt set|unset|list|config|stats|test|pause|resume``
slash commands.

License: MIT
"""

from __future__ import annotations

import asyncio
import time
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

# Default prompt text used when a session has no per-session prompt_file,
# prompt_files, or inline prompt configured.  Kept short and conversational
# so a freshly-installed heartbeat is immediately useful without forcing
# the user to author a prompt file.  Users can still override globally
# (config.yaml: ``agent_heartbeat.default_prompt``) or per-session
# (``/xt set prompt=...``).
_DEFAULT_PROMPT_ZH = (
    "[Heartbeat 唤醒] 检查我们之前的对话、当前任务和已有计划，主动找出尚未完成或可以推进的事项并直接开始处理。"
    "优先完成用户已经明确提出但尚未完成的任务；如果没有明确待办，再选择一个有价值且安全的相关事项主动推进。"
    "如果所有事情都已完成，不要只停在那里：可以自己找点有意义的事情做，例如研究、学习、整理、优化、测试、创作，或者找点有趣的东西探索一下。"
    "总之要保持主动，不要只是汇报状态或等待用户输入。只有在确实完成了有意义的工作、发现重要信息或需要用户决策时才发送简短总结；如果本次没有实质进展、没有新信息，也没有需要用户知道的事项，最终回复必须严格只输出 [SILENT]，不要附加任何文字。"
    "注意：这条规则只适用于自动 Heartbeat 唤醒，不适用于用户主动发送的消息或 /xt test、/xt stats 等命令。"
)
_DEFAULT_PROMPT_EN = (
    "[Heartbeat Wakeup] Review our previous conversation, current tasks, and existing plans. "
    "Find unfinished or actionable items and start working on them directly. "
    "Prioritize explicitly requested unfinished work; if there is no clear todo, choose one safe and valuable related task to advance. "
    "If everything is complete, do not just wait: research, learn, organize, optimize, test, create, or explore something useful. "
    "Stay proactive instead of merely reporting status or waiting for input. Only send a brief summary when you made meaningful progress, found important information, or need a user decision. "
    "If there is no substantive progress, new information, or user-facing item this cycle, the final response MUST be exactly [SILENT] with no additional text. "
    "This rule applies only to automatic Heartbeat wakeups, not user messages or commands such as /xt test and /xt stats."
)
# Backward-compatible name for integrations that imported the old constant.
_DEFAULT_PROMPT = _DEFAULT_PROMPT_ZH

_LANGUAGE_ALIASES = {
    "zh": "zh", "zh-cn": "zh", "中文": "zh", "chinese": "zh",
    "en": "en", "en-us": "en", "英文": "en", "english": "en",
}

# Chinese aliases are normalized before dispatch so the existing command
# implementation remains the single source of truth.  In particular, users
# naturally type `/xt 间隔 1800`, which is shorthand for
# `/xt config interval 1800`.
_XT_SUBCOMMAND_ALIASES = {
    "列表": "list", "列出": "list",
    "设置": "set", "启用": "set", "开启": "set",
    "停用": "unset", "关闭": "unset",
    "配置": "config", "设置配置": "config",
    "统计": "stats", "测试": "test",
    "暂停": "pause", "恢复": "resume",
    "语言": "language", "间隔": "interval", "频率": "interval",
}
_XT_CONFIG_KEY_ALIASES = {
    "启用": "enabled", "开启": "enabled",
    "间隔": "interval", "频率": "interval",
    "提示词": "prompt", "提示": "prompt",
    "提示文件": "prompt_file", "提示词文件": "prompt_file",
    "提示文件列表": "prompt_files", "提示词文件列表": "prompt_files",
    "活跃开始": "active_start", "活跃结束": "active_end",
    "语言": "language", "时区": "utc_offset", "utc偏移": "utc_offset",
    "空闲自动暂停": "idle_auto_pause_enabled", "空闲暂停": "idle_auto_pause_enabled",
    "空闲阈值": "idle_auto_pause_minutes",
}


def _extract_xt_command(text: str) -> str | None:
    """Return the args of a standalone ``/xt`` line in a Telegram text batch.

    Telegram's adapter may merge messages received in one short burst with a
    newline.  A command must still win over the preceding ordinary text, but
    ``/xt`` embedded in a normal sentence must not be treated as a command.
    """
    for line in (text or "").splitlines():
        parts = line.strip().split(None, 1)
        if not parts:
            continue
        command = parts[0].split("@", 1)[0].lower()
        if command == "/xt":
            return parts[1].strip() if len(parts) > 1 else ""
    return None


def _normalize_xt_args(raw_args: str) -> str:
    """Normalize Chinese command/key aliases to the canonical English syntax."""
    args = (raw_args or "").strip()
    if not args:
        return ""

    parts = args.split(None, 1)
    subcmd = _XT_SUBCOMMAND_ALIASES.get(parts[0].lower(), parts[0].lower())
    rest = parts[1].strip() if len(parts) > 1 else ""

    # `/xt 间隔 1800` and `/xt 频率 1800` are convenient config shortcuts.
    if subcmd == "interval":
        return f"config interval {rest}".strip()

    if subcmd == "config" and rest:
        key_parts = rest.split(None, 1)
        key = _XT_CONFIG_KEY_ALIASES.get(key_parts[0].lower(), key_parts[0])
        rest = key + (f" {key_parts[1]}" if len(key_parts) > 1 else "")

    return subcmd + (f" {rest}" if rest else "")

_TEXT = {
    "no_context": ("❌ 没有会话上下文，请先发送一条消息。", "❌ No session context. Send a message first."),
    "triggered": ("✅ 已触发当前会话的 Heartbeat：{source}", "✅ Heartbeat triggered for {source}"),
    "no_active": ("❌ 当前会话没有运行中的 Heartbeat，请先使用 `/xt set`。", "❌ No active heartbeat for this session. Use `/xt set` first."),
    "no_active_short": ("❌ 当前会话没有运行中的 Heartbeat。", "❌ No active heartbeat for this session."),
    "list_empty": ("当前没有已配置的会话，请使用 `/xt set` 添加。", "No sessions configured. Use `/xt set` to add one."),
    "not_configured": ("当前会话尚未配置 Heartbeat，请使用 `/xt set` 启用。", "No heartbeat configured for this session. Use `/xt set` to enable one."),
    "global_disabled": ("❌ Heartbeat 已被全局禁用，请在 config.yaml 中设置 `agent_heartbeat.enabled: true`。", "❌ Heartbeat is globally disabled. Set `agent_heartbeat.enabled: true` in config.yaml."),
    "set": ("✅ 已为 {source} 启用 Heartbeat。\n   间隔：{interval} 秒\n   发送一条消息后开始运行。", "✅ Heartbeat enabled for {source}.\n   Interval: {interval}s\n   Send a message to activate."),
    "unset": ("✅ 已为 {source} 停用 Heartbeat。", "✅ Heartbeat disabled for {source}."),
    "not_configured_source": ("❌ 会话 {source} 尚未配置，请先使用 `/xt set`。", "❌ Session {source} not configured. Use `/xt set` first."),
    "invalid_duration": ("❌ 时长无效，请使用 `30m`、`2h` 或秒数。", "❌ Invalid duration. Use `30m`, `2h`, or seconds."),
    "paused": ("⏸️ 已暂停 {source} 的 Heartbeat（{duration}）。", "⏸️ Heartbeat paused for {source} ({duration})."),
    "not_paused": ("ℹ️ {source} 的 Heartbeat 当前没有暂停。", "ℹ️ Heartbeat for {source} is not paused."),
    "resumed": ("▶️ 已恢复 {source} 的 Heartbeat。", "▶️ Heartbeat resumed for {source}."),
    "stats_cleared": ("✅ 已清除 {source} 的统计数据。", "✅ Stats cleared for {source}."),
    "usage_config": ("❌ 用法：`/xt config <键> <值>`", "❌ Usage: `/xt config <key> <value>`"),
    "unknown_config": ("❌ 未知配置项 `{config_key}`，可用项：{valid}", "❌ Unknown config key: `{config_key}`. Valid keys: {valid}"),
    "config_set": ("✅ 已为 {source} 设置 `{config_key}` = `{value}`。", "✅ Set `{config_key}` = `{value}` for {source}."),
    "invalid_config": ("❌ `{config_key}` 的值无效。", "❌ Invalid value for `{config_key}`."),
    "unknown_subcommand": ("❌ 未知子命令 `{subcmd}`。可用：`list`、`set`、`unset`、`config`、`stats`、`test`、`pause`、`resume`。", "❌ Unknown subcommand: `{subcmd}`. Try: `list`, `set`, `unset`, `config`, `stats`, `test`, `pause`, `resume`."),
}


def _normalize_language(value: Any) -> str:
    return _LANGUAGE_ALIASES.get(str(value or "").strip().lower(), "zh")


def _language_for_session(key: str | None = None, config: dict[str, Any] | None = None) -> str:
    if config is not None and "language" in config:
        return _normalize_language(config.get("language"))
    if key:
        sessions = _load_sessions()
        if isinstance(sessions.get(key), dict) and "language" in sessions[key]:
            return _normalize_language(sessions[key].get("language"))
    return _normalize_language(_global_config().get("default_language", "zh"))


def _t(language: str, key: str, **kwargs: Any) -> str:
    value = _TEXT[key][0 if _normalize_language(language) == "zh" else 1]
    return value.format(**kwargs)


def _default_prompt(language: str) -> str:
    lang = _normalize_language(language)
    g = _global_config()
    if lang == "en":
        return str(g.get("default_prompt_en", "") or _DEFAULT_PROMPT_EN)
    return str(g.get("default_prompt", "") or _DEFAULT_PROMPT_ZH)

_SESSIONS_FILE = Path("~/.hermes/heartbeat/sessions.json").expanduser()
_STATS_FILE = Path("~/.hermes/heartbeat/stats.json").expanduser()

# ── sessions.json schema versioning ────────────────────────────────────────────
#
# _SESSIONS_FORMAT_VERSION is the CURRENT schema version.  When the stored
# file carries an older _version, _load_sessions() migrates it forward one
# step at a time through _SCHEMA_MIGRATIONS instead of wiping the user's
# configuration.  Every release that changes the schema MUST:
#   1. bump _SESSIONS_FORMAT_VERSION,
#   2. append a (old_version, new_version, migrate_fn) tuple to
#      _SCHEMA_MIGRATIONS (in chronological order),
#   3. make the migrate_fn idempotent and pure (no I/O).
#
# Migration steps (old -> new, applied in listed order):
#   pre-0.3.3/legacy -> 0.3.3 : normalize the file shape (no-op for
#       0.3.3-era files; anything without _version is treated as legacy).
#   0.3.3 -> 0.4.0             : v0.4.0 renamed no config keys — the
#       session-key shape stayed "<platform>:<chat_id>:<thread>", so the
#       step only normalizes entries to the 0.4.0 defaults.
#   0.4.0 -> 0.4.1             : v0.4.1 flipped default `enabled` to True
#       and added the inline fallback prompt.  Entries that didn't set
#       `enabled` explicitly get it filled; entries that were explicitly
#       disabled stay disabled.
#
_OLDEST_SCHEMA_VERSION = "0.0.0"

_SCHEMA_MIGRATIONS: list[tuple[str, str, Any]] = [
    # (old, new, migrate_fn)
    ("0.0.0", "0.3.3", lambda d: _migrate_legacy(d)),
    ("0.3.3", "0.4.0", lambda d: _migrate_033_to_040(d)),
    ("0.4.0", "0.4.1", lambda d: _migrate_040_to_041(d)),
    ("0.4.1", "0.4.2", lambda d: _migrate_041_to_042(d)),
    ("0.4.2", "0.4.4", lambda d: dict(d)),
]


def _ver_at_least(version: str, target: str) -> bool:
    """Compare dotted version strings: is ``version`` >= ``target``?"""
    try:
        v = tuple(int(p) for p in str(version).split("."))
        t = tuple(int(p) for p in str(target).split("."))
    except (ValueError, TypeError):
        return False
    return v >= t


def _migrate_legacy(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize a pre-0.3.3 / versionless file into the 0.3.3 shape.

    Older files may have omitted ``_version`` entirely or stored entries
    whose values were plain dicts.  We keep every entry (so nothing is
    lost), but strip any ``_version`` key that might have slipped in and
    ensure per-session values are dicts.
    """
    result: dict[str, Any] = {}
    for key, value in data.items():
        if key == "_version":
            continue
        if isinstance(value, dict):
            result[key] = dict(value)
        else:
            # e.g. "telegram:123:" -> true (legacy boolean form)
            result[key] = {"enabled": bool(value)}
    return result


def _migrate_033_to_040(data: dict[str, Any]) -> dict[str, Any]:
    """0.3.3 -> 0.4.0.

    0.4.0 changed defaults but kept the same per-session key format and
    config keys.  Fill in any keys missing from each entry with the
    0.4.0-era defaults, preserving what the user explicitly set.
    """
    result: dict[str, Any] = {}
    for key, entry in data.items():
        if not isinstance(entry, dict):
            entry = {"enabled": bool(entry)}
        merged = dict(entry)
        merged.setdefault("enabled", True)
        merged.setdefault("interval", _DEFAULT_INTERVAL)
        merged.setdefault("prompt_file", "")
        merged.setdefault("prompt_files", [])
        merged.setdefault("active_start", "")
        merged.setdefault("active_end", "")
        merged.setdefault("utc_offset", "+8")
        result[key] = merged
    return result


def _migrate_040_to_041(data: dict[str, Any]) -> dict[str, Any]:
    """0.4.0 -> 0.4.1.

    0.4.1 added an inline fallback ``prompt`` and kept `enabled` default
    True.  Explicitly-disabled entries stay disabled; everything else is
    left untouched (the new prompt default is picked up by
    ``_session_defaults`` at runtime, so we don't need to write it here).
    """
    result: dict[str, Any] = {}
    for key, entry in data.items():
        if not isinstance(entry, dict):
            entry = {"enabled": bool(entry)}
        merged = dict(entry)
        merged.setdefault("enabled", True)
        result[key] = merged
    return result


def _migrate_041_to_042(data: dict[str, Any]) -> dict[str, Any]:
    """0.4.1 -> 0.4.2.

    0.4.2 introduces the version-migration framework itself; no config
    keys changed.  This step exists only so the migration chain is
    explicit and future upgrades have a well-defined baseline.  Entries
    are normalized (dict-ified) but otherwise preserved verbatim.
    """
    result: dict[str, Any] = {}
    for key, entry in data.items():
        if not isinstance(entry, dict):
            entry = {"enabled": bool(entry)}
        result[key] = dict(entry)
    return result

# Bump this when the session key schema or config layout changes.
# On plugin upgrade, _load_sessions detects the mismatch and clears
# stale per-session config so users get a fresh start instead of
# silently inheriting an old session's heartbeat.
#
# v0.4.1: defaults flipped to enabled=True with an inline fallback
# prompt so the loop actually starts on a fresh install (v0.4.0 had
# the same shape but sessions.json was empty post-version-mismatch,
# so _is_session_active returned False and the loop never started).
#
# v0.4.2: introduced the schema-migration framework.  Older files are
# now upgraded forward through _SCHEMA_MIGRATIONS instead of wiped, so
# a stale sessions.json keeps the user's enabled/interval/prompt config.
# v0.5.0: adds per-session language selection (zh default, en optional).
_SESSIONS_FORMAT_VERSION = "0.5.1"

# ── module state ───────────────────────────────────────────────────────────────

_tasks: dict[str, asyncio.Task] = {}
_triggers: dict[str, asyncio.Event] = {}  # key -> manual trigger event
_last_user_message: dict[str, float] = {}  # key -> last USER message timestamp
_next_trigger_at: dict[str, float | None] = {}  # key -> next scheduled automatic wake
_gateway_ref: Any = None  # last seen gateway (for slash command)
_sources: dict[str, Any] = {}  # key -> SessionSource for manual trigger
_last_source: Any = None  # source of the last incoming user message
_start_lock: asyncio.Lock = asyncio.Lock()  # prevent race on loop creation
_after_wake: dict[str, bool] = {}  # key -> True if heartbeat just fired, wait for next user msg

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
    """Load per-session config from ``sessions.json``.

    If the stored ``_version`` field is older than the running plugin
    version, the config is **migrated forward** through each schema
    version's migration step rather than being cleared.  Unknown/legacy
    versions (or a file missing ``_version`` entirely) are treated as
    pre-0.3.3 and migrated from there.  Only a genuinely corrupt file
    (unparseable JSON) falls back to ``{}``.
    """
    try:
        if _SESSIONS_FILE.exists():
            with _SESSIONS_FILE.open(encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            stored_version = data.pop("_version", None) or _OLDEST_SCHEMA_VERSION
            if stored_version != _SESSIONS_FORMAT_VERSION:
                # Walk each schema upgrade in order.
                for old, new, migrate in _SCHEMA_MIGRATIONS:
                    if _ver_at_least(stored_version, new):
                        continue
                    data = migrate(data)
                    stored_version = new
                    logger.info(
                        "agent-heartbeat: sessions.json migrated %s -> %s",
                        old, new,
                    )
                # Persist the upgraded config so the next load is a no-op.
                _save_sessions(data)
            return data
    except (json.JSONDecodeError, OSError):
        logger.exception("agent-heartbeat: failed to load sessions.json")
    return {}


def _save_sessions(data: dict[str, dict[str, Any]]) -> None:
    """Save per-session config to ``sessions.json``."""
    _SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = dict(data)
        payload["_version"] = _SESSIONS_FORMAT_VERSION
        with _SESSIONS_FILE.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
    except OSError:
        logger.exception("agent-heartbeat: failed to save sessions.json")


def _session_defaults() -> dict[str, Any]:
    """Return default values for a session config.

    The inline ``prompt`` default is the friendly nudge that ships the first
    time someone enables heartbeat on a fresh install. The user can override
    it per-session (via ``/xt set prompt=...``) or per-key globally
    (``agent_heartbeat.default_prompt`` in config.yaml). We deliberately do
    NOT require a ``prompt_file`` — pointing at a missing file was the
    silent-failure mode that left wakeups at ``empty prompt, skip``
    forever.
    """
    g = _global_config()
    return {
        "enabled": True,
        "interval": float(g.get("default_interval", _DEFAULT_INTERVAL)),
        "prompt_file": str(g.get("default_prompt_file", "") or ""),
        "prompt_files": list(g.get("default_prompt_files", []) or []),
        "language": _normalize_language(g.get("default_language", "zh")),
        "prompt": "",
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
    """Return whether the user explicitly enabled Heartbeat for this session.

    A missing entry is intentionally inactive.  Otherwise every ordinary
    message would silently start a Heartbeat loop after a restart or after the
    session file was cleared, while ``/xt config`` and ``/xt list`` report that
    no session is configured.
    """
    g = _global_config()
    if not bool(g.get("enabled", True)):
        return False
    session = _load_sessions().get(session_key)
    return bool(session and session.get("enabled", False))


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

    # Fall back to the built-in prompt. Keep the language-specific default
    # dynamic even for sessions created before language support was added.
    inline = str(sc.get("prompt", "") or "").strip()
    if not inline or inline == _DEFAULT_PROMPT_ZH:
        return _default_prompt(_normalize_language(sc.get("language", "zh")))
    return inline


def _parse_interval_value(value: Any) -> int | float:
    """Parse an interval as seconds, accepting optional s/m/h suffixes."""
    text = str(value).strip().lower()
    if not text:
        raise ValueError("empty interval")
    multiplier = 1
    if text[-1:] in ("s", "m", "h"):
        multiplier = {"s": 1, "m": 60, "h": 3600}[text[-1]]
        text = text[:-1].strip()
    parsed = float(text) * multiplier
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError("invalid interval")
    return int(parsed) if parsed.is_integer() else parsed


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


def _set_next_trigger(key: str, timestamp: float | None) -> None:
    """Publish the next automatic wake timestamp for status/test commands."""
    _next_trigger_at[key] = timestamp


def _clear_next_trigger(key: str) -> None:
    _next_trigger_at.pop(key, None)


def _format_next_trigger(
    timestamp: float | None, language: str, utc_offset: str = "+8"
) -> str:
    """Format a next-wakeup timestamp using the session's configured timezone."""
    lang = _normalize_language(language)
    if not timestamp:
        return "未安排" if lang == "zh" else "not scheduled"
    offset_text = str(utc_offset or "+8").strip()
    try:
        sign = -1 if offset_text.startswith("-") else 1
        offset_hours = int(offset_text.lstrip("+").lstrip("-"))
        tz = timezone(timedelta(hours=sign * offset_hours))
    except (TypeError, ValueError):
        offset_text = "+8"
        tz = timezone(timedelta(hours=8))
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(tz)
    remaining = int(timestamp - time.time())
    if remaining <= 0:
        return "即将触发" if lang == "zh" else "due now"
    if remaining < 60:
        eta = f"{remaining}秒后" if lang == "zh" else f"in {remaining}s"
    elif remaining < 3600:
        eta = f"{remaining // 60}分钟后" if lang == "zh" else f"in {remaining // 60}m"
    else:
        eta = f"{remaining / 3600:.1f}小时后" if lang == "zh" else f"in {remaining / 3600:.1f}h"
    clock = dt.strftime("%Y-%m-%d %H:%M:%S")
    return f"{clock} UTC{offset_text}（{eta}）" if lang == "zh" else f"{clock} UTC{offset_text} ({eta})"


def _fallback_next_trigger(key: str, sc: dict[str, Any]) -> float | None:
    """Estimate the next trigger when the loop has not published one yet."""
    # After a wakeup the loop deliberately waits for a new user message;
    # do not let a stale in-memory schedule override that state and render
    # "due now" forever in /xt stats. Check this before the cached schedule.
    if _after_wake.get(key, False):
        return None
    if not bool(sc.get("enabled", False)):
        return None
    if _check_paused(sc) is not None:
        return None
    scheduled = _next_trigger_at.get(key)
    if scheduled is not None:
        return scheduled
    last_message = _last_user_message.get(key)
    if last_message is None:
        # Preserve the estimate across a gateway restart when available.
        stats = _load_stats().get(key, {})
        last_message = stats.get("last_user_message_ts")
    if last_message is None:
        return None
    try:
        interval = float(sc.get("interval", _DEFAULT_INTERVAL))
    except (TypeError, ValueError):
        interval = _DEFAULT_INTERVAL
    interval = max(_MIN_INTERVAL, min(_MAX_INTERVAL, interval))
    return float(last_message) + interval


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
            "last_user_message_ts": None,
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
    elif field == "last_user_message_ts":
        stats[key]["last_user_message_ts"] = float(value) if value is not None else None
    elif value is not None:
        stats[key][field] = value
    _save_stats(stats)


def _clear_stats(key: str) -> None:
    """Reset stats for a session."""
    stats = _load_stats()
    stats.pop(key, None)
    _save_stats(stats)


# ── loop lifecycle helpers ──────────────────────────────────────────────────────


def _cancel_loop_for_key(key: str) -> None:
    """Cancel and clean up the heartbeat loop for a specific session key.

    Safe to call when no loop exists.  Removes the task from ``_tasks`` so
    that ``_start_loop`` can immediately create a fresh one.  The cancelled
    task's ``finally`` block is guarded by an identity check
    (``_tasks.get(key) is current_task``) and will skip cleanup, preventing
    it from wiping the new loop's state.
    """
    task = _tasks.pop(key, None)
    if task is not None and not task.done():
        task.cancel()
    _triggers.pop(key, None)
    _sources.pop(key, None)
    _last_user_message.pop(key, None)
    _after_wake.pop(key, None)
    _clear_next_trigger(key)


def _start_loop(gateway: Any, source: Any, key: str) -> None:
    """Start a heartbeat loop for *key* if one is not already running.

    Always updates ``_sources[key]`` so the loop sees the latest routing
    metadata, then schedules task creation under ``_start_lock``.
    """
    _sources[key] = source  # ensure source is fresh
    task = _tasks.get(key)
    if task is not None and not task.done():
        return  # already running
    try:
        loop = asyncio.get_running_loop()

        async def _start_locked() -> None:
            async with _start_lock:
                t = _tasks.get(key)
                if t is not None and not t.done():
                    return
                _tasks[key] = asyncio.create_task(
                    _run(gateway, source, key), name=f"agent-heartbeat:{key}"
                )
                logger.info("agent-heartbeat: bound to session %s", key)

        loop.create_task(_start_locked())
    except RuntimeError:
        logger.warning("agent-heartbeat: no running event loop, skip binding %s", key)


# ── heartbeat loop ─────────────────────────────────────────────────────────────


async def _run(gateway: Any, source: Any, key: str) -> None:
    """Heartbeat loop for a single session. Runs until disabled or cancelled.

    ``source`` is the *initial* routing metadata captured at loop-start time.
    The loop reads the **latest** source from ``_sources[key]`` on every tick
    so that it always routes wakeups to the current conversation, even after
    ``/new`` rotated the underlying session.
    """
    global _gateway_ref
    _gateway_ref = gateway
    _sources[key] = source

    event = _triggers.get(key)
    if event is None:
        event = asyncio.Event()
        _triggers[key] = event

    this_task = asyncio.current_task()
    logger.info("agent-heartbeat: loop started for %s", key)

    try:
        while True:
            # ── check global master switch ──
            g = _global_config()
            if not bool(g.get("enabled", True)):
                logger.info("agent-heartbeat: master disabled, stopping %s", key)
                return

            # ── check session config ──
            sc = _get_session_config(key)
            if not sc.get("enabled", False):
                logger.info("agent-heartbeat: %s session disabled, stopping", key)
                return

            # ── check active window ──
            if not _in_active_window(sc):
                _clear_next_trigger(key)
                _track_stat(key, "skip", "outside active window")
                await asyncio.sleep(_interval(sc))
                continue

            # ── check idle pause ──
            if _check_idle_pause(sc, key):
                _clear_next_trigger(key)
                _track_stat(key, "skip", "idle")
                await asyncio.sleep(_interval(sc))
                continue

            # ── check manual pause ──
            pause_remaining = _check_paused(sc)
            if pause_remaining is not None:
                _clear_next_trigger(key)
                _track_stat(key, "skip", f"paused ({int(pause_remaining)}s remaining)")
                await asyncio.sleep(min(pause_remaining, _interval(sc)))
                continue

            # ── wait for heartbeat ──
            # 1) If heartbeat just fired, wait for next user message.
            # 2) Wait for agent to finish processing (user msg or wake).
            # 3) Count down from _last_user_message.
            is_manual = False
            interval = _interval(sc)
            current_source = _sources.get(key, source)  # refresh before countdown
            # Publish a provisional schedule immediately; it is refreshed below
            # whenever the countdown is reset or a wake is delivered.
            last_msg = _last_user_message.get(key, 0.0)
            _set_next_trigger(key, (last_msg + interval) if last_msg > 0.0 else (time.time() + interval))
            while True:
                # After a heartbeat wake, do NOT restart the countdown —
                # wait for the next user message to reset the timer.
                if _after_wake.get(key, False):
                    try:
                        await asyncio.wait_for(event.wait(), timeout=5.0)
                        event.clear()
                        is_manual = True
                        break  # manual trigger fires immediately
                    except asyncio.TimeoutError:
                        continue  # re-check _after_wake

                # Wait for agent to finish processing (user msg or wake).
                # Uses the gateway's session key format, not the plugin's
                # shorter key, to correctly match _running_agents entries.
                current_source = _sources.get(key, source)  # refresh during wait
                gw_key = ""
                try:
                    gw_key = gateway._session_key_for_source(current_source)
                except Exception:
                    pass
                running = getattr(gateway, "_running_agents", {})
                if gw_key and gw_key in running:
                    try:
                        await asyncio.wait_for(event.wait(), timeout=2.0)
                        event.clear()
                        is_manual = True
                        break  # manual trigger fires immediately
                    except asyncio.TimeoutError:
                        continue  # re-check if agent is still busy

                # Agent is idle — countdown from last user message.
                last_msg = _last_user_message.get(key, 0.0)
                now = time.time()
                if last_msg > 0.0:
                    _set_next_trigger(key, last_msg + interval)
                    remaining = max(0.0, interval - (now - last_msg))
                else:
                    _set_next_trigger(key, now + interval)
                    remaining = interval  # no user message yet, wait full interval

                if remaining <= 0:
                    break  # enough time has elapsed since user's last message

                # Sleep in short chunks so manual triggers (/xt) are responsive.
                try:
                    await asyncio.wait_for(
                        event.wait(), timeout=min(remaining, 5.0),
                    )
                    event.clear()
                    is_manual = True
                    break
                except asyncio.TimeoutError:
                    pass  # check remaining again (may have been reset by hook)

            # ── read the LATEST source (may have been updated by hook) ──
            current_source = _sources.get(key, source)

            # ── read prompt ──
            prompt = _prompt(sc)
            if not prompt:
                logger.info("agent-heartbeat: %s empty prompt, skip", key)
                await asyncio.sleep(_interval(sc))
                continue

            # ── resolve the CURRENT gateway session ──
            # The platform source is stable across /new, while the gateway session
            # id is not.  Passing only ``source`` lets deliver_wake consult a stale
            # routing entry on some gateway versions and resurrect the pre-/new
            # context.  Resolve the canonical id immediately before every wake.
            session_id = ""
            try:
                gateway_key = gateway._session_key_for_source(current_source)
                store = getattr(gateway, "session_store", None)
                if store is not None:
                    session_id = str(store.peek_session_id(gateway_key) or "")
            except Exception:
                logger.debug("agent-heartbeat: current session lookup failed for %s", key, exc_info=True)
            if not session_id:
                logger.warning("agent-heartbeat: no current session id for %s, skip wake", key)
                _track_stat(key, "error", "no current session id")
                await asyncio.sleep(_interval(sc))
                continue

            # ── deliver wake ──
            adapter = _adapter_for_source(gateway, current_source)
            if adapter is None:
                logger.warning("agent-heartbeat: %s no adapter for platform", key)
                _track_stat(key, "error", "no adapter")
            else:
                try:
                    await deliver_wake(
                        adapter, text=prompt, session_id=session_id, source=current_source
                    )
                    _after_wake[key] = True
                    _clear_next_trigger(key)
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
        # Only clean up if THIS task is still the registered one.
        # If _cancel_loop_for_key already popped us (e.g. /xt set replacing
        # a stale loop) or a new loop took over, leave state intact.
        if _tasks.get(key) is this_task:
            _tasks.pop(key, None)
            _triggers.pop(key, None)
            _sources.pop(key, None)
            _last_user_message.pop(key, None)
            _after_wake.pop(key, None)
            _clear_next_trigger(key)
        logger.info("agent-heartbeat: loop ended for %s", key)


# ── hooks ──────────────────────────────────────────────────────────────────────


def _on_pre_gateway_dispatch(event: Any, gateway: Any, **_: Any) -> dict | None:
    """Intercept /xt commands, then bind heartbeat to sessions."""
    global _last_source

    source = getattr(event, "source", None)
    if source is None:
        return

    # Record the current message's source BEFORE interception so /xt command
    # handlers (set/status/...) know which conversation they came from.
    _last_source = source

    # ── Intercept only /xt before normal dispatch ──
    # Hermes core owns /heartbeat and /hb. The plugin deliberately does not
    # shadow either built-in name. /xt is the sole plugin command.
    text = (getattr(event, "text", "") or "").strip()
    xt_args = _extract_xt_command(text)
    if xt_args is not None:
        response_text = _cmd_xt(xt_args)
        if response_text:
            # Send reply via adapter, then skip built-in dispatch.
            # adapter.send is async — schedule it on the running event loop
            # instead of calling synchronously (which silently drops the
            # coroutine without delivering the message).
            try:
                adapter = _adapter_for_source(gateway, source)
                if adapter:
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(adapter.send(str(source.chat_id), response_text))
                    except RuntimeError:
                        logger.warning("agent-heartbeat: no running event loop, cannot send response")
                else:
                    logger.warning("agent-heartbeat: adapter is None for platform=%s chat=%s",
                                   getattr(source, "platform", "?"), getattr(source, "chat_id", "?"))
            except Exception:
                logger.warning("agent-heartbeat: failed to send command response", exc_info=True)
            return {"action": "skip", "reason": "agent-heartbeat handled command"}

    g = _global_config()
    if not bool(g.get("enabled", True)):
        return

    key = _session_key(source)

    # Only track user-initiated messages for idle detection
    # Check configuration before recording or binding ordinary messages.
    # Commands are intercepted above; unconfigured chats must remain entirely
    # invisible to Heartbeat and must never start a loop after restart.
    if not _is_session_active(key):
        return

    if _is_user_message(event):
        message_ts = datetime.now().timestamp()
        _last_user_message[key] = message_ts
        _track_stat(key, "last_user_message_ts", message_ts)
        _after_wake[key] = False  # allow heartbeat to fire again after user msg
        # Keep source fresh so wake uses the latest routing metadata
        _sources[key] = source
        # The countdown starts from this user message. The loop refreshes this
        # value too, but publishing it here makes /xt test and /xt stats useful
        # immediately after a message arrives.
        if key in _tasks and not _tasks[key].done():
            _set_next_trigger(key, message_ts + _interval(_get_session_config(key)))

    # Start the loop if not already running — uses the shared helper that
    # always refreshes _sources[key] first.
    _start_loop(gateway, source, key)


def _on_session_finalize(**kwargs: Any) -> None:
    """Cancel all heartbeat loops on session finalization (/new, /reset, shutdown).

    ``on_session_finalize`` fires when ``_finalize_session_off_loop`` runs
    with reason ``"new_session"`` (slash_commands._handle_reset_command) or
    on gateway shutdown.  The payload carries ``session_id``, ``platform``,
    ``reason``, ``old_session_id``, ``new_session_id`` — but NOT the
    plugin-level ``platform:chat_id:thread`` key, so we cancel ALL active
    loops.  The next user message in any configured chat will re-start its
    loop via ``_on_pre_gateway_dispatch`` → ``_start_loop``.
    """
    if not _tasks:
        return
    reason = kwargs.get("reason", "")
    count = len(_tasks)
    for k in list(_tasks.keys()):
        _cancel_loop_for_key(k)
    logger.info(
        "agent-heartbeat: cancelled %d loops on session finalize (reason=%s)",
        count, reason,
    )


def _on_session_end(**kwargs: Any) -> None:
    """Log agent turn finalization.  Per hooks.md this fires at each turn
    completion (completed/failed/interrupted).  We intentionally do NOT
    cancel loops here — that would kill the heartbeat after every wake.
    Session-boundary cancellation is handled by ``_on_session_finalize``.
    """
    session_id = kwargs.get("session_id", "")
    completed = kwargs.get("completed", False)
    if completed:
        logger.debug("agent-heartbeat: turn finalized for session %s", session_id)


# ── slash commands ─────────────────────────────────────────────────────────────


def _get_current_key() -> str | None:
    """Return the session key for the last incoming message, or None."""
    if _last_source is None:
        return None
    return _session_key(_last_source)


def _current_language() -> str:
    return _language_for_session(_get_current_key())


def _language_label(language: str) -> str:
    return "中文" if _normalize_language(language) == "zh" else "English"


def _cmd_xt(raw_args: str) -> str | None:
    args = _normalize_xt_args(raw_args)

    # ── /xt (no subcommand) — manual trigger for CURRENT session ──
    language = _current_language()

    if not args:
        current_key = _get_current_key()
        if current_key is None:
            return _t(language, "no_context")
        task = _tasks.get(current_key)
        if task is None or task.done():
            return _t(language, "no_active")
        event = _triggers.get(current_key)
        if event is not None:
            event.set()
            _clear_next_trigger(current_key)
            logger.info("agent-heartbeat: manual trigger for %s", current_key)
            return _t(language, "triggered", source=_format_source(current_key))
        return _t(language, "no_active_short")

    subcmd = args.split()[0].lower()

    # ── /xt help — localized command help ──
    if subcmd == "help":
        if language == "en":
            return (
                "**/xt Help**\n"
                "  `/xt` — trigger an immediate heartbeat in this session.\n"
                "  `/xt help` — show this help.\n"
                "  `/xt set` — enable heartbeat for this conversation (15m default).\n"
                "  `/xt unset` — disable heartbeat for this conversation.\n"
                "  `/xt status` — show current status and interval.\n"
                "  `/xt list` — list configured conversations.\n"
                "  `/xt config` — view current configuration.\n"
                "  `/xt config <key> <value>` — change a setting.\n"
                "  `/xt language en|zh` — change this conversation's language.\n"
                "  `/xt stats` — show wakeup statistics.\n"
                "  `/xt test` — check configuration without triggering a wakeup.\n"
                "  `/xt pause [30m|2h|seconds]` — pause heartbeat.\n"
                "  `/xt resume` — resume heartbeat.\n\n"
                "Default: prioritize unfinished work, then proactively advance one safe and useful related task."
            )
        return (
            "**/xt 命令说明**\n"
            "  `/xt` — 立即触发一次 Heartbeat，主动检查并推进未完成事项。\n"
            "  `/xt help` — 显示本帮助。\n"
            "  `/xt set` — 启用当前对话的 Heartbeat（默认每 15 分钟）。\n"
            "  `/xt unset` — 停用当前对话的 Heartbeat。\n"
            "  `/xt status` — 查看当前对话状态和间隔。\n"
            "  `/xt list` — 列出所有已配置的对话。\n"
            "  `/xt config` — 查看当前配置。\n"
            "  `/xt config <键> <值>` — 修改配置。\n"
            "  `/xt language zh|en` — 修改当前对话语言。\n"
            "  `/xt stats` — 查看唤醒统计。\n"
            "  `/xt test` — 检查配置但不触发唤醒。\n"
            "  `/xt pause [30m|2h|秒数]` — 暂停 Heartbeat。\n"
            "  `/xt resume` — 恢复 Heartbeat。\n\n"
            "默认行为：优先处理已有未完成任务；没有明确待办时，主动推进一个安全且有价值的相关事项。"
        )

    # `/xt language en|zh` is a convenient alias for per-session config.
    if subcmd in ("language", "lang"):
        key = _get_current_key()
        if key is None:
            return _t(language, "no_context")
        rest = args.split(None, 1)[1].strip() if len(args.split(None, 1)) > 1 else ""
        selected = _LANGUAGE_ALIASES.get(rest.lower())
        if selected not in ("zh", "en"):
            return ("❌ 请输入 `zh` 或 `en`。", "❌ Use `zh` or `en`.")[language == "en"]
        sessions = _load_sessions()
        if key not in sessions:
            return _t(language, "not_configured")
        sessions[key]["language"] = selected
        _save_sessions(sessions)
        return (("✅ 已将当前对话语言设置为中文。", "✅ Current conversation language set to English.")[selected == "en"])

    if subcmd == "status":
        current_key = _get_current_key()
        if current_key is None:
            return _t(language, "no_context")
        sessions = _load_sessions()
        sc = sessions.get(current_key)
        if sc is None or not sc.get("enabled", False):
            return _t(language, "not_configured")
        session_language = _language_for_session(current_key, sc)
        task = _tasks.get(current_key)
        is_active = task is not None and not task.done()
        paused_until = str(sc.get("paused_until", "") or "").strip()
        if paused_until:
            pause_status = (f"⏸️ 暂停至 {paused_until}" if session_language == "zh" else f"⏸️ Paused until {paused_until}")
        elif is_active:
            pause_status = "🟢 运行中" if session_language == "zh" else "🟢 Active"
        else:
            pause_status = "⚪ 已配置（等待消息）" if session_language == "zh" else "⚪ Configured (waiting for message)"
        if session_language == "zh":
            return f"**Heartbeat 状态** — {_format_source(current_key)}\n   状态：{pause_status}\n   间隔：{int(sc.get('interval', 900))} 秒\n   时间段：{sc.get('active_start', '') or '全天'}–{sc.get('active_end', '') or '全天'}"
        return f"**Heartbeat Status** — {_format_source(current_key)}\n  Status: {pause_status}\n  Interval: {int(sc.get('interval', 900))}s\n  Window: {sc.get('active_start', '') or 'all day'}–{sc.get('active_end', '') or 'all day'}"

    # ── /xt list ────────────────────────────────────────────────────
    if subcmd == "list":
        sessions = _load_sessions()
        if not sessions:
            return _t(language, "list_empty")
        if language == "en":
            list_title = "**Heartbeat Sessions:**"
            configured_label = "⚪ Configured"
            disabled_label = "🔴 Disabled"
            paused_label = "⏸️ Paused"
            active_label = "🟢 Active"
            interval_label = "Interval"
            prompt_label = "Prompt"
            window_label = "Window"
        else:
            list_title = "**Heartbeat 会话：**"
            configured_label = "⚪ 已配置"
            disabled_label = "🔴 已停用"
            paused_label = "⏸️ 已暂停"
            active_label = "🟢 运行中"
            interval_label = "间隔"
            prompt_label = "Prompt"
            window_label = "时间段"

        lines = []
        for key, sc in sorted(sessions.items()):
            enabled = sc.get("enabled", False)
            active = key in _tasks and not _tasks[key].done()
            if active:
                status = active_label
            elif enabled:
                paused_until = str(sc.get("paused_until", "") or "").strip()
                status = paused_label if paused_until else configured_label
            else:
                status = disabled_label
            interval = sc.get("interval", _DEFAULT_INTERVAL)
            lines.append(f"  {status} {_format_source(key)}")
            lines.append(f"         {interval_label}: {int(interval)}s")
            lines.append(f"         {prompt_label}: {sc.get('prompt_file', '(inline)')}")
            if sc.get("active_start"):
                lines.append(f"         {window_label}: {sc['active_start']}-{sc['active_end']} UTC{sc.get('utc_offset', '+8')}")
        return list_title + "\n" + "\n".join(lines)

    # ── /xt set ─────────────────────────────────────────────────────
    if subcmd == "set":
        g = _global_config()
        if g.get("enabled") is False:
            return _t(language, "global_disabled")
        key = _get_current_key()
        if key is None:
            return _t(language, "no_context")
        # Cancel any stale loop from a previous session so the next
        # message starts a fresh loop with current routing metadata.
        _cancel_loop_for_key(key)
        sessions = _load_sessions()
        defaults = _session_defaults()
        if key in sessions:
            # Older installs may contain a deliberately minimal entry such as
            # {"enabled": false}. Merge defaults before enabling it; otherwise
            # the acknowledgement itself raises KeyError("interval"), the hook
            # fails, and the gateway later reports /xt as unknown.
            merged = dict(defaults)
            merged.update(sessions[key])
            sessions[key] = merged
            sessions[key]["enabled"] = True
            sessions[key].pop("paused_until", None)
        else:
            sessions[key] = dict(defaults)
            sessions[key]["enabled"] = True
        _save_sessions(sessions)
        logger.info("agent-heartbeat: enabled for %s via /xt set", key)
        return _t(_language_for_session(key, sessions[key]), "set", source=_format_source(key), interval=int(sessions[key]["interval"]))

    # ── /xt unset ───────────────────────────────────────────────────
    if subcmd == "unset":
        key = _get_current_key()
        if key is None:
            return _t(language, "no_context")
        sessions = _load_sessions()
        if key in sessions:
            sessions[key]["enabled"] = False
            sessions[key].pop("paused_until", None)
            _save_sessions(sessions)
            # Actually cancel the running loop, not just flip the flag
            _cancel_loop_for_key(key)
            logger.info("agent-heartbeat: disabled for %s via /xt unset", key)
            return _t(_language_for_session(key, sessions[key]), "unset", source=_format_source(key))
        return _t(language, "not_configured")

    # ── /xt pause ───────────────────────────────────────────────────
    if subcmd == "pause":
        key = _get_current_key()
        if key is None:
            return _t(language, "no_context")
        sessions = _load_sessions()
        if key not in sessions:
            return _t(language, "not_configured_source", source=_format_source(key))

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
                return _t(language, "invalid_duration")

        paused_until = (datetime.now() + timedelta(seconds=duration)).isoformat()
        sessions[key]["paused_until"] = paused_until
        _save_sessions(sessions)
        logger.info("agent-heartbeat: paused for %s (%ds)", key, duration)
        human = f"{duration//60}m" if duration < 3600 else f"{duration//3600}h"
        return _t(_language_for_session(key, sessions[key]), "paused", source=_format_source(key), duration=human)

    # ── /xt resume ──────────────────────────────────────────────────
    if subcmd == "resume":
        key = _get_current_key()
        if key is None:
            return _t(language, "no_context")
        sessions = _load_sessions()
        if key not in sessions:
            return _t(language, "not_configured_source", source=_format_source(key))
        if "paused_until" not in sessions[key] or not sessions[key].get("paused_until"):
            return _t(language, "not_paused", source=_format_source(key))
        sessions[key].pop("paused_until", None)
        _save_sessions(sessions)
        logger.info("agent-heartbeat: resumed for %s", key)
        return _t(_language_for_session(key, sessions[key]), "resumed", source=_format_source(key))

    # ── /xt stats clear ────────────────────────────────────────────
    if subcmd == "stats" and len(args.split()) > 1 and args.split()[1].lower() == "clear":
        key = _get_current_key()
        if key is None:
            return _t(language, "no_context")
        _clear_stats(key)
        return _t(_language_for_session(key), "stats_cleared", source=_format_source(key))

    # ── /xt stats ───────────────────────────────────────────────────
    if subcmd == "stats":
        key = _get_current_key()
        if key is None:
            return _t(language, "no_context")
        stats = _load_stats()
        s = stats.get(key, {})
        task = _tasks.get(key)
        is_active = task is not None and not task.done()

        if language == "zh":
            lines = [f"**Heartbeat 统计：{_format_source(key)}**"]
            lines.append(f"  状态：{'🟢 运行中' if is_active else '⚪ 空闲'}")
            lines.append(f"  唤醒次数：{s.get('total_wakeups', 0)}")
            lines.append(f"  跳过次数：{s.get('total_skipped', 0)}")
        else:
            lines = [f"**Heartbeat Stats for {_format_source(key)}:**"]
            lines.append(f"  Status: {'🟢 Active' if is_active else '⚪ Idle'}")
            lines.append(f"  Total wakeups: {s.get('total_wakeups', 0)}")
            lines.append(f"  Total skipped: {s.get('total_skipped', 0)}")
        sc = _get_session_config(key)
        next_trigger = _fallback_next_trigger(key, sc)
        utc_offset = sc.get("utc_offset", "+8")
        if language == "zh":
            lines.append(f"  下一次触发：{_format_next_trigger(next_trigger, language, utc_offset)}")
        else:
            lines.append(f"  Next trigger: {_format_next_trigger(next_trigger, language, utc_offset)}")
        last_wakeup = s.get("last_wakeup_ts")
        if last_wakeup:
            try:
                dt = datetime.fromisoformat(last_wakeup)
                elapsed = (datetime.now() - dt).total_seconds()
                if elapsed < 60:
                    age = f"{int(elapsed)}{'秒前' if language == 'zh' else 's ago'}"
                elif elapsed < 3600:
                    age = f"{int(elapsed // 60)}{'分钟前' if language == 'zh' else 'm ago'}"
                else:
                    age = f"{elapsed / 3600:.1f}{'小时前' if language == 'zh' else 'h ago'}"
                lines.append(f"  {'最近唤醒' if language == 'zh' else 'Last wakeup'}: {age}")
            except (ValueError, TypeError):
                pass
        last_error = s.get("last_error")
        if last_error:
            lines.append(f"  {'最近错误' if language == 'zh' else 'Last error'}: `{last_error}`")
        last_skip_reason = s.get("last_skip_reason")
        if last_skip_reason:
            lines.append(f"  {'最近跳过' if language == 'zh' else 'Last skip'}: {last_skip_reason}")
        created = s.get("created_ts")
        if created:
            lines.append(f"  {'创建时间' if language == 'zh' else 'Created'}: {created}")
        return "\n".join(lines)

    # ── /xt stats clear ─────────────────────────────────────────────
    if subcmd == "stats" and len(args.split()) > 1 and args.split()[1].lower() == "clear":
        key = _get_current_key()
        if key is None:
            return _t(language, "no_context")
        _clear_stats(key)
        return _t(_language_for_session(key), "stats_cleared", source=_format_source(key))

    # ── /xt test — dry run ──────────────────────────────────────────
    if subcmd == "test":
        key = _get_current_key()
        if key is None:
            return _t(language, "no_context")
        sc = _get_session_config(key)
        g = _global_config()

        if language == "zh":
            lines = [f"**Heartbeat 测试：{_format_source(key)}**"]
            lines.append(f"  全局启用：{bool(g.get('enabled', True))}")
            lines.append(f"  会话启用：{bool(sc.get('enabled', False))}")
            lines.append(f"  间隔：{int(sc.get('interval', _DEFAULT_INTERVAL))} 秒")
        else:
            lines = [f"**Heartbeat Test for {_format_source(key)}:**"]
            lines.append(f"  Global enabled: {bool(g.get('enabled', True))}")
            lines.append(f"  Session enabled: {bool(sc.get('enabled', False))}")
            lines.append(f"  Interval: {int(sc.get('interval', _DEFAULT_INTERVAL))}s")

        # Check jitter
        jitter_pct = float(g.get("jitter", _DEFAULT_JITTER))
        if jitter_pct > 0:
            lines.append(f"  {'随机偏移' if language == 'zh' else 'Jitter'}: ±{jitter_pct * 100:.0f}%")

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
                if language == "zh":
                    lines.append(f"  时间段：{active_start}-{active_end} UTC{utc_offset_str}")
                    lines.append(f"  当前时间：{now.strftime('%H:%M')}（{'✅ 在时间段内' if in_window else '❌ 不在时间段内'}）")
                else:
                    lines.append(f"  Window: {active_start}-{active_end} UTC{utc_offset_str}")
                    lines.append(f"  Current time: {now.strftime('%H:%M')} ({'✅ in window' if in_window else '❌ outside window'})")
            except (ValueError, TypeError):
                pass
        else:
            lines.append("  时间段：始终启用" if language == "zh" else "  Window: always active")

        # Check idle
        idle_enabled = bool(sc.get("idle_auto_pause_enabled", False))
        if idle_enabled:
            last_ts = _last_user_message.get(key)
            if last_ts:
                elapsed = (datetime.now().timestamp() - last_ts) / 60
                idle_minutes = float(sc.get("idle_auto_pause_minutes", 120))
                if language == "zh":
                    lines.append(f"  空闲：距上次消息 {elapsed:.0f} 分钟（阈值：{int(idle_minutes)} 分钟）{'✅ 活跃' if elapsed < idle_minutes else '❌ 已暂停'}")
                else:
                    lines.append(f"  Idle: {elapsed:.0f}m since last message (threshold: {int(idle_minutes)}m) {'✅ active' if elapsed < idle_minutes else '❌ paused'}")
            else:
                lines.append("  空闲：尚无消息（活跃）" if language == "zh" else "  Idle: no messages yet (active)")
        else:
            lines.append("  空闲自动暂停：已禁用" if language == "zh" else "  Idle pause: disabled")

        # Check pause
        pause_remaining = _check_paused(sc)
        if pause_remaining is not None:
            lines.append(f"  {'暂停：剩余' if language == 'zh' else 'Paused:'} {int(pause_remaining)}{'秒' if language == 'zh' else 's remaining'}")
        else:
            lines.append("  暂停：否" if language == "zh" else "  Paused: no")

        # Check prompt
        prompt = _prompt(sc)
        if language == "zh":
            if prompt:
                preview = prompt[:80].replace("\n", "\\n")
                lines.append(f"  Prompt：{preview}…")
                lines.append(f"  Prompt 长度：{len(prompt)} 字符")
            else:
                lines.append("  ❌ 未配置 Prompt！请设置 prompt_file 或 prompt。")
        elif prompt:
            preview = prompt[:80].replace("\n", "\\n")
            lines.append(f"  Prompt: {preview}...")
            lines.append(f"  Prompt length: {len(prompt)} chars")
        else:
            lines.append("  ❌ No prompt configured! Set prompt_file or prompt.")

        # Check adapter
        adapter = _adapter_for_source(_gateway_ref, _last_source) if _gateway_ref and _last_source else None
        if language == "zh":
            lines.append(f"  适配器：{'✅ 可用' if adapter else '❌ 未找到'}")
        else:
            lines.append(f"  Adapter: {'✅ available' if adapter else '❌ not found'}")

        # Check if loop is running
        task = _tasks.get(key)
        if task and not task.done():
            lines.append("  循环状态：🟢 运行中" if language == "zh" else "  Loop status: 🟢 running")
        else:
            lines.append("  循环状态：⚪ 尚未启动（下一条消息时启动）" if language == "zh" else "  Loop status: ⚪ not started (will start on next message)")

        next_trigger = _fallback_next_trigger(key, sc)
        utc_offset = sc.get("utc_offset", "+8")
        lines.append(
            f"  下一次触发：{_format_next_trigger(next_trigger, language, utc_offset)}"
            if language == "zh"
            else f"  Next trigger: {_format_next_trigger(next_trigger, language, utc_offset)}"
        )

        return "\n".join(lines)

    # ── /xt config [key] [value] ────────────────────────────────────
    if subcmd == "config":
        key = _get_current_key()
        if key is None:
            return _t(language, "no_context")
        sessions = _load_sessions()
        if key not in sessions:
            return _t(language, "not_configured_source", source=_format_source(key))

        # Parse key=value or key value
        rest = args[len("config"):].strip()
        if not rest:
            # Show current config
            sc = _get_session_config(key)
            session_language = _language_for_session(key, sc)
            if session_language == "zh":
                lines = [
                    f"**Heartbeat 配置：{_format_source(key)}**",
                    f"  启用：{sc.get('enabled', False)}",
                    f"  间隔：{int(sc.get('interval', _DEFAULT_INTERVAL))} 秒",
                    f"  语言：{_language_label(session_language)}",
                    f"  prompt_file：{sc.get('prompt_file', '') or '（无）'}",
                    f"  prompt_files：{sc.get('prompt_files', []) or '（无）'}",
                    f"  活跃开始：{sc.get('active_start', '') or '（无）'}",
                    f"  活跃结束：{sc.get('active_end', '') or '（无）'}",
                    f"  UTC 偏移：{sc.get('utc_offset', '+8')}",
                    f"  空闲自动暂停：{sc.get('idle_auto_pause_enabled', False)}",
                    f"  空闲阈值：{sc.get('idle_auto_pause_minutes', 120)} 分钟",
                    f"  暂停至：{sc.get('paused_until', '') or '（无）'}",
                ]
            else:
                lines = [
                    f"**Heartbeat Config for {_format_source(key)}:**",
                    f"  Enabled: {sc.get('enabled', False)}",
                    f"  Interval: {int(sc.get('interval', _DEFAULT_INTERVAL))}s",
                    f"  Language: {_language_label(session_language)}",
                    f"  prompt_file: {sc.get('prompt_file', '') or '(none)'}",
                    f"  prompt_files: {sc.get('prompt_files', []) or '(none)'}",
                    f"  Active start: {sc.get('active_start', '') or '(none)'}",
                    f"  Active end: {sc.get('active_end', '') or '(none)'}",
                    f"  UTC offset: {sc.get('utc_offset', '+8')}",
                    f"  Idle auto-pause: {sc.get('idle_auto_pause_enabled', False)}",
                    f"  Idle threshold: {sc.get('idle_auto_pause_minutes', 120)}m",
                    f"  Paused until: {sc.get('paused_until', '') or '(none)'}",
                ]
            return "\n".join(lines)

        # Parse key value
        parts = rest.split(None, 1)
        if len(parts) < 2:
            return _t(language, "usage_config")
        cfg_key, cfg_val = parts[0], parts[1]
        sc = sessions[key]

        # Validate and coerce
        valid_keys = {
            "enabled", "interval", "prompt_file", "prompt_files", "prompt", "language",
            "active_start", "active_end", "utc_offset",
            "idle_auto_pause_enabled", "idle_auto_pause_minutes",
        }
        if cfg_key not in valid_keys:
            return _t(language, "unknown_config", config_key=cfg_key, valid=', '.join(sorted(valid_keys)))

        try:
            if cfg_key in ("enabled", "idle_auto_pause_enabled"):
                cfg_val = cfg_val.lower() in ("true", "1", "yes")
            elif cfg_key == "interval":
                cfg_val = _parse_interval_value(cfg_val)
            elif cfg_key == "idle_auto_pause_minutes":
                cfg_val = int(cfg_val)
            elif cfg_key == "prompt_files":
                cfg_val = [p.strip() for p in cfg_val.split(",") if p.strip()]
            elif cfg_key == "language":
                normalized = _LANGUAGE_ALIASES.get(str(cfg_val).strip().lower())
                if normalized not in ("zh", "en"):
                    raise ValueError("language must be zh or en")
                cfg_val = normalized
            elif cfg_key in ("active_start", "active_end", "prompt_file", "prompt", "utc_offset"):
                cfg_val = str(cfg_val)
            else:
                cfg_val = str(cfg_val)

            sc[cfg_key] = cfg_val
            _save_sessions(sessions)
            return _t(_language_for_session(key, sc), "config_set", source=_format_source(key), config_key=cfg_key, value=cfg_val)
        except (ValueError, TypeError):
            return _t(language, "invalid_config", config_key=cfg_key)

    return _t(language, "unknown_subcommand", subcmd=subcmd)


# ── entry point ────────────────────────────────────────────────────────────────


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    # on_session_finalize fires on /new, /reset, and gateway shutdown —
    # cancel all heartbeat loops so stale wakes never land in a reset session.
    ctx.register_hook("on_session_finalize", _on_session_finalize)
    # on_session_end fires at each turn finalization (agent done replying).
    # Kept as a lightweight observer; the heartbeat loop does NOT depend on it.
    ctx.register_hook("on_session_end", _on_session_end)
    # Primary interception is the pre_gateway_dispatch hook (see
    # _on_pre_gateway_dispatch), which fires before built-in dispatch and
    # sends the reply itself. /heartbeat and /hb are taken over there (Hermes
    # core has a built-in /heartbeat with alias /hb, so those two names are
    # rejected if registered normally). /xt never collides, so we also register
    # it as a normal command — a second safety net if the hook ever fails to
    # load.
    ctx.register_command(
        name="xt",
        handler=_cmd_xt,
        description="Persistent heartbeat: periodic wakeups in this conversation",
        args_hint="[set|unset|list|config|stats|test|pause|resume]",
        menu_priority=0,  # Telegram: show /xt first by default.
    )