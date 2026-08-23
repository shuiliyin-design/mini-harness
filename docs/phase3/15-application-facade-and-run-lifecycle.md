# Application Façade and Run Lifecycle

## Public business boundary

`DigestApplication` 是 CLI、未来 loopback HTTP/Web UI 与 product tests 的唯一主要业务入口。它组合
repository、Subscription service、generation workflow、Delivery service 与 Feedback service；transport
不直接拼这些对象。公开方法是：

```text
create/update/enable/disable/list/get subscription
run/recover subscription
list/get digest
deliver digest
record feedback
get profile
```

返回值是 immutable application DTO：`SubscriptionView`、`RunView`、`DigestView`、`DeliveryView`、
`FeedbackView`、`ProfileView`。DTO 不包含 Harness Result、Evidence、Artifact、checkpoint、audit event、
run envelope 或 SQLite row。`application_run_id` 是产品 identity；Harness run ID 只留在 workflow/repository
correlation data。

## Subscription lifecycle and history

```text
create(v1, enabled) -> update(vN+1) -> disable(vN+1) -> enable(vN+1)
```

Update 使用 `expected_version` 的 SQLite compare-and-swap，可修改 topic、原始自然语言、daily cadence、
language、max chars/items、focus topics 和 delivery preference。Disable 只阻止 future Run，不删除或改写
Digest、Feedback、Profile、Run、Artifact 或 Delivery history；V1 不做 hard delete。

每个 application run 在 reservation 时保存完整 validated Subscription snapshot + version 和 Profile safe
projection。后续更新 Subscription 不改变旧 Digest 的长度、语言、topic 或 ranking 解释。

## Application run identity and states

三个 identity 永不复用：

```text
subscription_id
  + idempotency_key -> application_run_id (digest_run_id storage column)
                         -> harness_run_id (separate durable binding)
```

公开稳定状态是 `reserved`、`running`、`completed`、`incomplete`、`failed`、`blocked`、`cancelled`、
`recovery_required`；`rejected` 用于 disabled request。`running_recovery` 是 SQLite 内部 CAS claim，投影为
active，不是 transport contract。Application 不复制 Harness state machine；terminal generation state 始终
由 immutable Harness Result 决定。

## Reservation, binding and recovery

1. 短事务 reserve application run，唯一键为 `(subscription_id, idempotency_key)`；此时 Harness ID 只是
   预分配 correlation，`harness_bound_at` 为空。
2. 短事务 CAS bind，写 `running/harness_bound_at/started_at`；commit 后才开始 Search/LLM/Harness work。
3. 普通重复请求只返回 existing terminal resource，或对 active run 返回 `run_already_active`，不会自动接管。
4. 显式 `recover_run` 表示单实例 operator 已判定旧 owner 不再活动：
   - reserved + unbound：CAS bind 后继续，同一 application/Harness identity；
   - bound + 没有 Harness audit event/Result：CAS `running_recovery` 后用同一 Harness ID 启动；
   - terminal Harness Result：不执行 Search/LLM，只校验 Result/Artifact current identity 并修复 SQLite projection；
   - 有 Harness events 但无 terminal Result：不猜测 side effect，标为 `recovery_required`。

该设计不实现 Harness resume engine，也不做 distributed lock。SQLite CAS 只保证单实例 claim；
`recovery_required` 必须由明确的后续 reconciliation 决策处理。

两个近同时的 `run_subscription` 请求先竞争同一 unique reservation：winner bind 后执行，loser 读取同一
application/Harness identity 并返回 active。确定性 concurrency test 在 winner 的 Search seam 阻塞 loser，
最终证明只有一条 run row、一个 Harness ID、一次 Search/Provider 和一个 Digest。

## Transaction boundaries

| Boundary | SQLite transaction | Transaction 外 |
|---|---|---|
| Subscription update/enable/disable | version CAS + payload | 无 |
| Run reservation | identity、snapshots、reserved | Search/LLM/Harness |
| Harness binding/recovery claim | status + timestamps CAS | Harness execution |
| Generation projection | Digest + seen content + terminal run | 已完成的 immutable Result/Artifact |
| Delivery | reserve/crash fence/terminal 各短事务 | notification dispatch |
| Feedback | Interaction + Profile weights/head/update | 无 external call |

任何 HTTP、LLM、Search、notification 都不在长 SQLite transaction 中。External work 之前先 durable intent；
之后只持久化 safe outcome。

## Safe failure projection

Façade 只返回 `configuration_error`、`search_unavailable`、`generation_incomplete`、
`subscription_disabled`、`run_already_active`、`recovery_required`、`delivery_failed`、
`delivery_unknown` 等 application code。Traceback、raw provider body、prompt、Harness reason/details 不进入 DTO。

## Deterministic Product E2E

`tests/apps/test_digest_application.py` 全程通过 façade 完成 create → update → disable/rejected → enable →
run → list/get Digest → delivery → feedback → Profile update → next run，并从两个 Digest 的 deterministic
score breakdown 证明 `profile_weight` 改变。另覆盖 unbound recovery、bound/no-event identity reuse、terminal
Result projection repair、ambiguous `recovery_required`、public DTO sealing 与 safe failure projection。
并发 fixture 还覆盖两个近同时相同 idempotency request 不会产生双 Run。

真实 external services 仍只提供 integration confidence；Fake Search/FakeProvider/FakeDelivery 是
deterministic correctness gate。本 slice 不增加 HTTP server、Web UI、scheduler、auth、worker 或 Harness capability。

上一页：[`14-product-readiness-review.md`](14-product-readiness-review.md) · 返回：[`README.md`](README.md)
