# Session、Memory 与 Working Context

## 读完你应该理解什么

- Session、长期 Memory、Provider Working Context 为什么是三个不同的数据面。
- resume 如何保留 continuity，同时要求 Current Reality 重新 grounding。
- `RuntimeContextAssembler` 如何在 transport 前组装、投影和压缩消息。

## Scope / Not Scope

本篇覆盖 `SessionStore` schema/migration、Memory 生命周期、Observation projection、context budget 和 active
control/Plan injection。

本篇不把 Session 或 Memory 当数据库真相，不描述模型 tokenizer 的精确 token 数，也不允许 project
instructions、Skill、Memory 或旧 Observation 提升 Authority。

## 真实模块与关键函数

- [`session.py`](../mini_harness_core/session.py)：`SessionStore.create/load/save/_validate`。
- [`memory.py`](../mini_harness_core/memory.py)：`screen_memory_content`、`validate_memory_candidate`、
  `MemoryStore`、`select_memories`、`format_memory_context`。
- [`context.py`](../mini_harness_core/context.py)：`project_observations_for_model`、`compact_messages`、
  `RuntimeContextAssembler.assemble/prepare_request`。
- [`observation.py`](../mini_harness_core/observation.py)：`persisted_safe_observation`、
  `model_context_observation`。
- [`agent.py`](../mini_harness_core/agent.py)：`_complete`、`_prepare_turn`、`_process_observation`、
  `_initialize_runtime_execution`。

## 核心状态/数据结构

### Session

当前 `SESSION_VERSION=6`。Session 持有：

- full persisted message history（其中 Tool message 已经过 safe projection）；
- Verification/Degraded state；
- current Plan 与 revision history；
- current action checkpoint、retry state、run control、governance state。

`SessionStore.load` 接受历史 V1–V5 schema，并在内存中补齐后来字段到 V6；升级后的内容在后续 `save` 时以
当前 schema 原子写回。`save` 使用 temporary file、`fsync` 和 `os.replace`，避免半写 JSON 成为合法 Session。

### Memory

Memory record 只有 `preference`、`project_fact`、`workflow` 三种 kind，必须经过 deterministic screening 和
Human Approval，source 固定为 `user_approved`。`forget` 将状态改为 `inactive`；`update` 仍重新 screening。

### Working Context

Working Context 是每次 Provider request 前临时组装的 messages，不等于 Session 文件。它可包含：

- Provider system instructions；
- compact MCP catalog；
- untrusted `AGENTS.md`/Skill；
- selected approved Memory；
- safe Session observations；
- active Plan、Run Control、Retry、Governance、Recovery、Output Contract summaries。

## 三条 truth boundary

```text
Session = continuity, not Runtime truth
Memory  = user-approved long-term hint, not Current Reality
Historical data = recorded past identity, not Fresh Observation
```

resume 时 `_initialize_runtime_execution` 对 recovered checkpoint 设置
`requires_fresh_grounding=True`。旧 Session 可以告诉模型“上次进行到哪里”，但不能证明文件、cwd、MCP server
或外部系统现在仍相同。

## Context assembly 为什么属于 Harness

对于具有字符串 `SYSTEM_PROMPT` 的 Provider，`_complete` 在 transport 前调用
`RuntimeContextAssembler.prepare_request`。assembler 决定：

1. 哪些来源可进入 context；
2. project/Skill/Memory 的 trust label；
3. raw Observation 如何投影；
4. active control facts 必须放回本轮 context；
5. budget 超限时如何 deterministic compact。

Provider 只接收最终 messages 并返回 intent。如果由 Provider 自行装配 context，它就会间接拥有 secret
boundary、Authority labels、resume grounding 与 control-state visibility，这些属于 Harness。

兼容边界：没有字符串 `SYSTEM_PROMPT` 的 custom/Fake Provider 会直接收到调用方 messages；此时不会自动走
完整 assembler。这是测试与 embedding 兼容路径，不应被误写成所有 Provider 都必经 assembly。

## Budget 与 deterministic compaction

`measure_context` 是字符启发式估算，不是真实 tokenizer。`prepare_request` 只在估算超过 budget 时尝试一次
`compact_messages`：

- 保留 system/runtime project context；
- 保留最近六条 messages；
- 保留最新非控制 user task；
- 对较旧内容生成字段受限的 deterministic summary；
- 重新附加 active control state。

compaction 不修改 full Session history；若压缩结果没有更小，则仍发送原 messages；若仍超 budget，也只警告并
发送一次，不递归摘要。

## Worked Trace：resume 后重新 grounding

```text
Run A Session
  messages: write report.md, safe Observation identity
  checkpoint: succeeded
  Plan: active
  -> process exits

resume
  -> SessionStore.load validates/migrates schema
  -> recover checkpoint and continuity
  -> requires_fresh_grounding=true
  -> RuntimeContextAssembler reads current project context
  -> selected Memory is labelled continuity hint only
  -> active_control_state tells model recovery/verification duties
  -> Provider requests fresh read-only cat report.md
  -> Tool returns Fresh Observation
  -> safe projection enters Session/model context
  -> only fresh accepted Evidence may prove Current Reality
```

如果 Session 说 `report.md` 曾存在，而 fresh `cat` 说不存在，Current Reality wins；旧记录仍可作为历史事实保留。

## Key Invariants

1. Full Session History 与 model Working Context 概念分离。
2. raw Tool/MCP output 不进入 Session 或 Provider context。
3. resume continuity 不取消 fresh grounding requirement。
4. Memory 必须先 screening、再 Human Approval；Memory 不能修改 Policy/Approval/Verification。
5. compaction 只构造一次 request view，不删除 Session history。
6. active control state 由 Harness 注入，不能由模型自行降级。
7. Provider 不拥有 context assembly 或 trust classification。

## Failure / Edge Cases

- Session JSON 损坏、ID 与文件名不一致或未知 schema：load fail closed。
- Legacy Session 中可能有 raw-shaped Tool messages：`project_observations_for_model` 再投影，且对已安全数据幂等。
- Memory 疑似 secret、raw stdout/stderr、临时状态或 authority override：在 Approval 前拒绝。
- Memory store 满、写盘失败或用户拒绝：只返回 `memory not saved`，不改变 Tool Authority。
- compacted context 仍超 budget：警告但不递归压缩，也不伪称已满足真实模型限制。
- 当前文件已漂移：Session/Memory 不可用于解除 Verification 或 Output Contract gate。

## Review Anchors

- `SessionStore._validate`：各 schema version 的字段集合是否精确、迁移是否只补安全默认值。
- `SessionStore.save`：atomic replace 前是否 flush/fsync，失败是否清理临时文件。
- `project_observations_for_model`：legacy raw-shaped Observation 是否被重新投影。
- `RuntimeContextAssembler.assemble`：每种 context source 的 trust label 与插入顺序。
- `compact_messages`：是否保留最新 task 和 active control，而不修改输入 history。
- `_complete`：哪些 Provider 走 assembler，Provider 是否可能提前看到 raw Session 数据。

## Common Misreadings

- **“Session 记录了文件，所以文件现在存在。”错误。** 需要 Fresh Observation。
- **“Memory 是经过批准的，所以具有 Authority。”错误。** Approval 只允许保存 hint。
- **“Working Context 就是完整 Session。”错误。** 它是临时、投影、可能 compacted 的 request view。
- **“compaction 会删除历史。”错误。** full Session 不变。
- **“Provider 可以自行挑选可信 context。”错误。** trust/boundary assembly 属于 Harness。

## 与其他文档的链接

- Agent request boundary：[`02-agent-loop.md`](02-agent-loop.md)
- Authority labels：[`03-authority-and-policy.md`](03-authority-and-policy.md)
- Historical objects：[`09-audit-and-historical-objects.md`](09-audit-and-historical-objects.md)
- Evidence freshness：[`10-evidence-artifact-result.md`](10-evidence-artifact-result.md)
- Secret boundary：[`12-security-boundaries.md`](12-security-boundaries.md)

## Navigation

- Previous: [`06-durability-and-recovery.md`](06-durability-and-recovery.md)
- Next: [`08-mcp-and-subagents.md`](08-mcp-and-subagents.md)
- Related: [`02-agent-loop.md`](02-agent-loop.md), [`09-audit-and-historical-objects.md`](09-audit-and-historical-objects.md)
