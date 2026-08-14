"""Tests for the agent-heartbeat plugin (multi-session version)."""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch


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

    def test_load_missing_file(self):
        mod = _load_plugin()
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

    def test_load_missing_file(self):
        mod = _load_plugin()
        assert mod._load_stats() == {}

    def test_track_stat(self, tmp_path):
        mod = _load_plugin()
        test_file = tmp_path / "stats.json"
        with patch.object(mod, "_STATS_FILE", test_file):
            mod._track_stat("telegram:123:", "wakeup")
            stats = mod._load_stats()
            assert stats["telegram:123:"]["total_wakeups"] == 1
            assert stats["telegram:123:"]["total_skipped"] == 0

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

    def test_empty_prompt(self):
        mod = _load_plugin()
        assert mod._prompt({}) == ""

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
    def test_no_subcommand_no_active(self):
        mod = _load_plugin()
        result = mod._cmd_heartbeat("")
        assert "context" in result

    def test_list_empty(self):
        mod = _load_plugin()
        result = mod._cmd_heartbeat("list")
        assert "set" in result

    def test_set_no_context(self):
        mod = _load_plugin()
        mod._last_source = None
        result = mod._cmd_heartbeat("set")
        assert "context" in result

    def test_unknown_subcommand(self):
        mod = _load_plugin()
        result = mod._cmd_heartbeat("foobar")
        assert "Unknown" in result

    def test_pause_no_context(self):
        mod = _load_plugin()
        mod._last_source = None
        result = mod._cmd_heartbeat("pause")
        assert "context" in result

    def test_resume_no_context(self):
        mod = _load_plugin()
        mod._last_source = None
        result = mod._cmd_heartbeat("resume")
        assert "context" in result

    def test_stats_no_context(self):
        mod = _load_plugin()
        mod._last_source = None
        result = mod._cmd_heartbeat("stats")
        assert "context" in result

    def test_test_no_context(self):
        mod = _load_plugin()
        mod._last_source = None
        result = mod._cmd_heartbeat("test")
        assert "context" in result

    def test_config_no_context(self):
        mod = _load_plugin()
        mod._last_source = None
        result = mod._cmd_heartbeat("config")
        assert "context" in result

    def test_pause_duration_invalid(self):
        mod = _load_plugin()
        mod._last_source = object()  # non-None but won't match sessions
        result = mod._cmd_heartbeat("pause 30x")
        # Should return "not configured" since session not in sessions.json
        assert "not configured" in result


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

            def register_command(self, name, handler, description="", args_hint=""):
                self.commands.append((name, handler, description, args_hint))

        ctx = FakeCtx()
        mod.register(ctx)

        assert len(ctx.hooks) == 2  # pre_gateway_dispatch + on_session_end
        assert ctx.hooks[0][0] == "pre_gateway_dispatch"
        assert ctx.hooks[1][0] == "on_session_end"
        assert len(ctx.commands) == 1
        assert ctx.commands[0][0] == "heartbeat"
        assert "set" in ctx.commands[0][2]  # description mentions subcommands