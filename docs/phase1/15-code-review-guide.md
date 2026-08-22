# Code Review Guide：按边界读 Mini Harness

## 读完你应该理解什么

- 第一次下载仓库后，如何选择一条与自己目标匹配的源码阅读路线。
- 如何从真实 function、module 和 deterministic test 定位 Runtime ownership boundary。
- 如何审查 authority bypass、stale truth、blind replay、secret persistence 与虚假 completion。

本篇不是通用 Python code-quality checklist。它只检查 Mini Harness 自己承诺的 Authority、Durability、
Historical Reality 和 Result invariants。

## 开始前

先运行离线基线：

```bash
python -m unittest -q
python mini_harness.py --self-check
```

默认入口是 [`mini_harness.py`](../../mini_harness.py)，核心实现位于
[`mini_harness_core/`](../../mini_harness_core/)，系统场景位于
[`test_end_to_end_runtime.py`](../../tests/e2e/test_end_to_end_runtime.py)。不要从 README 中的版本叙述反推当前行为；
源码、schema validation 和 deterministic assertions 才是 review grounding。

## A. Beginner：看懂一个最小 Run

目标：理解 Task 如何经过 Model decision、Harness Authority 和 Environment Observation，最后形成 Result。

### 1. 从 deterministic Provider 开始

- 读 [`providers.py`](../../mini_harness_core/providers.py) 的 `FakeProvider.complete`。
- 再读 [`test_mini_harness.py`](../../tests/integration/test_mini_harness.py) 的 `FakeProviderRegressionTests`。

先确认 Provider 只返回 decision，不执行 Tool、不授予 Authority，也不拥有完整 Session/Context assembly。

### 2. 找到 public entry 与真实 loop

- 读 [`agent.py`](../../mini_harness_core/agent.py) 的 `run_agent`。
- 紧接着读 `_run_agent_runtime`、`_initialize_runtime_execution`、`_prepare_runtime_turn`。
- 对照 [`02-agent-loop.md`](02-agent-loop.md) 的 phase 图。

重点不是记住参数，而是看出：`run_agent` 是薄入口，orchestrator 路由 phase helper，Model decision 只是输入。

### 3. 跑通 Golden E2E

- 读 [`test_end_to_end_runtime.py`](../../tests/e2e/test_end_to_end_runtime.py) 的
  `test_01_golden_success_lineage_and_offline_bundle`。
- 顺着 assertion 找到 Audit、Envelope、Evidence、Artifact、Result 和 Bundle store。

这个 test 是最短的跨模块地图；它不代表每个 V28 scenario 都是完整 `run_agent` 链。

### 4. 再读 Authority

- [`authority.py`](../../mini_harness_core/authority.py)：`classify_shell`、`request_approval`。
- [`policy_composition.py`](../../mini_harness_core/policy_composition.py)：`compose_static_policy`、
  `compose_subagent_policy`。
- [`dispatch.py`](../../mini_harness_core/dispatch.py)：`authorize_action`。
- 测试：[`test_policy_composition.py`](../../tests/unit/test_policy_composition.py) 和
  [`test_v26_boundary.py`](../../tests/security/test_v26_boundary.py) 的 `AuthorizedDispatchTests`。

### 5. 最后读 Action Lifecycle

- [`durability.py`](../../mini_harness_core/durability.py)：`create_action_checkpoint`、
  `transition_action_checkpoint`、`recover_action_checkpoint`。
- [`dispatch.py`](../../mini_harness_core/dispatch.py)：`dispatch_authorized_action`。
- 测试：[`test_v26_failure_semantics.py`](../../tests/security/test_v26_failure_semantics.py) 的
  `test_dispatch_crash_points_preserve_forward_truth`。

读完 Beginner 路线，应能解释 `ASK` 为什么不等于 side effect，以及 Tool success 为什么不等于 Run completed。

## B. Intermediate：沿执行链读 control flow

目标：理解每一层回答不同问题，且只有 Harness 能把 Model Intent 变成 executable action。

```text
Model Decision
  -> Classification / Effect
  -> Static Policy Composition
  -> Capability + Runtime Gates
  -> Human Approval when ASK
  -> AuthorizedAction
  -> dispatch + Tool/MCP/Subagent
  -> safe Observation
  -> Verification / Reconciliation
  -> Planning / Retry / Governance
  -> Evidence / Artifact / Output Contract
  -> Authoritative Result
```

| Stage | 读什么 | 关键 function | 对照 test |
|---|---|---|---|
| Model Decision | [`providers.py`](../../mini_harness_core/providers.py)、[`agent.py`](../../mini_harness_core/agent.py) | `FakeProvider.complete`、`_prepare_runtime_turn`、`_handle_shell_decision`、`_handle_mcp_decision` | `FakeProviderRegressionTests`、V28 golden |
| Classification / Policy | [`authority.py`](../../mini_harness_core/authority.py)、[`policy_composition.py`](../../mini_harness_core/policy_composition.py) | `classify_shell`、`compose_static_policy`、`policy_for` | `ToolPolicyTests`、`PolicyCompositionV18Tests` |
| Runtime gates | [`governance.py`](../../mini_harness_core/governance.py)、[`run_control.py`](../../mini_harness_core/run_control.py) | `normal_action_decision`、`can_schedule_action`、`safety_reconciliation_decision` | `ExecutionGovernanceV16Tests`、V28 deadline |
| Approval / authorization | [`authority.py`](../../mini_harness_core/authority.py)、[`dispatch.py`](../../mini_harness_core/dispatch.py) | `request_approval`、`authorize_action` | `ApprovalGateTests`、`AuthorizedDispatchTests` |
| Dispatch | [`dispatch.py`](../../mini_harness_core/dispatch.py) | `dispatch_authorized_action` | `FailureSemanticsV26Tests` |
| Observation | [`observation.py`](../../mini_harness_core/observation.py)、[`context.py`](../../mini_harness_core/context.py) | `persisted_safe_observation`、`model_context_observation`、`project_observations_for_model` | `ObservationProjectionTests` |
| Verification / recovery | [`verification.py`](../../mini_harness_core/verification.py)、[`durability.py`](../../mini_harness_core/durability.py) | `replay_verification_transition`、`reconcile_file_observation` | `VerificationQualityTests`、V28 crash |
| Planning / Retry | [`planning.py`](../../mini_harness_core/planning.py)、[`retry.py`](../../mini_harness_core/retry.py) | `select_ready_step`、`complete_step`、`decide_retry` | `PlanningRuntimeIntegrationTests`、`RetryV15Tests` |
| Delivery | [`evidence.py`](../../mini_harness_core/evidence.py)、[`artifacts.py`](../../mini_harness_core/artifacts.py)、[`result.py`](../../mini_harness_core/result.py) | `evidence_gate`、`current_output_contract_gate`、`bind_final_result` | `EvidenceTests`、`OutputContractTests`、`ResultStatusTests` |

Intermediate review 时，每跨过一列都问一次：“这个 module 是产生事实、记录事实，还是决定是否允许执行？”

## C. Deep Review：逆向审查安全承诺

目标：不从 happy path 开始，而从最危险的 terminal claim 和 executor 反向寻找 bypass。

### 1. Final Result：模型能否自封 completed

- 从 [`result.py`](../../mini_harness_core/result.py) 的 `bind_final_result`、
  [`historical_types.py`](../../mini_harness_core/historical_types.py) 的 `evaluate_result_transition` 向上追。
- 对照 [`test_result.py`](../../tests/unit/test_result.py) 的 `ResultStatusTests`、`ResultClaimAndSecurityTests`。

### 2. Bundle / Replay：历史检查能否触发外部执行

- 读 [`run_bundle.py`](../../mini_harness_core/run_bundle.py) 的 `BundleHistoricalResolver`、
  `collect_reference_closure`、`check_bundle`、`replay_bundle`。
- 读 [`run_envelope.py`](../../mini_harness_core/run_envelope.py) 的 `harness_replay_check`。
- 对照 `RunBundleV25Tests.test_bundle_resolver_never_falls_back_and_has_no_authority_api`。

### 3. Historical Integrity：完整性是否被误当 freshness

- 读 `evidence_integrity_check`、`artifact_integrity_check`、`result_integrity_check`。
- 再读 `evidence_gate` 与 `current_output_contract_gate`，比较 Historical Check 和 Current Reality Gate。
- 对照 `EvidenceTests.test_historical_evidence_replays_without_current_filesystem` 和
  `ResultPersistenceReplayAndAuditTests.test_current_filesystem_change_does_not_affect_historical_result`。

### 4. Failure / Recovery：unknown 是否被普通 retry 吞掉

- 读 [`dispatch.py`](../../mini_harness_core/dispatch.py) 的 post-tool ordering。
- 读 [`durability.py`](../../mini_harness_core/durability.py) 的 recovery 与 Reconciliation。
- 读 [`retry.py`](../../mini_harness_core/retry.py) 的 `decide_retry`、
  `reopen_retry_after_reconciliation`。
- 对照 V26 crash tests 与 V28 Scenario 3。

### 5. Dispatch Seam：是否存在 executor bypass

- 从所有 executor call site 反查是否都需要 sealed `AuthorizedAction`。
- 对照 [`test_v27_architecture.py`](../../tests/architecture/test_v27_architecture.py) 的
  `test_authority_order_and_dispatch_boundary_remain_explicit`，以及 `AuthorizedDispatchTests`。

### 6. Policy Ceiling：Authority 是否可能被项目内容或委派放大

- 读 `compose_static_policy`、`delegated_ceiling`、`compose_subagent_policy`。
- 读 [`project_context.py`](../../mini_harness_core/project_context.py)；确认它不进入 Authority input。
- 对照 `PolicyCompositionV18Tests.test_project_context_has_no_authority_input` 和
  `StructuredHandoffTests.test_allowed_tools_and_main_authority_only_reduce`。

## Authority Review

- [ ] 所有 Tool/MCP/Subagent executor 是否只能从 `dispatch_authorized_action` 到达？
- [ ] `authorize_action` 是否要求 genuine sealed `AuthorizedAction` 所需的 exact checkpoint binding？
- [ ] `DENY`、capability ceiling、protected path 是否都在 Approval 前阻断？
- [ ] Approval waiting 后是否重新检查 Run Control、deadline、budget 和 current authority facts？
- [ ] Historical Approval 是否可能为新 attempt 或新 Run 提供 reusable authority？
- [ ] `compose_static_policy` 是否保持 `DENY > ASK > ALLOW`？
- [ ] Delegated Authority、Profile 与 allowed-tools intersection 是否只能衰减？
- [ ] MCP description、Project Instructions 或 Skill body 是否可能抬高 Effect/Policy？

## Durability Review

- [ ] `prepared` 与 `executing` 是否都在 side effect 前成功持久化？
- [ ] executor 返回后，store failure 是否保留 forward Tool truth？
- [ ] durable `executing` 在 resume 时是否无条件变成 `unknown`？
- [ ] unknown non-read-only effect 是否可能进入普通 transient Retry？
- [ ] Reconciliation 是否 read-only、targeted，并只在 `not_applied` 后重开 retry gate？
- [ ] Evidence/Artifact/Result 缺失是否可能通过重做原副作用“修复”？
- [ ] process crash 与 caught persistence failure 是否被错误合并成 Degraded？
- [ ] 单次 dispatch call count 是否被误宣传成通用 exactly-once？

## Secret Boundary Review

- [ ] `.env.local`、protected path 和 symlink escape 是否在 executor 前 fail closed？
- [ ] raw stdout/stderr/MCP result 是否先经过 `persisted_safe_observation` 再进入 Session/store？
- [ ] Provider context 是否再次经过 `model_context_observation`/context projection？
- [ ] Audit、Evidence、Artifact、Result、Envelope、Bundle validator 是否拒绝 secret-bearing fields/text？
- [ ] command identity 是否保存 digest/shape，而不是 raw dangerous payload？
- [ ] MCP late completion journal 是否只保存 historical-safe projection？
- [ ] direct executor/forged `AuthorizedAction` test 是否保持 0 calls？

## Historical Replay Review

- [ ] Policy replay 是否加载 Historical Snapshot，而不把它激活为 Current Policy？
- [ ] Envelope replay 是否只调用 pure transition helpers？
- [ ] `LocalHistoricalResolver`/`BundleHistoricalResolver` 是否没有 executor/Approval API？
- [ ] Bundle resolver 是否禁止 fallback 到 local `.audit`？
- [ ] Bundle 是否只读取 index 内 regular files，并拒绝 symlink/path escape？
- [ ] Envelope fingerprint 是否被正确理解为 initial-input identity，而非 whole live record hash？
- [ ] Audit sequence/object refs/Bundle byte hash 是否没有被误称为签名或 hash chain？
- [ ] Historical MATCH 是否始终与 Current Reality gate 分开？

## Result / Artifact Review

- [ ] Model `claimed_status` 是否只能成为 candidate metadata？
- [ ] `evaluate_result_transition` 是否保持 cancelled/failed/blocked/incomplete/completed precedence？
- [ ] accepted Artifact 是否绑定 Evidence、producer action 和 Output Contract requirement？
- [ ] `current_output_contract_gate` 是否重新读取当前 workspace content identity？
- [ ] supersession 是否创建新 immutable Artifact，而不是改写旧记录？
- [ ] rejected/superseded Artifact 是否可能进入 completed Result closure？
- [ ] Output Contract unsatisfied + final candidate 是否 terminalize 为 `incomplete`？
- [ ] Result persistence/Audit failure 是否可能反向重做 Provider 或 Tool？

## Review 输出应包含什么

一次有效 review 至少应给出：具体 invariant、真实 function、可复现 test、最后 durable/current truth，以及问题会
导致 authority bypass、stale truth、duplicate effect、secret leak 还是 false completion。仅写“逻辑复杂”或
“建议重构”不能证明边界有问题。

## Navigation

- Previous: [`14-testing-strategy.md`](14-testing-strategy.md)
- Next: [`16-design-decisions.md`](16-design-decisions.md)
- Related: [`02-agent-loop.md`](02-agent-loop.md)、[`03-authority-and-policy.md`](03-authority-and-policy.md)、
  [`13-failure-semantics.md`](13-failure-semantics.md)
