# Mini Harness 教学总览

## 读完你应该理解什么

- Mini Harness 为什么不把 Agent 简化成“LLM + `while` loop + tools”。
- Model、Harness、Environment 三层分别拥有什么权力。
- V0–V28 最终组合出的核心 Runtime 能力。
- 一次任务如何从模型意图走到可审计的 Authoritative Result。

Mini Harness 是一个使用 Python 标准库实现的教学型 Agent Harness。它的目标不是提供
production framework，而是把真实 Agent Runtime 中容易混在一起的概念拆开：模型负责提出
下一步意图，Harness 负责权限、状态、持久化和事实约束，Environment 只返回执行结果。

入口 façade 是 [`mini_harness.py`](../mini_harness.py)，主循环在
[`mini_harness_core/agent.py`](../mini_harness_core/agent.py)。离线示例 Provider 在
[`mini_harness_core/providers.py`](../mini_harness_core/providers.py)；它让核心行为无需真实 LLM
或网络即可测试。

## 为什么不止是 LLM + while loop + tools

下面这种循环适合演示 tool calling，却没有回答关键工程问题：

```python
while True:
    decision = model(messages)
    if decision["type"] == "tool_call":
        messages.append(run_tool(decision))
    else:
        return decision["final_answer"]
```

它没有说明：谁能授权副作用、Approval 是否可复用、crash 后工具是否已经执行、timeout 是否
能 retry、旧 Evidence 能否证明当前文件、模型说 `completed` 时任务是否真的完成。Mini Harness
把这些问题交给独立的 Harness-owned 模块，而不是依赖模型自律。

## 三层模型

### Model：提出 Intent

Provider 返回 `tool_call`、`final_answer` 或 memory candidate。返回值是不可信 candidate，不能
直接获得执行 Authority，也不能决定最终 Result status。相关实现：
[`providers.py`](../mini_harness_core/providers.py) 和 `agent._prepare_turn`。

### Harness：拥有 Authority 与 Runtime Truth

Harness 负责 Plan、Policy、Approval、`AuthorizedAction`、checkpoint、retry、deadline、
Verification、Evidence、Artifact、Output Contract、Result Binding、Audit 和 replay。各状态由
对应模块拥有，`agent.py` 只编排顺序。

### Environment：产生 Observation

Shell、MCP 或 Subagent adapter 执行一次已授权 action，并返回 raw Observation。Environment
不决定该 Observation 是否足够完成 Plan。raw output 必须先经过
[`observation.persisted_safe_observation`](../mini_harness_core/observation.py)，才能进入 Session、
model context 或历史对象。

## V0–V28 最终能力地图

当前仓库形成了以下组合能力：

- 统一 model decision protocol 与离线/真实 Provider adapter。
- Shell/MCP classification，`ALLOW` / `ASK` / `DENY`，Policy composition 和 protected paths。
- fresh human Approval 与 sealed `AuthorizedAction` dispatch seam。
- Plan、step evidence、bounded replan。
- retry classification、attempt budget、deterministic backoff。
- pause/cancel、deadline、tool timeout、action/subagent budget。
- durable action checkpoint、crash recovery、targeted reconciliation。
- Full Session History、Long-term Memory、Working Context 的概念分离。
- MCP transport/registry 与 attenuated Subagent handoff。
- Audit、Policy Snapshot、Run Manifest、Run Envelope。
- Evidence、Artifact、Output Contract、Authoritative Result。
- deterministic historical replay、portable Bundle 和 offline self-check。

历史版本到当前模块的对应关系后续应由单独的 version map 文档维护；当前设计请以源码和测试为准。

## 总地图

```text
User Task
   |
   v
Model Intent
   |
   v
Planning
   |
   v
Classification --> Effect
   |
   v
Static Policy
   |
   v
Runtime Gates (run control / deadline / budget / durability / verification)
   |
   v
Approval (only for ASK)
   |
   v
AuthorizedAction
   |
   v
Tool / MCP / Subagent
   |
   v
Raw Observation
   |
   v
Safe Observation Projection
   |
   +--> Verification --------+
   |                          |
   +--> Reconciliation -------+
                              v
                           Evidence
                              |
                              v
                           Artifact
                              |
                              v
                       Output Contract
                              |
                              v
                    Authoritative Result
                              |
                              v
                         Final Answer
```

其中 `Final Answer` 是 Result 的展示内容，不是完成权限。完整绑定逻辑位于
[`result.py`](../mini_harness_core/result.py) 和 `agent._emit_runtime_result`。

## 一个具体例子

任务是“创建 `report.md`”：

1. Model 请求 `echo hello > report.md`。
2. Shell classification 得到 `ASK` 和 `side_effecting`。
3. Policy、protected-path、run control、deadline 和 budget 允许继续。
4. Human Approval 被授予，Harness 创建 `AuthorizedAction`。
5. dispatch 先持久化 `executing`，再执行一次 shell。
6. Tool success 只说明命令返回 0；Harness 仍设置 Verification obligation。
7. Model 请求 `cat report.md`，Harness 确认它是相关的 read-only verification。
8. fresh Observation 形成 Evidence，满足时生成 accepted Artifact。
9. Output Contract 满足后，Result Binding 才允许 `status=completed`。
10. Audit、Envelope 和 Bundle 保存安全 identity，可在不重执行 Tool 的情况下离线 replay。

对应系统测试是
[`test_end_to_end_runtime.py`](../tests/e2e/test_end_to_end_runtime.py) 中的 Golden scenario。

## Phase 1 学习目标与边界

Phase 1 重点是理解单 Run Agent Runtime：权限在哪里产生、事实如何建立、失败后什么仍为真、
历史记录能证明什么。它明确不提供 distributed execution、multi-run orchestration、GUI、性能
dashboard、通用事务系统或 production-grade sandbox。

下一步阅读：[`01-architecture.md`](01-architecture.md) 和
[`02-agent-loop.md`](02-agent-loop.md)。

## Navigation

- Previous: [`docs/README.md`](README.md)
- Next: [`01-architecture.md`](01-architecture.md)
- Related: [`15-code-review-guide.md`](15-code-review-guide.md), [`18-version-learning-map.md`](18-version-learning-map.md)
