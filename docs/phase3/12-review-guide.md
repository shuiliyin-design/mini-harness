# Phase 3 Review Guide

## 1. Ten-minute product review

按顺序读：

1. [`00-overview.md`](00-overview.md) 的全链路与 tree；
2. [`01-product-scope.md`](01-product-scope.md) 的 V1/out-of-scope；
3. [`03-subscription-schema.md`](03-subscription-schema.md) 的一等 constraints；
4. [`06-output-contracts.md`](06-output-contracts.md) 的 deterministic/semantic split；
5. [`09-failure-and-recovery.md`](09-failure-and-recovery.md) 的 matrix。
6. [`15-application-facade-and-run-lifecycle.md`](15-application-facade-and-run-lifecycle.md) 的 public DTO、
   idempotency 与 recovery truth table。
7. [`16-cli-bootstrap-and-readiness.md`](16-cli-bootstrap-and-readiness.md) 的 transport/bootstrap/readiness boundary。
8. [`17-application-admin-recovery.md`](17-application-admin-recovery.md) 的 durable fact/action truth table。
9. [`19-llm-structured-output-reliability.md`](19-llm-structured-output-reliability.md) 的 JSON parser、attempt
   privacy boundary 与 retry budget。
10. [`20-subscription-agent-harness.md`](20-subscription-agent-harness.md) 的 Slice A/B current implementation、
    product commit ordering、后续 worker target 与 repository gap analysis；注意 current/planned 标记。

当前 release 验收问题：用户能否从自然语言开始，手动得到有 sources 的 bounded Digest，反馈后明确改变
下一次排序；且没有 scheduler、auth、vector DB 或真实网络 test dependency？Phase 3.5 Slice A/B 另证明
durable conversation 与 atomic product commit；不能用 pending outbox冒充 worker/async generation 已完成。

## 2. Boundary review

```text
apps/digest_agent -> Harness façade / integrations
mini_harness_core -X-> apps
Bridge transport  -X-> app domain/environment authority
Environment       -X-> Subscription/Profile/Result decisions
Model             -X-> persistence/ranking/contract/delivery truth
```

检查未来源码：domain 是否 import sqlite/HTTP/Harness；Brave 是否只在 adapter；CRUD 是否误用 Agent
Run；application 是否自己伪造 Evidence/Artifact/Result；delivery 是否绕过 authorized dispatch。

## 3. Data review

- Subscription 是否保存 `max_chars/max_items` 并严格校验 int-not-bool？
- Digest 是否绑定 subscription version、period key、Harness run/result/artifact identity？
- SourceRef 是否只能引用 selected candidate 与 accepted current-run evidence？
- Profile 是否能由 Interaction + rule version 重算？
- SQLite transaction/unique keys 是否让 Result projection 与 Feedback 幂等？
- API key/raw response 是否可能进入 DB、Session、Evidence、Artifact、logs？
- Brave 空 topic tags 是否只经 bounded lexical rule 派生，而非因 HTTP 200 自动获得相关性？
- Vertex prompt 是否只有 Subscription/candidates/Evidence/Profile safe projections，排除 raw request、
  Interaction history、raw Brave、secret 与 hidden Harness state？

## 4. Authority and failure review

- Search server metadata 是否被错误当成 local policy/effect？
- Model 是否能宣称字符数、排序、source validity 或 completed？
- Vertex malformed/fenced/empty/refusal 是否 fail closed；invalid ref/duplicate/too-long 是否仍由 contract 拒绝？
- no results 是否诚实 `incomplete`，而非空成功？
- Search 成功但 Digest incomplete 时，smoke 是否安全返回 non-zero 而不访问空 projection？
- generation completed + delivery failed 是否保留两个 truth？
- side-effecting notification unknown 是否禁止 blind resend？
- SQLite projection failure 是否只重试 projection，不重跑 Search/LLM？
- façade 是否隐藏 Harness Result/Evidence/Artifact/action/audit 与 SQLite row？
- reserved/running 重复请求是否只复用 identity；ambiguous recovery 是否 fail closed？
- 两个近同时相同 idempotency request 是否只有一条 run、一个 Harness ID 与一次 external workflow？
- CLI 是否只 import façade/bootstrap，readiness 是否完全不调用 external service？
- 有 key 时 fake mode 是否仍保持 fake，readiness/CLI 是否只报告 presence 而不打印 value？
- recovery inspection 是否只从 durable facts 派生 allowlist，projection repair 是否保证 external call count=0？
- concurrent admin recovery 是否只有一个 owner；audit 是否不复制 Harness Result/Evidence/Audit payload？
- HTTP endpoint 是否只调用 DigestApplication；JSON/HTML 是否完全没有 Harness identity/internal schema？
- server 是否拒绝非 `127.0.0.1` bind；mutation 是否有 CSRF、body cap 与 exact field validation？
- `/ready` 是否仍无 external I/O；unexpected error 是否只返回 stable safe code 而无 traceback/secret？
- application run 是否 durable 区分 status 与 failure stage/code；是否还会用通用 `TIMEOUT` 猜 Search？
- Search accepted Evidence 后的 Vertex failure 是否投影 generation；legacy NULL provenance 是否保持 unknown？
- real Vertex product mode 是否把 `chat-completions/strict_flat_scalar_tool_requested_prompt_reinforced` 与
  enforcement proof 分开；wire 是否只接收 Harness rank-1 projection，canonical lists/refs 是否由本地
  确定性重建；legacy completions 是否
  仍诚实标为 prompt strict JSON 且 readiness fail closed？
- generation attempt 是否只保存 safe metadata；fence/prose/truncation 是否仍 fail closed？
- JSON syntax 是否只保存 allowlisted lexical subtype/line/column，历史未知是否保持未知而不猜？
- bounded retry 是否最多一次、复用相同 accepted inputs，且最终仍经过完整 Output Contract？
- contract subtype 是否只由 validator violation 产生，并与 status/stage/code 分离？
- contract diagnostics 是否只有 limits/counts/rule identity，且 rejection 保证 Provider calls=1？
- 旧 `output_contract_failed` row 是否保持 generic，而不是从历史 reason 猜 subtype？
- Offline Deterministic Correctness Gate、Real Vertex Provider Compatibility Gate、Real Brave + Vertex HTTP
  Product Integration Journey、Manual Mobile Browser Acceptance 是否分别有相符证据？Automated
  Browser-Engine E2E 是否诚实标为 NOT IMPLEMENTED / NOT RUN，而不是借用 HTTP 或人工证据？

## 5. Test review

第一条 slice 的三条 E2E 覆盖 valid、overlong、invalid source；当前 feedback slice 再覆盖 empty
profile、liked 上升、dismissed 下降三条 E2E。Real Brave slice 另有 16 条、Real Vertex slice 另有
13 条离线 adapter/contract/workflow 测试；真实 Brave/Vertex/Termux smoke 必须 opt-in。
Architecture test 应阻止 core→apps、domain→infrastructure，并证明当前 slice 无网络/API；Termux
mapping 必须依赖注入的 authorized Environment dispatcher，不能直接执行设备命令。
Loopback HTTP tests 使用真实 ephemeral server，但只接 all-fake application bootstrap；Product E2E 不得
直接调用 repository 制造 Subscription、Run、Digest、Feedback/Profile 或 Delivery 状态。

## 6. Core-change gate

若实现者认为必须修改 Harness core，先回答：

1. 现有 `MCPClient/MCPRegistry`、workspace Artifact 或 integration seam 为什么无法表达？
2. 需求是 Digest-specific，还是至少两个独立应用共有？
3. 新 schema 如何保持旧 Result/Bundle/Replay compatibility？
4. 是否可以先在 app adapter/workflow 中实现而不削弱 Authority？

当前实现答案是：**Fake/Real Brave Search 与 Fake/Real Vertex synthesis fixed workflow 都不需要
修改 core。只有未来自主多轮搜索
才重新评估 post-observation acceptor；historical schema、Authority model 与 Artifact/Result
semantics 均无需改变。**

## 7. Subscription Agent Harness design review

实现/评审每个 Subscription Agent Harness slice 时逐项检查：

### Protocol / UI

- `NEXT_QUESTION` 是否能连续出现两次以上，refresh/restart 后仍从 durable Conversation 恢复？
- HTTP/UI 是否只读取 server-owned conversation state，而不是 `asked_once`、前端轮数或临时 local state？
- `REJECT` 是否 terminal 且不创建 relation，并与 Harness `DENY` 明确区分？`DONE` 是否先经过 Application
  validation，只产生 accepted DefinitionOutcome 而不创建 Subscription truth？
- Model 生成的 question/reason/definition 是否经过 exact schema 与 safe projection，且不生成 durable IDs？
- user turn 是否先 durable；claim 后、Provider 前 crash 与 Result 后、outcome 前 crash 是否复用同一 turn/
  Harness identity并避免重复 Provider call？
- Definition Vertex是否走与Digest相同的strict-tool/canonical envelope seam；是否先验provider wire、再验exact
  variant、最后验Definition business rules？任何一层是否被错误写成Subscription success？
- retry是否只覆盖allowlisted malformed envelope/JSON/schema，且复用同一turn/attempt lineage；business-invalid
  DONE是否明确不重试？attempt ledger是否排除raw prompt/model正文/credential？

### Commit / lifecycle

- accepted DefinitionOutcome、Definition、Subscription/UserSubscription relation、reserved first application run、
  Outbox 与 activation binding 是否在同一个 SQLite transaction？
- commit input 是否只能由 server side读取 accepted DONE outcome，HTTP body是否无法塞入 caller-crafted definition？
- 若产品定义 quota，是否在 transaction 内基于 current durable rows 重查（Slice B 当前未定义 quota，不能
  虚构 limit）？COMMIT 前是否绝不返回 subscription success？
- transaction 内是否完全没有 Brave、Vertex、notification 或其他 external I/O？
- 是否可同时观察 `Subscription=ACTIVE` 与 `Briefing=FAILED/INCOMPLETE/BLOCKED`，且后者不更新前者？
- historical Digest 是否仍绑定 reservation 时的 definition ID/version/snapshot？
- `ACTIVE` 是否明确只表示 relation + work intent durable，而不是 Briefing READY；commit时 `harness_run_id` 是否
  为 NULL、且只在 worker materialize同一 reserved application run时绑定？

### Outbox / worker / idempotency

- Outbox payload 是否只有 allowlisted identity/ref + hash，不含 raw Prompt、Model/provider body 或 secret？
- claim/finalize 是否各为短 transaction，external work 是否在其外；无 lease/fencing时 claimed unknown是否
  fail closed而非按 timestamp自动 reclaim？
- DONE callback、HTTP double click、worker crash、Digest-commit-before-outbox-finish 是否都收敛到 existing identity？
- conversation、definition、subscription、relation、outbox、application run、Harness run、Digest、Delivery IDs
  是否分列且不复用？文档/代码是否避免宣称 distributed exactly-once？
- Harness events without terminal Result 是否保持 BLOCKED/reconciliation，而不是换 ID 重跑？
- Digest已 durable而Outbox未 succeeded时是否只 mark completed，并断言 Search/Provider/Harness/Digest均不重复？
- CLI是否只经 Application façade；HTTP commit与只读 polling是否都不会隐式执行 worker？
- Search/Provider retry是否仍由 generation workflow拥有，而Outbox不形成双重 retry engine？

### Delivery / event consistency

- `UserSubscription=ACTIVE` 是否是 relation truth；event/push/delivery 是否只是 downstream projection？
- READY 后的 automatic delivery intent 是否与 READY application projection一起 commit，而非 fire-and-forget？
- `DeliveryService` 的 `unknown` 是否仍禁止 blind retry；failure/unknown 是否不回滚 Subscription/Digest？
- relation event 与 delivery 是否使用不同 typed event/identity，而不是用一次 publish/notification 代表全部？
- relation insert与`USER_SUBSCRIPTION_CREATED` intent是否在同一transaction，且fault matrix证明不存在
  relation-without-intent或intent-without-relation？旧relation是否明确不做虚构backfill？
- event_id是否跨publication attempts稳定，attempt identity/certainty是否独立保存；payload是否只有最小refs，
  不含conversation/Definition/Prompt/Evidence/Harness/credential/Profile？
- publisher调用前是否有durable unknown-effect fence；accepted后success落库前crash与timeout unknown是否都
  fail closed，禁止按时间或blind retry？只有明确not-applied failure是否允许下一attempt？
- relation publisher是否只经Application façade/manual CLI，且从不调用Agent Harness/Search/Vertex；普通用户UI
  是否仍立即显示ACTIVE而不暴露内部publication failure？
- `FIRST_BRIEFING_REQUESTED`与relation event是否由不同typed handler独立claim/finalize，使任一失败或阻塞均不
  覆盖另一状态机？

### Repository truth gate

- 所有 “current” 是否可由 `apps/digest_agent`、SQLite migration 或 tests 直接支持？
- Conversation/DefinitionOutcome、v12 Definition attempt reliability、v11 product commit、Briefing outbox worker及
  v13 relation-event outbox/publisher是否都有current
  deterministic tests；daemon/scheduler/Delivery outbox是否始终标为 target/not implemented？
- 是否只增加 application abstractions；`mini_harness_core` 是否仍不含订阅业务名词？
- 每个 slice 是否有 all-fake offline tests，并继续通过 `python -m unittest -q` 与 `git diff --check`？

上一页：[`11-design-decisions.md`](11-design-decisions.md) · 下一篇：
[`13-first-vertical-slice.md`](13-first-vertical-slice.md)
