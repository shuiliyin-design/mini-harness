# Personalization and Recommendation

## InterestProfile

V1 Profile 是 application state，不是 prompt memory：

```json
{
  "user_id": "32-lower-hex",
  "version": 7,
  "rule_version": 1,
  "topic_weights": {
    "agent": 6,
    "模型发布": 3,
    "开发工具": -1
  },
  "updated_at": "2026-08-23T00:00:00Z"
}
```

Topic keys 经 trim、casefold（适用时）和长度限制规范化；projection 再限制为当前 Subscription
topic/focus 集合。weight 是范围
`[-20, 20]` 的整数。完整 Interaction history 单独保存，不塞进 Profile JSON 或 Model context。

当前实现由 `InterestProfile` 与有界 `TopicWeight` 表达。SQLite schema v2 用
`interest_profiles` 保存 head、用 `profile_topic_weights` 保存 normalized topic。没有 feedback
时不虚构数据库行；generation 使用 version `0` 的空 Profile，并把其 safe projection identity
绑定到本次 DigestRun。

## Deterministic feedback update

| Interaction | Delta |
|---|---:|
| `opened` | +1 |
| `liked` | +3 |
| `saved` | +4 |
| `dismissed` | -3 |

item-level feedback 更新该 item 的 normalized topic tags；digest-level `opened` 对本 Digest 的
去重 topic 集合各加 +1。每个 `feedback_id` 只应用一次，结果 clamp 到 `[-20, 20]`，并在同一
SQLite transaction 中保存 Interaction、ProfileUpdate 与新 profile version。

这套规则刻意简单：用户能知道某次点击怎样影响下一期，也能通过历史事件重算 Profile。
以后改变 delta 必须增加 `profile_rule_version`，不能静默重写旧解释。

`feedback_id` 是 canonical `(user_id, digest_id, item_id|null, feedback_type, event_key)` 的
SHA-256 前 32 hex。相同 event 重放返回已有 ProfileUpdate 与 `applied=false`，不会二次累计；不同
`event_key` 是不同的显式用户事件。Interaction、weights、Profile head 与 ProfileUpdate 在同一个
SQLite transaction 中提交。

## Deterministic ranking

对通过 freshness/dedup/evidence gate 的候选计算整数 score：

```text
+40  subscription topic tag match
+15  each focus topic match, capped at +30
+2   per profile weight point, naturally bounded -40..+40
+20  published <= 24h
+10  published <= 72h
 +5  published <= 7d
-100 already seen canonical content identity
```

对每个 candidate，先把 tags 命中的 projection weights 求和并 clamp 到 `[-20,20]`，再乘 2；所以
profile 分量始终在 `[-40,40]`。当前 `score_breakdown` 固定保存五项（包括零值）：
`subscription_topic`、`focus_topics`、`profile_weight`、`freshness`、
`already_seen_penalty`。

先按 `score DESC`，再按 `published_at DESC`，最后按 `candidate_id ASC` 打破平局。保存完整
score breakdown 与 rule version。若候选已见但没有新内容，它通常被排到末尾；产品可明确
返回 `no_new_content`，不要求 Model 编造条目。

Model 可以为已确定的排序生成短解释，但不能增删候选、改分或改变 tie-break。V1 不使用
embedding、vector DB、collaborative filtering、semantic reranker 或 RAG。

## Safe profile projection

送给 Model 的 projection 只包含与当前 Subscription 相关、按 absolute weight 排序的有限 topic
及整数 weight，例如最多 10 项。不得发送 user ID、Interaction timestamps、完整点击历史、
delivery state 或其他 Subscription 数据。Model Context 是 Profile 的受限视图，不是 Profile
本体；Session Memory 也不是业务数据库。

实际 projection 只有 `profile_version`、`profile_rule_version`、相关 `topic_weights` 与
`projection_id`。identity 是前三者 canonical JSON 的 SHA-256，不含 user/history/timestamp。
DigestRun、Digest payload 和 Artifact references 保存 version/identity；旧 Digest immutable，反馈
只影响新 run。Profile State 不创建 Evidence，也不进入 Search Evidence chain。

上一页：[`06-output-contracts.md`](06-output-contracts.md) · 下一篇：
[`08-delivery-and-feedback.md`](08-delivery-and-feedback.md)
