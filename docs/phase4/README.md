# Phase 4 — Product-driven Agent Harness Design

Phase 4 先设计 Feeds 产品，再从用户价值反推 Agent 与 Harness。本文档集以当前 repository、schema v14、
`DigestApplication`、loopback HTTP/Web UI 和离线测试为事实源；不把旧 transcript 或未来设想写成现状。

已确认的顶层产品抽象是：用户用自然语言定义持续关注目标，系统经必要澄清和明确确认建立 durable Subscription，
持续观察外部世界，在满足关注条件时生成 Update，再通过用户级 Distribution / Notification 分发。`BRIEFING`、
`CONDITION`、`EVENT` 是三种 execution semantics；Digest/Briefing 只是其中一种结果形态。

推导顺序固定为：

```text
User Need
  -> Product UX
  -> Product State
  -> Agent Capability
  -> Harness Requirement
  -> Implementation
```

设计 checkpoint 已完成；P4.1 Confirmed Feed Creation、P4.1.1 Intent-driven Onboarding Correction、P4.2
Updates / Feed Detail 与 P4.3 Flight CONDITION Vertical Slice 已实现。实现状态与可复核证据见
[`10-p41-implementation-status.md`](10-p41-implementation-status.md) 和
[`11-p42-implementation-status.md`](11-p42-implementation-status.md)、
[`12-p411-intent-onboarding-correction.md`](12-p411-intent-onboarding-correction.md) 和
[`13-p43-flight-condition-status.md`](13-p43-flight-condition-status.md)。P4.4+ 仍是设计，不应从本文档推断为已实现。

## 阅读顺序

1. [`00-product-vision.md`](00-product-vision.md)：产品承诺、核心问题与成功标准。
2. [`01-user-journeys-and-ia.md`](01-user-journeys-and-ia.md)：端到端 journey 与推荐 IA。
3. [`02-wireframes.md`](02-wireframes.md)：七组低保真移动端 wireframe。
4. [`03-product-state-projections.md`](03-product-state-projections.md)：durable truth 到用户状态的投影。
5. [`04-agent-capability-map.md`](04-agent-capability-map.md)：Agent / deterministic 边界。
6. [`05-harness-requirement-map.md`](05-harness-requirement-map.md)：现有支持、应用缺口与真正的 Harness 缺口。
7. [`06-current-ui-gap-analysis.md`](06-current-ui-gap-analysis.md)：基于当前 `web.py` 与 HTTP tests 的 gap analysis。
8. [`07-incremental-slice-plan.md`](07-incremental-slice-plan.md)：从 User Value 开始的 P4 slices。
9. [`08-design-decisions.md`](08-design-decisions.md)：已作决定、推迟决定与非目标。
10. [`09-review-guide.md`](09-review-guide.md)：产品、边界、状态与实现前 review checklist。
11. [`10-p41-implementation-status.md`](10-p41-implementation-status.md)：P4.1 实现边界、设计差异与验证证据。
12. [`11-p42-implementation-status.md`](11-p42-implementation-status.md)：P4.2 Updates、Feed Detail、状态投影与验证证据。
13. [`12-p411-intent-onboarding-correction.md`](12-p411-intent-onboarding-correction.md)：P4.1.1 intent、默认值来源与兼容边界。
14. [`13-p43-flight-condition-status.md`](13-p43-flight-condition-status.md)：P4.3 typed Observation、确定性判断与 Update/Distribution 证据。

## 当前事实摘要

- durable 多轮 `NEXT_QUESTION` 已实现，默认 application ceiling 为 8；UI 没有写死轮数。
- Conversation Agent 只提出带 user-turn 来源的 intent candidate；application 在 `DONE` 后确定性补充 product/policy
  defaults。clarification 由 material ambiguity 驱动，不再为字数、条数等内部配置凑字段。
- `DONE` 通过 deterministic validation 后只产生 durable `DEFINITION_ACCEPTED` proposal；浏览器显示 Definition
  Confirmation，并区分“你告诉我的”与“系统默认设置”；只有用户明确确认才调用 product commit。
- BRIEFING product commit 原子产生 ACTIVE Subscription/UserSubscription、PENDING first Briefing 与 durable promises；
  CONDITION commit 改为原子产生 Tracking Definition/policy/Observation request，不创建 Briefing reservation 或
  `FIRST_BRIEFING_REQUESTED`。
- first Briefing 已有 PENDING/RUNNING/READY/INCOMPLETE/FAILED/BLOCKED 投影；P4.1 Web runtime 会在确认响应写出后及
  server 启动时唤醒现有 durable worker。持续 cadence 仍未实现。
- Updates 已成为 `/` 主入口，按 ready、needs attention/failed、preparing、no update 分组；读取不会推进 worker。
- Feed Detail 展示历史内容、用户可理解的来源和时间、当前 definition，以及每期绑定的历史 definition snapshot。
- “为什么推荐”已用历史 topic/focus、Profile snapshot、freshness 与 seen facts 做只读 deterministic 解释；P4.7 的
  feedback acknowledgement 与 bounded reason-code DTO 尚未实现。
- liked/dismissed/saved/opened 会幂等、原子地更新 InterestProfile；P4.2 用户 UI 尚未提供 feedback controls 或
  可理解的兴趣演化。
- Following 已承接关注列表；product-safe pause/resume 与 definition edit 现规划在 P4.8，不能直接复用只更新 compatibility
  Subscription 的旧控制。
- 一个窄 flight CONDITION 已实现：Application 确定性选择 workflow，Fake provider 产出 typed CNY 往返价格，Evidence
  acceptance 后由 versioned comparator 判断 `observed_price < threshold`；未命中是 durable `NO_UPDATE`。
- selector 只有在 definition 明确满足 BRIEFING shape 时选择 `BRIEFING`，只有深圳—武汉 9 月往返机票的受支持
  criterion 才选择 `CONDITION`；其他 CONDITION、EVENT 与 UNKNOWN 都在 commit 前 fail closed。
- 命中 CONDITION 才创建 durable Update，并为当前单一 UserSubscription 创建独立 Distribution；相同 logical signal
  跨 retry 复用，历史 Update 绑定当时 Definition version 与 Evidence。本轮不创建 Notification。
- 现有 READY Digest 继续通过 read adapter 表现为 BRIEFING Update；历史 Digest 表与包名未重命名。EVENT、continuous
  observation、scheduler、真实 flight API、Notification、shared execution 仍未实现；Fake flight 只执行首次 one-shot 检查。
- legacy Delivery 仍以 `digest_id + user_id` 为中心；P4.3 的新 Distribution truth 尚未接入外部 Notification。

## Phase 4 约束

- 最终用户文案不出现 Harness、Evidence、Artifact、Outbox、Run、Provider、worker、CLI 或 recovery action。
- Product confirmation 不是 Harness Approval；Subscription ACTIVE 也不等于 Briefing READY。
- Product read model 可以聚合现有 truth，但不得创造新的 durable truth 或掩盖 unknown。
- 先证明现有 Harness 是否足够；产品字段、状态机、scheduler、outbox 与 UI 不进入 `mini_harness_core`。
- 不为了显得 Agentic，把导航、确认、状态投影、排序、幂等、调度或反馈计分交给 LLM。
- Conversation schema ≠ Definition schema；required internal fields ≠ required user questions；clarification 由 ambiguity
  驱动，不由 schema completion 驱动。
- Tracking Definition 与 execution/presentation/distribution policy 分离；字段优先级固定为 explicit user preference
  > confirmed clarification > product default > internal policy，并保存 provenance。
