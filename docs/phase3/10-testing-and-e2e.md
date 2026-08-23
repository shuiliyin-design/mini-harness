# Testing and E2E

## Correctness gates

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
generation attempt ledger 不保存 raw output。Workflow test 证明 provider error 仍得到 authoritative incomplete。

Output Contract diagnostics fixtures 覆盖 parser/provider candidate success 后的 too long、too many items、
invalid content/source ref、duplicate item、topic/focus mismatch、missing required content、invalid marker 与
other deterministic failure。Application/SQLite/HTTP tests 断言 subtype restart persistence、safe counts/limits、
UI 精确文案、旧 row generic compatibility、Provider call count=1，以及 rejected synthesis candidate 不落盘。

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
provider/contract/workflow tests，当前共 77 个 application tests，
逐项覆盖上一节边界，并固化真实结果只部分命中多词 query 时的 topic provenance 与 incomplete smoke
安全退出。完整 Golden E2E 还缺少 API 层串接；
Delivery 与 Feedback 各自的应用链路已经离线闭环。

## Manual integration confidence

真实服务只提供显式 opt-in smoke：

```bash
BRAVE_SEARCH_API_KEY=... python -m tools.brave_search_smoke
```

1. `BRAVE_SEARCH_API_KEY` 存在时调用 `AI agent engineering latest developments`，最多 5 条；
2. 只打印 query、normalized count、title/domain、Observation identity、candidate-set identity、Evidence ID；
3. Search smoke 成功后用相同 Brave client + FakeProvider 跑完整 Digest workflow，检查 Result、Digest、
   source refs、`max_chars` 与 candidate provenance；
4. key 缺失时明确 `CONFIGURATION_ERROR`，不读取 `.env.local`，不打印 raw headers/JSON；
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
`python -m tools.digest_http_smoke` 只提供 Brave+Vertex integration confidence。

Browser acceptance provenance regressions 另外证明：Fake Brave success + accepted Evidence + Vertex
`TIMEOUT/INVALID_RESPONSE` 得到 incomplete + `generation_timeout/generation_invalid_response` 且无 Digest；
Search TIMEOUT 得到 `search_timeout` 且 Provider calls=0。HTTP `GET /runs/{id}` 与 `/?last_run={id}` 同时断言
stage/code/安全文案，generation timeout 不得出现 search unavailable。Legacy NULL provenance 只读为
`unknown_stage/legacy_failure`。

Structured-output real smoke 固定为少量 3 个 logical generation runs；每个 run 仍服从最多两个 attempt 与
125 秒 deadline。只汇总 completed/invalid/timeout 计数及安全 latency，不保存模型正文，不以真实成功率
替代 fake-transport correctness gate。

2026-08-23 实测 3 runs 为 2 completed + 1 deterministic contract incomplete，模型 attempts 中
invalid-response=0、timeout=0，latency min/median/max 为 13.965/14.102/44.993 秒。第三次 JSON/schema parser
成功仍未绕过 Output Contract；测试在三次后停止，没有以追加 runs 追求 100% success。

schema v8 diagnostics 后的下一组（同样最多三次）为 1 completed + 2 `too_long`；两个 rejection 都是
630/600 chars、Provider attempts=1，invalid-response=0、timeout=0。UI 所需 subtype/limits 因而来自 validator
事实，不来自 Model explanation，也没有因 contract failure regeneration。

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
