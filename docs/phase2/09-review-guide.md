# Phase 2 Offline Review Guide

本指南面向未来离线 review。先确认 reviewed commit 与测试一致，再按三条路径检查真实 call sites；不要只读 Prompt、CLI 输出或设计文档。

## Path A: Bridge Protocol Review

建议顺序：

```text
bridge_paths
→ bridge_publisher
→ bridge_inspector
→ bridge_claimer
→ bridge_reconciler
→ bridge_executor / result_repairer
→ bridge_worker
→ tests/e2e/test_bridge_end_to_end.py
```

Checklist：

- tmp 是否始终被忽略？
- ready 是否 temp publish 且最后 rename？
- 最终 record 是否 no-replace/immutable？
- Claimer 是否先 atomic mkdir，再在锁内重跑 Inspector？
- attempt number 与 previous nonce 是否形成无 fork 链？
- timestamp 是否被错误当成 ordering/lease？
- unknown old claim 是否被 Worker 自动 execute？
- not_applied 是否只创建新 nonce？
- uncertain 是否始终 block？
- result JSON without ready 是否禁止 task re-execution？
- Inspector 是否是唯一 state derivation？

## Path B: Bridge ↔ Harness Authority Review

建议顺序：

```text
bridge_harness_worker
→ bridge_adapter.read/bind
→ Binding Store
→ run_bound_bridge_request
→ run_agent
→ ResultStore
→ recover_environment_evidence / projection repair
→ project_harness_result_to_bridge → result.ready
```

Checklist：

- 是否出现 Bridge → Environment Adapter direct call？
- Bridge payload 是否只作为 `untrusted_external_input` textual request？
- publisher/consumer/claim metadata 是否进入 Policy 或 Approval？
- Binding 是否在 Harness Run 前 durable？
- source fingerprint 是否在关键边界重新检查？
- same task/claim 是否固定同一 run id？
- same Binding 的两个并发 caller 能否同时启动 Run？
- attempt fence 是否在锁内重读 Binding、Task、Claim 与 Run truth？
- attempt fence 是否覆盖 Harness start 到 Bridge `result.ready` 的 live owner 生命周期，而非仅覆盖 Run start？
- stale attempt fence 是否保持 fail closed、没有时间抢占？
- ASK 是否仍要求当前 Run Approval？
- 新 attempt 是否错误继承旧 Approval？
- Harness terminal 是否先于 Bridge projection？
- Environment Evidence 是否 durable 且绑定到 Result 后才 projection？
- Evidence failure recovery 是否只读取 succeeded checkpoint + safe Audit Observation，且 Adapter calls=0？
- 缺失 Observation 时是否拒绝凭空生成 Evidence？
- `bridge_harness_task` 是否可能误走 generic Result Repair？
- projection repair 是否只读取 Binding 与 durable Harness Result且 external calls=0？
- terminal Harness marker 是否在 projection 前 durable，Reconciler 是否在 fence 外/内都拒绝该 attempt？
- result JSON 未 ready 时，第二 worker/第二 projection writer 是否被单 owner fence 拒绝？
- Bridge Result 是否被读入 Harness Evidence？
- replay 是否读取 Current Bridge、claim 或 publish？

## Path C: Environment Capability Review

建议顺序：

```text
environment_adapters
→ environment_registry
→ capability-specific termux adapter
→ dispatch.environment_invocation_from_authorized
→ agent._handle_environment_decision
→ Observation / Evidence / Result
→ replay and bundle tests
```

Checklist：

- Model 是否只看到 logical capability 与 safe args schema？
- executable、argv、host path 是否隐藏？
- Registry 是否静态且 Harness-owned？
- MCP/Subagent/Bridge metadata 能否修改 spec/effect/zone/callable？
- 是否先经过 Classification、Policy、Runtime Gate、Approval、AuthorizedAction？
- side-effecting dispatch 前 executing checkpoint 是否持久化？
- `effect_certainty=unknown` 是否通过唯一 mapping 变成 checkpoint unknown/reconcile-block，而非 Retry？
- battery retry 是否仍受 deadline/budget/governance 限制？
- raw stdout/stderr 是否进入 Session、Context、Audit、Evidence、Result、Envelope 或 Bundle？
- notification Evidence 是否仅 claim request accepted，而非 user seen？
- historical battery Evidence 是否被错误当作 Current Reality？
- replay/bundle 是否重新调用 Android？

## Cross-layer red flags

看到以下模式应立即停止 review 并追踪 Authority：

- `bridge_*` 模块导入 `termux_capabilities` 或直接调用 registry adapter；
- Adapter import Policy/Approval/Evidence/Result authority；
- Bridge claim nonce 被复用为 action/approval/evidence id；
- `COMPLETED` 未标注属于 Bridge 还是 Harness；
- `unknown` 未标注 Bridge、Harness 或 Environment layer；
- Evidence persistence failure 被改写成 Environment action failure或普通 retry；
- Harness terminal Result 已存在，Bridge Reconciler 仍可追加 effect judgment；
- projection 在未持有 attempt fence 时被 orchestration/recovery caller 调用；
- timeout 直接等于 retry；
- Binding 存在被当成 Harness Run 已安全开始；
- Bridge Result 被验证为 Evidence；
- replay code 调用 live provider、Bridge Worker 或 Environment registry；
- Android raw output 被写入历史对象。

## Required review evidence

一次 Phase 2 change review 至少应给出：

- modified call graph；
- state/ordering impact；
- crash before/after durable boundary；
- capability/Authority ceiling impact；
- new deterministic test；
- replay/external-call count；
- `python -m unittest -q`、self-check、diff-check 结果。

开始新功能前，先逐项核对 [Resolved in P2.6](06-recovery-and-failure-semantics.md#resolved-in-p26)
和 [Resolved in P2.6.1](06-recovery-and-failure-semantics.md#resolved-in-p261)
的 invariant 是否仍由真实 call sites 保持。
