# P4.5 Implementation Status — Distribution-aware Notification

## 1. Product chain 与 eligibility

P4.5 在 P4.4 的 Fake Flight CONDITION 后补齐：

```text
Update + AVAILABLE UserSubscription Distribution
  -> deterministic Application eligibility
  -> durable logical Delivery + attempt
  -> FakeDelivery | authorized termux:notification
  -> accepted | failed/not_started | unknown
  -> safe Feed projection
```

Update/Distribution 是不可被 delivery outcome 改写的产品事实。Application 只允许 active ProductSubscription、active
UserSubscription、ACTIVE temporal lifecycle、AVAILABLE Distribution 与其 version-bound policy 全部匹配，且
`notification=termux_notification` 时创建 Notification。默认 Flight policy 是 `feed_only / PRODUCT_DEFAULT`；本 slice 的通知
acceptance fixture 使用用户明确确认的“本机通知”。NO_UPDATE、still-true、duplicate fact、paused/completed 与 feed-only 均不调用
adapter。

## 2. Identity、schema 与复用边界

schema v16 没有新增第二套 notification framework，而是重建并兼容迁移现有 `delivery_records/delivery_attempts`：target 必须是
legacy Digest 或新 Distribution 二选一。logical Notification identity 绑定 `distribution_id + termux_notification`；attempt
identity 继续绑定 `logical_delivery_id + attempt_number`。一条 Distribution 即使被 tick、HTTP、restart 或 worker replay 多次，
也只能收敛为一个 logical record。

v15→v16 migration 保留所有 Digest delivery 与 attempt identity/status；DDL/copy/swap 任一 fault 全事务回滚，重复 migration
幂等。新 Distribution notification 不伪造 Digest。

closure gates 后真实 `.digest-demo/digest.db` 已原地 v15→v16；33 张非 Delivery 历史表的 117 行 hash/count 不变，旧 Delivery
rows（当时为 0）不被伪造，migration idempotent、integrity/FK clean。

## 3. Outcome 与 retry semantics

- `accepted/known_applied`：只表示 request accepted；保存 safe Evidence 与 DeliveryRecord，不产生 user_seen/user_read。
- `failed/not_started`：明确没有开始 effect；只允许一次显式 retry，新的 attempt identity 与 logical identity 分离。
- `unknown/unknown`：timeout、adapter exception 或 action 后 terminal/Evidence persistence ambiguity；禁止 blind retry。
- pending/not_started：effect 尚未进入 dispatch；process restart 可 claim 同一 attempt 并发送一次。

adapter 返回的 raw stdout/stderr 不进入 application Evidence、DeliveryRecord、DTO 或 HTML。accepted Evidence 只保存
`notification_requested=true/request_accepted=true`、Distribution/logical identity 与 certainty claim。

## 4. Temporal acceptance

批准 trace 的 deterministic 结果：

```text
920 -> NO_UPDATE                              -> notification calls 0
760 -> Update #1 + Distribution #1           -> notification calls 1
750 -> successful still-matched NO_UPDATE    -> notification calls 1
900 -> successful re-arm NO_UPDATE           -> notification calls 1
780 -> Update #2 + Distribution #2           -> notification calls 2
```

duplicate tick 与直接 replay 同一 Distribution 都不增加 call；accepted/unknown restart 不发送，pending/not-started restart 只发送
一次。Notification failure/unknown 不改变历史 Update/Distribution；Feeds 仍显示 ready Update。

## 5. HTTP/UI

Feed Detail 在对应 Update 下只显示“通知请求已发送”“正在发送通知”或“通知暂不可用；这条更新仍可在这里查看”。DTO/HTML 不暴露
certainty enum、adapter、AuthorizedAction、attempt ID、Termux executable、Outbox、Evidence ID，也不声称用户已看到或已阅读。

## 6. Real Android smoke

deterministic gates 全绿后，使用一条新的 demo Flight Distribution，经 prepared→executing→terminal Authority 与既有
`termux:notification` adapter 真实提交一次。结果为 `request_accepted`、Delivery `accepted/known_applied`、attempt=1；safe
Evidence 只有 `notification_requested/request_accepted`，没有 raw stdout/stderr 或 user_seen。对同一 Distribution 立即 replay，
实际 Termux call 仍为 1。

第一次手工 smoke composition 把 replay policy 写成不存在的 `never_retry`，在 checkpoint persistence/registry/executable 前
抛错。DeliveryService 已在 dispatch-start boundary 后保守保存另一条 `unknown/ADAPTER_EXCEPTION`；该 Distribution 没有重试。
第二条全新 Distribution 使用正式 `never_auto_retry` 后 accepted。这个 incident 没有被删除或改写，也不能被写成一次真实
Termux failure。

## 7. Scope boundary 与 Harness gap

Application eligibility 与 durable intent/attempt 是本 slice 新增能力；已有 DeliveryService certainty、Termux adapter 与 core
Authority 无需修改。没有 Delivery scheduler：P4.4 worker 在 Update transaction 完成后 dispatch，startup wake 会收口已存在的
pending Distribution。email/SMS、real/cloud push、EVENT、shared execution、read receipt 与 production scheduler 均未实现。

新暴露的 Application gap：repository 有 injected authorized dispatcher port，但项目尚无一个小型、可复用的非 Agent app
composition helper 来创建/persist Environment action checkpoint；真实 smoke 需要手工组合 core primitives，容易发生上述配置错误。
现有 core 在错误发生后正确 fail closed，未暴露必须修改 `mini_harness_core` 的缺口。

## 8. Verification record

```text
P4.5 focused temporal/restart/failure/policy/UI tests: PASS (8 tests)
v15 -> v16 success/idempotency/partial rollback: PASS
python -m unittest -q: PASS (917 tests)
python mini_harness.py --self-check: PASS
JavaScript syntax: PASS (4 inline scripts)
docs links: PASS (612 local links)
git diff --check + secret/runtime scan: PASS
git diff --exit-code -- mini_harness_core: PASS (empty)
real demo DB v15 -> v16: PASS (history preserved; integrity/FK clean)
Real Android termux:notification smoke: PASS (request_accepted; actual call=1; duplicate replay call=0)
```

P4.5 unresolved correctness blocker：0。COMPLETE 只覆盖 Flight CONDITION Distribution-aware Termux notification；真实 smoke
accepted 不代表 user_seen/user_read，更不代表 cloud push 或 Notification scheduler 已上线。
