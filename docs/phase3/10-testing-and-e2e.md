# Testing and E2E

## Offline Deterministic Correctness Gate

实现阶段每项能力都必须增加离线测试，默认不读取网络、API key、Android 或真实 LLM：

- Domain unit：Subscription ranges/defaults、Unicode count、URL canonicalization、dedup、score、
  tie-break、feedback delta/clamp/idempotency。
- Contract unit：unknown fields、over length、too many items、duplicate IDs、foreign candidate、
  missing/foreign Evidence、orphan source marker 全部拒绝。
- Repository integration：临时 SQLite migrations、foreign keys、transactions、unique period key、
  result-to-digest idempotent projection、profile version concurrency。
- Harness integration：FakeProvider + FakeSearch MCP client，验证 local policy/effect、Observation !=
  Evidence、accepted candidates、Artifact contract 与 authoritative Result。
- Delivery integration：Fake environment adapter 的 accepted/failed/timeout/unknown，验证不推导 opened。
- Golden E2E：create natural language Subscription -> manual run -> saved Digest -> delivery record ->
  feedback -> profile update -> second run ranking changes。
- Architecture：`mini_harness_core` 不 import `apps`；domain 不 import infrastructure；Brave/Vertex/Termux
  只出现在 app adapters/integration wiring。
- Security：key 不进入 logs/SQLite/Session/Evidence/Artifact；raw search result 不跨 persistence boundary。

## Deterministic fixtures

FakeSearch 返回固定 URLs、timestamps、duplicates、missing date 与 error variants。Clock、IDs、period
key 与 provider outputs 全部注入。FakeProvider 输出包括 valid、overlong、unknown candidate、duplicate
item、bad source ref 和 repeated repair failure。测试断言业务语义，不断言 prompt prose。

第二次 E2E 必须使用第一次 feedback 产生的 profile version，并断言 deterministic score/rank
改变；不能只断言 Profile JSON 被写入。

Real Brave adapter 的 correctness 仍完全离线：注入 fake HTTP transport/fixture，覆盖 success
normalization、duplicate URL、timeout、429/Retry-After、401/403、network error、malformed JSON、
oversized response、empty results、query validation、credential redaction、unknown fields ignored、
deterministic source identity 与 bounded count。测试不得读取真实环境 key，也不得访问网络。

Real Vertex adapter 也只用 fake HTTP transport：覆盖 valid structured output、malformed/extra prose/
Markdown fence、too long、invalid source ref、duplicate item、unsupported item、timeout、401/403、429、
refusal、empty output、credential/raw-response isolation，以及 FakeProvider/VertexProvider downstream
contract parity。Structured reliability regressions 另覆盖 harmless whitespace、truncated JSON、exact safe
subtype、timeout/JSON/schema 单次 bounded retry、两次耗尽、fresh attempt identity、重启后 provenance 与
generation attempt ledger 不保存 raw output。JSON parser 另把错误压缩为六种 allowlisted lexical subtype，
并用接近真实 browser failure 的脱敏 shape 验证 line/column/category 跨 SQLite restart 保留。Workflow test
证明 provider error 仍得到 authoritative incomplete。

Output Contract diagnostics fixtures 覆盖 parser/provider candidate success 后的 too long、too many items、
invalid content/source ref、duplicate item、topic/focus mismatch、missing required content、invalid marker 与
other deterministic failure。Application/SQLite/HTTP tests 断言 subtype restart persistence、safe counts/limits、
UI 精确文案、旧 row generic compatibility、Provider call count=1，以及 rejected synthesis candidate 不落盘。

## Phase 3.5 Slice A conversation gate

`tests/apps/test_digest_conversation.py`、`test_definition_provider.py`、`test_digest_http.py` 与 architecture/domain/
repository tests 使用临时 SQLite + Fake Definition adapter 覆盖：initial/multiple `NEXT_QUESTION`、NEXT→DONE、
NEXT→REJECT、immediate DONE/REJECT、invalid DONE、governance turn ceiling、restart continuation、HTTP double
click与 concurrent duplicate。断言每轮 safe user text 先 durable，且不保存 hidden reasoning/raw provider body。

两条 fault fence 是 correctness 必选项：claim 后、Provider 前 crash 由新 owner 恢复同一 turn；Harness Result
落盘后、DefinitionOutcome 前 crash 只投影 existing Result。两者都断言一个 logical turn 最多一次 Provider
execution。Slice A 当时的 schema v10 没有 outbox/UserSubscription；code schema v11 regression改为断言
DONE alone 不写任何 product/outbox row。conversation façade/HTTP tests 仍断言未调用 commit 时没有
Subscription、Digest 或 generation run，public DTO 不含 Harness internals。

Fake/Vertex protocol parity 由 fake HTTP transport 离线验证；真实 Vertex 只做显式 opt-in compatibility smoke：

```bash
python -m tools.vertex_conversation_smoke
```

Slice D后该脚本经loopback HTTP固定执行四个logical turns：ambiguous `NEXT_QUESTION`、一次user answer后的
`DONE`、complete input immediate `DONE`、unsupported `REJECT`；随后只对第一条conversation提交Subscription。
它断言ACTIVE/PENDING/PENDING、Digest absent、Briefing Search/Digest Vertex/Delivery calls均为0、secret scan PASS。
它不运行Outbox worker；缺少Vertex配置时明确报configuration error，不能记为PASS。

Slice B时的 **NOT RUN / CONFIGURATION UNAVAILABLE** 是历史状态；统一bootstrap修复后Slice D运行时配置READY。
Real compatibility仍不替代Fake Definition deterministic correctness gate。

## Phase 3.5 Slice B product-commit gate

`tests/apps/test_subscription_activation.py` 使用真实 SQLite v11 与 Fake Definition adapter，覆盖 accepted DONE
到 `ACTIVE Subscription + ACTIVE UserSubscription + PENDING briefing reservation + pending outbox`。一个
`ForbiddenDependency` 同时占据 Search/generation/Delivery/Harness 依赖位：commit 若访问任何 external-work
dependency，测试立即失败；成功路径另断言 `digest_runs=0`、`digests=0`、`harness_run_id=NULL`。

Atomicity fault matrix 分别在 Definition、Subscription/aggregate、relation、Briefing reservation、Outbox 与
activation binding 写入后且 COMMIT 前抛错，
逐表断言 Definition/Subscription/aggregate/relation/reservation/outbox/activation 全为 0；随后同 outcome 可正常
重试。COMMIT 后 response-loss fault 则断言所有 truth 保留，并由新 service instance 以同一 outcome读取。
duplicate 与两个 thread concurrent commit 只得到一套 resource identities，第二个结果为 reused。

Repository migration fixture 从真实 v10 tables携带 legacy Subscription + Digest 前进到 v11，断言二者可读且
没有被回填虚构 Definition/relation。HTTP all-fake E2E 明确分两步：conversation DONE 返回 accepted outcome，
`POST /conversations/{id}/subscription {}` 返回 201/200、ACTIVE/PENDING 与安全成功文案；request body不能携带
definition。Outbox payload scan断言只有 IDs/version refs，无 raw request、Definition副本或 Harness identity。

2026-08-24 closure gate 的真实结果为：focused Slice A/B/migration/HTTP suite `Ran 68 tests ... OK`；全仓
`python -m unittest -q` 为 `Ran 830 tests ... OK`；`git diff --check` 与
`python mini_harness.py --self-check` 均 PASS。随后 current demo DB 从 ledger v9 显式迁移到 v11，旧 1 条
Subscription 与 1 条 Digest 可读、历史表计数不变、foreign-key/integrity checks PASS、虚构 backfill 为 0。

## Phase 3.5 Slice C durable-worker gate

`tests/apps/test_digest_outbox.py` 使用临时 SQLite、固定 clock/IDs、Fake Search 与 Fake Vertex，覆盖 pending
claim、no-work、两个 tick并发 single claim、claim 后 crash、同一 reserved `application_run_id` 与 Harness
binding复用、READY/INCOMPLETE/BLOCKED、duplicate entry、payload refs-only及 bounded drain。HTTP regression
证明 commit响应时 Digest absent 且 Search/Provider calls=0；CLI regression证明 run-once/drain/inspect/recover
全部经 Application façade。

Crash matrix 按 durable window逐项断言：claim 后未开始只能显式 release；Harness bind 后无 event用相同 binding
resume；Search/Evidence 已开始但无 terminal Result则 BLOCKED；Digest 已 durable但 Outbox仍 CLAIMED时只做
mark-only completion，Search/Provider call count保持 1、Harness ID不变、Digest仍只有一份；Outbox completed 后
即使 response/UI read丢失，下一 tick也为 NO_WORK，polling直接读取 READY。

Happy E2E 是 `DONE → atomic Subscription COMMIT → HTTP ACTIVE/PENDING（Digest absent）→ manual tick →
Digest READY → Outbox SUCCEEDED`。failure E2E 是 authoritative incomplete无 fake Digest，且
Subscription/UserSubscription仍 ACTIVE、Briefing=INCOMPLETE。2026-08-24 Slice C 全仓新基线为
`python -m unittest -q`: `Ran 844 tests ... OK`。

Real worker integration smoke 需要 Brave与四个 `LLM_*` 同时可用。配置调查发现 `.env.local` 五项实际均为 SET，
此前 CONFIGURATION UNAVAILABLE 是 application CLI/Web/smoke只读 process environment、没有统一加载项目
`.env.local`；Conversation smoke又在 bootstrap 前自行检查 `os.environ`。现在 `bootstrap.py` 统一加载且 process
environment优先，显式 `environ={}` 仍保持离线隔离；CLI、Web、Conversation smoke与async worker smoke均复用
同一 bootstrap/readiness contract。

2026-08-24 Real Definition Agent单独尝试得到 authoritative `INCOMPLETE`（共 2 次 provider calls），没有
伪造 DONE、没有 Subscription commit，也没有反复调用求偶然成功。Async first-Briefing目标随后使用明确的
deterministic validated Definition fixture；真实 Search/Generation不使用 fake fallback：

```text
T0 committed_at = 2026-08-24T08:02:20.988952Z
Subscription/UserSubscription = ACTIVE/ACTIVE
Outbox/reservation = PENDING/PRESENT
Digest = absent
Brave/Digest Vertex calls = 0/0

manual run_outbox_once

T1 ready_at = 2026-08-24T08:02:30.337371Z
Brave/Evidence/Digest Vertex = 1/accepted/1
same reserved application run = true
Digest/Outbox = READY/SUCCEEDED
secret scan = PASS
```

因此 `committed_at < ready_at`，且 Subscription success不依赖首篇 generation。该 real smoke是 integration
confidence；offline deterministic correctness仍由全仓 844 tests、self-check与diff-check独立证明。

## Phase 3.5 Slice D Definition reliability gate

离线tests覆盖strict-tool body/schema identity、canonical envelope、exact三variant、malformed/schema mismatch、
business-invalid DONE不重试、同一turn两次bounded attempts、safe attempt ledger，以及成功attempt后/process
restart前崩溃只复用candidate、不产生第二provider call/outcome。schema v1→v12 migration断言
`definition_attempts`和turn failure provenance columns存在；Digest原structured-output tests继续通过，证明共享抽取
没有回归generation。

Real acceptance固定一次代表性集合，不循环追求漂亮结果：

```text
ambiguous -> NEXT_QUESTION
user answer -> DONE
complete input -> DONE
unsupported -> REJECT
HTTP commit -> ACTIVE / First briefing PENDING / Outbox PENDING
Definition Vertex calls = 4
Briefing Search / Digest Vertex / Delivery calls = 0 / 0 / 0
Digest = absent; secret scan = PASS
```

Slice D durable closure增加rich v11→v12 fixture：所有application histories可读，重复migration无第二条ledger，
三个DDL fault seam均回滚columns/table/ledger。真实demo DB随后原位迁移到v12，历史identity/count不变、
`definition_attempts=0`且没有启动async work。最终全仓count以本次gate输出为准；self-check、diff-check、docs
links与secret/runtime scan另列为独立release gates。

第一次真实诊断曾稳定暴露REJECT wire的无关`language`字段；修复采用Digest已验证的flat-scalar + encoded JSON
strict-tool wire，而不是丢字段/coercion或反复调用。最终PASS才是当前gateway compatibility evidence。

## Offline baseline gates

Offline baseline 曾提供 48 个离线测试。第一条 slice 的 19 个测试继续覆盖 Subscription、Search
Observation → Evidence、contract、Artifact/Result；本 slice 新增 15 个 migration/domain/
feedback/ranking tests，以及 14 个 Delivery domain/service/adapter tests。全部使用临时 SQLite、
固定 clock/IDs、FakeSearch/FakeProvider/FakeDelivery，无网络与随机输出。

第一条 slice 的三条 generation E2E 继续保留：

```text
A valid candidate       -> accepted Artifact -> completed Result -> SQLite Digest
B overlong candidate    -> no Artifact       -> authoritative incomplete
C nonexistent source ID -> no Artifact       -> authoritative incomplete
```

本 slice 新增三条 E2E：

```text
A empty profile -> generation -> Digest binds profile version 0 / projection identity
B like original rank #2 -> atomic +3/topic -> same candidates next run rank #1
C dismiss original rank #1 -> atomic -3/topic -> same candidates next run rank #2
```

B/C 中两条候选的 timestamp、subscription/focus score 与 already-seen penalty 相同；测试断言
`profile_weight` 分量分别为 `+12`/`-12`，并核对 FakeProvider 收到的 candidate order，所以变化不
可能来自 Model 随机性。另有测试覆盖 opened != liked、saved、clamp、duplicate event、projection
redaction、历史 snapshot、v1→v2 migration，以及 transaction 中途失败的完整 rollback。

Delivery slice 的三条 E2E：

```text
A completed Digest -> Fake accepted/known_applied -> persisted DeliveryRecord
B completed Digest -> Fake failed/not_started -> original Digest/Result still completed
C Fake timeout -> unknown -> duplicate request/retry both do not dispatch again
```

其余测试覆盖 stable delivery/attempt identity、safe explicit retry、terminal persistence failure 保留
unknown、raw provider response 不落库、accepted 不产生 Interaction、旧 Digest immutable、v1→v3
migration，以及 Termux safe preview/certainty mapping。

Real Brave slice 另增 16 个 fake HTTP adapter/workflow/smoke tests；Real Vertex slice 再增 13 个
provider/contract/workflow tests；当时 checkpoint 共 77 个 application tests，
逐项覆盖上一节边界，并固化真实结果只部分命中多词 query 时的 topic provenance 与 incomplete smoke
安全退出。当前 application suite 已包含 Phase 3.5 的 conversation/product-commit 回归，精确数量以
`python -m unittest discover -s tests/apps -q` 的当次输出为准。完整 Golden E2E 还缺少 API 层串接；
Delivery 与 Feedback 各自的应用链路已经离线闭环。

## External integration evidence

真实服务只提供显式 opt-in smoke：

```bash
BRAVE_SEARCH_API_KEY=... python -m tools.brave_search_smoke
```

1. `BRAVE_SEARCH_API_KEY` 存在时调用 `AI agent engineering latest developments`，最多 5 条；
2. 只打印 query、normalized count、title/domain、Observation identity、candidate-set identity、Evidence ID；
3. Search smoke 成功后用相同 Brave client + FakeProvider 跑完整 Digest workflow，检查 Result、Digest、
   source refs、`max_chars` 与 candidate provenance；
4. key 缺失时明确 `CONFIGURATION_ERROR`；application smokes由统一 bootstrap加载 `.env.local`，不打印 raw
   headers/JSON或任何配置值；
5. failure confidence 使用 fake transport 的 timeout/429，不消耗真实 quota，也不伪造 Evidence；
6. 真实 Termux/Android 继续是其他独立变量。

脚本使用临时 SQLite/workspace/audit，退出后不保留服务数据。smoke 的网络、quota 或 credential
状态不进入 `python -m unittest -q` correctness gate。

Thin CLI slice 以全 fake bootstrap 跑完整 product journey，并覆盖 readiness no-I/O、missing config、invalid
paths、schema migration、secret non-disclosure 与 explicit provider selection。真实 Brave+Vertex CLI smoke
只提供 integration confidence，不进入 deterministic assertions。

Loopback HTTP slice 再用 ephemeral `127.0.0.1` server 覆盖被动 readiness、自然语言 CRUD、double-click
Run idempotency、Digest/status、Feedback/Profile、Delivery、CSRF/body validation、HTML escaping、safe failure
与 public-field scan。Golden HTTP E2E 全程只调用 HTTP/façade，并断言 Like 后第二次排序改变。真实
`python -m tools.digest_http_smoke` 是 Real Brave + Vertex HTTP Product Integration Journey，只提供
integration confidence。它使用标准库 `http.client`，不启动或驱动浏览器引擎，不能称为 Automated
Browser-Engine E2E。

Browser acceptance provenance regressions 另外证明：Fake Brave success + accepted Evidence + Vertex
`TIMEOUT/INVALID_RESPONSE` 得到 incomplete + `generation_timeout/generation_invalid_response` 且无 Digest；
Search TIMEOUT 得到 `search_timeout` 且 Provider calls=0。HTTP `GET /runs/{id}` 与 `/?last_run={id}` 同时断言
stage/code/安全文案，generation timeout 不得出现 search unavailable。Legacy NULL provenance 只读为
`unknown_stage/legacy_failure`。

Real Vertex sample 使用显式 `LLM_API_MODE=chat-completions` 并请求 strict tool schema。五组固定脱敏输入
各执行两次：2 candidates + focus、5 candidates + no focus、long snippet、
Chinese + many refs、browser subscription shape。每个 case 禁用 workflow retry，因此十条统计就是十次独立 Provider call；只输出
transport/parse/schema/refs/contract、lexical subtype 与 latency，不输出或持久化正文。

最初 `response_format=json_schema` 的简单 probe 被 browser 的两次 `ITEMS_TYPE` 推翻：gateway 接受参数不
等于底层路由实施 grammar constraint。加入 exact safe schema rule ledger 与脱敏 regression 后，改用对抗性
probe 通过的 required strict tool。但随后真实 browser Run `fa31f8…` 的两次 tool arguments 均为
`ITEMS_TYPE`，证明请求 `strict=true` 不等于 enforcement。

该 synthetic 8/8 只保留为 false-confidence 教训。真实 Browser 先后证明 collection array 会变 object，
改名无效，nested singleton object 又会变 string。最终 Vertex wire 收缩为六个顶层 string；
chat 输入只投影 Harness 已排定的 rank-1 candidate，adapter 确定性重建 canonical singleton lists，
再进入未放宽的完整 schema 与 Output Contract。

最终十次重复 Real Vertex Provider Compatibility Gate 为 10/10：transport、envelope、parse、wire schema、
canonical refs 与 Output Contract 全部通过，无 timeout 或 lexical/schema/envelope subtype。Real Brave +
Vertex HTTP Product Integration Journey 连续 3/3 通过；每轮包含订阅、首次生成、Digest read、Like/Profile 和第二次生成，
共六次真实 generation call。每轮临时 server 均已关闭。

Manual Mobile Browser Acceptance 由用户在真实手机浏览器操作 live service 完成，并由 application run
`7500de417cde44aabaa855b52be9368a`、Harness run `f0643ea853a34f339f76f7764b6f97e2`
与 Digest `1dbf926baf084e8fab33fe3bd14bb611` 的 durable lineage 佐证，状态为 PASS。该证据不具备自动化
重复执行语义。Automated Browser-Engine E2E 当前为 NOT IMPLEMENTED / NOT RUN；没有 Playwright、Selenium
或其他 browser automation PASS 证据。

完整 Evidence Matrix 见 [`14-product-readiness-review.md`](14-product-readiness-review.md)。任何必需 gate
失败都不得宣称当前 Web Demo ready。

2026-08-23 credential-dependent smoke 首次发现多词 topic exact-match 与 incomplete Digest
解引用缺陷；两个离线 regression 先失败后修复。修复后重跑得到 3 条 normalized results、accepted
candidate-set Evidence、completed Result、1 条 source ref，Digest 为 `228 <= 600`。这些数量会随
实时搜索变化，不写入 deterministic assertions。

Vertex real smoke 使用当前进程已有的 `LLM_API_KEY/LLM_API_MODE/LLM_ENDPOINT/LLM_MODEL`，并按顺序运行：

```bash
python -m tools.vertex_digest_smoke
```

```text
Fake Search -> Real Vertex -> contract -> Artifact/Result/Digest
Real Brave  -> Real Vertex -> contract -> Artifact/Result/Digest
```

2026-08-23 实测 provider/model 为 `vertex` / `sonnet-4.6`。Fake Search slice 得到 2 items、valid refs、
`140/600`、completed；Real Brave slice 得到 2 items、valid refs、`314/600`、completed；两次 secret
scan 均 PASS。模型内容和实时结果会变化，这些数值只记录 integration confidence，不进入离线 assertions。

## Future release gate

```bash
python -m unittest -q
git diff --check
python mini_harness.py --self-check
```

当前 slice 可用 `python -m unittest discover -s tests/apps -q` 单独运行，并必须同时通过全仓
gate；真实网络与 Android 仍不是 correctness gate。

上一页：[`09-failure-and-recovery.md`](09-failure-and-recovery.md) · 下一篇：
[`11-design-decisions.md`](11-design-decisions.md)
