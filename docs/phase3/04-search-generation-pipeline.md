# Search and Generation Pipeline

## Boundary objects

```text
SearchQuery
  query_id, text, language, freshness_days, max_results

SearchObservation
  provider + query digest + bounded normalized rows + safe metadata + identity

ContentCandidate
  candidate_id, canonical_url, title, snippet,
  published_at?, retrieved_at, source_domain, topic_tags, content_identity

AcceptedEvidence
  Harness evidence_id + accepted verification/provenance
```

Brave Search response 只产生 `SearchObservation`。Adapter 先校验 HTTP/protocol/schema、做 size
limit 与 safe normalization；Harness 调用 application-supplied deterministic verifier，把当前 Run
的 MCP Observation identity 与规范化 candidate-set identity 绑定为 accepted verification Evidence。
只有引用该 accepted `evidence_id` 的 ContentCandidate 能进入 selection。

## Brave boundary

Real Brave slice 把 app-owned `BraveSearchClient` 实现成现有 `MCPClient` seam，并注入：

```text
MCPRegistry(
  clients={"search": BraveSearchClient.from_environment(...)},
  tool_policies={"mcp:search:web_search": "ALLOW"},
  tool_effects={"mcp:search:web_search": "read_only"},
)
```

Policy/effect 是 Harness 本地配置，不能信任 Brave metadata。Endpoint 固定为
`https://api.search.brave.com/res/v1/web/search`；官方接口要求把 subscription token 放在
`X-Subscription-Token` header，`q` 最多 400 characters/50 words，`count` 最大 20。实现只使用
stdlib `urllib`、固定 timeout、固定 User-Agent、`Accept: application/json`、bounded body read，
不引入 SDK，也不允许 caller 传 endpoint/header/auth/request options。参考 Brave 官方
[Web Search API](https://api-dashboard.search.brave.com/api-reference/web/search/get) 与
[authentication guide](https://api-dashboard.search.brave.com/documentation/guides/authentication)。

API key 只在 dispatch 时从 `BRAVE_SEARCH_API_KEY` 环境变量读取，并只进入 required auth header。
它不进入 arguments、client call journal、SQLite、Audit、Evidence、Artifact、Result 或 exception。
缺 key 产生 safe `CONFIGURATION_ERROR`。Adapter 不读取 `.env.local`，也不接受明文 key 参数。

`FakeSearchClient` 与 `BraveSearchClient` 输出同一 safe MCP result contract：

```json
{
  "provider": "brave",
  "query_identity": "sha256(normalized query)",
  "result_count": 2,
  "request_metadata": {"result_limit": 5},
  "response_metadata": {
    "http_status": 200,
    "response_bytes": 1234,
    "retry_after_seconds": null
  },
  "observation_identity": "sha256(all safe fields except itself)",
  "results": [{
    "source_id": "sha256(canonical URL)",
    "title": "bounded title (240 chars)",
    "url": "https://canonical.example/path",
    "snippet": "bounded description (320 chars)",
    "published_at": "optional reliable ISO-like provider age",
    "topic_tags": []
  }]
}
```

Brave raw JSON、unknown fields 和 raw headers 都不会越过 adapter。只从 `web.results` 提取
title/url/description/age；无效 item 丢弃。缺可靠 provider date 时，domain normalization 使用当前
`observed_at`。URL 先走 application canonicalization，再以 canonical URL 的 SHA-256 生成
`source_id`；同 URL 只保留第一次 valid result，source identity 不依赖 list position。
下游 `normalize_candidates` 再执行与 Fake 相同的 schema、exact dedup 与 candidate identity 规则。
Brave 不提供可信 topic tags，因此 domain 只从 bounded title/snippet 做保守词法派生：完整 topic/focus
短语命中即可；多词英文主题先移除 `latest/news/updates` 等低信号 query 词，再要求至少两个且不少于
40% 的 token 精确命中。派生 tag 表示可重放的 metadata match，不表示深层语义已被证明。

Query 先 NFC/whitespace normalization，再检查 non-empty、UTF-8、control chars、400 characters、
50 words 与 Harness secret patterns。Caller 只可提供 `query/max_results`；query raw text 是本次调用的
ephemeral input，safe result/Audit/Evidence 只保留 digest。结果数同时受 caller、Brave count 20 与
application search cap 10 约束；workflow request limit 是
`min(10, max(3, subscription.max_items))`。

Fake 与 Brave 都使用固定 application integration：它通过现有 sealed dispatch 调用
`mcp:search:web_search`，先保存 untrusted `mcp_observation`，再做 deterministic normalization，
最后调用 Harness verification Evidence constructor/store 绑定 candidate-set SHA-256。它没有新增
callback，也没有让 Search success 自动 accepted。HTTP 200/valid JSON 只产生 safe Search
Observation；只有 shared normalization/acceptance PASS 才产生 accepted verification Evidence。

## Generation sequence

1. Application 加载 enabled Subscription、Profile safe projection，并 reserve `DigestRun`。
2. 当前固定 workflow 从 normalized topic/focus 机械构造一条 Search query。
3. Adapter 与 MCP schema 分别校验 query、result limit 和 exact arguments。
4. Harness 对 `mcp:search:web_search` 执行 local policy/effect 与 sealed dispatch。
5. Search Observation 安全规范化；失败不会被包装成空成功。
6. Application 接受并绑定 Evidence 后生成 ContentCandidates。
7. 丢弃缺 URL/title、超 freshness window 或无 accepted evidence 的候选；同时保存 lexical topic tags。
8. canonical URL/content identity dedup；相同 cluster 只保留确定性 winner。
9. 使用固定 score 选出至多 `max_items`，记录每项 breakdown 和 tie-break。
10. Model 只看到 selected candidate safe projection，并返回结构化 DigestDraft。
11. Application/Harness deterministic contract 验证 Draft，包括至少一个 topic/focus tag match；当前
    不合格直接 incomplete，不 regenerate。
12. 合格内容写 canonical JSON Artifact；Harness 执行 exists/non-empty/identity/verified gate。
13. Harness 绑定 Authoritative Result；只有 `completed` 且 accepted artifact 才可投影 SQLite。
14. Application 在事务中保存 Digest/Items/SourceRefs/seen content，随后单独尝试 delivery。

当前 generation slice 实现到第 14 步的 Digest/seen persistence，并让后续 run 使用 Profile safe
projection。独立 delivery slice 已在 completed Digest 后保存 DeliveryRecord/attempt，且不反写
generation Result。固定 workflow 通过 `WorkspaceArtifactClient` 的 materialize/observe 两个
capability 走 Harness Authority，只有 application contract PASS 的 payload identity 才可写入。

## Freshness and dedup

- `published_at` 可验证时按它计算；缺失时使用 `retrieved_at`，并明确标注 `date_quality=retrieved`。
- freshness cutoff 由 Subscription cadence 的固定 V1 window 计算，例如 daily 默认 7 天。
- URL canonicalization 只做公开的机械规则：lowercase host、移除 fragment、删除 allowlisted tracking
  params、稳定排序 query；不发网络请求猜 canonical URL。
- exact canonical URL 或 normalized title identity 相同视为 duplicate；模糊语义聚类不在 V1。
- already-seen content 不删除历史，只在 ranking 中强惩罚；若全部已见可得到 no-new-content。

## Model context

Model 不接收 raw Brave response、API key、完整 Profile/Interaction history 或任意数据库记录。
它只接收 schema 限定的 Subscription/Profile projection 与 selected candidates（ID、title、snippet、
source marker、日期）。每个 DigestItem 必须回传 candidate ID，不能生成新 URL。

## Vertex synthesis boundary

`VertexDigestProvider` 位于 `apps/digest_agent/adapters/provider.py`，使用当前环境已有的
Vertex-backed LiteLLM OpenAI-compatible gateway：

```text
LLM_ENDPOINT + LLM_API_MODE + LLM_MODEL
  -> bounded HTTPS POST
Authorization: Bearer <LLM_API_KEY>
  -> completions/chat-completions envelope
  -> strict JSON synthesis candidate
```

当前实验配置走 `chat-completions` 并请求 strict schema tool；真实 browser run 证明 gateway 接受请求不
代表 schema enforcement。改用无 nested object/array 的顶层标量 tool wire schema 后，10 次有界真实
provider gate 与连续 3 次 Browser/HTTP 产品验收均通过。Legacy
`completions` 仍保留为 prompt-only compatibility path，但 real Vertex startup readiness 不接受它。Endpoint 必须是
无 userinfo/query/fragment 的 HTTPS URL，timeout/body size 固定有界，redirect 禁止。Credential 只在
dispatch 时进入 Authorization header，不进入 prompt、call journal、exception、Audit、Evidence、
Artifact、Result 或 SQLite。Raw response/error body 不越过 adapter。

Prompt input 只有：Subscription 的 ID/version/topic/focus/language/max limits、ordered ranked candidate
safe projections、accepted Evidence IDs、period key 与 Profile safe projection。明确排除 natural-language
raw request、user ID、raw Brave response、Interaction history、secret 与 Harness hidden state。

模型在 Vertex tool wire 上必须返回唯一 JSON object：

```json
{
  "summary": "bounded content",
  "candidate_id": "rank-1 selected ID",
  "content_identity": "rank-1 selected identity",
  "content": "source-bounded synthesis",
  "recommendation_reason": "short explanation",
  "source_ref_id": "S1"
}
```

Chat singleton 只向 Model 投影 Harness 已排名的 rank-1 candidate；Model 不拥有 selection Authority。
Adapter 检查 transport/envelope/JSON/exact wire schema 后，由六个标量确定性重建 canonical
`items`/`selected_source_refs` lists，再执行未放宽的完整 candidate schema。Canonical URL、Evidence ID、topic tags、
rank、score 与 breakdown 从既有 ordered selection 补回；模型改变 order/identity/ref、产生重复或遗漏
source 时仍会保留为不可信 candidate，并由同一个 `evaluate_digest_contract` 拒绝。`character_count`
永远由代码对最终 rendered text 重算，模型没有“已控制长度”的声明通道。

上一页：[`03-subscription-schema.md`](03-subscription-schema.md) · 下一篇：
[`05-harness-integration.md`](05-harness-integration.md)
