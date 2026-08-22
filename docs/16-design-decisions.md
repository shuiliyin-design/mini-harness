# Design Decisions：为什么 Runtime 长成这样

## 读完你应该理解什么

这些决定不是生产框架建议，而是当前教学 Harness 为了保持 Authority、Current Reality、durability 和历史
可解释性所做的取舍。每项都给出真实实现位置和改变决定所需的新条件。

## 1. Intent 不等于 Authority

- **Decision**：Model decision 只表达 Intent；Harness 独立决定能否执行。
- **Context**：模型输出可能错误、被提示注入影响或使用过时上下文。
- **Alternatives Considered**：Provider 直接调用 Tool；把 tool schema 暴露等同于授权。
- **Why Chosen**：执行许可必须由可测试、确定性的 Harness boundary 拥有。
- **Consequences**：每个 executable decision 都要重新经过 classification、Policy、runtime gates 和 authorization。
- **Where Implemented**：[`agent.py`](../mini_harness_core/agent.py) `_handle_shell_decision`/`_handle_mcp_decision`；
  [`dispatch.py`](../mini_harness_core/dispatch.py) `authorize_action`；`ApprovalGateTests`。
- **What Would Change This Decision**：只有把 Model 明确定义为同一 trusted computing base，才会改变；当前项目不做。

## 2. Policy 与 Effect 分离

- **Decision**：`ALLOW/ASK/DENY` 与 `read_only/side_effecting/unknown` 是正交维度。
- **Context**：read-only action 也可能需要 Approval；被 ALLOW 的 write 仍需 Verification。
- **Alternatives Considered**：用 `ASK` 直接表示 side effect；从 disposition 推导 Effect。
- **Why Chosen**：Approval requirement、durability 和 Verification 回答不同问题。
- **Consequences**：组合结果必须同时携带 disposition 与 Effect；invalid Effect fail closed 为 `unknown`。
- **Where Implemented**：[`authority.py`](../mini_harness_core/authority.py) `classify_shell`；
  [`policy_composition.py`](../mini_harness_core/policy_composition.py) `compose_static_policy`；
  `test_zone_ask_readonly_uses_approval_without_verification`。
- **What Would Change This Decision**：只有所有 capability 的风险与交互要求永远一一对应时才可合并；当前反例已存在。

## 3. `DENY > ASK > ALLOW`

- **Decision**：静态组合取最严格 disposition。
- **Context**：Global、Trust Zone、Profile、Delegation 和 local mapping 可能给出不同结论。
- **Alternatives Considered**：last-writer-wins；多数投票；Approval 覆盖 DENY。
- **Why Chosen**：安全层只能收紧，不能因层顺序或用户确认绕过 ceiling。
- **Consequences**：任一 DENY 使 Final Authorization 失败，且 Approval 不出现。
- **Where Implemented**：`compose_static_policy`、`DECISION_ORDER`；
  [`test_policy_composition.py`](../test_policy_composition.py)。
- **What Would Change This Decision**：若未来引入显式、独立审计的 policy override layer，需要重新定义；本阶段禁止。

## 4. Capability Authority 只能衰减

- **Decision**：Profile、Delegation 和 subagent effective capability 使用 intersection/ceiling，只能减少权限。
- **Context**：子任务或 project hint 不能比 parent 获得更多 Tool/write/MCP 权限。
- **Alternatives Considered**：子代理按角色名获得新权限；delegation union。
- **Why Chosen**：委派不能成为 authority escalation path。
- **Consequences**：parent `write=false` 时，即使 profile `write=true`，Final Authorization 仍 DENY。
- **Where Implemented**：`delegated_ceiling`、`compose_subagent_policy`；
  `StructuredHandoffTests.test_allowed_tools_and_main_authority_only_reduce`。
- **What Would Change This Decision**：需要独立 principal、独立 credential 与显式 grant protocol；当前没有。

## 5. Approval 不继承、不复用

- **Decision**：ASK 的每个新 attempt/Run 需要 fresh Approval。
- **Context**：等待期间 deadline、Run Control、arguments 或 Current Reality 可能变化。
- **Alternatives Considered**：按 command 缓存 Approval；resume 自动继承历史批准。
- **Why Chosen**：Approved earlier 不等于 Authorized forever。
- **Consequences**：Approval 前保存 `prepared`；返回后重新检查关键 gates；拒绝不消费执行 attempt。
- **Where Implemented**：`request_approval`、`authorize_action`、`_handle_shell_decision`；
  `test_prepared_ask_action_requires_fresh_approval`、V28 pause/resume。
- **What Would Change This Decision**：需要有 scope、expiry、arguments binding 的独立 reusable grant object；当前不存在。

## 6. `executing` checkpoint 必须先于 side effect durable

- **Decision**：dispatch 在 executor 前持久化 `prepared` 和 `executing`。
- **Context**：先执行后记录会让 crash resume 把已发生副作用误认成未开始。
- **Alternatives Considered**：Tool 完成后一次性写 terminal checkpoint；只写 Audit started event。
- **Why Chosen**：最后 durable `executing` 能诚实表达“可能已发生”。
- **Consequences**：任一 pre-tool checkpoint write 失败时 executor call count 必须为 0。
- **Where Implemented**：[`dispatch.py`](../mini_harness_core/dispatch.py) `dispatch_authorized_action`；
  `AuthorizedDispatchTests`、`FailureSemanticsV26Tests`。
- **What Would Change This Decision**：若 executor 与 checkpoint store 共享原子事务，可采用事务协议；当前不共享。

## 7. Unknown side effect 必须先 Reconciliation

- **Decision**：non-read-only `unknown` 不进入 blind retry。
- **Context**：timeout、crash 或 lost terminal write 不能证明外部效果未发生。
- **Alternatives Considered**：把 timeout 当 transient；直接重复原 command。
- **Why Chosen**：错误重放可能造成重复或不可逆副作用。
- **Consequences**：只允许 targeted read-only Reconciliation；仅 `not_applied` 可重开 retry gate。
- **Where Implemented**：[`durability.py`](../mini_harness_core/durability.py) `recover_action_checkpoint`、
  `reconcile_file_observation`；[`retry.py`](../mini_harness_core/retry.py) `decide_retry`。
- **What Would Change This Decision**：若 capability 提供可信 idempotency key/transaction status API，可增加专用 reconciliation contract。

## 8. Retry 是 bounded Harness policy

- **Decision**：failure classification、attempt budget、backoff 与 retry state 由 Harness 管理。
- **Context**：让 Model 自由重复 action 会绕过 budget、durability 和 Approval。
- **Alternatives Considered**：模型看到 failure 后自行决定重试；无限 retry。
- **Why Chosen**：retry 必须 deterministic、可持久化、可审计并受 Governance 限制。
- **Consequences**：默认有限 attempts；每次 retry 是新 action/attempt，并重新过 current gates。
- **Where Implemented**：[`retry.py`](../mini_harness_core/retry.py) `classify_failure`、`decide_retry`、
  `record_failure`；`RetryV15Tests`。
- **What Would Change This Decision**：若引入新的 scheduler，仍必须保留相同 owner contract，而不是交给 Model。

## 9. User Pause 冻结 active deadline

- **Decision**：显式 user pause 与 Approval waiting 保存剩余 active duration，resume 后重建 deadline。
- **Context**：用户思考时间不应消耗可执行工作预算。
- **Alternatives Considered**：wall-clock deadline 始终流逝；resume 重置整个 timeout。
- **Why Chosen**：前者惩罚人类审批，后者扩大原 budget。
- **Consequences**：pause 冻结时间但不重置 action/attempt counters。
- **Where Implemented**：[`governance.py`](../mini_harness_core/governance.py) `freeze_governance`、
  `resume_governance`；`test_pause_resume_and_repeated_pause_do_not_add_active_time`。
- **What Would Change This Decision**：若 deadline 被定义为外部 SLA wall clock，pause policy 需要显式改写。

## 10. Running crash downtime 不冻结 deadline

- **Decision**：只有显式 frozen state 保存 remaining duration；普通 crash 后 UTC deadline 继续流逝。
- **Context**：进程消失不等于用户授权暂停，也不能凭 resume 获得更多运行时间。
- **Alternatives Considered**：所有 downtime 自动冻结；resume 重置 deadline。
- **Why Chosen**：持久化 UTC deadline 才能在新进程中保持原治理上限。
- **Consequences**：长时间 crash 后 resume 可能立即 blocked；paused crash 则保持冻结值。
- **Where Implemented**：`run_remaining`、`deadline_status`、`resume_governance`；
  `test_running_crash_consumes_but_paused_crash_does_not`。
- **What Would Change This Decision**：只有引入外部 lease/scheduler 并明确拥有 downtime accounting 才会改变。

## 11. Deadline 后保留一次 Safety Reconciliation

- **Decision**：normal work 到期后，只为既有 unknown side effect 提供一次 targeted read-only permit。
- **Context**：完全停止会永久保留不确定性；继续 normal work 又突破 deadline。
- **Alternatives Considered**：到期后一律拒绝所有 action；允许任意 read-only diagnostics。
- **Why Chosen**：以最小额外执行降低安全不确定性，而不恢复生产性工作。
- **Consequences**：permit 不能绕过 Security/DENY，不能 retry/advance Plan，完成后 Result 仍 blocked/cancelled。
- **Where Implemented**：`safety_reconciliation_decision`、`consume_safety_reconciliation`；
  `test_safety_reconciliation_is_read_only_related_and_once`、V28 Scenario 6。
- **What Would Change This Decision**：若外部系统提供零执行的 authoritative status feed，可减少此 permit。

## 12. Audit 不是真实恢复源

- **Decision**：Audit 是 append-only observability plane；Action checkpoint/Run Control 才决定恢复。
- **Context**：Audit 可能在 terminal checkpoint 前后缺失或 torn。
- **Alternatives Considered**：扫描 Audit 重建 action state；把 event trace 当 event-sourced runtime。
- **Why Chosen**：当前 Audit 没有完整 event-sourcing contract、hash chain 或所有恢复字段。
- **Consequences**：Audit 可 explain/trace，但不能单独授权 retry/resume。
- **Where Implemented**：[`audit.py`](../mini_harness_core/audit.py) `AuditWriter`/`read_events`；
  `recover_action_checkpoint`；V26 crash tests。
- **What Would Change This Decision**：需要把 Audit 重构成经过事务验证的完整 event store；当前不做。

## 13. Policy Snapshot 不等于 Manifest

- **Decision**：Policy Snapshot 保存 authority definitions；Manifest 保存 Run configuration identity。
- **Context**：Policy replay 与 provider/project/memory drift 是不同问题。
- **Alternatives Considered**：把所有配置塞进 Policy Snapshot；只保存一个 run metadata blob。
- **Why Chosen**：Policy fingerprint 可跨 Run 内容寻址复用，Manifest 仍能绑定每次 Run 的其他配置。
- **Consequences**：Manifest 引用 Policy fingerprint；两者分别 check/replay。
- **Where Implemented**：[`policy_snapshot.py`](../mini_harness_core/policy_snapshot.py)、
  [`run_manifest.py`](../mini_harness_core/run_manifest.py)；对应 test modules。
- **What Would Change This Decision**：若所有 configuration 都成为 Authority input，边界才需重划；当前不成立。

## 14. Envelope 不归档 raw messages

- **Decision**：Envelope 保存 task/history/request/decision 的 identity 和 pure transition inputs/outputs，不保存正文。
- **Context**：deterministic replay 需要可绑定身份，但 raw context 可能含 secret、Memory 或项目内容。
- **Alternatives Considered**：归档完整 prompt/response；只保存 Audit。
- **Why Chosen**：digest 足以做 identity comparison，同时缩小 secret/history exposure。
- **Consequences**：Envelope replay 不能重建原对话正文，也不是 Session backup。
- **Where Implemented**：[`run_envelope.py`](../mini_harness_core/run_envelope.py) `build_envelope`、
  `RunEnvelopeStore.append_request`；`test_forbidden_raw_fields_are_rejected`。
- **What Would Change This Decision**：若增加明确加密、retention 与 consent 的 prompt archive，会是新的 historical object；本阶段不做。

## 15. Historical Evidence 不等于 Current Reality

- **Decision**：fingerprint/integrity 只证明历史 record；当前 filesystem claim 必须 fresh ground。
- **Context**：Evidence 创建后 workspace 可以漂移。
- **Alternatives Considered**：只要 Evidence MATCH 就允许完成；按时间戳猜 freshness。
- **Why Chosen**：历史自洽不能证明环境仍保持旧状态。
- **Consequences**：`evidence_gate` 检查 scope/Run；Output Contract 还用 `current_output_contract_gate` 重新观察文件。
- **Where Implemented**：[`evidence.py`](../mini_harness_core/evidence.py) `evidence_gate`；
  [`artifacts.py`](../mini_harness_core/artifacts.py) `current_output_contract_gate`；historical drift tests。
- **What Would Change This Decision**：只有 immutable external substrate 或可信 current attestation 能减少 fresh observation。

## 16. Artifact 是 immutable version identity

- **Decision**：文件改变时创建新 Artifact，并用 `supersedes_artifact_id` 连接旧版本。
- **Context**：改写历史 Artifact 会破坏已完成 Run 的 provenance。
- **Alternatives Considered**：path 对应一个 mutable record；删除旧版本。
- **Why Chosen**：历史 acceptance 与当前版本可以同时诚实存在。
- **Consequences**：Current Reality drift 不修改 A1；新观察产生 A2，supersession 必须同 path、不同 identity、无 cycle。
- **Where Implemented**：[`artifacts.py`](../mini_harness_core/artifacts.py) `create_artifact`、
  `validate_supersession`、`select_supersession`；`ArtifactLifecycleTests`。
- **What Would Change This Decision**：若 Artifact 变成 content-addressed external blob，记录结构可调整，但历史不可变原则仍保留。

## 17. Output Contract 拥有 deliverable acceptance

- **Decision**：Model/Tool success 不能自行宣布文件已交付；Output Contract 评估 required Artifact。
- **Context**：成功写空文件、错误路径或 stale Artifact 都不应完成任务。
- **Alternatives Considered**：command exit 0 即完成；Model final answer 决定 acceptance。
- **Why Chosen**：交付条件必须 deterministic、可 replay，并与 Current Reality gate 连接。
- **Consequences**：unsatisfied contract + final candidate 绑定 `incomplete` 并 terminalize 当前 Run。
- **Where Implemented**：`evaluate_artifact_contract`、`current_output_contract_gate`、`_handle_final_candidate`；
  `OutputContractTests`。
- **What Would Change This Decision**：若任务没有 deliverable contract，兼容 reactive run 仍可只依赖其他 gates。

## 18. Final Answer 不拥有 Completion Authority

- **Decision**：Model answer 是 candidate；Harness 绑定 Authoritative Result status/refs。
- **Context**：模型可在 retry exhausted、blocked 或 evidence 缺失时声称 completed。
- **Alternatives Considered**：原样返回 `final_answer`；用第二个模型 judge。
- **Why Chosen**：Harness state 可 deterministic 判断，模型 judge 仍非 Authority。
- **Consequences**：contradictory claim 被记录并替换为 safe result summary。
- **Where Implemented**：[`result.py`](../mini_harness_core/result.py) `bind_final_result`；
  [`historical_types.py`](../mini_harness_core/historical_types.py) `evaluate_result_transition`；V28 retry test。
- **What Would Change This Decision**：即使增加 semantic judge，它也只能提供 Evidence，不会直接获得 completion authority。

## 19. Bundle 是 portable history，不是 Authority

- **Decision**：Bundle resolver 永远 read-only，不提供 resume/import/execute API。
- **Context**：可移植 historical closure 很容易被误用成可执行包。
- **Alternatives Considered**：Bundle import 到 `.audit`；从 Bundle 恢复 Approval/Session。
- **Why Chosen**：历史 bytes 来自外部介质，不能自动成为 current trusted state。
- **Consequences**：offline check/replay 零外部执行；MATCH 不证明 Current Reality 或来源真实性。
- **Where Implemented**：[`run_bundle.py`](../mini_harness_core/run_bundle.py) `BundleHistoricalResolver`、
  `check_bundle`、`replay_bundle`；`test_bundle_resolver_never_falls_back_and_has_no_authority_api`。
- **What Would Change This Decision**：需要独立、显式授权的 import protocol 与 current revalidation；属于新功能。

## 20. Provider 不组装 Context

- **Decision**：Harness 的 `RuntimeContextAssembler` 准备最终 messages；Provider 只完成 transport/decision parsing。
- **Context**：Session、Memory、Project Context、active control 和 compaction 有不同 trust/lifecycle。
- **Alternatives Considered**：Provider 自己读取 stores；把完整 Session 直接传给 Provider。
- **Why Chosen**：context selection 是 Harness control plane，也必须可离线测量和安全投影。
- **Consequences**：Fake/Real Provider 共享 prepared-message contract；Provider 不拥有 continuity truth。
- **Where Implemented**：[`context.py`](../mini_harness_core/context.py) `RuntimeContextAssembler`；
  [`context.py`](../mini_harness_core/context.py)；`ContextMeasurementTests`。
- **What Would Change This Decision**：Provider adapter 可提供 token estimator，但不能接管 Authority/selection ownership。

## 21. Project Instructions 不能提高 Authority

- **Decision**：AGENTS/Skills 是 untrusted project context，只影响 Model working context/identity。
- **Context**：仓库内容可被任务或依赖修改，不能覆盖 Harness security policy。
- **Alternatives Considered**：允许 AGENTS 声明 Tool ALLOW；Skill 自动扩展 capability。
- **Why Chosen**：项目内容与执行安全策略处于不同 trust domain。
- **Consequences**：Project drift 进入 Manifest；Policy composition 输入不包含 project body。
- **Where Implemented**：[`project_context.py`](../mini_harness_core/project_context.py)、
  `build_configuration`；`test_project_instructions_cannot_change_policy_or_shell_environment`、
  `test_project_context_has_no_authority_input`。
- **What Would Change This Decision**：需要 Harness 管理的签名 policy extension，不是普通 project file。

## 22. Subagent 不能修改 Main Plan

- **Decision**：Subagent Return 是 candidate/evidence input；Main Harness 独占 Plan transition。
- **Context**：delegated runtime 有隔离 messages、steps 和 Authority ceiling，不能提交主状态突变。
- **Alternatives Considered**：共享 mutable Plan；接受子代理 `completed` 状态。
- **Why Chosen**：Main 必须 fresh ground 并验证 delegated output。
- **Consequences**：Subagent 可返回 structured actions/evidence summary，但 `subagent_result_evidence` 不完成主 step。
- **Where Implemented**：[`handoff.py`](../mini_harness_core/handoff.py)、
  [`planning.py`](../mini_harness_core/planning.py) `subagent_result_evidence`；
  `test_subagent_return_is_candidate_and_cannot_mutate_main_plan`。
- **What Would Change This Decision**：若引入共同事务协调器，需要新的 multi-run ownership model；当前不做。

## 23. Raw Observation 在 persistence/context 前投影

- **Decision**：stdout/stderr/result/error 先变成 allowlisted fields、length 和 digest，再跨边界。
- **Context**：Tool/MCP 可返回 credentials 或任意不可信正文。
- **Alternatives Considered**：先存 raw 再 redact；仅依赖日志过滤。
- **Why Chosen**：一旦 raw secret 进入 Session/Audit，就无法靠后续显示过滤撤回。
- **Consequences**：模型通常只能看到 safe identity；需要正文的验证在受控 handler 内完成。
- **Where Implemented**：[`observation.py`](../mini_harness_core/observation.py) `persisted_safe_observation`、
  `model_context_observation`；`ObservationProjectionTests`、V26 cross-store projection test。
- **What Would Change This Decision**：需要独立 secret vault/reference type，而不是扩大 persisted schema。

## 24. `AuthorizedAction` 是 dispatch seam

- **Decision**：executor 只接受 Harness 私有 seal 创建的 `AuthorizedAction` 与 exact prepared checkpoint binding。
- **Context**：普通 dict、旧 arguments 或旁路 helper 都不能代表最终授权。
- **Alternatives Considered**：每个 handler 自己判断后直接调用 executor；公开构造 authorization record。
- **Why Chosen**：集中最终 Authority boundary，便于结构测试和 defense-in-depth recheck。
- **Consequences**：plain dict/forged object fail closed；seal 本身不是跨进程一次性 token。
- **Where Implemented**：[`dispatch.py`](../mini_harness_core/dispatch.py) `AuthorizedAction`、`authorize_action`、
  `dispatch_authorized_action`；`AuthorizedDispatchTests`、V27 architecture test。
- **What Would Change This Decision**：若 dispatch 移到独立进程，需要可序列化、认证且防重放的 capability token。

## 25. Recovery 是 replay-safe，不是通用 exactly-once

- **Decision**：承诺 unknown side effect 不被 recovery/retry path blind replay，不承诺通用 exactly-once delivery。
- **Context**：Harness 无法控制 OS、remote service、executor 内部行为或并发 embedding。
- **Alternatives Considered**：把单次 test call count 宣传成 exactly-once；对所有 Tool 强制 idempotency。
- **Why Chosen**：当前可证明的是持久化顺序和保守 recovery，而不是外部系统原子性。
- **Consequences**：窄 file-write 可 Reconciliation；任意 MCP/外部 effect unknown 时可能长期 blocked。
- **Where Implemented**：`dispatch_authorized_action`、`recover_action_checkpoint`、
  `reconcile_file_observation`；V28 Scenario 3（其 assertion boundary 见
  [`14-testing-strategy.md`](14-testing-strategy.md)）。
- **What Would Change This Decision**：外部 idempotency key、transaction log 或 authoritative status API 可增加专用保证，但必须逐 capability 定义。

## Navigation

- Previous: [`15-code-review-guide.md`](15-code-review-guide.md)
- Next: [`17-glossary-and-state-reference.md`](17-glossary-and-state-reference.md)
- Related: [`01-architecture.md`](01-architecture.md)、[`13-failure-semantics.md`](13-failure-semantics.md)、
  [`18-version-learning-map.md`](18-version-learning-map.md)
