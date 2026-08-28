# Phase 4 Review Guide

## 1. 建议审查路径

### Product reviewer

1. 从 [`00-product-vision.md`](00-product-vision.md) 的五个用户问题开始。
2. 用 [`02-wireframes.md`](02-wireframes.md) 逐屏走首次创建、回访阅读、反馈和失败分支。
3. 检查 [`07-incremental-slice-plan.md`](07-incremental-slice-plan.md) 是否每步先交付用户价值。

### Architecture reviewer

1. 对照 [`03-product-state-projections.md`](03-product-state-projections.md) 检查每句用户文案来自哪些 durable facts。
2. 对照 [`04-agent-capability-map.md`](04-agent-capability-map.md) 查找被错误 Agent 化的 deterministic logic。
3. 对照 [`05-harness-requirement-map.md`](05-harness-requirement-map.md) 审核任何 core proposal。

### Implementation reviewer（未来 slice）

1. 从 public application DTO/operation 开始，不从 SQLite table 或 Harness store 开始；旧 `DigestApplication` 是兼容入口。
2. Web/HTTP 只依赖 façade/bootstrap；architecture test 必须继续成立。
3. 检查 crash、idempotency、response loss、restart 与 unknown-effect tests。

## 2. Journey checklist

- [ ] NEXT_QUESTION 可以重复任意次数，UI 只读 server state。
- [ ] refresh/restart 后能恢复当前 conversation，而不是依赖前端 boolean。
- [ ] DONE 后先 confirmation；未确认时没有 product relation。
- [ ] confirmation 显示的是 validated server candidate。
- [ ] commit 成功与 observation/update ready 是两个状态，workflow 不能由 Model 自行 commit。
- [ ] Home 以 ready update 为主，不以 Run/Subscription debug card 为主。
- [ ] Feed Detail 能说明来源与 why updated；CONDITION 展示确定性阈值事实，EVENT 展示已验证事件事实。
- [ ] feedback 重复不会二次学习，旧 briefing 不漂移。
- [ ] pause/edit/history 不删除或改写旧内容。
- [ ] failure 页面能说明 Feed 是否仍有效及安全下一步。

## 3. State projection checklist

- [ ] projection 是 pure read，没有外部调用或 worker tick。
- [ ] ACTIVE + FAILED/INCOMPLETE/BLOCKED 是合法组合。
- [ ] Update、Distribution、Notification 正交；任一 notification failed/unknown 不覆盖 Update READY。
- [ ] 用户可见 Update 必须有 active UserSubscription Distribution，recipient 不从内容对象猜测。
- [ ] absent/legacy/conflicting facts 不被猜成成功。
- [ ] INCOMPLETE 按 safe facts区分 no update 与执行失败。
- [ ] browser 不自己组合 product/relation/outbox/run 状态。
- [ ] 用户 DTO 不包含 Harness ID、Evidence、Artifact、checkpoint、raw provider body、traceback。

## 4. Agent boundary checklist

- [ ] Agent output 始终是 candidate。
- [ ] protocol validity、business validity、confirmation、commit 分层。
- [ ] Conversation schema ≠ Definition schema；internal required field 不会成为默认问题。
- [ ] workflow selector 验证 definition shape，不信任 Model 自报类型。
- [ ] CONDITION 数值判断 deterministic；EVENT candidate 绑定 accepted Evidence 后才成为 Update。
- [ ] 排序、limit、identity、idempotency、cadence、quota、state mapping 均 deterministic。
- [ ] recommendation reason 引用真实 reason facts。
- [ ] Profile 是 application state，不是 Session Memory。
- [ ] full product/conversation history 不等于 model working context。

## 5. Harness/core change checklist

在接受 core diff 前，必须全部为“是”：

- [ ] 有一个具体 P4 acceptance criterion 被阻塞。
- [ ] 已证明 façade composition 和 application-owned implementation 不足。
- [ ] 新能力对多个 app 有稳定通用语义，不包含 Feeds nouns。
- [ ] Authority owner、effect、policy、verification、recovery 都明确。
- [ ] unknown/replay/crash 不会因为新能力被弱化。
- [ ] 有离线 core、integration、security/architecture tests。

若任一项为“否”，先留在 application 或 runtime host。

## 6. P4.1 focused review

P4.1 通过的最小证据：

```text
3+ NEXT_QUESTION
  -> DONE
  -> durable DEFINITION_ACCEPTED
  -> refresh still shows confirmation
  -> zero Subscription before click
  -> explicit confirm
  -> ACTIVE Feed + first update PREPARING
  -> duplicate confirm returns same identities
```

同时覆盖 REJECT、invalid DONE、turn ceiling、processing restart、confirm response loss 和 adjustment path。
P4.1 不以“页面更漂亮”作为完成标准，也不要求改 `mini_harness_core`。

## 7. Release gates

每个 implementation slice 至少运行：

```bash
python -m unittest -q
git diff --check
```

涉及真实 provider/browser 的 smoke 只能增加 integration confidence，不能替代 offline deterministic correctness。
design-only 本轮也运行相同 gate，证明文档修改没有破坏仓库。
