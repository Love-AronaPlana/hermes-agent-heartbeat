# Hermes Agent Heartbeat Plugin

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Test](https://github.com/Love-AronaPlana/hermes-agent-heartbeat/actions/workflows/test.yml/badge.svg)](https://github.com/Love-AronaPlana/hermes-agent-heartbeat/actions/workflows/test.yml)
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
| ✅ | **`/xt set`** — enable heartbeat for the current conversation | — |
| ✅ | **`/xt unset`** — disable heartbeat for the current conversation | — |
| ✅ | **`/xt list`** — view all configured sessions and their status | — |
| ✅ | **`/xt config [key] [value]`** — view/set per-session settings | — |
| ✅ | **`/xt`** — trigger an immediate wakeup in the current session | — |
| ✅ | **`/xt stats`** — view wakeup statistics (total, skipped, last error) | — |
| ✅ | **`/xt stats clear`** — reset statistics for the current session | — |
| ✅ | **`/xt test`** — dry-run showing all config checks without delivering | — |
| ✅ | **`/xt pause [duration]`** — temporarily pause heartbeats (`30m`, `2h`, or seconds) | `1h` |
| ✅ | **`/xt resume`** — resume paused heartbeats | — |
| ✅ | **Idle auto-pause** — skip heartbeats when you're away (opt-in) | `off` |
| ✅ | **Active time window** — only fire during business hours (opt-in) | `off` |
| ✅ | **UTC offset** — configure your timezone for the active window | `+8` |
| ✅ | **Multi-prompt rotation** — array of prompt files, picked randomly each cycle | — |
| ✅ | **Jitter** — random interval offset (±10% etc.) to avoid predictable beats | `0%` |
| ✅ | **Persistent stats** — wakeup/skip counts survive gateway restarts | — |
| ✅ | **`[SILENT]`** — return this in the prompt to skip this cycle silently | — |
| ✅ | **Hot-reload config** — change interval/prompt without restarting gateway | — |
| ✅ | **Structured logging** — every wakeup is logged in `gateway.log` | — |
| ✅ | **Graceful shutdown** — all heartbeat tasks cancelled on session end | — |
| ✅ | **Multi-platform** — dynamically adapts to any platform adapter (Telegram, Discord, etc.) | — |

---

## Installation

### Prerequisites

- Hermes Agent (v0.2.0+)
- Any messaging platform connected (Telegram, Discord, WeChat, etc.)

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
2. **Per-session settings** in `~/.hermes/heartbeat/sessions.json` — managed via `/xt set` / `/xt config` slash commands (no manual editing needed).

### Global config (config.yaml)

```yaml
agent_heartbeat:
  # Required: master switch (must be true for any session to fire)
  enabled: true

  # Defaults applied when a new session is added via /xt set
  default_interval: 900                   # 60-86400 seconds
  default_prompt_file: ~/.hermes/heartbeat/HEARTBEAT.md

  # Optional extras
  jitter: 0.1                             # ±10% random offset on interval
  default_prompt_files:                    # multi-prompt rotation (picked randomly)
    - ~/.hermes/heartbeat/morning.md
    - ~/.hermes/heartbeat/evening.md
```

### Per-session config (via slash commands)

```bash
# In any conversation:
/xt set                    # Enable heartbeat for THIS conversation
/xt config                 # View current session settings
/xt config interval 1800   # Change this session's interval (seconds)
/xt config prompt_file ~/.hermes/heartbeat/HEARTBEAT.md
/xt config prompt_files file1.md,file2.md  # comma-separated list
/xt config active_start 08:00
/xt config active_end 22:00
/xt config utc_offset +8
/xt config idle_auto_pause_enabled true
/xt config idle_auto_pause_minutes 120
/xt unset                  # Disable heartbeat for THIS conversation
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
/xt set
```

### Slash commands

> **Aliases:** `/heartbeat` and `/hb` are also intercepted and route to the same
> handler, so both old muscle memory and the Hermes built-in name keep working.

| Command | What it does |
|---------|--------------|
| `/xt` | Trigger an immediate wakeup in the current session |
| `/xt set` | Enable heartbeat for the **current** conversation |
| `/xt unset` | Disable heartbeat for the **current** conversation |
| `/xt list` | Show all configured sessions and their status |
| `/xt config` | View the current session's settings |
| `/xt config <key> <value>` | Change a setting |
| `/xt stats` | View wakeup statistics |
| `/xt stats clear` | Reset statistics for current session |
| `/xt test` | Dry-run: check all config conditions without actual delivery |
| `/xt pause 30m` | Temporarily pause (30m, 2h, or seconds) |
| `/xt resume` | Resume paused heartbeats |

### The prompt file

Create a prompt file (e.g. `~/.hermes/heartbeat/HEARTBEAT.md`):

```markdown
[Heartbeat Wakeup]

Check the current market state. If there's something worth reporting,
summarize it concisely. If nothing changed, return [SILENT] to stay quiet.
```

- The prompt is **re-read every cycle** — edit it live, no restart needed.
- Return `[SILENT]` as the first line of the response to skip this cycle silently.

### Multi-prompt rotation

Configure multiple prompt files for variety:

```yaml
# config.yaml
agent_heartbeat:
  default_prompt_files:
    - ~/.hermes/heartbeat/market.md
    - ~/.hermes/heartbeat/system.md
    - ~/.hermes/heartbeat/greeting.md
```

Each cycle picks one randomly. If all files are missing, falls back to
`prompt_file` → inline `prompt`.

### Jitter

Add a random offset to the interval to avoid predictable beats:

```yaml
agent_heartbeat:
  jitter: 0.1   # ±10% random offset (range: 0.0 - 0.5)
```

### Manual trigger

In any conversation where the heartbeat is active, type:

```
/xt
```

This immediately fires the next wakeup cycle in the **current session only**,
skipping the interval wait.

### Idle auto-pause

When enabled, the heartbeat tracks your last message. If no messages arrive
for `idle_auto_pause_minutes`, the heartbeat goes silent. The next message
you send restores it automatically.

```bash
/xt config idle_auto_pause_enabled true
/xt config idle_auto_pause_minutes 120
```

> **Note:** Idle tracking only counts user-initiated messages — agent responses
> from heartbeat wakeups don't reset the idle timer.

### Active time window

When configured, the heartbeat only fires between `active_start` and
`active_end`. Cross-midnight windows work (e.g. 22:00-02:00). Configure
`utc_offset` to match your local timezone.

```bash
/xt config active_start 08:00
/xt config active_end 02:00
/xt config utc_offset +8
```

### Pause/Resume

Temporarily pause heartbeats without disabling the session config:

```bash
/xt pause          # Pause for 1 hour (default)
/xt pause 30m      # Pause for 30 minutes
/xt pause 2h       # Pause for 2 hours
/xt pause 7200     # Pause for 7200 seconds
/xt resume         # Resume immediately
```

### Stats

View wakeup statistics:

```bash
/xt stats
# > Heartbeat Stats for telegram/6211819157/DM:
# >   Status: 🟢 Active
# >   Total wakeups: 47
# >   Total skipped: 3
# >   Last wakeup: 12m ago
# >   Last error: (none)

/xt stats clear    # Reset counters
```

### Test (dry-run)

Check all conditions without actually delivering a wakeup:

```bash
/xt test
# > Heartbeat Test for telegram/6211819157/DM:
# >   Global enabled: True
# >   Session enabled: True
# >   Interval: 900s
# >   Jitter: ±10%
# >   Window: 08:00-02:00 UTC+8
# >   Current time: 14:35 (✅ in window)
# >   Idle: 5m since last message (active)
# >   Paused: no
# >   Prompt: [Heartbeat Wakeup]\n\nCheck the current market...
# >   Prompt length: 142 chars
# >   Adapter: ✅ available
# >   Loop status: 🟢 running
```

---

## How It Works

```
User sends a message
       │
       ▼
pre_gateway_dispatch hook  ──►  Starts asyncio loop (with lock)
       │                              │
       │                              ▼
  (normal dispatch)           Every interval:
                                  1. Read config (hot-reload)
                                  2. Check time window
                                  3. Check idle pause (user msgs only)
                                  4. Check manual pause
                                  5. Read prompt file (hot-reload)
                                  6. deliver_wake() → same session
                                  7. Track stats
                                  8. User sees agent response
                                  9. User can reply (context preserved)
```

The plugin registers:
- **2 hooks**: `pre_gateway_dispatch` — binds to session, `on_session_end` — graceful shutdown
- **1 slash command**: `/xt` — manual trigger with all subcommands

---

## Development

```bash
# Clone and test
git clone https://github.com/Love-AronaPlana/hermes-agent-heartbeat.git
cd hermes-agent-heartbeat

# Run unit tests
python3 -m pytest tests/ -v
```

---

## Compatibility

| Platform | Status |
|----------|--------|
| Telegram | ✅ Tested |
| Discord  | ✅ Supported (dynamic adapter) |
| WeChat   | ✅ Supported (dynamic adapter) |
| Feishu   | ✅ Supported (dynamic adapter) |

The plugin dynamically looks up the correct adapter for each platform —
no hardcoded Telegram dependency.

---

## License

MIT © Love-AronaPlana

---

## Related

- [Hermes Agent](https://hermes-agent.nousresearch.com)
- [Hermes Plugin API](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins)