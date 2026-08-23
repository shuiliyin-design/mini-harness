# Product Scope

## V1 user story

用户提交：

> 帮我订阅 AI 行业动态，每天一份，600 字以内，重点关注 Agent、模型发布和开发工具。

系统保存可检查、可编辑的 Subscription。V1 不运行 daily cron；用户通过 `run subscription
now` 手动模拟某一期。成功链路能够保存 Digest、尝试通知、接收反馈，并让下一次排序使用
更新后的 Profile。

## V1 capabilities

1. 从自然语言创建结构化 Subscription，并显示规范化结果。
2. 列出、启用或停用 Subscription。
3. 手动 reserve 并触发一次 Digest generation。
4. 规划 query，调用 Search，规范化候选。
5. 确定性执行 freshness、canonical URL dedup、selection 与 ranking。
6. 让 LLM 仅基于选中候选生成 DigestDraft。
7. 确定性检查字符数、item、candidate/source reference 与 required fields。
8. 接受 workspace Digest Artifact，并从 completed Result 投影进 SQLite。
9. 通过 DeliveryService 尝试 `termux:notification`，保存 DeliveryRecord。
10. 记录 opened/liked/dismissed/saved Feedback。
11. 用固定 delta 更新 topic weights 与 seen history。
12. 下一次 query context/ranking 使用 safe profile projection。

## Explicitly out of scope

- 自动调度、daemon、cron ownership 和后台 worker；
- authentication、authorization account system、支付、多租户；
- Postgres、Redis、Celery、Kafka、Docker orchestration；
- embedding、vector database、semantic recommender、RAG；
- 大规模 crawler、全文抓取与 publisher-specific parsing；
- LLM semantic grader 作为 correctness gate；
- “通知已提交”等价于“用户已阅读”；
- 把 Brave SDK、prompt、订阅表或业务 ranking 写入 Harness core。

## Acceptance boundary

V1 的 correctness 由 FakeSearch、FakeProvider、FakeDelivery 和临时 SQLite 离线证明。真实
Brave、真实 LLM 与真实 Android notification 只做人工 integration confidence；缺少网络、API
key 或 Android 不影响测试通过。

## Implemented boundary

当前实现三条离线闭环：create/list Subscription、manual generation、Fake Search、
normalization/dedup/ranking、Fake synthesis、deterministic contract、Artifact/Result 与 SQLite Digest；
Feedback/Profile/Explainable Ranking；以及 Fake Delivery 与 authorized Termux result mapping。
此外已实现 Real Brave Search 与 Vertex-backed real LLM app adapters，以及 Fake Search + Real
Vertex、Real Brave + Real Vertex 两条 opt-in smoke。产品 HTTP API、scheduler 和真实 Termux
execution 尚未实现，这些不是测试通过后可隐式声称的功能。

上一页：[`00-overview.md`](00-overview.md) · 下一篇：
[`02-domain-model.md`](02-domain-model.md)
