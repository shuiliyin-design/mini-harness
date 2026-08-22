# Failure Semantics：失败、暂停、阻止与未知副作用

## 读完你应该理解什么

- Explicit Failure、Unknown Effect、Denial、Pause/Cancel、Deadline、Persistence/Verification/Contract failure
  为什么不能压成一个 `error`。
- 每种情况是否执行过 Tool、是否可 retry/resume、是否需要 Reconciliation，以及 Result 倾向。
- `blocked`、`failed`、`incomplete`、`cancelled` 的 Authoritative Result precedence。

## Scope / Not Scope

本篇统一术语和决策顺序，但**不统一状态机**。Action、Retry、Run Control、Governance、Verification、Plan 和
Result 各有 owner 与合法状态；把它们合并会丢失“Tool 是否执行过”这类关键事实。

本篇不新增 failure type、compensation workflow 或自动恢复能力。

## 真实模块与关键函数

- [`dispatch.py`](../mini_harness_core/dispatch.py)：`dispatch_authorized_action`、`DispatchOutcome`。
- [`durability.py`](../mini_harness_core/durability.py)：`recover_action_checkpoint`、
  `reconcile_file_observation`。
- [`retry.py`](../mini_harness_core/retry.py)：`classify_failure`、`decide_retry`、`record_failure`、
  `reopen_retry_after_reconciliation`。
- [`run_control.py`](../mini_harness_core/run_control.py)：`request_pause/cancel`、
  `settle_control_boundary`、`resume_run`。
- [`governance.py`](../mini_harness_core/governance.py)：`normal_action_decision`、`deadline_status`、
  `safety_reconciliation_decision`。
- [`verification.py`](../mini_harness_core/verification.py)：`replay_verification_transition`。
- [`artifacts.py`](../mini_harness_core/artifacts.py)：`current_output_contract_gate`。
- [`historical_types.py`](../mini_harness_core/historical_types.py)：`evaluate_result_transition`。
- [`agent.py`](../mini_harness_core/agent.py)：`_handle_shell_decision`、`_handle_mcp_decision`、
  `_handle_final_candidate`、`_emit_runtime_result`。

## 核心状态/数据结构

不要只看一个 `status` 字符串。review 时至少同时观察：

```text
Action checkpoint: prepared/executing/succeeded/failed/unknown
Retry state:       ready/waiting_backoff/executing/reconciling/exhausted/completed/blocked
Run Control:       running/pause_requested/paused/cancel_requested/cancelled
Governance:        deadline + action/subagent/safety counters
Verification:      requires_verification + degraded
Plan:              active/completed/failed/blocked
Result:            completed/blocked/failed/cancelled/incomplete
```

这些状态相互约束，但不是同一个状态机。

### 不要跨层复用同一个词

- `failed`：Authoritative Result/Plan 已有可靠 terminal failure semantics。
- `blocked`：Harness 当前不允许继续 normal work；原因可能是 safety、Policy、Governance 或 Plan，而不是 Tool
  failure。
- `incomplete`：Final Result 所需的 Plan、Output Contract、Evidence 或 candidate gate 尚未满足。
- `cancelled`：User/Run Control 已形成 terminal cancellation；它不是 `failed` 的别名。
- `unknown`：Action checkpoint 对外部 effect 的确定性不足；它不是 Run Result status。

同一个 Run 可以同时有 action=`unknown`、Plan=`blocked`、Result=`blocked`；这不是状态冲突，而是三个 owner
分别描述 effect certainty、工作可继续性与 terminal binding。

## Failure taxonomy 与处理矩阵

| Condition | Tool may have executed? | Retry? | Reconciliation? | Resume? | New side effect? | Terminal Result tendency |
|---|---|---|---|---|---|---|
| Explicit Failure | 是；收到 definite nonzero Observation | read-only transient 且 budget/gates 允许时可创建新 attempt；permanent/exhausted 否 | side-effecting nonzero 不能证明无部分效果，Retry Policy 先返回 `reconcile_before_retry` | 尚未绑定 terminal Result 时可由 Plan/replan 继续 | 仅在 uncertainty 已解除且新 attempt 重新通过全部 gates 后 | retry exhausted/replan limit 常为 `blocked`；明确 terminal failure 或 failed Plan 为 `failed` |
| Unknown Effect | 是或可能已开始；没有可信终态 | **不可 blind retry** | non-read-only 必须；applied→不重放，not_applied→retry gate 才可能重开，uncertain→blocked | durable `executing/unknown` 可进入 recovery lifecycle | uncertainty 存在时只允许 targeted read-only Reconciliation；之后仍需全部 current gates | 通常 `blocked`；cancelled precedence 仍可覆盖 |
| Policy Denial | 否 | 当前 denied attempt 不重试 | 否 | 不是 recovery 问题；非 terminal loop 可提出不同 action | denied action 不允许；不同 action 必须重新分类/授权 | denial Observation 本身不自动 terminal；由 Plan/blocking/completion gates 决定 |
| User Rejection | Approval prompt 前拒绝时否 | 当前 attempt 不重试，旧 Approval 不可复用 | 否 | 非 terminal loop 可继续；若 pause 则必须 explicit resume | 当前 action 不允许；未来 action 需要 fresh Approval/gates | rejection 本身不必 terminal；取决于 owner state 与最终 binding |
| Pause | prompt 前通常否；in-flight action 可能已执行并先 settle | paused 期间否 | 仅已有 unknown effect 才需要 | **是**，`paused -> running`；budget/counter 不重置 | resume 后 fresh grounding、current gates、必要时新 Approval | 当前 invocation 通常 `blocked`；不是 `failed`，control 仍为 paused |
| Cancel | prompt 前通常否；in-flight action 可能先完成到可靠边界 | 否 | 已有 unknown effect 可走 bounded safety Reconciliation | **否**，`cancelled` terminal | 不允许 normal side effect | `cancelled` precedence；safety observation 不改回 completed |
| Deadline | 到期前或 in-flight 时可能已执行 | normal retry 否 | 已有 unknown effect 最多一次 targeted read-only safety Reconciliation | 当前 Run 的 normal work 不恢复 | 不允许 normal side effect | `blocked` |
| Budget Exhaustion | 既往 attempts/actions 可能已执行 | 不允许超过 counter；retry exhaustion 转 replan-or-block | 仅同时存在 unknown effect 时 | resume 不重置 counter | 不允许超预算 normal action | 当前 agent 的 budget/retry-exhaustion stop path 提供 blocking reason，因此为 `blocked` |
| Persistence Failure | pre-tool failure 时否；post-tool failure 时可能已执行 | 不得通过重做 Tool 修复 store | 只有最后 durable checkpoint 无法确定 effect 时需要 | 取决于最后 durable checkpoint；process crash 与 caught exception 分开处理 | caught post-tool failure 的 Degraded gate 阻止新 side effect；crash recovery 先判断/reconcile | pre-tool exception/crash 可能没有 Result；Result 前 degradation 通常绑定 `blocked`，明确 terminal failure 可为 `failed`；Result save 后的 Audit failure不能反写已 durable Result |
| Verification Failure | 原 side effect 已执行；verification read 也可能执行过 | transient read-only verification 可按 Retry Policy 新建 attempt；绝不重放 write | 否，除非原 action 本身为 unknown | 未绑定 terminal Result且 governance 允许时可继续验证/replan | pending Verification obligation 阻止新的 side effect | Evidence gate 未满足为 `incomplete`，或由 Plan/reason 得到 `blocked/failed` |
| Output Contract Unsatisfied | 可能执行过，也可能没有产出 | 不是同一 action retry；需要新的计划工作 | 否，除非另有 unknown action | **final candidate 到达后当前 Run terminalizes；不能在同一 Result 后自动 resume** | 当前 terminalized Run 不再执行；新 lifecycle/Run 必须重新通过 current gates | `incomplete` |

“Result 倾向”不是用 failure label 直接映射 status；最终由 Result Binding 的全部输入决定。

## Failure decision tree

```text
Action request
  |
  +-- gates/Policy/Approval denied before executor?
  |      |
  |      +-- yes -> no Tool execution
  |                 -> denial/pause/cancel/deadline semantics
  |                 -> continue, block, cancel, or replan by owner state
  |
  +-- executor started
         |
         +-- outcome known?
         |      |
         |      +-- success
         |      |    -> terminal checkpoint durable?
         |      |          +-- yes -> Verification/Artifact/Result pipeline
         |      |          +-- no
         |      |               +-- caught persistence failure -> in-memory unknown + Degraded
         |      |               +-- process crash -> resume durable executing as unknown
         |      |               -> Reconciliation for non-read-only effect
         |      |
         |      +-- explicit nonzero failure
         |           -> read_only + transient + budget/gates?
         |                 +-- yes -> new attempt through Retry Policy
         |                 +-- no  -> replan/block
         |           -> side_effecting/unknown?
         |                 +-- reconciliation-before-retry
         |
         +-- timeout / outcome unknown
                -> never infer "not executed"
                -> unknown checkpoint
                -> targeted read-only Reconciliation
                     +-- applied     -> no replay
                     +-- not_applied -> retry gate may reopen
                     +-- uncertain   -> blocked

After every branch:
  Run Control + Governance + Verification + Plan + Output Contract
  -> Authoritative Result Binding
```

## Worked Trace：post-tool persistence failure

```text
side-effecting Tool
  -> executing checkpoint durable
  -> Tool returns success; external effect happened
  -> terminal checkpoint persistence raises OSError
  -> dispatch keeps raw success Observation
  -> in-memory checkpoint fallback=unknown
     (fallback persistence can also fail; durable store may still say executing)
  -> DispatchOutcome.degraded=true
  -> runtime marks Degraded
  -> new side effects blocked
  -> original Tool is not retried to repair Session/Audit/Evidence
  -> resume performs Reconciliation if supported
  -> Authoritative Result remains blocked/incomplete until gates are satisfied
```

这个 trace 展示 `Failure ≠ Crash`：Tool 没有失败，失败的是 post-tool persistence；如果把它改写成 Tool failure，
普通 retry 就可能制造重复副作用。

如果同一位置触发的是 `InjectedFault`，进程直接中断，不会执行上述 Degraded handler。恢复者读取最后 durable
`executing`，再由 `recover_action_checkpoint` 推导 `unknown`。

## Output Contract terminalization

`_handle_final_candidate` 收到 Model final candidate 后，会调用 `current_output_contract_gate`。若 Output
Contract 不满足，当前实现立即执行：

```text
Output Contract unsatisfied + Model final candidate
  -> _emit_runtime_result
  -> Authoritative Result=incomplete
  -> current Run terminalizes
```

当前 Agent Loop 不会保持这个 Run 活着、等待未来自动补 Artifact，也不会在已保存 immutable Result 后自动
续跑。若仍要完成任务，需要新的执行生命周期/Run，重新读取 Current Reality，并重新通过当前 Policy、
Governance、Approval、Verification 与 Output Contract gates。

## Result status precedence

`evaluate_result_transition` 的主要 precedence 是：

```text
cancel_requested/cancelled -> cancelled
terminal failure or failed Plan -> failed
blocking reason or blocked Plan -> blocked
non-completed Plan -> incomplete
unsatisfied Output Contract -> incomplete
required Evidence unsatisfied -> incomplete
missing final candidate -> incomplete
otherwise -> completed
```

因此 `blocked != failed`：blocked 表示 Harness 不能安全/合法继续，但不一定存在 terminal execution failure。
`incomplete` 表示 required completion gates 尚未满足，不一定存在错误。`unknown` 不出现在 Result status 中；
它必须先由 recovery semantics 转换为 blocked/reconciled action facts，再进入 Result Binding。

## Key Invariants

1. Failure 不等于 Crash；store failure 不得伪造 Tool failure。
2. Pause 不等于 Failure；Cancel 不等于 Failure。
3. timeout 不等于“没有执行”。
4. blocked 不等于 failed；incomplete 也不等于 failed。
5. unknown side effect 的 Reconciliation precedence 高于 Retry。
6. retry/resume 不重置 attempt、action 或 deadline budget。
7. DENY/User rejection 不进入 executor，也不自动转成可重试 failure。
8. post-tool persistence failure 不允许通过重做副作用修复后续 store。
9. Model final answer/claimed status 不覆盖 Authoritative Result。
10. deadline/cancel 后的 safety Reconciliation 不恢复 normal work。

## Failure / Edge Cases

- 同一个 `exit_code=-1`：read-only 可按 transient retry；side-effecting 必须视为 unknown/reconcile。
- side-effect command 返回 nonzero：不能仅凭 nonzero 推断未产生部分效果。
- pause/cancel 在 Tool 运行中到达：cooperative semantics 要求当前 action 先写 truthful terminal checkpoint。
- Approval waiting 可冻结 governance active time，但不能重置 budget；返回后仍重检 run state。
- Plan 已 completed 但 Output Contract drift：Result 仍 `incomplete`。
- Output Contract 在 final candidate 时不满足：当前 Run 立即 terminalize；修复需要新执行生命周期，而不是当前
  loop 自动等待。
- Policy/User denial 后模型直接说 completed：若没有 Plan/Contract 等 terminal gate，denial Observation 本身并非
  自动 terminal blocker；review 不应假装当前实现覆盖更强语义。
- `ResultStore.save` 失败：不能重做 Provider/Tool；调用路径返回 safe terminal degradation。
- Result 已 durable 后 `final_result_emitted`/`run_state_changed` Audit append 失败：标记 live runtime Degraded，
  但不能重新绑定或改写已保存 Result。

## Review Anchors

- `classify_failure` 与 `decide_retry`：Effect/durability 是否在 transient convenience 前检查。
- `dispatch_authorized_action`：pre-tool 与 post-tool persistence exception 是否分流。
- `settle_control_boundary`：in-flight pause/cancel 是否在可靠边界 settle。
- `normal_action_decision`/`safety_reconciliation_decision`：deadline 后 normal 与 safety mode 是否分离。
- `_handle_final_candidate`：Verification/Output Contract/Plan gates 是否可能被 Model candidate 跳过。
- `evaluate_result_transition`：status precedence 与 contradiction 是否保持 deterministic/replay-safe。

## Common Misreadings

- **“Failure 和 Crash 都走 retry。”错误。** crash/unknown 先 Reconciliation。
- **“Pause 是失败。”错误。** 它是 cooperative user control，可 resume。
- **“Cancel 是失败。”错误。** Result 有独立 `cancelled` status，且不可 resume。
- **“Timeout 表示 Tool 没执行。”错误。** 只表示未及时获得可信终态。
- **“Blocked 就是 Failed。”错误。** blocked 是安全/治理停机；failed 是 terminal failure precedence。
- **“Output Contract unsatisfied 应标 failed。”错误。** 当前 Result semantics 是 `incomplete`。
- **“Output Contract unsatisfied 后当前 Run 会自动继续补 Artifact。”错误。** final candidate 路径已经绑定
  terminal `incomplete` Result。
- **“unknown 是一种 Result status。”错误。** 它只描述 Action effect certainty。

## Deep Review Questions

1. 相同的 `exit_code=-1` 为什么对 read-only 与 side-effecting action 导致不同 Retry/Recovery 决策？
2. Pause 和 Cancel 在 Result status、resume eligibility、in-flight action settling 上有什么不同？
3. terminal checkpoint `OSError` 与 `InjectedFault` crash 各会留下什么 durable state 和内存状态？
4. Model 提交 final candidate 时 Output Contract 不满足，当前调用是否还会继续 Agent Loop？
5. 哪些 Result Binding 输入分别使结果成为 `failed`、`blocked` 或 `incomplete`？

## 与其他文档的链接

- Retry/Governance：[`05-planning-retry-governance.md`](05-planning-retry-governance.md)
- Durability：[`06-durability-and-recovery.md`](06-durability-and-recovery.md)
- Evidence/Result：[`10-evidence-artifact-result.md`](10-evidence-artifact-result.md)
- Security：[`12-security-boundaries.md`](12-security-boundaries.md)
- Tests：[`14-testing-strategy.md`](14-testing-strategy.md)

## Navigation

- Previous: [`12-security-boundaries.md`](12-security-boundaries.md)
- Next: [`14-testing-strategy.md`](14-testing-strategy.md)
- Related: [`06-durability-and-recovery.md`](06-durability-and-recovery.md), [`17-glossary-and-state-reference.md`](17-glossary-and-state-reference.md)
