# Harness ↔ Bridge Adapter v1

> Bridge solves transport. Harness owns authority.

Adapter 只把 committed、合法 claim 的 `bridge_harness_task` 转成 fresh Harness Run，
并把 Harness terminal Authoritative Result 单向投影回 Bridge。P2.7 的 optional structured
threshold 只选择固定 mobile workflow，不增加 Bridge Authority。

```text
Bridge Task
→ Claim
→ immutable Binding
→ Harness Run
→ normal Policy / Approval / AuthorizedAction
→ Authoritative Result
→ Bridge Result Projection
```

实现位于 [integrations/bridge_adapter.py](../../mini_harness_core/integrations/bridge_adapter.py) 与 [integrations/bridge_worker.py](../../mini_harness_core/integrations/bridge_worker.py)。旧 `bridge_worker.py` 仍只处理 `bridge_test`。

## Inbound schema

唯一允许的 task type 保持不变；普通 payload 为：

```json
{
  "task_type": "bridge_harness_task",
  "payload": {"request": "external textual request"}
}
```

P2.7 mobile variant 允许同一 payload 增加 `"threshold": 0..100` 整数。
`request` 必须是非空 UTF-8 string，不超过 16 KiB，并通过 secret screening；threshold
拒绝 bool、负数与大于 100 的值。除此以外，`shell`、`command`、`tool`、`mcp`、
`subagent`、`approval`、`policy`、`effect` 或 `authorized_action` 字段均被拒绝。

进入 Harness 时使用固定 label `untrusted_external_input`。publisher、consumer、ready 和 claim metadata 不进入 Model Context 作为信任声明。

## Identity separation

```text
Transport Identity = task_id + claim_nonce
Source Identity    = canonical committed Task SHA-256
Run Identity       = harness_session_id + harness_run_id
Action Identity    = Harness-created action_id/logical_action_id
```

这些 identity 不能互换。Bridge task/claim 不得成为 action、approval 或 evidence identity。

## Immutable Binding

路径：

```text
.audit/bridge_bindings/<task_id>/<claim_nonce>.json
```

```json
{
  "binding_schema_version": 1,
  "task_id": "task-010",
  "claim_nonce": "claim-010-a",
  "harness_session_id": "32 lowercase hex",
  "harness_run_id": "32 lowercase hex",
  "source": "bridge",
  "source_fingerprint": "sha256",
  "created_at": "audit timestamp",
  "binding_fingerprint": "sha256"
}
```

Binding 使用 canonical JSON、immutable atomic publish 和 fingerprint。相同 task/claim 的有效 Binding 被复用；内容或 source 不同则 `BINDING_CONFLICT`。同一 claim 必须固定到同一 run id。

## Exact ordering

1. Inspector 确认 current attempt history 合法。
2. Adapter 校验 Task schema、type、size、secret。
3. 固定 `source_fingerprint`。
4. Worker 只使用本次 invocation 刚创建的 claim。
5. Binding 前重新读取 Task 与 Claim，fingerprint 必须一致。
6. 预分配 Harness session/run IDs。
7. durable publish Binding。
8. 使用 Binding IDs 创建或恢复 Harness Run。
9. request 进入正常 Context、Model、Classification、Policy、Runtime Gate、Approval 与 AuthorizedAction pipeline。
10. Harness 持久化 terminal Authoritative Result。
11. Adapter 生成 safe Bridge projection。
12. Bridge `result.ready` 最后发布。

正常路径不允许 Harness Run 先于 Binding durable。

## Authority boundary

```text
Bridge Task               ≠ Harness Intent Authority
Bridge Claim              ≠ Harness AuthorizedAction
Bridge ready/binding      ≠ Human Approval
Bridge Result             ≠ Harness Evidence
Bridge COMPLETED          ≠ Harness Result completed
```

Bridge metadata不能把 DENY 提升为 ASK、ASK 提升为 ALLOW、read-only 提升为 side-effecting，也不能增加 write、MCP、Subagent 或 Termux capability。ASK 只能由当前 Harness Run 的 Approval 满足；新 attempt 不继承旧 Run Approval。

## Result projection

Harness 的 `completed|blocked|failed|cancelled|incomplete` 都可以投影为 committed transport result：

```json
{
  "result_schema_version": 1,
  "task_id": "task-010",
  "claim_nonce": "claim-010-a",
  "consumer_id": "codex-proot",
  "status": "completed",
  "result": {
    "bridge_result_schema_version": 1,
    "harness_run_id": "32 lowercase hex",
    "harness_result_status": "blocked",
    "summary": "safe summary",
    "artifact_refs": []
  },
  "artifact_refs": [],
  "completion_source": "harness_result_projection",
  "completed_at": "audit timestamp"
}
```

Bridge `COMPLETED` 应显示/理解为 `RESULT_COMMITTED` 或 `TRANSPORT_COMPLETED`。Projection 不复制 raw Audit、Evidence、Tool output、hidden reasoning 或 secret。

## Crash A/B/C matrix

| Window | Durable truth | Recovery state | Allowed recovery |
|---|---|---|---|
| A: claim 后、Binding 前 | Bridge Claim | `INTEGRATION_UNKNOWN` | 显式 integration recovery；不自动 new claim/execute |
| B: Binding 后、Run create 前 | Binding + fixed IDs | `BOUND_NOT_STARTED` | 用同一 IDs 创建/恢复；不得新建第二 Run |
| Run 已开始、Result 未 terminal | Harness Audit/checkpoint | 普通 Run 为 `HARNESS_RECOVERY_REQUIRED`；fixed mobile workflow 可在同 Binding/Run 内 resume | 完全委托 Harness durability；禁止 Bridge action retry |
| C: Harness terminal 后、Bridge Result 前 | Authoritative Result | `RESULT_PROJECTION_REQUIRED` | projection only；不得重跑 Harness |
| Result ready 已发布 | complete histories | `DONE` | 无 live action |

Fault hooks 只覆盖 integration seam：`after_claim_before_binding`、`after_binding_before_run_create`、`after_harness_terminal_before_bridge_result`。

## Worker boundary

BridgeHarnessWorker 每次最多处理 lexical order 中一个 fresh `READY_TO_CLAIM` task。它不
自动处理未绑定的旧 claim，不 reclaim、reconcile、approve、并发运行多个 Harness Runs，
也不直接调用 Environment Adapter。P2.7 只允许已绑定 fixed mobile Run 依据 durable
checkpoint/Evidence resume；详见 [Mobile Agent Orchestration](10-mobile-agent-orchestration.md)。

## Current baseline caveat

上述 ordering 和 identity 是当前稳定设计。中期审计发现的 BridgeHarness Evidence
persistence、same-Binding concurrency 和 generic Bridge repair fencing 已在 P2.6 关闭；
实现与测试证据见 [Resolved in P2.6](06-recovery-and-failure-semantics.md#resolved-in-p26)。
