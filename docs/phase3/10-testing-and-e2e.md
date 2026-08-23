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
contract parity。Workflow test 证明 provider error 仍得到 authoritative incomplete。

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
