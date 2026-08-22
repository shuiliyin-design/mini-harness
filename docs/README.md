# Mini Harness Documentation Map

这些文档以当前源码、schema 和 deterministic tests 为准。推荐先读总览，再根据目标选择 Runtime control flow、
安全恢复或设计审查路线；不需要网络、在线模型或仓库外上下文。

## Start Here

- [`00-overview.md`](00-overview.md)：理解 Mini Harness 的教学目标、Model/Harness/Environment 三层与完整能力地图。
- [`01-architecture.md`](01-architecture.md)：理解 façade、CLI、orchestrator、leaf modules 和 dependency DAG。
- [`15-code-review-guide.md`](15-code-review-guide.md)：按 Beginner、Intermediate、Deep Review 三条路线进入真实源码和测试。

## Runtime Core

- [`02-agent-loop.md`](02-agent-loop.md)：跟踪 `run_agent -> _run_agent_runtime -> phase helpers` 的真实调用链。
- [`03-authority-and-policy.md`](03-authority-and-policy.md)：区分 Classification、Effect、Static Composition、Capability、Runtime Gate、Approval 与 AuthorizedAction。
- [`04-action-lifecycle.md`](04-action-lifecycle.md)：理解 Action checkpoint、dispatch ordering、Verification obligation 与 replay-safe recovery。
- [`05-planning-retry-governance.md`](05-planning-retry-governance.md)：区分任务下一步、是否允许新 attempt、当前预算是否允许继续。
- [`07-session-memory-context.md`](07-session-memory-context.md)：区分 continuity、长期 Memory、Provider working context 与 Current Reality。
- [`08-mcp-and-subagents.md`](08-mcp-and-subagents.md)：理解 MCP local mapping、Structured Handoff、Subagent isolation 与 Authority attenuation。

## Safety & Failure

- [`06-durability-and-recovery.md`](06-durability-and-recovery.md)：审查 crash window、Degraded、Reconciliation 和 no-blind-replay 保证边界。
- [`12-security-boundaries.md`](12-security-boundaries.md)：审查 protected paths、secret projection、dispatch seal 与 historical read-only boundary。
- [`13-failure-semantics.md`](13-failure-semantics.md)：区分 failure、unknown、pause、cancel、deadline、blocked、failed 与 incomplete。

## Historical / Replay

- [`09-audit-and-historical-objects.md`](09-audit-and-historical-objects.md)：理解八类 Historical Object、reference graph 与各自 fingerprint 精度。
- [`11-replay-and-bundles.md`](11-replay-and-bundles.md)：理解 identity check、deterministic Harness replay、resolver 与 external re-execution 禁区。

## Delivery

- [`10-evidence-artifact-result.md`](10-evidence-artifact-result.md)：理解 Observation、Evidence、Artifact、Output Contract 与 Authoritative Result。
- [`14-testing-strategy.md`](14-testing-strategy.md)：了解 unit/integration/regression/E2E 的真实覆盖、self-check 和 RealProvider manual boundary。

## Review & Design

- [`15-code-review-guide.md`](15-code-review-guide.md)：使用五组 boundary checklist 做源码 Review。
- [`16-design-decisions.md`](16-design-decisions.md)：学习 25 个真实设计取舍、替代方案、后果与改变条件。
- [`17-glossary-and-state-reference.md`](17-glossary-and-state-reference.md)：快速查询 44 个核心对象和状态术语。
- [`18-version-learning-map.md`](18-version-learning-map.md)：按问题演进学习 V0–V28，并映射到仓库真实 Git tags。

## 推荐顺序

```text
第一次学习：00 -> 01 -> 02 -> 03 -> 04 -> Golden E2E
理解控制流：02 -> 03 -> 05 -> 06 -> 10 -> 13
安全审查：  12 -> 04 -> 06 -> 09 -> 11 -> 13 -> 15
设计复盘：  16 -> 17 -> 18，并按 tag 对照源码
```

## Navigation

- Previous: [`../README.md`](../README.md)
- Next: [`00-overview.md`](00-overview.md)
- Related: [`15-code-review-guide.md`](15-code-review-guide.md)、[`18-version-learning-map.md`](18-version-learning-map.md)
