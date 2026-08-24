# Phase 3.5 Architecture Review

> Review date: 2026-08-24. Slice E checkpoint `b02b8d2` 已 push 到 `origin/main`。
> 本文只描述当前 repository、schema v13、durable demo DB v13 与 863-test gate 能证明的事实。
> Delivery Outbox、真实 broker/consumer、daemon/scheduler、shared Briefing 与 product operations aggregate view
> 均未实现；下文出现这些名词时只代表 gap 或候选方案。

## 1. Executive decision

Phase 3.5 已经真实体现一个生产型 Subscription Agent Harness 最核心的结构：多轮协议不会直接创建产品事实；
Definition、Subscription/UserSubscription 与两个不同的durable promise有明确transaction boundary；不确定的
Agent execution、relation publication和Delivery effect各自保留独立truth/certainty。三个最初production lessons
在Demo correctness范围内均已关闭。

当前不建议机械增加第三个Outbox。现有Delivery产品语义是用户显式调用，而不是“每个READY Digest必须自动
投递”；因此Digest READY后没有Delivery intent是明确事实，但暂时不是违反current product contract的blocker。
Phase 3.5应先freeze。若继续一小步，优先做只读Product Operations View；若要转向正式shared-content产品，先
设计shared Briefing与user-level Delivery cardinality，再决定Delivery Outbox的transaction owner与event identity。

## 2. 当前真实产品链

图例：`[T]` durable business/product truth；`[W]` durable work/publication promise；`[A]` Agent Harness
authoritative execution truth；`[P]` derived/downstream projection；`--async-->` durable asynchronous boundary；
`--effect-->` 外部副作用边界。

```text
HTTP/Web user turns
  -> Conversation + ConversationTurn [T]
  -> Definition Agent (Vertex/Fake, structured attempts)
  -> Harness authoritative terminal Result [A]
  -> NEXT_QUESTION | REJECT | DONE DefinitionOutcome [T, candidate only]
  -> deterministic Definition protocol + business validation
  -> SQLite BEGIN IMMEDIATE
       SubscriptionDefinition snapshot [T]
       Subscription / ProductSubscription ACTIVE [T]
       UserSubscription ACTIVE [T]
       BriefingReservation(application_run_id, PENDING) [W]
       FIRST_BRIEFING_REQUESTED PENDING [W]
       USER_SUBSCRIPTION_CREATED PENDING [W]
       activation binding [T]
     COMMIT
  -> HTTP/UI: subscribed + first briefing PENDING [P]

  A. FIRST_BRIEFING_REQUESTED --async/manual tick-->
       DurableOutboxWorker claim [W ownership]
       -> reuse reserved application_run_id
       -> Search Observation -> Evidence
       -> deterministic ranking/profile projection
       -> Vertex structured synthesis
       -> Output Contract
       -> Harness Authoritative Result [A]
       -> DigestRun terminal + optional Digest READY [T]
       -> briefing Outbox completed [W fulfilled]
       -> UI Briefing READY | INCOMPLETE | FAILED | BLOCKED [P]

  B. USER_SUBSCRIPTION_CREATED --async/manual tick-->
       RelationEventPublisher claim + publication attempt [W ownership]
       -> dispatch unknown-effect fence [T/certainty]
       --effect--> Fake downstream event projection [P]
       -> SUCCEEDED | RETRYABLE | UNKNOWN/BLOCKED [P]
       (UserSubscription remains ACTIVE [T])

Digest READY [T]
  -> explicit HTTP/CLI Deliver action (current product contract)
  -> DeliveryService reserves DeliveryRecord + attempt [T/delivery state]
  -> dispatch unknown-effect fence
  --effect--> Fake/Termux delivery adapter
  -> accepted | failed/not_started | unknown [T/delivery certainty]
```

当前最后一段不是async Outbox chain。Digest durable与第一次`deliver_digest()`调用之间没有Delivery promise；
这正是后文的conditional gap。

## 3. Application Harness 与 Agent Harness

### Application Harness 当前拥有

- Conversation/turn/outcome、NEXT_QUESTION/REJECT/DONE protocol与deterministic Definition validation；
- Definition snapshot、Subscription/ProductSubscription、UserSubscription与activation binding；
- SQLite business transaction、application idempotency/uniqueness与HTTP response-loss replay；
- briefing reservation、两种typed Outbox、manual claim/drain/inspection/recovery；
- Briefing、relation publication、Delivery、Feedback/Profile等产品projection；
- provider attempt的safe metadata ledger与产品级failure stage mapping。

### Agent Harness 当前拥有

- 不确定model/tool loop的execution authority、deadlines/budgets与tool authorization；
- Search/observation的Authority边界、Evidence与workspace Artifact；
- Output Contract evaluation所依赖的Harness stores与verification chain；
- immutable Authoritative Result、run durability/recovery与safe Result projection。

### 渗漏审计

`mini_harness_core`没有Conversation、Subscription、UserSubscription、Briefing、Outbox或Delivery业务实体。
`SubscriptionActivationService`、两个worker与`DeliveryService`也不修改Harness policy/core。边界因此健康。

存在一个有意保留的紧耦合integration seam：`DefinitionConversationWorkflow`与
`DigestGenerationWorkflow`直接装配`run_agent`、Result/Evidence/Artifact/OutputContract stores和dispatch primitives。
这是app-to-Harness adapter，而不是职责反转，因为它只把authoritative execution facts投影回application records；
Model DONE、provider schema PASS或Harness terminal本身都不能写Subscription truth。若未来继续扩张，应该收窄
这个integration façade，而不是把product lifecycle放进core。

## 4. 三个 production lessons

| Lesson | Status | Implementation evidence | Deterministic evidence | Remaining limitation |
|---|---|---|---|---|
| Multi-turn protocol/UI alignment | **CLOSED in Demo** | durable conversation/turn/outcome；UI按WAITING/REJECTED/ACCEPTED继续、终止或commit | multi-turn HTTP、duplicate message、restart/recovery、strict union与Real Vertex acceptance | loopback固定user、无真实auth；没有自动browser-engine gate |
| Definition/briefing fire-and-forget gap | **CLOSED in Demo** | Definition + relation + reserved run + `FIRST_BRIEFING_REQUESTED`同事务；manual worker复用run | COMMIT fault matrix、concurrency、response loss、crash windows、Digest mark-only repair | promise不会丢，但没有daemon/scheduler自动推进 |
| Relation truth/downstream consistency | **CLOSED in Demo correctness** | relation + `USER_SUBSCRIPTION_CREATED`同事务；typed publisher attempt/certainty | accepted/failure/timeout、unknown no-blind-retry、briefing/event独立组合 | 仅Fake publisher；无broker consumer/reconciliation query或legacy backfill |

结论中的“closed”不等于production infrastructure complete；它表示最初要教学的commit/certainty invariants已经有
durable implementation和offline proof。

## 5. State model review

| Lifecycle | Representative states | Category | Authority |
|---|---|---|---|
| Conversation | COLLECTING / WAITING_FOR_ANSWER / REJECTED / DEFINITION_ACCEPTED / INCOMPLETE | input/product workflow truth | Application |
| Subscription + UserSubscription | ACTIVE / DISABLED | business truth | Application transaction |
| Briefing | PENDING / RUNNING / READY / INCOMPLETE / FAILED / BLOCKED | derived product outcome | Application from run + Digest + Outbox |
| Briefing Outbox | pending / claimed / retry_wait / completed / failed / blocked | work ownership/handoff | Application worker |
| Relation Event Outbox | pending / claimed / retry_wait / completed / failed / blocked plus attempt certainty | publication lifecycle | Application publisher |
| Delivery | pending / accepted / failed / unknown plus not_started/known_applied/unknown | delivery side-effect truth | DeliveryService |

六个lifecycle不是错误的state explosion：它们回答不同问题，合并会重新制造“订阅成功=Briefing READY”或
“event failed=relation failed”之类错误。真正的复杂度问题是operator必须跨这些正交facts手工拼接答案。
正确减法是提供只读aggregate operations view，而不是合并durable状态机。

## 6. Outbox abstraction review

当前适度复用的是原则和小型infrastructure pattern：SQLite transaction、status/version CAS、bounded manual
run/drain、safe inspection DTO、fault injection与fail-closed recovery。两个handler仍是typed且清楚：

- `FIRST_BRIEFING_REQUESTED`绑定`application_run_id`，retry/recovery authority来自existing generation workflow与
  Harness truth；它没有另造provider retry engine。
- `USER_SUBSCRIPTION_CREATED`绑定stable logical event与publication attempts；dispatch前写unknown-effect fence，
  只有明确not-applied才允许下一attempt。

这不是generic event framework：两者使用不同tables、claim queries、payload schema、service与recovery actions。
这种重复目前是健康的教学重复，因为它揭示work execution与external publication为何不能共享一个猜测型handler。
unknown原则一致但实现不被过度抽象：Briefing ambiguity由Harness events/Result判断，publication ambiguity由effect
certainty判断。

复杂度警戒线已经出现：再加入第三套table/status/CLI/recovery而没有新invariant，会开始重复已有pattern。
不要先造`GenericOutbox<Event>`；只有出现第三个已确认业务语义且能抽出稳定共同最小面时，才评估公共claim
storage。typed payload/handler/certainty仍不应被generic化。

## 7. Delivery Outbox：真实但有条件的 gap

准确答案：**Digest READY后、第一次调用DeliveryService前若process crash，当前没有durable promise保证未来一定
尝试Delivery。** `finish_digest_run`提交run/Digest；DeliveryRecord只在显式`deliver_digest()`时reserve。

是否构成blocker取决于产品合同：

- **A. Mandatory eventual delivery**：每个READY Digest必须最终产生user-level delivery attempt。此时必须在
  产生READY truth的同一durable boundary写Delivery intent（或由另一个不会漏的durable derivation创建）；当前
  存在真实fire-and-forget gap，Delivery Outbox是必要invariant。
- **B. Optional manual action（current Demo）**：用户点击Deliver才创建logical Delivery。此时Digest READY并不
  承诺投递；没有pre-existing intent不是数据丢失，Delivery Outbox不是current blocker。

所以不能因为已有两个Outbox就机械实现第三个。先写清产品选择A/B，以及delivery target/cardinality，才能定义
正确transaction owner、event identity与retry boundary。

## 8. Push/Delivery 与 shared content

当前schema仍近似`一个Subscription ≈ 一个user ≈ 一个Briefing/Digest`：

- legacy Subscription直接有`user_id`；
- `user_subscriptions.subscription_id`是UNIQUE，一个ProductSubscription只能有一个relation；
- BriefingReservation按subscription唯一，Digest也绑定subscription；
- Delivery虽保存`user_id`，logical uniqueness是`(digest_id, channel)`，无法表达同一Digest在同channel向多个
  UserSubscription分别投递。

因此当前没有表达“共享Buddy/Briefing content，按user relation独立Push”。如果正式产品确实要shared content，
这个cardinality gap比第三个Outbox更有教学价值：它要求明确共享Definition/Briefing truth与user-level
subscription/delivery projection的边界。也更危险，因为直接实现Delivery Outbox会把当前1:1假设固化进event
identity。建议先做设计slice/ADR，不在Phase 3.5直接迁移schema。

## 9. Quota、Review、Moderation 与 authorization

已有Harness等价能力包括：max steps/deadline/action budgets、Tool Policy与approval、protected paths、Evidence/
verification、Output Contract与Authoritative Result。Application层已有ownership checks、CSRF、enabled/disabled、
Definition字段bounds、provider attempt bounds和safe diagnostics。

尚未模拟的product controls：

- per-user/tenant active-subscription quota、generation/delivery spend/rate quota；
- Definition或Digest的review/moderation/release状态，以及“结构正确但不允许发布”的独立判断；
- admin/operator roles、真实authentication与publisher/delivery audience authorization；
- quota/review decision的durable provenance与appeal/manual approval flow。

其中“Output Contract validity ≠ content approved for publication”是新的Harness教学lesson，价值高于重复Outbox，
但会引入policy/product scope；应在明确威胁模型后单独设计。真实auth/multi-tenant boundary也是正式产品必要项，
但超出当前local teaching Demo的Phase 3.5目标。

## 10. Observability review

当前operator可以得到答案，但需要多个入口：

- Subscription成功：subscription/product/relation projection；
- Briefing卡点：first-briefing view + briefing Outbox inspect + run recovery inspect；
- relation event卡点：relation-events inspect + attempt certainty；
- Agent失败：run status、safe failure stage/subtype与Harness terminal/recovery facts；
- Delivery是否尝试：DeliveryRecord/attempt（当前普通CLI没有统一inspect journey）；
- 是否safe retry：分别由Briefing recovery、relation-event recovery与Delivery certainty派生。

因此“为什么这个用户没收到首篇资讯？”仍需跨application tables与多个safe DTO心算。无需暴露raw Harness对象，
但值得新增一个**只读 Product Operations View**，按user_subscription/subscription聚合：relation truth、Definition
ref、Briefing promise/run/Digest、relation publication、Delivery attempts、safe next action与blocking reason。它应完全
derived、无force flags、无新状态机，是当前最高价值的下一小步。

## 11. Complexity budget

值得保留：

- provider validity / Definition validity / product truth三层分离；
- relation、Briefing、publication、Delivery的正交certainty；
- transactional promises、stable identities、CAS与no-blind-retry；
- Harness Evidence/Contract/Result authority与application product boundary；
- deterministic crash/concurrency gates。

应该停止扩张：

- 未有mandatory product promise时继续复制Outbox CRUD/status/CLI；
- daemon、scheduler、distributed lease、broker/consumer infrastructure；
- generic event framework、统一所有unknown/retry的抽象；
- 为“更像production”引入Kafka/Redis/Celery或大量rename；
- 在没有shared-content cardinality决定前固化Delivery event schema。

863 tests与SQLite v13本身不是坏事，但说明后续每个backend mechanism都必须带来新的可解释invariant；否则会
降低Mini Harness的教学信噪比。

## 12. 下一阶段候选排序

评分：教学/产品/必要性越高越优；概念量/重复度/复杂度越高成本越大。

| Rank | Candidate | Harness教学价值 | 正式产品映射 | 新概念量 | 重复已有pattern | 工程复杂度 | 当前必要性 | Decision |
|---:|---|---|---|---|---|---|---|---|
| 1 | H. Phase 3.5 freeze + architecture docs | 高 | 高 | 低 | 无 | 低 | 很高 | **现在做；本review即freeze evidence** |
| 2 | C. Product Operations / Observability View | 高 | 高 | 低–中 | 低 | 中 | 高 | **下一最小code slice** |
| 3 | B. Shared Briefing → user-level Delivery projection | 很高 | 很高 | 高 | 低 | 高 | 中 | **先设计cardinality/identity，不立即迁移** |
| 4 | D. Quota / Review / Moderation controls | 高 | 高 | 中–高 | 低 | 中–高 | 中 | 先定义threat/product policy |
| 5 | I. Real auth / multi-user authorization boundary | 中–高 | 很高 | 高 | 低 | 高 | local Demo低、正式产品高 | Phase 4候选 |
| 6 | F. Automated Browser Engine E2E | 中 | 中 | 中 | 低 | 中 | 中 | 在UX稳定后做少量journey |
| 7 | E. Browser UX refinement | 中 | 中 | 低–中 | 低 | 中 | 低–中 | 不改变backend truth |
| 8 | A. Delivery Transactional Outbox | 中 | 取决于mandatory delivery | 低–中 | **高** | 中 | **current contract低** | 仅产品选择A后实施 |
| 9 | G. Scheduler/daemon | 低 | 中 | 中–高 | 高 | 高 | 低 | Phase 3.5不做 |

## 13. Freeze recommendation

建议现在freeze Phase 3.5，不继续实现Delivery Outbox。保留`b02b8d2`作为最后一个backend-mechanics checkpoint。
下一步若只允许一个小slice，选择C：只读Product Operations View；它用现有truth降低operator认知负担，不增加
第三套retry engine。之后先完成B的设计判断，明确shared Briefing、UserSubscription与per-user Delivery identity；
只有product contract明确为mandatory eventual delivery，才把A提升为blocker。

关联现状与failure/recovery细节见：
[`20-subscription-agent-harness.md`](20-subscription-agent-harness.md)、
[`09-failure-and-recovery.md`](09-failure-and-recovery.md)、
[`10-testing-and-e2e.md`](10-testing-and-e2e.md) 与
[`14-product-readiness-review.md`](14-product-readiness-review.md)。
