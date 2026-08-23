# Domain Model

## Aggregate boundaries

### Subscription aggregate

- `User`：V1 只有本地用户 identity 与 locale；不引入 auth/multitenancy。
- `Subscription`：用户意图的正式、可编辑状态；`max_chars`/`max_items` 是字段，不是 prompt 注释。
- `DeliveryPreference`：channel 与 channel-safe settings；不保存 secret。

`SubscriptionService` 负责 create/list/update/enable/disable。自然语言 parser 只返回
`SubscriptionCandidate`；schema validator、defaults 和 repository commit 都由 Application 决定。

### Generation aggregate

- `DigestRun`：一次手动运行 reservation，绑定 `subscription_id`、`period_key`、Harness
  `run_id`、generation status 与 Artifact/Result references。
- `SearchQuery`：query text、language、freshness window、proposal provenance。
- `SearchObservation`：一次 Search tool 的安全规范化投影，含 provider/query digest、bounded rows、
  allowlisted metadata 与 identity；raw provider payload 从不成为 domain object。
- `ContentCandidate`：接受后的候选，含 canonical URL、标题、摘要、发布/获取时间、topic tags；
  Search source identity 可由 accepted Observation 中的 canonical URL 稳定派生。
- `CandidateSelection`：确定性 dedup/ranking 后的有序 candidate IDs 与 score breakdown。
- `DigestDraft`：Model 输出候选，没有 acceptance authority。
- `Digest` / `DigestItem` / `SourceRef`：应用保存、呈现与反馈所针对的 immutable 内容版本。

V1 的 Harness Artifact 是 workspace 中的 canonical Digest JSON（可另带 Markdown view）；SQLite
Digest 是 completed Result 之后的 application projection。两者以 `artifact_id`、SHA-256 与
`harness_run_id` 关联，不能互相假装是同一种历史对象。

`AcceptedEvidence` 不是 application domain entity；它仍是 Harness-owned immutable historical
object。`ContentCandidate` 只保存 Harness evidence ID 的引用，不能自行声明 accepted。

### Personalization and delivery aggregates

- `InterestProfile`：每个 User 的解释性 topic weights、version 与更新时间。
- `Interaction`：对 Digest 或 DigestItem 的 opened/liked/dismissed/saved 事件。
- `ProfileUpdate`：从单个 Interaction 计算出的固定 delta 与 before/after version。
- `DeliveryRecord`：一次通知 attempt 的 request identity、状态、Harness action/run reference。

DeliveryRecord 的 `accepted` 只表示 adapter/Termux 接受请求。`Interaction(opened)` 必须来自
独立用户反馈，不能由 delivery success 推导。

## Lifecycles

```text
Subscription: enabled <-> disabled

DigestRun: reserved -> running -> generated
                    -> incomplete | failed | blocked

DeliveryRecord: pending -> accepted | failed | unknown

Digest: persisted -> delivery attempted -> feedback accumulated
```

这些是 application lifecycle，不取代 Harness Run/Action/Result 状态。Application 可以显示
组合状态，例如 `generated_not_delivered`，但必须同时保留原始 authoritative generation Result
与 DeliveryRecord，不得重写二者。

## Identity and immutability

- IDs 使用应用生成的 32 位小写 hex，时间统一 UTC ISO-8601。
- Subscription 可更新，但每个 DigestRun 保存 `subscription_snapshot` identity/version。
- Digest 与 DigestItem 内容生成后 immutable；修正生成新 Digest/version，不原地改历史。
- Feedback 使用唯一 `feedback_id` 幂等；相同 ID 重放不得重复调整 Profile。
- `period_key` 是用户 cadence 下的逻辑期次；`(subscription_id, period_key)` 唯一。

## Persistence model

V1 使用 Python `sqlite3`。`repositories.py` 声明 ports，`adapters/sqlite.py` 才拥有 SQL；domain
objects 不依赖 row shape。目标数据库模型如下：

| Table | Key / important references | Purpose |
|---|---|---|
| `schema_migrations` | `version` | 小型、前向 migration history |
| `users` | `user_id` | 固定本地用户与 locale |
| `subscriptions` | `subscription_id`, `user_id`, `version` | 正式 Subscription 与自然语言 provenance |
| `interest_profiles` | `user_id`, `version`, `rule_version` | Profile head |
| `profile_topic_weights` | `(user_id, topic_key)` | bounded integer weights |
| `digest_runs` | `digest_run_id`; unique `(subscription_id, period_key)` | reservation、Harness run/status/Result refs |
| `content_candidates` | `(digest_run_id, candidate_id)` | normalized safe candidates、score breakdown、Evidence ref |
| `digests` | `digest_id`; unique `harness_run_id`, `artifact_id` | immutable accepted Digest projection |
| `digest_items` | `item_id`, `digest_id`, `candidate_id` | ordered user-visible items |
| `source_refs` | `(digest_id, source_ref_id)` | URL/candidate/evidence traceability |
| `seen_content` | `(user_id, content_identity)` | already-seen ranking penalty |
| `delivery_records` | `delivery_id`; unique `idempotency_key` | notification attempts/outcome truth |
| `interactions` | `feedback_id`; `digest_id`, `item_id?` | immutable feedback events |
| `profile_updates` | `feedback_id`; before/after version | deterministic update audit |

当前 schema v3 的实际表是 `schema_migrations`、`subscriptions`、`digest_runs`、
`content_candidates`、`digests`、`interest_profiles`、`profile_topic_weights`、`interactions`、
`profile_updates`、`seen_content`、`delivery_records` 与 `delivery_attempts`。Digest item/source ref
仍以 immutable canonical payload JSON
保存；FeedbackService 在写入前验证其归属，尚不需要为教学 slice 拆独立 item/ref tables。v2 对
v1 使用前向 migration 并为 DigestRun 增加 profile snapshot；v3 再增加 logical delivery head 与
attempt history。

只保存 normalized candidates 与 safe identities，不保存 raw Brave response、prompt transcript、hidden
reasoning 或 credentials。打开 `PRAGMA foreign_keys=ON`；每个 aggregate commit 使用 explicit
transaction。数据库文件路径由应用配置，不能放在 Harness `.audit/` 中。

上一页：[`01-product-scope.md`](01-product-scope.md) · 下一篇：
[`03-subscription-schema.md`](03-subscription-schema.md)
