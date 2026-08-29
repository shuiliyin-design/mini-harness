# Product State Projections

## 1. 原则

Product state 是多个 durable truth 的只读投影，不是把所有表压成一个新状态机。projection 可以降低用户认知负担，
但不得：

- 用较新的 downstream 状态覆盖较早的 business truth；
- 把缺少数据猜成成功或失败；
- 让一次 GET/polling 触发 worker、retry 或外部调用；
- 向用户泄漏 Outbox/Run/Harness/Provider 名称；
- 把 `ACTIVE` 翻译成“首篇已完成”。

## 2. 用户可见的正交状态

```text
Feed relationship:   draft | active | paused | completed
Definition flow:     clarifying | ready_to_confirm | rejected | needs_attention
Observation cycle:   pending | observing | evaluated | failed | needs_attention
Latest update:       absent | ready | failed | needs_attention
Distribution:        pending | available | read | suppressed | failed
Notification:        off | pending | sent | unavailable
```

`draft` 在 V1 不要求新增 durable Subscription state：未 commit 的 durable Conversation 充当 creation draft。
只有 subscription transaction COMMIT 后才有 `active` Feed。

## 3. Conversation / Definition 投影

| Durable application facts | Product state | 用户文案 | 可用动作 |
|---|---|---|---|
| turn reserved/running | `clarifying` + busy | 正在理解你的回答 | 离开并稍后返回；不重复发送 |
| `WAITING_FOR_ANSWER` + question | `clarifying` | 还需要确认一件事 | 回答、保存退出 |
| `DEFINITION_ACCEPTED` + no activation | `ready_to_confirm` | 请确认关注范围 | 确认、调整 |
| `REJECTED` | `rejected` | 当前不能创建这个关注 | 重新描述 |
| `INCOMPLETE/turn_limit_reached` | `needs_attention` | 还没能确认关注范围 | 继续/重新开始（取决于安全事实） |
| processing turn after refresh | `clarifying` + busy | 正在处理上一次回答 | polling；不创建重复 turn |

关键决定：`DEFINITION_ACCEPTED` 必须投影为 confirmation screen，而不是自动 commit。初次创建和 material edit
默认都需要用户确认；同一已确认 candidate 的幂等 replay 不再询问。

confirmation 还必须按 durable provenance 分组：`USER_EXPLICIT/USER_CONFIRMED` 属于“你告诉我的”，
`PRODUCT_DEFAULT/POLICY_DEFAULT` 属于“系统默认设置”。缺少 provenance 的 legacy candidate 只能标为
`SYSTEM_INFERRED` 并请用户确认，不能倒推为用户亲选。topic、constraints、goal、trigger、time/location 等 tracking
intent 在 commit 后继续进入 Feed Detail projection，不能退化成只剩摘要长度和条数。

## 4. Subscription 投影

| Durable facts | Product state | 含义 |
|---|---|---|
| no activation binding | no Feed | Agent DONE 不能产生关系 |
| product + relation both `ACTIVE` | `active` | 关注关系已建立；不保证内容 ready |
| product + relation both `DISABLED` / legacy enabled false | `paused` | 不再请求未来更新；历史保留 |
| temporal lifecycle `COMPLETED` + `TIME_WINDOW_ENDED` | `completed` | 有界关注已正常结束；不再观察，历史保留 |
| product/relation facts conflict or absent companion | `needs_attention` | 不向用户猜 active；需要内部诊断 |

当前 façade 的 `enabled`、`product_status` 与 relation status 是分离字段。Application 对 P4.4 Flight CONDITION 优先使用
canonical temporal lifecycle 输出 sealed `active/paused/completed/needs_attention`；compatibility product/relation 在 completed
时仍写 `DISABLED`，浏览器不能据此自行猜 completed。过期只由 deterministic tick 写入，GET 不推进状态。

## 5. Observation / Update 投影

目标 projection 必须先区分“完成了一次观察但没有满足关注条件”和“执行失败”。前者是成功的 `no_update`，后者才是
`failed`；没有 durable fact 时保持 absent/pending，不能猜测。

| Durable execution facts | Product state | 用户解释 |
|---|---|---|
| observation request durable，尚未执行 | observation `pending` | 已关注，等待首次/下次检查 |
| observation 执行中 | observation `observing` | 正在检查新的变化 |
| accepted Observation + condition/event 未满足 | observation `evaluated` + update `absent` | 已检查，本次没有需要提醒的变化 |
| accepted Observation + verified trigger + Update | observation `evaluated` + update `ready` | 有一条新 Update |
| plausible EVENT candidate + support/coverage/conflict gate 未完成 | observation `needs_attention` + update `absent` | 这次发现了可能的变化，但证据还不足；会继续关注 |
| execution 明确失败 | observation `failed` | 本次没有检查成功，Feed 仍 active |
| effect/current truth 不明确 | observation/update `needs_attention` | 状态不明确，不自动重做 |

`CONDITION` 的 trigger fact 必须包含 normalized observed value、operator、threshold、unit 与 evaluation rule version；
`EVENT` 必须包含 accepted Evidence refs 和 validation outcome。UI 只显示用户可理解的事实，不泄漏内部 rule hash、
Evidence ID 或 Harness Result。

EVENT 的 successful `NO_UPDATE` 只用于完整 observation/detection/verification 后的 `NO_EVENT_FOUND`、
`DUPLICATE_VERIFIED_EVENT` 或明确 `OUTSIDE_SCOPE`。缺 official support、conflicting evidence、时间/模型名无法确认、result
coverage truncated 都投影为 `verification incomplete`，不能伪装成“没有新事件”。完整语义见
[`17-p46-verified-event-semantics.md`](17-p46-verified-event-semantics.md)；exact OpenAI MODEL_RELEASED 的 Fake runtime 与
HTTP/UI 投影已实现，其他 EVENT 仍不受支持。

### 当前 Briefing compatibility 投影

当前 `DurableOutboxWorker.briefing_status` 已提供基础映射：

| Durable run/work facts | 当前 public state | 目标 product state | 用户解释 |
|---|---|---|---|
| reservation exists, run absent/reserved | `PENDING` | `preparing` | 已关注，资讯等待准备 |
| run `running/running_recovery` | `RUNNING` | `preparing` | 正在查找并整理 |
| run completed + Digest exists | `READY` | `ready` | 可以阅读 |
| authoritative incomplete / completed without Digest | `INCOMPLETE` | `no_update` 或 `failed` | 必须按 safe failure code 区分“没有合格内容”和执行失败 |
| explicit application failure | `FAILED` | `failed` | 本次没有准备好，Feed 仍 active |
| ambiguous/recovery required | `BLOCKED` | `needs_attention` | 状态不明确，不自动重做 |

`INCOMPLETE` 不能一律显示失败：真实 empty results 可能是“本期未发现值得推送的变化”，而 provider/contract
incomplete 是“本次没有准备好”。当前 safe failure provenance 已足够做初步 deterministic 分类，但需要一个
application-owned product mapping，而不是在 JS 内散落英文映射。

表中的 Digest/Briefing 是当前 storage compatibility facts。目标 application projection 把 READY Digest 适配成
`update_type=BRIEFING` 的 Update；不要求立即重命名历史表，也不能把这种适配误认为 CONDITION/EVENT 已实现。

## 6. Updates/Home 聚合规则

Home read model 的排序和分组必须 deterministic：

1. 有 `available` Distribution 且未读的 ready Update，按 `occurred_at/created_at` 倒序；
2. `needs_attention/failed` 且用户可采取动作的 Feed；
3. 正在 observation/preparing 的 Feed；
4. 已读历史不占据 Home 首屏，可在 Feed Detail 查看。

当前 BRIEFING 读取模型可继续由 `subscriptions + briefing reservation + digest run + digests + interactions` 派生。
目标多 workflow read model 必须从 Update + Distribution 读取用户可见内容，以 active `UserSubscription` 校验
recipient；它不需要新 Harness state，也不应读取 raw Harness Result。

## 7. Recommendation / Feedback / Interest 投影

### Why recommended

来源 facts：

- `subscription_topic` / `focus_topics`：与确认过的 definition 匹配；
- `profile_weight`：明确 feedback 导致的 topic 权重；
- `freshness`：确定性发布时间 bucket；
- `already_seen_penalty`：历史 seen content；
- provider `recommendation_reason`：只能作为候选文案，不能覆盖上述事实。

目标 `WhyRecommendedView` 应输出 1–3 条用户句子和 bounded reason codes；不暴露分数、projection identity、
Evidence ID 或 rule hash。

### Feedback

| Durable fact | Product projection |
|---|---|
| first event applied | 已记录，将影响后续更新 |
| same stable event replay | 已记录，不重复累计 |
| transaction failed | 未能保存，旧兴趣不变 |
| feedback on old Digest | 合法；只影响未来 generation |

### Interest evolution

当前 InterestProfile 只有 current weights，Interaction/ProfileUpdate history 已 durable。目标历史视图应从这些
facts 派生“何时、因什么 feedback、topic 从多少变化到多少”，用 `更关注/少关注/无明显偏好` 文案，不把 raw
integer weight 当主要 UI。

## 8. Distribution 与 Notification 投影

Distribution 独立于 Update 事实：

| Durable distribution facts | Product state |
|---|---|
| Update 已创建，目标 UserSubscription 尚未绑定 | `pending`；不能向任何用户显示 |
| active UserSubscription binding 已 commit | `available` |
| 用户对该 binding 产生稳定 read event | `read` |
| 分发 policy 明确抑制 | `suppressed`；Update 仍存在 |
| binding 创建明确失败 | `failed`；不改写 Update |

Notification/Delivery 独立于 Feed、Update 和 Distribution：

| Durable delivery facts | Product state |
|---|---|
| preference none / no request | `off` / not requested |
| reserved/pre-dispatch | `pending` |
| accepted + known_applied | `sent`；只表示 request accepted，不表示 user seen/read |
| explicit failed + not_started | `failed`，可安全再次请求 |
| unknown certainty | `unavailable`，禁止 blind resend；UI 不暴露 certainty enum |

通知请求必须引用 Distribution，而不是直接用 `digest_id + user_id` 推导 recipient。通知失败不从 Home 移除
available Update，也不把 Feed、Update 或 Distribution 标成失败。
