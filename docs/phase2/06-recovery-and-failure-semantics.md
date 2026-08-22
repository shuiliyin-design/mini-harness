# Recovery and Failure Semantics

本章记录当前 Phase 2 recovery baseline。Bridge recovery、Harness durability recovery 与
Environment effect certainty 是三个相互关联但不同的状态空间。P2.7 mobile workflow
resume 复用这些状态，不合并它们，也不让一层代替另一层判断。

## Three recovery domains

```text
Bridge immutable history
  owns: task/claim/reconciliation/result commitment
  states: CLAIMED_UNKNOWN / SAFE_TO_RECLAIM / NEEDS_RESULT_REPAIR / BLOCKED
          │
          │ transport facts only
          ▼
Harness durable action lifecycle
  owns: prepared/executing/succeeded/failed/unknown and recovery permission
          │
          │ authorized invocation only
          ▼
Environment Adapter result
  owns: no_side_effect / known_applied / not_started / unknown
```

### Bridge recovery

```text
CLAIMED_UNKNOWN
  ├─ not_applied → SAFE_TO_RECLAIM_WITH_NEW_NONCE → new attempt
  ├─ applied     → EFFECT_APPLIED_NEEDS_RESULT_REPAIR → Result repair
  └─ uncertain   → BLOCKED_UNCERTAIN_EFFECT
```

Bridge Reconciliation 是 attempt-level caller judgment。它不应解释 Harness 内部 Action checkpoint。

### Harness recovery

```text
prepared  → fresh policy/approval boundary before retry
executing → unknown after crash
succeeded → continue from durable Observation
failed    → return to plan
unknown side effect → reconcile or block; never blind retry
```

Harness durability owns all external side-effect recovery。Bridge Worker 或 Adapter 不得创建第二个 Action recovery engine。

### Environment certainty

```text
no_side_effect : no external side effect to reconcile
known_applied  : adapter-defined effect is known, e.g. request accepted
not_started    : execution mechanics did not begin
unknown        : external outcome is ambiguous
```

Adapter reports facts; Harness decides retry/reconciliation using current Action state、effect、governance 和 policy。

## Similar names are not aliases

| Term | Layer | Exact meaning |
|---|---|---|
| `CLAIMED_UNKNOWN` | Bridge | Claim exists; Bridge history alone does not establish attempt effect |
| `CLAIMED_BY_SELF_UNKNOWN` | Bridge observer view | consumer/nonce hints match; still no continuation authority |
| checkpoint `unknown` | Harness | dispatch began, but no reliable terminal action classification is durable |
| `effect_certainty=unknown` | Environment | Adapter cannot determine external invocation outcome |
| `HARNESS_RECOVERY_REQUIRED` | Integration | bound Run started without terminal Authoritative Result |
| `EVIDENCE_REPAIR_REQUIRED` | Integration | Environment action、safe Observation 已 durable；Harness Evidence 尚未 durable |
| `RESULT_PROJECTION_REQUIRED` | Integration | Harness terminal Result 已 durable；Bridge result.ready 尚未 committed |

`CLAIMED_UNKNOWN` can exist before any Harness action. Environment unknown can occur within a live Harness Run. Harness checkpoint unknown may also arise after a known Adapter return if terminal persistence fails. Documentation and code review must always name the layer.

## Cross-layer ordering

```text
Task JSON → task.ready
→ Claim
→ Binding
→ attempt lifecycle fence acquired
→ Harness Run
→ prepared checkpoint
→ executing checkpoint
→ Environment invocation
→ terminal checkpoint / Observation
→ durable Harness Evidence
→ Model candidate identity durable（若有）
→ Harness authoritative candidate identity durable
→ deterministic Result binding
→ Harness Authoritative Result
→ Result integrity MATCH
→ Bridge Result JSON
→ bridge result.ready
→ attempt lifecycle fence released
```

Required invariants：

- Binding durable before Harness Run。
- attempt fence owned before Harness Run start，并由同一 owner 保持到
  `result.ready`；live execution/projection 与 reconciliation 互斥。
- executing durable before external side effect。
- accepted Environment Evidence durable before Authoritative Result binding。
- normalized Harness candidate durable before Result binding；Model candidate、Harness
  candidate 与 Result 是三个不同语义对象。
- Harness terminal Result durable before Bridge projection。
- Bridge projection 前必须重新验证 Result integrity。
- terminal Result 出现后先发布 immutable Harness-terminal marker；Bridge
  Reconciler 必须服从这条更高层 durable truth。
- Bridge ready published last。
- Unknown side effect never grants retry permission。

## Crash window ownership

| Window | Last durable truth | Recovery owner | Forbidden action |
|---|---|---|---|
| Task JSON before ready | uncommitted transport bytes | Publisher/operator | claim |
| Claim tmp/lock crash | lock or committed Claim | Bridge protocol/operator | stale-lock auto takeover |
| Claim before Binding | Bridge Claim | explicit integration recovery | Worker auto-execute old claim |
| Binding before Run | fixed binding IDs | Bridge Adapter recovery | allocate second run id |
| Run started before terminal | Harness Audit/Session/checkpoint | Harness durability | Bridge-level action retry |
| executing before Adapter return | Harness executing checkpoint | Harness recovery | blind side-effect retry |
| Adapter return before terminal checkpoint | Environment result plus executing checkpoint | Harness recovery | infer failure from persistence error |
| safe Observation before Evidence | succeeded action checkpoint + safe Audit Observation | Evidence-only recovery | dispatch Environment Adapter、凭空构造 Observation |
| Evidence before Harness Result | immutable Evidence + Session action identity | Result-only continuation | repeat Environment action |
| authoritative candidate before Result binding | finalized candidate digest + normalization refs | deterministic Result-only continuation | call Model、Environment、Approval or Bridge action |
| Harness terminal before projection | Authoritative Result | projection-only recovery | re-run Harness |
| Bridge Result JSON before ready | matching partial projection | projection publisher | execute or re-run Harness |

## Known P2.6 Safety Gaps

P2.6 release review 重新打开了两个窗口：Evidence persist failure 缺少
Evidence-only recovery，以及 terminal Result 后 attempt fence 提前释放导致 projection/
Reconciler 竞争。它们不能被算入“P2.6 已关闭”的结论；实现和测试依据记录在
[Resolved in P2.6.1](#resolved-in-p261)。当前 P2.6.1 re-review 未发现遗留 P0；后续新风险仍须先列为 Known Gap。

## Resolved in P2.6

### 1. BridgeHarness Environment Evidence durability

- **Invariant:** accepted Environment Evidence 必须 durable 且进入 Harness Result binding，之后才允许 Bridge projection；Evidence 失败不得重调 capability。
- **Implementation:** `bridge_adapter._start_bound_harness_run()` 显式注入 Harness-owned `EvidenceStore`；`historical_evidence_accepted()` 接受经 Harness 验证的 `termux_observation`；projection 前重新加载 durable Result。P2.6 只做到 Evidence persist 失败不重调 capability，尚缺少 Evidence-only repair；该窗口由 P2.6.1 补齐。
- **Test evidence:** `test_p26_cross_layer_safety` 对 battery、notification、Evidence failure、Result failure与两段 fault window 断言 Evidence/Result ordering 和 Environment call count。

### 2. Integration Result repair boundary

- **Invariant:** `bridge_harness_task` 的 Result 只能由 Binding 指向的 durable Harness Result 投影，caller payload 无权完成 integration attempt。
- **Implementation:** generic `repair_bridge_result()` 只允许 `bridge_test`；integration 返回 `INTEGRATION_REPAIR_REQUIRED`。`repair_bridge_harness_projection()` 只读 Binding/Result 并做幂等 projection，不运行 Harness 或 Environment。
- **Test evidence:** forged generic repair 被拒绝；terminal-result 与 partial-JSON recovery 均断言 Environment calls 为零。

### 3. Same-Binding single execution owner

- **Invariant:** 相同 `task_id + claim_nonce` 同时最多一个 Harness Run execution owner；Binding identity 相同不等于并发启动许可。
- **Implementation:** atomic mkdir attempt fence 在锁内重读 Binding、Task、Claim、Run/Result；竞争者返回 `BINDING_LOCKED`。残留 fence 不按时间抢占或自动删除。
- **Test evidence:** 真实线程竞争只有一个 provider/run start；stale fence 测试保持 fail closed。

### 4. Environment certainty/checkpoint mapping

- **Invariant:** `unknown → checkpoint unknown`，`known_applied → succeeded`，`not_started → failed`；read-only `no_side_effect` 走明确 success/failure，不进入 side-effect reconciliation。
- **Implementation:** `dispatch.environment_checkpoint_outcome()` 是唯一映射 helper，Environment dispatch 不再用 exit code 猜 side-effect certainty；terminal persistence failure只可降级为 unknown，不能反写 failed。
- **Test evidence:** battery timeout、notification timeout 与 nonzero+unknown 均覆盖；unknown notification 不 retry。

### 5. Live execution/reconciliation exclusion

- **Invariant:** 同一 Bridge attempt 的 Executor、BridgeHarness execution 与 Reconciler 互斥；只有 execution ownership 已停止后才可写 reconciliation。
- **Implementation:** 三条路径共用 `bridge_attempt_fence.py` 的 atomic mkdir fence。P2.6 只覆盖 Run start/live execution；terminal-to-projection 生命周期由 P2.6.1 延长并增加 immutable terminal marker。
- **Test evidence:** held fence 同时拒绝 Executor/Reconciler；并发 Harness execution 拒绝第二 owner；residual lock 不被偷取。

## Resolved in P2.6.1

### A. Evidence-only recovery

- **Review finding:** Environment effect 与 safe Observation 已可靠完成后，Evidence store failure 只有 degraded 状态，没有可执行的补写路径。
- **Invariant:** recovery 必须绑定 `run_id/action_id/observation_event_id/capability/effect/effect_certainty`、safe Observation 与 stream digest identity；缺少 durable Observation 必须 fail closed。battery 与 notification 都不得再次调用 Adapter。
- **Implementation:** `recover_environment_evidence()` 只读取 Binding、succeeded action checkpoint 与 environment Audit safe Observation；生成与 durable source 一致的 deterministic Evidence ID/fingerprint，补写 Evidence/Audit/Result，再走 projection。它没有 Provider 或 Adapter callable，也不会获取新 claim。若原 final candidate 未以可恢复正文持久化，恢复 Result 保守为 `incomplete`，不伪造 completed。
- **Test evidence:** battery/notification Evidence store failure 后 Environment call count 均保持 1；Evidence 后 Result store failure 的第二次调用只补 Result；删除 safe Observation identity 后返回 `OBSERVATION_RECOVERY_REQUIRED`。

### B. Full attempt fence lifecycle

- **Review finding:** Harness Result durable 后 fence 已释放，projection 尚未 committed 时 Reconciler 可与 recovery 竞争。
- **Invariant:** live owner 按 `Claim → Binding → Run → Evidence → Result → projection → ready` 持有 attempt fence；terminal Harness truth 永远优先于 Bridge reconciliation。
- **Implementation:** `run_bound_bridge_request()` 与 `repair_bridge_harness_projection()` 在同一 fence 内完成 projection/ready；terminal Result 先发布 immutable Harness-terminal marker。Reconciler 在 fence 外与 fence 内都检查 marker并拒绝。marker publication failure 保留 fence、fail closed；fence 仍不是 lease，进程硬崩溃后的残留不自动 takeover。
- **Test evidence:** terminal-before-projection 与 JSON-before-ready 均只做 projection/ready repair；暂停 projection 的线程持有 fence，第二 worker、第二 projection writer 和 Reconciler 全部不能推进，Environment calls 为 0，最终只有一个 committed Bridge Result。

### C. P2.7.1 authoritative candidate identity

- **Real finding:** 首次 P2.7 Android workflow smoke 中，A/B semantic Result 正确，
  但 mobile contract 改写 candidate 后，Result integrity 仍检查改写前
  `final_candidate_received` digest，因而 MISMATCH；Approval-denied 的 C 没有经过该
  rewrite，保持 MATCH。
- **Invariant:** Model Candidate ≠ Harness Authoritative Candidate ≠ Authoritative Result。
  `authoritative_candidate_finalized` 必须先 durable，Result binding 才能发布；Result
  不能引用 stale Model digest。
- **Implementation:** `agent._handle_final_candidate()` 分别记录
  `model_final_candidate_received` 与 legacy alias；
  `agent._persist_authoritative_candidate_finalized()` 持久化 finalized digest；
  `result.finalize_authoritative_candidate()` 为 live/replay 共用纯函数；
  `result_integrity_check()` 对新历史验证 finalized event，对无该 event 的旧历史保留
  legacy check。`bridge_adapter._harness_result_integrity_valid()` 在 terminal marker 与
  Bridge projection 前 fail closed。
- **Recovery:** finalized event 后 crash 可按相同 digest 幂等复用；replay 只读取 Audit、
  Envelope、Evidence 与 Result，不调用 Provider、Environment、Approval 或 Bridge。
- **Test evidence:** `test_battery_80_completes_without_notification`、
  `test_battery_20_asks_then_accepts_notification`、
  `test_approval_denied_is_authoritative_incomplete` 均断言 Result MATCH；
  `test_unsatisfied_contract_binds_finalized_harness_candidate`、
  `test_finalized_candidate_digest_mismatch_fails_result_integrity`、
  `test_history_without_finalized_candidate_uses_legacy_check` 和
  `test_bridge_projection_waits_for_result_integrity` 覆盖 rewrite、tamper、compatibility
  与 projection ordering。

## P2.6.1 status

原五个 P2.6 项与 release review 重新打开的两个窗口均有实现和 deterministic test
依据；当前 unresolved P0 为 0。P2.7 随后增加了 fixed mobile orchestration，但没有引入
GUI、daemon、第三 capability 或通用/multi-run orchestration；其四个 workflow crash
窗口见 [Mobile Agent Orchestration](10-mobile-agent-orchestration.md#crash--resume-matrix)。
shared-storage 与 stale-lock 的既有限制仍然成立。
