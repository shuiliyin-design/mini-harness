# P4.3 Implementation Status — Flight CONDITION Vertical Slice

## 1. 产品结论

P4.3 已为唯一目标需求“关注深圳—武汉 9 月往返机票，低于 800 元提醒我”形成完整离线 vertical slice：

```text
natural-language onboarding
  -> Definition Confirmation
  -> ACTIVE CONDITION Subscription
  -> typed Fake price Observation + accepted Evidence
  -> deterministic price < 800 evaluation
  -> NO_UPDATE | Update
  -> one UserSubscription Distribution
  -> Updates / Feed Detail projection
```

Model 只负责把自然语言提出为 provenance-aware intent candidate。Application 校验受支持的 route、月份、往返语义、
单一 CNY 阈值和 trigger 后选择 `CONDITION`；Model 不能声明 workflow，也不参与数值判断或 product commit。

selector 没有默认 workflow：V1 legacy Definition 明确属于 BRIEFING-only schema；V2 只有不含 reactive
constraint/trigger 的 topic scope 才进入 `BRIEFING`；只有完整命中本 slice allowlist 的 flight criterion 才进入
`CONDITION`。其他 CONDITION、EVENT 与 UNKNOWN 都返回 `unsupported_tracking_intent`，不能 fallback BRIEFING。

## 2. CONDITION commit path

BRIEFING 与 CONDITION 共用 Conversation → clarification → proposal → explicit human confirmation → durable commit，
但在 commit 前由 deterministic selector 分流：

- 受支持的 flight candidate 原子写入 Subscription Definition、ACTIVE ProductSubscription、ACTIVE UserSubscription、
  relation event、Tracking Definition、Tracking Policy、首个 Observation request 与 activation binding；
- `workflow_kind=CONDITION`，不写 `BriefingReservation`，不写 application outbox，也不创建
  `FIRST_BRIEFING_REQUESTED`；
- event-like、缺 route/date/threshold、非 `lt`、trigger 与阈值不一致或 provenance 不是用户来源的 candidate 均
  `unsupported_tracking_intent`，不能 fallback BRIEFING；
- commit 中途 crash 全事务回滚；commit 成功但响应丢失时按 accepted Definition outcome 返回同一组 identities。

schema v14 只给 `subscription_aggregates` 增加兼容的 `workflow_kind`，并新增窄 CONDITION 表；既有 Digest/Briefing 表、
历史行和名称不迁移。

closure gates 通过后，真实 `.digest-demo/digest.db` 已原地从 v13 迁移到 v14。迁移保留 20 张历史表的 34 行、旧
identity 和 count，既有 ProductSubscription 默认投影为 `BRIEFING`；8 张 CONDITION 表保持 0 行，没有虚构历史
Observation/Update。重复 migration、`integrity_check` 与 foreign-key check 均通过。

## 3. Tracking Definition 与 policy

Tracking Definition 只保存影响 tracking truth 的最小快照：

```text
subject
route { origin=深圳, destination=武汉, trip_type=round_trip }
travel_month=9
signal.criterion {
  metric=round_trip_price, operator=lt, value=800, unit=CNY
}
provenance { subject, route, travel_month, signal.criterion }
```

threshold `800` 来自已确认 Definition，provenance 必须是 `USER_EXPLICIT` 或 `USER_CONFIRMED`。Fake source、manual-once
cadence、freshness、evaluator version、language、presentation limits 和 notification=`none` 保存在独立 policy snapshot；
产品默认值不被伪装成用户选择。

## 4. Observation、Evidence 与确定性判断

`FakeFlightPriceProvider` 是唯一 source，不访问网络。它只接受本 slice 的深圳→武汉、9 月、往返 query，并返回完整
`FlightPriceQuote`：stable source signal、route、trip type、month、metric、integer price、currency 与 timezone-aware
`observed_at`。

Application 在接受前验证：

- route/date/trip type 与 Tracking Definition 一致；
- metric 只能是 `round_trip_price`，currency 只能是 `CNY`；
- price 是正整数；时间不超过 policy freshness，也不能显著来自未来；
- read-only Observation 先保存为 immutable accepted Evidence，再进入 durable evaluation transaction。

比较器固定为 `flight_price_lt_v1`，domain entity 会重新推导结果并拒绝不一致的 claimed result。¥920 得到
`NO_UPDATE`；¥760 得到 `MATCHED`。Model/provider 都不能传入 condition result。

## 5. NO_UPDATE、Update 与 Distribution

`NO_UPDATE` 是成功 evaluation：Observation、Evidence、observed price、threshold 和 evaluator version 都持久化，
request 进入 `EVALUATED`，Subscription/UserSubscription 继续 ACTIVE；不创建 Update 或 Distribution，也不是 failure。

`MATCHED` 在同一 SQLite transaction 中创建：

1. immutable accepted Flight Observation；
2. versioned Condition Evaluation；
3. durable CONDITION Update；
4. 绑定当前 active UserSubscription 的一条 AVAILABLE Distribution；
5. request completion。

Update 通过 immutable `(definition_id, definition_version)` FK 绑定当时 Tracking Definition，并保存 normalized observed
value、threshold、currency、observed time 与 Evidence reference。Distribution 拥有 recipient relation；Update 不拥有用户或
送达状态。本轮没有 Notification，也没有 shared execution/fan-out。

## 6. Dedupe、crash 与历史正确性

logical signal identity 由 Subscription、provider signal identity 与完整 typed quote 确定。相同 Observation/retry 会复用
同一 Observation、Evaluation、Update 和 Distribution；新的 Observation request 只链接既有 Evaluation。

Observation、Evaluation、Update、Distribution 与 request completion 是一个事务。任一 durable stage crash 都不留下
半套产品 truth；transaction 前已安全保存的 immutable Evidence 在 retry 时按同一 identity 复用。Update 的 Definition
version 和 Evidence binding 不随当前设置或页面读取漂移。

## 7. HTTP/UI projection

Subscription、Updates Home 与 Feed Detail 增加 product-safe CONDITION projection。这里的“监测”只表示 Subscription
产品状态；P4.3 只在 commit 后执行一次 Fake Observation，不包含持续 cadence 或 scheduler：

- ACTIVE 关注明确显示“当前监测状态”；
- 已接受 Observation 显示“最近价格 ¥xxx”；
- ¥920 显示“未达到提醒条件”，Feed 保持 active，历史为空；
- ¥760 显示“已达到提醒条件”，Updates 出现一条 CONDITION Update，详情历史可读；
- GET/polling 只读取 durable truth，不重新观察或评估。

DTO/HTML 不返回或显示 Evidence ID、Observation ID、Evaluation/predicate、Harness Run、worker 或 outbox。首篇 Briefing
endpoint 对 CONDITION 返回 not found，后台也不会唤醒 BRIEFING worker。

## 8. Digest-specific debt 隔离

保留 `apps.digest_agent`、`DigestApplication`、legacy `Subscription` compatibility row、Digest tables 与 BRIEFING read
adapter。本 slice 只在新 product boundary 使用 Tracking Definition、Update 和 Distribution；没有为了名称统一做大迁移。

主要 debt 仍是：BRIEFING execution/storage 使用 `digest_runs`、`digests`、`briefing_reservations` 与
`FIRST_BRIEFING_REQUESTED`，legacy Delivery 直接绑定 `digest_id + user_id`。这些名称不能自然承载 CONDITION/EVENT，
但当前通过 deterministic commit 分支和 read adapter 隔离，不影响 flight correctness。

## 9. Harness gap 结论

P4.3 没有暴露必须修改 `mini_harness_core` 的新 gap。现有 immutable Evidence 与 application-owned transaction 足以完成
typed read-only Observation acceptance、确定性判断和 crash recovery。

真实用户需求暴露的是 Application Harness 缺口：此前没有 workflow-aware commit、typed observation work、successful
NO_UPDATE、signal-level dedupe，以及 Update/Distribution 分离。本 slice 已为单次 flight observation 补齐最小边界。
仍未补齐的是 P4.4 所需的持续 request cadence、pause/resume scheduling、worker claim/lease 与跨 period identity；这不是
把 generic scheduler 或 condition DSL 放进 core 的理由。

## 10. Verification record

P4.3 closure 后重新实际执行并记录（不复用旧 verification claim）：

```text
Flight CONDITION deterministic/application/repository tests: PASS（7 tests）
P4.3 + activation + HTTP focused suite: PASS（39 tests）
selector regressions: PASS（unsupported non-flight CONDITION + unsupported EVENT；0 Briefing/Search/Vertex/Update side effects）
temporary v13 -> v14 success/idempotency/partial-rollback gates: PASS（9 focused tests，含 P4.3 regression）
python -m unittest -q: PASS（894 tests）
python mini_harness.py --self-check: PASS
JavaScript syntax check（Node.js v22.22.1）: PASS
docs links（16 Markdown roots）+ whitespace scan: PASS
git diff --check: PASS
changed-scope configured-secret value scan + new runtime artifact scan: PASS
git diff --exit-code -- mini_harness_core: PASS（empty）
real demo DB v13 -> v14: PASS（history identities/counts preserved；CONDITION rows=0；integrity/FK clean）
real flight API / scheduler / EVENT / Notification / shared execution: NOT RUN（out of scope）
```

P4.3 unresolved blocker：0。这里的 COMPLETE 只覆盖受支持 flight CONDITION 的首次 one-shot evaluation vertical slice；
不代表持续监控、真实机票价格、自动通知或 EVENT workflow 已完成。
