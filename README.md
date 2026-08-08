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
| ✅ | **Multi-session** — independent heartbeat per channel/thread | — |
| ✅ | **Periodic wakeup** — configurable interval (60s – 24h) | `900s` |
| 🆕 | **`/heartbeat set`** — enable heartbeat for the current conversation | — |
| 🆕 | **`/heartbeat unset`** — disable heartbeat for the current conversation | — |
| 🆕 | **`/heartbeat list`** — view all configured sessions and their status | — |
| 🆕 | **`/heartbeat config [key] [value]`** — view/set per-session settings | — |
| 🆕 | **`/heartbeat`** — trigger an immediate wakeup in the current session | — |
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

Two layers of config:

1. **Global settings** under `agent_heartbeat:` in `~/.hermes/config.yaml` — the master switch and defaults for new sessions.
2. **Per-session settings** in `~/.hermes/heartbeat/sessions.json` — managed via `/heartbeat set` / `/heartbeat config` slash commands (no manual editing needed).

### Global config (config.yaml)

```yaml
agent_heartbeat:
  # Required: master switch (must be true for any session to fire)
  enabled: true

  # Defaults applied when a new session is added via /heartbeat set
  default_interval: 900                   # 60-86400 seconds
  default_prompt_file: ~/.hermes/heartbeat/HEARTBEAT.md
```

### Per-session config (via slash commands)

```bash
# In any conversation:
/heartbeat set                    # Enable heartbeat for THIS conversation
/heartbeat config                 # View current session settings
/heartbeat config interval 1800   # Change this session's interval (seconds)
/heartbeat config prompt_file ~/.hermes/heartbeat/HEARTBEAT.md
/heartbeat config active_start 08:00
/heartbeat config active_end 22:00
/heartbeat config utc_offset +8
/heartbeat config idle_auto_pause_enabled true
/heartbeat config idle_auto_pause_minutes 120
/heartbeat unset                  # Disable heartbeat for THIS conversation
```

Per-session settings are stored in `~/.hermes/heartbeat/sessions.json`:

```json
{
  "telegram:123456789:": {
    "enabled": true,
    "interval": 900,
    "prompt_file": "~/.hermes/heartbeat/HEARTBEAT.md",
    "active_start": "",
    "active_end": "",
    "utc_offset": "+8",
    "idle_auto_pause_enabled": false,
    "idle_auto_pause_minutes": 120
  }
}
```

Session keys follow the format `platform:chat_id:thread_id` — e.g. `telegram:123456789:` for a DM, `telegram:-1001234567890:17585` for a topic thread.

### Quick config via CLI

```bash
# Master switch
hermes config set agent_heartbeat.enabled true
hermes config set agent_heartbeat.default_interval 1800
hermes config set agent_heartbeat.default_prompt_file "~/.hermes/heartbeat/HEARTBEAT.md"
```

---

## Usage

### Quick start

```bash
# 1. Enable the master switch (once)
hermes config set agent_heartbeat.enabled true

# 2. In the conversation where you want a heartbeat:
/heartbeat set
```

### Slash commands

| Command | What it does |
|---------|--------------|
| `/heartbeat` | Trigger an immediate wakeup in the current session |
| `/heartbeat set` | Enable heartbeat for the **current** conversation |
| `/heartbeat unset` | Disable heartbeat for the **current** conversation |
| `/heartbeat list` | Show all configured sessions and their status |
| `/heartbeat config` | View the current session's settings |
| `/heartbeat config <key> <value>` | Change a setting (interval, prompt_file, active_start, active_end, utc_offset, idle_auto_pause_*) |

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

```bash
/heartbeat config idle_auto_pause_enabled true
/heartbeat config idle_auto_pause_minutes 120
```

### Active time window

When configured, the heartbeat only fires between `active_start` and
`active_end`. Cross-midnight windows work (e.g. 22:00-02:00). Configure
`utc_offset` to match your local timezone.

```bash
/heartbeat config active_start 08:00
/heartbeat config active_end 02:00
/heartbeat config utc_offset +8
```

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