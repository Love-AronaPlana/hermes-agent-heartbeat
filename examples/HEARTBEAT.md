[Heartbeat Wakeup]

This is a sample heartbeat prompt. Customize it for your use case.

When the agent receives this, it runs the heartbeat workflow. You can:
- Ask it to check market data, system status, or any recurring task
- Return `[SILENT]` as the first line to skip this cycle silently
- Edit this file live — the plugin re-reads it every cycle

Example:

```markdown
[Heartbeat Wakeup]

Briefly check the current market state. If there's something notable
(crossing a key level, unusual volume), report it concisely.
Otherwise return [SILENT].
```