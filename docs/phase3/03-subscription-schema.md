# Subscription Schema

## Canonical record

```json
{
  "schema_version": 1,
  "subscription_id": "32-lower-hex",
  "user_id": "32-lower-hex",
  "topic": "AI 行业动态",
  "natural_language_request": "帮我订阅 AI 行业动态……",
  "cadence": "daily",
  "language": "zh-CN",
  "max_chars": 600,
  "max_items": 5,
  "focus_topics": ["Agent", "模型发布", "开发工具"],
  "delivery_channel": "termux_notification",
  "enabled": true,
  "version": 1,
  "created_at": "2026-08-23T00:00:00Z",
  "updated_at": "2026-08-23T00:00:00Z"
}
```

## Validation rules

| Field | V1 rule |
|---|---|
| `subscription_id`, `user_id` | 32 位小写 hex；由应用生成，不接受 Model 自造持久 ID |
| `topic` | trim 后 1..120 个 Unicode code points |
| `natural_language_request` | trim 后 1..2000；保留用户原文但不含 credentials |
| `cadence` | V1 仅 `daily`；只表达期次，不启动 scheduler |
| `language` | allowlist，V1 至少 `zh-CN`、`en` |
| `max_chars` | integer 且非 bool，100..4000；默认 600 |
| `max_items` | integer 且非 bool，1..10；默认 5 |
| `focus_topics` | 0..10 个去重、保序字符串，每项 1..60 |
| `delivery_channel` | `termux_notification` 或 `none` |
| `enabled` | strict boolean |
| `version` | 从 1 单调增加，用于 optimistic update |

`max_chars` 的计量定义是 Python `len(rendered_text)`：Unicode code points 数，不是 bytes、
tokens 或模型估算。`rendered_text` 是用户实际看到的标题/条目/短 source marker；canonical
URL 等机器 metadata 在 `source_refs`，不靠隐藏 prompt 规避长度检查。

## Natural-language normalization

```text
raw request
  -> SubscriptionParser candidate
  -> allowlisted fields only
  -> deterministic type/range/default validation
  -> user-visible normalized preview
  -> Application transaction commit
```

Parser 可以由 Model 实现，但它不能写数据库、选择 ID、扩张 enum 或绕过 validation。缺少明确
值时使用公开 defaults；相互矛盾或无法安全默认的输入返回 validation error，不猜测提交。
更新 Subscription 时保留 `natural_language_request` 作为 provenance，并递增 `version`。

## Safe model projection

送入 query/synthesis context 的 Subscription projection 只包含：topic、cadence、language、
max_chars、max_items、focus_topics。它不包含 user ID、delivery settings、timestamps 或自由形式
历史。原始 request 仅在确有语言歧义时使用，并按 untrusted application content 处理。

上一页：[`02-domain-model.md`](02-domain-model.md) · 下一篇：
[`04-search-generation-pipeline.md`](04-search-generation-pipeline.md)
