# Phase 2 Design Decisions

以下 ADR-style 条目记录已经稳定的方向。`Where implemented` 指向当前主要实现位置；它不表示相邻的 [P2.6 gaps](06-recovery-and-failure-semantics.md#known-p26-safety-gaps) 已修复。

| # | Decision | Why | Alternative rejected | Consequence | Where implemented |
|---:|---|---|---|---|---|
| 1 | Transport identity ≠ Authority | task/claim 只能说明 transport ownership | 用 publisher/consumer metadata 提升信任 | Bridge 输入始终进入 external/untrusted Harness path | [integrations/bridge_adapter.py](../../mini_harness_core/integrations/bridge_adapter.py) |
| 2 | Visibility ≠ Commitment | crash 可能留下 tmp 或 partial JSON | 看见文件就消费 | ready marker 必须最后发布 | [bridge/publisher.py](../../mini_harness_core/bridge/publisher.py)、[bridge/inspector.py](../../mini_harness_core/bridge/inspector.py) |
| 3 | Claim ≠ Execution Authority | ownership 不能授予 shell/Android 权限 | Claim 后直接执行任意 payload | Executor 只有 tiny allowlist；Harness 仍重新授权 | [bridge/claimer.py](../../mini_harness_core/bridge/claimer.py)、[bridge/executor.py](../../mini_harness_core/bridge/executor.py) |
| 4 | History immutable | overwrite 会抹掉 crash/retry truth | 更新单个 mutable task state | records append/no-replace；状态由历史推导 | [bridge/paths.py](../../mini_harness_core/bridge/paths.py)、[integrity.py](../../mini_harness_core/integrity.py) |
| 5 | State derived from history | mtime/Session/timestamp 不可靠 | Worker 持有 mutable queue state | Inspector 是 Bridge state 的单一推导点 | [bridge/inspector.py](../../mini_harness_core/bridge/inspector.py) |
| 6 | Timestamp ≠ lease authority | wall clock 不可信且跨环境漂移 | 以 claimed_at 超时抢占 | V1 stale lock 只人工 recovery | [bridge/claimer.py](../../mini_harness_core/bridge/claimer.py) |
| 7 | Binding before Run | Run 必须先拥有 durable transport↔run identity | Run 后补 Binding | crash 后复用 fixed session/run IDs | [integrations/bridge_adapter.py](../../mini_harness_core/integrations/bridge_adapter.py) |
| 8 | Bridge Result ≠ Harness Evidence | transport output 不是执行真实性证明 | 把 Bridge Result 自动 accepted | 只允许 Harness Result 单向投影 | [integrations/bridge_adapter.py](../../mini_harness_core/integrations/bridge_adapter.py)、[evidence.py](../../mini_harness_core/evidence.py) |
| 9 | Bridge completed ≠ Harness completed | Bridge outer status 表示 transport commitment | 把外层 completed 当业务成功 | projection 携带独立 `harness_result_status` | [integrations/bridge_adapter.py](../../mini_harness_core/integrations/bridge_adapter.py) |
| 10 | Environment capability existence ≠ permission | 本机 executable 存在不授予 Agent Authority | Registry entry 自动 ALLOW | capability 仍经过完整 Harness chain | [environment/registry.py](../../mini_harness_core/environment/registry.py)、[agent.py](../../mini_harness_core/agent.py) |
| 11 | Effect ≠ disposition | read-only/side-effecting 是分类，不是 Policy 结果 | read-only 自动 ALLOW、side effect 自动 DENY | Global/Zone/Profile/Delegation composition 决定 ALLOW/ASK/DENY | [policy_composition.py](../../mini_harness_core/policy_composition.py) |
| 12 | Adapter Result ≠ Evidence | Adapter 只知道 mechanics 与 safe observation | Adapter 自己接受 Evidence | Harness 创建、验证并持久化 Evidence | [environment/contracts.py](../../mini_harness_core/environment/contracts.py)、[agent.py](../../mini_harness_core/agent.py) |
| 13 | Request accepted ≠ user seen | notification exit 0 不能证明用户感知 | 生成 `user_seen=true` | Evidence 只支持 request acceptance | [environment/termux.py](../../mini_harness_core/environment/termux.py) |
| 14 | Unknown side effect ≠ retry permission | 重试可能重复真实 Android effect | timeout 统一普通 retry | unknown 进入 reconcile/block；read-only 才可 bounded retry | [durability.py](../../mini_harness_core/durability.py)、[retry.py](../../mini_harness_core/retry.py) |
| 15 | Registry Harness-owned | Model/MCP/Subagent metadata 不可信 | 动态 plugin/discovery 或外部注册 | static closed registry、unknown fail closed、manifest fingerprint | [environment/registry.py](../../mini_harness_core/environment/registry.py)、[run_manifest.py](../../mini_harness_core/run_manifest.py) |
| 16 | Attempt execution and reconciliation are mutually exclusive | Bridge history alone cannot distinguish live execution from crash recovery | 允许 operator 与 executor 同时推进 | atomic attempt fence；残留锁人工恢复 | [bridge/attempt_fence.py](../../mini_harness_core/bridge/attempt_fence.py) |
| 17 | Environment certainty drives checkpoint truth | exit code 不能表达 side-effect ambiguity | 每个 handler 自己猜 outcome | 单一 certainty→checkpoint mapping，unknown 永不降为 failed | [dispatch.py](../../mini_harness_core/dispatch.py) |
| 18 | Integration projection requires durable Harness truth | caller repair payload 不拥有 Harness Result Authority | generic repair 完成 bridge_harness_task | generic repair fail closed；projection-only repair读取 Binding/Result | [integrations/bridge_adapter.py](../../mini_harness_core/integrations/bridge_adapter.py)、[bridge/result_repairer.py](../../mini_harness_core/bridge/result_repairer.py) |
| 19 | Observation truth survives Evidence-store failure | persistence failure 不能反写已完成的 Environment action，也不能授权重调 capability | 把 Evidence failure 当 action failure/retry | deterministic Evidence-only repair 绑定 durable checkpoint + safe Audit Observation；缺一 fail closed | [integrations/bridge_adapter.py](../../mini_harness_core/integrations/bridge_adapter.py)、[evidence.py](../../mini_harness_core/evidence.py) |
| 20 | Attempt fence covers projection commitment | terminal Harness Result 与 Bridge ready 之间仍有并发 recovery 窗口 | Result durable 后立即释放 Run fence | fence 持有至 ready；immutable terminal marker 永久排除 Reconciler | [bridge/attempt_fence.py](../../mini_harness_core/bridge/attempt_fence.py)、[integrations/bridge_adapter.py](../../mini_harness_core/integrations/bridge_adapter.py)、[bridge/reconciler.py](../../mini_harness_core/bridge/reconciler.py) |
| 21 | Condition evaluation belongs to Harness, not Model | 数学 branch 是 completion/security gate | 让 Model 自由解释 percentage/threshold | 固定 integer `lt` evaluator，可 deterministic replay | [integrations/mobile.py](../../mini_harness_core/integrations/mobile.py) `evaluate_battery_condition()` |
| 22 | Conditional action depends on accepted fresh Evidence | raw/historical/other-run state 不能证明当前电量 | 从 Adapter return、Bridge payload 或 Session history 直接 branch | Battery Evidence 必须 accepted、run-scoped、current-run | [integrations/mobile.py](../../mini_harness_core/integrations/mobile.py) `condition_allows_notification()` |
| 23 | Step dependency does not transfer Authority | ordering correlation 不是 execution permission | battery Step completed 后直接 dispatch notification | notification 仍经过 Policy、gates、Approval、AuthorizedAction | [planning.py](../../mini_harness_core/planning.py)、[agent.py](../../mini_harness_core/agent.py) |
| 24 | Observation step Approval cannot authorize action step | Approval 必须绑定 exact current action | 复用 battery/旧 checkpoint Approval | condition 后 crash 恢复为 fresh notification Approval | [durability.py](../../mini_harness_core/durability.py) `recover_action_checkpoint()`、[agent.py](../../mini_harness_core/agent.py) |
| 25 | Bridge claim spans workflow transport, not per-step authority | 一个用户 task 应绑定一个 Run，但 claim 只拥有 transport | 每 Step 新 claim，或 claim 自动授权所有 Step | one claim/Binding/Run；每 action 单独授权 | [integrations/bridge_adapter.py](../../mini_harness_core/integrations/bridge_adapter.py) `run_bound_bridge_request()` |
| 26 | Conditional Output Contract depends on the branch actually taken | false branch 不应伪造 notification artifact，true branch 不应掩盖未满足 action | 所有 branch 使用同一 Evidence requirement | not-required/accepted satisfied；denied incomplete；unknown blocked | [integrations/mobile.py](../../mini_harness_core/integrations/mobile.py) `build_mobile_workflow_output()` |
| 27 | Model Candidate ≠ Harness Authoritative Candidate ≠ Result | mobile/output contracts 可确定性归一化 presentation，但 Model 不拥有 terminal truth | 让 Result 继续引用最早的 Model digest | 分别记录 model received、Harness finalized 与 Result fingerprint | [agent.py](../../mini_harness_core/agent.py) `_handle_final_candidate()`、`_persist_authoritative_candidate_finalized()`；[result.py](../../mini_harness_core/result.py) |
| 28 | Candidate normalization durable before Result binding | crash 或 stale identity 不能让 Result 绑定未持久化/错误 candidate | Result 先发布，再补 candidate Audit；或回写旧 event | finalized candidate event 先 fsync；live/replay 共用纯 digest derivation | [result.py](../../mini_harness_core/result.py) `finalize_authoritative_candidate()`、`result_integrity_check()` |
| 29 | Bridge projection requires Result integrity MATCH | schema-valid Result 仍可能拥有错误 cross-record identity | 只调用 `ResultStore.load()` 就投影 | terminal marker/result JSON/ready 前执行 integrity gate，失败返回 recovery required | [integrations/bridge_adapter.py](../../mini_harness_core/integrations/bridge_adapter.py) `_harness_result_integrity_valid()` |

## Additional consequences

- Worker composition convenience 不增加 Authority。
- Schema evolution 可以改变 reader，但不能重写 task-001/002 历史。
- Historical replay 只能检查历史 identity/decision，不读取 Current Bridge 或重调 Android。
- Registry fingerprint 记录 capability specs/adapter versions，不记录当前 battery state 或 Android permission state。
- shared-storage 行为是设备实验前提，不是跨 Android 形式保证。
- Evidence-only recovery 不凭历史 digest 构造缺失的 Observation；没有 safe
  Observation identity 时必须人工处理。
- attempt fence 是 concurrency protection，不是 Policy、Approval、lease 或 execution Authority。
- Mobile workflow resume 是固定 structured task 的窄恢复路径，不把普通 BridgeHarness
  Run 扩张为自动 action replay。

## Alternatives Considered for P2.7

- **每个 Step 创建 Bridge claim：** 被否定；会把 transport attempt 与 Harness action
  Authority 混在一起，并破坏 one task ↔ one Run identity。
- **让 Model 判断 `percentage < threshold`：** 被否定；无法 deterministic replay，也会让
  Model claim 影响 completion gate。
- **把 battery ALLOW/Approval 复用给 notification：** 被否定；违反 exact-action Authority。
- **新增通用 workflow state machine/DSL：** 被否定；第一版只有固定两个 Step，现有
  Plan/Step/Evidence/Result 加窄 condition correlation 足够。
- **Approval denied 记为 completed-without-notification：** 被否定；当前 strict conditional
  contract 把 required-but-not-authorized 记录为 unsatisfied，因此 Result 是 incomplete。
- **让 `final_candidate_received` 同时代表 Model 与 normalized candidate：** 被否定；首次
  Android workflow smoke 已证明两个 identity 可不同，一个 event 不能承担两个角色。
- **回写旧 `final_candidate_received` digest：** 被否定；会破坏 append-only Audit 和旧 Run
  可审计性。新 Run 写 `authoritative_candidate_finalized`，旧 Run 保持 legacy check。
- **为 mobile Result 新增专用 schema：** 被否定；candidate finalization 是通用 Result
  boundary。普通 Run 无 rewrite 时 identity 可相等，不需要 mobile-only Result 类型。

## Change discipline

改变上述决策必须同时更新：

1. 对应 schema/contract；
2. Authority 与 crash invariants；
3. deterministic regression/E2E；
4. Manifest/replay compatibility；
5. 本目录与 Phase 1 相关文档。

不能仅为了通过测试降低 fail-closed 行为。
