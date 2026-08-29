# Incremental Phase 4 Slice Plan

## 1. 排序原则

每个 slice 必须先交付一个可观察的 User Value，再引入它严格需要的 capability。实现顺序不等于最终页面浏览顺序。
P4.1/P4.1.1 已纠正创建语义，P4.2 已建立 BRIEFING Updates/read model，P4.3—P4.5 已交付窄 Flight CONDITION 的
continuous observation与Distribution-aware Notification。P4.6窄OpenAI `MODEL_RELEASED` EVENT已实现；后续 slice 不从该
Fake vertical slice 推断generic EVENT、真实source、其他CONDITION或新的BRIEFING pipeline已经存在。

## P4.1 — Confirmed Feed Creation

**User Value**：用户可以通过任意多轮对话定义关注范围，并在建立长期订阅前看懂、调整和明确确认。

```text
User Need
  -> Product Feature: Create Feed + Conversation + Definition Confirmation + Success/Preparing
  -> Agent Capability: NEXT_QUESTION / REJECT / DONE definition candidate
  -> Harness Dependency: bounded turn execution + durable authoritative Result
  -> Current Support / Gap: execution/protocol/commit 已支持；UI 自动 commit，缺 confirmation 与自然 revision
  -> Acceptance Criteria
```

Acceptance Criteria：

1. UI 不包含轮数假设；连续三个以上 `NEXT_QUESTION` 仍由同一 server conversation 恢复。
2. `DEFINITION_ACCEPTED` 只显示 confirmation；未点击确认时 Subscription/Relation/Briefing rows 都不存在。
3. confirmation 显示 topic/focus/language/cadence/limits/notification，值来自 server DTO，不由 client 重建。
4. confirm double-click/response loss 只得到同一 subscription；成功与 first update `preparing` 同屏但语义独立。
5. “调整”不修改 client candidate；若最小实现尚无 continuation，则明确重新进入 server-owned clarification，
   并在 slice 前决定 supersession identity。
6. REJECT/INCOMPLETE/processing/restart 均有产品文案和安全动作。
7. 删除 UI 的自动 commit 分支；不改 `mini_harness_core`。
8. 新增离线 application/HTTP/render tests，并保留 Web thin-client architecture gate。

范围控制：P4.1 不做 Home aggregate、scheduler、automatic Delivery、auth、browser engine 或 generic frontend framework。

### P4.1.1 — Intent-driven Onboarding Correction

**User Value**：用户描述任意可支持的关注目标时，产品准确保留该意图，只追问真正影响目标的歧义，不把内部摘要配置
包装成用户选择。

```text
User Need: 自然表达“要关注什么、何时算有价值”
  -> Product Feature: intent-driven clarification + provenance-aware confirmation
  -> Agent Capability: extract intent, identify material ambiguity, cite supporting user turn
  -> Harness Dependency: existing bounded turns + strict structured candidate
  -> Current Support / Gap: Harness 已支持；Conversation contract、defaults materialization、UI 分组是 application gap
  -> Acceptance Criteria
```

Acceptance Criteria：

1. AI、模糊机票、明确阈值机票、事件触发四类 fake journey 确定性覆盖；非 AI intent 不出现 AI fallback。
2. 模糊机票围绕日期、阈值或提醒条件等 material ambiguity 追问；明确阈值机票和明确事件触发尽量直接 `DONE`。
3. 不因 `max_chars`、`max_items`、language、cadence、delivery 缺失而提问。
4. durable Definition 区分 `USER_EXPLICIT`、`USER_CONFIRMED`、`PRODUCT_DEFAULT`、`POLICY_DEFAULT`，并拒绝不存在的
   future turn provenance。
5. confirmation 明确分为“你告诉我的”与“系统默认设置”；默认值不能看起来像用户选择。
6. 保留 P4.1 proposal/explicit commit/refresh/idempotency/first-briefing 边界，不修改历史 snapshot 或 Harness core。

范围控制：只纠正 onboarding representation。价格 tracker、scheduler、自动提醒、P4.2/P4.3 扩展均不属于此 slice。

## P4.2 — Updates Home + Feed Detail

**User Value**：回访用户首先看到可读更新，能从 Feed 状态进入内容与历史，而不是操作内部运行。

```text
User Need
  -> Product Feature: Updates Home, Feed cards, Feed Detail, first-update progress
  -> Agent Capability: existing briefing synthesis only
  -> Harness Dependency: existing Search/Evidence/Output Contract/Result
  -> Current Support / Gap: Digest/briefing facts 已有；缺 sealed aggregate read model 和产品排序
  -> Acceptance Criteria
```

Acceptance Criteria：

1. application façade 提供不含 Harness internals 的 Home/Feed DTO；Web 不读 repositories。
2. ready content 优先于 preparing cards；排序 deterministic 且有离线 tests。
3. ACTIVE + FAILED/INCOMPLETE/BLOCKED 合法呈现，任何 briefing 结果都不反写 Feed success。
4. GET/polling 是 pure read，不调用 Search/LLM/Delivery/worker。
5. Feed Detail 展示 source links、history 和当前 definition；旧 Digest 绑定旧 snapshots。
6. 用户 UI 不出现 Run、Digest、Outbox、Provider、stage 或 CLI。

## P4.3 — Flight CONDITION Vertical Slice（已实现）

**User Value**：用户确认“深圳到武汉 9 月往返机票，低于 800 元提醒我”后，系统能检查一次结构化价格 Observation，
只在条件成立时生成一条可解释的 Update，并出现在该用户的 Updates 中。

```text
User Need: 价格达到明确阈值时告诉我
  -> Product Feature: confirmed CONDITION subscription + one observation/evaluation + Update/Distribution
  -> Agent Capability: existing intent understanding only
  -> Harness Dependency: existing tool policy / Observation / Evidence / Result boundaries
  -> Current Support: deterministic selector、typed price observation、comparator、Update/Distribution 已实现
  -> Acceptance Criteria
```

Acceptance Criteria：

1. Application 只对明确 BRIEFING shape 选择 `BRIEFING`，只对受支持 threshold criterion 选择 `CONDITION`；
   UNKNOWN、其他 CONDITION 与 EVENT 都 fail closed，Model 自报类型不能越过 allowlist/shape validator。
2. 最小 Tracking Definition 保存 route/date/round-trip/price criterion 和 provenance；cadence、source、quota、
   language、max chars/items、delivery 分别留在 policy，不反塞 definition。
3. Fake price source 产出 typed Observation；application 验证 metric=`round_trip_price`、currency=`CNY`、单位和时间后，
   用 versioned deterministic comparator 判断 `< 800`。LLM 不参与数值结论。
4. 条件不成立持久化 successful `no_update` evaluation；条件成立才原子创建 Update 与当前 active
   UserSubscription 的一条 Distribution。稳定 signal identity 防止 replay 重复提醒。
5. Update 绑定 definition snapshot、normalized observed value、threshold、observed_at 与 Evidence refs；UI 能说明
   “观察到多少、为何满足条件”，但不显示 Harness internals。
6. P4.2 的 READY Digest 通过 read adapter 继续表现为 `BRIEFING` Update；不迁移/重命名历史表。
7. 覆盖 deterministic domain/application/repository tests、HTTP/UI 两种阈值分支、restart/idempotency 与 full gates。
8. 明确不含 scheduler、真实机票 provider、外部 Notification、EVENT、shared execution 或 core 修改。

这个 slice 只支持一个窄 condition vocabulary：往返机票总价的单阈值比较。它验证产品边界，不引入通用表达式 AST、
规则引擎或万能 tracker schema。

## P4.4 — Continuous CONDITION Observation

**User Value**：已确认的 flight condition 按明确时间策略持续检查；首次满足或在一次 false 后再次越过阈值时产生 Update，
持续满足、重复 fact、失败和停机恢复都不会刷屏。

设计基线见
[`14-p44-continuous-observation-semantics.md`](14-p44-continuous-observation-semantics.md)。本 slice 已按该基线实现；代码、schema、
测试与产品投影证据见 [`15-p44-implementation-status.md`](15-p44-implementation-status.md)。

Acceptance Criteria：

1. versioned execution policy 保存 cadence、provenance、timezone、anchor 与带年份的 travel-window end；未指定 Flight
   CONDITION 默认 `6h`，明确偏好只接受 `1h/6h/12h/24h` allowlist。
2. commit 后立即安排 initial cycle；fake clock 离线证明同一 Subscription/policy/scheduled slot 只有一个 logical cycle。
3. predicate truth 与 emission decision 分离：first true 和 false→true 各产生一次 Update；true→true 成功但 suppressed；
   true→false re-arm；失败/duplicate/out-of-order 不改变 latch。
4. same logical provider fact 跨 cycle/restart 复用 Observation/Evaluation，不重复 Update/Distribution；新 fact 也只有发生合格
   transition 才能创建 Update。Definition version 重新从 `UNKNOWN` 评估。
5. pause 后不再创建/claim cycle且历史/latch不变；resume 至多一次 immediate cycle，不补造历史 periods。time window 结束进入
   `COMPLETED/TIME_WINDOW_ENDED`，不再 resume。
6. downtime 只把所有 overdue slots coalesce 为一个 catch-up cycle；`next_due_at` 推进到严格晚于 now 的 anchored slot，
   不按 completion time 漂移。
7. provider timeout/error 只使当前 cycle FAILED，Subscription 保持 ACTIVE、下个正常 cadence 继续；started cycle 用稳定 identity
   与 claim recovery 收口。
8. due planner 只写 durable cycle intent，external read 在 transaction 外执行，finalize 原子；clock、comparison、ordering、
   crossing/re-arm、dedupe、lifetime 与 next due 全属于 deterministic Application logic。
9. schema/migration、restart/concurrency、序列语义、pause/resume、8 小时 downtime 与 window completion 均有离线 tests；不启动
   daemon、不调用真实 provider、不增加 EVENT/Notification/BRIEFING cadence。
10. 只有真实 acceptance 被现有通用 Evidence/recovery seam 阻塞才提出 core ADR；当前设计没有已证实 core gap。

## P4.5 — Distribution-aware Notification

**User Value**：Update 已经可读后，用户按明确偏好得到外部通知；发送失败不会丢内容或重复发送。

Acceptance Criteria：

1. Notification request 引用 Distribution；recipient/channel 从 active UserSubscription 与 policy 解析。
2. ready Update、available Distribution、Notification attempt 保持三个正交 durable truth。
3. known failure 可安全创建新 attempt；unknown 永不 blind resend。
4. 通知结果不修改 Subscription、Update 或 Distribution；产品内内容始终可读。
5. 先支持一 Update/一 UserSubscription；schema cardinality 不封死未来一 Update 多 Distribution，但不实现 shared execution。

本 slice 已实现，证据与边界见
[`16-p45-distribution-notification-status.md`](16-p45-distribution-notification-status.md)。

## P4.6 — Verified EVENT Vertical Slice

**User Value**：用户关注“OpenAI 发布新模型”后，只在新的发布事件有可验证来源时得到 Update。

稳定 Product/Application/Harness 设计见
[`17-p46-verified-event-semantics.md`](17-p46-verified-event-semantics.md)。exact selector、Fake runtime与验收证据已实现，见
[`18-p46-implementation-status.md`](18-p46-implementation-status.md)；其他 EVENT 继续 fail closed。

Acceptance Criteria：

1. Application 只对 exact `OpenAI + MODEL_RELEASED + MODEL/PUBLIC_AVAILABILITY + FUTURE_FROM_ACTIVATION` criterion 选择
   `EVENT`；其他 EVENT/UNKNOWN fail closed，不 fallback BRIEFING，也不信任 Model 自报 workflow。
2. commit 原子建立 EVENT Definition/policy、active Subscription/relation、initial cycle与 temporal cursor；不创建 Briefing
   reservation、`FIRST_BRIEFING_REQUESTED`或 CONDITION work。
3. typed Fake Source Observation 包含 query/window/coverage、normalized results与 content fingerprint，但不含 verified truth。
   bounded Fake Agent只提出 entity/type/model/time candidate及 Observation 内 exact source refs/spans。
4. versioned Application gate验证 accepted Observation binding、official OpenAI provenance、entity、release assertion、
   source-owned published time、eligible temporal window、sufficient support与conflict；Agent confidence不能越权。
5. verification outcome明确区分 successful `NO_UPDATE`（no event、duplicate、outside scope）、
   `VERIFICATION_INCOMPLETE`（plausible但证据/coverage/conflict未闭合）与execution failed/unknown；后三类都不能伪造Update。
6. logical event identity由validated entity/event/object/canonical model计算；同一发布的多来源、duplicate tick、restart/replay
   只能创建一个Verified Event、一个EVENT Update和每个UserSubscription一条Distribution。
7. 复用P4.4的immediate initial、6h default、Fake Clock、due/tick、pause/resume、missed-cycle coalescing、failure isolation、
   restart/concurrency；EVENT不使用threshold crossing/re-arm latch，默认open-ended，pause期间不回填。
8. 复用P4.5 Distribution-bound Delivery与certainty；feed-only/NO_UPDATE/duplicate/incomplete/paused通知为0，explicit
   termux policy下首次新Distribution才eligible，notification failure不影响Feed truth。
9. correction/retraction、其他entity/event type、real Brave/Vertex、production daemon、generic ontology/RAG/vector DB与core修改
   均不在本slice；HTTP/UI只显示safe产品状态。
10. 离线tests覆盖first event、empty、multi-source duplicate、insufficient/conflict、wrong entity/type、freshness/window、
    replay/restart/crash/concurrency、pause/resume、downtime、notification与schema rollback。

## P4.7 — BRIEFING Feedback + Interest Evolution

**User Value**：用户理解 BRIEFING 推荐依据，明确反馈影响后续内容，并能看到可解释的兴趣变化。

Acceptance Criteria：

1. why/feedback 只使用 definition、profile、ranking、freshness 与 seen facts；不增加默认模型调用。
2. stable event replay 不二次修改 Profile；旧 Update 使用 execution-time snapshot。
3. history 从 durable Interaction/ProfileUpdate 重建，并以用户语言解释变化。
4. CONDITION/EVENT 不直接继承 item-like/disliked 语义；需要时另做用户研究和小 slice。

## P4.8 — Subscription Management + Definition History

**User Value**：用户可以暂停、恢复和调整任一 tracking intent，并信任旧 Update 不被新设置改写。

Acceptance Criteria：

1. pause/resume 使用 expected version，重复请求收敛，历史仍可读。
2. material edit 生成新 candidate/version，重新 clarification/confirmation；不 PATCH 历史 snapshot。
3. 后续 execution 绑定新 definition/policy version；旧 Update 与 Distribution 不漂移。
4. V1 不 hard delete；retention 另做设计。

## 渐进泛化规则（不单列大重构 slice）

- 新 product-facing DTO 使用 Update/Distribution/Notification；旧 `DigestView`/`FeedBriefingView` 由 adapter 兼容。
- `digest_runs`、`digests`、`briefing_reservations` 和 `FIRST_BRIEFING_REQUESTED` 在 BRIEFING path 暂时保留。
- 只有新旧双路径、restart/idempotency 和历史 projection 都有测试后，才做 storage rename/migration。
- 不把 `apps/digest_agent` 包名或 `DigestApplication` 改名作为用户价值 slice；等至少两个 workflow 共存且边界稳定再评估。
- shared execution/fan-out 只保留 cardinality seam，不在上述 slices 实现。

## 2. 推荐顺序

```text
P4.1 Confirmed creation
  -> P4.1.1 Intent-driven correction
  -> P4.2 BRIEFING Updates / Feed Detail
  -> P4.3 Flight CONDITION vertical slice
  -> P4.4 Continuous CONDITION observation
  -> P4.5 Distribution-aware Notification
  -> P4.6 Verified EVENT vertical slice
  -> P4.7 BRIEFING feedback / interest
  -> P4.8 Management / versioned edit
```

P4.6 Verified EVENT implementation slice 已完成并复用 P4.5 的 Distribution/Delivery certainty。真实网络、email/SMS、
cloud push、Delivery scheduler、shared execution、correction/retraction 与 read receipt 仍未实现。
