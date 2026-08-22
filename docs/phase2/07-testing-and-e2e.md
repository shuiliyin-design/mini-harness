# Testing and E2E Baseline

Phase 2 correctness gate 是 deterministic offline tests。真实 Android smoke 只说明当前设备链路可用，不能替代 fault、authority 和 schema tests。

## Snapshot

P2.6.1 closure 后最近一次完整运行的 snapshot 为 605 tests passing。这个数字用于定位文档时点，不是长期测试数量承诺；新增或重组测试后应以当前 `python -m unittest -q` 输出为准。

## Bridge deterministic tests

独立测试覆盖：

- Publisher：tmp/ready ordering、duplicate、secret/path safety。
- Inspector：formal history、claim chain、reconciliation/result states、invalid history。
- Claimer：atomic task lock、attempt number、previous nonce、stale lock。
- Reconciler：latest-claim only、three-way judgment、immutability。
- Executor：tiny `bridge_test` allowlist、partial result、no re-execution。
- Worker：deterministic discovery、single-task limit、old claim not continued。
- Result Repairer：applied-only、matching partial ready repair、conflict。

主要文件位于 `tests/unit/`：`test_bridge_publisher.py`、`test_bridge_inspector.py`、
`test_bridge_claimer.py`、`test_bridge_reconciler.py`、`test_bridge_executor.py`、
`test_bridge_result_repairer.py`；Worker 组合测试位于
`tests/integration/test_bridge_worker.py`。

## Bridge vertical E2E

[test_bridge_end_to_end.py](../../tests/e2e/test_bridge_end_to_end.py) 使用 `TemporaryDirectory` 固定八条纵向场景：

1. happy publish → claim → execute → completed；
2. completed duplicate consumption；
3. claim crash；
4. `not_applied` 新 attempt；
5. `applied` result repair；
6. `uncertain` block；
7. Result JSON publish crash；
8. two-consumer claim competition。

它们验证 visibility/commitment、immutable history、no old-claim retry、recovery three-way split 和 terminal completion。

## Harness ↔ Bridge tests

[test_bridge_adapter.py](../../tests/integration/test_bridge_adapter.py) 与
[test_bridge_harness_worker.py](../../tests/e2e/test_bridge_harness_worker.py) 覆盖：

- strict `bridge_harness_task` mapping；
- Binding integrity、reuse、conflict 与 source drift；
- Bridge metadata 不提升 Harness Policy；
- ASK 仍需要 current Run Approval；
- DENY 不执行 action；
- Harness 五种 terminal status projection；
- crash A/B/C；
- old `bridge_test` Worker 不变；
- replay 不执行 Current Bridge action。

P2.6/P2.6.1 另以真实线程竞争覆盖 same-Binding single start、完整 attempt
lifecycle fence，并覆盖 Reconciler/live execution/projection exclusion。

## Environment Adapter tests

- [test_environment_adapters.py](../../tests/unit/test_environment_adapters.py)：Contract、certainty、registry immutability/fingerprint、model catalog、generic dispatch structure。
- [test_termux_capabilities.py](../../tests/unit/test_termux_capabilities.py)：battery fixed executable、JSON/schema/output/error handling。
- [test_termux_notification.py](../../tests/unit/test_termux_notification.py)：fixed argv、shell false、input safety、request-accepted semantics、unknown effect。
- [test_termux_harness_integration.py](../../tests/integration/test_termux_harness_integration.py)：battery Policy/AuthorizedAction/Observation/Evidence/Retry/Replay/Bridge boundary。
- [test_termux_notification_harness.py](../../tests/integration/test_termux_notification_harness.py)：ASK/Approval、durability、known/unknown effect、no blind retry、Evidence、Replay/Bridge boundary。

## Real Android smoke

已经在当前 PRoot/Android 环境做过：

- Bridge Protocol 历史只读 inspection 与 publish/claim/execute/repair 教学链；
- Harness battery 调用，返回 percentage、`effect=read_only`、`effect_certainty=no_side_effect` 和 safe Observation；
- Harness notification 调用，经过 ASK → Human Approval → AuthorizedAction，返回 `request_accepted=true`、`effect_certainty=known_applied`；
- BridgeHarness read-only、ASK、duplicate Binding 和 non-success terminal projection smoke。

Smoke 不打印 raw subprocess stdout/stderr，不调用真实 LLM；Model decision 使用 deterministic/FakeProvider。

真实 smoke 只能支持以下结论：当前设备、当前安装与当前时点链路工作。它不能证明所有 Android filesystem、Termux companion、process liveness 或 future Current Reality。

## Standard validation

```bash
python -m unittest -q
git diff --check
python mini_harness.py --self-check
```

Self-check 覆盖 dependency DAG、Authority、protected paths、golden run、exactly-once、secret boundary 和 bundle replay。

## P2.6 cross-layer closure tests

[test_p26_cross_layer_safety.py](../../tests/security/test_p26_cross_layer_safety.py) 当前包含 18 个组合测试，覆盖：

- battery/notification BridgeHarness Evidence durability 与 Result binding；
- Evidence、Result、projection JSON/ready 故障后的 no re-execution；
- generic integration repair rejection 与 projection-only repair；
- same-Binding 线程竞争、stale fence 与 Executor/Reconciler exclusion；
- certainty→checkpoint 四态映射，包括 nonzero exit + unknown。
- battery/notification Evidence-only repair，且 Environment call count 始终为 1；
- Evidence durable 后 Result-only continuation，以及缺失 safe Observation 时 fail closed；
- terminal Harness truth 排除 Reconciler；
- projection owner 暂停时，第二 worker/第二 writer 被同一 attempt fence 拒绝。

这些测试为 [Resolved in P2.6](06-recovery-and-failure-semantics.md#resolved-in-p26)
与 [Resolved in P2.6.1](06-recovery-and-failure-semantics.md#resolved-in-p261)
提供 deterministic evidence；它们不把 shared-storage mkdir 扩张为所有 Android filesystem 的形式保证。
