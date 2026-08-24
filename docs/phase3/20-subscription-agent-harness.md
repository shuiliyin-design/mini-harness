# Subscription Agent Harness 演进设计

> Phase 3.5 closure 状态分四层：**Implemented in code**：repository 声明 `SCHEMA_VERSION = 13`，Slice A/B/C/D/E
> 已实现 durable Conversation/DefinitionOutcome、atomic product commit、manual durable Outbox worker 与独立
> Briefing projection及 typed relation-event publication；**Verified by deterministic tests**：863 tests PASS；claim、concurrency、
> crash/recovery、unknown fence、HTTP/UI、CLI与Definition reliability均有all-fake证据；**Migrated in current demo DB**：
> `.digest-demo/digest.db` 已显式原位迁移到v13；历史identity/count不变，v13没有为旧 relation伪造 publication intent；
> **Planned for later Slice**：daemon/scheduler、真实 broker、Delivery outbox 与 distributed lease 均未实现。
> **Real async first-Briefing integration smoke**：deterministic validated Definition fixture完成 T0 commit，随后
> manual tick真实调用 Brave/Vertex各 1 次并得到 READY/SUCCEEDED；它是 integration confidence，不替代 offline gate。

## 1. Why this evolution

当前 Digest Demo 已经证明 Search → Evidence → deterministic ranking → LLM candidate → Output Contract
→ authoritative Result → Digest projection，也证明了 versioned Subscription、Delivery/Feedback、恢复、
loopback HTTP/Web UI 与 release evidence gates。Phase 3.5 的教学价值不在横向增加 provider 或工具，而在
分步补齐订阅产品最关键的两个 durable commit point；Slice B 已完成第一个：

```text
subscription committed  !=  first briefing ready
```

Agent `DONE` 只给出 definition candidate。Application 完成 deterministic schema/ownership/idempotency 检查，
并在一个 SQLite transaction 中提交 Definition、Subscription/UserSubscription relation、PENDING
application run 与 outbox 后，订阅才成为 product truth。Brave/Vertex 随后只由 manual worker tick 执行；首篇
Digest 失败、Delivery/Event 失败都不得回滚已成立的关系。

核心 invariant 是：

> Model output is a candidate. Durable application commit makes it product truth.

## 2. Current vs target architecture

### Current worktree and durable demo facts

| Area | Current implementation at HEAD | Consequence |
|---|---|---|
| Definition conversation | `DigestApplication.start/continue/get_subscription_conversation` 调用 application-owned `DefinitionConversationWorkflow`；Vertex复用 Digest provider的 canonical strict-tool envelope/attempt mechanics | 支持 durable多轮与 exact union；provider structured PASS仍不等于 Definition accepted，DONE本身仍不创建 Subscription truth |
| Legacy create | `DigestApplication.create_subscription` 仍调用 `SubscriptionService.create_from_natural_language`，用 regex/defaults 构造旧 `Subscription` | 为兼容保留；尚未接入新 activation commit，不能把 conversation acceptance 等同旧 create |
| Product commit | `DigestApplication.commit_subscription_from_definition` 只按 conversation 定位 durable accepted DONE outcome；`SubscriptionActivationService` 调用一个 `BEGIN IMMEDIATE` Unit of Work | COMMIT 后 `Subscription/UserSubscription=ACTIVE` 且 first Briefing=`PENDING`；Search/LLM/Delivery/Harness calls=0 |
| Persistence | code与current demo DB均为schema v13。v13新增typed relation event outbox/attempt ledger且不回填旧relation | v12→v13 fixture验证历史可读、idempotency与partial-DDL rollback；demo DB原位迁移后identity/count不变 |
| Run | legacy explicit run仍同步；首篇由 `DurableOutboxWorker` 复用 Slice B reserved application run 调用 `DigestGenerationWorkflow.execute_reserved/recovery` | subscription commit HTTP 不等待 Search、Vertex、Harness Result 或 Digest |
| Run durability | workflow 先 `reserve_digest_run`，再 CAS bind Harness identity；`finish_digest_run` 原子写 Digest、seen content 与 run terminal projection | 可直接复用 run identity、snapshot、Result projection与恢复语义 |
| Historical definition | `briefing_reservations` 绑定 immutable Definition ref；v11 `digest_runs` 增加 nullable definition ref，legacy run 仍保留原 `subscription_version/snapshot` | 新 product run 可追溯 Definition version；legacy Digest 不伪造新 ref，且仍不随当前 Subscription update 漂移 |
| HTTP/UI | commit立即返回 ACTIVE/PENDING；`GET /subscriptions/{id}/briefings/latest` 独立读取 progress，页面轮询 PENDING/RUNNING/READY/INCOMPLETE/FAILED/BLOCKED | polling只读，不隐式 tick worker；manual CLI 才推进 work |
| Delivery | `DeliveryService` 以 `(digest_id, channel)` 建稳定逻辑 identity；dispatch 前持久化 `unknown` crash fence；只允许 `failed/not_started` 显式 retry | 可复用 side-effect certainty 与 attempt 机制，但当前是用户显式同步调用，不是 subscription event/outbox consumer |
| Relation event | product COMMIT原子写`USER_SUBSCRIPTION_CREATED`；manual Fake publisher独立claim/attempt/finalize | relation始终是truth；unknown publication fail closed，普通UI不等待或暴露内部outbox |

### Current through Slice E

```text
User / loopback Web
  -> Conversation application API
  -> Definition Agent through Agent Harness
  -> NEXT_QUESTION | REJECT | DONE candidate
  -> Application deterministic validation / ownership / idempotency
  -> one SQLite business transaction
       Definition + Subscription + UserSubscription
       + PENDING Briefing reservation + Outbox + activation binding
  -> COMMIT: subscription is ACTIVE product truth
  -> HTTP: “订阅成功，正在准备首篇资讯”

manual worker tick (current Slice C)
  -> claim Outbox
  -> existing Search / Evidence / Ranking / Vertex / Output Contract
  -> authoritative Harness Result
  -> Digest/Briefing projection
  -> finish Outbox or schedule safe retry/reconciliation
  -> downstream Delivery/Event projection
```

## 3. Application Harness vs Agent Harness

这两层协作，但不合并成一个万能 Harness：

| Layer | Owns | Does not own |
|---|---|---|
| **Application Harness** (`apps/digest_agent`) | conversation continuity、definition schema、Subscription/UserSubscription truth、business transaction、product quota/idempotency、outbox/worker、briefing lifecycle、application recovery、Delivery/Feedback/Profile persistence | Model/tool execution authority、Harness Evidence/Result 语义 |
| **Agent Harness** (`mini_harness_core`) | model/tool execution、context assembly、policy/approval、execution quota/governance/idempotency、Observation acceptance、Evidence、Output Contract、run durability/retry/recovery、Authoritative Result | Feeds/Buddy/Subscription/Conversation/Delivery 等业务 truth，不写业务 relation |

```text
Application Harness orchestrates product truth.
        |
        | invokes and consumes bounded candidate/result
        v
Agent Harness controls uncertain agent execution.
```

Definition Agent 的 Harness `completed` 只证明这次 agent execution 得到符合协议 envelope 的 candidate；
它不等于 definition 合法，更不等于 Subscription 已提交。反向地，Application `ACTIVE` 也不表示首篇
Digest READY。

现有 `mini_harness_core` 不需要 redesign。Conversation repositories、Definition validator、relation、outbox、
worker、briefing projection、Delivery event 都绝对不应进入 core；Brave/Vertex adapters、ranking 与
Digest contract 继续留在 app/integration 边界。

## 4. `NEXT_QUESTION / REJECT / DONE` protocol

### Candidate envelope

Definition Agent 每轮只可提出一个 exact、versioned envelope。Model 不生成任何 durable product ID：

```json
{
  "protocol_version": 1,
  "type": "NEXT_QUESTION",
  "question": "首篇资讯准备好后，需要本地通知吗？"
}
```

```json
{
  "protocol_version": 1,
  "type": "REJECT",
  "reason": "当前只能创建资讯订阅。"
}
```

```json
{
  "protocol_version": 1,
  "type": "DONE",
  "definition": {
    "topic": "AI 行业动态",
    "language": "zh-CN",
    "cadence": "daily",
    "max_chars": 600,
    "max_items": 5,
    "focus_topics": ["Agent", "模型发布", "开发工具"],
    "delivery_preference": "none"
  }
}
```

Slice A 的三种 envelope 都要求 exact fields。Application 对 types、Unicode/control characters、length/range、
enum、secret policy 与 safe user-facing text 做 deterministic validation；`natural_language_request` 的 current
provenance 是 durable safe user turns，不是 DONE 中由 Model 回抄的字段。Agent Harness 的 candidate envelope
gate 与 Application business validation 是两道不同的 gate，均不能由 Model 的自我声明替代。

### Outcome semantics

- `NEXT_QUESTION`：保存 safe DefinitionOutcome，并把 conversation 置为 `WAITING_FOR_ANSWER`；不创建
  Subscription 或 relation。用户回答作为新 durable user turn 后可再次得到 `NEXT_QUESTION`，次数只受明确的
  application governance ceiling 限制，不受 UI 写死的 `asked_once` 限制。
- `REJECT`：保存 durable terminal DefinitionOutcome 与 safe reason；conversation 置为 `REJECTED`，不创建
  relation。它是业务 Agent candidate，不是 Agent Harness `DENY`。重复请求返回同一 terminal view。
- `DONE`：通过 deterministic validation 后保存 durable DefinitionOutcome，并把 conversation 置为
  `DEFINITION_ACCEPTED`；这只表示“definition candidate 已被应用接受”，仍不是 Subscription product truth。
  invalid DONE 转为 `INCOMPLETE/invalid_candidate`，不自动修字段、不保存 accepted outcome。

### Definition validation and versioning

目标 Definition 复用当前 `Subscription.__post_init__` 的确定性约束，不让 Definition Agent 改写规则：

| Field | Initial target rule |
|---|---|
| `topic` | trim 后 1..120 Unicode code points |
| safe user turns | 每轮 1..2000 的用户 provenance；经过 application input policy，不复制到 outbox |
| `language` | 继续使用 application allowlist（当前 `zh-CN`、`en`） |
| `cadence` | 初始仍只接受 `daily`；它是 definition state，不代表 scheduler 已实现 |
| `max_chars` | integer-not-bool，100..4000；继续用 `len(rendered_text)` deterministic enforcement |
| `max_items` | integer-not-bool，1..10 |
| `focus_topics` | 0..10 个 trim、case-insensitive 去重、保序字符串，每项 1..60 |
| `delivery_preference` | 映射到 application allowlist；Model 不能自造 channel |

Current `protocol_version=1`；`DefinitionCandidate.schema_version=1` 在 application domain 内验证，二者概念
不同。Slice B 在 transaction input 外生成 application-owned `definition_id`，以
`(definition_id, definition_version=1)` 保存 immutable snapshot 和 hash，并唯一绑定 conversation/outcome；
Model 不生成 ID/version。首篇和后续
`application_run` 保存当时的 `definition_id + definition_version + exact validated snapshot`。因此未来当前
Definition 更新也不会改变历史 Digest 的 language、limits、focus 或 Delivery provenance。现有
`digest_runs.subscription_version/subscription_snapshot_json` 是这条设计可增量复用的基础。

推荐 HTTP resource，而不是在旧 create endpoint 上塞临时字段：

```text
POST /conversations                    (Idempotency-Key required)
POST /conversations/{id}/messages      (Idempotency-Key required)
GET  /conversations/{id}
GET  /subscriptions/{id}/briefings/latest  (planned)
```

每次 message response 都返回当前 durable `ConversationView`、当前 safe question 或 terminal result。UI 根据
server state 循环渲染同一个 answer form；它不保存轮数上限、不用 local boolean 猜 Agent state，刷新后通过
`GET /conversations/{id}` 恢复。

Application repository 当前保存经过 input policy 的 safe user turns 与 normalized outcomes；每轮送给
Model 的 working context 则由 context assembly 从 durable history 生成 bounded safe projection。二者必须保持
概念和存储边界，不能把 Model 当前窗口或 Session Memory 当作 conversation current truth，也不能为了恢复
把 raw provider prompt/response 落入 application tables。

### Slice A durable resources（current）

- `conversations`：`conversation_id`、`user_id`、`status`、`turn_count`、`version`、create idempotency key、
  safe terminal reason 与 timestamps；`UNIQUE(user_id, start_idempotency_key)`。
- `conversation_turns`：独立 `turn_id`、conversation/turn number、固定 user role、safe user text、message
  idempotency key、独立 `harness_run_id`、processing status、`outcome_id` 与 safe error；conversation 内 message
  key 和 turn number 都唯一。
- `definition_outcomes`：独立 `outcome_id`，对 `turn_id` 唯一，保存 normalized protocol payload、candidate
  identity 与 timestamp。它是 validated definition outcome，不是 Subscription/Definition aggregate table。

每轮先提交 user turn，再 claim 执行。预分配的 `harness_run_id` 和 Agent Harness `ResultStore` 构成 crash
fence：Provider 前崩溃可 claim 同一 turn；Result 已落盘而 outcome 尚未投影时只重放 Result，不再调用 Provider。
同 idempotency key 不同 safe text 返回 conflict。Application DTO 只暴露 conversation status/question/reason/
definition/failure，不暴露 Result、Evidence、Artifact、Provider response 或 checkpoint。

### Slice D structured-protocol reliability（current）

Definition Vertex不再自行解析 HTTP/envelope。它复用 Digest Vertex provider 的 strict required-tool request、
exact one-tool canonical extraction、JSON lexical diagnostics、schema identity、bounded timeout与safe response
metadata。已验证 gateway 对嵌套/union schema约束并不稳定，因此 wire采用两个顶层 scalar：`type` 与
`payload_json`；后者按 type严格解析为 exact `{question}`、`{reason}` 或 `{definition}`，不猜测、不丢弃
字段、不 coercion。随后 application才执行 topic/language/cadence/max_chars/max_items/focus/delivery rules。

顺序必须读作：

```text
provider envelope + wire schema valid
  != Definition Protocol valid
  != Definition business valid
  != Subscription product truth
```

schema v12 `definition_attempts` 以 `(turn_id, attempt_number)` 保存 stable attempt identity、request/schema/
mechanism identity及 allowlisted HTTP/envelope/parse/schema/latency/subtype；只保存已规范化 candidate用于
crash replay，不保存 raw model body、prompt、hidden reasoning或credential。JSON/envelope/schema错误最多两次且
受 125s deadline约束；business validation失败不重试。Harness projection前崩溃重开时复用同一 successful
attempt、同一 turn与同一 Harness run。public failure provenance区分 `definition_generation`、
`protocol_validation`、`definition_validation`，不会写成 briefing generation failure。

2026-08-24 Real Vertex有限 acceptance（无循环求偶然成功）得到：ambiguous `NEXT_QUESTION` → user answer
`DONE`、complete input immediate `DONE`、unsupported `REJECT`。同一 loopback HTTP journey随后 atomic commit为
`Subscription=ACTIVE / First briefing=PENDING / Outbox=PENDING`；Definition Vertex调用4次，而 Briefing Search、
Digest Vertex、Delivery调用均为0，Digest不存在，且没有运行manual Outbox worker。

## 5. Three independent lifecycles

一个大状态机无法表达合法的正交组合，因此保留三个 aggregate lifecycle：

### A. Conversation / Definition

```text
COLLECTING --NEXT_QUESTION--> WAITING_FOR_ANSWER
     ^                              |
     |--------- user answer --------|

COLLECTING --REJECT---------------> REJECTED (terminal)
COLLECTING --valid DONE outcome---> DEFINITION_ACCEPTED (terminal)
COLLECTING --invalid/ceiling------> INCOMPLETE (terminal for Slice A)
```

`DONE` candidate validation 失败不是 `DEFINITION_ACCEPTED`。Slice A 把 invalid candidate、execution incomplete
或 turn ceiling 明确投影为 `INCOMPLETE`；claim/Result crash 则可按同一 logical turn 恢复，不能伪造成 DONE 或
`REJECTED`。下一 slice 读取 accepted outcome 再做 activation，Conversation acceptance 本身不表示 ACTIVE。

### B. Subscription

```text
ACTIVE <-> DISABLED
```

Slice B 直接在一个 transaction 内从 accepted outcome 创建最终 `ACTIVE` aggregate 和 `ACTIVE` relation；
只有 COMMIT 后才能观察到。这里 `ACTIVE` 的精确定义是“用户订阅关系已经成功建立且 future-work intent 已
durable”，**不表示首篇 Briefing READY**。当前没有 transaction 外的 activation step，所以没有强造
`DRAFT/ACTIVATION_PENDING`；未来只有出现真实 durable 中间语义才增加。

### C. Briefing / Digest

```text
PENDING -> RUNNING -> READY
                   -> INCOMPLETE
                   -> FAILED
                   -> BLOCKED
```

Slice B 只提交独立 `briefing_reservations=PENDING` 且 `harness_run_id=NULL`；Slice C 当前消费时以同一个
`application_run_id` 接入现有 generation run；不得在 product commit 中预造 Harness execution。后续 public projection映射 `reserved -> PENDING`、`running -> RUNNING`、
`completed + persisted Digest -> READY`、`incomplete -> INCOMPLETE`、`failed -> FAILED`、
`recovery_required/blocked -> BLOCKED`。`digests` 仍只保存 READY 内容。Outbox state 是 work transport truth，
不是第四个产品大状态机。

合法组合包括：

```text
Conversation=DEFINITION_ACCEPTED
Subscription=ACTIVE
Latest Briefing=FAILED
Delivery=not requested
```

Briefing/Delivery transition 不得写回 Subscription success。

## 6. Product commit boundaries

### Subscription activation commit（Slice B current，最重要）

Exact ordering：

1. 读取 terminal `Conversation=DEFINITION_ACCEPTED` 与其 validated `DONE` DefinitionOutcome；它仍只是 candidate。
2. transaction 外重复 deterministic candidate validation，读取首轮 safe request provenance，并生成六个互不
   复用的 application-owned identities。当前没有已定义的产品 quota，因此不虚构 quota limit。
3. `BEGIN IMMEDIATE`。
4. 重读 conversation owner/status、outcome identity/type/payload；按 `definition_outcome_id` 查询 activation
   unique binding。若已提交，完整读取并返回同一 commit。
5. 写 versioned `SubscriptionDefinition`。
6. 写兼容的旧 `subscriptions` payload row，再写 companion
   `subscription_aggregates(status=ACTIVE, definition version=1)`；二者同 transaction。
7. 写 `UserSubscription(status=ACTIVE)` relation；其 unique identity 是 product relation truth。
8. 写 `briefing_reservations(application_run_id, status=PENDING, definition ref)`；`harness_run_id=NULL`。
9. 写引用该 run/definition/activation identities 的 `FIRST_BRIEFING_REQUESTED` Outbox row；不复制 Definition
   snapshot/raw request，也不顺带实现其他 event type。
10. 写 outcome-to-activation 的唯一 durable binding，使 response loss/replay 可找回相同 resources。
11. `COMMIT`。

COMMIT 成功时，Subscription success 成为 product truth，HTTP 可立即返回：
“订阅成功，正在准备首篇资讯。”若响应丢失，重复 idempotency request 必须读取同一 committed resource。

Brave、Vertex、notification、HTTP callback 与任何其他 external I/O 都不在该 transaction 内。COMMIT 失败时
relation/outbox/application run 一起不存在，不得返回“订阅成功”。

### Briefing commit（Slice C current）

Agent Harness terminal Result 先成立；然后沿用当前 `finish_digest_run` 的短 transaction，把 contract-valid
Digest、seen content 与 application-run terminal projection一起提交。此 commit 产生 `READY` product truth，
但不改变 Subscription。Worker 重读 terminal run 与 Digest 后才以 CAS 将 Outbox 标为 `completed`；Digest
已 durable、Outbox 仍 `claimed` 时只允许 mark-only repair，不再 Search/Vertex。若未来需要自动 Delivery，应在这个 application transaction 中同时插入
`DELIVERY_REQUESTED` outbox intent，而不是在 commit 后 fire-and-forget。

## 7. Minimal SQLite transactional outbox

这是 Slice B 的实际 v11 schema；第一版只表达一种 event，不是 generic event-bus framework：

```sql
CREATE TABLE application_outbox (
    outbox_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL CHECK(event_type = 'FIRST_BRIEFING_REQUESTED'),
    subscription_id TEXT NOT NULL REFERENCES subscription_aggregates(subscription_id),
    application_run_id TEXT NOT NULL UNIQUE
        REFERENCES briefing_reservations(application_run_id),
    payload_ref_json TEXT NOT NULL,
    payload_identity TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'pending', 'claimed', 'retry_wait', 'completed', 'failed', 'blocked'
    )),
    attempt_number INTEGER NOT NULL CHECK(attempt_number >= 0),
    created_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    last_error_code TEXT,
    version INTEGER NOT NULL CHECK(version >= 1),
    updated_at TEXT NOT NULL,
    UNIQUE(event_type, subscription_id)
);
CREATE INDEX application_outbox_ready
    ON application_outbox(status, available_at, created_at);
```

- `payload_ref_json` exact 只存 `activation_id/definition_id/definition_version/application_run_id`；worker 按 ref 从 canonical
  tables 读取 snapshot。它不存 raw Prompt、Model output、Brave/Vertex body、credentials 或 secret。
- `payload_identity` 是 canonical ref 的 SHA-256，用来发现篡改/错误投影，不是新的 aggregate identity。
- `(event_type, subscription_id)` 与 unique application run 收敛第一篇 handoff；真正的 product activation
  idempotency key 是 `definition_outcome_id`，不能互相复用。
- `attempt_number` current 为 0；worker claim 后才递增。`last_error_code` 只能使用 bounded safe taxonomy，不保存
  exception text、traceback 或 provider body。
- Slice C 用 SQLite `BEGIN IMMEDIATE` + row version/status CAS claim 一个 eligible item；`claimed` 是 work
  ownership，不是 Digest 状态。单实例没有 lease/fencing token，因此 claimed-but-owner-unknown 不按时间自动
  reclaim，必须由 durable facts 派生显式 recovery action。
- `completed` 只表示 outbox event 已被确定性处理，不等于 Briefing READY。例如 authoritative generation
  `INCOMPLETE` 可被完整投影后将 outbox 标为 completed；产品状态仍是 INCOMPLETE。

Activation transaction 已写 `FIRST_BRIEFING_REQUESTED`。关系事件若未来有 consumer，也必须写自己的
`SUBSCRIPTION_COMMITTED` outbox row；不能在 COMMIT 后做 best-effort publish。

## 8. Async worker model

Slice C 提供 application-owned `run_outbox_once()` / `drain_outbox(maximum)` 与 CLI
`outbox-run-once`、`outbox-drain --max N`、`outbox-inspect`、`outbox-recover`，不启动 daemon/scheduler：

```text
short transaction:
  select one eligible pending/retry_wait
  BEGIN IMMEDIATE + CAS(status/version) -> claimed
  attempt_number += 1
COMMIT

outside transaction:
  dispatch by allowlisted event_type
  FIRST_BRIEFING_REQUESTED
    -> load Subscription/Definition/UserSubscription/reservation refs
    -> materialize exactly the reserved application_run_id
    -> bind/recover its existing Harness identity
    -> Search / Evidence / Ranking / Vertex / Contract / Result
    -> persist Briefing/Digest outcome

short transaction:
  CAS using outbox_id + claimed version
  -> completed | retry_wait | failed | blocked
```

Worker 不直接信任 payload 中的业务数据，不把 event type 当任意函数名，也不在 claim transaction 内运行
external work。`DurableOutboxWorker` 只接受 `FIRST_BRIEFING_REQUESTED`，通过 Application façade 调用现有
`DigestGenerationWorkflow.execute_reserved/resume_bound_run/recover_projection` seam；CLI 不直接 UPDATE SQLite，
HTTP handler 也不组装或隐式启动 worker。

Retry 只补未完成的 work handoff 或安全可证的 transient failure：

- claim 后、run materialize 前 crash：保持 CLAIMED 并 fail closed；inspection 证明没有开始后才允许显式
  `release_not_started`，随后仍复用同一 `application_run_id`；
- run 已 materialize、Harness 未 bind：只允许同一 reserved run 的 `resume_original_run`；
- bound 且无 Harness events：使用现有 safe recovery；
- terminal Result 已有但 SQLite projection 缺失：只 repair projection；
- events 已开始但没有 terminal Result：outbox `blocked`，等待 reconciliation，不创建第二个 Harness run；
- authoritative `INCOMPLETE/FAILED` 已投影：event 已处理，不用 outbox 重写 Harness truth；新的产品 retry 必须
  是新的明确 request/application run identity。

Retry ownership 分层：Search/Provider 的 bounded attempt 与 Harness execution recovery 继续由
`DigestGenerationWorkflow` 拥有；Outbox 只负责 durable handoff、读取现有 recovery facts 与 transport terminal
projection，不在 terminal INCOMPLETE/FAILED 外再套一层生成 retry engine。

## 9. Idempotency identities

不承诺 distributed exactly-once。目标是 durable idempotency + at-least-once work + idempotent product commit。

| Identity | Scope / uniqueness | Duplicate behavior |
|---|---|---|
| `conversation_id` | 一次 definition conversation；与 user 的 conversation-create idempotency key 建 unique binding | HTTP double click 返回同一 conversation |
| message/turn idempotency | `(conversation_id, client_message_key)`；不是 conversation ID | 重复 answer 不新增 Agent turn |
| `definition_outcome_id` | accepted DONE 与 activation 的唯一业务输入 | duplicate commit/concurrent commit 读取同一 activation binding |
| `definition_id` + `definition_version` | 一个 definition aggregate 的 immutable version | DONE callback 重复读取同一 accepted version |
| `activation_id` | accepted outcome 到六个 product resources 的 binding | response loss 后恢复所有 resource identities |
| `subscription_id` | 订阅 aggregate | 不复用 definition/conversation identity |
| `user_subscription_id` | relation row primary identity；另有 `UNIQUE(user_id, subscription_id)` | relation commit replay 返回 existing |
| `outbox_id` | 一次 durable work/event record | retry 复用 outbox ID，仅 attempt 增长 |
| `application_run_id` | 一次 Briefing product execution | outbox replay/recovery复用；新的用户 retry 才新建 |
| `harness_run_id` | 一次 Agent Harness execution binding | Slice B 为 NULL；Slice C 执行时才生成/绑定，禁止拿 application ID 冒充 |
| `digest_id` | 一份 READY immutable Digest | projection replay用 unique run/artifact/digest binding 去重 |
| `delivery_id` | 一个 logical `(digest_id, channel/user target)` delivery | attempt 重试不换 logical delivery ID |

HTTP conversation idempotency keys、outbox uniqueness/version 与上述 resource IDs 也不能互相复用。commit endpoint
不接受 caller definition 或另造 outcome key；durable outcome ID 本身就是 activation idempotency identity。

典型 failure 的 exactly-once illusion：DONE callback 与双击由 conversation/activation unique keys 收敛；worker
crash 由 outbox claim + existing run recovery 收敛；Digest persist 后 crash 由 terminal projection读取收敛；
Delivery 重复由 logical delivery identity + attempt certainty 收敛。底层执行仍是 at least once，只有 product
commit 对调用方表现为幂等。

## 10. Delivery/event eventual consistency

`UserSubscription=ACTIVE` 是 business truth；`USER_SUBSCRIPTION_CREATED` event、Updates/Push 和 Delivery 是
downstream projection。任一发布/通知失败都保留 Subscription ACTIVE，并通过 retry/outbox/reconciliation
处理。

Slice E current使用独立typed table与manual Fake publisher。relation + event intent同transaction；event_id跨
attempt稳定。publisher前的unknown-effect fence使timeout或accepted后落库前crash只能BLOCKED，明确not-applied
failure才可retry。它与`FIRST_BRIEFING_REQUESTED`拥有不同claim query、service与CLI，互不覆盖。

当前 `DeliveryService` 可直接复用：

- `(digest_id, channel)` logical idempotency；
- `DeliveryRecord` 与独立 attempts；
- dispatch 前 `unknown/unknown` crash fence；
- 只有 `failed/not_started` 才能 safe retry；unknown 禁止 blind resend；
- delivery status 从不反写 completed generation。

需要扩展而不是重写：Delivery 目前由 `POST /digests/{id}/deliver` 同步触发，且 reservation 与 briefing
READY commit 分离。目标 automatic delivery 应由 READY transaction 同时写 `DELIVERY_REQUESTED` outbox，
worker 再调用 DeliveryService。若 dispatch 已发生但 terminal persistence 失败，保留 Delivery unknown、
outbox blocked/reconciliation-required；绝不能重发或回滚 Subscription/Digest。关系事件 publisher 也使用
同类outbox原则但拥有独立table、event type/handler/idempotency key，不能借 notification accepted 冒充
relation event published。

## 11. Crash/recovery matrix

| Crash / duplicate point | Durable fact | Safe action | Must not do |
|---|---|---|---|
| DONE/commit callback duplicate | definition outcome/activation unique binding | 返回同一 SubscriptionCommitView | 创建第二个 relation |
| HTTP answer/create double click | message/create idempotency binding | 返回同一 conversation/turn | 再调用 Model |
| activation transaction before COMMIT | 无完整 relation/outbox truth（SQLite rollback） | 同 key 重试整个 transaction | 宣称订阅成功 |
| COMMIT 后 response lost | ACTIVE relation + outbox 同在 | 查询并返回 existing success | 新建 Subscription |
| worker claim 后、generation 前 crash | CLAIMED outbox，run 尚未 materialize | 普通 tick fail closed；inspection 后显式 release | 按时间自动 reclaim |
| reserved run materialize 后、Harness bind 前 crash | CLAIMED outbox + 同一个 reserved application run | `resume_original_run`，复用 application identity | 新建 run |
| Harness bind 后、无 event crash | existing bound/no-event facts | 复用现有 safe resume path | 换 Harness ID |
| Harness events、无 terminal Result | effect ambiguous | outbox/briefing BLOCKED，reconcile | 猜 failed 或重新生成 |
| terminal Result、Digest commit 前 crash | immutable Result/Artifact | repair SQLite projection only | 重跑 Brave/Vertex |
| Digest commit 后、outbox finish 前 crash | READY Digest + run terminal + CLAIMED outbox | mark-only completed | Search、Vertex 或创建第二个 Digest |
| Outbox completed 后、HTTP/UI read 前 crash | READY Digest + completed outbox | GET/polling重读 durable truth | 再执行 worker |
| relation + two intents COMMIT 后 crash | ACTIVE relation + 两条PENDING promise | 各自manual tick | 回滚relation或丢event |
| relation event claim 后、publish 前 crash | claimed event + prepared/not_started attempt | inspection后`release_not_started` | 按时间自动reclaim |
| publisher accepted、success落库前 crash | claimed event + unknown-effect attempt | `block_unknown`等待reconciliation | blind retry或换event_id |
| relation event success后crash | completed event + ACTIVE relation | 重读durable truth | republish |
| publisher explicit not-applied failure | retry_wait event + ACTIVE relation | manual next attempt | disable relation或影响Briefing |
| authoritative INCOMPLETE/FAILED | terminal run projection | outbox event completed；显示真实 briefing state | 自动覆盖 terminal truth |
| Delivery dispatch 前 adapter 明确 not started | failed/not_started | bounded explicit/outbox retry attempt N+1 | 回滚 ACTIVE relation |
| Delivery dispatch 后 crash/terminal write failure | unknown certainty | BLOCKED + reconciliation | blind resend |
| relation event publish failure | ACTIVE relation + pending/retry outbox | retry publisher/reconcile | 删除/disable Subscription |

## 12. Current worktree gap analysis

下表只描述已通过 deterministic gate 的 Slice A/B/C code 与已迁移 demo DB，不把后续 target 写成现状：

| Gap | Current evidence | Target delta |
|---|---|---|
| Conversation protocol | **Slice A 已补齐** strict v1 protocol、durable turns/outcomes、restart recovery、façade/HTTP/UI loop | activation 只消费 accepted DONE outcome，不绕回 Provider |
| Subscription commit boundary | **Slice B 已补齐** Definition + compatible Subscription row + aggregate + relation + briefing reservation + outbox + binding one transaction | worker outcome不得反写 ACTIVE success |
| Outbox | v11引入的Briefing outbox与v13 relation-event outbox均有atomic claim/finalize、bounded inspect/recover；typed payload只有safe refs/projection | distributed lease/fencing不是本 Slice目标 |
| Async worker | manual single-process tick复用 reserved run与现有 generation/recovery；并发 tick只有一个 claim owner | daemon/scheduler/background polling未实现 |
| UI progress | latest briefing endpoint与页面 polling独立显示 Subscription success 和 briefing state | polling不自动推进 worker |
| Lifecycle/state | ACTIVE Subscription 与 PENDING/RUNNING/READY/INCOMPLETE/FAILED/BLOCKED Briefing已正交投影 | Delivery仍是独立显式操作 |
| Idempotency | outbox/application/Harness/Digest identities分离；replay/concurrency与 Digest mark-only repair有 deterministic test | 不宣称 distributed exactly-once |
| Delivery/event consistency | relation creation transaction已原子写独立 `USER_SUBSCRIPTION_CREATED` intent；manual Fake publisher支持accepted/explicit failure/unknown | Delivery仍为显式同步操作；没有 Delivery outbox或真实 broker |
| Recovery | Briefing与relation event各有typed inspection；relation publish未开始可release，effect unknown只能BLOCKED | 第一版无 broker reconciliation API，不允许force retry |

### Reuse / evolve / add

- **直接复用**：`DigestApplication` public boundary、Slice A 的 `DefinitionConversationWorkflow`、strict protocol
  validator、Fake/Vertex Definition adapters、SQLite v11 repositories、Slice B `SubscriptionActivationService` 与
  Unit of Work、`DigestGenerationWorkflow` 的
  reserve/bind/execute/recover、migration pattern、Subscription deterministic field validation、run snapshot、Brave adapter、Vertex
  provider、Evidence/Output Contract、Profile/ranking、Digest projection、Delivery attempt certainty、admin recovery。
- **重命名/扩展**：Slice B 没有重命名旧 `Subscription`，而是以 companion `SubscriptionDefinition`、
  `subscription_aggregates` 与 `UserSubscription` 承载新 product truth；legacy absence 明确投影。`RunView` 后续
  已增加 Briefing public projection；同步 Run保留为显式 manual generation use case，首篇 generation由 worker
  消费 reserved run。未来 DeliveryService 若接 outbox，不改变 adapter certainty semantics。
- **新的 application abstractions**：Slice A conversation/protocol 和 Slice B `SubscriptionActivationService`、
  typed product records/transactional repository、`DurableOutboxWorker`、typed first-briefing handler 与最小
  `FirstBriefingView` 已存在，且都属于 `apps/digest_agent`。
- **绝不进入 `mini_harness_core`**：Conversation/Definition/Subscription/UserSubscription、SQLite business
  transaction/outbox schema、cadence/quota product rule、Briefing/Digest lifecycle、Delivery/Feedback/Profile、
  HTTP route/UI state与 product idempotency mapping。

## 13. Incremental implementation plan

每个 slice 都增加离线 tests，并保持旧 application path 可迁移；不要一次实现全部：

1. **Slice A — Conversation protocol（已实现）**：strict versioned candidate validator、durable
   conversation/turn/outcome、Fake/Vertex Definition Agent adapters、façade/HTTP resource；离线证明连续多次
   NEXT_QUESTION、restart recovery、REJECT terminal、invalid DONE 与 DONE 不创建 product truth。
2. **Slice B — Subscription commit + transactional outbox（已实现）**：引入 versioned Definition、
   Subscription/UserSubscription/activation/outbox identities与 application Unit of Work；同一 transaction 提交
   relation、reserved first run 与 `FIRST_BRIEFING_REQUESTED`。证明 duplicate activation、HTTP response loss和
   transaction fault 只得到一个 relation，且不会出现 relation-without-work-intent。暂不消费 outbox。
3. **Slice C — Manual async worker（已实现）**：SQLite CAS claim、typed dispatch、existing workflow/recovery、
   Briefing DTO/polling与admin CLI；HTTP activation不等待 external services。无 daemon、scheduler或时间 lease。
4. **Slice D — Definition structured reliability（已实现）**：复用 canonical strict-tool envelope/attempt
   mechanics，并保持 provider validity、Definition validity和product truth三层分离。
5. **Slice E — Relation event eventual consistency（已实现）**：relation transaction写独立typed intent；
   manual Fake publisher、attempt ledger与unknown fence证明publication failure不反写relation。
6. **后续 Slice — Delivery outbox（未实现）**：若需要，把 READY Digest 的delivery intent作为另一种typed
   promise设计；不得把relation-created event当作Digest delivery。

Slice A/B/C/D/E 已固定多轮协议、product commit、manual generation与relation publication边界。后续不得把
polling变成隐式 worker，也不得用时间猜测 CLAIMED owner 已死亡。

## 14. Worked user journey

```text
User: 帮我订阅 AI 行业动态，每次 600 字以内，重点关注 Agent、模型发布和开发工具。

Agent candidate: NEXT_QUESTION
  “你希望资讯统一用中文撰写吗？”
Application: persist safe turn; Conversation=WAITING_FOR_ANSWER

User: 是的。
Agent candidate: NEXT_QUESTION
  “首篇资讯准备好后，需要本地通知吗？”
Application: persist safe turn; Conversation=WAITING_FOR_ANSWER

User: 暂时不用。
Agent candidate: DONE {definition candidate}
Slice A current: deterministic schema validation
  -> persist DefinitionOutcome
  -> Conversation=DEFINITION_ACCEPTED
  -> HTTP/UI 明示尚未创建 Subscription

Slice B current activation transaction:
  policy/quota/idempotency checks
  Definition(v1) + Subscription(ACTIVE) + UserSubscription(ACTIVE)
  + ApplicationRun(PENDING, definition snapshot) + Outbox
COMMIT

HTTP/UI: 订阅成功，正在准备首篇资讯。

T0 — Subscription COMMIT:
  UI = Subscription Successful / First briefing PENDING
  Digest absent; Search/Vertex calls = 0

T1 — manual worker tick:
  claim outbox -> bind existing Harness run

T2 — Agent generation:
  -> Brave Observation -> accepted Evidence -> deterministic Ranking
  -> Vertex candidate -> Output Contract -> Authoritative Result

T3 — Briefing product projection:
  -> Digest READY (durable)

T4 — work transport projection:
  -> Outbox SUCCEEDED/completed

UI polling: Subscription=ACTIVE; Latest Briefing=READY
Delivery worker (if requested): accepted | failed | unknown
  Regardless: Subscription remains ACTIVE.
```

如果 Model 返回 `REJECT`，conversation durable terminal 且没有 Subscription。如果 generation 失败，UI 显示
`Subscription=ACTIVE, Latest Briefing=FAILED/INCOMPLETE`，用户关系仍成立。

## 15. Design lessons from production

1. **Protocol capability必须与 UI state machine一致。** Agent 能多轮，不代表一个 textarea + 一次 response
   就支持多轮；conversation 必须成为 server-owned durable resource。
2. **Fire-and-forget 不跨 durable boundary。** relation commit 后再 best-effort trigger 会留下永久漏单；
   business facts 与 outbox intent必须同 transaction。
3. **Non-blocking downstream不等于无状态。** event/push失败不阻塞 relation commit，但必须有 durable retry、
   effect certainty与 reconciliation；不能通过 rollback relation伪造一致性。

这些是抽象架构原则；本文不复制任何生产系统私有 schema、接口或基础设施实现。

## 16. Slice E worked trace：business truth 与 publication projection

```text
T0  SQLite business transaction COMMIT
    UserSubscription=ACTIVE
    FIRST_BRIEFING_REQUESTED=PENDING
    USER_SUBSCRIPTION_CREATED=PENDING

T1  HTTP/UI 立即显示 subscribed / first briefing preparing
    不等待 relation publisher，也不显示内部 outbox 为“订阅失败”

T2a manual briefing tick（独立状态机）
     -> READY | INCOMPLETE | FAILED | BLOCKED

T2b manual relation-events tick（独立状态机）
     -> SUCCEEDED | RETRYABLE | UNKNOWN/BLOCKED
```

relation event identity由 `USER_SUBSCRIPTION_CREATED + user_subscription_id + relation_version`
确定性派生；attempt identity另由 `event_id + attempt_number` 派生。publisher调用前先把attempt持久化为
effect `unknown`：accepted后、success落库前crash因此只能进入manual reconciliation，不能blind retry。
显式 `not applied` failure可以创建下一attempt，但logical event identity不变。两条outbox promise可独立
成功、失败或阻塞；任何publication结果都不更新或删除UserSubscription。

## 16. Non-goals

本阶段不引入 MySQL、Redis、Kafka/PubSub、Celery、scheduler、daemon、cloud deployment、auth/payment、
multi-tenant security、新 Search/LLM provider、GUI automation 或 Harness core redesign。SQLite + loopback HTTP
+ current Brave/Vertex 继续模拟产品链路。也不承诺 distributed exactly-once、HA、跨进程 lease、自动解决
unknown side effect，或把 Model candidate 当 product truth。

返回：[`README.md`](README.md) · 现有 façade：
[`15-application-facade-and-run-lifecycle.md`](15-application-facade-and-run-lifecycle.md) · 当前 HTTP/UI：
[`18-loopback-http-and-web-ui.md`](18-loopback-http-and-web-ui.md)
