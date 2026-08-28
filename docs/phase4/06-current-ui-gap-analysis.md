# Current Web UI Gap Analysis

> 本文记录 Phase 4 设计 checkpoint 时的 P4.1 前 baseline，用于解释 slice 来源；P4.1/P4.2 已解决项与当前验证事实见
> [`10-p41-implementation-status.md`](10-p41-implementation-status.md) 和
> [`11-p42-implementation-status.md`](11-p42-implementation-status.md)。

## 1. 审查基线

本分析直接基于当前：

- `apps/digest_agent/web.py` 的 `_render_page`、routes 与 inline JavaScript；
- `DigestApplication` public DTO/operations；
- `tests/apps/test_digest_http.py`、conversation/activation/outbox/application tests；
- Phase 3.5 schema v13 与 architecture review。

当前 Web 是一个安全的 loopback teaching demo：stdlib server-rendered 单页、CSRF、exact JSON fields、bounded body、
HTML escaping、safe URL 和 sealed DTO 都已有测试。下表评估的是产品体验，不否定这些 correctness 成果。

## 2. 当前页面真实结构

```text
AI Digest / 本机订阅式 Agent Demo
├── sticky Last run Status / Stage / Reason
├── 定义订阅 textarea + conversation div
├── Subscriptions cards
│   ├── Subscription: Successful / Legacy
│   ├── First briefing: PENDING/...
│   ├── enable/disable
│   └── Run now
├── Recent Digests
│   ├── rendered text + source links
│   ├── Like/Dismiss/Save/Opened
│   └── Deliver
└── Profile raw topic weights
```

这是一张功能验证台，不是以 Updates 为核心的信息架构。

## 3. Gap matrix

| Area | 当前真实行为 | 用户问题 | 目标变化 | Owner |
|---|---|---|---|---|
| IA | 所有功能在 `/` 一页纵向堆叠 | 内容、创建、管理和 debug 无优先级 | Updates / Following / Me + Feed Detail | Web/product |
| 命名 | 中英混排：Subscriptions、Digest、Profile、Run now、Deliver | 暴露技术/demo 语言 | 关注、更新、兴趣、立即更新、通知 | Product copy |
| Home | Subscription cards 先于内容 | 回访用户不能先看到新资讯 | ready updates 优先，progress 次之 | Product read model |
| Create | 一个 textarea 后进入 latest-state box | 缺少专注的创建流程和保存退出语义 | 独立 Create/Conversation route/view | Web |
| 多轮 | server 支持多轮；UI 只显示当前 question，不显示 transcript | 用户忘记上下文 | safe conversation history projection | Application + Web |
| Confirmation | JS 在 `DEFINITION_ACCEPTED` 后立即 POST `/subscription` | Agent candidate 未经用户确认就建立长期关系 | 显示 definition，明确确认/调整 | Web；revision 是 app gap |
| Success | commit 后短暂显示成功，500ms reload | 成功与准备状态转瞬即逝 | 独立 success state + clear next actions | Web |
| First briefing | 每 2 秒轮询所有 product subscriptions；GET 不推进 worker | manual worker 未运行时会永久 PENDING | truthful progress；自动推进另做 slice | Web/runtime host |
| Subscription | 同时显示 `enabled`、product/legacy 和 briefing text | 用户不理解多套状态 | sealed `active/paused + latest update` projection | Application façade |
| Legacy path | HTTP 仍允许直接 `POST /subscriptions`，绕过 conversation | 两种创建语义并存 | 产品 UI/API 明确只走 confirmed flow；legacy 仅兼容 | HTTP/app migration |
| Digest read | 整份 rendered text 放在首页卡片 | 首页过长、Feed 归属弱 | preview -> Feed Detail | Product read model/Web |
| Why recommended | payload 有 reason/breakdown，页面不渲染 | 无法建立推荐信任 | user-facing deterministic explanation | Application projection |
| Feedback | 每个 item 连续渲染 Like/Dismiss/Save，另有 digest-level Opened | 无选中态、反馈确认、撤销语义 | 多来点/少来点/收藏 + saved acknowledgement | Web/app projection |
| Interest | raw topic integer weights + rule/profile version | 像 debug 数据，不像“更懂我” | interest change summary/history | Application read model |
| Management | card 上只有 enable/disable/run；PATCH 无 UI | 无 definition/history 管理心智 | dedicated management + versioned edit | Product/app |
| Run | `Run now` 和 sticky last Run status | 暴露 execution model | “立即更新”仅在产品需要时出现 | Product copy/app request |
| Failure | `Status/Stage/Reason`、部分英文错误、admin CLI 文案 | 无法判断订阅是否仍有效、下一步是什么 | failure by user impact + safe action | Application mapping/Web |
| Delivery | `Deliver` 按钮和 fake channel | 暴露 adapter/demo 行为 | 通知偏好和 delivery outcome 独立呈现 | Product/app |
| Restore | conversation ID 在 `localStorage`，GET 只恢复 latest view | 多设备/清存储不可发现 draft；无 draft list | server-owned draft discovery（后续） | Application/HTTP |
| Loading | 全局 `Working…`，mutation 后普遍 reload | 局部状态不清、会丢阅读位置 | per-action pending + stable optimistic rules | Web |
| Accessibility | viewport 和原生 controls 有基础；无 focus/error region 设计 | 动态更新可能不被辅助技术理解 | landmark、labels、aria-live、focus recovery | Web |

## 4. 已有可直接复用的产品基础

- conversation create/message/get 全部经 façade，idempotency key 已要求；
- server state 决定 `WAITING_FOR_ANSWER`，没有 `asked_once`；
- `ConversationView.definition` 已能支撑 confirmation summary；
- commit endpoint 不接受 client definition，可防止浏览器篡改 candidate；
- `SubscriptionCommitView` 已明确 ACTIVE + PENDING + 中文 success message；
- `FirstBriefingView` 已把 Subscription 与 briefing 分离；
- Digest DTO 已移除 Evidence ID / profile projection identity；
- feedback 和 delivery 已幂等；failure provenance 已有 safe stage/code/subtype；
- architecture tests 已保证 Web 只依赖 application façade/bootstrap。

## 5. 当前 façade 的产品缺口

这些不是要求立刻横向扩张 backend，而是实现 slice 前必须承认的不足：

1. 没有 `HomeUpdateView` / `FeedDetailView` 聚合 DTO，浏览器需要拼接多个 endpoints。
2. ConversationView 只给 latest outcome，没有 safe transcript/draft discovery。
3. `DEFINITION_ACCEPTED` 是 terminal；“调整”缺少明确 continuation/supersession use case。
4. product Subscription 的 generic `update_subscription` 会修改 compatibility Subscription payload，却不会产生新的
   `SubscriptionDefinition` version；不能把该 PATCH 当成正式 definition edit。
5. only-first briefing 有 dedicated projection；没有后续 scheduled briefing series/period history contract。
6. current ProfileView 只有 weights，没有 user-facing change history。
7. `INCOMPLETE` 还需要进一步区分 `no_update` 与 `failed` 产品语义。

## 6. 不应沿用的 UI 设计

- 不把 current single page 换皮后称为产品首页；
- 不把 uppercase durable states 直接翻译后交给浏览器组合；
- 不因 commit endpoint 现成就继续自动 commit；
- 不在 polling GET 中调用 manual worker；
- 不把 admin recovery action 或 relation publication 放进普通用户 UI；
- 不用 LLM 生成导航、状态映射、feedback delta 或 failure action。
