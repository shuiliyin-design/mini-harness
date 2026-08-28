# Harness Requirement Map

## 1. 分类

本表使用四种结论：

- **Supported**：现有 Harness 语义足够，最多需要 app wiring。
- **Application gap**：应在 `apps/digest_agent`、façade、HTTP 或 product read model 解决。
- **Runtime host gap**：进程/调度能力缺失，但不属于 Agent Harness core。
- **Harness gap**：确实需要新的通用 execution/recovery seam；必须先有产品 slice 证明必要性。

## 2. Requirement map

| Product requirement | Harness dependency | Current support / gap | Decision |
|---|---|---|---|
| 多轮 clarification | bounded model turn、durable Result、safe context | **Supported**；application 已用一 turn 一 Harness run + durable conversation 组合 | 不把 Conversation 放进 core |
| Full history 与 model context 分离 | safe bounded context assembly | core 有明确概念；Definition flow 也从 durable turns组装 bounded projection | 保持 app history / working context 分离 |
| strict NEXT/REJECT/DONE | structured candidate fail-closed | provider wire、Harness terminal Result、application validators 已有 | 无 core 修改 |
| Definition Confirmation | 无 Harness Approval 依赖 | **Supported at app layer**：P4.1 UI 已显式 proposal/confirm | 保持 explicit product action；不复用 Approval |
| atomic subscription truth | 无 Agent execution authority | **Supported at app layer**：v13 transaction + bindings | 永不进 core |
| workflow selection | bounded structured candidate | **Supported at app layer for P4.3**：明确 BRIEFING shape 与窄 flight CONDITION allowlist；其他 CONDITION/EVENT/UNKNOWN fail closed | selector 不信任 Model 自报类型；EVENT 仍未实现 |
| Tracking Definition / policy 分离 | 无 core dependency | **Supported for narrow flight CONDITION**：tracking truth 与 execution/presentation/distribution policy 已分表 | BRIEFING 继续使用兼容 snapshot，不做大迁移 |
| 首篇异步准备 | durable work intent + bounded agent execution | application outbox/manual worker 已有；Harness execution 已有 | 自动推进属于 runtime host，不是 core |
| 通用 Observation intake | Tool Policy、Observation、Evidence acceptance | **Supported**；具体 price/event schema 是 application adapter contract | 不新增领域专属 core Observation |
| deterministic CONDITION | accepted typed Observation | **Supported for P4.3 flight slice**：typed CNY Observation、`lt`、rule version 与 signal dedupe 已实现 | 比较器不进 Model，也不要求 core 专属逻辑 |
| verified EVENT | Evidence + bounded Agent output + validator | **Supported in principle**；event detector/validator 是 application gap | Agent 提候选，application 只有在 Evidence 成立时创建 Update |
| durable Update | authoritative Result/Artifact refs | **Supported for CONDITION**：application-owned Update 绑定 Definition version 与 accepted Evidence；BRIEFING 用兼容 adapter | EVENT Update 未实现 |
| per-user Distribution | 无 Harness dependency | **Supported for CONDITION**：Update↔active UserSubscription 有独立 durable binding | UserSubscription 是 recipient 真源；本轮不 shared execution |
| external Notification | authorized side effect + certainty | Delivery 语义部分 **Supported**，但当前以 digest/user 为键 | Notification 必须引用 Distribution；unknown 不 blind retry |
| 持续 cadence | time trigger、dedupe、quota、pause semantics | **Runtime host gap**；没有 scheduler/daemon | 产品合同明确后做 application-owned slice |
| 内容生成 authority | tool policy、Observation/Evidence、Output Contract、Result | **Supported** | 复用现有 workflow |
| 用户可读 progress | safe result/failure projection | **Application gap**；现有 DTO 粒度可做 BRIEFING 基础映射 | sealed Observation/Update/Distribution DTO |
| Why recommended | trustworthy ranking/profile facts | **Supported at app layer**；不需要新 Evidence type | deterministic explanation projection |
| feedback learning | stable user event + application state | **Supported at app layer** | 不使用 Session Memory 代替 Profile |
| pause/edit/history | versioned business truth + snapshot binding | pause 部分支持；definition edit/versioning 是 **Application gap** | material edit 重新确认并创建 version |
| notification certainty | authorized side effect + no-blind-retry | current Delivery adapter/service 已有 certainty | mandatory auto-delivery 未决定，不先加 Outbox |
| unattended crash recovery | inspect durable run truth，安全恢复 nonterminal | terminal repair/no-event resume 已有；started + no Result 只能 BLOCKED | 见下方真正缺口 |
| 用户/产品 quota | per-run governance + product-level allowance | core 有 run budget；per-user/feed cadence quota 是 **Application gap** | 不把 billing/product quota 放进 core |
| browser/product E2E | browser engine | 未实现，但不是 Harness capability | UX 稳定后再加少量 journey |

## 3. 真正缺失的 Harness 能力

### H1. Started-nonterminal run 的通用 reconciliation seam

当前 Harness 能安全地 fail closed：已有 durable events、没有 terminal Result 时，application 得到
`NO_SAFE_AUTOMATIC_RECOVERY`，Briefing 投影为 BLOCKED。对手动 Demo 这是正确行为；对“持续无人值守关注”则可能
长期卡住。

真正缺的不是“自动再跑一次”，而是一条 Harness-owned、基于 checkpoint/effect certainty/current observation
的 reconciliation contract，能回答：

```text
terminal truth 已存在？
action 确定未开始？
side effect 已知完成？
仍然 unknown，必须人工/外部查询？
```

只有当 P4 的 unattended execution slice 证明 BLOCKED 无法用现有 facts 处理时，才设计 core change。不能为了
产品页面先改 `mini_harness_core`，也不能把 unknown 当 failed。

### H2. Narrow application-facing execution façade（ergonomic gap）

Definition 与 Briefing workflow 目前直接装配 `run_agent`、Audit/Result/Evidence/Artifact stores 与 dispatch
primitives。语义是正确的，但 EVENT 加入后会扩大 integration coupling。未来可能需要一个窄 façade，接收 bounded
task/capabilities/output contract，返回 sealed authoritative execution projection。

这不是 CONDITION vertical slice 的 blocker，也不证明 core state machine 缺失。只有 EVENT 成为第三个真实 Agent
workflow、且重复边界稳定后才评估抽取；不能预建 abstraction，也不能借此把 Subscription/Profile/Conversation 放进 core。

## 4. 明确不是 Harness 缺口

- Home/Updates、Feed Detail、navigation 和文案；
- Definition Confirmation 和修改确认；
- Subscription/Definition version、history、pause；
- First Briefing/Product state projection；
- Tracking Definition、workflow selector、CONDITION comparator 与 event taxonomy；
- Update、Distribution、Notification 的 product cardinality；
- scheduler、daemon、HTTP polling 与 SQLite outbox；
- feedback、InterestProfile、why-recommended templates；
- multi-user auth、product quota、notification preference；
- browser automation。

这些能力即使缺失，也不能直接导致 `mini_harness_core` 修改。

## 5. Core change gate

任何未来 core proposal 必须同时回答：

1. 哪个已批准的 Product acceptance criterion 被现有 façade/组合方式阻塞？
2. 为什么它不是 application state、runtime hosting 或 UX projection？
3. 新能力的 Authority owner 是谁，Model 为什么不能控制它？
4. crash/unknown/replay 时是否仍 fail closed？
5. 是否有离线 core test、application integration test 和 architecture test？

当前已确认方向和下一条 CONDITION vertical slice 都不满足“需要 core change”的条件。潜在 H1 仍只在 unattended
runtime slice 以真实 blocked evidence 重新评估；H2 仍是 ergonomic observation，不是 core requirement。
