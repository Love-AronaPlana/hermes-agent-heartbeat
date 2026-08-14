# Hermes Agent Heartbeat 插件

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Test](https://github.com/Love-AronaPlana/hermes-agent-heartbeat/actions/workflows/test.yml/badge.svg)](https://github.com/Love-AronaPlana/hermes-agent-heartbeat/actions/workflows/test.yml)

为 [Hermes Agent](https://hermes-agent.nousresearch.com) 打造的 **心跳（Heartbeat）** 插件。
它会在 **同一个会话** 中周期性唤醒 agent，完整保留上下文——你可以在两次唤醒之间随时讨论、下指令、调整方向。

> **告别那些忘记你说过什么的孤立 cron 任务。**
> 心跳共享同一个会话——每次唤醒都记得完整的对话历史。

---

## 功能特性

| # | 功能 | 默认值 |
|---|------|--------|
| ✅ | **多会话** — 每个频道/线程独立的心跳 | — |
| ✅ | **周期唤醒** — 可配置间隔（60秒 – 24小时） | `900s` |
| ✅ | **`/heartbeat set`** — 为当前对话开启心跳 | — |
| ✅ | **`/heartbeat unset`** — 为当前对话关闭心跳 | — |
| ✅ | **`/heartbeat list`** — 查看所有已配置会话及状态 | — |
| ✅ | **`/heartbeat config [key] [value]`** — 查看/设置当前会话参数 | — |
| ✅ | **`/heartbeat`** — 在当前会话立即触发一次唤醒 | — |
| ✅ | **`/heartbeat stats`** — 查看唤醒统计（次数、跳过、最后错误） | — |
| ✅ | **`/heartbeat stats clear`** — 重置当前会话的统计 | — |
| ✅ | **`/heartbeat test`** — 干运行，检查所有配置条件但不实际触发 | — |
| ✅ | **`/heartbeat pause [时长]`** — 临时暂停心跳（`30m`、`2h`、或以秒为单位） | `1h` |
| ✅ | **`/heartbeat resume`** — 恢复暂停的心跳 | — |
| ✅ | **空闲自动暂停** — 长时间不发言时自动静默（可选） | `off` |
| ✅ | **活跃时间段** — 只在设定时段内唤醒（可选） | `off` |
| ✅ | **UTC 时区偏移** — 为活跃时间段配置你的时区 | `+8` |
| ✅ | **多 prompt 轮换** — 配置多个 prompt 文件，每次随机选取 | — |
| ✅ | **Jitter 随机偏移** — 间隔随机偏移（±10%等），避免可预测的节拍 | `0%` |
| ✅ | **持久化统计** — 唤醒/跳过次数在 gateway 重启后保留 | — |
| ✅ | **`[SILENT]`** — prompt 中返回该标记可静默跳过本轮 | — |
| ✅ | **配置热更新** — 修改间隔/prompt 无需重启 gateway | — |
| ✅ | **结构化日志** — 每次唤醒都记录在 `gateway.log` | — |
| ✅ | **优雅关闭** — 会话结束时自动取消所有心跳任务 | — |
| ✅ | **多平台** — 动态适配任何平台适配器（Telegram、Discord 等） | — |

---

## 安装

### 前置条件

- Hermes Agent (v0.2.0+)
- 已连接任一消息平台（Telegram、Discord、微信等）

### 安装步骤

```bash
# 克隆到插件目录
git clone https://github.com/Love-AronaPlana/hermes-agent-heartbeat.git \
  ~/.hermes/plugins/agent-heartbeat

# 启用插件
hermes config set plugins.enabled '["agent-heartbeat"]'

# 或者在 config.yaml 中：
# plugins:
#   enabled:
#     - agent-heartbeat
```

### 重启 gateway

```bash
# 用户服务（最常见）
systemctl --user restart hermes-gateway

# 或通过 Hermes
hermes gateway restart
```

---

## 配置

配置分两层：

1. **全局设置**：`~/.hermes/config.yaml` 的 `agent_heartbeat:` 下 — 主开关和新会话的默认值。
2. **每个会话的设置**：`~/.hermes/heartbeat/sessions.json` — 通过 `/heartbeat set` / `/heartbeat config` 命令管理（无需手动编辑）。

### 全局配置（config.yaml）

```yaml
agent_heartbeat:
  # 必填：主开关（任何会话要唤醒都必须为 true）
  enabled: true

  # 通过 /heartbeat set 新建会话时应用的默认值
  default_interval: 900                   # 60-86400 秒
  default_prompt_file: ~/.hermes/heartbeat/HEARTBEAT.md

  # 可选附加功能
  jitter: 0.1                             # ±10% 间隔随机偏移
  default_prompt_files:                    # 多 prompt 轮换（随机选取）
    - ~/.hermes/heartbeat/morning.md
    - ~/.hermes/heartbeat/evening.md
```

### 每会话配置（斜杠命令）

```bash
# 在任意对话中：
/heartbeat set                    # 为【当前】对话开启心跳
/heartbeat config                 # 查看当前会话设置
/heartbeat config interval 1800   # 修改当前会话间隔（秒）
/heartbeat config prompt_file ~/.hermes/heartbeat/HEARTBEAT.md
/heartbeat config prompt_files file1.md,file2.md  # 逗号分隔列表
/heartbeat config active_start 08:00
/heartbeat config active_end 22:00
/heartbeat config utc_offset +8
/heartbeat config idle_auto_pause_enabled true
/heartbeat config idle_auto_pause_minutes 120
/heartbeat unset                  # 为【当前】对话关闭心跳
```

每个会话的设置存储在 `~/.hermes/heartbeat/sessions.json`：

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

会话键格式为 `platform:chat_id:thread_id` — 例如 `telegram:123456789:` 表示私聊，`telegram:-1001234567890:17585` 表示话题线程。

### CLI 快速配置

```bash
# 主开关
hermes config set agent_heartbeat.enabled true
hermes config set agent_heartbeat.default_interval 1800
hermes config set agent_heartbeat.default_prompt_file "~/.hermes/heartbeat/HEARTBEAT.md"
```

---

## 使用方法

### 快速开始

```bash
# 1. 开启主开关（只做一次）
hermes config set agent_heartbeat.enabled true

# 2. 在你想开启心跳的对话中：
/heartbeat set
```

### 斜杠命令

| 命令 | 作用 |
|------|------|
| `/heartbeat` | 在当前会话立即触发一次唤醒 |
| `/heartbeat set` | 为当前对话开启心跳 |
| `/heartbeat unset` | 为当前对话关闭心跳 |
| `/heartbeat list` | 查看所有已配置会话及其状态 |
| `/heartbeat config` | 查看当前会话的设置 |
| `/heartbeat config <key> <value>` | 修改设置 |
| `/heartbeat stats` | 查看唤醒统计 |
| `/heartbeat stats clear` | 重置当前会话的统计 |
| `/heartbeat test` | 干运行：检查所有配置条件但不实际触发 |
| `/heartbeat pause 30m` | 临时暂停（30m、2h、或以秒为单位） |
| `/heartbeat resume` | 恢复暂停的心跳 |

### 唤醒词文件

创建一个唤醒词文件（如 `~/.hermes/heartbeat/HEARTBEAT.md`）：

```markdown
[Heartbeat 唤醒]

检查当前市场状态。如果有值得汇报的内容，简洁地总结。
如果什么都没有变化，返回 [SILENT] 保持安静。
```

- 唤醒词文件**每个周期都会重新读取**——实时修改，无需重启。
- 在回复首行返回 `[SILENT]` 可静默跳过本轮。

### 多 prompt 轮换

配置多个 prompt 文件增加多样性：

```yaml
# config.yaml
agent_heartbeat:
  default_prompt_files:
    - ~/.hermes/heartbeat/market.md
    - ~/.hermes/heartbeat/system.md
    - ~/.hermes/heartbeat/greeting.md
```

每个周期随机选取一个。如果所有文件都缺失，依次回退到 `prompt_file` → 内联 `prompt`。

### Jitter 随机偏移

给间隔添加随机偏移，避免可预测的节拍：

```yaml
agent_heartbeat:
  jitter: 0.1   # ±10% 随机偏移（范围：0.0 - 0.5）
```

### 手动触发

在任意激活了 heartbeat 的会话中发送：

```
/heartbeat
```

仅在当前会话触发一次唤醒，不干扰其他会话。

### 空闲自动暂停

启用后，插件会记录你的最后一条**用户**消息。如果超过 `idle_auto_pause_minutes` 没有任何消息，heartbeat 自动静默。你下次发送消息后自动恢复。

```bash
/heartbeat config idle_auto_pause_enabled true
/heartbeat config idle_auto_pause_minutes 120
```

> **注意：** 空闲检测只统计用户主动发送的消息——心跳唤醒的 agent 回复不会重置空闲计时器。

### 活跃时间段

配置后，heartbeat 只在 `active_start` 到 `active_end` 之间唤醒。支持跨午夜时间段（如 22:00-02:00）。通过 `utc_offset` 配置你的本地时区。

```bash
/heartbeat config active_start 08:00
/heartbeat config active_end 02:00
/heartbeat config utc_offset +8
```

### 暂停/恢复

临时暂停心跳，不删除会话配置：

```bash
/heartbeat pause          # 暂停 1 小时（默认）
/heartbeat pause 30m      # 暂停 30 分钟
/heartbeat pause 2h       # 暂停 2 小时
/heartbeat pause 7200     # 暂停 7200 秒
/heartbeat resume         # 立即恢复
```

### 统计

查看唤醒统计：

```bash
/heartbeat stats
# > Heartbeat Stats for telegram/6211819157/DM:
# >   Status: 🟢 Active
# >   Total wakeups: 47
# >   Total skipped: 3
# >   Last wakeup: 12m ago
# >   Last error: (none)

/heartbeat stats clear    # 重置计数器
```

### 测试（干运行）

检查所有条件但不实际触发：

```bash
/heartbeat test
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

## 工作原理

```
用户发送消息
       │
       ▼
pre_gateway_dispatch 钩子  ──►  启动 asyncio 循环（带锁）
       │                              │
       │                              ▼
  (正常分发)                   每个间隔周期：
                                  1. 读取配置（热更新）
                                  2. 检查时间段
                                  3. 检查空闲暂停（仅用户消息）
                                  4. 检查手动暂停
                                  5. 读取唤醒词文件（热更新）
                                  6. deliver_wake() → 同一会话
                                  7. 记录统计
                                  8. 用户看到 agent 回复
                                  9. 用户可回复（上下文保留）
```

插件注册了：
- **2 个钩子**：`pre_gateway_dispatch` — 绑定会话，`on_session_end` — 优雅关闭
- **1 个斜杠命令**：`/heartbeat` — 手动触发及所有子命令

---

## 开发

```bash
# 克隆并测试
git clone https://github.com/Love-AronaPlana/hermes-agent-heartbeat.git
cd hermes-agent-heartbeat

# 运行单元测试
python3 -m pytest tests/ -v
```

---

## 兼容性

| 平台 | 状态 |
|------|------|
| Telegram | ✅ 已验证 |
| Discord  | ✅ 受支持（动态适配器） |
| 微信     | ✅ 受支持（动态适配器） |
| 飞书     | ✅ 受支持（动态适配器） |

插件动态查找每个平台的适配器——不再硬编码 Telegram 依赖。

---

## 许可证

MIT © Love-AronaPlana

---

## 相关链接

- [Hermes Agent](https://hermes-agent.nousresearch.com)
- [Hermes 插件 API](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins)