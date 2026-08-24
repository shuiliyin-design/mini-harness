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

Fake transport/fixtures 是 correctness gate。Real Vertex compatibility gate 只证明当前 gateway credential、
显式 native-schema protocol、model 与 request shape 可用；模型随机性、quota 和网络不替代离线 correctness。

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

1. **Requested strict output is not proven enforcement.** 当前 gateway 的 `/v1/chat/completions` 接受 required
   strict tool request，但历史真实 browser input 仍产生 `ITEMS_TYPE`，nested singleton 又被编码成
   string。因此最终 wire 只使用六个顶层 string，并只投影 Harness 已选定的 rank-1 candidate；
   mechanism 标记为 `strict_flat_scalar_tool_requested_prompt_reinforced`。10/10 Real Vertex Provider
   Compatibility Gate 与 3/3 Real Brave + Vertex HTTP Product Integration Journey
   观测仍不能宣称 native guarantee。产品
   readiness 要求显式 `chat-completions`。`completions` 仍是 `prompt_strict_json` compatibility path，不因 key
   presence 自动切换，也不能冒充 Demo-ready。
2. **Normalization is narrow.** 接受 JSON whitespace 与 completions prefill 唯一缺失的 `{`；拒绝 fence、
   prose、substring guessing 与 truncated JSON。
3. **Generation attempts persist metadata, never content.** schema v7 记录 bounded hashes/lengths/status/finish/
   parser flags/subtype/latency；`JSONDecodeError` 只留下六种 allowlisted lexical category 与 line/column；
   prompt、raw output、headers、provider envelope 与 secret 不落库。
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

## D66. Release readiness has three independent gates

Fake stack/fake transport 是 deterministic correctness gate；真实 transport-envelope-shaped Vertex suite 是 provider
compatibility gate；Real Brave + Real Vertex loopback HTTP happy path 是 product integration gate。真实服务不
替代 fake assertions，但没有 compatibility/product happy path 也不能宣称 Web Demo ready。

## D67–D75. Subscription Agent Harness evolution direction

> Slice A/B/C 已实现并通过 deterministic gate；current demo DB 也已显式迁移至 v11。manual worker 与
> Briefing async progress 是 current，Delivery/event eventual consistency 仍是 target。current/planned 明细见
> [`20-subscription-agent-harness.md`](20-subscription-agent-harness.md)。

1. **Application Harness and Agent Harness remain separate.** Application Harness owns conversation、business
   transaction、Subscription/UserSubscription、outbox、product idempotency 与 application recovery；Agent
   Harness owns uncertain model/tool execution、Authority、Evidence、Output Contract 与 Authoritative Result。
   业务名词和 SQLite outbox 不进入 `mini_harness_core`。
2. **Definition Agent has a versioned three-outcome protocol.** `NEXT_QUESTION` 可重复多轮，`REJECT` 是
   durable terminal definition outcome，`DONE` 只携带 structured candidate。UI/HTTP 以 durable conversation
   resource 驱动，不写死追问次数。
3. **Model completion is not product completion.** Agent Harness completed 不能创建 relation；Definition
   candidate 必须再经 deterministic schema、policy、quota、idempotency 与 application commit。
4. **Conversation、Subscription 与 Briefing are orthogonal lifecycles.** `Subscription=ACTIVE` 与
   `Latest Briefing=FAILED` 是合法 truth；Digest/Delivery failure 永不反写 Subscription success。
5. **The activation commit includes the work intent.** Definition、Subscription、UserSubscription、reserved
   first application run、transactional outbox 与 conversation terminal link 在一个 SQLite transaction 中提交。
   COMMIT 后立即宣布订阅成功；Brave/Vertex 绝不进入该 transaction。
6. **Work is at least once; product commits are idempotent.** Conversation、Definition、Subscription、relation、
   outbox、application run、Harness run、Digest 与 Delivery 使用不同 identities。unique keys、CAS、Result/
   Artifact truth 共同提供 exactly-once illusion，但不承诺 distributed exactly-once。
7. **The first worker is a manual tick.** application-owned `run_outbox_once`/`drain_outbox` 先短事务 claim，
   transaction 外执行现有 generation workflow，再短事务 finalize/retry/block；不引入 daemon、scheduler 或
   external queue。
8. **Relation truth precedes downstream projection.** relation event、Updates/Push 与 Delivery failure 只更新
   自己的 outbox/projection/attempt；Subscription 保持 ACTIVE。现有 DeliveryService 的 logical identity、
   pre-dispatch unknown fence 与 no-blind-retry 规则继续适用。
9. **Evolution is incremental.** 先固定 Conversation protocol，再做 activation Unit of Work、outbox、manual
   worker、progress UI 与 Delivery/Event consistency；复用 schema v11 migrations、run recovery、Brave/Vertex、
   Evidence/Contract、Profile/ranking 与 Delivery，而非重写 DigestApplication。

## D76–D81. Phase 3.5 Slice A accepted decisions

1. **Definition protocol belongs to the Application Agent boundary.** Exact protocol v1 只有
   `NEXT_QUESTION {question}`、`REJECT {reason}`、`DONE {definition}`。`REJECT` 是业务 outcome，不是 Agent
   Harness Authority 的 `DENY`；协议/业务词不进入 `mini_harness_core`。
2. **A validated outcome is durable, but not Subscription truth.** DONE 经过 deterministic business validation
   后保存 `DefinitionOutcome` 并令 Conversation 为 `DEFINITION_ACCEPTED`；不创建/启用 Subscription，不运行
   Digest，也不写 outbox。invalid DONE 为 `INCOMPLETE/invalid_candidate`，不自动修 Model 字段。
3. **Durable turns precede uncertain execution.** SQLite v10 只增加 `conversations`、`conversation_turns`、
   `definition_outcomes`。user turn 先提交；表中只存 safe user text、normalized outcome 与 safe errors，不存
   hidden reasoning、raw prompt、raw provider response 或 secret。
4. **Harness Result is the per-turn crash fence.** 每个 turn 预分配独立 `harness_run_id`。Provider 前崩溃重领
   同一 turn；Result 已保存、application outcome 未保存时只投影 existing Result。conversation/message unique
   keys 和 outcome-per-turn unique constraint 收敛 double click 与 crash replay。
5. **Multi-turn is server-owned state.** `WAITING_FOR_ANSWER` 可循环 N 次；UI 不使用 `asked_once`。turn ceiling
   是 application governance（current default 8），命中时明确 `INCOMPLETE/turn_limit_reached`，不伪造 DONE。
6. **Transport stays behind the façade.** HTTP 只能调用 `DigestApplication.start/continue/get_subscription_conversation`；
   public `ConversationView` 不暴露 Result/Evidence/Artifact/Provider/checkpoint。Fake 与 Vertex 使用同一个 adapter
   contract，但 Definition payload 不强行复用 Digest synthesis schema。

## D82–D88. Phase 3.5 Slice B accepted decisions

1. **Only durable accepted DONE may activate.** `commit_subscription_from_definition(user, conversation_id)` 在
   server side定位 latest durable `DefinitionOutcome(type=DONE)`；HTTP exact-empty body不能提交 raw output、
   definition JSON 或自称 validated 的 payload。
2. **One SQLite v11 transaction is the product commit.** `BEGIN IMMEDIATE` 内按顺序写 immutable Definition、
   legacy-compatible Subscription row、Product Subscription companion、UserSubscription、PENDING Briefing
   reservation、`FIRST_BRIEFING_REQUESTED` Outbox 与 activation binding。任一点异常整笔 rollback。
3. **ACTIVE means relation success, not content readiness.** COMMIT 后 Subscription/UserSubscription=`ACTIVE`，
   first Briefing=`PENDING`，此时即可显示“订阅成功，正在准备首篇资讯。”当前没有真实 activation 中间阶段，
   所以不强造 `DRAFT/ACTIVATION_PENDING`。
4. **No Harness identity or external call exists at commit.** Briefing reservation 有独立 `application_run_id`，
   `harness_run_id=NULL`；transaction/service 不持有 Search、Vertex、Delivery 或 Harness execution dependency。
5. **The accepted outcome is the idempotency key.** `definition_outcome_id UNIQUE` activation binding和 SQLite
   serialization使 retry/concurrent double commit 返回同一 Definition/Subscription/relation/run/outbox；不承诺
   distributed exactly-once。
6. **The outbox is deliberately narrow.** v11 只接受 `FIRST_BRIEFING_REQUESTED`；payload 只有 allowlisted refs +
   hash，另存 attempt/available/error/version metadata，不保存 raw request、Definition snapshot、prompt、secret
   或 Harness internals。Slice B 只生产 pending row，不 claim/consume。
7. **Legacy is absence, not invented history.** v10 rows不回填 Definition/relation；companion aggregate不存在时
   public projection标为 `legacy`。新 product ownership以 UserSubscription 为 truth，旧 `Subscription.user_id`
   仅作为兼容 payload；既有 Subscription/Digest 保持可读。

## D89–D96. Phase 3.5 Slice C accepted decisions

1. **Claim authority is SQLite state, not timing.** `BEGIN IMMEDIATE` 选择一个 pending/retry_wait row并以
   status/version CAS 到 claimed；两个 concurrent ticks只有一个 owner。无 lease/fencing，所以 claimed unknown
   fail closed，不按 elapsed time自动执行 Agent。
2. **Work ownership and business outcome are separate.** Outbox内部 lifecycle是
   pending/claimed/retry_wait/completed/failed/blocked；Briefing独立投影 PENDING/RUNNING/READY/INCOMPLETE/FAILED/
   BLOCKED。completed Outbox可对应 authoritative INCOMPLETE/FAILED，因为 event 已处理但内容未 READY。
3. **The Slice B application run is the generation identity.** handler从 canonical refs读取 Subscription、immutable
   Definition、UserSubscription 与 reservation，以 reservation的 `application_run_id` materialize `digest_runs`；
   只在此时创建 Harness binding，不创建第二 logical application run。
4. **Harness owns generation retry.** Search/Provider bounded attempts、Result与execution recovery继续由
   `DigestGenerationWorkflow`/Harness facts决定。Outbox只负责 handoff与transport terminal projection，不形成
   第二套 retry engine。
5. **Digest truth precedes Outbox success.** `finish_digest_run` 先持久化 terminal run与可选 Digest；finalize
   Outbox时再次验证 terminal row，completed run还必须存在匹配 Digest。Digest已 durable的 recovery只 mark
   Outbox success，禁止再次 Search/Vertex。
6. **Ambiguous effects block.** binding后无 event可以相同 Harness ID resume；已有 event但没有 terminal Result
   一律 BLOCKED/recovery_required，不猜 applied/not-applied、不换 Harness ID。
7. **Subscription success never waits for briefing.** commit HTTP继续立即返回 ACTIVE/PENDING；只读 briefing
   endpoint与UI polling分别显示 Subscription和首篇状态，polling不触发 worker。
8. **Operations stay application-owned and manual.** CLI run-once/drain/inspect/recover只经 Application façade；
   本 Slice没有 daemon、scheduler、cloud queue、distributed lease、Delivery outbox或 Harness core 修改。

上一页：[`10-testing-and-e2e.md`](10-testing-and-e2e.md) · 下一篇：
[`12-review-guide.md`](12-review-guide.md)
