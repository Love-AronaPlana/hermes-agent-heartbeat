"""Tests for the agent-heartbeat plugin (multi-session version)."""

import importlib.util
import json
import os
import sys
import types
from enum import Enum
from pathlib import Path
from unittest.mock import patch

# ── Hermes module stubs ────────────────────────────────────────────────────────
# The plugin imports Hermes internals (hermes_cli / gateway) at module top.
# These are NOT installable from PyPI, so in a clean CI environment we inject
# minimal stubs BEFORE loading the plugin. When the real modules are available
# (e.g. inside a Hermes venv), sys.modules already has them and setdefault
# leaves them untouched.


def _install_hermes_stubs() -> None:
    """Inject minimal stubs for Hermes internals when they are not importable."""
    if "hermes_cli" in sys.modules:
        return  # real Hermes present — do not stub

    # hermes_cli.config.load_config
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli_config = types.ModuleType("hermes_cli.config")
    hermes_cli_config.load_config = lambda: {}
    hermes_cli.config = hermes_cli_config
    sys.modules["hermes_cli"] = hermes_cli
    sys.modules["hermes_cli.config"] = hermes_cli_config

    # gateway.platforms.base.Platform
    gateway = types.ModuleType("gateway")
    gateway_platforms = types.ModuleType("gateway.platforms")
    gateway_platforms_base = types.ModuleType("gateway.platforms.base")

    class Platform(Enum):
        TELEGRAM = "telegram"
        DISCORD = "discord"

    gateway_platforms_base.Platform = Platform
    gateway.platforms = gateway_platforms
    gateway_platforms.base = gateway_platforms_base
    sys.modules["gateway"] = gateway
    sys.modules["gateway.platforms"] = gateway_platforms
    sys.modules["gateway.platforms.base"] = gateway_platforms_base

    # gateway.wake.deliver_wake
    gateway_wake = types.ModuleType("gateway.wake")

    async def deliver_wake(*args, **kwargs):
        return None

    gateway_wake.deliver_wake = deliver_wake
    sys.modules["gateway.wake"] = gateway_wake


_install_hermes_stubs()


def _load_plugin() -> object:
    plugin_dir = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "agent_heartbeat", plugin_dir / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class TestPluginImports:
    def test_import_ok(self):
        mod = _load_plugin()
        assert mod is not None

    def test_register_function_exists(self):
        mod = _load_plugin()
        assert hasattr(mod, "register")
        assert callable(mod.register)

    def test_constants(self):
        mod = _load_plugin()
        assert mod._MIN_INTERVAL == 60.0
        assert mod._MAX_INTERVAL == 86400.0
        assert mod._DEFAULT_INTERVAL == 900.0
        assert mod._DEFAULT_JITTER == 0.0
        assert mod._SESSIONS_FORMAT_VERSION == "0.5.1"


class TestSessionKey:
    def test_telegram_dm(self):
        mod = _load_plugin()

        class FakeSource:
            platform = mod.Platform.TELEGRAM
            chat_id = "6211819157"
            thread_id = None

        assert mod._session_key(FakeSource()) == "telegram:6211819157:"

    def test_telegram_topic(self):
        mod = _load_plugin()

        class FakeSource:
            platform = mod.Platform.TELEGRAM
            chat_id = "-100123"
            thread_id = "17585"

        assert mod._session_key(FakeSource()) == "telegram:-100123:17585"


class TestToMinutes:
    def test_hh_mm(self):
        mod = _load_plugin()
        assert mod._to_minutes("08:30") == 510

    def test_hh_only(self):
        mod = _load_plugin()
        assert mod._to_minutes("14") == 840

    def test_empty(self):
        mod = _load_plugin()
        assert mod._to_minutes("") == 0

    def test_invalid(self):
        mod = _load_plugin()
        assert mod._to_minutes("abc") == 0


class TestSessionsPersistence:
    def test_save_and_load(self, tmp_path):
        mod = _load_plugin()
        test_file = tmp_path / "sessions.json"
        with patch.object(mod, "_SESSIONS_FILE", test_file):
            data = {"telegram:123:": {"enabled": True, "interval": 600}}
            mod._save_sessions(data)
            loaded = mod._load_sessions()
            assert loaded == data

    def test_load_missing_file(self, tmp_path):
        mod = _load_plugin()
        test_file = tmp_path / "sessions.json"
        with patch.object(mod, "_SESSIONS_FILE", test_file):
            assert mod._load_sessions() == {}


class TestStatsPersistence:
    def test_save_and_load(self, tmp_path):
        mod = _load_plugin()
        test_file = tmp_path / "stats.json"
        with patch.object(mod, "_STATS_FILE", test_file):
            data = {"telegram:123:": {"total_wakeups": 5, "total_skipped": 2}}
            mod._save_stats(data)
            loaded = mod._load_stats()
            assert loaded == data

    def test_load_missing_file(self, tmp_path):
        mod = _load_plugin()
        with patch.object(mod, "_STATS_FILE", tmp_path / "missing-stats.json"):
            assert mod._load_stats() == {}

    def test_track_stat(self, tmp_path):
        mod = _load_plugin()
        test_file = tmp_path / "stats.json"
        with patch.object(mod, "_STATS_FILE", test_file):
            mod._track_stat("telegram:123:", "wakeup")
            mod._track_stat("telegram:123:", "last_user_message_ts", 1000.0)
            stats = mod._load_stats()
            assert stats["telegram:123:"]["total_wakeups"] == 1
            assert stats["telegram:123:"]["total_skipped"] == 0
            assert stats["telegram:123:"]["last_user_message_ts"] == 1000.0

    def test_track_stat_skip(self, tmp_path):
        mod = _load_plugin()
        test_file = tmp_path / "stats.json"
        with patch.object(mod, "_STATS_FILE", test_file):
            mod._track_stat("telegram:123:", "skip", "idle")
            stats = mod._load_stats()
            assert stats["telegram:123:"]["total_skipped"] == 1
            assert stats["telegram:123:"]["last_skip_reason"] == "idle"

    def test_clear_stats(self, tmp_path):
        mod = _load_plugin()
        test_file = tmp_path / "stats.json"
        with patch.object(mod, "_STATS_FILE", test_file):
            mod._track_stat("telegram:123:", "wakeup")
            mod._clear_stats("telegram:123:")
            stats = mod._load_stats()
            assert "telegram:123:" not in stats


class TestNextTrigger:
    def test_format_chinese_and_english(self, monkeypatch):
        mod = _load_plugin()
        now = 1_700_000_000.0
        monkeypatch.setattr(mod.time, "time", lambda: now)
        zh = mod._format_next_trigger(now + 120, "zh", "+8")
        en = mod._format_next_trigger(now + 120, "en", "+8")
        assert "分钟后" in zh
        assert "in 2m" in en
        assert "UTC+8" in zh

    def test_fallback_uses_last_message_and_interval(self, monkeypatch):
        mod = _load_plugin()
        monkeypatch.setattr(mod, "_next_trigger_at", {})
        monkeypatch.setitem(mod._last_user_message, "telegram:1:", 1000.0)
        assert mod._fallback_next_trigger("telegram:1:", {"enabled": True, "interval": 900}) == 1900.0

    def test_fallback_is_unscheduled_after_wakeup_or_pause(self, monkeypatch):
        mod = _load_plugin()
        monkeypatch.setitem(mod._last_user_message, "telegram:1:", 1000.0)
        monkeypatch.setitem(mod._after_wake, "telegram:1:", True)
        assert mod._fallback_next_trigger("telegram:1:", {"enabled": True, "interval": 900}) is None
        monkeypatch.setitem(mod._after_wake, "telegram:1:", False)
        assert mod._fallback_next_trigger(
            "telegram:1:",
            {"enabled": True, "interval": 900, "paused_until": "2099-01-01T00:00:00"},
        ) is None

    def test_stats_and_test_show_next_trigger(self, tmp_path, monkeypatch):
        mod = _load_plugin()
        source = types.SimpleNamespace(
            platform=mod.Platform.TELEGRAM, chat_id="1", thread_id=None
        )
        key = "telegram:1:"
        mod._last_source = source
        now = 1_700_000_000.0
        monkeypatch.setattr(mod.time, "time", lambda: now)
        monkeypatch.setitem(mod._next_trigger_at, key, now + 120)
        sessions_file = tmp_path / "sessions.json"
        stats_file = tmp_path / "stats.json"
        with patch.object(mod, "_SESSIONS_FILE", sessions_file), patch.object(
            mod, "_STATS_FILE", stats_file
        ):
            mod._save_sessions({key: {"enabled": True, "interval": 900}})
            stats = mod._cmd_xt("stats")
            test = mod._cmd_xt("test")
            mod._save_sessions({key: {"enabled": True, "interval": 900, "language": "en"}})
            stats_en = mod._cmd_xt("stats")
            test_en = mod._cmd_xt("test")
        assert "下一次触发" in stats
        assert "2分钟后" in stats
        assert "下一次触发" in test
        assert "2分钟后" in test
        assert "Next trigger" in stats_en
        assert "in 2m" in stats_en
        assert "Next trigger" in test_en
        assert "in 2m" in test_en


class TestInterval:
    def test_default_interval(self):
        mod = _load_plugin()
        assert mod._interval({}) == 900.0

    def test_custom_interval(self):
        mod = _load_plugin()
        assert mod._interval({"interval": 1800}) == 1800.0

    def test_clamped_min(self):
        mod = _load_plugin()
        assert mod._interval({"interval": 5}) == 60.0

    def test_clamped_max(self):
        mod = _load_plugin()
        assert mod._interval({"interval": 99999}) == 86400.0


class TestActiveWindow:
    def test_no_window(self):
        mod = _load_plugin()
        assert mod._in_active_window({}) is True

    def test_empty_window(self):
        mod = _load_plugin()
        assert mod._in_active_window({"active_start": "", "active_end": ""}) is True

    def test_invalid_window_defaults_true(self):
        mod = _load_plugin()
        assert mod._in_active_window({"active_start": "bad", "active_end": "bad"}) is True


class TestIdlePause:
    def test_disabled_by_default(self):
        mod = _load_plugin()
        assert mod._check_idle_pause({}, "test:key") is False

    def test_disabled_explicit(self):
        mod = _load_plugin()
        assert mod._check_idle_pause({"idle_auto_pause_enabled": False}, "test:key") is False

    def test_no_interaction(self):
        mod = _load_plugin()
        assert mod._check_idle_pause({"idle_auto_pause_enabled": True}, "test:none") is False


class TestPause:
    def test_no_pause(self):
        mod = _load_plugin()
        assert mod._check_paused({}) is None

    def test_empty_pause(self):
        mod = _load_plugin()
        assert mod._check_paused({"paused_until": ""}) is None

    def test_future_pause(self):
        mod = _load_plugin()
        from datetime import datetime, timedelta
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        result = mod._check_paused({"paused_until": future})
        assert result is not None
        assert result > 0

    def test_expired_pause(self):
        mod = _load_plugin()
        from datetime import datetime, timedelta
        past = (datetime.now() - timedelta(hours=1)).isoformat()
        assert mod._check_paused({"paused_until": past}) is None


class TestPrompt:
    def test_inline_prompt(self):
        mod = _load_plugin()
        result = mod._prompt({"prompt": "Hello heartbeat"})
        assert result == "Hello heartbeat"

    def test_empty_prompt_uses_default_chinese_with_silent_policy(self):
        mod = _load_plugin()
        prompt = mod._prompt({})
        assert "[Heartbeat 唤醒]" in prompt
        assert "[SILENT]" in prompt
        assert "必须严格只输出 [SILENT]" in prompt
        assert "只适用于自动 Heartbeat 唤醒" in prompt

    def test_default_prompt_can_be_english_with_silent_policy(self):
        mod = _load_plugin()
        prompt = mod._prompt({"language": "en"})
        assert "[Heartbeat Wakeup]" in prompt
        assert "MUST be exactly [SILENT]" in prompt
        assert "only to automatic Heartbeat wakeups" in prompt

    def test_custom_prompt_is_preserved(self):
        mod = _load_plugin()
        assert mod._prompt({"prompt": "Custom prompt"}) == "Custom prompt"

    def test_language_normalization_defaults_to_chinese(self):
        mod = _load_plugin()
        assert mod._normalize_language("unknown") == "zh"
        assert mod._normalize_language("English") == "en"

    def test_localized_text(self):
        mod = _load_plugin()
        assert "上下文" in mod._t("zh", "no_context")
        assert "session context" in mod._t("en", "no_context")

    def test_prompt_files_empty_list(self):
        mod = _load_plugin()
        result = mod._prompt({"prompt_files": [], "prompt": "fallback"})
        assert result == "fallback"

    def test_prompt_file_overrides_inline(self, tmp_path):
        mod = _load_plugin()
        pf = tmp_path / "test_prompt.md"
        pf.write_text("file content")
        result = mod._prompt({"prompt_file": str(pf), "prompt": "inline"})
        assert result == "file content"


class TestFormatSource:
    def test_dm(self):
        mod = _load_plugin()
        assert mod._format_source("telegram:6211819157:") == "telegram/6211819157/DM"

    def test_thread(self):
        mod = _load_plugin()
        assert mod._format_source("telegram:-100123:17585") == "telegram/-100123/17585"


class TestCmdHeartbeat:
    def test_chinese_interval_alias_normalizes_to_config(self):
        mod = _load_plugin()
        assert mod._normalize_xt_args("间隔 1800") == "config interval 1800"
        assert mod._normalize_xt_args("config 间隔 1800") == "config interval 1800"

    def test_no_subcommand_no_active(self):
        mod = _load_plugin()
        result = mod._cmd_xt("")
        assert "上下文" in result

    def test_list_empty(self, tmp_path):
        mod = _load_plugin()
        test_file = tmp_path / "sessions.json"
        with patch.object(mod, "_SESSIONS_FILE", test_file):
            result = mod._cmd_xt("list")
            assert "set" in result

    def test_set_no_context(self):
        mod = _load_plugin()
        mod._last_source = None
        result = mod._cmd_xt("set")
        assert "上下文" in result

    def test_unknown_subcommand(self):
        mod = _load_plugin()
        result = mod._cmd_xt("foobar")
        assert "未知子命令" in result

    def test_pause_no_context(self):
        mod = _load_plugin()
        mod._last_source = None
        result = mod._cmd_xt("pause")
        assert "上下文" in result

    def test_resume_no_context(self):
        mod = _load_plugin()
        mod._last_source = None
        result = mod._cmd_xt("resume")
        assert "上下文" in result

    def test_stats_no_context(self):
        mod = _load_plugin()
        mod._last_source = None
        result = mod._cmd_xt("stats")
        assert "上下文" in result

    def test_test_no_context(self):
        mod = _load_plugin()
        mod._last_source = None
        result = mod._cmd_xt("test")
        assert "上下文" in result

    def test_config_no_context(self):
        mod = _load_plugin()
        mod._last_source = None
        result = mod._cmd_xt("config")
        assert "上下文" in result

    def test_pause_duration_invalid(self):
        mod = _load_plugin()
        mod._last_source = object()  # non-None but won't match sessions
        result = mod._cmd_xt("pause 30x")
        # Should return the localized not-configured message.
        assert "尚未配置" in result

    def test_language_command_requires_configured_session(self):
        mod = _load_plugin()
        mod._last_source = object()
        result = mod._cmd_xt("language en")
        assert "尚未配置" in result


class TestIsUserMessage:
    def test_user_message(self):
        mod = _load_plugin()

        class FakeMsg:
            role = "user"

        class FakeEvent:
            message = FakeMsg()

        assert mod._is_user_message(FakeEvent()) is True

    def test_assistant_message(self):
        mod = _load_plugin()

        class FakeMsg:
            role = "assistant"

        class FakeEvent:
            message = FakeMsg()

        assert mod._is_user_message(FakeEvent()) is False

    def test_no_message_returns_true(self):
        mod = _load_plugin()

        class FakeEvent:
            pass

        assert mod._is_user_message(FakeEvent()) is True


class TestRegister:
    def test_register_creates_hook_and_command(self):
        mod = _load_plugin()

        class FakeCtx:
            def __init__(self):
                self.hooks = []
                self.commands = []

            def register_hook(self, name, callback):
                self.hooks.append((name, callback))

            def register_command(self, name, handler, description="", args_hint="", **kwargs):
                self.commands.append((name, handler, description, args_hint, kwargs))

        ctx = FakeCtx()
        mod.register(ctx)

        assert [name for name, _ in ctx.hooks] == [
            "pre_gateway_dispatch", "on_session_finalize", "on_session_end"
        ]
        assert len(ctx.commands) == 1  # /xt registered as a normal command
        assert ctx.commands[0][0] == "xt"
        assert ctx.commands[0][4]["menu_priority"] == 0