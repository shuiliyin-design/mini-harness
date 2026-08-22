# Bridge Protocol v1

Bridge Protocol v1 是 Android shared storage 上的教学 transport protocol。它通过 immutable records 和 derived state 协调跨环境 task attempt，但不提供 Harness 或 Android execution authority。

> Bridge claim 只表示 queue/attempt ownership。它不等价于 Human Approval、Android permission、Harness `AuthorizedAction`、shell authority 或 GUI authority。

实现入口：

- [Publisher](../../mini_harness_core/bridge_publisher.py)
- [Inspector](../../mini_harness_core/bridge_inspector.py)
- [Claimer](../../mini_harness_core/bridge_claimer.py)
- [Reconciler](../../mini_harness_core/bridge_reconciler.py)
- [Executor](../../mini_harness_core/bridge_executor.py)
- [Worker](../../mini_harness_core/bridge_worker.py)
- [Result Repairer](../../mini_harness_core/bridge_result_repairer.py)

## Directory layout

```text
agent-bridge/
├── inbox/
│   ├── <task_id>.json
│   └── <task_id>.ready
├── locks/
│   └── <task_id>.lock/
├── claims/
│   └── <task_id>/<claim_nonce>.json
├── reconciliations/
│   └── <task_id>/<claim_nonce>.json
└── outbox/
    ├── result-<task_id>.json
    └── result-<task_id>.ready
```

隐藏文件和 `.tmp` 不属于 committed history。所有工具复用 [bridge_paths.py](../../mini_harness_core/bridge_paths.py) 的 task-id allowlist、canonical root、realpath containment 与 symlink fail-closed 规则。

## Task v1

```json
{
  "task_schema_version": 1,
  "task_id": "task-010",
  "task_type": "bridge_test",
  "payload": {"message": "hello"},
  "publisher_id": "operator",
  "published_at": "audit timestamp"
}
```

`published_at` 仅用于审计。正式 task type 包括教学用无副作用 `bridge_test`，以及由独立 Adapter 接收的 `bridge_harness_task`；Bridge Executor 只执行 `bridge_test`。

## Claim v1

```json
{
  "claim_schema_version": 1,
  "task_id": "task-010",
  "consumer_id": "codex-proot",
  "claim_nonce": "claim-010-a",
  "attempt_number": 1,
  "previous_claim_nonce": null,
  "claimed_at": "audit timestamp"
}
```

第一条 attempt 为 `1/null`。只有链尾 reconciliation 为 `not_applied`，Claimer 才允许创建全新 nonce 的 N+1 attempt，并把 `previous_claim_nonce` 指向旧链尾。timestamp 不参与排序，也不提供 lease。

Claimer 先通过 atomic `mkdir locks/<task_id>.lock` 取得 task lock，再在锁内调用 Inspector 重新推导状态。锁已存在时返回 `TASK_LOCKED`；V1 不等待、抢占或自动清理 stale lock。

## Reconciliation v1

```json
{
  "reconciliation_schema_version": 1,
  "task_id": "task-010",
  "claim_nonce": "claim-010-a",
  "result": "applied",
  "checked_by": "operator",
  "method": "manual_inspection",
  "reconciled_at": "audit timestamp"
}
```

只允许 `applied`、`not_applied`、`uncertain`。Reconciler 持久化 caller 明确提供的 judgment，不读取 payload 猜测结果，不执行后续动作。

## Result v1

```json
{
  "result_schema_version": 1,
  "task_id": "task-010",
  "claim_nonce": "claim-010-a",
  "consumer_id": "codex-proot",
  "status": "completed",
  "result": {"echo": "hello"},
  "artifact_refs": [],
  "completed_at": "audit timestamp"
}
```

Result Repairer 可增加 `completion_source="reconciliation_repair"`；Harness projection 使用 `completion_source="harness_result_projection"`。外层 `status="completed"` 表示 transport result committed，不表示业务成功。

## Commit protocol

Task 与 Result：

```text
write tmp
→ flush/fsync file
→ atomic no-replace publish JSON
→ write ready tmp
→ flush/fsync file
→ atomic no-replace publish ready LAST
```

Claim 与 Reconciliation 通过单个 tmp → fsync → atomic no-replace publish 提交。最终路径存在时不得覆盖。

因此：

- tmp 文件被 Inspector 忽略；
- Task JSON 无 ready 为 `NOT_READY`；
- Result JSON 无 ready 为 partial publish，不允许重跑 task；
- ready marker 才将对应 JSON 提升为 committed record。

## Derived state

```text
NOT_READY
  ↓ task JSON + ready
READY_TO_CLAIM
  ↓ immutable claim
CLAIMED_UNKNOWN
  ├─ not_applied → SAFE_TO_RECLAIM_WITH_NEW_NONCE → new attempt
  ├─ applied     → EFFECT_APPLIED_NEEDS_RESULT_REPAIR → COMPLETED
  └─ uncertain   → BLOCKED_UNCERTAIN_EFFECT

normal bridge_test:
CLAIMED_UNKNOWN → Executor → result JSON → result.ready → COMPLETED
```

Inspector 还可根据 observer hints 显示 `CLAIMED_BY_SELF_UNKNOWN` 或 `CLAIMED_BY_OTHER`。这些标签不提供续跑权限。`INVALID_HISTORY` fail closed。

状态只从 immutable history 推导，不从 mtime、wall clock、Session Memory 或 Worker process memory 推导。

## Recovery three-way split

- `not_applied`：effect 已确认没有发生；只能创建新 nonce、新 attempt，不能复用旧 claim。
- `applied`：effect 已确认发生；禁止重执行，只允许修复 Result。
- `uncertain`：无法证明发生或未发生；阻塞，不自动 retry/reclaim。

Worker 只把本次 invocation 刚创建的 claim 立即交给 Executor；它不会根据旧 `CLAIMED_BY_SELF_UNKNOWN` 自动继续。

## Tool responsibilities

| Tool | Owns | Does not own |
|---|---|---|
| Publisher | commit Task v1 | claim、execution、result |
| Inspector | validate history and derive state | mutation、lease、Authority |
| Claimer | immutable attempt ownership | execution permission、effect |
| Reconciler | caller effect judgment | execution、retry、result |
| Executor | tiny `bridge_test` semantics | claim、reconcile、Harness Authority |
| Worker | conservative one-step composition | old-claim continuation、reclaim、repair |
| Result Repairer | applied-effect Result publication | task execution、new claim、reconciliation |

## Schema evolution

- `task-001`：pre-v1 marker experiment。
- `task-002`：legacy/transitional schema experiment。
- `task-003+`：formal Bridge Protocol v1。

Schema evolution 可以改变新 reader 如何解释历史，但不得重写 immutable history。Inspector 不为 task-001/002 放宽正式 v1 validation。

## Filesystem limitations

本设备观察到 shared-storage mkdir 提供实验所需互斥，但这不是所有 Android/filesystem 的形式保证。V1 不保证 hostile concurrent filesystem、distributed consensus、trusted clock、automatic stale-lock recovery、remote exactly-once、cryptographic identity、Android process liveness 或 crash-proof shared-storage fsync。

跨层已知缺口见 [Recovery and Failure Semantics](06-recovery-and-failure-semantics.md)。

## Offline CLI teaching chain

```bash
BRIDGE_ROOT=/path/to/agent-bridge

python bridge_publisher.py --root "$BRIDGE_ROOT" --task-id task-010 \
  --task-type bridge_test --payload-json '{"message":"hello"}' --publisher operator
python bridge_inspector.py --root "$BRIDGE_ROOT" task-010
python bridge_claimer.py --root "$BRIDGE_ROOT" --task-id task-010 \
  --consumer codex-proot --claim-nonce claim-010-a
python bridge_inspector.py --root "$BRIDGE_ROOT" --consumer codex-proot \
  --claim claim-010-a task-010
python bridge_executor.py --root "$BRIDGE_ROOT" --task-id task-010 \
  --consumer codex-proot --claim-nonce claim-010-a
python bridge_inspector.py --root "$BRIDGE_ROOT" task-010
```

`not_applied` 创建新 attempt：

```bash
python bridge_reconciler.py --root "$BRIDGE_ROOT" --task-id task-010 \
  --claim-nonce claim-010-a --result not_applied \
  --checked-by operator --method manual_inspection
python bridge_claimer.py --root "$BRIDGE_ROOT" --task-id task-010 \
  --consumer codex-proot --claim-nonce claim-010-b
```

`applied` 只修 Result，不调用 Executor：

```bash
python bridge_reconciler.py --root "$BRIDGE_ROOT" --task-id task-010 \
  --claim-nonce claim-010-a --result applied \
  --checked-by operator --method manual_inspection
python bridge_result_repairer.py --root "$BRIDGE_ROOT" --task-id task-010 \
  --claim-nonce claim-010-a --consumer codex-proot \
  --result-json '{"message":"effect already applied"}'
```
