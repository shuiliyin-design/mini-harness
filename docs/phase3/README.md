# Phase 3：AI Digest Subscription Agent

Phase 3 在 Mini Harness 之上设计一个“小而完整”的订阅型 Agent 应用。它把一句自然语言
请求规范化为 Subscription，在用户手动触发时搜索、筛选、生成、验证并保存 Digest，随后
尝试本地通知；反馈再以确定性规则更新下一次生成使用的 Interest Profile。

repository-first design 已通过；generation、Feedback/Profile/Explainable Ranking 与 Delivery 三条
offline vertical slice 已封存。当前又实现 app-owned Real Brave Search 与 Vertex-backed LLM adapters；
其 correctness 使用 fake HTTP transports，真实网络只做 opt-in smoke。Fake/Brave 共用 Observation → candidate-set
acceptance → Evidence 路径；首次真实 Brave + FakeProvider smoke 暴露的多词 topic 匹配与 incomplete
projection 缺陷、首次 Real Vertex smoke 暴露的 fenced JSON 均先固化为 deterministic regressions
再修复。Fake/Vertex 共用同一 Output Contract；当前 loopback HTTP + minimal Web UI 只消费
`DigestApplication` DTO，scheduler 仍未实现，也未修改 Harness core。

Phase 3 release 之后的方向不再横向增加能力，而是把实例应用增量演进为 Subscription Agent Harness。
[`20-subscription-agent-harness.md`](20-subscription-agent-harness.md) 同时记录 current 与 target：Phase 3.5
Slice A 已实现 application-owned durable multi-turn conversation 和 strict `NEXT_QUESTION/REJECT/DONE`；
Slice B 已实现从 accepted DONE 到 ACTIVE relation + PENDING first Briefing + transactional outbox 的原子 product
commit，并已通过 830 个全仓 deterministic tests。代码目标 schema 为 v11；当前 demo DB 已从 ledger v9 显式
迁移至 v11且旧历史可读。Slice C manual worker与Briefing async projection已实现；daemon/scheduler、
Delivery/event outbox与distributed lease仍是 planned。

## 阅读地图

| 文档 | 回答的问题 |
|---|---|
| [`00-overview.md`](00-overview.md) | 产品链路、分层与推荐 app tree |
| [`01-product-scope.md`](01-product-scope.md) | V1 做什么、明确不做什么 |
| [`02-domain-model.md`](02-domain-model.md) | Application-owned objects 与生命周期 |
| [`03-subscription-schema.md`](03-subscription-schema.md) | 自然语言如何成为正式结构 |
| [`04-search-generation-pipeline.md`](04-search-generation-pipeline.md) | Search、Evidence、ranking、synthesis 的顺序 |
| [`05-harness-integration.md`](05-harness-integration.md) | 哪些进入 Harness Run，哪些只是 CRUD |
| [`06-output-contracts.md`](06-output-contracts.md) | deterministic contract 与 semantic quality |
| [`07-personalization-and-recommendation.md`](07-personalization-and-recommendation.md) | 可解释 Profile 与排序规则 |
| [`08-delivery-and-feedback.md`](08-delivery-and-feedback.md) | 通知、Interaction 与 profile update |
| [`09-failure-and-recovery.md`](09-failure-and-recovery.md) | retry/incomplete/blocked/failed 与幂等恢复 |
| [`10-testing-and-e2e.md`](10-testing-and-e2e.md) | Offline Deterministic Correctness Gate 与真实服务 evidence |
| [`11-design-decisions.md`](11-design-decisions.md) | 已接受决定、代价与改变条件 |
| [`12-review-guide.md`](12-review-guide.md) | 分层、安全、数据与测试审查路径 |
| [`13-first-vertical-slice.md`](13-first-vertical-slice.md) | 三条 offline slice 的 checkpoint 记录 |
| [`14-product-readiness-review.md`](14-product-readiness-review.md) | 从真实用户 journey 逆向评估 Demo Release 缺口与优先级 |
| [`15-application-facade-and-run-lifecycle.md`](15-application-facade-and-run-lifecycle.md) | Public application DTO、Subscription/Run lifecycle、恢复与 Product E2E |
| [`16-cli-bootstrap-and-readiness.md`](16-cli-bootstrap-and-readiness.md) | Thin CLI、显式 app config、shared bootstrap 与 no-I/O readiness |
| [`17-application-admin-recovery.md`](17-application-admin-recovery.md) | Durable facts 派生的 admin recovery allowlist、claim/audit 与 worked traces |
| [`18-loopback-http-and-web-ui.md`](18-loopback-http-and-web-ui.md) | Loopback API、极薄手机 UI、安全与 HTTP Product Integration Journey |
| [`19-llm-structured-output-reliability.md`](19-llm-structured-output-reliability.md) | Vertex lexical diagnostics、provider acceptance 与五层 release evidence |
| [`20-subscription-agent-harness.md`](20-subscription-agent-harness.md) | Subscription Agent Harness：Slice A/B current、worker/progress target、commit/outbox/crash matrix |

## 一句话边界

```text
Application state + orchestration
    -> Harness Run / Authority
        -> app-provided Search adapter or Environment integration

Harness is infrastructure; Digest Agent is the product application.
```

上一层导航：[`docs/README.md`](../README.md) · 应用占位：
[`apps/digest_agent/README.md`](../../apps/digest_agent/README.md)
