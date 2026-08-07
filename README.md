# Hermes Agent Heartbeat Plugin

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![中文文档](https://img.shields.io/badge/文档-中文-blue)](README_zh.md)

A **heartbeat** plugin for [Hermes Agent](https://hermes-agent.nousresearch.com).  
It periodically wakes up the agent **in the same conversation**, preserving full context so the user can discuss, instruct, and steer between wakeups.

> **No more isolated cron jobs that forget what you said.**  
> The heartbeat shares the same session — every wakeup remembers the conversation history.

---

## Features

| # | Feature | Default |
|---|---------|---------|
| ✅ | **Periodic wakeup** — configurable interval (60s – 24h) | `900s` |
| 🆕 | **`/heartbeat`** — slash command to trigger an immediate wakeup | — |
| 🆕 | **Idle auto-pause** — skip heartbeats when you're away (opt-in) | `off` |
| 🆕 | **Active time window** — only fire during business hours (opt-in) | `off` |
| 🆕 | **UTC offset** — configure your timezone for the active window | `+8` |
| ✅ | **`[SILENT]`** — return this in the prompt to skip this cycle silently | — |
| ✅ | **Hot-reload config** — change interval/prompt without restarting gateway | — |
| ✅ | **Structured logging** — every wakeup is logged in `gateway.log` | — |

---

## Installation

### Prerequisites

- Hermes Agent (v0.2.0+)
- Telegram platform connected (other platforms coming soon)

### Install

```bash
# Clone into the plugins directory
git clone https://github.com/Love-AronaPlana/hermes-agent-heartbeat.git \
  ~/.hermes/plugins/agent-heartbeat

# Enable the plugin
hermes config set plugins.enabled '["agent-heartbeat"]'  # merge with existing entries

# Or via YAML (config.yaml):
# plugins:
#   enabled:
#     - agent-heartbeat
```

### Restart gateway

```bash
# User service (most common)
systemctl --user restart hermes-gateway

# Or via Hermes
hermes gateway restart
```

---

## Configuration

All settings go under `agent_heartbeat:` in `~/.hermes/config.yaml`.

```yaml
agent_heartbeat:
  # Required
  enabled: true
  platform: telegram
  chat_id: "123456789"                    # Your Telegram chat ID

  # Interval (60-86400 seconds)
  interval_seconds: 900                   # 15 minutes

  # Prompt source (file path or inline string)
  prompt_file: ~/.hermes/heartbeat/HEARTBEAT.md
  # prompt: "Inline prompt if no file"

  # Optional: active time window (default: off)
  active_start: "08:00"                   # Window start (local time)
  active_end: "22:00"                     # Window end (cross-midnight OK)
  utc_offset: "+8"                        # Your timezone offset

  # Optional: idle auto-pause (default: off)
  idle_auto_pause_enabled: true
  idle_auto_pause_minutes: 120            # Pause after 2h of no messages
```

### Quick config via CLI

```bash
hermes config set agent_heartbeat.enabled true
hermes config set agent_heartbeat.interval_seconds 1800
hermes config set agent_heartbeat.chat_id "123456789"
hermes config set agent_heartbeat.platform telegram
hermes config set agent_heartbeat.prompt_file "~/.hermes/heartbeat/HEARTBEAT.md"

# Optional: active window
hermes config set agent_heartbeat.active_start "08:00"
hermes config set agent_heartbeat.active_end "02:00"
hermes config set agent_heartbeat.utc_offset "+8"

# Optional: idle pause
hermes config set agent_heartbeat.idle_auto_pause_enabled true
hermes config set agent_heartbeat.idle_auto_pause_minutes 120
```

---

## Usage

### The prompt file

Create a prompt file (e.g. `~/.hermes/heartbeat/HEARTBEAT.md`):

```markdown
[Heartbeat Wakeup]

Check the current market state. If there's something worth reporting,
summarize it concisely. If nothing changed, return [SILENT] to stay quiet.
```

- The prompt is **re-read every cycle** — edit it live, no restart needed.
- Return `[SILENT]` as the first line of the prompt to skip this cycle silently.

### Manual trigger

In any conversation where the heartbeat is active, type:

```
/heartbeat
```

This immediately fires the next wakeup cycle, skipping the interval wait.

### Idle auto-pause

When enabled, the heartbeat tracks your last message. If no messages arrive
for `idle_auto_pause_minutes`, the heartbeat goes silent. The next message
you send restores it automatically.

### Active time window

When configured, the heartbeat only fires between `active_start` and
`active_end`. Cross-midnight windows work (e.g. 22:00-02:00). Configure
`utc_offset` to match your local timezone.

---

## How It Works

```
User sends a message
       │
       ▼
pre_gateway_dispatch hook  ──►  Starts asyncio loop
       │                              │
       │                              ▼
  (normal dispatch)           Every interval:
                                  1. Read config
                                  2. Check time window
                                  3. Check idle pause
                                  4. Read prompt file
                                  5. deliver_wake() → same session
                                  6. User sees agent response
                                  7. User can reply (context preserved)
```

The plugin registers:
- **1 hook**: `pre_gateway_dispatch` — binds to the session on first message
- **1 slash command**: `/heartbeat` — immediate manual trigger

---

## Development

```bash
# Clone and test
git clone https://github.com/Love-AronaPlana/hermes-agent-heartbeat.git
cd hermes-agent-heartbeat

# The plugin is tested live in a Hermes gateway — there's no separate test suite
# yet. Contributions welcome!
```

---

## Compatibility

| Platform | Status |
|----------|--------|
| Telegram | ✅ Tested |
| Discord | 🚧 Planned |
| WeChat | 🚧 Planned |

---

## License

MIT © Love-AronaPlana

---

## Related

- [Hermes Agent](https://hermes-agent.nousresearch.com)
- [Hermes Plugin API](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins)