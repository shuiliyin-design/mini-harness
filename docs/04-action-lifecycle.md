# Action Lifecycle 与 Replay-Safe Recovery

## 读完你应该理解什么

- `prepared`、`executing`、`succeeded`、`failed`、`unknown` 分别证明什么。
- 为什么 `executing` 必须在 side effect 前持久化。
- 为什么 Tool success 仍需 Verification，unknown side effect 不能直接 retry。

核心实现位于 [`durability.py`](../mini_harness_core/durability.py)、
[`dispatch.py`](../mini_harness_core/dispatch.py) 和
[`agent.py`](../mini_harness_core/agent.py)。故障注入点位于
[`fault_injection.py`](../mini_harness_core/fault_injection.py)。

## 状态机

```text
prepared
   |
   v
executing
   |
   +----------> succeeded
   |
   +----------> failed
   |
   +----------> unknown

unknown ------> succeeded   (fresh reconciliation proves applied)
unknown ------> failed      (fresh reconciliation proves not applied)
```

合法 transition 由 `durability._TRANSITIONS` 和 `transition_action_checkpoint` 强制。`succeeded` 与
`failed` 是 terminal；它们不会因为后续 Audit/Evidence/Result store 失败而回退。

## 每个状态证明什么

- `prepared`：Harness 已记录 exact capability/arguments/effect，但 executor 尚未被允许开始。
- `executing`：dispatch 已经过所有 Authority gates，side effect 现在可能发生。
- `succeeded`：Tool 返回 definite success，并已有 safe Observation。
- `failed`：Tool 返回 definite failure，并已有 safe Observation；对 side-effecting action，非零返回仍不总能证明没有效果，因此 retry policy 仍保守。
- `unknown`：外部效果可能发生，但 Harness 没有足够 terminal durability 证明结果。

## AuthorizedAction dispatch seam

`authorize_action` 创建 sealed `AuthorizedAction`；`dispatch_authorized_action` 是唯一接受它并调用
executor 的 seam。核心顺序：

```text
1. validate sealed AuthorizedAction + prepared checkpoint binding
2. persist prepared checkpoint
3. transition and persist executing checkpoint
4. audit/start callback
5. call the selected executor once for this dispatch invocation
6. derive succeeded / failed / unknown
7. persist terminal checkpoint
8. append safe Audit outcome
9. optionally persist Session projection
```

步骤 2/3 是执行前置条件。任一步失败，Tool 不启动。

## 为什么必须先持久化 `executing`

假设先写文件再保存 checkpoint：

```text
Tool writes file
process crashes
checkpoint still says prepared
resume repeats Tool
```

这会产生重复副作用。当前顺序把最坏情况变成：

```text
persist executing
Tool writes file
process crashes before terminal checkpoint
resume sees executing -> unknown
Harness reconciles instead of replaying
```

这里保证的是 replay-safe recovery：Harness 已追踪的 side effect 为 `unknown` 时，recovery/retry path 不会
blind replay 原 action，而是先 Reconciliation。它不保证 OS/distributed exactly-once、remote deduplication、
executor 内部只产生一次副作用或 global action-id idempotency。

## Crash windows 与 forward truth

`DeterministicFaultInjector` 提供六个测试点。最关键的三个 dispatch window：

| Crash point | 持久事实 | Resume interpretation |
|---|---|---|
| Tool 前 executing persist 失败 | prepared 或无新状态 | executor 未启动，可安全阻断/重新授权。 |
| Tool success 后、terminal checkpoint 前 | executing | 恢复为 unknown，reconcile-or-block。 |
| terminal checkpoint 后、Audit 前 | succeeded/failed | 保留 terminal Tool truth，不重放。 |

被 live Harness 捕获的 Evidence、Artifact、Result 或 Audit persistence failure 会令 run Degraded；
`InjectedFault` 模拟的是进程中断，不会自动写 Degraded，恢复时应读取最后 durable state。两者都不能把 Tool
outcome 改写成另一次普通失败。相关测试在 [`test_v26_boundary.py`](../test_v26_boundary.py) 和
[`test_v26_failure_semantics.py`](../test_v26_failure_semantics.py)。

## Observation

executor 返回 raw Observation；checkpoint 只保存
`observation.persisted_safe_observation` 的 projection。stdout/stderr/result/error 只保留 digest、长度和
少量安全字段。这样 crash recovery 能比较历史 identity，同时避免 secret 进入 Session。

## Tool Success ≠ Step Complete

`succeeded` 只表示 Tool invocation 成功。side-effecting action 还会创建 Verification obligation：

```text
Action succeeded
  -> requires_verification=True
  -> targeted read-only action
  -> fresh Observation
  -> accepted Verification Evidence
  -> Artifact contract evaluation
  -> Plan step may complete
  -> Result may become completed
```

如果模型在 Verification 前输出 `final_answer`，`_handle_final_candidate` 会拒绝并返回 verification
feedback。

## Unknown side effect 与 reconciliation

`recover_action_checkpoint` 把 `executing` 转为 `unknown`。若 replay policy 不是 `safe_to_retry`，原 action
不会自动重放。

当前 `reconcile_file_observation` 只理解严格的 `echo ... > path` 写入，并接受相关的 `cat path` 或
`ls path` read-back。结果：

- `applied`：fresh Observation 与预期内容相符；旧 action 转为 `succeeded`。
- `not_applied`：fresh Observation 明确证明目标不存在；旧 action 转为 `failed`，retry gate 才可能重开。
- `uncertain`：证据不足；保持 blocked。

Historical Evidence 不能代替这次 fresh observation，因为它只证明过去记录过什么。

## Pause/Cancel 与可靠边界

pause/cancel 是 cooperative，而不是杀死 in-flight process。若请求发生在 Tool 执行期间，dispatch 先保存
真实 terminal checkpoint，再由 `run_control.settle_control_boundary` 转为 `paused` 或 `cancelled`。
这样用户控制不会破坏 action truth。

下一步阅读：[`05-planning-retry-governance.md`](05-planning-retry-governance.md)；完整循环见
[`02-agent-loop.md`](02-agent-loop.md)。

## Navigation

- Previous: [`03-authority-and-policy.md`](03-authority-and-policy.md)
- Next: [`05-planning-retry-governance.md`](05-planning-retry-governance.md)
- Related: [`06-durability-and-recovery.md`](06-durability-and-recovery.md), [`13-failure-semantics.md`](13-failure-semantics.md)
