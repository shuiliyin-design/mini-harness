# P4.4 Implementation Status — Continuous Flight CONDITION

## 1. 产品结论与边界

P4.4 已在 P4.3 唯一受支持的 Flight CONDITION 上实现 deterministic temporal vertical slice：

```text
durable CONDITION Subscription
  -> immediate INITIAL cycle
  -> deterministic due/tick + Fake Clock
  -> typed Fake price Observation + Evidence
  -> predicate truth + edge-trigger emission decision
  -> NO_UPDATE | Update + UserSubscription Distribution
  -> next anchored due / pause / completion projection
```

这是 application-owned tick seam，不是生产常驻 scheduler。runtime host 只在 startup、commit 与 resume 时唤醒 fake worker；
测试用 Fake Clock 显式推进时间并调用 tick。本 slice 不含真实航班 API、Notification、EVENT、BRIEFING cadence、通用 scheduler
framework 或 `mini_harness_core` 修改。

## 2. Policy、cycle 与 temporal cursor

Flight CONDITION 未明确频率时 materialize 为 `6h / PRODUCT_DEFAULT`；明确值只接受可回查 user turn 的
`1h/6h/12h/24h`，每天规范化为 `24h`。continuous execution policy v1 保存 cadence seconds、provenance、
`Asia/Shanghai` anchor、freshness/evaluator、resolved travel year 与 UTC window boundary。

schema v15 新增 immutable `condition_observation_cycles` 和 mutable `condition_temporal_states`。cycle 区分
`INITIAL/SCHEDULED/CATCH_UP/RESUME/MANUAL`，保存 scheduled slot、coalesced range/count、claim、terminal status、truth、
emission/failure 与结果 refs。temporal cursor 保存 lifecycle、next due、attempt/success/failure、last accepted fact、
`UNKNOWN/FALSE/TRUE` latch、armed 状态与 last emitted refs。旧 v14 rows 不 backfill、不伪造 temporal history。

closure gates 通过后，真实 `.digest-demo/digest.db` 已原地从 v14 迁移到 v15。迁移前历史 count/identity 哈希可重建且一致，
117 条非 ledger 历史行保持不变，两张 temporal 表均为 0 行；重复 migration、integrity 与 foreign-key checks 均通过。

## 3. Deterministic tick 与 lifetime

commit 原子写入 temporal state 和 immediate INITIAL cycle；后续 due 始终从 activation anchor 计算，不从 worker completion
滚动。tick 先写到期完成事实，再为每个 ACTIVE subscription 最多 reserve 一个 logical due cycle，随后在 transaction 外读取
Fake provider，并用 claim token + temporal version 原子 finalize。

停机恢复把所有 overdue anchored slots 合并成一个 `CATCH_UP`；8 小时恢复只读一次，跨 3 个 overdue slots 也只形成一个
cycle，并把 next due 推到严格晚于 now 的下一个 anchor。`now >= window_end_exclusive` 写
`COMPLETED/TIME_WINDOW_ENDED`，停止 claim、保留历史且禁止 resume。GET 不推进 due 或 expiry。

## 4. First-match、crossing 与 re-arm

predicate truth 与 emission decision 分开持久化。状态机严格遵守：首次 true=`EMIT_FIRST_MATCH`；false→true=
`EMIT_THRESHOLD_CROSSING`；true→true=`SUPPRESS_STILL_MATCHED`；true→false=`SUPPRESS_REARMED`；其他 false=
`SUPPRESS_FALSE`。所有 suppress 都是成功 `NO_UPDATE`，不是 incomplete/failed。

批准序列的离线结果是：

```text
920 -> NO_UPDATE / SUPPRESS_FALSE
760 -> UPDATE_CREATED / EMIT_THRESHOLD_CROSSING
750 -> NO_UPDATE / SUPPRESS_STILL_MATCHED
900 -> NO_UPDATE / SUPPRESS_REARMED
780 -> UPDATE_CREATED / EMIT_THRESHOLD_CROSSING
```

最终只有 ¥760 与 re-arm 后的 ¥780 各有一条 Update 和一条 Distribution。首次 Observation=¥760 会立即创建且只创建一次
Update/Distribution。

## 5. Dedupe、ordering、failure 与 recovery

cycle identity 绑定 subscription、policy version、scheduled due 与 kind；provider fact 继续使用 P4.3 typed signal identity。
相同 logical fact 在新 cycle 复用 Observation/Evaluation，并以 `DUPLICATE_OBSERVATION` 成功收口，不推进 latch、不重复 Update
或 Distribution。更旧 fact 和同时间冲突分别 fail closed 为 `OUT_OF_ORDER_OBSERVATION` / `OBSERVATION_CONFLICT`；stale、
route/date/currency 与 definition/evaluator binding 仍由 P4.3 validation 覆盖。

timeout/provider/invalid/Evidence failure 只让当前 cycle FAILED，Subscription 保持 ACTIVE，latch 不变，下个 cadence 继续。
STARTED claim 超过五分钟可由同一 cycle recovery；并发 worker 只有一个得到 claim。finalize 的 Observation/Evaluation/
Update/Distribution/cursor 在一个 SQLite transaction 中，注入 crash 会全回滚；retry 收敛为一套结果。

## 6. Pause/resume 与 HTTP/UI

pause 原子写 temporal `PAUSED`、清空 next due，并 supersede 未终结 cycles；历史与 latch 不变。resume 在有效 window 内至多创建
一个 immediate `RESUME` cycle，从当前 anchor 的下一未来 slot 继续，不补 pause 期间周期，也不重置 latch。

Subscription、Following、Updates 与 Feed Detail 安全展示 ACTIVE/PAUSED/COMPLETED、cadence、resolved year、last check、next due、
latest price 与最近失败；pause/resume endpoint 使用 expected version。页面不投影 Evidence/Observation/Evaluation/cycle/claim 等
内部 identity。P4.4 没有新增外部 Notification。

## 7. Verification record

P4.4 closure 的最终 gate 结果以本文件提交前最后一次实际运行记录为准：

```text
P4.4 deterministic/domain/repository/HTTP focused suites: PASS
python -m unittest -q: PASS (907 tests)
python mini_harness.py --self-check: PASS
git diff --check: PASS
JavaScript syntax: PASS
docs links + secret/runtime scan: PASS
git diff --exit-code -- mini_harness_core: PASS (empty)
real demo DB v14 -> v15: PASS (history preserved; temporal rows=0; integrity/FK clean)
```

P4.4 unresolved blocker：0。COMPLETE 只表示 Fake Flight CONDITION 的 continuous observation semantics、durable temporal truth
与 deterministic tick 已闭合；真实机票持续采集、生产 scheduler、自动通知与 EVENT workflow 仍未实现。
