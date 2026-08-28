# Mini Harness

## 1. 项目是什么

Mini Harness 是一个只依赖 Python 标准库的教学型 Agent Harness Runtime。它用小而可测试的代码展示：
Model 如何提出 Intent，Harness 如何拥有 Policy、Approval、Execution、Verification Authority，Environment
如何返回 Observation，以及这些事实如何成为 Evidence、Artifact、Authoritative Result 和可离线 Replay 的历史。

**Harness 是 runtime infrastructure，不是最终用户应用。** 它不追求 production framework、通用 sandbox、
分布式 exactly-once、GUI 或产品级移动体验。

## 2. Current architecture

```text
Future apps
    ↓
Harness ↔ Bridge / mobile integrations
    ↓
Core Harness Runtime ──authorized dispatch──> Environment adapters
    ↑                                         ↑
Provider proposes Intent only                fixed Termux capabilities

Bridge transport ──transport facts──> integrations

Historical plane: Audit / Snapshot / Manifest / Envelope / Evidence
                  / Artifact / Result / Bundle / Replay
```

边界要点：

- Provider/Model 负责决策候选，不拥有执行 Authority。
- Bridge claim/Result 是 transport fact，不是 Harness Approval 或 Evidence。
- Environment adapter 执行已授权 capability，不决定 Policy、Retry 或 Result。
- Session/Memory 提供 continuity；当前现实必须由 fresh Observation 证明。
- Replay 与 Bundle 读取 schema/object-type identity，不导入历史 Python 类型，也不执行外部动作。

`mini_harness.py` 是 Phase 1 兼容 façade；真实 CLI wiring 在
[`mini_harness_core/cli.py`](mini_harness_core/cli.py)，Agent loop 在
[`mini_harness_core/agent.py`](mini_harness_core/agent.py)。

## 3. Repository layout

```text
mini_harness.py              Phase 1 façade 与主 CLI 入口
mini_harness_core/
  *.py                       Core Runtime / Authority / History
  bridge/                    Phase 2 filesystem transport
  environment/               adapter contract、registry、Termux implementation
  integrations/              Harness ↔ Bridge 与 mobile workflow composition
cli/                         Bridge / Termux 命令实现
tools/                       self-check 与本地 MCP teaching server
tests/                       unit / integration / e2e / security / architecture
docs/
  phase1/                    Core Harness 教材
  phase2/                    Bridge / Mobile integration 教材
  phase3/                    AI Digest application design
apps/
  digest_agent/              Phase 3 AI Digest 离线垂直切片
skills/                      教学项目 skill
```

根目录的 `bridge_*.py`、`termux_capability.py`、`mini_harness_self_check.py` 和
`mcp_demo_server.py` 是保留旧命令行为的薄兼容入口；实现分别位于 `cli/` 与 `tools/`。

## 4. Quick start

默认 `FakeProvider` 不需要网络或 API Key：

```bash
python mini_harness.py
python mini_harness.py --self-check
python mini_harness.py --resume SESSION_ID
```

真实 Provider 只用于人工 protocol/UX 实验，不进入 correctness gate。配置见
[`providers.py`](mini_harness_core/providers.py) 和
[`testing-strategy.md`](docs/phase1/14-testing-strategy.md)。Secret 只能来自环境变量，不能写入源码、Session、
日志或 Git。

## 5. Core concepts

- **Intent ≠ Authority**：Model output 不能直接触发执行。
- **Effect ≠ disposition**：read-only/side-effecting 与 ALLOW/ASK/DENY 是不同维度。
- **AuthorizedAction**：执行前必须绑定 checkpoint、Policy、runtime gate、必要 Approval 和参数。
- **Observation ≠ Evidence**：raw 结果要先安全投影，再由 Harness 验证和持久化。
- **Continuity ≠ current reality**：Session/Memory 不能替代新的 Environment observation。
- **Historical truth ≠ live authority**：旧 Approval、Snapshot、Evidence、Result 和 Bundle 都不能授权重执行。
- **Authoritative Result**：Model 的 final answer/claimed status 不能覆盖 Harness terminal truth。

安全边界与非保证范围见
[`security-boundaries.md`](docs/phase1/12-security-boundaries.md) 和
[`durability-and-recovery.md`](docs/phase1/06-durability-and-recovery.md)。

## 6. Phase status

### Phase 1 — Core Harness Runtime

已形成可独立学习和离线运行的 Harness：Provider boundary、Authority/Policy、sealed dispatch、Verification、
Plan/Retry/Governance、Session/Memory/Context、historical objects、Result、Bundle 与 deterministic replay。
它不要求 Bridge、Termux 或 Android 才能运行。

### Phase 2 — Bridge / Mobile integration

已实现 filesystem Bridge v1、Harness↔Bridge binding/projection、Environment adapter contract、固定 Termux battery
与 notification capability，以及条件式 mobile workflow。它是真实 integration environment，用于检验跨进程、
跨存储和 Android 边界，**不是产品方向**。

### Phase 3 — Applications

repository-first design 之后，已实现三条 AI Digest 离线垂直切片：generation、
Feedback/Profile/Explainable Ranking，以及独立 DeliveryRecord/attempt。Fake Search/Provider/Delivery
是 correctness gates；Termux 仅做 authorized mapping。`DigestApplication` 已成为稳定 public business
boundary，提供 versioned Subscription lifecycle、幂等 Run/recovery、Digest query、Delivery 与
Feedback/Profile DTO，且不暴露 Harness internals。设计导航与当前状态见
[`docs/phase3/`](docs/phase3/README.md) 和
[`testing-and-e2e.md`](docs/phase3/10-testing-and-e2e.md)。这些切片未修改 Harness core。

最短 AI Digest Demo（默认全 fake）：

```bash
python -m apps.digest_agent.cli readiness
python -m apps.digest_agent.cli subscription-create --request "订阅 AI 行业动态，600 字以内"
python -m apps.digest_agent.cli subscription-list
```

本机手机布局 Web Demo（显式全 fake）：

```bash
python -m apps.digest_agent.web --search-provider fake --llm-provider fake --delivery-provider fake
# 浏览器打开 http://127.0.0.1:8765/
```

Real Vertex Provider Compatibility Gate 已以顶层标量 tool wire schema 通过 10/10 有界调用；
Real Brave + Vertex HTTP Product Integration Journey 也连续 3/3 通过。后者由 `http.client`
驱动，不是 Browser Engine E2E。Manual Mobile Browser Acceptance 已由真实手机操作及 durable
run lineage 验证为 PASS；Automated Browser-Engine E2E 为 NOT IMPLEMENTED / NOT RUN。以下仍是
显式 opt-in 真实服务诊断，不替代 Offline Deterministic Correctness Gate：

```bash
LLM_API_MODE=chat-completions python -m apps.digest_agent.web --search-provider brave --llm-provider vertex --delivery-provider fake
```

## 7. Documentation map

完整导航：[`docs/README.md`](docs/README.md)

| 目标 | 入口 |
|---|---|
| Phase 1 总览与架构 | [`00-overview.md`](docs/phase1/00-overview.md) → [`01-architecture.md`](docs/phase1/01-architecture.md) |
| Runtime / Authority | [`02-agent-loop.md`](docs/phase1/02-agent-loop.md) → [`03-authority-and-policy.md`](docs/phase1/03-authority-and-policy.md) |
| Durability / failure | [`06-durability-and-recovery.md`](docs/phase1/06-durability-and-recovery.md) → [`13-failure-semantics.md`](docs/phase1/13-failure-semantics.md) |
| History / Result / Replay | [`09-audit-and-historical-objects.md`](docs/phase1/09-audit-and-historical-objects.md) → [`10-evidence-artifact-result.md`](docs/phase1/10-evidence-artifact-result.md) → [`11-replay-and-bundles.md`](docs/phase1/11-replay-and-bundles.md) |
| Phase 2 | [`Phase 2 overview`](docs/phase2/00-overview.md) |
| Phase 3 | [`AI Digest map`](docs/phase3/README.md) → [`Application façade`](docs/phase3/15-application-facade-and-run-lifecycle.md) → [`review`](docs/phase3/14-product-readiness-review.md) |
| Phase 4 | [`Feeds product map`](docs/phase4/README.md) → [`slice plan`](docs/phase4/07-incremental-slice-plan.md) → [`P4.3 status`](docs/phase4/13-p43-flight-condition-status.md) |

## 8. Testing / self-check

Release gate：

```bash
python -m unittest -q
git diff --check
python mini_harness.py --self-check
```

测试按 `tests/unit/`、`integration/`、`e2e/`、`security/`、`architecture/` 分类，但分类并不表示每个测试
只属于一种语义。所有 correctness gate 都离线、deterministic，不需要真实 Android 或 LLM。具体 assertion
boundary 见 [`testing-strategy.md`](docs/phase1/14-testing-strategy.md) 和
[`Phase 2 testing`](docs/phase2/07-testing-and-e2e.md)。

## 9. Learning / review paths

```text
Beginner:     FakeProvider → run_agent → Golden E2E → Authority
Intermediate: Intent → Policy → Approval → AuthorizedAction → Observation → Result
Deep review:  dispatch seam → durability → recovery → historical integrity → replay
Phase 2:      Bridge protocol → integration binding → environment contract → mobile flow
Phase 3:      product scope → generation contract → profile ranking → delivery/recovery
```

源码审查入口见 [`code-review-guide.md`](docs/phase1/15-code-review-guide.md) 与
[`Phase 2 review guide`](docs/phase2/09-review-guide.md)。历史 milestone/tag 对照完整保留在
[`version-learning-map.md`](docs/phase1/18-version-learning-map.md)。

## 10. Future application layer

未来业务代码进入 `apps/<application>/`：app 可以向下依赖稳定 façade、MCP boundary 与按需
integrations；Core、Bridge transport、Environment implementation 不反向导入 app。第一个设计是
[`apps/digest_agent/`](apps/digest_agent/README.md)，其 Subscription/Profile/Digest/SQLite/Delivery
规则全部属于应用。三条 Fake vertical slice 已复用现有 historical schema 与 sealed dispatch；
Real Brave Search 与 Vertex-backed LLM app adapters 继续复用同一路径，证明 fixed workflow
不需要修改 core。产品 HTTP API 与 scheduler 仍是后续独立 slice。
