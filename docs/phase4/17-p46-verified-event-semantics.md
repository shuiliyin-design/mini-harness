# P4.6 — Verified EVENT Semantics

## 1. 范围与事实基线

P4.6 的唯一用户需求是：

> OpenAI 发布新模型时告诉我。

本节保留实现前的设计基线：当时 repository 已能形成 `OpenAI 新模型发布 / 出现新模型时提醒` candidate，但所有 EVENT
仍在 commit 前 fail closed。P4.3 已提供 Update/Distribution，P4.4 已提供 temporal semantics，P4.5 已提供
Distribution-bound Notification。P4.6 后续已按本文 exact selector 与 authority boundary 落地；当前实现事实与验证证据见
[`18-p46-implementation-status.md`](18-p46-implementation-status.md)。

本文定义第一条窄 EVENT vertical slice 的 Product/Application/Harness 语义。实现使用 Fake source/candidate，不调用 Brave Search
或 Vertex，也不修改 `mini_harness_core`；不建设 generic event ontology、RAG framework、vector DB、通用 scheduler 或第二套 delivery。

## 2. 最小 EVENT Definition 与 policy

V1 只支持一个 exact selector shape：

```json
{
  "schema_version": 1,
  "subject": "OpenAI 新模型发布",
  "signal": {
    "kind": "EVENT",
    "criterion": {
      "entity": {"kind": "ORGANIZATION", "key": "openai", "name": "OpenAI"},
      "event_type": "MODEL_RELEASED",
      "constraints": {
        "object_type": "MODEL",
        "release_scope": "PUBLIC_AVAILABILITY"
      }
    }
  },
  "temporal_scope": {"mode": "FUTURE_FROM_ACTIVATION", "end_at": null},
  "provenance": {}
}
```

这些字段的产品含义是：

- `entity` 必须明确且规范化为 OpenAI；只提“AI 公司”或多个主体需要澄清，不能由 Model 随意选一个。
- `event_type=MODEL_RELEASED` 只表示 OpenAI 已公开发布并表示当前可用的具名模型。传闻、泄露、benchmark、价格变化、
  SDK 更新、模型退役、单纯“coming soon”或第三方产品使用某模型都不属于这个 event type。
- `constraints` 是上述窄 criterion，不是 generic predicate/ontology。V1 不区分所有模型 taxonomy，也不支持用户自定义事件 DSL。
- `temporal_scope` 默认从 Subscription activation 时刻开始、无自动结束时间；激活前的历史发布不产生首轮“补发”。显式的
  任意日期范围在 V1 尚不支持时必须澄清或 fail closed，不能静默丢弃。
- entity、event type 与用户明确表达的约束必须保留 `USER_EXPLICIT/USER_CONFIRMED` provenance；
  `FUTURE_FROM_ACTIVATION` 是在 confirmation 中可见的 `PRODUCT_DEFAULT`。

Cadence、observation source、verification policy version、presentation 和 notification 都不属于 Tracking Definition truth：

- EVENT V1 cadence 默认 `6h / PRODUCT_DEFAULT`，允许用户明确覆盖 `1h / 6h / 12h / 24h`；复用 P4.4 allowlist、anchor、
  Fake Clock 与 provenance 规则，不接受任意 cron 或 Model 自报时间。
- source policy 初版是 `fake_event_search`；以后 real smoke 才可换成 authorized Brave Search + Vertex detector。
- distribution 默认 `feed_only / PRODUCT_DEFAULT`。只有用户明确确认“本机通知”才是 `termux_notification`；“告诉我”表达
  Update 时机，不足以授权某个 Environment channel。

只有上述完整、validated shape 才能选择 `EVENT`。其他 entity、event type、复合 intent、未知 constraint 或 unsupported EVENT
继续 fail closed；绝不能 fallback BRIEFING，也不能信任 Model 自报 `workflow_kind=EVENT`。

## 3. 事实链与各层 identity

目标链路是：

```text
EVENT Observation cycle
  -> accepted typed Source Observation + Observation Evidence
  -> Agent Event Candidate(s)
  -> deterministic Evidence/verification gate
  -> Verification outcome
       VERIFIED              -> logical Verified Event -> EVENT Update -> Distribution
       NO_UPDATE             -> no product Update
       VERIFICATION_INCOMPLETE -> no product Update; cycle remains diagnosable
  -> existing P4.5 Notification eligibility/delivery
```

以下对象不能合并：

| Object | 回答的问题 | Stable identity owner |
|---|---|---|
| Subscription | 用户确认了哪个持续目标 | existing product identity |
| Observation cycle | 哪个 scheduled/coalesced slot 被执行 | Application；subscription + policy version + due + kind |
| Source Observation | 本次 source adapter 实际返回什么 | Application adapter；query/window + normalized results fingerprint |
| Observation Evidence | 哪份 immutable source payload 被接受 | Evidence store |
| Event Candidate | Agent 从哪次 Observation 提出了什么候选 | canonical candidate payload + observation/run binding；不是 event identity |
| Verification | 哪个 policy version 对 candidate/source refs 得出什么 outcome | Application verifier |
| Verified Event | 哪个 logical external event 已达到产品验证标准 | Application-computed event key |
| Update | 哪个 Verified Event 成为用户可读产品事实 | Application；绑定 Verified Event/Definition version |
| Distribution | 哪个 UserSubscription 可看到该 Update | existing Update + UserSubscription identity |
| Notification | 是否/如何尝试告知该 Distribution | existing Distribution + channel logical delivery identity |

Candidate、source URL、Observation 和 Agent run identity 都不能充当 logical event identity。同一事件由多个来源、多个 cycle、
不同查询或 restart 重复发现时仍须收敛为一个 Verified Event。

## 4. Event Candidate contract

Agent 只处理开放语义，输出 bounded structured candidate。V1 candidate 至少包含：

```json
{
  "schema_version": 1,
  "entity_key": "openai",
  "event_type": "MODEL_RELEASED",
  "object": {
    "type": "MODEL",
    "display_name": "GPT-5",
    "canonical_name_candidate": "gpt-5"
  },
  "occurred_at_candidate": "2026-09-10T08:00:00Z",
  "support": [
    {"source_ref": "source-1", "exact_span": "We are releasing GPT-5 today."}
  ]
}
```

Agent 可以：

- 从 accepted Observation 的 title/snippet 中识别一个候选发布事件；
- 提出 entity、event type、具名模型、发布时间的归一化 candidate；
- 提出 Observation 内已有的 supporting `source_ref` 与 exact support span；
- 对多个报道提出它们可能是同一 logical event。

Agent 不可以：

- 把 candidate、置信度、模型推理或 structured-output success 宣称成 Verified Event；
- 引用 Observation 中不存在的 URL、span、时间或 source；
- 计算 final event key，决定 duplicate，commit Update/Distribution，或决定/执行 Notification；
- 用自己的“常识记忆”补足本次 Evidence 中不存在的发布事实。

Candidate output 必须通过 strict schema、数量上限、字段 allowlist、source-ref membership 与 exact-span containment 检查。
invalid Agent output 是 execution/contract failure，不是 NO_UPDATE，也不能被 Application 猜测修复。

## 5. Deterministic verification / Evidence gate

“Verified”在 V1 的准确含义是：candidate 满足一个窄、versioned、可重放的 application verification contract；它不是对整个
互联网的全知证明，也不是 cryptographic attestation。初版 policy 可命名为 `openai_model_release_v1`，并按以下顺序 fail closed：

1. **Observation binding**：source ref 必须来自本 cycle 接受的 immutable Source Observation；content fingerprint、query/window、
   retrieved time 与 Observation Evidence 必须一致。
2. **Source provenance**：至少一条 supporting source 是 HTTPS 且 canonical hostname 通过 exact OpenAI official-host allowlist；
   不能只相信 provider 给出的 display domain，也不能用 secondary report、社交传闻或聚合页替代 official source。
3. **Entity match**：criterion entity 必须是 `openai`，official source publisher/host 与 candidate entity 一致；只是在正文中提到
   OpenAI 的第三方页面不能建立 entity truth。
4. **Event type**：exact span 必须逐字存在于 accepted source title/snippet，包含同一个 normalized model name，并符合 V1
   bounded release assertion；future/rumor/denial/deprecation/price/SDK 等 unsupported assertion fail closed。
5. **Freshness/temporal scope**：`occurred_at` 必须取自 supporting source 的 accepted `published_at`，不能由 Agent 自造；它必须
   不早于当前 eligible observation window、不能晚于 retrieval/clock-skew boundary，且 activation 前或 pause window 内的发布
   不补发。
6. **Sufficient support**：V1 一条符合上述全部条件的 official primary source即足够；多个 secondary sources不能投票替代它。
   额外来源可作为 supporting refs，但不改变 threshold。
7. **Conflict/unsupported evidence**：同一 accepted Observation 中若存在 official denial、correction、仅预告或 model identity
   冲突，candidate 为 `VERIFICATION_INCOMPLETE`；不能在冲突中挑一个方便结论。
8. **Identity/dedupe**：全部字段验证后，Application 才从 validated `entity_key + event_type + object_type + canonical_model_key`
   计算 logical event key；source URL、报道数量、发布时间格式和 Agent candidate id 不进入 key。

模型名 normalization 是 deterministic、versioned、窄规则：Unicode/大小写/空白与连字符等表现差异归一，且 canonical name
必须能回指 official exact span。Agent 可提出 alias candidate，Application 决定它是否落在 allowlist normalization 中；V1 不用
embedding 或模糊相似度决定 identity。

Verification 自身保存 immutable Evidence，包含 policy version、Observation Evidence ref、candidate ref、supporting source refs、
每个 gate 的 bounded reason code 与 outcome。它不复制 raw provider stdout/stderr，也不向 UI 暴露 Evidence ID。只有
`VERIFIED` verification 才能进入 product transaction。

## 6. Verified Event truth、NO_UPDATE 与 incomplete

Application 是 Verified Event truth 的唯一 owner。通过 gate 后，它在一个 idempotent transaction 中：

1. reserve/reuse logical Verified Event；
2. 保存 verification binding；
3. 若 event key 是首次出现，创建一条 `update_type=EVENT` 的 Update；
4. 为当前 active UserSubscription 创建一条 AVAILABLE Distribution；
5. finalize cycle 与 temporal cursor。

Update 必须绑定 Definition/version、Verified Event、verification Evidence、canonical model display name、official occurred/published time
和 safe source projection。Agent Result 单独存在时不能被 Feed 读取。transaction crash 不能留下只有 Update、没有 Verified Event
或 Distribution 的半套 truth；transaction 前已保存的 immutable Evidence 可由 stable identity 在 retry 时复用。

### Successful `NO_UPDATE`

`NO_UPDATE` 表示本 cycle 的 source read、Agent detector 和 verifier 都完整结束，只是没有新的合格 event：

- `NO_EVENT_FOUND`：coverage complete，detector 返回零个 candidate；这只表示“本次受支持 source/window 未发现”，不宣称
  世界上绝对没有事件。
- `DUPLICATE_VERIFIED_EVENT`：candidate 通过验证，但 event key 已存在；可保存新的 verification/support refs，不创建第二个
  Verified Event、Update、Distribution 或 Notification。
- `OUTSIDE_SCOPE`：Evidence 明确证明是其他 entity/event type，或发生在 activation/pause eligible window 外。

这些都是 successful cycle，Subscription 保持 ACTIVE，notification calls 为 0。

### `VERIFICATION_INCOMPLETE`

出现 plausible in-scope candidate，但缺 official support、source provenance 不足、时间缺失/矛盾、模型名无法归一、Evidence
conflicting、result coverage truncated，或无法证明 exact event assertion时，结果是 `VERIFICATION_INCOMPLETE`：

- 不创建 Verified Event、Update、Distribution 或 Notification；
- 不伪装成 `NO_EVENT_FOUND`，也不记成 verified false；
- Subscription 继续 ACTIVE，下一个正常 cadence 可以重新观察；
- 记录 bounded reason 与 last attempted cycle，但不推进 `verified_through` success watermark；
- UI 只需表达“这次发现了可能的变化，但证据还不足；会继续关注”，不暴露 provider/Agent/Evidence internals。

provider timeout、Agent contract failure、Evidence persistence failure属于 cycle `FAILED`；effect/current truth 不确定时属于
`NEEDS_ATTENTION/UNKNOWN`。它们都不同于 verification incomplete 和 successful NO_UPDATE。

若一个 bounded Observation 含多个 candidates，verification 是 per-candidate truth；cycle aggregate 优先级是：存在首次 verified event
则创建对应 Update；否则有 plausible incomplete 就是 `VERIFICATION_INCOMPLETE`；否则是 `NO_UPDATE`。P4.6 acceptance fixture
只要求一个 logical new event，可有多个来源，不借此建设 batch event framework。

## 7. Event identity、dedupe 与 correction

V1 logical event key 绑定：

```text
openai | MODEL_RELEASED | MODEL | canonical_model_key
```

因此同一个“OpenAI 发布 GPT-5”由 official page、两篇媒体报道、不同 search query 或不同 cycle 重复出现，只能有：

- one logical Verified Event；
- one EVENT Update；
- one Distribution per UserSubscription；
- at most one logical Notification per Distribution/channel。

新 source/Observation 可以形成新的 candidate/verification audit fact，但不能改写历史 Update 或重新通知。若 OpenAI 将来使用同一
模型名表达语义上不同的再次发布，V1 会保守视为同一 logical event；扩展 identity 必须先有明确产品案例，不能让 Agent 临时加
字段绕过 dedupe。

P4.6 V1 **不支持 correction/retraction**。在首次 commit 前发现冲突会得到 `VERIFICATION_INCOMPLETE`；在 Verified Event 已存在后
才出现否定/更正 Evidence 时：

- 不删除、改写或静默撤回历史 Verified Event/Update/Distribution；
- 不创建重复 Notification；
- 保存新的 conflict verification/needs-attention fact；
- 不声称旧事件已被自动纠正。

未来 correction 必须是显式 `CORRECTION/RETRACTION` product event，绑定被纠正 event 并有独立 Update/Distribution policy；它是
后续 slice，不得在 V1 用 mutable boolean 拼凑。

## 8. Continuous observation：复用与差异

P4.6 直接复用 P4.4 已验证的语义：

- versioned cadence、timezone、activation anchor、immediate initial cycle 与 Fake Clock；
- deterministic due/tick、cycle reserve/claim/CAS、external read 在 transaction 外、atomic finalize；
- pause 后不创建/claim cycle，resume 至多一个 immediate cycle；
- downtime 把 missed slots coalesce 为一个 catch-up cycle，不机械补跑每个周期；
- provider/Agent/Evidence failure 只影响当前 cycle，Subscription 保持 ACTIVE；
- restart/concurrency/stable cycle identity 与 `next_due_at` 都由 Application 管理。

EVENT 与 threshold CONDITION 的差异必须显式保存：

- EVENT 没有 boolean predicate latch、crossing、armed/disarmed 或 false→true re-arm；是否 emit 由“新的 logical Verified Event”决定。
- 多个不同 Source Observations 可以指向同一个 event；Observation dedupe 与 Verified Event dedupe 是两层 identity。
- EVENT 默认 open-ended，没有 Flight travel-window expiry。pause/disable 才停止观察；未来用户指定 end date 需另行支持。
- first cycle 只查询 `[activation_at, observed_through]`，不补发 activation 前的模型。首次 observation 若含 activation 后、已验证的
  新发布，立即创建一次 Update。
- successful cycle 推进 durable `verified_through` watermark；下一 cycle 使用从该 watermark 向前保留 bounded overlap 的 window，
  容忍 source indexing delay并由 event key 去重。incomplete/failed 不推进 watermark。
- catch-up 只执行一个 query，但 query window 从 last successful watermark 覆盖 downtime；若 adapter 明确报告 truncation/
  coverage incomplete，结果必须是 `VERIFICATION_INCOMPLETE`，不能错误推进 watermark。
- pause 期间的 event 不回填；resume window 从 `resumed_at` 开始。已验证 event set 保留，resume 不能再次通知旧 event。
- CONDITION 的严格 out-of-order price rule不能照搬 EVENT：在 eligible/overlap window 内迟到的 official source仍可验证；最终由
  temporal scope和 event identity控制，而不是倒放 boolean latch。

P4.6 可以复用 application tick entry point 与纯 due-math helper，但当前 CONDITION-specific tables/types 不应为了“通用”先大改。
EVENT 可增加自己的窄 durable cycle/cursor/verification records；只有两个已实现 workflow 的重复代码边界经 tests 证明稳定后，
才考虑抽取 application helper，绝不建设 core scheduler framework。

## 9. Distribution / Notification 复用

Verified Event 首次 commit 后创建现有 `TrackingUpdate + UpdateDistribution` 关系；EVENT 只需增加 safe Update payload/read adapter，
不得增加 event-specific distribution 或 delivery table。

P4.5 的规则全部保持：

- Application 根据 available Distribution、active relation/lifecycle 与 version-bound policy决定 notification eligibility；
- logical Notification identity 继续绑定 `distribution_id + channel`，attempt identity 独立；
- feed-only、NO_UPDATE、duplicate、incomplete、paused 均是 notification calls=0；
- accepted 只表示 request accepted，不表示 user seen/read；explicit not-started 可有限显式 retry，unknown 不 blind retry；
- Notification failure/unknown 不修改 Verified Event、Update 或 Distribution，Feed 内容仍可读。

当前 P4.5 content preview 与部分 eligibility 代码是 CONDITION-shaped，P4.6 implementation 需要在 application adapter 层增加
EVENT safe projection；这是复用现有 Delivery 的 application extension，不是第二套 notification framework。

## 10. Deterministic Fake EVENT fixture

正确性 gates 不依赖真实 OpenAI、互联网、Brave 或 Vertex。Fake source 返回 typed、immutable observation，示例：

```json
{
  "provider": "fake_event_search",
  "entity_key": "openai",
  "window_start_at": "2026-09-10T00:00:00Z",
  "window_end_at": "2026-09-10T12:00:00Z",
  "retrieved_at": "2026-09-10T12:00:00Z",
  "coverage": {"complete": true, "truncated": false},
  "results": [{
    "source_ref": "openai-gpt5-release",
    "canonical_url": "https://openai.com/index/introducing-gpt-5/",
    "publisher": "OpenAI",
    "source_kind": "official_primary",
    "title": "Introducing GPT-5",
    "snippet": "We are releasing GPT-5 today.",
    "published_at": "2026-09-10T08:00:00Z",
    "content_fingerprint": "..."
  }]
}
```

Observation 不包含 `verified=true`、final event key 或“应该创建 Update”之类答案。Fake Agent outcome 与 source fixture分别注入，
让 tests 可以独立证明：正确 candidate 通过；candidate 无法凭空引用 source/span；media-only rumor、coming-soon、conflict、wrong
entity/type、pre-activation、future time、truncated coverage 均 fail closed。

最小 acceptance matrix：

| Fixture | Verification result | Update / Distribution / Notification |
|---|---|---|
| complete empty results | `NO_UPDATE/NO_EVENT_FOUND` | `0 / 0 / 0` |
| official release GPT-5 | `VERIFIED` | exactly `1 / 1 / policy-dependent 1` |
| same event from 3 sources | first verified, remaining duplicate | still exactly `1 / 1 / 1` |
| same observation/candidate replay or restart | existing identities reused | no increase |
| media rumor only / official coming soon | `VERIFICATION_INCOMPLETE` | `0 / 0 / 0` |
| official conflicting evidence | `VERIFICATION_INCOMPLETE` | `0 / 0 / 0` |
| old pre-activation release | `NO_UPDATE/OUTSIDE_SCOPE` | `0 / 0 / 0` |
| provider/Agent/Evidence failure | cycle `FAILED/UNKNOWN` as supported facts dictate | `0 / 0 / 0` |

还必须覆盖 immediate first event、duplicate tick、missed-cycle coalescing、pause/resume no-backfill、concurrency/crash atomicity、
Definition/version binding、Notification failure不影响 Feed，以及 HTTP/UI 不泄漏 Candidate/Evidence/Agent Run/identity internals。

## 11. Agent Harness assessment

P4.6 需要的通用能力已经存在：bounded Agent execution、strict Output Contract、Tool Policy/Authority、immutable Observation/Evidence、
durable Result 与 fail-closed recovery。Agent detector 可以消费 sealed Observation projection并返回 candidate；Application verifier和
product transaction继续拥有 truth/identity/commit Authority。未来 Brave read 与 Vertex call必须走已有授权和 Evidence边界，
但本轮不调用它们。

**结论：P4.6 设计没有暴露新的 `mini_harness_core` gap。**

需要实现的都是 Application gaps：EVENT Definition/selector、typed fake observation、candidate protocol、verification policy、
Verified Event/Update persistence、EVENT temporal cursor、safe read model，以及把 EVENT Update接入现有 Distribution/Delivery。
现有 application-facing Agent composition 较啰嗦，P4.5 real smoke也暴露过非 Agent action composition ergonomics；这些仍是
ergonomic observations，不构成 core change理由。只有实际实现被现有 seam 阻塞并提供 crash/recovery evidence后，才重新走
core change gate。

## 12. P4.6 implementation slice

下一轮实现严格限定为 Fake OpenAI `MODEL_RELEASED` vertical slice：

1. 扩展 validated Tracking Definition/selector与 commit transaction；EVENT commit不得创建 Briefing reservation、
   `FIRST_BRIEFING_REQUESTED`或 CONDITION work。
2. 增加 versioned EVENT execution/distribution policy、open-ended temporal cursor、durable cycle与 stable identity；复用P4.4 due/tick
   语义，不启动daemon。
3. 实现 typed Fake Source Observation、Observation Evidence、bounded Fake Agent Event Candidate contract与 exact source-ref/span binding。
4. 实现 `openai_model_release_v1` gate、per-candidate Verification、logical Verified Event identity、NO_UPDATE与
   VERIFICATION_INCOMPLETE分离。
5. 原子创建/reuse Verified Event、EVENT Update和现有 UserSubscription Distribution；覆盖 crash/concurrency/restart/dedupe。
6. 复用P4.5 Delivery/attempt/certainty，增加EVENT safe preview；不创建第二套Notification。
7. 用Fake Clock验证initial、cadence、pause/resume、missed-cycle coalescing与failure recovery；不实现correction/retraction。
8. HTTP/UI只显示“没有新发布 / 可能有变化但证据不足 / 新模型发布 Update / 通知状态”等产品文案。
9. 全部 correctness tests离线运行；真实 Brave+Vertex+Android仅留给后续显式 smoke，不在实现正确性gate中冒充事实。

P4.6完成后可以证明“一个受支持 OpenAI模型发布EVENT，经accepted source Evidence和deterministic gate成为唯一
Verified Event/Update/Distribution，并可复用Notification”；仍不能证明全网无遗漏、correction/retraction、其他entity/event
type、生产daemon或generic event platform。
