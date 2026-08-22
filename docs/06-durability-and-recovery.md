# Durability 与 Recovery：崩溃后如何不说谎

## 读完你应该理解什么

- Action checkpoint 五态如何把“准备执行”“可能执行过”和“已有终态”分开。
- 为什么 `executing` 必须在副作用前持久化，以及 Tool success 为什么不等于 terminal checkpoint durable。
- replay-safe recovery、Reconciliation 与 Degraded state 如何分别阻止不安全的后续执行。

## Scope / Not Scope

本篇覆盖单个 action 的持久化顺序、进程崩溃恢复、六个确定性 fault hooks 和当前文件写入的窄
Reconciliation。

本篇不承诺通用 exactly-once delivery，也不处理事务型外部系统、通用 MCP 补偿或跨机器共识。无法机械
确认的 unknown side effect 会 blocked，而不是猜测。

## 真实模块与关键函数

- [`durability.py`](../mini_harness_core/durability.py)：`create_action_checkpoint`、
  `transition_action_checkpoint`、`recover_action_checkpoint`、`build_action_correlation_facts`、
  `reconcile_file_observation`。
- [`dispatch.py`](../mini_harness_core/dispatch.py)：`authorize_action`、`dispatch_authorized_action`。
- [`fault_injection.py`](../mini_harness_core/fault_injection.py)：`FAULT_POINTS`、
  `DeterministicFaultInjector`、`trigger_fault`。
- [`agent.py`](../mini_harness_core/agent.py)：`_dispatch_shell_action`、`_persist_runtime_evidence`、
  `_finalize_runtime_artifact`、`_emit_runtime_result`。
- 关键测试：[`test_v26_failure_semantics.py`](../tests/security/test_v26_failure_semantics.py)、
  [`test_v26_boundary.py`](../tests/security/test_v26_boundary.py)、
  [`test_end_to_end_runtime.py`](../tests/e2e/test_end_to_end_runtime.py)。

## 核心状态/数据结构

`ACTION_STATES` 只有五个值：

```text
prepared -> executing -> succeeded
                      -> failed
                      -> unknown

unknown  -> succeeded   # fresh reconciliation proves applied
         -> failed      # fresh reconciliation proves not_applied
```

| State | 能证明什么 | 不能证明什么 |
|---|---|---|
| `prepared` | intent 已固定，但 executor 尚未开始 | Approval 仍有效、Tool 已执行 |
| `executing` | durable record 已表明 dispatch 即将或已经开始 | 副作用成功、失败或未发生 |
| `succeeded` | terminal safe Observation 已持久化为成功 | 当前现实仍与当时相同 |
| `failed` | terminal safe Observation 已持久化为失败，或 Reconciliation 证明未应用 | side-effecting nonzero 天然等于“无副作用” |
| `unknown` | 外部效果不确定 | 可以安全重放 |

checkpoint 还绑定 `action_id`、exact `tool`/`arguments`、Effect、Plan/step identity、Observation identity 和
`replay_policy`。默认 replay policy 是：`read_only -> safe_to_retry`；其他 Effect ->
`requires_reconciliation`。

Degraded 不是第六个 action state。它保存在 Verification/session runtime state 中，表示 Harness 已明确
观察到某个 persistence/observability stage 失败，因而知道自己的记录能力受损。它会限制新的 side effect，
但不能反写 Tool outcome。

Crash window 与 Degraded 必须分开：

- **Crash window**：执行生命周期在某个持久化边界被中断。新进程只能读取最后 durable state，再判断外部
  效果是否确定。
- **Degraded**：仍在运行的 Harness 捕获到明确的 persistence/observability failure，并把受损事实写入
  runtime state，用它阻止不安全的后续执行。

`InjectedFault` 继承 `BaseException`，用于模拟进程在边界上消失；它不会被普通 `Exception` handler 捕获并
自动设置 Degraded。V26 fault hooks 是 deterministic crash-window probes，不表示每个 hook 都等价于
Degraded。

## 持久化顺序与 crash window

`dispatch_authorized_action` 的顺序是安全语义的一部分：

```text
sealed AuthorizedAction + matching prepared checkpoint
  -> persist prepared
  -> persist executing
  -> invoke the selected executor once for this dispatch call
  -> derive succeeded / failed / unknown
  -> persist terminal checkpoint
  -> Audit / Session / Evidence / Artifact / Result
```

如果 `prepared` 或 `executing` 的持久化失败，executor 不启动。副作用一旦返回，被当前进程捕获的后续
存储失败会让 run 进入 Degraded，或让内存中的 checkpoint 退守 `unknown`；进程直接 crash 时则只能由新
进程读取最后 durable state。两种情况都不能把真实 Tool success 改写成普通 Tool failure。

因此：

```text
Tool success != terminal checkpoint durability
```

Tool 可能已经成功，但进程恰好在 terminal checkpoint 落盘前崩溃；恢复只能知道“可能发生过”。

## Crash 与明确 persistence failure 对照

下表的 persistence failure 指普通 `Exception` 被 Harness 捕获；crash 指进程在 fault boundary 被中断。

| Condition | Last durable truth | Recovery behavior | Degraded? | Reconciliation? | Side effect replay? |
|---|---|---|---|---|---|
| crash before terminal checkpoint | action checkpoint=`executing`；Tool 可能已经返回 | resume 经 `recover_action_checkpoint` 得到 `unknown` | crash 本身不会写 Degraded | non-read-only 必须 reconcile-or-block | 禁止盲目重放 |
| Audit persistence failure | terminal checkpoint 已 durable；Audit 事件缺失；若发生在 Result save 后，Result 也可能已 durable | 保留 terminal Observation；Result 前的失败限制后续 binding，Result 后的失败不能反写已绑定 Result | 是，当前进程标记 `audit` stage | 否，除非 checkpoint 本来就是 `unknown` | 否 |
| Session persistence failure | terminal checkpoint 已 durable；Audit 可能已存在；Session projection 落后 | 保留 terminal Tool truth；从已有 durable records 恢复/阻止 | 是，当前进程标记 `session` stage | 通常否 | 否 |
| Evidence persistence failure | action/Session terminal truth 已有；Evidence 缺失 | completion gate 不满足；没有通用自动重建器 | 是，标记 `evidence` stage | 否；这是后处理缺口，不是 effect uncertainty | 否 |
| Artifact persistence failure | terminal truth 与 Evidence 可存在；Artifact 缺失 | Output Contract 不满足；后续 Result 不能假装 completed | 是，标记 `artifact` stage | 否 | 否 |
| Result persistence failure | Artifact/Evidence 和内存中的 binding 可存在；immutable Result 未 durable | 返回 safe degraded outcome；历史 Result 可能 unavailable | 是，标记 `result` stage | 否 | 否 |

如果相同边界由 `InjectedFault` 中断，表中的 Degraded 不会自动写入；恢复者必须从最后 durable object 判断
下一步。只有 effect certainty 丢失时才需要 Reconciliation。Evidence、Artifact 或 Result 缺失不能用重做
副作用来修复。

## 六个 V26 fault injection point

这些 hook 只可由 embedding test/developer 直接传入；不进入 CLI、Session、Envelope、Provider context 或
action arguments。

| Fault point | 最后可依赖的 durable truth | 恢复原则 |
|---|---|---|
| `after_tool_success_before_terminal_checkpoint` | Tool 已返回成功；durable checkpoint 仍为 `executing` | 恢复为 `unknown`，Reconciliation-or-block |
| `after_terminal_checkpoint_before_audit` | terminal checkpoint 已 durable | 继续使用 safe terminal Observation，不重放 Tool |
| `after_audit_before_session` | terminal checkpoint 与 action Audit 已存在；Session 可能落后 | 从 terminal truth 继续或显式阻止；hook 本身不写 Degraded，不重放 |
| `after_session_before_evidence` | Session 已前进；Evidence 尚未保存 | completion evidence 缺失；hook 本身不写 Degraded，不重放 |
| `after_evidence_before_artifact` | Evidence 已保存；Artifact 尚未保存 | Artifact/contract closure 不完整；没有通用自动修复，不重放 |
| `after_artifact_before_result` | Artifact 可能已保存；Result 尚未绑定/保存 | Result history 可能 unavailable；不能通过重做 Tool 补 Result |

`test_v26_failure_semantics.py` 对前三个 dispatch window 核对 durable checkpoint/Audit 次数；“六点
one-shot”测试只证明 hook 可确定触发一次。`test_end_to_end_runtime.py` 的 crash scenario 是
dispatch + manual Reconciliation slice，并没有断言六个点都能自动恢复完整 Evidence/Artifact/Result closure。

## Worked Trace：副作用已发生，terminal checkpoint 前 crash

当前窄例子是 `echo once > once.txt`：

```text
create_action_checkpoint
  state=prepared, effect=side_effecting,
  replay_policy=requires_reconciliation
  -> authorize_action
  -> persist prepared
  -> persist executing
  -> this dispatch invocation calls the test executor once
  -> Tool returns exit_code=0
  -> crash: after_tool_success_before_terminal_checkpoint

resume
  persisted state=executing
  -> recover_action_checkpoint
  -> state=unknown
  -> decision=reconcile_or_block
  -> original echo is NOT dispatched again
  -> fresh targeted read-only: cat once.txt
  -> stdout exactly "once\n"
  -> reconcile_file_observation
  -> applied / checkpoint=succeeded
  -> reconciliation Evidence
  -> no replay
```

这里验证的是 **no blind replay of an unknown side effect**：测试中的 dispatch 调用 executor 一次；crash
后 recovery path 先观察，不自动重新 dispatch 原副作用。`AuthorizedAction` 不是跨进程一次性 token，代码也
没有 global action-id deduplication ledger。

### Reconciliation 的三个语义结果

| 语义结果 | `reconcile_file_observation` 的直接返回 | 证明条件 | 后续含义 |
|---|---|---|---|
| applied | `status="succeeded"`，checkpoint=`succeeded` | `cat` 的 stdout 与 `expected_file_write` 的期望内容完全一致 | Evidence 将结果记为 `applied`；不重放原 action |
| not applied | `status="not_applied"`，checkpoint=`failed` | targeted `cat/ls` nonzero、stdout 为空，且 stderr 明确包含 `no such file`/`not found` | Retry gate 才可能为新 attempt 重新打开；仍需 current Policy/Governance/Approval |
| uncertain | `status="blocked"`，reason=`uncertain side effect` | command 不相关、Observation 不足、内容不匹配，或成功的 `ls` 只证明存在 | 保持 blocked；不得重放 |

当前只在 `expected_file_write` 能机械识别的 `echo ... > relative-file` 形式上确认 applied/not-applied。

### Replay-safe recovery 的保证边界

对于 Harness 已追踪的 logical action，当副作用结果处于 `unknown` 时，recovery/retry path 不会自动盲目
重新 dispatch 原副作用；必须先 Reconciliation。当前不保证：

- OS-level 或 distributed exactly-once；
- remote service deduplication；
- executor 内部只产生一次副作用；
- process kill 后外部系统一定仍可观测；
- global action-id idempotency，或 embedding 重复调用 dispatch seam 时的去重。

## Key Invariants

1. `executing` 必须先 durable，executor 才能启动。
2. `executing` resume 一律转成 `unknown`，不能猜 success/failure。
3. non-read-only action 不能被提升为 `safe_to_retry`。
4. unknown side effect 先 Reconciliation；未证明 `not_applied` 前不得创建 retry attempt。
5. terminal checkpoint、Audit、Evidence、Artifact 或 Result 缺口都不能成为重放原副作用的理由。
6. 被捕获的 post-tool persistence failure 保留 forward truth 并使 live run Degraded；process crash 则由最后
   durable state 决定 recovery。

## Failure / Edge Cases

- pre-tool checkpoint persist 失败：Tool 未执行，错误可安全上抛。
- Tool 返回 `exit_code=-1` 且 Effect 非只读：terminal state=`unknown`。
- terminal checkpoint persist 失败：返回 raw outcome 与内存中的 `unknown`，`degraded=True`；若 fallback
  `unknown` 也无法持久化，store 中最后状态仍可能是 `executing`。
- Audit/Session/Evidence/Artifact/Result 在 Tool 后失败：进入 Degraded 或 incomplete/blocked，不改写 Tool。
- Reconciliation command 不相关、内容不匹配或 Observation 不足：`uncertain`，Run/Plan blocked。
- MCP/任意外部 action 没有已实现的通用 Reconciliation：unknown outcome 不能靠本模块自动确认。

## Review Anchors

- 在 `dispatch_authorized_action` 检查 `persist executing` 是否严格位于 executor 之前。
- 在 `recover_action_checkpoint` 检查 `executing -> unknown` 是否无旁路。
- 在 `decide_retry` 与 `reconcile_file_observation` 之间检查 Durability 是否优先于 retry convenience。
- 在 `_mark_runtime_degraded`、`_persist_runtime_evidence`、`_finalize_runtime_artifact`、
  `_emit_runtime_result` 检查后处理失败是否可能反向触发 Tool。
- 在测试中区分：fault injector 的 one-shot、单次 dispatch 的 executor call count，以及 recovery path 没有
  自动重放；三者都不等于全局 action-id idempotency。

## Common Misreadings

- **“Tool 返回成功，所以 checkpoint 一定是 succeeded。”错误。** crash window 位于两者之间。
- **“unknown 等于 failed。”错误。** unknown 表示不能证明 applied 或 not_applied。
- **“重跑一次可以修复缺失 Evidence/Artifact。”错误。** 这是副作用重复，不是存储修复。
- **“这是通用 exactly-once delivery。”错误。** 当前保证是 unknown side effect 的 replay-safe recovery，
  不是 OS、remote service 或全局 action-id 去重。
- **“Degraded 是 Tool failure。”错误。** 它描述 post-tool persistence reliability。
- **“每个 injected crash 都会保存 Degraded。”错误。** fault hook 模拟进程中断；Degraded 需要 live Harness
  捕获明确失败。

## Deep Review Questions

1. 为什么 `executing` checkpoint 写入失败时 executor call count 必须为 0？
2. 恢复时只看到 durable `executing`，能证明和不能证明哪些外部事实？
3. `reconcile_file_observation` 在什么严格条件下才能返回 `not_applied`？
4. 为什么成功的 `ls target` 仍不足以把 unknown 写操作判定为 applied？
5. 当前 replay-safe recovery 依赖哪些 Harness 假设，又明确排除了哪些 OS、executor 和并发保证？

## 与其他文档的链接

- 上游执行 seam：[`04-action-lifecycle.md`](04-action-lifecycle.md)
- Retry/Governance：[`05-planning-retry-governance.md`](05-planning-retry-governance.md)
- Evidence/Result：[`10-evidence-artifact-result.md`](10-evidence-artifact-result.md)
- 统一 Failure 语义：[`13-failure-semantics.md`](13-failure-semantics.md)
- 对应测试策略：[`14-testing-strategy.md`](14-testing-strategy.md)

## Navigation

- Previous: [`05-planning-retry-governance.md`](05-planning-retry-governance.md)
- Next: [`07-session-memory-context.md`](07-session-memory-context.md)
- Related: [`04-action-lifecycle.md`](04-action-lifecycle.md), [`13-failure-semantics.md`](13-failure-semantics.md)
