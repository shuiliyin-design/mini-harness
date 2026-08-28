# Incremental Phase 4 Slice Plan

## 1. 排序原则

每个 slice 必须先交付一个可观察的 User Value，再引入它严格需要的 capability。实现顺序不等于最终页面浏览顺序。
P4.1/P4.1.1 已纠正创建语义，P4.2 已建立 BRIEFING Updates/read model。下一步用一个 CONDITION vertical slice
证明顶层产品不是 Digest；不同时建设 CONDITION、EVENT 和新的 BRIEFING pipeline。

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

**User Value**：已确认的 flight condition 无需 CLI，可按 application policy 持续检查；未满足时保持安静，满足时只更新一次。

Acceptance Criteria：

1. fake clock 离线证明同一 Subscription/observation period 只有一个 logical request。
2. pause 后不再创建 request；resume 不补造未承诺的历史 periods。
3. scheduler 只写 durable work intent，external observation 在 transaction 外执行。
4. condition signal identity 跨 restart 去重；新价格或 definition version 才能形成新的合格 Update。
5. started-nonterminal unknown 继续 fail closed；只有真实 acceptance 被阻塞才提出 H1 core ADR。

## P4.5 — Distribution-aware Notification

**User Value**：Update 已经可读后，用户按明确偏好得到外部通知；发送失败不会丢内容或重复发送。

Acceptance Criteria：

1. Notification request 引用 Distribution；recipient/channel 从 active UserSubscription 与 policy 解析。
2. ready Update、available Distribution、Notification attempt 保持三个正交 durable truth。
3. known failure 可安全创建新 attempt；unknown 永不 blind resend。
4. 通知结果不修改 Subscription、Update 或 Distribution；产品内内容始终可读。
5. 先支持一 Update/一 UserSubscription；schema cardinality 不封死未来一 Update 多 Distribution，但不实现 shared execution。

## P4.6 — Verified EVENT Vertical Slice

**User Value**：用户关注“OpenAI 发布新模型”后，只在新的发布事件有可验证来源时得到 Update。

Acceptance Criteria：

1. Application 对受支持 event criterion 选择 `EVENT`，不把缺阈值的 intent 当 CONDITION，也不强制 cadence/长度提问。
2. Agent 可提出 event candidate；candidate 必须绑定 accepted Evidence，并通过 entity/event/time/source validator。
3. 新事件 identity 与历史 Update 去重；事实不足时是 `no_update`，不是让 Agent 猜测成立。
4. 复用 P4.3 的 Update/Distribution 与 P4.5 的 optional Notification，不建立 event 专属 core path。

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

下一条 implementation slice 是 P4.4。P4.3 已用一个明确阈值的 flight journey 落地 workflow selection、
deterministic condition、Update 与 UserSubscription Distribution 边界；P4.4 只在这条已验证路径上增加持续观察与
durable request cadence，不扩张到 EVENT、Notification 或万能 schema。
