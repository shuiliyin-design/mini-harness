# Phase 4 Design Decisions

## D1. 产品主对象叫 Feed/关注，结果叫 Update

用户 IA 使用“更新、关注、关注范围”。`Subscription` 是 durable product relationship；`Briefing` 是一种 Update
形态，不再是顶层产品对象。旧 `Digest` / `Briefing` application/domain 名称可在兼容层保留，但 Harness 内部名词
完全隐藏。

## D2. Definition Confirmation 默认必需

初次创建和 material definition edit 都会产生持续行为、成本与未来内容，因此 Agent `DONE` 后必须显式确认。
同一已确认 candidate 的幂等 replay 可直接返回既有 commit。Agent 不决定是否跳过 confirmation。

## D3. Product confirmation 不是 Harness Approval

confirmation 建立业务关系；Harness Approval 授权某次受 policy 约束的 side effect。二者的 identity、scope、
有效期和 Authority owner 不同，禁止复用 Approval state 或 UI。

## D4. 保留正交 lifecycles，新增只读聚合

Conversation、Subscription relation、observation/execution、Update、Distribution、Notification 与 publication 回答
不同问题。目标 UI 使用 aggregate read model 降低认知负担，不把 durable state machines 合并。

## D5. Updates-first IA

回访价值来自内容，不来自配置。Home 先展示 ready updates，再展示需要处理/preparing 的 Feed。管理与兴趣放到
Following/Me，不占首页主要空间。

## D6. Why recommended 先 deterministic

V1 用 definition/profile/ranking facts 生成模板解释；不增加模型调用。未来 Agent 只能润色已绑定 reason facts，
不能发明推荐因果。

## D7. 显式 feedback 才驱动 V1 学习

liked/dismissed/saved/opened 的规则可审计、幂等、可重放。停留、滚动、Delivery accepted 和 generated reason
都不自动成为兴趣证据。

## D8. 不承诺未实现的 cadence

字段 `cadence=daily` 不等于 scheduler 存在。在 P4.4 离线证明自动触发前，release 文案必须诚实表达当前能力。
调度属于 application/runtime host，不能让 LLM 决定何时运行。

## D9. Product edit 必须版本化

当前 compatibility Subscription PATCH 不能代表正式 definition edit。material change 必须走 clarification、
validation、confirmation 和新 definition version；历史 briefing 继续绑定旧 snapshot。

## D10. GET/polling 永远只读

读取 progress 不 claim work、不 tick worker、不触发 Search/LLM/Delivery。推进 work 只能来自明确 mutation 或
application runtime trigger。

## D11. Failure 以用户影响和安全动作表达

普通 UI 不显示 stage、provider subtype、recovery CLI 或 unknown 内部结构。它可以说 Feed 是否仍有效、内容是否
可读、是否能安全重试；如果不能确定，就明确不重复执行。

## D12. P4.1 不修改 core

当前 Harness 已支持 bounded execution、safe context、Authority、Evidence/Artifact/Result 与恢复所需基础事实。
P4.1 的缺口在 product flow/UI/application continuation，不构成 core change 理由。

## D13. Conversation schema 不等于 Definition schema

Conversation Agent 的输出只表达它从对话中理解到的用户 intent，并为每个值指出支持它的 user turn。durable
Definition 由 application 在 Agent `DONE` 后确定性 materialize：合并用户 intent、产品默认值与执行 policy，再做完整
business validation。Agent 不需要、也不能为了凑齐 durable schema 而向用户索取所有字段。

Agent 对 explicit execution preference 的 `source_turn` 只是 candidate evidence。application 会对 language、cadence、
max chars/items 与 delivery 等可确定性识别的偏好回查对应 user turn；没有文本证据就拒绝 candidate，不能仅凭 Model
自报把默认值标成用户选择。

字段优先级固定为：

```text
explicit user preference
  > confirmed clarification
  > product default
  > internal policy
```

## D14. Required internal fields 不等于 required user questions

`language`、`max_chars`、`max_items`、默认 delivery 等是生成或呈现所需配置，但缺失时由 product policy 提供。
`cadence` 在当前实现中是 compatibility execution policy；它不证明 scheduler 已实现，也不能伪装成用户选择。
confirmation 必须将 `USER_EXPLICIT` / `USER_CONFIRMED` 与 `PRODUCT_DEFAULT` / `POLICY_DEFAULT` 分组展示。
legacy Definition 缺少来源时只能显示为 `SYSTEM_INFERRED`，不能追认成 `USER_EXPLICIT`，也不改写旧 durable snapshot。

## D15. Clarification 由 ambiguity 驱动，而不是 schema completion

只有“不知道答案会显著改变追踪对象、响应时机或用户目标能否满足”的歧义，才能产生 `NEXT_QUESTION`。每轮只问一个
对当前 intent 最有价值的问题，使用用户语言，不提 schema、field、config 或内部限制；已足够明确时直接 `DONE`。
允许多轮，但轮数由剩余 ambiguity 决定。

v2 application contract 还会确定性拒绝字数、条数、语言设置、本机通知或 schema/config 等内部字段式问题；v1 durable
outcome 只保留读取兼容，不再是默认 Fake/Vertex onboarding 输出。

## D16. 先做最小 Tracking Definition，不建设万能 schema

现有 Subscription execution projection 仍偏资讯摘要。目标最小模型是：

```text
TrackingDefinitionSnapshot
  subject
  goal?
  constraints[]
  temporal_scope?
  location_scope[]
  signal
    kind: BRIEFING | CONDITION | EVENT
    criterion: bounded shape selected for that kind
  field_provenance

ExecutionPolicy     = observation source/cadence/quota/verification/dedupe policy
PresentationPolicy  = language/format/max_chars/max_items
DistributionPolicy  = user channel/quiet hours/notification preference
```

`signal.criterion` 不使用通用表达式 AST。P4.3 只增加一个窄 `ThresholdCondition(metric, operator, value, unit)`，且只
allowlist 往返机票总价；EVENT 在自己的 vertical slice 定义窄 event criterion。Definition v2 的 topic、constraints、
goal、trigger、time window、locations 与 provenance 先作为兼容输入，由 application adapter 投影到目标模型。

Tracking Definition 只回答用户要持续关注什么、何时算满足目标；执行频率、工具、生成长度和送达渠道不属于 tracking
truth。把 policy 的 resolved value 和来源保存在独立 snapshot/version 中，不能为了兼容旧 schema 继续让 onboarding
询问它们。

## D17. Feeds 是持续关注系统，不是 Digest 产品

正式产品抽象是：自然语言定义目标 → 理解和必要澄清 → durable Subscription → 持续 Observation → 满足条件时创建
Update → 用户级 Distribution / Notification。BRIEFING、CONDITION、EVENT 是同一 Tracking Subscription 的三种
execution semantics，必须共享 proposal、human confirmation 和 COMMIT 路径。

## D18. Workflow selection 与 commit 由 Application Harness 控制

Agent 可以提出 intent/signal candidate，但 application selector 必须根据 validated criterion shape、metric/event allowlist
和字段一致性确定 `BRIEFING/CONDITION/EVENT`。Model 提供的类型标签不是 Authority。只有 selector 与完整 business
validation 通过、用户确认的 candidate identity 匹配后，transaction 才能 COMMIT Subscription 和正确类型的 work
intent；当前“所有 commit 都创建 first Briefing”的行为是待迁移 debt。

## D19. CONDITION truth 必须 deterministic；EVENT truth 必须有 Evidence

CONDITION 的 typed Observation、单位归一、operator、threshold comparison、rule version 与 signal dedupe 全由 application
执行，不允许 LLM 输出“已满足”作为 truth。EVENT 可以用 Agent 从开放内容提出 candidate，但 candidate 必须绑定
accepted Evidence 并通过 entity/event/time/source validation；验证失败或事实不足产生 `no_update`，不是虚构 Update。

## D20. Update、Distribution、Notification 是三层 durable truth

```text
Update       = 满足 tracking goal 后产生的事实/内容；不包含 recipient 或送达状态
Distribution = Update 与 UserSubscription 的用户级绑定；承载 available/read/suppressed
Notification = 针对 Distribution 的渠道送达 attempt；承载 effect certainty
```

`UserSubscription` 是分发真源。目标 cardinality 允许一个 Update 将来 fan-out 为多条 Distribution；当前 slices 保持
一对一、不实现 shared execution。Notification 失败不改变 Update 或 Distribution，Distribution 失败也不改写 Update。
当前 `DeliveryRecord(digest_id, user_id)` 是兼容实现，不是目标关系。

## D21. 三类 workflow 复用同一个 Agent Harness

core 只提供通用的 bounded execution、Tool Policy/Authority、Observation/Evidence、Output Contract、Result 与 recovery。
workflow orchestration、selector、condition comparator、event taxonomy、Update/Distribution transaction 都属于 application。
禁止在 `mini_harness_core` 增加 briefing/flight/event 专属分支。只有已批准 vertical slice 被现有通用 seam 实际阻塞时，
才按 Harness change gate 提案。

## D22. Digest-specific 泛化采用 strangler/adapter，不做当前大重构

P4.3 先增加 product-facing Update/Distribution boundary，并把现有 READY Digest 适配为 `BRIEFING` Update。历史
`digest_runs`、`digests`、`briefing_reservations`、`FIRST_BRIEFING_REQUESTED`、`DigestApplication` 与 package 名先保留。
只有多 workflow durable facts、历史读取、restart/idempotency 都被测试覆盖后，才逐步迁移 storage/repository 名称。
命名清理不能独立冒充用户价值 slice。

当前 debt 按影响排序：

| Debt | 为什么是结构性问题 | 渐进处理 |
|---|---|---|
| commit 固定创建 `BriefingReservation` + `FIRST_BRIEFING_REQUESTED` | 任何 Subscription 都被当成 BRIEFING 执行 | P4.3 先由 selector 创建 typed work intent；旧 path 保留 |
| `DigestRunRecord` / `Digest` / `digest_runs` / `digests` 是唯一 canonical execution/result | CONDITION 的 evaluation 和 EVENT fact 无自然 durable 位置 | 新 workflow 使用通用 Observation/Evaluation/Update boundary；不迁历史表 |
| `DefinitionCandidate` 同时含 intent、cadence、limits、delivery | tracking truth 与 execution/presentation/distribution policy 混合 | 用 adapter 投影成独立 versioned snapshots，保持 v1/v2 读取兼容 |
| `DeliveryRecord` 直接绑定 `digest_id + user_id` | 没有 Distribution，recipient 不是从 UserSubscription 关系解析 | 新 Notification 引用 Distribution；旧 Delivery 只读兼容 |
| `DigestApplication`、`DigestRepository`、`DigestView`、`FeedBriefingView` | public/application vocabulary 会诱导所有结果都按摘要设计 | 先加 product-facing DTO/ports，再在调用方迁完后评估 rename |
| Search → rank → generate 的 payload/feedback 假设 | 只适合 item-based BRIEFING | 留在 BRIEFING adapter，不提升为通用 Update contract |

历史 Digest 在迁移期可由 application 依据其 Subscription 和 active UserSubscription 派生 compatibility Distribution
projection；新 Update 必须写真实 Distribution。该派生只用于已有单用户数据，不能作为未来 fan-out 实现。

## D23. Flight CONDITION 默认每 6 小时，首次 observation 立即执行

Cadence 是 versioned Execution Policy。用户未指定时使用 `6h / PRODUCT_DEFAULT`；明确偏好只接受
`1h / 6h / 12h / 24h` allowlist，并回查 user-turn provenance。commit 后立即安排 initial cycle，后续 slot 以
activation time 与 `Asia/Shanghai` 为 anchor；completion time 不改变 anchor。任意 cron、亚小时频率与 Model 自报 cadence
不获得 Authority。

当前 compatibility Definition 的 `daily` 和 P4.3 Tracking Policy 的 `manual_once` 都不能被解释为已实现 cadence；P4.4
必须用新 policy version 显式承载，不改写历史 snapshot。

## D24. Reminder 是 edge-triggered latch，不是每次 true 都发 Update

每个 Definition/evaluator binding 保存 `UNKNOWN/FALSE/TRUE` temporal truth。首次 accepted true 产生
`FIRST_MATCH` Update，false→true 产生 `THRESHOLD_CROSSING` Update，true→true 只产生 successful suppressed decision，
true→false 立即 re-arm。failure、pause/resume、duplicate、stale 或 out-of-order fact 不改变 latch。新 Definition version
从 `UNKNOWN` 开始，旧 Update 不漂移。

predicate truth 与 emission decision 是两个 durable facts；`NO_UPDATE` 不能同时含混表示 false、still matched、duplicate
和 failure。

## D25. Observation cycle 与 provider fact 是不同 identity

Cycle 表示 subscription/policy 的 scheduled slot；Observation 表示 provider 的 typed logical fact。cycle 先 durable reserve，
external read 在 transaction 外执行，再以 CAS 原子 finalize。相同 fact 可被多个 cycle 看见，但只保存一份
Observation/Evidence/Evaluation，且不能再次推进 latch 或产生 Distribution。只有按 `observed_at` 前进的新 accepted fact 才能
推进 temporal state；同时间冲突和倒序 fact fail closed。

## D26. Missed periods coalesce；failure 与 pause 不改写历史 truth

服务恢复时，不逐个补跑 overdue slots；所有 missed periods 只形成一个使用最新 due slot identity 的 catch-up cycle，随后
`next_due_at` 前进到第一个未来 anchored slot。一次 cycle failure 不停用 Subscription、不 re-arm，下一正常 cadence 继续。
pause 停止新 cycle并保留历史/latch；resume 至多一次 immediate cycle，不回填 pause 期间。

“9 月”在 confirmation 前解析为具体 year 和 `Asia/Shanghai` end-exclusive window。窗口结束后 Subscription 正常进入
`COMPLETED/TIME_WINDOW_ENDED`，历史保留且不能 resume。

## D27. Temporal scheduling 与 condition transition 属于 Application

clock、due calculation、cycle claim、lifetime、freshness/ordering、predicate、crossing/re-arm、dedupe、emission 与 next due 都是
deterministic Application Authority。LLM 不拥有时间或 condition truth；scheduler/due planner 不进入 `mini_harness_core`。
现有 typed Observation、immutable Evidence 与 transaction/recovery seam 足以设计 Fake Flight slice，目前没有已证实 core gap。
完整语义见
[`14-p44-continuous-observation-semantics.md`](14-p44-continuous-observation-semantics.md)。

## D28. Notification eligibility 属于 Application，capability execution 属于 Authority

只有 AVAILABLE Distribution、active UserSubscription/Product/temporal lifecycle 与 version-bound distribution policy 全部成立，
Application 才能创建 notification intent。Flight 默认 `feed_only / PRODUCT_DEFAULT`；只有明确确认的
`termux_notification` 才 eligible。Model、adapter 或 capability availability 都不能反向决定“应该通知”。Environment registry、
Policy/Approval/AuthorizedAction 与 Termux adapter 只决定已经选择的 notification 允许如何执行。

## D29. Logical Notification 绑定 Distribution；attempt identity 独立

P4.5 不建第二套 framework。schema v16 把原 `DeliveryRecord` target 扩为 Digest 或 Distribution 二选一：legacy Digest delivery
保持原 identity，新 Notification identity 固定绑定 `distribution_id + channel`；第 N 次 publication attempt 继续绑定
`logical_delivery_id + attempt_number`。pending intent 可在 restart 后安全 dispatch；accepted/failed/unknown terminal replay 都不
再次调用 adapter。

## D30. Request accepted 不等于 user seen；unknown 永不 blind retry

`accepted/known_applied` 只支持“notification request accepted”并保存 safe Evidence，不产生 user_seen/user_read。明确
`failed/not_started` 最多允许一次显式 retry；timeout、adapter exception、terminal/Evidence persistence ambiguity 都记为
`unknown`，禁止自动或手动 blind resend。任何 Delivery outcome 都不得修改或删除 Update/Distribution，Feed 内容始终独立可读。

## D31. EVENT V1 只支持一个 exact criterion，默认从 activation 向未来观察

P4.6 selector 只接受 `OpenAI + MODEL_RELEASED + MODEL/PUBLIC_AVAILABILITY + FUTURE_FROM_ACTIVATION`。传闻、coming soon、
benchmark、价格/SDK变化、退役、其他entity/event type、复合intent与任意temporal DSL都不受支持并fail closed；绝不能fallback
BRIEFING。激活前历史发布不在initial cycle补发，默认无end date。EVENT cadence默认`6h / PRODUCT_DEFAULT`，允许
`1h/6h/12h/24h`明确覆盖；notification默认feed-only，只有明确确认本机通知才使用Termux。这些execution/distribution值不属于
Tracking Definition truth。

## D32. Event Candidate 是 Agent proposal；Verified Event 是 Application truth

Agent可以从accepted Source Observation识别entity/type/model/time candidate，提出exact source refs/spans和名称归一化候选；它不
拥有Verified Event truth、logical identity、Update commit或Notification Authority。Application按versioned
`openai_model_release_v1` gate确定性验证Observation binding、official provenance、entity、release assertion、source-owned time、
eligible window、support sufficiency与conflict，之后才计算event key并原子创建Verified Event/Update/Distribution。structured
output valid、candidate confidence或多来源投票都不能跳过gate。

## D33. EVENT 的NO_UPDATE、verification incomplete与failure必须分离

source read、detector和verifier完整完成后的`NO_EVENT_FOUND`、`DUPLICATE_VERIFIED_EVENT`、明确`OUTSIDE_SCOPE`才是successful
`NO_UPDATE`。plausible candidate缺official support、时间/名称无法确认、Evidence冲突或coverage truncated是
`VERIFICATION_INCOMPLETE`；provider/Agent contract/Evidence persistence错误是failed，effect/current truth不清是unknown。后三者
均不创建Update/Distribution/Notification，也不能被UI简化成“没有新事件”。incomplete/failed不推进EVENT success watermark，
Subscription保持ACTIVE并在正常cadence继续。

## D34. Logical EVENT identity独立于Observation/source；V1不支持correction

V1 event key绑定`entity_key + event_type + object_type + canonical_model_key`，不绑定source URL、报道数量、cycle、Agent run或
candidate id。同一“OpenAI发布GPT-5”的多来源、restart/replay只能有一个Verified Event、一个Update和每个relation一条
Distribution。后续source可以形成新verification audit fact，但不改写历史Update或再次通知。

V1不实现correction/retraction。首次commit前有冲突则verification incomplete；commit后才出现否定Evidence时保留历史事实、记录
needs-attention，不删除/改写/重复通知。未来correction必须是显式、关联原event的product event和独立slice，不能用mutable verified
boolean拼凑。

## D35. EVENT复用temporal/delivery语义，但不继承CONDITION latch

P4.6复用P4.4的versioned cadence、immediate initial、Fake Clock、due/tick、claim/CAS、pause/resume、missed-slot coalescing、failure
isolation与restart/concurrency，复用P4.5的UpdateDistribution、Delivery identity/attempt/certainty。EVENT没有boolean predicate、
crossing/re-arm或Flight expiry；它用successful `verified_through` watermark、bounded overlap与logical event set决定新旧。pause期间
event不回填，incomplete/failed不推进watermark。当前CONDITION-specific table/type不为泛化先大改；实现可增加窄EVENT records，
不建设generic scheduler或第二套delivery。完整设计见
[`17-p46-verified-event-semantics.md`](17-p46-verified-event-semantics.md)。

## Deferred decisions

- quiet hours、mandatory eventual SLA 与非 Termux notification channel；
- shared execution 的 identity、成本归属与 Update fan-out transaction；
- free-text feedback 是否有足够用户价值；
- real auth/multi-user threat model；
- started-nonterminal reconciliation 的通用 core contract；
- hard delete/retention；
- EVENT correction/retraction、同名模型再次发布identity与其他entity/event type；
- browser framework 与 automated browser-engine gate。

## Non-goals for the design checkpoint

最初设计 checkpoint 不实现 UI/Runtime。P4.1—P4.6 只有各自 status 文档明确列出的部分已经落地；P4.6 的 Fake EVENT runtime
证据见 [`18-p46-implementation-status.md`](18-p46-implementation-status.md)。不能从该窄 slice 推断真实 provider、生产 daemon、
generic EVENT、correction/retraction 或其他 P4.7+ 能力已经实现。
