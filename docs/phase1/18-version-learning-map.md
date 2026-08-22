# Version Learning Map：按问题学习 V0–V28

## 读完你应该理解什么

这不是逐 commit changelog，而是一张“问题 → 抽象 → 新风险 → 下一步修正”的学习地图。读者可以离线切换
Git tag，观察一个最小 loop 如何逐步长出 Authority、durability、history 与 delivery boundaries。

## 教学 milestone 与真实 Git tag

文档中的 V0–V28 是教学 milestone 编号；仓库 tag 从 `v0.1` 开始，并单独记录了 Real MCP、模块化等中间
checkpoint，因此数字后缀并不相同。不要构造 `v28` 之类不存在的 tag；先运行：

```bash
git tag --list --sort=version:refname
git show v0.15-durable-execution
```

| 教学 milestone | 对应真实 tag / snapshot |
|---|---|
| V0 | `v0.1` |
| V1 | `v0.2-real-provider` |
| V2 | `v0.3-approval-policy` |
| V3 | `v0.4-verification-gate` |
| V4 | `v0.5-verification-evidence` |
| V5 | `v0.6-session-resume` |
| V6 | `v0.7-context-compaction` |
| V7 | `v0.8-project-skills` |
| V8 | `v0.9-long-term-memory` |
| V9 / V9.2 | `v0.10-mcp-capabilities` / `v0.11-real-mcp-stdio` |
| V10 | `v0.12-subagent-handoff` |
| V11 | `v0.13-modular-harness` |
| V12 | `v0.14-planning-runtime` |
| V13 | `v0.15-durable-execution` |
| V14 | `v0.16-run-control` |
| V15 | `v0.17-retry-policy` |
| V16 | `v0.18-execution-governance` |
| V17 | `v0.19-audit-trace` |
| V18 | `v0.20-policy-composition` |
| V19 | `v0.21-policy-snapshot` |
| V20 | `v0.22-run-manifest` |
| V21 | `v0.23-run-envelope` |
| V22 | `v0.24-evidence-provenance` |
| V23 | `v0.25-artifact-lifecycle` |
| V24 | `v0.26-result-binding` |
| V25 | `v0.27-run-bundle` |
| V26 | `v0.28-boundary-hardening` |
| V27 | `v0.29-architecture-consolidation` |
| V28 | 当前 Documentation snapshot 中的 deterministic E2E/self-check；审查时以 `git tag --list` 确认是否已有后续 tag |

## Foundation — V0–V1

- **What problem appeared?** 单次模型回答无法执行工具；加入 Tool loop 后，又需要真实 Provider protocol 和
  Observation feedback。
- **What abstraction solved it?** `FakeProvider`、`RealProvider`、统一 JSON decision，以及最小 `run_agent` loop。
- **What new failure mode did that abstraction introduce?** Model 可请求任意 command；transport/decision 错误也可能
  击穿 loop。
- **What did the next stage fix?** V2 把 Tool Policy 与 Human Approval 放到 Model 之外。

建议比较 `v0.1` 与 `v0.2-real-provider` 的 [`providers.py`](../../mini_harness_core/providers.py) 当前形态，理解
Provider 最终为何只拥有 decision boundary。

## Authority — V2

- **What problem appeared?** “模型想执行”被错误当成“系统允许执行”。
- **What abstraction solved it?** `ALLOW/ASK/DENY`、classification、Approval 与 Harness-owned Tool executor gate。
- **What new failure mode did that abstraction introduce?** Approval/Policy 只能说明调用许可，不能证明副作用结果正确。
- **What did the next stage fix?** V3–V4 加入 Verification obligation 与 evidence tracking。

学习入口：[`authority.py`](../../mini_harness_core/authority.py)、[`03-authority-and-policy.md`](03-authority-and-policy.md)。

## Verification — V3–V4

- **What problem appeared?** Tool exit 0 容易被误当成任务完成；写操作后的现实可能未验证。
- **What abstraction solved it?** side-effect success 建立 Verification Gate；相关 read-only Observation 才能解除。
- **What new failure mode did that abstraction introduce?** verification state 需要跨进程连续，历史消息也开始增长。
- **What did the next stage fix?** V5 Session persistence 保存 continuity；V6 再分离完整历史与 working context。

学习入口：[`verification.py`](../../mini_harness_core/verification.py)、`VerificationGateTests`。

## Persistence — V5

- **What problem appeared?** 进程退出会丢失 messages 和 active Verification state。
- **What abstraction solved it?** versioned Session、atomic save 与 explicit resume。
- **What new failure mode did that abstraction introduce?** Session 容量持续增长，且容易被误当成 Current Reality。
- **What did the next stage fix?** V6–V8 引入 deterministic context assembly、Project Context 和长期 Memory，并明确
  continuity 不等于 runtime truth。

学习入口：[`session.py`](../../mini_harness_core/session.py)、[`07-session-memory-context.md`](07-session-memory-context.md)。

## Context / Memory — V6–V8

- **What problem appeared?** 完整 Session 直接发送给 Provider 会超预算；项目指令和长期偏好又需要进入工作上下文。
- **What abstraction solved it?** context measurement/compaction、`RuntimeContextAssembler`、Project Instructions/Skills、
  Memory selection/lifecycle。
- **What new failure mode did that abstraction introduce?** 不可信项目内容或 Memory 可能被误当 Security Policy；压缩也可能
  丢失 active control facts。
- **What did the next stage fix?** Authority inputs 与 context inputs 分开，active control 被显式重注入；外部能力再通过 MCP
  进入 Harness-owned mapping。

学习入口：[`context.py`](../../mini_harness_core/context.py) 的 `RuntimeContextAssembler`、
[`project_context.py`](../../mini_harness_core/project_context.py)、[`memory.py`](../../mini_harness_core/memory.py)。

## MCP / Subagent — V9–V10

- **What problem appeared?** Tool catalog、外部 MCP transport 与复杂 delegated work 无法都塞在 shell handler 中。
- **What abstraction solved it?** `MCPRegistry/MCPClient`、Harness-owned local Effect/Policy mapping，以及 Structured Handoff 与
  attenuated Subagent Authority。
- **What new failure mode did that abstraction introduce?** server metadata 可能尝试 authority uplift；timeout 产生 unknown；
  Subagent 可能越权修改 Main Plan。
- **What did the next stage fix?** Main ownership、isolated return contract 与 V11 模块化边界把这些责任拆开；之后 Planning
  正式拥有 task progression。

学习入口：[`mcp.py`](../../mini_harness_core/mcp.py)、[`handoff.py`](../../mini_harness_core/handoff.py)、
[`08-mcp-and-subagents.md`](08-mcp-and-subagents.md)。

## Planning — V11–V12

- **What problem appeared?** 单文件 orchestrator 难以 review；Model final text 不能稳定表示多步任务进度。
- **What abstraction solved it?** `mini_harness_core` 模块化、versioned Plan、dependency steps、evidence-gated completion 与 bounded replan。
- **What new failure mode did that abstraction introduce?** Action 执行和 Plan 状态都需要 crash-safe ordering；否则恢复可能重复副作用。
- **What did the next stage fix?** V13 durable Action checkpoint 与 Reconciliation，V14 cooperative Run Control。

学习入口：[`planning.py`](../../mini_harness_core/planning.py)、`PlanningRuntimeIntegrationTests`。

## Durability / Control — V13–V14

- **What problem appeared?** side effect 已发生但 terminal state 未保存；pause/cancel 也可能在 action 中途到达。
- **What abstraction solved it?** `prepared/executing/succeeded/failed/unknown` checkpoint、replay policy、narrow Reconciliation，
  以及 cooperative Run Control state machine。
- **What new failure mode did that abstraction introduce?** 明确 failure、unknown effect、pause/cancel 与 retry eligibility 很容易
  被混成同一个 error path。
- **What did the next stage fix?** V15 把 Retry Policy 独立出来；V16 用 deadline/budget 再限制 scheduling。

学习入口：[`durability.py`](../../mini_harness_core/durability.py)、[`run_control.py`](../../mini_harness_core/run_control.py)、
[`06-durability-and-recovery.md`](06-durability-and-recovery.md)。

## Retry / Governance — V15–V16

- **What problem appeared?** transient failure 需要 retry，但 side effect unknown 不能重放；无限 backoff/attempt 也会失控。
- **What abstraction solved it?** bounded Retry state/policy、FakeClock、UTC deadline、action/subagent budgets，以及 deadline 后一次
  Safety Reconciliation。
- **What new failure mode did that abstraction introduce?** pause、crash downtime、deadline 和 budget 的时间语义需要可解释；
  历史上发生了什么也需要独立记录。
- **What did the next stage fix?** V17 Audit Trace 提供 observability；V18–V21 再建立可组合、可重放的历史 identity objects。

学习入口：[`retry.py`](../../mini_harness_core/retry.py)、[`governance.py`](../../mini_harness_core/governance.py)、
[`05-planning-retry-governance.md`](05-planning-retry-governance.md)。

## Audit / History / Policy Composition — V17–V21

- **What problem appeared?** Session 既不适合 forensic trace，也无法回答“当时用了哪套 Policy/configuration、pure transition
  能否重算”。单层 Policy 也不足以表示 zone/profile/delegation ceiling。
- **What abstraction solved it?** Audit Event、Static Policy Composition、Policy Snapshot、Run Manifest、Run Envelope 与
  deterministic Harness replay。
- **What new failure mode did that abstraction introduce?** Historical integrity 容易被误当 Current Reality；Manifest、Envelope、
  Audit 也容易被误画成线性 truth chain。
- **What did the next stage fix?** V22–V24 用 Evidence/Artifact/Result 明确 claim、deliverable 和 terminal outcome 的不同 ownership。

学习入口：[`09-audit-and-historical-objects.md`](09-audit-and-historical-objects.md)、
[`11-replay-and-bundles.md`](11-replay-and-bundles.md)。

## Evidence / Artifact / Result — V22–V24

- **What problem appeared?** Audit event 不能单独证明 Plan step、文件版本或最终 completion；Model 也可能虚报 completed。
- **What abstraction solved it?** immutable Evidence provenance、Artifact version lifecycle、Output Contract 和 Authoritative Result Binding。
- **What new failure mode did that abstraction introduce?** 多个 historical store 形成引用 closure，本地 `.audit` 缺失时难以移植检查。
- **What did the next stage fix?** V25 Bundle 导出 required closure，并提供 offline resolver/check/replay。

学习入口：[`10-evidence-artifact-result.md`](10-evidence-artifact-result.md)、[`result.py`](../../mini_harness_core/result.py)。

## Bundle — V25

- **What problem appeared?** Historical check 依赖本地 `.audit`，无法把一个 Run 的必要 closure 安全带到另一目录。
- **What abstraction solved it?** deterministic Bundle index、per-file hash/size、typed closure 与 `BundleHistoricalResolver`。
- **What new failure mode did that abstraction introduce?** portable history 可能被误当 portable Authority/resume package；secret、symlink、path
  escape 和 optional forensic trace 需要 fail closed。
- **What did the next stage fix?** V26 统一 protected paths、secret projection、AuthorizedAction seam 与 post-tool failure semantics。

学习入口：[`run_bundle.py`](../../mini_harness_core/run_bundle.py)、[`11-replay-and-bundles.md`](11-replay-and-bundles.md)。

## Boundary Hardening — V26

- **What problem appeared?** 各模块局部正确仍可能通过 direct executor、raw Observation persistence、stale Approval 或 fault ordering 组合出旁路。
- **What abstraction solved it?** protected-path ceiling、sealed `AuthorizedAction`、cross-store secret projection、六个 fault hooks 和 forward-truth rules。
- **What new failure mode did that abstraction introduce?** Orchestrator 汇集大量 boundary logic，重复 gate 与模块依赖难以 review。
- **What did the next stage fix?** V27 收敛 façade/orchestrator、明确 phase helper 与 dependency DAG。

学习入口：[`12-security-boundaries.md`](12-security-boundaries.md)、V26 test modules。

## Architecture Consolidation — V27

- **What problem appeared?** 高 fan-out agent、façade exports 与模块依赖可能产生 cycle、authority bypass 或重复 ownership。
- **What abstraction solved it?** 薄 `run_agent`、`_run_agent_runtime` phase orchestration、leaf pure helpers、architecture/DAG tests。
- **What new failure mode did that abstraction introduce?** 单个模块 test 通过仍不能证明完整 lineage、failure 和 portable replay 组合正确。
- **What did the next stage fix?** V28 用少量 deterministic system scenarios 和 self-check 验证跨模块 invariants。

学习入口：[`01-architecture.md`](01-architecture.md)、[`02-agent-loop.md`](02-agent-loop.md)、
[`test_v27_architecture.py`](../../tests/architecture/test_v27_architecture.py)。

## E2E Validation — V28

- **What problem appeared?** Unit/integration assertions 分散，可能遗漏“每层单独正确、组合后错误”。
- **What abstraction solved it?** 八个离线 scenario：golden、retry exhaustion、crash replay safety、pause/resume、cancel、deadline safety、
  secret boundary、historical drift/portable Bundle；另有七项 self-check sanity。
- **What new failure mode did that abstraction introduce?** Scenario 名称可能强于 assertion；self-check 也可能被误当完整 test suite。
- **What did the next stage fix?** Documentation Pass 明确 assertion boundary、review route 和 design decisions；没有新增 Runtime capability。

学习入口：[`test_end_to_end_runtime.py`](../../tests/e2e/test_end_to_end_runtime.py)、[`14-testing-strategy.md`](14-testing-strategy.md)。

## 如何离线使用 tags 学习

```bash
git worktree add /tmp/mini-harness-v13 v0.15-durable-execution
git diff v0.14-planning-runtime..v0.15-durable-execution -- mini_harness_core test_mini_harness.py
git show v0.29-architecture-consolidation:mini_harness_core/agent.py
```

使用独立 worktree 可避免覆盖当前修改。阅读旧 tag 时，以该 tag 自带测试和 schema 为准；不要用当前文档声称旧
版本已经拥有后来才加入的能力。

## Navigation

- Previous: [`17-glossary-and-state-reference.md`](17-glossary-and-state-reference.md)
- Next: [`README.md`](../README.md)
- Related: [`00-overview.md`](00-overview.md)、[`15-code-review-guide.md`](15-code-review-guide.md)、
  [`16-design-decisions.md`](16-design-decisions.md)
