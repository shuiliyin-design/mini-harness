# Harness Integration

## What uses a Harness Run

`generate_digest(subscription_id, period_key)` 使用一个 generation Harness Run，因为它包含
Model decisions、external read-only tool、Plan/Retry、fresh Observation/Evidence、workspace
Artifact acceptance 与 authoritative terminal Result。

建议固定步骤：

```text
plan_queries
  -> search_and_accept_evidence
  -> normalize_rank_select
  -> synthesize_candidate
  -> validate_and_materialize_artifact
  -> authoritative_result
```

Application workflow 负责在 Run 前加载业务快照、在 Run 后解释 Result 并做 SQLite projection；
它不复制 `_run_agent_runtime`、dispatch、Approval、EvidenceStore 或 Result binding。

## What stays ordinary application code

- create/list/update/enable/disable Subscription；
- preview/validate natural-language normalization candidate；
- get Digest/Profile；
- record Feedback 与 deterministic Profile update；
- SQLite migrations/transactions；
- reserve duplicate `period_key`；
- 从 completed Result 的 accepted Artifact 投影 Digest。

这些动作不需要 Agent 自主规划。Parser 使用 Model 时也只是生成候选；Application validator 与
repository commit 拥有最终业务决定。

## Search integration

现有 `MCPRegistry` 接受注入的 clients，并从 Harness 本地 `tool_policies`/`tool_effects` 获取
Authority facts。因此 `BraveSearchClient` 和 `FakeSearchClient` 都应位于 app adapter，暴露
`mcp:brave:web_search`，无需 core import Brave。

现有 MCP Evidence 刻意把 external observation 标为 untrusted，且不能直接通过当前现实
`evidence_gate`。Digest workflow 需要 application-defined、Harness-executed deterministic
acceptance：校验成功状态、schema、limits 与 candidate-set identity，再由 Harness 创建 accepted
verification Evidence 并绑定原 MCP evidence/action。“搜索成功”与“某条资料可被 Digest 引用”
是两个不同判断；这也不表示 Harness 证明了 publisher 的陈述绝对真实。

## Artifact and persistence integration

当前 Harness `OutputContract` 的 artifact type 只有 `workspace_file`，requirements 是
`exists/non_empty/content_identity/verified`。V1 应顺着这个真实边界工作：

```text
workspace/runs/<digest_run_id>/digest.json
  -> Harness Artifact + SHA-256 + Evidence refs
  -> Output Contract accepted
  -> Authoritative Result completed
  -> Application verifies exact artifact/result identity again
  -> one SQLite transaction inserts Digest/Items/SourceRefs
```

应用的 character/item/source contract 先作为 pure deterministic validation gate；通过后再写
文件。Harness 的文件 contract 随后证明当前 Artifact identity。不要把 SQLite row 伪装成现有
`workspace_file` Artifact，也不要为 V1 扩展 historical schema。

## Delivery integration

生成与 delivery 是两段 truth：

1. generation Run 决定 Digest 是否 completed；
2. Digest 成功投影后，DeliveryService 通过独立的 authorized environment action/run 调用
   `termux:notification`，并保存 `DeliveryRecord`。

这允许 generation completed 而 delivery failed/unknown。Application 可展示
`generated_not_delivered`，但不能把 generation Result 改成 failed，也不能把 notification
request accepted 改成 opened。

## Does core need changes?

**当前 Fake vertical slice 不需要 core change。** 固定 application workflow 可以组合现有
`authorize_action`、`dispatch_authorized_action`、MCPRegistry、Evidence constructors/stores、
workspace Artifact 与 `run_agent` Result binding；CRUD 全程不进入 Harness。

当前真实执行链是：

```text
reserve application run
  -> sealed Fake Search dispatch
  -> untrusted MCP Evidence
  -> deterministic normalization acceptance
  -> accepted verification Evidence
  -> FakeDigestProvider candidate
  -> application Output Contract
  -> sealed materialize + read-only observe
  -> verification Evidence + accepted Artifact
  -> run_agent final candidate
  -> authoritative Result
  -> SQLite Digest projection
```

这里的固定 workflow 明确知道何时调用 app verifier；通用 Agent loop 仍没有 post-MCP-observation
extension。真实 Brave 若要求 Model 在 Agent loop 中自主 search，再进入 deterministic application
selection，可能需要下面的最小 seam，但本切片不实现它。

若真实 Brave slice 证明固定 integration 不足，候选最小 seam 是给 Agent runtime 增加一个 typed
`observation_acceptor`（默认 `None`，保持所有现有行为）：

```text
safe MCP observation + run/action/event identities
  -> app verifier returns normalized identity + accepted/reason
  -> Harness validates return schema
  -> Harness creates existing verification Evidence
  -> accepted Evidence may complete the correlated Plan step
```

它不得接收 raw secret、dispatch Tool、授予 Policy、修改 Result 或引入 app import；core 只依赖一份
小 protocol/value schema，具体 Digest verifier 由 app 注入。需要新增离线 unit/integration/security/
architecture tests，并证明 default-None 路径、historical replay、bundle 与 Result schema 零变化。

未来 seam 仍不得新增 Evidence type、Artifact type、state machine 或 plugin framework，也不能让
untrusted MCP Evidence 自动 accepted。具体状态见
[`13-first-vertical-slice.md`](13-first-vertical-slice.md)。

上一页：[`04-search-generation-pipeline.md`](04-search-generation-pipeline.md) · 下一篇：
[`06-output-contracts.md`](06-output-contracts.md)
