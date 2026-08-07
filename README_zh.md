# Hermes Agent Heartbeat 插件

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

为 [Hermes Agent](https://hermes-agent.nousresearch.com) 打造的 **心跳（Heartbeat）** 插件。  
它会在 **同一个会话** 中周期性唤醒 agent，完整保留上下文——你可以在两次唤醒之间随时讨论、下指令、调整方向。

> **告别那些忘记你说过什么的孤立 cron 任务。**  
> 心跳共享同一个会话——每次唤醒都记得完整的对话历史。

---

## 功能特性

| # | 功能 | 默认值 |
|---|------|--------|
| ✅ | **周期唤醒** — 可配置间隔（60秒 – 24小时） | `900s` |
| 🆕 | **`/heartbeat`** — 斜杠命令，立即手动触发一次唤醒 | — |
| 🆕 | **空闲自动暂停** — 你长时间不发言时自动静默（可选） | `off` |
| 🆕 | **活跃时间段** — 只在设定时段内唤醒（可选） | `off` |
| 🆕 | **UTC 时区偏移** — 为活跃时间段配置你的时区 | `+8` |
| ✅ | **`[SILENT]`** — prompt 中返回该标记可静默跳过本轮 | — |
| ✅ | **配置热更新** — 修改间隔/prompt 无需重启 gateway | — |
| ✅ | **结构化日志** — 每次唤醒都记录在 `gateway.log` | — |

---

## 安装

### 前置条件

- Hermes Agent (v0.2.0+)
- 已连接 Telegram 平台（其他平台开发中）

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

所有设置都在 `~/.hermes/config.yaml` 的 `agent_heartbeat:` 下。

```yaml
agent_heartbeat:
  # 必填
  enabled: true
  platform: telegram
  chat_id: "123456789"

  # 间隔（60-86400 秒）
  interval_seconds: 900

  # 唤醒词来源（文件路径或内联字符串）
  prompt_file: ~/.hermes/heartbeat/HEARTBEAT.md

  # 可选：活跃时间段（默认关闭）
  active_start: "08:00"
  active_end: "22:00"
  utc_offset: "+8"

  # 可选：空闲自动暂停（默认关闭）
  idle_auto_pause_enabled: true
  idle_auto_pause_minutes: 120
```

### CLI 快速配置

```bash
hermes config set agent_heartbeat.enabled true
hermes config set agent_heartbeat.interval_seconds 1800
hermes config set agent_heartbeat.chat_id "123456789"
hermes config set agent_heartbeat.platform telegram
hermes config set agent_heartbeat.prompt_file "~/.hermes/heartbeat/HEARTBEAT.md"

# 可选：活跃时间段
hermes config set agent_heartbeat.active_start "08:00"
hermes config set agent_heartbeat.active_end "02:00"
hermes config set agent_heartbeat.utc_offset "+8"

# 可选：空闲暂停
hermes config set agent_heartbeat.idle_auto_pause_enabled true
hermes config set agent_heartbeat.idle_auto_pause_minutes 120
```

---

## 使用方法

### 唤醒词文件

创建一个唤醒词文件（如 `~/.hermes/heartbeat/HEARTBEAT.md`）：

```markdown
[Heartbeat 唤醒]

检查当前市场状态。如果有值得汇报的内容，简洁地总结。
如果什么都没有变化，返回 [SILENT] 保持安静。
```

- 唤醒词文件**每个周期都会重新读取**——实时修改，无需重启。
- 在 prompt 首行返回 `[SILENT]` 可静默跳过本轮。

### 手动触发

在任意激活了 heartbeat 的会话中发送：

```
/heartbeat
```

立即触发一次唤醒，无需等待间隔时间。

### 空闲自动暂停

启用后，插件会记录你的最后一条消息。如果超过 `idle_auto_pause_minutes` 没有任何消息，heartbeat 自动静默。你下次发送消息后自动恢复。

### 活跃时间段

配置后，heartbeat 只在 `active_start` 到 `active_end` 之间唤醒。支持跨午夜时间段（如 22:00-02:00）。通过 `utc_offset` 配置你的本地时区。

---

## 工作原理

```
用户发送消息
       │
       ▼
pre_gateway_dispatch 钩子  ──►  启动 asyncio 循环
       │                              │
       │                              ▼
  (正常分发)                   每个间隔周期：
                                  1. 读取配置
                                  2. 检查时间段
                                  3. 检查空闲暂停
                                  4. 读取唤醒词文件
                                  5. deliver_wake() → 同一会话
                                  6. 用户看到 agent 回复
                                  7. 用户可回复（上下文保留）
```

插件注册了：
- **1 个钩子**：`pre_gateway_dispatch` — 首条消息时绑定会话
- **1 个斜杠命令**：`/heartbeat` — 立即手动触发

---

## 开发

```bash
# 克隆并测试
git clone https://github.com/Love-AronaPlana/hermes-agent-heartbeat.git
cd hermes-agent-heartbeat

# 运行单元测试
python3 -m pytest tests/ -v

# 插件在 Hermes gateway 中实时测试。欢迎贡献！
```

---

## 兼容性

| 平台 | 状态 |
|------|------|
| Telegram | ✅ 已测试 |
| Discord | 🚧 计划中 |
| 微信 | 🚧 计划中 |

---

## 许可证

MIT © Love-AronaPlana

---

## 相关链接

- [Hermes Agent](https://hermes-agent.nousresearch.com)
- [Hermes 插件 API](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins)