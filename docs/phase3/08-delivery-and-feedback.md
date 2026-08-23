# Delivery and Feedback

## DeliveryService boundary

```text
completed generation Result
  -> persisted Digest
  -> create DeliveryRecord(pending)
  -> authorized termux:notification action
  -> Observation / Evidence / delivery Result
  -> DeliveryRecord(accepted | failed | unknown)
```

`DeliveryService` 接受 `digest_id` 与 `DeliveryPreference`，加载已持久化 Digest，生成长度受限
的 notification arguments，并通过既有 Environment/Harness integration 调用
`termux:notification`。Adapter 不查询 Subscription/Profile，也不直接写应用数据库。

当前实现只发送最长 160 字符、移除换行的 deterministic preview + local Digest ID，title 固定为
`AI Digest`。notification 只是 delivery hint；canonical Digest 仍在 Artifact/SQLite。这种 transport
formatting 不改变 Digest 或其 `max_chars` contract。

## DeliveryRecord

SQLite schema v3 使用逻辑 head + immutable attempt history：

```text
delivery_records:
  delivery_id, digest_id, user_id, channel, status,
  current_attempt_number, current_attempt_id, created_at, updated_at

delivery_attempts:
  attempt_id, delivery_id, attempt_number, status,
  provider_message_id?, requested_at, completed_at?, error_code?, effect_certainty
```

`delivery_id = SHA256(digest_id, channel)[:32]`，数据库同时 unique `(digest_id, channel)`；
`attempt_id = SHA256(delivery_id, attempt_number)[:32]`。普通重复 `deliver_digest` 只返回当前记录，
不再次 dispatch。只有当前 attempt 是 `failed/not_started` 时，显式 `retry_delivery` 才能创建
attempt N+1；`unknown` 禁止 blind retry。

写入顺序固定为：durable `pending/not_started` → dispatch 前 durable `unknown/unknown` → adapter
dispatch → durable terminal outcome。第二步是保守的 crash fence：若 dispatch 已完成但 terminal
write 失败，数据库仍是 unknown，不会假定未发送。raw provider response 不持久化，只保存 safe
external ref、error code 与 certainty。

`accepted` 的语义仅是 notification request accepted。它不是 handset displayed、user opened、
user read 或 user liked。只有独立 Feedback endpoint 生成 Interaction。

当前 correctness adapter `FakeDeliveryAdapter` 有三个 deterministic mode：

| Mode | Delivery status | Effect certainty |
|---|---|---|
| `accepted` | `accepted` | `known_applied` |
| `explicit_failure` | `failed` | `not_started` |
| `timeout_unknown` | `unknown` | `unknown` |

`TermuxNotificationDeliveryAdapter` 只做 safe preview 和结果映射；它必须注入已有的 authorized
Environment dispatcher，不能直接调用设备 executable。真实 Termux smoke 是 opt-in，不进入测试 gate。

## Feedback flow

```text
POST feedback
  -> validate digest/item belongs to local user
  -> enforce feedback_id idempotency
  -> insert Interaction
  -> compute fixed topic deltas
  -> update Profile + ProfileUpdate atomically
  -> next generation loads new profile version
```

Feedback types 是 `opened/liked/dismissed/saved`。`opened` 可以是 digest-level；其余默认绑定
具体 item，避免把一个条目的偏好错误传播到全篇。V1 允许同一用户对不同 item 分别反馈；
是否允许撤销需另行设计，不通过删除历史 Interaction 实现。

当前 slice 已实现 `FeedbackService` 与 SQLite v2 feedback path。Service 先验证
Digest/Item 属于 user，从 immutable Digest item 读取 normalized topic tags，再调用 repository 的
单事务更新。固定 delta 是 opened `+1`、liked `+3`、dismissed `-3`、saved `+4`，每个 topic
clamp 到 `[-20,20]`。notification accepted 仍不会生成 opened。

若 transaction 任一步失败，Interaction、weights、Profile version 与 ProfileUpdate 全部回滚；调用方
收到 persistence error，不能返回“profile 已更新”。已经 completed 的 Digest、Harness Result 与其
原 profile/ranking snapshot 不参与该事务，因此 feedback 成败都不会反向改写历史 generation truth。

## API sketch

| Endpoint | Semantics |
|---|---|
| `POST /subscriptions` | body 含 natural language；返回 normalized Subscription 或 422 |
| `GET /subscriptions` | 列出本地用户 Subscription |
| `POST /subscriptions/{id}/run` | reserve period key，运行 generation，然后尝试 delivery |
| `GET /digests/{id}` | 返回 Digest、sources 与 delivery summary |
| `POST /digests/{id}/feedback` | 幂等记录 feedback 并返回 profile version |
| `GET /profile` | 返回可解释 topic weights，不返回 prompt/session |

推荐响应：create `201`，查询 `200`，manual run 首次 `202`/同步教学实现可 `200`，重复 run
返回已有资源 `200`，validation `422`，不存在 `404`，当前 blocked/unknown 用资源内明确状态
而不是伪造 `500`。V1 不做 auth；server 只绑定 loopback，user identity 使用固定本地配置。

上一页：[`07-personalization-and-recommendation.md`](07-personalization-and-recommendation.md) · 下一篇：
[`09-failure-and-recovery.md`](09-failure-and-recovery.md)
