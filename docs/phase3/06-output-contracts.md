# Digest Output Contracts

## Canonical Digest payload

```json
{
  "schema_version": 1,
  "digest_id": "32-lower-hex",
  "subscription_id": "32-lower-hex",
  "subscription_version": 1,
  "period_key": "2026-08-23",
  "language": "zh-CN",
  "profile_snapshot": {
    "profile_version": 1,
    "profile_rule_version": 1,
    "topic_weights": [{"topic_key": "agent", "weight": 3}],
    "projection_id": "64-lower-hex"
  },
  "rendered_text": "AI 日报……[S1]",
  "character_count": 123,
  "items": [
    {
      "item_id": "32-lower-hex",
      "candidate_id": "32-lower-hex",
      "content_identity": "64-lower-hex",
      "topic_tags": ["ai 行业动态", "agent"],
      "rank": 1,
      "score": 81,
      "score_breakdown": [
        {"component": "subscription_topic", "value": 40},
        {"component": "focus_topics", "value": 15},
        {"component": "profile_weight", "value": 6},
        {"component": "freshness", "value": 20},
        {"component": "already_seen_penalty", "value": 0}
      ],
      "recommendation_reason": "已确定排序的展示解释",
      "text": "……[S1]",
      "source_ref_ids": ["S1"]
    }
  ],
  "source_refs": [
    {
      "source_ref_id": "S1",
      "candidate_id": "32-lower-hex",
      "canonical_url": "https://example.test/article",
      "evidence_id": "32-lower-hex"
    }
  ]
}
```

Artifact 内不保存 hidden reasoning、raw search response 或 secret。`character_count` 必须等于
`len(rendered_text)`，而不是 Model 自报值。

## Deterministic contract

下列条件全部可由纯代码判定，任一失败都不能接受 Artifact：

1. schema version、required fields、types、IDs、timestamps 合法；无 unknown fields。
2. `character_count == len(rendered_text) <= subscription.max_chars`。
3. `1 <= len(items) <= subscription.max_items`；no-results branch 不生成假 Digest。
4. item IDs、candidate IDs、source ref IDs 均唯一；不存在重复 item。
5. 每个 item 的 `candidate_id` 属于本 Run 的 `CandidateSelection`。
6. 每个 item 至少一个 source ref；ref 指向同一 selected candidate。
7. 每个 source ref 的 URL/identity 与 normalized candidate 完全一致。
8. 每个 source ref 绑定本 Run accepted evidence；historical/foreign/unaccepted ID 拒绝。
9. `subscription_id/version/period_key/language` 与 reserved snapshot 完全一致。
10. `rendered_text` 只能引用 payload 中存在的 `[S<n>]` marker，且不得留孤立 ref。
11. selection 的 deterministic topic/focus tags 至少命中 subscription topic 或 focus allowlist。
12. canonical JSON 再经 Harness `exists/non_empty/content_identity/verified` file contract。
13. profile snapshot 与本 Run reserved safe projection identity/version 一致；item order、score 和固定
    五分量 breakdown 必须与 deterministic selection 完全一致。

第 11 条只验证可追溯的 metadata match，不声称理解文章的深层语义。真正“是否足够相关”属于
semantic quality，不能用一个伪精确分数冒充 correctness。

## Semantic quality requirements

- 摘要是否忠实、信息密度是否合适；
- 对 topic/focus 的自然语言相关性；
- 多条内容是否覆盖面均衡；
- 表达是否清楚、是否真正有用；
- source snippet 是否足以支撑更细的事实表述。

V1 通过 prompt 约束、source-limited synthesis、人工 review 与 feedback 改善这些属性。Model
可以解释推荐理由，但不能 self-certify contract，也不引入 LLM grader 作为测试 gate。

## Rejection and bounded repair

- 过长、缺字段、ref 错误：保留 rejection reason，最多进行固定次数 regeneration。
- Repair context 只包含结构化错误与原 selected candidates，不加入新搜索事实。
- 每次 Draft 都是 candidate；只有最终 deterministic PASS 才 materialize Artifact。
- budget 用尽仍不合格：generation Result 为 `incomplete`，不得截断正文后伪装通过。机械
  truncation 可能破坏 item/source pairing，因此不作为默认 repair。

## Current implementation

`contracts.evaluate_digest_contract()` 已实现 schema、computed character count、max chars/items、
selected membership/order、score breakdown、profile snapshot、unique items、source URL/Evidence
binding、marker closure 与 topic/focus metadata match。失败 payload 不会 materialize Artifact；
`run_agent` 看到 unsatisfied workspace Output Contract 后发布 authoritative `incomplete`。本切片不做
regeneration，保留原始 violation codes 作为 application Result reason。

上一页：[`05-harness-integration.md`](05-harness-integration.md) · 下一篇：
[`07-personalization-and-recommendation.md`](07-personalization-and-recommendation.md)
