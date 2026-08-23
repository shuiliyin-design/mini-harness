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
- Architecture：`mini_harness_core` 不 import `apps`；domain 不 import infrastructure；Brave/Termux
  只出现在 app adapters/integration wiring。
- Security：key 不进入 logs/SQLite/Session/Evidence/Artifact；raw search result 不跨 persistence boundary。

## Deterministic fixtures

FakeSearch 返回固定 URLs、timestamps、duplicates、missing date 与 error variants。Clock、IDs、period
key 与 provider outputs 全部注入。FakeProvider 输出包括 valid、overlong、unknown candidate、duplicate
item、bad source ref 和 repeated repair failure。测试断言业务语义，不断言 prompt prose。

第二次 E2E 必须使用第一次 feedback 产生的 profile version，并断言 deterministic score/rank
改变；不能只断言 Profile JSON 被写入。

## Current slice gates

当前 `tests/apps/` 提供 48 个离线测试。第一条 slice 的 19 个测试继续覆盖 Subscription、Search
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

完整 Golden E2E 还缺少 API 层串接；Delivery 与 Feedback 各自的应用链路已经离线闭环。

## Manual integration confidence

真实服务只提供显式 opt-in smoke：

1. `BRAVE_API_KEY` 存在时调用一条窄 query，检查 schema/projection/secret redaction；
2. 真实 Provider 生成一次 Draft，由同一 deterministic contract 检查；
3. Termux/Android 可用时发送测试 notification，记录 request accepted，不声称 user read；
4. smoke 的网络波动不进入 `python -m unittest -q` release gate。

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
