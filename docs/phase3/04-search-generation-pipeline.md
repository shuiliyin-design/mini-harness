# Search and Generation Pipeline

## Boundary objects

```text
SearchQuery
  query_id, text, language, freshness_days, max_results

SearchObservation
  tool/action identity + untrusted raw response (ephemeral)

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

推荐把 app-owned `BraveSearchClient` 实现成现有 `MCPClient` seam，并注入：

```text
MCPRegistry(
  clients={"brave": BraveSearchClient(...)},
  tool_policies={"mcp:brave:web_search": "ALLOW"},
  tool_effects={"mcp:brave:web_search": "read_only"},
)
```

Policy/effect 是 Harness 本地配置，不能信任 Brave metadata。Adapter 只从环境读取 key；返回
structured result，不把 credential 放入参数、Observation projection 或 persisted object。
`FakeSearchClient` 实现相同 list/call contract，作为 correctness gate。

第一条 Fake slice 使用固定 application integration：它通过现有 sealed dispatch 调用
`mcp:search:web_search`，先保存 untrusted `mcp_observation`，再做 deterministic normalization，
最后调用 Harness verification Evidence constructor/store 绑定 candidate-set SHA-256。它没有新增
callback，也没有让 Search success 自动 accepted。真实 Brave 若要在通用 Agent loop 内使用，
仍需重新审查 post-MCP-observation verifier seam；当前代码不假装已经解决该扩展点。

## Generation sequence

1. Application 加载 enabled Subscription、Profile safe projection，并 reserve `DigestRun`。
2. Application 建立固定 Plan 与 workspace Digest Output Contract。
3. Model 根据安全投影提出 1..3 个 SearchQuery candidate；Application 校验长度、数量、language。
4. Harness 对 `mcp:brave:web_search` 重新执行 schema、local policy/effect 与 runtime gates。
5. Search Observation 安全规范化；失败不会被包装成空成功。
6. Application 接受并绑定 Evidence 后生成 ContentCandidates。
7. 丢弃缺 URL/title、超 freshness window、无 accepted evidence 的候选。
8. canonical URL/content identity dedup；相同 cluster 只保留确定性 winner。
9. 使用固定 score 选出至多 `max_items`，记录每项 breakdown 和 tie-break。
10. Model 只看到 selected candidate safe projection，并返回结构化 DigestDraft。
11. Application/Harness deterministic contract 验证 Draft；不合格可 bounded regenerate。
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

上一页：[`03-subscription-schema.md`](03-subscription-schema.md) · 下一篇：
[`05-harness-integration.md`](05-harness-integration.md)
