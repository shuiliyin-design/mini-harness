# Design Decisions

每项决定都以 V1 教学应用为范围；改变条件出现前，不扩张 Runtime 或基础设施。

## D1. Harness is infrastructure, not application

User、Subscription、Profile、Digest、Delivery 与 Feedback 全部在 `apps/digest_agent`。代价是应用
需要显式 orchestration；收益是 Phase 1 可独立成立。只有跨多个应用复用且属于执行 Authority
的能力，才可能下沉 Harness。

## D2. CRUD does not require Agent execution

CRUD 由 application services/transactions 完成。Model parser 只能提出 SubscriptionCandidate；
validator 和 commit 保留 Authority。不会为简单列表/更新创建虚假 Agent Run。

## D3. Natural language is normalized into structured state

原文保留 provenance，运行使用正式 Subscription schema。这样 `max_chars`、cadence、focus 不依赖
prompt 回忆；代价是需要明确 defaults、validation 与用户可见 preview。

## D4. Search Observation is not Evidence by itself

Brave response 是不可信 external observation；schema/limits/identity acceptance 后才可支撑候选。
代价是多一层记录；收益是搜索 API 的“成功”不能直接变成事实 Authority。

## D5. Length is deterministic

`len(rendered_text) <= max_chars` 由代码检查，Model 自报无效。V1 不把 tokens/bytes/“约 600 字”
混在一起。改变计量方法需 schema/version migration。

## D6. Recommendation ranking starts deterministic

固定整数 weights、freshness buckets、seen penalty 和 tie-break 拥有排序 Authority。Model 只可解释。
当真实反馈证明简单规则不足时再评估更复杂方案，不能先引入 opaque semantic ranker。

## D7. Profile is application state, not raw prompt memory

SQLite 保存解释性 weights 与 events；Model 只拿有限 safe projection。Session/Memory 不替代
repository，也不证明 Profile current version。

## D8. Delivery committed does not mean consumed

notification request accepted、displayed、opened、liked 是不同事件。只有用户 feedback 能产生
Interaction；这避免把 transport success 冒充产品 engagement。

## D9. Fakes are correctness gates; real services are smoke

FakeSearch/FakeProvider/FakeDelivery 与临时 SQLite 覆盖全部 semantics。真实 Brave/LLM/Termux
是 opt-in manual confidence，不让网络或设备决定单元/E2E 正确性。

## D10. Phase 3 must not modify core without a true boundary need

Fake 与 Real Brave slices 都复用 MCPRegistry、sealed dispatch、Evidence constructors、workspace
Artifact 和 Result binding；固定 application workflow 足以表达 acceptance ordering，所以没有改
core。只有未来自主多轮 search 证明不足时才评估 default-off post-MCP verifier，且不得把 Digest
rules/ranking/SQLite 下沉。

## D11. SQLite + stdlib is the local server simulation

Repository ports 隔离 persistence；实现先用 `sqlite3`、foreign keys 与 explicit transactions。
不引入 Postgres/Redis/queue/container。迁移数据库时保持 domain/service contracts，而非提前模拟
分布式架构。

## D12. Digest generation and delivery keep separate truth

Generation 先得到 accepted Artifact/Result 并投影 Digest，Delivery 再单独执行、记录。代价是 UI
要组合两种状态；收益是通知失败不会抹掉已生成内容，unknown side effect 也不会触发 blind retry。

## D13. Harness Artifact remains a workspace file in V1

现有 Historical Artifact schema 只管理 `workspace_file`。V1 生成 canonical JSON 文件并在 completed
Result 后投影 SQLite，不扩展为 database-row Artifact。未来若多个应用需要 typed non-file Artifact，
需设计 versioned historical schema、bundle/replay compatibility 与迁移测试。

## D14. Manual period execution precedes scheduling

`run subscription now` 使用 explicit `period_key` 与 idempotency reservation；cadence 只是 Subscription
状态。先证明完整 chain/recovery，再讨论 scheduler ownership。

## D15. Feedback identity and Profile updates are application transactions

`feedback_id` 由 user/digest/item/type/event key 稳定派生；重复 event 返回原更新，不重复计权。
Interaction、bounded weights、Profile version 与 ProfileUpdate 原子提交。这样 persistence failure
不会伪造画像成功，也不会反写已完成的 Harness Result。规则由 versioned application constants
拥有，不交给 Model 或 Harness core。

## D16. Profile snapshot is state provenance, not Evidence

每次 run 保存只含相关 weights 的 safe projection、version 与 SHA-256 identity；候选评分保存固定五
分量 breakdown。它能解释历史排序，但不表示外部事实，故不会创建 Harness Evidence。Search
Observation 的 acceptance/Evidence chain 保持独立。Model 可读 projection 并生成理由，不能改变
selected order、score 或 tie-break。

## D17. Delivery is a downstream application operation

Harness `completed` 只证明 Digest generation 的 Artifact/Result；DeliveryService 在其后创建独立
DeliveryRecord/attempt。Delivery failed/unknown 不反写 generation truth，accepted 也不生成 opened。
这样产品可以组合展示状态，而不用污染 Harness state machine。

## D18. Dispatch uncertainty is persisted before the external effect

逻辑 delivery 对 digest+channel 幂等，attempt 按序稳定派生。dispatch 前先把 durable attempt 标为
unknown；只有 safe terminal result 落库后才变 accepted 或 failed/not_started。代价是 crash 可能把
实际未开始的 operation 保守记为 unknown；收益是绝不会因 terminal write failure 盲目重发。

## D19. Termux mapping requires existing Authority

Application adapter 只生成 160-character safe preview，并把已有 authorized Environment dispatch 的
`known_applied/not_started/unknown` 映射为应用状态。它不直接执行 Termux binary、不保存 raw response，
也不把 notification 变成 Artifact。真实设备调用仍是 opt-in smoke。

## D20. Brave is an app-owned fixed HTTPS adapter

Brave Web Search endpoint、auth header、timeout、response cap、count 与 User-Agent 都由 adapter 固定；
caller 只能给 normalized query/result limit。使用 stdlib `urllib`，不引入 SDK，不把 provider code
放进 Harness core。

## D21. Credential and raw response never cross the adapter

`BRAVE_SEARCH_API_KEY` 只在 dispatch 时从 process environment 读取并进入
`X-Subscription-Token`。Raw JSON/header/error body 只在 adapter stack 内短暂存在；safe result 只含
bounded normalized rows 与 allowlisted metadata。Exceptions 只暴露 error code。

## D22. Source identity is URL-derived, not rank-derived

每个 result 先 canonicalize URL，`source_id=SHA256(canonical_url)`；同 URL 只留第一个 valid row。
因此 Brave 调整 list order 不会改变 source identity。Candidate identity 与 downstream exact dedup
继续由 application domain 统一计算，Fake/Brave 不分叉。

## D23. Real services are confidence, not correctness

Fake HTTP transport 覆盖所有 adapter/error/Evidence/Provider semantics，真实 Brave/Vertex smoke 只在 env key 存在时
通过 `python -m tools.brave_search_smoke` 显式运行。Real Search + FakeProvider E2E 一次只改变 Search
变量；`tools.vertex_digest_smoke` 再依次只改变 Provider、然后同时使用 Real Search/Provider。HTTP API、
scheduler、scraping、RAG 与第二 real LLM provider 都不进入本 slice。

## D24. Search integration does not migrate SQLite

`source_id` 留在 safe Search Observation/candidate-set acceptance 中，并可由 canonical URL 重算；
现有 ContentCandidate persistence shape、Digest schema 与 SQLite table 保持不变。因此 Real Brave slice
仍使用 schema v3，不新增或跳过 migration，也不把 raw provider payload 写入 JSON columns。

## D25. Topic provenance uses conservative lexical evidence

Brave 的空 topic tags 不会被直接信任，也不会因为“搜索返回了它”就自动标记全部 subscription/focus。
Domain 从 bounded title/snippet 做 exact phrase 或有阈值的多词 token match，再写入 normalized topic
tag。低信号 query modifiers 不计入 token；至少两个且达到 40% 才通过。代价是仍可能漏掉同义表达，
但规则离线、可解释、可回放，并保持 semantic quality 与 deterministic contract 的边界。

## D26. Manual smoke must preserve incomplete truth

真实 Search 成功不等于 Digest completed。Smoke 在访问 projection 前先检查 application/Harness status；
incomplete 只输出 safe reason 并返回 non-zero，不解引用空 Digest，也不把失败包装成成功。Workflow
内部教学日志在 smoke 中被捕获，终端只保留 allowlisted normalized summaries 与 identities。

## D27. Vertex synthesis stays app-owned

`VertexDigestProvider` 位于 app adapter，复用现有 `LLM_*` Vertex-backed LiteLLM gateway 配置，
不 import/修改 `mini_harness_core.providers.RealProvider`。Provider 只负责 structured request/response/candidate；
Harness completion authority、Evidence acceptance 与 Artifact/Result semantics 不变。

## D28. Model selects refs; application restores authority-owned fields

模型输出 summary/content/candidate/content identity/source-ref selection。Canonical URL、accepted Evidence ID、
rank、score/breakdown 与 topic tags 从已选候选的 safe projection 机械补回，然后完整 payload 仍走
与 FakeProvider 相同 contract。补回不等于验收；模型改 ID/order/ref 时 contract 仍拒绝。

## D29. Strict JSON is not repaired inside the adapter

Adapter 不剥 Markdown fence、不从 prose 中搜 JSON、不 sleep/retry。真实 completions 首次返回
fenced JSON 后，修复的是 assistant-prefill prompt，不是放宽 parser。错误只上报 safe taxonomy；
当前 bounded retry 由 application workflow 显式给出一次 retry 与总 deadline，adapter 仍不拥有预算。

## D30. Real Vertex is integration confidence

Fake transport/fixtures 是 correctness gate。Real Vertex 只证明当前 gateway credential、completions protocol、
model 与 prompt shape 可用；模型随机性、quota 和网络都不进入 release correctness 结论。

## D31–D38. Application façade and lifecycle boundary

1. **Application façade is the public business boundary.** CLI/HTTP/UI/tests 调用 `DigestApplication`，不直接
   拼 repository/workflow。
2. **Harness internals are not application API.** Result/Evidence/Artifact/action/audit 只作内部 truth 与
   correlation，公开 contract 只含 application DTO。
3. **Disabled blocks future Runs, not history.** Disable 不删除或改写旧 Digest、Feedback、Profile、Run、
   Delivery。
4. **Subscription updates are versioned.** SQLite CAS 递增 version；Run/Digest 绑定 reservation 时的 snapshot。
5. **Application run identity differs from Harness run identity.** Subscription + idempotency identity 只创建一个
   application run，再单独 durable bind Harness run。
6. **Application recovery defers to Harness truth.** Terminal Result 只修 projection；ambiguous durable events
   fail closed 为 recovery-required，不猜测或创建第二个 Harness run。
7. **External calls never enter long SQLite transactions.** reserve/bind/terminal 分开短事务，Search/LLM/
   notification 在 transaction 外。
8. **Idempotency prevents duplicate logical Runs.** SQLite unique key + CAS claim 保证单实例重复请求不产生
   双 Run、双 Harness identity 或隐式 delivery；近同时请求由 deterministic blocked-search test 证明。

## D39–D43. Thin transport and startup boundary

1. **CLI is transport, not composition.** CLI 只消费 `DigestApplication` DTO 与 bootstrap seam，不 import
   repositories/workflows/Harness internals。
2. **Bootstrap is shared application wiring.** SQLite、adapters、services/workflow 只在 app bootstrap 组装，
   未来 HTTP 复用同一入口。
3. **Provider selection is explicit.** fake/brave、fake/vertex、fake/termux 由 config mode 决定；key presence
   永不触发隐式 real switch。
4. **Readiness is configuration confidence, not liveness.** 不 probe Brave/Vertex、不发送 notification，只报告
   path/schema/config/capability 的 READY/NOT_READY/SET/MISSING。
5. **CLI output is an application projection.** JSON/human output 不含 Harness Result/Evidence/Artifact/audit、
   traceback、raw provider response 或 secret value。

## D44–D48. Admin recovery selects facts, never outcomes

1. **Inspection precedes action.** safe actions 只由 binding、event presence、verified terminal Result 与 app
   projection state 确定性派生。
2. **Admin chooses a proven path, not a state.** 不存在 force completed/failed、assume not applied、rerun anyway
   或 new Harness run action。
3. **Recovery reuses identity.** application/Harness IDs 不变；terminal repair 不执行 Search/LLM/Harness/
   Delivery。
4. **SQLite owns the single recovery claim and minimal audit.** stable operation identity 让 duplicate/concurrent
   request 返回 recovered/already-recovering，不做 distributed lock，也不复制 Harness Audit。
5. **Ambiguity remains blocked.** events without terminal truth、invalid terminal record 或无法证明的 effect
   只暴露 `NO_SAFE_AUTOMATIC_RECOVERY`；admin failure 不覆盖原 Agent Run truth。

## D49–D53. Loopback product transport stays thin

1. **HTTP/UI are DigestApplication clients.** Endpoint 不 import repository/workflow/Harness，UI 不理解
   domain normalization 或 internal identities。
2. **Loopback is a scope boundary.** Server 只接受 `127.0.0.1`；这不是 auth、HTTPS 或 multi-user security。
3. **Synchronous Run is explicit V1 semantics.** 复用 façade 的 bounded synchronous call 与 durable
   idempotency，不为 Demo 发明 queue/background worker。若 latency/parallel workload 成为真实问题再设计 worker。
4. **Transport validation is fail-closed.** 64 KiB body cap、exact JSON fields、CSRF token、HTML escaping、
   safe source URL 与 stable error projection 属于 HTTP safety，不改变业务 truth。
5. **Real HTTP smoke is confidence only.** ephemeral all-fake loopback E2E 是 correctness gate；Brave+Vertex
   HTTP journey 显式 opt-in，网络、quota 与模型随机性不决定 release correctness。

## D54–D56. Terminal status and failure provenance are separate

1. **Status answers outcome; provenance answers cause.** `incomplete` 不变；stage/code 必须由仍拥有上下文的
   workflow 写入，façade 不根据 `TIMEOUT` 等通用名称猜 provider。
2. **Provenance is durable application state.** schema v6 保存 bounded stage/code，使 restart 后 API/UI 归因
   一致；它不复制 raw provider error、Harness Audit 或 traceback。
3. **Legacy ambiguity is preserved.** 旧 run 不回填、不重写；缺 provenance 时显示
   `unknown_stage/legacy_failure`。Search succeeded + Generation failed must never project as search failure.

## D57–D61. Structured output is diagnosed and retried without weakening truth

1. **The current completions gateway is prompt-constrained JSON, not native JSON Schema.** Schema identity 描述
   adapter/parser contract；只有协议与 endpoint 明确迁移到 verified `rawPredict` 时才可宣称 native schema。
2. **Normalization is narrow.** 接受 JSON whitespace 与 completions prefill 唯一缺失的 `{`；拒绝 fence、
   prose、substring guessing 与 truncated JSON。
3. **Generation attempts persist metadata, never content.** schema v7 记录 bounded hashes/lengths/status/finish/
   parser flags/subtype/latency；prompt、raw output、headers、provider envelope 与 secret 不落库。
4. **Retry is application-owned and bounded.** timeout 或 structured parse/schema failure 最多 fresh retry 一次，
   no sleep、60 秒 call timeout、125 秒总 deadline；相同 Evidence/candidates/projections/Output Contract。
5. **Parser success never grants completion.** refs、membership、duplicates、max chars/items 与所有 Artifact/
   Result authority仍由 deterministic Output Contract/Harness 决定。

## D62–D65. Contract diagnostics explain constraints without changing authority

1. **Validator owns the subtype.** Model 不能自报失败原因；现有 deterministic violations 通过固定 priority
   折叠为一个 stable primary subtype。
2. **Status, stage/code and subtype are separate.** rejection 仍是 `incomplete / contract /
   output_contract_failed`；subtype 只回答哪类产品约束未满足。
3. **Diagnostics are bounded projections.** schema v8 仅保存 expected/actual limits、计数与 safe rule identity；
   rejected candidate、Model output、search content 与 validator stack 不落库或 public DTO。
4. **Contract rejection never triggers Provider retry.** Structured parse/schema failure 可用已有 bounded attempt；
   parser success 后的 contract FAIL 直接 authoritative incomplete。旧 row 不推导、不回写 subtype。

上一页：[`10-testing-and-e2e.md`](10-testing-and-e2e.md) · 下一篇：
[`12-review-guide.md`](12-review-guide.md)
