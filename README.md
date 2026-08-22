# Mini Harness

## 1. 项目是什么

Mini Harness 是一个只依赖 Python 标准库的教学型 Agent Runtime。它用可读、可测试的代码展示：Model 如何
提出 Intent，Harness 如何拥有 Policy/Approval/Execution/Verification Authority，Environment 如何返回
Observation，以及这些事实如何形成 Evidence、Artifact、Authoritative Result 和可离线 Replay 的历史记录。

它不是 production Agent framework，也不提供通用 sandbox、分布式执行、云端 orchestration、GUI/Mobile、
生产监控或通用 exactly-once delivery。

## 2. Quick Start

默认 `FakeProvider` 不需要网络或 API Key：

```bash
python mini_harness.py
```

运行完整离线 correctness gate 与快速 sanity check：

```bash
python -m unittest -q
python mini_harness.py --self-check
```

恢复本地 Session：

```bash
python mini_harness.py --resume SESSION_ID
```

真实 Provider 只用于人工 protocol/UX 实验，不进入 correctness gate。配置入口见
[`mini_harness_core/providers.py`](mini_harness_core/providers.py) 和
[`docs/14-testing-strategy.md`](docs/14-testing-strategy.md)；不要把 API Key 写入源码、Session、日志或 Git。

## 3. Core Architecture

```text
User Task
   |
   v
Provider / Model -------- proposes Intent only
   |
   v
Agent Orchestrator
   |
   +--> Classification + Effect
   +--> Static Policy + Capability Ceiling
   +--> Run Control / Governance / Verification gates
   +--> Human Approval when ASK
   +--> sealed AuthorizedAction
              |
              v
        Tool / MCP / Subagent
              |
              v
        safe Observation projection
              |
              v
Verification / Reconciliation / Planning / Retry
              |
              v
Evidence -> Artifact -> Output Contract -> Authoritative Result

Parallel historical planes:
Audit + Policy Snapshot + Manifest + Envelope -> Bundle offline check/replay
```

`mini_harness.py` 是兼容 façade；CLI wiring 在 `mini_harness_core/cli.py`，真实 loop 在
`mini_harness_core/agent.py::_run_agent_runtime`。模块依赖保持 DAG，History/Replay 不拥有执行入口。

## 4. Phase 1 Capabilities

- Fake/Real Provider boundary 与 JSON decision normalization。
- shell/MCP/Subagent classification、Policy Composition、Trust Zone、Capability Profile 和 Delegated Authority。
- Human Approval、protected-path ceiling 与 sealed `AuthorizedAction` dispatch seam。
- Verification Gate、fresh Observation、replay-safe recovery 与 bounded Reconciliation。
- Plan、Step、bounded Retry/Backoff、Pause/Cancel、Deadline 和 execution budgets。
- Session continuity、long-term Memory、Project Instructions/Skills 与 deterministic context compaction。
- Audit、Policy Snapshot、Manifest、Envelope、Evidence、Artifact、Output Contract 和 Authoritative Result。
- Bundle export、offline integrity check、deterministic Harness replay 与 V28 system validation/self-check。

能力名称不代表 production completeness。具体 assertion boundary 见
[`docs/14-testing-strategy.md`](docs/14-testing-strategy.md)。

## 5. Safety Disclaimer / Teaching Scope

项目的核心安全边界是：

- Intent 不等于 Authority；Provider/Model 不直接执行。
- `ASK` 不等于 side-effecting；Policy disposition 与 Effect 独立。
- Historical Approval/Policy/Evidence/Bundle 不授予当前执行 Authority。
- Session/Memory 用于 continuity；Current Reality 需要 fresh Observation。
- raw secret-bearing Observation 在 persistence/model-context 前投影。
- unknown side effect 不被 recovery/retry path blind replay；必须先 Reconciliation。
- Model final answer/`claimed_status` 不覆盖 Harness Authoritative Result。

本项目不保证恶意 Python 绕过 Harness 后的 OS 隔离，不保证 remote service deduplication、executor 内部副作用
次数或分布式原子性。完整边界见 [`docs/12-security-boundaries.md`](docs/12-security-boundaries.md) 和
[`docs/06-durability-and-recovery.md`](docs/06-durability-and-recovery.md)。

## 6. Documentation Map

完整导航：[`docs/README.md`](docs/README.md)

| 目标 | 从哪里开始 |
|---|---|
| 了解项目与架构 | [`00-overview.md`](docs/00-overview.md) → [`01-architecture.md`](docs/01-architecture.md) |
| 跟踪 Runtime control flow | [`02-agent-loop.md`](docs/02-agent-loop.md) → [`03-authority-and-policy.md`](docs/03-authority-and-policy.md) |
| 深入恢复与失败 | [`06-durability-and-recovery.md`](docs/06-durability-and-recovery.md) → [`13-failure-semantics.md`](docs/13-failure-semantics.md) |
| 学习历史与交付 | [`09-audit-and-historical-objects.md`](docs/09-audit-and-historical-objects.md) → [`10-evidence-artifact-result.md`](docs/10-evidence-artifact-result.md) → [`11-replay-and-bundles.md`](docs/11-replay-and-bundles.md) |
| 做源码 Review | [`15-code-review-guide.md`](docs/15-code-review-guide.md) |
| 理解设计取舍与术语 | [`16-design-decisions.md`](docs/16-design-decisions.md) → [`17-glossary-and-state-reference.md`](docs/17-glossary-and-state-reference.md) |
| 按历史 tag 学习 | [`18-version-learning-map.md`](docs/18-version-learning-map.md) |

旧 README 中的 MCP transport、context/Memory、Policy replay、Manifest/Envelope、Evidence/Artifact/Result 和 Bundle
细节已分别归入上述专题文档；README 只保留入口与安全范围。

## 7. Recommended Learning Paths

### Beginner

```text
FakeProvider -> run_agent -> Golden E2E -> Authority -> Action Lifecycle
```

目标：看懂一个最小 Agent Run 如何从 Task 到 Result。

### Intermediate

```text
Model Decision -> Policy -> Runtime Gate -> Approval -> AuthorizedAction
-> Observation -> Verification -> Planning/Retry -> Evidence/Artifact/Result
```

目标：理解 Runtime control flow 与各 owner 的边界。

### Deep Review

```text
Policy Ceiling -> Dispatch Seam -> Persistence Ordering -> Failure/Recovery
-> Historical Integrity -> Replay -> Bundle -> Final Result
```

目标：寻找 authority bypass、stale truth、blind replay、secret leak 与 false completion。每一步的真实
module/function/test 见 [`docs/15-code-review-guide.md`](docs/15-code-review-guide.md)。

## 8. Tests / Self-check

推荐 Release Gate：

```bash
python -m unittest -q
git diff --check
python mini_harness.py --self-check
```

本次 Documentation snapshot 的基线运行输出为 587 tests；数量会随测试变化，应以命令实际输出为准。这些测试
按 `tests/unit/`、`tests/integration/`、`tests/e2e/`、`tests/security/` 和
`tests/architecture/` 整理，仍混合 regression 与 deterministic adversarial 场景，**不是 587 个 pure unit tests**。

V28 有 8 个 system scenarios，但 Scenario 3/6 等包含 helper-level system slice，名称不能扩张为未断言的完整
E2E guarantee。`--self-check` 只运行 7 个快速离线 sanity checks，不替代 unittest、benchmark、network test
或 production health daemon。

```text
Deterministic tests/self-check = correctness gate
RealProvider manual experiment = protocol / UX confidence
```

详细 coverage/limitation：[`docs/14-testing-strategy.md`](docs/14-testing-strategy.md)。

## 9. Version / Tag Map

仓库真实 tags 从 `v0.1` 开始；教学 milestone V0–V28 与 tag 数字后缀并非一一相等。例如：

```text
Teaching V13 durability   -> v0.15-durable-execution
Teaching V25 Bundle       -> v0.27-run-bundle
Teaching V27 architecture -> v0.29-architecture-consolidation
```

先查看真实 tags，再切换独立 worktree：

```bash
git tag --list --sort=version:refname
git show v0.15-durable-execution
```

完整概念演进和 V0–V28/tag 对照见
[`docs/18-version-learning-map.md`](docs/18-version-learning-map.md)。不要假定旧 tag 已拥有后来加入的 schema、
Authority 或 recovery behavior。

## 10. 下一阶段方向

Phase 1 的当前工作是完成离线教学与源码 Review baseline，而不是继续横向增加 Provider、MCP transport、状态机
或 GUI。Documentation/Review closure 完成后，优先回到 Mobile Agent 总地图；只有 review 找到明确 invariant
缺口时，才回到 Harness 增加针对性 Runtime 修复与离线测试。

## Navigation

- Previous: [`docs/18-version-learning-map.md`](docs/18-version-learning-map.md)
- Next: [`docs/README.md`](docs/README.md)
- Related: [`docs/00-overview.md`](docs/00-overview.md)、[`docs/15-code-review-guide.md`](docs/15-code-review-guide.md)
