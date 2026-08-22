# Agent Loop：从 Task 到 Authoritative Result

## 读完你应该理解什么

- `run_agent`、`_run_agent_runtime` 和 phase helpers 的真实调用关系。
- Provider decision 如何经过 Authority、execution、Observation 和 completion gates。
- retry、verification、reconciliation 和 Result Binding 在循环中的位置。

主实现位于 [`mini_harness_core/agent.py`](../mini_harness_core/agent.py)。本页描述 main Agent；
Subagent 使用独立的 `run_subagent` / `_run_subagent_once` 路径，并受 attenuated authority 限制。

## 顶层调用链

```text
run_agent(...)
  |
  v
_run_agent_runtime(...)
  |
  +--> _bootstrap_agent_runtime(...)
  |
  +--> _initialize_runtime_execution(runtime)
  |
  +--> for each Harness step
  |      |
  |      +--> scheduling_allowed / governance entry gate
  |      +--> _prepare_turn(...)
  |      |      +--> RuntimeContextAssembler.prepare_request(...)
  |      |      +--> provider.complete(messages)
  |      |      +--> Audit + Envelope decision binding
  |      |
  |      +--> _handle_memory_candidate(...)
  |      +--> _handle_final_candidate(...)
  |      +--> _handle_mcp_decision(...)
  |      +--> _handle_shell_decision(...)
  |
  +--> _emit_runtime_result(...) on terminal boundary
```

`run_agent` 是兼容 public API；它只转交参数。`_run_agent_runtime` 才拥有 loop，具体 phase 逻辑由
helpers 承担。

## Phase 1：bootstrap

`_bootstrap_agent_runtime` 创建 `_AgentRuntimeState`，并 wiring：

- messages、Verification state、Plan/retry/checkpoint/run-control/governance state；
- current Policy binding；
- Audit、Evidence、Artifact、Output Contract、Result stores；
- Run Manifest 和 Run Envelope；
- MCP registry、context assembler、fault injector。

当配置了 Audit 时，bootstrap 先发布 Policy Snapshot、Manifest 和 Envelope，再记录 `run_started`。
这些对象记录 safe identity，不复制 task、project instructions、Memory 或 raw Observation 正文。

**Review 时重点看什么：** bootstrap 是否把 current Policy、历史对象和可变 runtime state 分开，恢复时是否误把
historical object 当成当前 Authority。

## Phase 2：entry recovery 与 Plan step selection

`_initialize_runtime_execution` 首先判断 run control 和 governance。paused、cancelled、expired 或 budget
exhausted 的 run 不再安排 normal work。唯一窄例外是一个已经存在的 unknown side effect，它可能需要
一次 safety reconciliation 才能诚实描述结果。

如果存在 `current_action_checkpoint`：

- `prepared` → 需要 fresh Approval；
- `executing` → `recover_action_checkpoint` 转为 `unknown`；
- `succeeded` → 可继续使用其已持久化 safe Observation；
- `unknown` + non-safe replay policy → reconciliation-or-block。

任何 resume checkpoint 都令 `requires_fresh_grounding=True`。Session continuity 不能替代 Current
Reality。

如果存在 Plan，函数验证 schema，选择已有 `in_progress` step，或通过
`planning.select_ready_step`/`start_step` 启动一个 ready step。Plan selection 也可写入 Envelope 的纯
`planning` transition。

**Review 时重点看什么：** resume 是否强制 fresh grounding；`prepared`、`executing`、`unknown` 是否沿不同
恢复分支处理，而不是统一重放。

## Phase 3：turn preparation 与 Provider boundary

`_prepare_turn` 调用 `_complete`。对于带 `SYSTEM_PROMPT` 的 Provider，
`RuntimeContextAssembler.prepare_request` 会重新装配 Working Context：当前 Task、Session history、
active Plan/control/recovery state、当前 project context、选中的 Memory 和 safe Observations。

如果启用 Envelope，发送前 context 先由 `RunEnvelopeStore.append_request` 记录 digest identity；
Provider decision 返回后再通过 `bind_decision` 绑定。这个 historical binding 只证明“当时返回了什么”，
不授予 Tool Authority。

`RealProvider` 在 [`providers.py`](../mini_harness_core/providers.py) 中把 HTTP response 解析成统一
decision；protocol repair retry 发生在 Provider adapter 内，不算新的 Harness step，也不执行 Tool。

**Review 时重点看什么：** Provider 输入是否只包含投影后的 Working Context；decision binding 是否仅记录
历史身份，而没有成为 Tool execution credential。

## Phase 4：decision routing

循环支持三类主决定：

- `memory_candidate` → `_handle_memory_candidate`，通过独立 Memory Policy/Approval。
- `final_answer` → `_handle_final_candidate`。
- `tool_call` → MCP reference 进入 `_handle_mcp_decision`，其余进入 `_handle_shell_decision`。

无效 shell decision 会形成 terminal failure，而不是猜测模型意图。

**Review 时重点看什么：** 每一种 decision 是否只能进入一个明确 handler；未知或无效结构是否 fail closed。

## Shell execution pipeline

```text
model tool_call
  -> scheduling entry gate
  -> classify_shell(command, current Policy Snapshot)
  -> build_action_correlation_facts(recovered checkpoint, exact arguments)
  -> persistence / verification / recovery gates
  -> if ASK: persist prepared checkpoint, then request fresh Approval
  -> re-check run-control / deadline after Approval
  -> consume normal action budget and start retry attempt
  -> _dispatch_shell_action
       -> authorize_action
       -> dispatch_authorized_action
            -> persist prepared
            -> persist executing
            -> invoke execute_shell once for this dispatch call
            -> persist succeeded / failed / unknown
            -> Audit safe outcome
  -> finish_or_decide_retry on the dispatched Observation
       -> safe explicit failure: Retry Policy / possible new attempt
       -> unknown side effect: record reconciliation-before-retry; do not replay
  -> on final success: update Verification state
  -> _process_observation (safe projection only)
  -> if recovering unknown action: fresh reconciliation
  -> Evidence / Artifact / Plan updates
```

“once for this dispatch call” 只描述该 seam 的一次调用；它不表示 global action-id idempotency。若历史
side effect 为 `unknown`，recovery path 的保证是先 Reconciliation、不得 blind replay 原 action。

`classify_shell` 同时返回当前 action decision 和 Effect，后续 gates 把它们作为正交维度使用。例如静态
composition 可把 `pwd` 的 disposition 从 `ALLOW` 收紧成 `ASK`，但它仍是 `read_only`；ASK 决定是否需要
Approval，Effect 决定 durability 与 post-action Verification。当前有限 shell classifier 的实现细节是：
初始 `ALLOW` 映射为 `read_only`，初始 `ASK`/`DENY` 映射为 `side_effecting`；静态 composition 随后可以
收紧 disposition，但不会借此改写已经分类的 Effect。

ASK action 在请求 Approval 之前先保存 `prepared` checkpoint。Approval 期间若用户输入 `pause` 或
`cancel`，action 不执行；run control 在可靠边界 settle。

### 为什么同一个 gate 会检查不止一次

Shell/action 路径会在 action preparation/entry、Approval 后、authorization seam 和 dispatch 前，从各自
owner 重新确认关键条件。这是 defense-in-depth 和 stale-state protection，不表示前一次判断可以永久缓存。

特别是 Approval 会暂停执行并等待外部输入；等待期间 Run Control 可能收到 pause/cancel，Deadline 或
budget 可能到期，恢复出来的 checkpoint/Authority reality 也可能已经变化。因此：

```text
Approved earlier
  != Authorized forever

fresh Approval
  + current runtime gates
  + exact prepared checkpoint binding
  + protected-path check
  -> AuthorizedAction
  -> dispatch
```

handler 在 Approval 后重新调用 `scheduling_allowed`；`authorize_action` 再检查 exact checkpoint、传入的
Policy/Approval facts 与 protected path；`dispatch_authorized_action` 最后只接受带私有 seal、且仍与
`prepared` checkpoint 精确匹配的对象。任何一步失败都不能进入 executor。

**Review 时重点看什么：** 从 `_handle_shell_decision` 跟到 `_dispatch_shell_action`、`authorize_action` 和
`dispatch_authorized_action`，确认不存在直接调用 `execute_shell` 的旁路，并确认 Approval 后会重检可能变旧的
runtime state。

## MCP execution pipeline

`_handle_mcp_decision` 先通过 `MCPRegistry.resolve` 和 `validate_json_schema` 验证 capability，再从
Harness-local Policy Snapshot 取得 Policy 与 Effect。MCP server description/effect metadata 不会提升
本地 Authority。

之后它执行与 shell 相同的 runtime gates、Approval、checkpoint 和 sealed dispatch。真正 adapter 是
`_dispatch_mcp_action` → `dispatch_authorized_action` → `execute_mcp_tool`。timeout 后的 late completion
只能进入 `LateMCPCompletionJournal` 作为 historical candidate，不能重新激活 run。

**Review 时重点看什么：** schema validation、Harness-local Effect mapping 和 protected-path checks 是否都在
adapter 调用前完成；timeout 后的 late completion 是否只能成为历史记录。

## Observation processing

Tool/MCP 返回 raw Observation 后，`_process_observation` 调用
`persisted_safe_observation`。Session/model continuity 只接收长度、SHA-256、exit code、denial reason 和
少量 allowlisted structured fields。raw stdout/stderr/result 不进入这些边界。

Audit 使用独立的 safe summary；Evidence 使用 Observation identity。它们记录的是历史事实身份，而不是
可复用执行权限。

**Review 时重点看什么：** raw stdout/stderr/result 是否可能从异常、Audit、Evidence 或 model feedback
旁路跨过 secret projection boundary。

## Retry 与 durability

`_handle_retry` 参与处理已经 dispatch 的非成功 Observation；它不假设所有非成功都可 retry：

1. `classify_failure` 得到 transient/permanent/policy/user/unknown。
2. `decide_retry` 同时检查 Effect、checkpoint replay policy、attempt budget 和 run state。
3. explicit failure 可以进入 Retry Policy，但只有明确的 read-only transient failure 进入
   `retry_with_backoff`。
4. unknown side effect 必须返回 `reconcile_before_retry`，不会进入普通 retry loop。

```text
explicit failure
  -> classify failure
  -> retry decision

unknown side effect
  -> reconciliation with fresh read-only Observation
  -> only if proven not_applied
  -> create a new attempt and make a new retry/authorization decision
```

Durability Replay Safety 高于普通 Retry：只要外部副作用是否发生仍未知，attempt budget 尚有余额也不能重放
原 action。`reconcile_before_retry` 是 retry helper/phase 作出的安全决定，不是“立即 retry”的同义词。

attempt 用新的 checkpoint 和新的 action id。ASK retry 还必须取得新的 Approval。retry exhausted 时 Plan
请求 replan/block；无 Plan 的 final candidate 也会被 authoritative Result 压为 blocked。

**Review 时重点看什么：** 普通 retry loop 是否只接收已证明可安全重试的结果；unknown side effect 是否存在
绕过 reconciliation 的路径；新 attempt 是否取得新 action id 和 fresh Approval。

## Verification 与 Artifact

成功的 side-effecting action 设置 `requires_verification=True`。模型如果立即提交 `final_answer`，
`_handle_final_candidate` 返回结构化 verification feedback，而不是接受完成声明。

下一次 action 必须 read-only；若已识别 target，还必须由 `is_related_verification` 判定相关。成功后：

- `replay_verification_transition` 产生确定性 acceptance；
- `create_verification_evidence` 和 `create_tool_observation_evidence` 建立 provenance；
- 如果 Output Contract 需要该文件，`_finalize_runtime_artifact` 评估并持久化 Artifact；
- accepted evidence 才能支持 environment Plan step completion。

Tool success 因此不等于 Step complete，也不等于 Result completed。

**Review 时重点看什么：** Verification Observation 是否在 action 之后 fresh 取得、是否与 target related，
以及 accepted Evidence 是否真的绑定到对应 action/step。

## Crash reconciliation

resume 后的 unknown side effect 不能匹配为普通 retry。如果模型提出相关 read-only observation，shell
handler 调用 `reconcile_file_observation`：

- fresh content 符合预期 → `applied`；
- fresh `ls/cat` 证明未应用 → `not_applied`，才可能重新打开 retry gate；
- 无法证明 → `uncertain`，Plan/Run blocked。

deadline 或 cancel 后只允许 `safety_reconciliation_decision` 授予的一次 targeted read-only action；
完成 reconciliation 后 run 仍 blocked，不恢复 normal scheduling。

**Review 时重点看什么：** reconciliation 是否严格 read-only、targeted、bounded；`not_applied` 是否来自 fresh
Observation，而不是旧 Session/Evidence 或模型陈述。

## Planning 与 finalization

`_handle_final_candidate` 按以下顺序检查：

1. pending Verification；
2. Output Contract；
3. 当前 Plan step 是否有 accepted fresh Evidence；
4. Harness-owned Result Binding。

`_emit_runtime_result` 调用 `build_authoritative_result_state` 和 `bind_final_result`。最终 status 由 run
control、terminal failure、blocking reason、Plan、Output Contract、Verification、Evidence/Artifact
共同决定。模型的 `claimed_status` 只用于检测 contradiction。

Result transition 写入 Envelope 后，Result store 保存 immutable object，Audit 最后记录 safe answer
identity 和 terminal run state。Bundle/replay 使用这些历史对象，但不会重新调用 Provider、Approval 或
Tool。

**Review 时重点看什么：** Model `final_answer`/`claimed_status` 是否可能覆盖 Harness-owned state；Plan step
completed 是否仍需通过 Output Contract、Verification 和 authoritative Result gates。

## Common Misreadings

- **“Tool success means Step complete。”错误。** side-effecting success 只建立 Verification obligation；
  Step 还需要 accepted fresh Evidence。
- **“Retry handles every non-success。”错误。** unknown side effect 先进入 reconciliation，不能进入普通
  retry loop。
- **“Plan completed means Run completed。”错误。** `_emit_runtime_result` 仍会检查 run control、terminal
  failure、Output Contract、Verification、Evidence 与 Artifact。
- **“Approval grants reusable authority。”错误。** Approval 只对应当前请求；resume/retry 的新 attempt 和
  dispatch 前变化的 runtime state 都必须重新确认。

具体端到端例子见 [`00-overview.md`](00-overview.md)；Authority 细节见
[`03-authority-and-policy.md`](03-authority-and-policy.md)，Action 时序见
[`04-action-lifecycle.md`](04-action-lifecycle.md)。

## Navigation

- Previous: [`01-architecture.md`](01-architecture.md)
- Next: [`03-authority-and-policy.md`](03-authority-and-policy.md)
- Related: [`04-action-lifecycle.md`](04-action-lifecycle.md), [`13-failure-semantics.md`](13-failure-semantics.md)
