# P4.1.1 Implementation Status — Intent-driven Onboarding Correction

## 1. 用户问题与 root cause

人工验收发现，输入“深圳往返武汉的机票优惠”会变成 AI Feed。真实原因在默认 Fake Definition Agent：它只识别
“订阅”前缀，其他输入直接回落到 hardcoded `AI 行业动态`，并且没有字数时固定追问 `max_chars`。真实 Vertex prompt
也要求对话提供 topic、language、cadence、limits、focus 和 delivery，等价于要求模型通过问题补齐内部 Definition schema。
Confirmation 随后把这些字段平铺展示，默认值看起来像用户选择。

Subscription legacy parser、Definition normalization 和历史投影没有另一条 AI fallback。Fake Search 的 AI corpus 是当前
briefing demo fixture，不是 onboarding root cause；本 slice 不把 flight tracking 后端塞进 Definition 修复。旧 durable
Definition/Digest snapshot 保持 immutable，不静默重写历史。

## 2. 新的 contract

```text
user turns
  -> Conversation Agent v2 candidate
       topic/subject, constraints, goal, trigger, time_window,
       locations, focus_topics, explicitly stated preferences
       + source_turn for every value
  -> deterministic application materialization
       explicit > confirmed > product default > internal policy
  -> validated durable Definition v2 proposal
  -> user confirmation
  -> existing atomic product commit
```

Conversation candidate 与 durable Definition 是两个 schema。Agent 不输出它没有从 user turn 得到的偏好；application
填入 `language=zh-CN`、`max_chars=600`、`max_items=5`、`delivery=none` 等 product defaults，以及当前 compatibility
`cadence=daily` policy default。每个 durable field 标记以下一种 provenance：

- `USER_EXPLICIT`：首个 user turn 已提供；
- `USER_CONFIRMED`：后续 clarification user turn 提供；
- `PRODUCT_DEFAULT`：产品体验默认；
- `POLICY_DEFAULT`：内部执行/兼容 policy。

`source_turn` 必须存在于当前 durable conversation history，不能由 Model 指向未来或不存在的回答。legacy v1
Definition 继续可读取；confirmation 对缺 provenance 的旧 proposal 标为 `SYSTEM_INFERRED`，不追认为用户选择，也不修改
durable row。对 max chars/items、language、cadence、delivery 等可确定性识别的 explicit preference，application 还会
回查 source user turn；Model 仅给一个存在的 turn number、但原文没有该偏好时，candidate fail closed。

## 3. Clarification decision rule

`NEXT_QUESTION` 只在答案会显著改变以下任一项时成立：追踪什么、何时响应、是否能满足用户目标。一次只问一个
当前最高价值的歧义，使用用户语言。禁止仅为了补齐 language、cadence、max chars、max items 或 delivery 等字段而追问。
信息已足够则直接 `DONE`；多轮上限仍由 application 保证，但实际轮数由 ambiguity 决定。

默认 Fake flight journey 在 route 后先问 travel window；若用户只回答月份，剩余“什么价格算优惠”仍会产生第二个
material question；给出 threshold/降价条件后直接 `DONE`。v2 protocol gate 同时拒绝“最多几条/多少字”、语言设置、
本机通知和 schema/config 式问题，不只依赖 prompt 自觉。

Fake product journeys 的预期如下：

| 输入 | 结果 |
|---|---|
| 帮我关注 AI Agent 行业动态 | 询问更关心产品、技术还是应用；回答后 `DONE` |
| 帮我关注深圳往返武汉的机票优惠 | 先问直接影响比价的日期；回答日期/阈值后 `DONE`，topic 保持机票 |
| 关注深圳到武汉 9 月往返机票，低于 800 元提醒我 | 首轮 `DONE`，保留地点、时间、价格约束与 trigger |
| 关注 OpenAI 新模型发布，有新模型就提醒我 | 首轮 `DONE`，不强制询问 cadence 或输出长度 |

## 4. UI 与 commit 边界

Definition Confirmation 分为“你告诉我的”和“系统默认设置”。主题、目标、条件、trigger、时间、地点与重点按
provenance 放入对应分组；语言、整理频率、条数、长度和通知默认值不会伪装成用户亲选。调整仍新增 durable user turn，
重新生成 proposal，不创建 Subscription；只有“确认订阅”进入原有 atomic commit。刷新、重启和重复确认语义不变。

commit 后的 Feed Detail projection 继续展示 goal、constraints、trigger、time window 与 locations，避免 flight intent 在
历史/详情页又退化成摘要配置。旧 snapshot 仍按原版本读取，不做批量改写。

## 5. 最小 Definition 泛化与诚实边界

原 Definition 只含 topic/focus 与摘要 rendering 参数，确实过度绑定资讯摘要。v2 只增加最小 tracking intent：
subject/topic、constraints、goal、trigger、time window、locations；没有建设 provider-specific 航班字段或万能 DSL。
commit 时这些 intent facts 保存在 immutable Definition snapshot，同时确定性投影到现有 Subscription execution fields。

这让产品能准确确认“想关注什么”，但当前 Search/briefing pipeline 仍不能保证查询实时票价、判断数值阈值、按事件自动
提醒或按 cadence 调度。它们是未来 tracker capability / application runtime 产品缺口，不是本轮 Harness core gap。

## 6. Harness assessment

现有 bounded multi-turn execution、strict structured output、durable Result 和 application-owned Authority 已支持该设计。
新增的是 application contract、deterministic materializer、durable Definition v2 与 UI projection；
`mini_harness_core` 无修改。没有发现阻塞 P4.1.1 的新 Harness capability gap。

## 7. Verification record

```text
focused domain/provider/conversation/application/HTTP tests: PASS（91 tests）
Fake onboarding journeys: PASS（AI / flight / explicit-threshold / event-trigger）
real Vertex Definition HTTP compatibility smoke: PASS
  （5 Definition calls；0 briefing Search/Vertex/Delivery calls；ACTIVE/PENDING）
python -m unittest -q: PASS（883 tests）
python mini_harness.py --self-check: PASS
Create/Updates/Feed inline JavaScript syntax: PASS（node --check）
git diff --check + docs whitespace scan: PASS
real-smoke configured-secret runtime artifact scan: PASS
git diff --exit-code -- mini_harness_core: PASS（empty）
Browser Engine E2E: NOT RUN
```

HTTP/UI regression 指真实 loopback HTTP server、页面 contract 与 durable SQLite state 的自动测试；它不冒充 Browser
Engine E2E。真实 smoke 关闭 first-briefing wake，因此确认 commit 之前及响应内没有 Search、briefing generation 或 Delivery。
