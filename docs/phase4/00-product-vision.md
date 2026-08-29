# Product Vision

## 1. 正式产品抽象

Feeds 的核心产品抽象正式定义为：

> 用户通过自然语言定义持续关注目标；系统理解并澄清意图，建立 durable Subscription，持续观察外部世界，
> 在满足用户关注条件时生成 Update，并通过用户级 Distribution / Notification 进行分发。

`Digest` / `Briefing` 是一种结果形态，不再是顶层产品抽象。一个关注可以要求定期整理变化、等待数值条件成立，
或等待一个可验证事件发生；三者共享同一套理解、确认、持久化、观察与用户分发边界。

它不是“手动运行一次摘要生成器”，也不是向用户展示 Agent/Harness 的教学控制台。用户购买的是持续关注、
可验证的触发理由和可控的分发关系。

## 2. Tracking Intent 的三种 execution semantics

| Semantics | 用户意图 | 执行语义 | 结果 |
|---|---|---|---|
| `BRIEFING` | 关注 AI Agent 行业动态 | Search / Observe → Select → Generate | 一条 Briefing-shaped Update |
| `CONDITION` | 深圳武汉往返机票低于 800 元提醒我 | Observe → deterministic condition evaluation | 条件成立时生成 Alert-shaped Update |
| `EVENT` | OpenAI 发布新模型就告诉我 | Observe → event detection → Evidence verification | 事件被验证后生成 Event-shaped Update |

这三个值描述的是 tracking intent 的执行语义，不是让用户选择的技术 workflow。Agent 可以从自然语言提出候选，
Application 必须根据受支持的 definition shape 确定性选择并验证；不支持或自相矛盾的候选不能 commit。

## 3. 用户的五个核心问题

产品必须始终能用用户语言回答以下问题：

1. 我正在关注什么？
2. 最近有什么值得我知道？
3. 为什么系统现在更新我？
4. 我的反馈改变了什么？
5. 如果内容还没来或失败了，我现在能做什么？

这些问题比“哪个 Run/Provider/Outbox 处于什么状态”更接近产品价值。

## 4. 共同 onboarding 与目标体验

```text
Updates
  -> Create Feed
  -> intent understanding
  -> ambiguity-driven Clarification loop
  -> Definition Proposal
  -> explicit Human Confirmation
  -> Subscription COMMIT
  -> First Observation / Update Preparing
  -> Feed Ready
  -> Read + Why Updated
  -> Feedback acknowledgement
  -> Interest evolution
  -> Manage / pause / history
```

“open-ended”表示 UI 永远根据 server-owned conversation state 决定是否继续，不假设一问一答；application
仍可用确定性 turn/deadline ceiling 防止无限消耗。

共同路径固定为：

```text
Conversation -> clarification -> proposal -> human confirmation
  -> deterministic validation -> Subscription COMMIT
```

`DONE` 只代表 Agent 已提出 proposal。workflow selection、definition validation、ownership、idempotency 与 commit
均由 Application Harness 控制。任何 execution semantics 都不能绕过 confirmation 或直接由 Model 建立关系。

## 5. 产品品质定义

### 有价值

- 首页优先展示 ready Updates，而不是订阅配置和开发者控制。
- 每条 Update 能说明命中了哪个关注范围、条件或事件，以及依据的时间和来源。
- BRIEFING 已看内容确定性降权；CONDITION/EVENT 用稳定 signal identity 防止重复 Update。

### 越来越懂我

- 明确反馈立即得到确认，并影响后续、而不是悄悄改变当前历史内容。
- 用户能看到“更关注 / 少关注”的可理解变化，并能纠正它。
- V1 只声称学习显式反馈；不把停留时长、滚动或 Delivery 状态冒充兴趣信号。

### 可控且可信

- Agent 提议的 definition 在创建长期关系前由用户确认。
- 订阅成功、Update 生成、Distribution 建立和外部 Notification 是独立结果。
- 失败文案说明用户影响和安全动作，不泄漏内部实现，也不虚构进度。

## 6. 产品成功标准

第一阶段不使用“DAU/留存”等尚无采集基础的指标冒充可测事实，先用 journey-level acceptance：

- 用户可以在刷新/重启后继续任意轮数的 clarification。
- 未经明确确认，不创建 ACTIVE subscription。
- 确认后立即看到订阅成功；首次观察/更新可以独立为 preparing、ready、no update 或需要处理。
- ready Update 能从首页进入 Feed Detail，并显示来源与“为什么现在更新”。
- CONDITION 只有在 application 对 accepted Observation 做确定性数值判断后才能生成 Update。
- EVENT 必须绑定 accepted Evidence/validation 后才能生成 Update。
- 一个 Update 的事实不因某个用户的已读状态或通知失败而改变。
- feedback 幂等；下一篇的排序变化可由 deterministic facts 解释。
- pause/edit/history 不改写旧 Digest 的 definition/profile provenance。

## 7. 当前产品事实与承诺缺口

当前 code 已证明 definition、atomic commit、first-Briefing handoff、generation、feedback 与 delivery 的独立
durable boundary，并为一个窄 flight CONDITION 证明 Fake Clock驱动的continuous Observation、Evidence、确定性
crossing/re-arm、Update/Distribution与Distribution-bound Termux Notification。P4.6 还为 exact OpenAI MODEL_RELEASED
需求证明 Fake Source Observation、Agent Event Candidate、deterministic verification、Verified Event 与复用的
Distribution/Notification；实现证据见
[`18-p46-implementation-status.md`](18-p46-implementation-status.md)。当前仍不能兑现真实价格、真实 OpenAI/Brave source、
生产常驻调度、generic notification、generic EVENT 或 correction/retraction；其他 EVENT selector 继续 fail closed。

以下是早期design checkpoint的verification archive，不是P4.5/P4.6当前gate结果；最新实现证据以各slice status文档为准：
