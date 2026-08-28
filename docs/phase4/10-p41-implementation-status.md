# P4.1 Implementation Status — Confirmed Feed Creation

> P4.1 的 confirmation/commit lifecycle 仍有效；onboarding 的 intent/default 语义已由 P4.1.1 修正。当前字段模型、
> provenance 和验证证据以 [`12-p411-intent-onboarding-correction.md`](12-p411-intent-onboarding-correction.md) 为准。

## 1. 结论

P4.1 已按 Product journey 实现。Agent `DONE` 仍只是 validated proposal；它不会建立 Subscription。用户看到
server-owned definition 后，必须显式点击“确认订阅”，才进入 Phase 3 已有的 atomic product commit。

```text
自然语言 -> NEXT_QUESTION × N -> DONE proposal
  -> 请确认关注范围 -> 继续调整 / 确认订阅
  -> atomic COMMIT -> 已开始关注 + 首篇资讯正在准备
  -> Web runtime 自动推进已有 durable first-briefing work
```

## 2. 实现边界

- Conversation：保留既有 bounded Agent execution、durable turns、validated DefinitionOutcome；新增仅允许对
  **未 commit 的** `DEFINITION_ACCEPTED` proposal 追加调整 turn。
- 调整语义：不在 client 修改 candidate，也不创建 Subscription。新 turn 带着完整 server conversation context 再交给
  Definition Agent；旧 DONE outcome 保持 immutable，最新 validated outcome 成为待确认 proposal。
- Confirmation：Web 从 `ConversationView.definition` 显示 topic、focus、language、cadence、max items、max chars 与
  delivery preference。页面不从自然语言重新解析这些值。
- Commit：继续复用 `SubscriptionActivationService` 与 SQLite 单事务 commit。调整 reserve 与 commit 都持有
  `BEGIN IMMEDIATE`，因此并发下只能由一方越过边界；commit 后再调整会确定性拒绝。
- 幂等：同一 proposal 重复确认返回既有 activation/subscription identities；不会创建第二个 Subscription。
- 恢复：proposal/turn/outcome 都在 SQLite；刷新通过 conversation GET，server 重启后仍能恢复 confirmation。
- 首篇：commit HTTP response 固定先返回 Subscription `ACTIVE` + first briefing `PENDING`。response 写出后，Web
  runtime 唤醒已有 durable outbox worker；server 启动时也唤醒一次以恢复未完成 work。
- UI：不显示 DefinitionOutcome、Run、Evidence、Outbox、Harness、Provider、worker 或 CLI。REJECT、INCOMPLETE 与
  processing 使用产品文案和安全动作。

没有修改 `mini_harness_core/`，没有新增第三方依赖或 schema migration。

## 3. 与原设计的差异

[`07-incremental-slice-plan.md`](07-incremental-slice-plan.md) 把 scheduler 排除在 P4.1 外，并把持续 cadence 留给 P4.6；
本次实现仍遵守该边界。实现 prompt 进一步要求“正常 Web journey 无需 CLI/manual worker、后台自动生成首篇内容”，因此
P4.1 增加的是 **post-commit + startup one-shot wake**，不是 cadence scheduler：

- 不按 `cadence` 创建后续 briefing；
- 不让 GET/polling 推进 work；
- 不自动 Delivery；
- 不修改 outbox/recovery/Harness 状态机；
- 真实 Definition-only smoke 显式关闭 first-briefing wake，继续验证 commit 时无 Search/Generation side effect。

因此，“Subscription success ≠ First Briefing ready”仍成立；Web 自动推进不把两个 durable result 合并。

## 4. Acceptance evidence

| P4.1 criterion | 实现/测试证据 |
|---|---|
| 3+ NEXT_QUESTION，无 UI 轮数假设 | HTTP Product E2E 连续 3 次 NEXT 后 DONE；client 只按 server status 渲染 |
| DONE 后零 product rows | application/HTTP 断言 Subscription、UserSubscription、Briefing 均为 0 |
| durable confirmation | application restart 与 HTTP server restart 后 GET 恢复同一 validated definition |
| server-owned fields | confirmation 从 `ConversationView.definition` 渲染全部七类字段 |
| 继续调整 | 新 durable turn 可 NEXT 或 DONE；调整前后均不 commit，重复 adjustment 幂等 |
| explicit atomic commit | 只有“确认订阅”调用既有 activation façade；commit response 为 ACTIVE/PENDING |
| duplicate confirmation | 重复 POST 返回相同 Subscription identity，product row 总数为 1 |
| success / preparing 分离 | commit body 固定 PENDING；后台随后独立变为 READY |
| 无 manual worker | 默认 Web Product E2E 只轮询 read endpoint，未调用 CLI 或 `run_outbox_once` |
| 产品 UI 不泄漏内部概念 | render/HTTP architecture assertions；页面移除 Run 操作与内部 failure stage |
| core boundary | `git diff -- mini_harness_core` 为空 |

## 5. Verification record

完成实现后执行并记录：

```text
focused deterministic/application/HTTP/architecture tests: PASS（64 tests）
real Vertex Definition HTTP smoke: PASS（4 Definition calls；0 briefing Search/Vertex/Delivery calls）
python -m unittest -q: PASS（866 tests）
python mini_harness.py --self-check: PASS
git diff --check + docs trailing-whitespace scan: PASS
changed-line/docs secret scan + new runtime artifact scan: PASS
git diff --exit-code -- mini_harness_core: PASS（empty）
Browser Engine E2E: NOT RUN（仓库没有 Browser Engine automation）
```

“HTTP/UI Product E2E”指真实 loopback HTTP server + 页面 contract + durable database 的离线测试；它不冒充浏览器引擎测试。

## 6. Deferred

这是 P4.1 完成时的 slice 边界记录：当时没有实现 P4.2 Home aggregate/Feed Detail、P4.5 profile/definition redesign、
P4.6 cadence scheduler 或 automatic Delivery。P4.2 后续实现状态见
[`11-p42-implementation-status.md`](11-p42-implementation-status.md)；P4.5/P4.6 仍未实现。
