# Mini Harness Documentation Map

这些文档以当前源码、schema 和 deterministic tests 为准。推荐先读总览，再根据目标选择 Runtime control flow、
安全恢复或设计审查路线；不需要网络、在线模型或仓库外上下文。

## Phase 1：Core Harness Runtime

Phase 1 文档完整保存在 [`phase1/`](phase1/00-overview.md)。Core Harness 不依赖 Bridge、Termux 或 Android
才能成立；先从下面三篇开始：

- [`00-overview.md`](phase1/00-overview.md)：理解 Mini Harness 的教学目标、Model/Harness/Environment 三层与完整能力地图。
- [`01-architecture.md`](phase1/01-architecture.md)：理解 façade、CLI、orchestrator、leaf modules 和 dependency DAG。
- [`15-code-review-guide.md`](phase1/15-code-review-guide.md)：按 Beginner、Intermediate、Deep Review 三条路线进入真实源码和测试。

### Runtime Core

- [`02-agent-loop.md`](phase1/02-agent-loop.md)：跟踪 `run_agent -> _run_agent_runtime -> phase helpers` 的真实调用链。
- [`03-authority-and-policy.md`](phase1/03-authority-and-policy.md)：区分 Classification、Effect、Static Composition、Capability、Runtime Gate、Approval 与 AuthorizedAction。
- [`04-action-lifecycle.md`](phase1/04-action-lifecycle.md)：理解 Action checkpoint、dispatch ordering、Verification obligation 与 replay-safe recovery。
- [`05-planning-retry-governance.md`](phase1/05-planning-retry-governance.md)：区分任务下一步、是否允许新 attempt、当前预算是否允许继续。
- [`07-session-memory-context.md`](phase1/07-session-memory-context.md)：区分 continuity、长期 Memory、Provider working context 与 Current Reality。
- [`08-mcp-and-subagents.md`](phase1/08-mcp-and-subagents.md)：理解 MCP local mapping、Structured Handoff、Subagent isolation 与 Authority attenuation。

### Safety & Failure

- [`06-durability-and-recovery.md`](phase1/06-durability-and-recovery.md)：审查 crash window、Degraded、Reconciliation 和 no-blind-replay 保证边界。
- [`12-security-boundaries.md`](phase1/12-security-boundaries.md)：审查 protected paths、secret projection、dispatch seal 与 historical read-only boundary。
- [`13-failure-semantics.md`](phase1/13-failure-semantics.md)：区分 failure、unknown、pause、cancel、deadline、blocked、failed 与 incomplete。

### Historical / Replay

- [`09-audit-and-historical-objects.md`](phase1/09-audit-and-historical-objects.md)：理解八类 Historical Object、reference graph 与各自 fingerprint 精度。
- [`11-replay-and-bundles.md`](phase1/11-replay-and-bundles.md)：理解 identity check、deterministic Harness replay、resolver 与 external re-execution 禁区。

## Phase 2：Mobile / Bridge integration

Phase 1 学习路径保持不变。进入移动环境与跨环境 transport 前，建议先理解 Phase 1 的 Authority、durability、Evidence 和 replay。

- [`phase2/00-overview.md`](phase2/00-overview.md)：Phase 2 主线、Transport/Authority/Environment 三层边界与当前 baseline 状态。
- [`phase2/01-mobile-environment.md`](phase2/01-mobile-environment.md)：本设备 Android/Termux/PRoot/shared-storage 观察与非保证边界。
- [`phase2/02-bridge-protocol-v1.md`](phase2/02-bridge-protocol-v1.md)：Bridge v1 schema、commit protocol、derived state 与 recovery 三叉口。
- [`phase2/03-harness-bridge-adapter.md`](phase2/03-harness-bridge-adapter.md)：Binding、fresh Harness Run、Authority boundary 与 Result projection。
- [`phase2/04-environment-adapter-contract.md`](phase2/04-environment-adapter-contract.md)：Spec、Invocation、AdapterResult、certainty 与静态 registry。
- [`phase2/05-mobile-capabilities.md`](phase2/05-mobile-capabilities.md)：battery 与 notification 两个已实现 capability。
- [`phase2/06-recovery-and-failure-semantics.md`](phase2/06-recovery-and-failure-semantics.md)：三层 recovery、crash ownership、P2.6 closure 与 P2.7 resume 边界。
- [`phase2/07-testing-and-e2e.md`](phase2/07-testing-and-e2e.md)：deterministic tests、纵向 E2E 与真实 Android smoke 边界。
- [`phase2/08-design-decisions.md`](phase2/08-design-decisions.md)：26 个稳定 Phase 2 design decisions。
- [`phase2/09-review-guide.md`](phase2/09-review-guide.md)：Bridge、Harness Adapter 与 Environment capability 三条离线 review 路径。
- [`phase2/10-mobile-agent-orchestration.md`](phase2/10-mobile-agent-orchestration.md)：单 Bridge Run 内 Observe → Condition → Act → Verify → Deliver、conditional contract 与 crash resume。

Phase 2 是真实 integration environment，不是最终产品方向。实现分别位于
`mini_harness_core/bridge/`、`environment/` 和 `integrations/`。

## Phase 3：Applications

- [`phase3/README.md`](phase3/README.md)：AI Digest Subscription Agent 设计导航。
- [`phase3/00-overview.md`](phase3/00-overview.md)：产品链路、ownership 与推荐 app tree。
- [`phase3/02-domain-model.md`](phase3/02-domain-model.md)：Domain objects 与 SQLite persistence model。
- [`phase3/03-subscription-schema.md`](phase3/03-subscription-schema.md)：正式 Subscription schema。
- [`phase3/04-search-generation-pipeline.md`](phase3/04-search-generation-pipeline.md)：Brave Search、Evidence、ranking 与 synthesis。
- [`phase3/05-harness-integration.md`](phase3/05-harness-integration.md)：Harness/CRUD/Artifact/delivery boundaries。
- [`phase3/06-output-contracts.md`](phase3/06-output-contracts.md)：deterministic contract 与 semantic quality split。
- [`phase3/07-personalization-and-recommendation.md`](phase3/07-personalization-and-recommendation.md)：Profile update 与 deterministic ranking。
- [`phase3/08-delivery-and-feedback.md`](phase3/08-delivery-and-feedback.md)：DeliveryRecord、Feedback loop 与 API sketch。
- [`phase3/09-failure-and-recovery.md`](phase3/09-failure-and-recovery.md)：failure matrix、duplicate prevention 与 partial persistence。
- [`phase3/10-testing-and-e2e.md`](phase3/10-testing-and-e2e.md)：Fake correctness gates、Golden E2E 与 manual smoke。
- [`phase3/11-design-decisions.md`](phase3/11-design-decisions.md)：V1 design decisions。
- [`phase3/12-review-guide.md`](phase3/12-review-guide.md)：product/boundary/data/authority/test review。
- [`phase3/13-first-vertical-slice.md`](phase3/13-first-vertical-slice.md)：当前 offline application baseline、真实链路、schema v3、测试与剩余边界。

当前已实现 generation、Feedback/Profile/Explainable Ranking 与 Delivery 三条离线应用垂直切片；
不包含真实 Brave/network 或 HTTP，且未修改 Harness core。

## Phase 1 Delivery

- [`10-evidence-artifact-result.md`](phase1/10-evidence-artifact-result.md)：理解 Observation、Evidence、Artifact、Output Contract 与 Authoritative Result。
- [`14-testing-strategy.md`](phase1/14-testing-strategy.md)：了解 unit/integration/regression/E2E 的真实覆盖、self-check 和 RealProvider manual boundary。

测试入口统一位于仓库 `tests/`：`unit/`、`integration/`、`e2e/`、`security/`、
`architecture/`；默认 `python -m unittest -q` 会递归发现全部 package，无需自定义 discover 命令。

## Phase 1 Review & Design

- [`15-code-review-guide.md`](phase1/15-code-review-guide.md)：使用五组 boundary checklist 做源码 Review。
- [`16-design-decisions.md`](phase1/16-design-decisions.md)：学习 25 个真实设计取舍、替代方案、后果与改变条件。
- [`17-glossary-and-state-reference.md`](phase1/17-glossary-and-state-reference.md)：快速查询 44 个核心对象和状态术语。
- [`18-version-learning-map.md`](phase1/18-version-learning-map.md)：按问题演进学习 V0–V28，并映射到仓库真实 Git tags。

## 推荐顺序

```text
第一次学习：00 -> 01 -> 02 -> 03 -> 04 -> Golden E2E
理解控制流：02 -> 03 -> 05 -> 06 -> 10 -> 13
安全审查：  12 -> 04 -> 06 -> 09 -> 11 -> 13 -> 15
设计复盘：  16 -> 17 -> 18，并按 tag 对照源码
Phase 2：    phase2/00 -> 01 -> 02 -> 03 -> 04 -> 05 -> 06 -> 07 -> 08 -> 09 -> 10
Phase 3：    phase3/README -> 00 -> 02 -> 04 -> 05 -> 06 -> 09 -> 12 -> 13 -> apps/digest_agent
```

## Navigation

- Previous: [`../README.md`](../README.md)
- Next: [`00-overview.md`](phase1/00-overview.md)
- Related: [`15-code-review-guide.md`](phase1/15-code-review-guide.md)、[`18-version-learning-map.md`](phase1/18-version-learning-map.md)
