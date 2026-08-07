"""Tests for the agent-heartbeat plugin."""

import importlib.util
import sys
from pathlib import Path


def _load_plugin() -> object:
    """Load the plugin module from the source directory."""
    plugin_dir = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "agent_heartbeat", plugin_dir / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class TestPluginImports:
    """Verify the plugin module loads without errors."""

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


class TestInterval:
    def test_default_interval(self):
        mod = _load_plugin()
        assert mod._interval({}) == 900.0

    def test_custom_interval(self):
        mod = _load_plugin()
        assert mod._interval({"interval_seconds": 1800}) == 1800.0

    def test_clamped_min(self):
        mod = _load_plugin()
        assert mod._interval({"interval_seconds": 5}) == 60.0

    def test_clamped_max(self):
        mod = _load_plugin()
        assert mod._interval({"interval_seconds": 99999}) == 86400.0


class TestActiveWindow:
    def test_no_window(self):
        mod = _load_plugin()
        assert mod._in_active_window({}) is True

    def test_empty_window(self):
        mod = _load_plugin()
        assert mod._in_active_window({"active_start": "", "active_end": ""}) is True

    def test_invalid_window_defaults_true(self):
        mod = _load_plugin()
        assert mod._in_active_window({"active_start": "not-a-time", "active_end": "not-a-time"}) is True


class TestIdlePause:
    def test_disabled_by_default(self):
        mod = _load_plugin()
        assert mod._check_idle_pause({}, "test:key") is False

    def test_disabled_explicit(self):
        mod = _load_plugin()
        assert mod._check_idle_pause({"idle_auto_pause_enabled": False}, "test:key") is False

    def test_no_interaction_recorded(self):
        mod = _load_plugin()
        assert mod._check_idle_pause({"idle_auto_pause_enabled": True}, "test:none") is False


class TestPrompt:
    def test_inline_prompt(self):
        mod = _load_plugin()
        result = mod._prompt({"prompt": "Hello heartbeat"})
        assert result == "Hello heartbeat"

    def test_empty_prompt(self):
        mod = _load_plugin()
        assert mod._prompt({}) == ""

    def test_missing_file(self):
        mod = _load_plugin()
        result = mod._prompt({"prompt_file": "/tmp/nonexistent-heartbeat-prompt.md"})
        assert result == ""  # Falls back to inline prompt, which is empty


class TestRegister:
    def test_register_called(self):
        """Verify register() accepts a context-like object."""
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

        assert len(ctx.hooks) == 1
        assert ctx.hooks[0][0] == "pre_gateway_dispatch"

        assert len(ctx.commands) == 1
        assert ctx.commands[0][0] == "heartbeat"