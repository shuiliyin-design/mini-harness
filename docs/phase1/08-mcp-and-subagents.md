# MCP 与 Subagent：外部能力和委派边界

## 读完你应该理解什么

- MCP discovery metadata、Harness-owned Policy/Effect mapping 和 Tool execution 为什么分层。
- Structured Handoff 如何创建隔离的 Subagent conversation，并使 delegated authority 只能衰减。
- Subagent Return Contract 为什么只是 main Agent 的 candidate input，而不接管 Main Plan。

## Scope / Not Scope

本篇覆盖现有 MCP registry/stdio/fake client、schema validation、timeout projection，以及单层 in-process
Subagent handoff。

本篇不新增 MCP transport，不支持递归 Multi-run orchestration，也不把 Subagent 当 trusted zone。MCP server
和 child Provider 都是可能不可信的决策/Observation 来源。

## 真实模块与关键函数

- [`mcp.py`](../../mini_harness_core/mcp.py)：`MCPRegistry.capability_catalog/resolve/policy_for/effect_for`、
  `validate_json_schema`、`execute_mcp_tool`、`LateMCPCompletionJournal`。
- [`handoff.py`](../../mini_harness_core/handoff.py)：`create_handoff`、`validate_handoff`、`_safe_result`。
- [`authority.py`](../../mini_harness_core/authority.py)：`_effective_subagent_authority`。
- [`policy_composition.py`](../../mini_harness_core/policy_composition.py)：`delegated_ceiling`、
  `compose_subagent_policy`。
- [`agent.py`](../../mini_harness_core/agent.py)：`_handle_mcp_decision`、`_dispatch_mcp_action`、
  `run_subagent`、`_run_subagent_once`。
- [`protected_paths.py`](../../mini_harness_core/protected_paths.py)：`inspect_mcp_paths`、
  `inspect_subagent_paths`。

## 核心状态/数据结构

### MCP capability

Model 看到 compact catalog：reference、description、顶层参数名/type。完整 `inputSchema` 留在
`MCPRegistry._details`，真正调用前由 `resolve` 和 `validate_json_schema` 使用。

每个 capability 的 disposition/Effect 来自 Harness-local configuration 或已绑定 Policy Snapshot：

```text
server tools/list metadata -> discovery hint
Harness local mapping      -> Policy + Effect authority input
```

Effect 只有 `read_only`、`side_effecting`、`unknown`。缺失/非法 local mapping fail closed；server 自称
read-only、trusted 或 ALLOW 都不能提升本地配置。

### Structured Handoff

handoff 只允许：`task`、显式 context/constraints/evidence hints、workspace identity、authority ceiling 和固定
Return Contract。Authority 维度包括 exact allowed tools、workspace write、MCP、max steps。

Return Contract 固定要求：

```text
require_summary=true
require_evidence=true
require_actions=true
```

Subagent result 只有 `status`、`summary`、`evidence`、`actions_taken`，并再次 secret-screen。

## MCP execution lifecycle

```text
tools/list discovery
  -> compact catalog to model
  -> Model Intent: mcp:server:tool + arguments
  -> MCPRegistry.resolve exact reference
  -> validate_json_schema
  -> Harness Policy Snapshot mapping
  -> Harness Effect mapping
  -> runtime gates / Approval / protected paths
  -> AuthorizedAction
  -> execute_mcp_tool
  -> raw untrusted Observation
  -> safe Observation projection
```

timeout 返回 `exit_code=-1`。对于 non-read-only Effect，这意味着 unknown side effect，不是“server 没执行”。
后台 late completion 只能写入 `LateMCPCompletionJournal` 的 safe historical candidate；cancelled/deadline run
不会因迟到成功而重新激活。

## Subagent isolation 与 Main ownership

`_run_subagent_once` 创建全新的 messages，只放一个 structured handoff user message；Main Session/messages 不被
接受或复制。child 拥有独立 max steps、safe observations、local Verification state 和 inner checkpoints。

handoff 中 workspace/evidence 明确标为 hints，冲突时必须用允许的 fresh Tool Observation grounding。child
return 进入 Main Audit/Evidence 时是 `subagent_return` candidate；`evidence_gate` 不把它直接当 Current Reality
acceptance。Main Agent 仍拥有 Main Plan step completion、Output Contract、Artifact acceptance 和
Authoritative Result。

`run_subagent` 本身也通过 `AuthorizedAction(effect=unknown,
replay_policy=never_auto_retry)` dispatch child。丢失 Return Contract 的 crashed Subagent 不会递归恢复或自动重跑。

## Worked Trace：delegated write=false

```text
Main effective profile
  workspace-editor
  can_write_workspace=true
    |
    v
Structured Handoff
  requested profile=workspace-editor
  authority.can_write_workspace=false
    |
    v
compose_subagent_policy
  requested write=true
  AND delegated write=false
  -> effective write=false
    |
    v
Subagent Model Intent
  echo hello > report.md
  effect=side_effecting
  static disposition may be ASK
    |
    v
_run_subagent_once authority check
  write authority not granted
  -> blocked / Final Authorization=DENY
  -> no Human Approval UI
  -> no AuthorizedAction for the shell write
  -> executor call count=0
    |
    v
Return Contract reports blocked candidate to Main
Main Plan remains Main-owned
```

Subagent 当前不继承 main interactive Approval；遇到 `ASK` 会 blocked。更重要的是，write ceiling 已是 false，
Human Approval 即使存在也不能补回被 Delegation 移除的能力。

## Key Invariants

1. MCP description/schema/server metadata 不提供 Authority。
2. MCP Policy 与 Effect 只信任 Harness-local mapping/Policy Snapshot。
3. MCP raw result 必须经过 safe Observation projection。
4. timeout 不证明 Tool 未执行；non-read-only timeout 不可自动 retry。
5. Subagent authority 是 requested、parent 和 delegated ceilings 的交集/min。
6. Main Session 不复制给 Subagent；handoff 是显式最小包。
7. Subagent Return 不直接完成 Main Plan、接受 Artifact 或决定 Result。
8. Subagent crash 缺少 durable Return Contract 时不自动重跑。

## Failure / Edge Cases

- discovery item 缺 name/schema 或 reference 非法：不进入 catalog。
- unknown server/tool、schema mismatch、unknown argument：在 adapter 调用前拒绝。
- MCP timeout：Observation `exit_code=-1`；late completion historical-only。
- MCP result 带 secret：raw value只存在执行边界内，Session/Audit/Evidence/Bundle 接收投影身份。
- handoff 含 secret、非法 path、authority schema 或非全 true Return Contract：validation fail closed。
- child 请求 parent 未授予的 tool/write/MCP/max_steps：只衰减，不提升。
- child 返回 `completed` 但 Main 缺 fresh accepted Evidence：Main Plan/Result 仍不能据此完成 Current Reality 工作。

## Review Anchors

- `MCPRegistry.capability_catalog`：model-visible metadata 是否与 full schema/authority mapping 分离。
- `MCPRegistry.policy_for/effect_for`：是否读取了 server metadata 作为 Authority。
- `_handle_mcp_decision`：schema、Policy、Effect、Approval、checkpoint 与 dispatch 顺序。
- `execute_mcp_tool`：timeout/late completion 是否被误当成可重新调用。
- `_effective_subagent_authority` 与 `compose_subagent_policy`：所有 capability 维度是否单调衰减。
- `_run_subagent_once`：Main messages 是否泄漏、write denial 是否在 executor 前、Return 是否只是 candidate。

## Common Misreadings

- **“MCP description 说 read-only，所以可以 ALLOW。”错误。** description 只是 discovery hint。
- **“Subagent 是内部代码，所以是 trusted zone。”错误。** child intent/return 仍需 Harness gates。
- **“Main 批准过 write，child 自动继承。”错误。** Handoff/parent ceiling 重新衰减。
- **“Subagent completed 就等于 Main Plan completed。”错误。** Main 仍执行 Evidence/Artifact/Result gates。
- **“MCP timeout 等于没有副作用。”错误。** 外部调用可能仍在运行或迟到完成。

## 与其他文档的链接

- Authority composition：[`03-authority-and-policy.md`](03-authority-and-policy.md)
- Action lifecycle：[`04-action-lifecycle.md`](04-action-lifecycle.md)
- Durability：[`06-durability-and-recovery.md`](06-durability-and-recovery.md)
- Session/context isolation：[`07-session-memory-context.md`](07-session-memory-context.md)
- Security：[`12-security-boundaries.md`](12-security-boundaries.md)

## Navigation

- Previous: [`07-session-memory-context.md`](07-session-memory-context.md)
- Next: [`09-audit-and-historical-objects.md`](09-audit-and-historical-objects.md)
- Related: [`03-authority-and-policy.md`](03-authority-and-policy.md), [`15-code-review-guide.md`](15-code-review-guide.md)
