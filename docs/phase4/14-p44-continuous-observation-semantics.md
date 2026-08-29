# P4.4 Design — Continuous Flight CONDITION Observation Semantics

## 1. 范围与事实基线

P4.3 已实现的事实链是：受支持的 Flight CONDITION commit 后创建一个 `manual_once` request；Fake provider 返回 typed
price Observation；Application 接受 Evidence，并用 `flight_price_lt_v1` 判断 `price < 800`；`NO_UPDATE` 是成功结果，
命中才创建 Update 与 UserSubscription Distribution。相同 logical provider fact 会复用同一 Observation、Evaluation、
Update 与 Distribution。

P4.4 只为这条深圳→武汉、9 月、往返、CNY 总价 `< 800` 路径增加跨时间的产品语义。本文是稳定语义基线；已落地的
runtime/schema 边界与验证证据见 [`15-p44-implementation-status.md`](15-p44-implementation-status.md)。实现不包含生产 daemon
或真实航班 API。

本 slice 不建设通用 cron、万能 scheduler、condition DSL、EVENT、Notification、真实 provider 或 shared execution，也不修改
`mini_harness_core`。

## 2. Cadence 与 lifetime

### 2.1 Observation cadence

- 用户未指定时，Flight CONDITION 的 Product Default 是**每 6 小时**检查一次，provenance 为
  `PRODUCT_DEFAULT`。这是 execution policy，不是 Tracking Definition truth。
- 用户可以明确覆盖为 allowlist 中的 `1h / 6h / 12h / 24h`；“每天”规范化为 `24h`。值必须能从 user turn
  确定性回查，provenance 为 `USER_EXPLICIT` 或经澄清后的 `USER_CONFIRMED`。
- 不接受任意 cron、亚小时频率或模型自报的 cadence。无法规范化的明确偏好要澄清或 fail closed，不能静默改成默认值。
- commit 后立即安排一次 `INITIAL` observation；第一次检查不等待一个完整 cadence。后续 slot 以 activation time 为 anchor，
  使用 `Asia/Shanghai` 计算，不能以某次 worker 完成时间滚动，避免长期漂移。
- 当前 repository 的 Definition compatibility default 是 `daily`，P4.3 Tracking Policy 是 `manual_once`；二者都不证明持续
  observation 已存在。P4.4 实现时必须显式迁移为上述 versioned execution policy，不能靠解释旧字段改变历史 snapshot。

### 2.2 Tracking lifetime

“9 月”必须在 Definition Confirmation 前确定性解析为具体年份与时区。本 journey 在 2026-08-28 创建时解析为
`2026-09-01T00:00:00+08:00` 至 `2026-10-01T00:00:00+08:00`（end exclusive）；若确认时已在当年 9 月，使用尚未结束的
当年窗口；若当年窗口已结束，使用下一年。confirmation 必须显示解析后的年份，用户可在 commit 前调整。

Observation 从 commit 后立即开始，不等到旅行月开始；当 `now >= travel_window_end_exclusive` 时，不再创建或 claim 新 cycle，
Subscription 以正常终态 `COMPLETED`、reason=`TIME_WINDOW_ENDED` 结束观察。它不是 failed、disabled 或 deleted：Definition、
Evidence、Update、Distribution 与历史仍可读，既有 Distribution 不被撤回。窗口结束后不能 resume；重新关注要走新的
confirmation/Subscription。

## 3. Match、crossing 与 re-arm

Evaluation 必须把两个问题分开保存：

1. predicate truth：当前 accepted Observation 对 versioned criterion 是 `TRUE` 还是 `FALSE`；
2. emission decision：本次是否有资格创建新 Update，以及原因。

P4.3 的 `MATCHED / NO_UPDATE` projection 可继续兼容读取，但 P4.4 durable truth 不能再把“条件为 false”和“条件仍为 true
但不重复提醒”混成一个值。

确定性状态机如下：

| 前一 accepted truth | 新 truth | emission decision | 新状态 |
|---|---|---|---|
| `UNKNOWN` | `TRUE` | `EMIT_FIRST_MATCH` | matched / disarmed |
| `UNKNOWN` | `FALSE` | `SUPPRESS_FALSE` | false / armed |
| `FALSE` | `TRUE` | `EMIT_THRESHOLD_CROSSING` | matched / disarmed |
| `TRUE` | `TRUE` | `SUPPRESS_STILL_MATCHED` | matched / disarmed |
| `TRUE` | `FALSE` | `SUPPRESS_REARMED` | false / armed |
| `FALSE` | `FALSE` | `SUPPRESS_FALSE` | false / armed |

因此：

- 第一次 observation 就是 ¥760：创建一次 Update 与一次 Distribution；不要求先看到 ¥920。
- ¥760 → ¥750 → ¥740：只有 ¥760 创建 Update；后两个 Evaluation 都成功且 truth=`TRUE`，但不刷屏。
- ¥920 → ¥760：¥760 是 threshold crossing，创建 Update。
- ¥760 → ¥900 → ¥780：¥900 是一个 accepted `FALSE`，立即 re-arm；¥780 再创建一次 Update。
- P4.4 不增加 hysteresis、百分比降价提醒或 cooldown。re-arm 只由**新的、有效且按时间前进的 false Observation**造成；
  timeout、invalid/stale fact、pause/resume、duplicate 或倒序 fact 都不能 re-arm。
- 新 Definition version 是新的 evaluator binding，状态从 `UNKNOWN` 开始；它必须经过重新 confirmation，不能继承旧 version
  的 match latch，也不能改写旧 Update。

## 4. Identity 与 dedupe

以下 identity 回答不同问题，禁止合并：

| 对象 | 含义与稳定 identity |
|---|---|
| Subscription | 一次已确认的长期用户目标；跨所有 cycles 稳定 |
| Observation cycle | 某 Subscription + policy version 的一个 scheduled slot，或一个 coalesced catch-up slot |
| Observation | provider 声明的一个 typed logical fact；沿用 P4.3 signal identity |
| Evidence | 对 accepted Observation 的 immutable proof；沿用 P4.3 Evidence identity |
| Evaluation | Definition/evaluator version 对某 Observation 的 predicate truth |
| Update | 某个合格 `FIRST_MATCH`/`THRESHOLD_CROSSING` emission 的事实内容 |
| Distribution | Update 与当时 active UserSubscription 的用户级绑定 |

Cycle identity 由 `subscription_id + execution_policy_version + scheduled_due_at + cycle_kind` 确定；并发 tick、restart 或 retry
只能收敛到同一 logical cycle。Provider read 在 durable cycle reservation 之后、数据库事务之外执行，finalize 用 cycle
identity/CAS 保证最多接受一个结果。

Observation dedupe 继续使用 P4.3 的 `subscription + source_signal_id + normalized typed quote` signal identity。相同 fact 即使在
另一个 cycle 再出现，也只链接已有 Observation/Evaluation：该 cycle 可成功完成为 `DUPLICATE_OBSERVATION`，更新
`last_successful_cycle_at`，但不改变 previous truth、re-arm 状态或 last-emitted refs，也不创建 Update/Distribution。

只有 `observed_at` 严格晚于该 binding 最后接受的新 Observation 才能推进 temporal state。相同时间却内容冲突要 fail closed；
更旧的、虽有不同 identity 的 fact 作为 out-of-order observation 记录失败，不能倒放状态机。Update identity 至少绑定
subscription、Definition/evaluator version、触发 Evaluation 与 transition kind；Distribution identity 继续绑定 Update 与
UserSubscription。unique constraints 与原子 finalize 共同保证 replay 不重复分发。

## 5. Failure、pause 与 resume

一次 provider timeout/error、invalid/stale/out-of-order observation 或 Evidence 持久化失败，只影响当前 cycle：

- cycle 记录 terminal `FAILED` 与稳定 failure code；Subscription 保持 `ACTIVE`；
- 不修改 previous truth、armed/disarmed、last accepted Observation/Evaluation 或 last-emitted Update；
- 保存 `last_attempted_at` 与 last failure，下一次仍走正常 cadence，不因为失败机械补造周期；
- crash 后可用同一 cycle identity/claim lease 恢复。Fake read 是只读操作；若 crash 发生在 fact durable 前，允许重新读取，
  但原子 finalize 最多接受一个结果，不宣称恰好采到了崩溃前的瞬时价格；
- P4.4 不定义 aggressive retry/backoff 或连续失败自动停订阅。UI 可显示“上次检查失败，将继续按计划检查”。

Pause 是 durable lifecycle transition，不是删除：

- pause 后不创建或 claim 新 cycle，清除可执行 `next_due_at`，历史 cycle/Evidence/Update/Distribution 与 latch 不变；
- 已 STARTED 的 read-only cycle 在 pause CAS 之后不得 finalize 为新 Update；结果丢弃或记录为 superseded，不能穿透 pause；
- resume 若窗口仍有效，创建至多一个 `RESUME` immediate cycle，并从当前 anchor 的下一个未来 slot 恢复；不补跑 pause 期间
  的 periods；
- resume 不重置 previous truth。pause 前已 matched，resume 后首个新 fact 仍为 true 时不重复提醒；先出现 false 才 re-arm；
- pause 期间窗口结束则进入 `COMPLETED/TIME_WINDOW_ENDED`，resume 不再重新激活。

P4.4 只需要让 Flight CONDITION 的 cadence 正确遵守 lifecycle。通用管理 UI、material Definition edit 与跨 workflow 的完整
pause/resume 仍留在 P4.8。

## 6. Downtime 与 missed-cycle policy

不追求补齐每个理论采样点。对 `next_due_at <= now` 的 ACTIVE Subscription，due planner 在一次 transaction 中只创建**一个**
coalesced `CATCH_UP` cycle，记录最早 missed due、采用的最新 due slot 与被合并的 slot 数；无论停机错过 1 个还是 100 个
period，都不会生成一串 provider calls。

catch-up 的 cycle identity 使用当前时刻之前最新的 anchored due slot，因此 restart/concurrent tick 稳定去重。reservation 后立刻
把 `next_due_at` 推进到严格晚于 `now` 的第一个 anchored slot；cycle 成功或失败都不从 completion time 重算。服务停 8 小时
后恢复时最多立即检查一次，然后回到正常 cadence。若已有未终结 cycle，先恢复/收口该 cycle，不再另建 catch-up。

## 7. Durable temporal state

P4.4 实现至少需要保存以下事实；字段名可随 schema review 调整，但语义不能靠进程内 memory 推导：

### Immutable execution/lifetime policy

- `execution_policy_version` 与绑定的 Definition/evaluator version；
- cadence value、cadence provenance、timezone、schedule anchor；
- observation source、freshness policy；
- resolved travel year、window start、window end exclusive。

### Mutable subscription temporal cursor

- lifecycle `ACTIVE | PAUSED | COMPLETED`、`paused_at`、`completed_at/reason`；
- `next_due_at` 与 cursor/version（用于 CAS）；
- last attempted cycle/time、last successful cycle/time、last failure code/time；
- last accepted new Observation/Evaluation 与 `observed_at`；
- previous predicate truth `UNKNOWN | FALSE | TRUE` 与 armed/disarmed；
- last emitted Evaluation/Update 与 emission time。

### Immutable cycle/evaluation history

- cycle identity/kind、scheduled due、coalesced range/count、policy/Definition binding、claim/status/failure；
- accepted/reused Observation 和 Evidence refs；
- predicate truth、emission decision/reason、evaluator version；
- Update/Distribution refs（若有）。

`last_successful_cycle` 与 `last accepted new Observation` 必须分开：provider 重复返回同一 fact 时，cycle 可以成功，但 temporal
truth 不能被当作新观察推进。

## 8. Agent、Application 与 Harness 边界

- LLM 只参与自然语言 intent/cadence/year clarification candidate；Application 回查 user-turn provenance，并 materialize
  versioned policy。
- due calculation、clock/timezone、lifetime completion、cycle reservation/claim、provider query binding、freshness/ordering、
  signal dedupe、`price < threshold`、crossing/re-arm、emission、`next_due_at` 与 pause CAS 全由 deterministic Application
  logic 管理。Model 不拥有时间、condition truth 或执行 Authority。
- 每个 unique provider fact 继续复用 P4.3 已有的 typed Observation、`EvidenceStore` immutable Evidence 与
  application-owned repository transaction。当前 Fake provider 是直接注入的只读 adapter，并未经过 Agent loop；P4.4 不为
  了重复 observation 虚构模型 run。未来真实 provider 的调用授权仍须服从 Tool Policy/Authority，但 scheduler/due planner
  属于 application/runtime host，不进入 core。
- 当前没有已证实的 `mini_harness_core` gap。对 Fake read-only source，同 cycle reclaim、stable identity、Evidence dedupe 与原子
  finalize 足够。只有未来真实外部 effect 暴露无法由 application 安全判断的 started-nonterminal 阻塞，并有 acceptance 证据时，
  才单独提出通用 core seam/ADR。

## 9. P4.4 implementation mapping

实现仍只覆盖 Fake Flight CONDITION：

1. schema migration 增加 versioned cadence/lifetime policy、durable temporal cursor 与 Observation cycle/emission decision；不改写
   P4.3 历史 snapshot；
2. 增加 injected fake clock 和 application-owned `plan/run due cycles` seam；不启动 daemon，不做通用 cron；
3. 把 initial request 纳入 cycle identity，provider read 保持 transaction 外，finalize 原子且可 restart/concurrency dedupe；
4. 实现上述 first-match、still-matched suppression、crossing、re-arm、duplicate/out-of-order 与 version binding；
5. 实现 Flight CONDITION 的 pause/resume、window completion 和 one-cycle catch-up；不扩为 P4.8 管理产品；
6. 离线序列测试至少覆盖 `760`、`760→750→740`、`920→760`、`760→900→780`、same fact replay、timeout 后下周期、
   pause/resume、8 小时 downtime、window end、restart/concurrency 与 migration rollback；
7. read model 只诚实显示 cadence、last check、next check、paused/completed 与最新 failure。外部 Notification、真实 flight API、
   EVENT、BRIEFING cadence 和生产后台 scheduler 均不在 P4.4。

这些 deterministic fake-clock gates 已纳入 P4.4 acceptance；即使通过，也只能说 temporal observation semantics 与 tick
已实现，不能宣称真实机票监控、常驻生产调度或自动通知已上线。
