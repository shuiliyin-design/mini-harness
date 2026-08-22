# Runtime 架构与依赖方向

## 读完你应该理解什么

- façade、CLI、orchestrator、Authority、状态模块和历史模块如何分层。
- 为什么 `agent.py` 可以 high fan-out，但底层模块不能反向依赖它。
- 当前 dependency DAG 如何保护 Authority 与 replay boundary。

本页以当前 Python import 为准。完整自动检查位于
[`test_v27_architecture.py`](../../tests/architecture/test_v27_architecture.py)；`dependency_graph()` 会解析整个
`mini_harness_core` AST，包括函数体中的 import。

## 外部入口

[`mini_harness.py`](../../mini_harness.py) 是兼容 façade：它 re-export 教学 API，并把命令行入口交给
`mini_harness_core.cli.main`。它不是另一个 Runtime 实现。

[`mini_harness_core/cli.py`](../../mini_harness_core/cli.py) 负责参数解析和依赖 wiring。CLI 可以依赖
`agent` 和各种 management modules；业务模块不得反向依赖 CLI。

`--self-check` 的实现放在
[`tools/self_check.py`](../../tools/self_check.py)，根目录
[`mini_harness_self_check.py`](../../mini_harness_self_check.py) 只保留旧命令兼容。这样验证入口可以组合
`agent`、Bundle 和 boundary checks，而不会制造新的 core layering 反向边。

Phase 2 实现不再占用 flat core namespace：filesystem transport 在
[`bridge/`](../../mini_harness_core/bridge/)，Environment contract/registry/Termux adapter 在
[`environment/`](../../mini_harness_core/environment/)，跨层组合在
[`integrations/`](../../mini_harness_core/integrations/)。

## 当前分层图

下面是为阅读压缩过的真实 import 方向；箭头表示“上层 imports 下层”。

```text
mini_harness.py (façade)
        |
        +---------------------> mini_harness_core.cli
        |                              |
        |                              +----> agent
        |                              +----> management/history modules
        |                              +----> mini_harness_self_check
        |
        +---------------------> public core symbols

agent (high fan-out orchestrator)
  |
  +--> providers / context / project_context / memory
  +--> planning / retry / governance / run_control / durability
  +--> authority / dispatch / protected_paths / verification
  +--> mcp / handoff
  +--> audit / evidence / artifacts / result
  +--> policy_snapshot / run_manifest / run_envelope

Phase 2 packages
  integrations.bridge_* --> bridge transport + core history/runtime
  integrations.mobile   --> core planning/evidence/integrity
  authorized dispatch   --> environment contract/closed registry
  bridge transport      -X-> environment adapter

Future apps --> integrations --> core/runtime
core/runtime and integrations -X-> apps

history and replay
  run_bundle --> result, run_envelope, run_manifest, policy_snapshot,
                 audit, evidence, artifacts, historical_types
  run_envelope --> planning, retry, verification, result_replay,
                   policy_snapshot, run_manifest, evidence, artifacts
  result --> artifacts, evidence, audit, historical_types, result_replay

leaf or near-leaf foundations
  security       integrity       providers       fault_injection
  run_control    protected_paths policy_composition
```

详细边不是手工维护的契约；如图与代码冲突，应以 architecture test 解析结果为准。

## 各区域的 ownership

### Agent orchestrator

[`agent.py`](../../mini_harness_core/agent.py) 拥有调用顺序和 live runtime assembly。它不重新实现
Plan/Retry/Checkpoint/Policy 状态机。`run_agent` 很薄，实际循环在 `_run_agent_runtime`，复杂阶段
被拆到 `_initialize_runtime_execution`、`_prepare_turn`、`_handle_*_decision` 和
`_emit_runtime_result`。

### Authority 与 Security

- [`policy_composition.py`](../../mini_harness_core/policy_composition.py)：静态 capability ceiling。
- [`authority.py`](../../mini_harness_core/authority.py)：shell classification、Approval、shell adapter。
- [`protected_paths.py`](../../mini_harness_core/protected_paths.py)：不可被 Approval 绕过的路径上限。
- [`dispatch.py`](../../mini_harness_core/dispatch.py)：sealed `AuthorizedAction` 和唯一 dispatch seam。
- [`observation.py`](../../mini_harness_core/observation.py)：raw Observation projection boundary。

这些模块分别回答不同问题，不能合并成一个 `allowed=True`。

### 独立的 Runtime state domains

- [`planning.py`](../../mini_harness_core/planning.py)：下一步是什么。
- [`retry.py`](../../mini_harness_core/retry.py)：明确失败后是否允许新 attempt。
- [`governance.py`](../../mini_harness_core/governance.py)：时间和预算是否允许继续。
- [`run_control.py`](../../mini_harness_core/run_control.py)：用户是否要求 pause/cancel。
- [`durability.py`](../../mini_harness_core/durability.py)：crash 后旧 action 可能发生了什么。

它们保持独立，由 orchestrator 显式组合，避免一个状态机同时拥有所有语义。

### Persistence 与 History

Session 保存 continuity；Audit 保存事件 trace；Policy Snapshot 保存历史 Authority 定义；Manifest
保存配置 identity；Envelope 保存执行输入和纯 transition；Evidence/Artifact/Result 保存 provenance
和交付结论。Bundle 只导出这些对象的 typed reference closure。

详见 [`04-action-lifecycle.md`](04-action-lifecycle.md) 和后续历史专题文档。

## 为什么 high fan-out orchestrator 是合理的

`agent.py` 必须同时询问很多 owner：Policy 是否允许、run 是否 running、deadline 是否过期、
checkpoint 是否 unknown、Verification 是否 pending。它因 orchestration 而 high fan-out，但底层模块
不能 import `agent`。这样纯决策模块可以独立测试，也避免让 Provider、Result 或 replay 获得执行入口。

V27 tests 明确要求：除 `cli` 外的 core 模块不得依赖 `agent`；`result` 与 `run_envelope` 也不得互相
形成 cycle，因此共享 replay 被下沉到 `historical_types.py` 和 `result_replay.py`。

递归 dependency audit 同时要求 Bridge transport 只能依赖 transport 内部模块、Bridge/Integration 不直连
Environment implementation、flat Core 不导入 Bridge transport 或 `environment.termux`。`dispatch` 只面对
Environment contract 与 Harness-owned closed registry；Termux adapter 不拥有 Authority。

## 为什么 DAG 很重要

Dependency DAG 不只是代码整洁要求：

- 如果 `result.py` import `agent.py`，历史 Result check 可能间接获得执行能力。
- 如果 `authority.py` import context/project content，非可信内容可能进入 Authority definition。
- 如果 `run_envelope.py` import live executor，replay boundary 将难以证明“零外部执行”。
- 如果 leaf module 反向依赖 orchestrator，单元测试无法隔离真正的 owner。

Historical resolver 只按 schema 与 object type 读取 JSON/JSONL；它不使用 `pickle`、`importlib` 或
`__module__` 恢复 Python 类型。因此本次源码 module path 迁移不会改变旧 Manifest、Envelope、Result 或 Bundle。

因此 `test_complete_core_dependency_graph_is_a_dag`、package boundary、historical module identity、layering
和 no-lazy-import tests 都属于
安全架构测试，而不仅是 style test。

下一步阅读：[`02-agent-loop.md`](02-agent-loop.md)、
[`03-authority-and-policy.md`](03-authority-and-policy.md)。

## Navigation

- Previous: [`00-overview.md`](00-overview.md)
- Next: [`02-agent-loop.md`](02-agent-loop.md)
- Related: [`15-code-review-guide.md`](15-code-review-guide.md)
