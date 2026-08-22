# Testing Strategy：离线 correctness gate 与人工协议实验

## 读完你应该理解什么

- 当前 396 个 tests 混合了 unit、integration、regression、adversarial 和 E2E，而不是 396 个 pure unit tests。
- V28 八个 scenario 实际断言到哪里、哪些测试名比 assertion 更强。
- 为什么 RealProvider manual experiment 只提供 protocol/UX confidence，不是 correctness gate。

## Scope / Not Scope

本篇描述当前仓库测试形态、V28 system suite、self-check 与 Release Gate。

本篇不把测试数量当覆盖率，不声称证明生产可靠性、性能、网络兼容性或任意 Provider 行为。测试分类允许重叠：
一个 E2E test 同时也可以是 regression/adversarial test。

## 真实模块与关键函数

- V28 suite：[`test_end_to_end_runtime.py`](../test_end_to_end_runtime.py)，
  `EndToEndRuntimeV28Tests.test_01...test_08`。
- V26 security/failure：[`test_v26_boundary.py`](../test_v26_boundary.py)、
  [`test_v26_failure_semantics.py`](../test_v26_failure_semantics.py)。
- V27 architecture：[`test_v27_architecture.py`](../test_v27_architecture.py)。
- composition：[`test_policy_composition.py`](../test_policy_composition.py)。
- self-check：[`mini_harness_self_check.py`](../mini_harness_self_check.py)，`run_self_check`、
  `print_self_check`。
- CLI入口：[`mini_harness.py`](../mini_harness.py) 与
  [`cli.py`](../mini_harness_core/cli.py)。

## 核心状态/数据结构

### 测试分类（非互斥）

| 类别 | 当前例子 | 主要价值 | 不证明什么 |
|---|---|---|---|
| Unit | validators、pure transition、policy/retry helpers | 局部 schema/决策确定性 | 模块组合顺序 |
| Integration | `run_agent` + stores/Policy/Approval/Verification | owner boundary 连接 | 完整 portable lineage（除专门 E2E） |
| Regression | V0–V27 历史行为与 bug guard | 防止已有 contract 回退 | 新场景天然覆盖 |
| Deterministic adversarial | secret marker、protected symlink、tamper、forged action、fault hooks | fail-closed/security ordering | 未枚举攻击、OS sandbox |
| E2E/system | V28 golden/retry/cancel/secret/drift flows | 跨多个 owner 的组合错误 | 每个 scenario 都完整覆盖所有列出的阶段 |
| Manual RealProvider | endpoint/protocol/UX smoke | transport compatibility 与交互信心 | Harness correctness |

当前测试全部使用 Python 标准库 `unittest`。V28 correctness scenario 使用 fake/provider/clock、mock/fault hook 和
TemporaryDirectory，不依赖网络或真实 LLM。

## V28 八个 E2E scenario：实际覆盖

| # | Scenario | 当前明确 assertion | 不应从名字额外推断 |
|---|---|---|---|
| 1 | Golden success lineage/offline Bundle | Plan/Result completed；Artifact/Evidence/run lineage；Approval/action Audit；Envelope replay；移走 local audit 后 Bundle MATCH | 不代表所有 Tool/MCP/Plan 组合 |
| 2 | Read-only retry exhaustion | executor 3 calls；final status 非 completed；模型 completed claim contradiction | 不覆盖 side-effect retry/Reconciliation |
| 3 | Crash/Reconciliation replay safety | sealed dispatch crash；`executing -> unknown`；file `cat` confirms applied；该测试 dispatch 的 write call count=1 | 这是 dispatch/durability helper flow；不证明 global action-id/OS/remote exactly-once，也未断言完整恢复后的 Evidence/Artifact/Result lineage |
| 4 | Pause/resume | pause 时 prepared、executor 0；resume fresh Approval；最终 completed | 测试中的 `used_before=0` 不是实际 governance/retry counter continuity assertion；“budget 不重置”覆盖弱于测试名 |
| 5 | Cancel | executor 0；Result cancelled；resume rejected；Bundle show/check | 不覆盖 cancel 到达 in-flight Tool 的所有时序（另有 V26 test） |
| 6 | Deadline safety reconciliation | FakeClock expiry；normal denied；related permit once；unrelated/second denied | 这是 governance helper scenario；未运行完整 Agent，也未断言 Authoritative Result 保持 blocked |
| 7 | Secret boundary/executor bypass | `.env.local` deny；plain dict dispatch rejected；MCP secret marker不进入 messages/context/audit/Bundle | “bypass”限定 Harness dispatch seam；不等于 OS 阻止恶意 Python 直接调用 subprocess |
| 8 | Historical drift/portability/tamper | workspace/AGENTS 改变后 historical replay/integrity仍 MATCH；local audit隐藏后 Bundle MATCH；copy tamper MISMATCH | 未直接调用 Manifest drift status，也未直接断言 `current_output_contract_gate` 拒绝 stale A1；golden 使用默认 context assembler，temp workspace 下新增的 `AGENTS.md` 未被明确绑定为 Manifest project root |

这些限制不是测试失败，而是 review 时必须维持的 assertion boundary。相关缺口由其他 unit/integration tests 部分
覆盖，但不能把多个分散 assertion 自动描述成一条未实现的完整 system trace。

## Worked Trace：Golden E2E 如何跨模块

```text
TemporaryDirectory
  workspace/ + audit/ + bundles/
  -> SequenceProvider:
       echo hello > report.md
       cat report.md
       final_answer completed
  -> patched deterministic Approval=true
  -> run_agent + Plan + Output Contract
  -> ResultStore/EvidenceStore/ArtifactStore assertions
  -> Audit event/reference assertions
  -> RunEnvelopeStore + harness_replay_check
  -> export_run_bundle
  -> rename local audit away
  -> check_bundle + replay_bundle MATCH
```

这条 test 价值在于验证同一个 `run_id` 下的跨 store closure；它不通过真实模型“碰巧回答正确”来证明 Harness
correctness。

## Self-check

`python mini_harness.py --self-check` 在 TemporaryDirectory 内运行七个简短检查：

```text
dependency_dag
authority
protected_paths
golden_run
exactly_once
secret_boundary
bundle_replay
```

它 offline、deterministic，不读真实 project secrets，不写用户 Session/Memory/.audit。它是快速 release sanity
gate，不替代 unittest、benchmark、RealProvider test、network test 或 production health daemon。

注意 self-check 的 `exactly_once` 只确认 crash 后 checkpoint 变 unknown、原 write call count=1 和文件内容没变；
它没有执行完整 Reconciliation/Evidence/Result recovery。名称应按 assertion 范围理解。

## RealProvider manual experiments

RealProvider 实验适合确认：endpoint auth/config、JSON decision protocol、repair UX、真实上下文大小和交互提示。
这些实验受模型随机性、网络、服务版本和账户配置影响，不进入 deterministic correctness gate。

```text
RealProvider manual experiments = protocol / UX confidence
Deterministic E2E             = correctness gate
```

## Release Gate

推荐顺序：

```bash
python -m unittest -q
git diff --check
python mini_harness.py --self-check
```

unittest 包含 CLI smoke、historical integrity/replay、dependency DAG、security boundary 与 V28 scenarios；
self-check 提供更短的跨模块 sanity signal。任一 non-zero 都应阻止 release，不能用 manual RealProvider success
覆盖 deterministic failure。

## Key Invariants

1. correctness gate 不调用真实 LLM 或外网。
2. tests 使用 TemporaryDirectory，不污染真实 `.audit`/Session/Memory/workspace。
3. fault hooks deterministic、one-shot，且不存在 CLI/model input surface。
4. security test failure 不能靠放宽 Policy/secret projection 修复。
5. E2E assertion 必须沿同一 `run_id` 检查 lineage，而不只比较最终字符串。
6. Bundle replay test 必须在 local history unavailable 时仍完全 offline。
7. test name/场景说明不能宣称超过实际 assertion。
8. RealProvider manual result 不覆盖 deterministic gate failure。

## Failure / Edge Cases

- unittest count 随新增测试变化；“396”是本次文档验证基线，不是固定产品规格。
- test 输出很长但 exit 0：以 unittest summary/exit code 为准，不能只扫日志字符串。
- self-check PASS 但 unittest FAIL：release 仍失败；self-check 不是替代品。
- E2E helper-level scenario PASS：不能自动声称 full `run_agent -> Result -> Bundle` 链全部覆盖。
- patch/mock 太强：可能绕过真实 owner boundary；review 应确认 patch 点位于环境 adapter，而非待测决策函数。
- RealProvider flaky success/failure：只记录实验，不修改 correctness conclusion。

## Review Anchors

- `test_end_to_end_runtime.py`：逐条核对 test name 与 assert，特别是 3/4/6/8。
- `test_v26_boundary.py`：executor call count、post-tool persistence 与 cross-store secret scanning。
- `test_v27_architecture.py`：DAG、orchestrator size 和 AuthorizedAction order guard 是否仍引用真实源码。
- `mini_harness_self_check.py`：每个 check 是否只用 temp path/fake，是否出现网络/RealProvider/import side effect。
- test setup/teardown：cwd、patch、thread/MCP client 是否可靠恢复。
- Release Gate：三个命令是否全部实际执行并检查 exit code。

## Common Misreadings

- **“396 tests 就是 396 pure unit tests。”错误。** 分类重叠且包含 integration/E2E/adversarial。
- **“E2E 文件里的每个 test 都运行完整 Agent。”错误。** Scenario 3/6 主要是 helper-level system slice。
- **“测试名含 exactly-once 就证明通用 delivery guarantee。”错误。** 当前 assertion 只证明该 dispatch slice
  的 call count 与 unknown side effect 不被 recovery path blind replay。
- **“self-check PASS 就可跳过 unittest。”错误。** 它只做快速 sanity。
- **“RealProvider 成功证明 Harness 正确。”错误。** 只增加 protocol/UX confidence。

## RealProvider manual experiment

只有在 deterministic gate 全部通过后，才可用下面的环境变量做人工 protocol/UX 实验：

```bash
MINI_HARNESS_PROVIDER=real \
LLM_ENDPOINT=https://provider.example/v1/chat/completions \
LLM_MODEL=example-model \
LLM_API_KEY=... \
python mini_harness.py
```

`LLM_API_MODE` 可显式设为 `chat-completions`（默认）或 `completions`。API key 只放环境变量，不写入命令记录、源码或文档示例的真实值。该实验依赖外部协议和网络，因此不属于 correctness gate。

## 与其他文档的链接

- Agent Loop：[`02-agent-loop.md`](02-agent-loop.md)
- Durability/fault hooks：[`06-durability-and-recovery.md`](06-durability-and-recovery.md)
- Replay/Bundle：[`11-replay-and-bundles.md`](11-replay-and-bundles.md)
- Security：[`12-security-boundaries.md`](12-security-boundaries.md)
- Failure semantics：[`13-failure-semantics.md`](13-failure-semantics.md)

## Navigation

- Previous: [`13-failure-semantics.md`](13-failure-semantics.md)
- Next: [`15-code-review-guide.md`](15-code-review-guide.md)
- Related: [`docs/README.md`](README.md), [`11-replay-and-bundles.md`](11-replay-and-bundles.md)
