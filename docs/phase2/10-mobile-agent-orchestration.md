# Mobile Agent Orchestration

P2.7 已实现一条窄的教学 workflow：在一个 Bridge claim、一个 Binding、一个
Harness Run 内观察电量，以 Harness 确定性整数比较决定是否请求通知，最后交付
conditional Output Contract。实现只组合既有 `termux:battery_status` 与
`termux:notification`，没有第三 capability、GUI、daemon 或 multi-run engine。

## Observe → Condition → Act → Verify → Deliver

```text
bridge_harness_task {request, threshold}
  → one Bridge claim / Binding / Harness Run
  → Plan.observe_battery
  → Model battery action intent
  → Classification / Policy / Runtime Gates / AuthorizedAction
  → Battery Observation → accepted current-run Battery Evidence
  → Harness integer condition: battery_percentage < threshold
  → Condition Evidence + notification Step correlation
       ├─ false → conditional obligation completed without notification
       └─ true  → Model notification action intent
                  → independent Policy / ASK / Approval / AuthorizedAction
                  → Notification Observation → accepted Notification Evidence
  → model_final_candidate_received (Model identity)
  → conditional MobileWorkflowOutput
  → authoritative_candidate_finalized (Harness normalized identity)
  → authoritative Harness Result
  → Bridge projection
```

`mobile_orchestration.evaluate_battery_condition()` owns the only comparison.
`agent._advance_mobile_workflow()` advances Plan state only from durable accepted
Evidence；it never dispatches。所有 Environment invocation 仍通过
`agent._handle_environment_decision()`、`dispatch.authorize_action()` 与
`dispatch.dispatch_authorized_action()`。

## 与普通 single Action Run 的区别

普通 Environment Run 处理一次 Model tool intent，产生 Observation/Evidence，再由
Model 提交 final candidate。Mobile workflow 仍使用同一 action path，但增加：

- 两个相关 Step，而不是把两个 capability 当成两个 Bridge task；
- Battery Evidence 到 Condition Evidence 的确定性 dependency；
- branch-sensitive completion：false 分支不要求 Notification Evidence；
- notification true 分支才开放 action gate，且仍重新走 Authority；
- terminal `MobileWorkflowOutput`，用于交付结构化 branch outcome；
- 同 Run crash resume，可重用本 Run 已接受的 Evidence，而不重调已完成 action。

它不是通用 workflow DSL，也不是新的万能状态机。

## Plan / Step 复用

`mobile_orchestration.create_mobile_workflow_plan()` 调用 Phase 1
`planning.create_plan()` 创建两个 Step：

1. `observe_battery`；
2. `conditional_notification`，其 `depends_on=["observe_battery"]`。

`planning.py` 只增加可选 `condition` correlation：`source_step_id`、Battery
`evidence_id`、Condition `decision_evidence_id`、固定 expression 与 outcome。
`planning._validate_condition()` 只验证结构和 dependency；它不计算条件、不授予
Authority、不调用 registry。false 分支把“条件通知义务”完成为 not-required，未伪造
Notification Evidence。

## Deterministic condition ownership

Model 理解 request 并提出 battery/notification action intent；Bridge payload 的
`threshold` 经 `bridge_adapter.read_bridge_harness_task()` 验证为 `0..100` 的整数。
Model 不能决定比较结果。Harness 调用：

```python
percentage < threshold
```

`evaluate_battery_condition()` 只接受 `int` 且拒绝 `bool`，同时验证 percentage 与
threshold 范围。operator 固定为 `lt`；没有 Model 自由解释、字符串比较或表达式执行。

## Evidence dependency 与 freshness

真实链为：

```text
Battery Observation
  → termux_observation Evidence (accepted, run-scoped)
  → condition_decision Evidence
       references.battery_evidence_id
  → notification Step.condition
       evidence_id + decision_evidence_id + outcome
  → optional Notification Observation
  → termux_observation Evidence (request_accepted=true)
```

`evaluate_battery_condition()` 同时检查 capability、accepted verification、当前
`run_id`、`freshness.scope == "run"`、freshness run binding 与 percentage schema。
因此 raw Adapter result、Bridge 声明、Session history、其他 Run Evidence 和
historical-scope Evidence 都不能成为 condition input。

`condition_allows_notification()` 在 notification Authority chain 前再次加载 Battery
与 Condition Evidence 并核对引用、run、freshness 和 outcome。Step dependency 只说明
执行顺序；它不携带 Policy、Approval 或 AuthorizedAction。

## Notification 的独立 Authority / Approval

Battery read-only action 完成后不会生成 notification Authority。true 分支只使
notification action intent 变得 eligible；`_handle_environment_decision()` 随后仍执行：

```text
normalize arguments
→ classify_environment_capability
→ runtime/governance gates
→ ASK
→ current action Approval
→ create_action_checkpoint
→ authorize_action
→ dispatch_authorized_action
```

Battery action 的 ALLOW/Approval、Bridge claim、Condition Evidence 与旧 Run Approval
均不能代替 notification Approval。crash 在 condition 后、Approval 前时，prepared
checkpoint 恢复为 `retry_with_fresh_approval`；恢复后的 notification intent 必须产生
新的 Approval 决定。

## Conditional Output Contract

`mobile_orchestration.build_mobile_workflow_output()` 创建 immutable、fingerprinted
`MobileWorkflowOutput`，至少交付：

- `battery_percentage`；
- `notification_required`；
- `notification_request_accepted`，未执行或 unknown 时为 `null`；
- `branch`、`satisfied`、`unsatisfied_requirements`；
- exact Battery/Condition/optional Notification Evidence IDs。

| Branch | Notification Evidence | Contract | Harness Result |
|---|---:|---|---|
| `not_required` | 不需要 | satisfied | `completed` |
| `accepted` | 必须，且 `request_accepted=true` | satisfied | `completed` |
| `approval_denied` | 不存在 | `notification_not_authorized` | `incomplete` |
| `unknown` | 不接受 ambiguous observation | `notification_delivery_unknown` | `blocked` |

Harness completed 时，`agent._handle_final_candidate()` 用
`mobile_output_answer()` 绑定结构化 deliverable 和 exact Evidence refs；Model summary
不能覆盖 branch facts。拒绝/unknown 时，标准 Authoritative Result 保留非成功 status，
immutable workflow output 仍由 Bridge projection 的 `workflow_output` 安全交付。

`request_accepted=true` 只证明 Termux notification request 被接受，不证明用户看见。

## P2.7.1 Result candidate identity closure

第一次真实 Android 组合 smoke 暴露过一个实际缺陷：no-notification 与 accepted
两个 completed branch 的业务 status、Evidence 和 Bridge projection 都正确，但
`result_integrity_check()` 返回 false。原因不是 condition 或 action outcome，而是
`agent._handle_final_candidate()` 先把原始 Model summary 记录为
`final_candidate_received`，随后 mobile contract 又生成结构化 deliverable；Result
绑定了后者，旧 integrity check 却仍拿前者的 digest 比较。

当前实现明确区分三层：

```text
Model Candidate
  model_final_candidate_received / legacy final_candidate_received
        ↓ deterministic Harness normalization
Harness Authoritative Candidate
  authoritative_candidate_finalized(candidate_digest, plan/output refs)
        ↓ Result Contract
Authoritative Result
  status + answer + accepted Evidence refs + result_fingerprint
```

`result.finalize_authoritative_candidate()` 从 Result binding input/output 纯函数重算
最终 candidate metadata；`agent._persist_authoritative_candidate_finalized()` 在
`result_binding` 和 Result publish 前 fsync Harness event。mobile normalization 只可改变
status/summary/refs/reason 的候选表达，不能创造 Authority、修改 accepted Evidence ID、
改写 action outcome 或解释 raw Observation。

`result.result_integrity_check()` 对新 Run 要求 Result candidate digest 精确匹配
`authoritative_candidate_finalized`，并核对它早于 `final_result_emitted`。没有 finalized
event 的旧 Run 继续使用 legacy `final_candidate_received` 检查；实现不回写旧 Audit 或
旧 Result。普通非-mobile Run 也记录两个语义角色；没有 normalization 时两个 digest
可以相同，但角色不合并。

最终 durable ordering 是：

```text
Model candidate received
→ mobile output contract evaluated
→ authoritative candidate derived and durable
→ result_binding durable
→ Result durable
→ final_result_emitted
→ Result integrity MATCH
→ Bridge terminal marker / projection / result.ready
```

`bridge_adapter._harness_result_integrity_valid()` 是 projection 前置 gate；invalid Result
返回 `HARNESS_RECOVERY_REQUIRED`，不会发布 Bridge Result。

## Bridge claim 与多步 Run

P2.7 没有新增 task type。`bridge_harness_task` payload 支持：

```json
{
  "request": "检查当前电量，如果低于阈值就发通知",
  "threshold": 30
}
```

没有 `threshold` 时继续走普通单 Run 行为；有 threshold 时
`bridge_adapter._start_bound_harness_run()` 创建固定 mobile Plan，并把同一 Session、
Run、EvidenceStore、ResultStore 和 MobileWorkflowOutputStore 交给 `run_agent()`。
Bridge claim spans transport ownership for the whole workflow；Step 不创建 claim，claim
也不提供 per-step Authority。

Bridge outer `status=completed` 只表示 Result projection committed；业务 truth 读取
`harness_result_status`。例如 Approval denied 的 Bridge transport completed，而 Harness
Result 是 `incomplete`。

## Crash / resume matrix

| Window | Durable truth | Resume behavior | Forbidden |
|---|---|---|---|
| A. Battery Evidence 后、condition 前 | succeeded battery checkpoint + accepted Battery Evidence | 同 Run `find_step_evidence()` 重用 Battery Evidence，Harness 重算/持久化 condition | 重调 battery（Evidence 仍满足 current-Run freshness 时） |
| B. condition=true 后、notification Approval 前 | Battery + Condition Evidence；可能有 prepared notification checkpoint | 恢复相同 Plan/Run，重新取得 fresh notification Approval | 继承 battery/旧 action/旧 Run Approval |
| C. notification executing 时 crash | executing checkpoint，effect 未知 | `recover_action_checkpoint()` 转 unknown；`_resume_mobile_workflow()` 输出 unknown 并 block | resend / blind retry notification |
| D. Notification Evidence 后、Result 前 | succeeded notification checkpoint + accepted Notification Evidence | 重用三段 Evidence、完成 Step、Output 与 Result binding | 重发 notification |
| E. Authoritative Candidate durable 后、Result binding 前 | finalized candidate digest + Model/mobile/Plan refs | 同 digest 幂等复用，继续纯 Result binding | 调 Model、Adapter、Bridge action 或 Approval |

P2.7 只允许 structured mobile task 的同-Binding Run resume。普通 BridgeHarness Run
仍保留既有 `HARNESS_RECOVERY_REQUIRED` 边界；这不是通用自动恢复扩张。

## Duplicate prevention

防重不是一个布尔 flag，而是多层 identity：

- Bridge Publisher 对同 `task_id` no-replace；
- Claim + immutable Binding 固定 `harness_run_id`；
- attempt fence 排除并发 owner；
- Session checkpoint 记录 prepared/executing/terminal action；
- accepted Evidence 以 action/event/run identity 关联；
- `MobileWorkflowOutputStore.save()` immutable/idempotent；
- Harness Result 与 Bridge projection no-replace/ready-last。

因此 duplicate Bridge entry 不产生第二 notification。

## Replay boundary

`mobile_orchestration.replay_mobile_workflow_output()` 只加载历史 Evidence，重算固定
integer condition 并核对 branch/Evidence identity；它没有 Provider、Bridge Worker、
Environment registry 或 Adapter callable。replay 不刷新 battery，也不补发 notification。

`test_historical_replay_calls_both_capabilities_zero_times` 断言 replay 前后 battery 与
notification call count 不变；同时 `harness_replay_check()` 重算 Result transition，
`result_integrity_check()` 重算 finalized candidate digest。两条 replay 都不调用 Model、
Environment Adapter、Bridge 或 Approval。

## Worked trace: battery 80% → no notification

1. Model 提出 battery action intent；Harness ALLOW、dispatch、接受 percentage=80 Evidence。
2. Harness 计算 `80 < 30 == false`，创建 Condition Evidence。
3. conditional notification Step 以 not-required 完成；notification adapter call count=0。
4. Output 为 `required=false`、`accepted=null`、`branch=not_required`；Result completed。

验证：`MobileAgentOrchestrationTests.test_battery_80_completes_without_notification`。

## Worked trace: battery 20% → ASK → Approval → notification

1. percentage=20 Battery Evidence accepted；Harness 计算 `20 < 30 == true`。
2. Model 提出 notification intent；condition gate 只确认 eligibility。
3. notification 独立分类为 ASK；current action Approval granted 后才形成 AuthorizedAction。
4. Adapter `known_applied/request_accepted=true`，Harness 接受 Notification Evidence。
5. Output `required=true/accepted=true/branch=accepted`；Result completed。

验证：`test_battery_20_asks_then_accepts_notification`。

## Worked trace: battery 20% → Approval denied

1. Battery/Condition chain 与 true branch 保持有效。
2. notification ASK 被用户拒绝；adapter call count=0，Notification Evidence 不存在。
3. Output `required=true/accepted=null/branch=approval_denied`，unsatisfied 为
   `notification_not_authorized`。
4. Harness Result `incomplete`；Bridge transport Result 仍可 committed。

验证：`test_approval_denied_is_authoritative_incomplete`。

## Common Misreadings

- **“Plan 写了 notification，所以已授权。”** 错；Plan 只表达下一步与 dependency。
- **“Battery 是 ALLOW，所以 notification 也 ALLOW。”** 错；每个 action 独立分类/审批。
- **“Model 看到 20 和 30，可以直接决定 true。”** 错；Harness 重算固定 integer `<`。
- **“Session 里有旧 Evidence，所以仍 fresh。”** 错；condition 要求当前 Run accepted Evidence。
- **“Approval denied 就是 notification rejected。”** 错；未 dispatch，`accepted=null`。
- **“notification timeout 可以再发一次。”** 错；unknown side effect block，不 blind retry。
- **“Bridge completed 代表 workflow success。”** 错；必须读 `harness_result_status`。
- **“Replay 应重新读取电量以确认。”** 错；那是新 observation，不是 historical replay。
- **“Result candidate 就是 Model final answer。”** 错；mobile contract 可确定性归一化
  Harness candidate，Result 再从该 identity 计算权威状态。

## Review Anchors

- Plan schema/correlation：`planning._validate_condition()`、`planning.complete_step()`。
- deterministic condition/output/replay：
  `mobile_orchestration.evaluate_battery_condition()`、
  `condition_allows_notification()`、`build_mobile_workflow_output()`、
  `replay_mobile_workflow_output()`。
- runtime advancement/Authority：`agent._advance_mobile_workflow()`、
  `_resume_mobile_workflow()`、`_mobile_action_gate()`、
  `_handle_environment_decision()`。
- Evidence：`evidence.create_condition_decision_evidence()`、
  `evidence.evidence_gate()`、`historical_types.historical_evidence_accepted()`。
- Bridge identity/resume/projection：`bridge_adapter.read_bridge_harness_task()`、
  `run_bound_bridge_request()`、`_start_bound_harness_run()`、
  `_harness_result_integrity_valid()`、`project_harness_result_to_bridge()`。
- Candidate/Result identity：`agent._handle_final_candidate()`、
  `_persist_authoritative_candidate_finalized()`、
  `result.finalize_authoritative_candidate()`、`result.result_integrity_check()`。
- Deterministic E2E：
  `tests/integration/test_mobile_agent_orchestration.py` 的 13 个测试，以及
  `tests/unit/test_result.py` 的 finalized digest/legacy compatibility tests。

## Deep Review Questions

1. 是否存在任何从 Plan/condition/Bridge claim 直接构造 AuthorizedAction 的路径？
2. notification dispatch 前是否重新加载并验证 Battery 与 Condition Evidence？
3. other-run 或 historical-scope Battery Evidence 能否绕过 condition gate？
4. Approval decision 是否绑定当前 notification action，而非 battery/旧 checkpoint？
5. crash C 的 executing notification 是否可能进入 read-only retry loop？
6. crash D 是否只重建 Plan/Output/Result，而不调用 Adapter？
7. false branch 是否意外要求或伪造 Notification Evidence？
8. denied/unknown Output 是否把 `null` 错写成 `false`？
9. Bridge duplicate、resume 与 projection repair 是否保持同一 run id 和一次 notification？
10. replay call graph 是否完全不依赖 Provider、Bridge live state 与 Environment registry？
11. Result 是否只绑定 durable finalized candidate，而不是较早的 Model digest？
12. Bridge projection 是否在 Result integrity MATCH 前 fail closed？

## Verification status

P2.7/P2.7.1 deterministic suite 位于
`tests/integration/test_mobile_agent_orchestration.py` 与 `tests/unit/test_result.py`，覆盖
branch、freshness、retry、crash、duplicate、candidate identity、transport gate 和零调用
replay。P2.7.1 合入时全量结果为 `621 tests passing`。

2026-08-23 第一次真实 Android workflow smoke 诚实记录了 A/B Result integrity failure，
并直接促成 P2.7.1；它不是“从未存在”的问题。修复后使用新的
`task-p271-smoke-{a,b,c}-20260823` 重跑：A=47%/false/completed/calls 1:0，
B=47%/threshold 48/Approval granted/request accepted/completed/calls 1:1，
C=46%/threshold 47/Approval denied/incomplete/calls 1:0；三条 Result integrity 均
MATCH，旧 smoke/history 未修改。真实 smoke 是环境/集成信心，621 deterministic tests
才是 correctness gate。
