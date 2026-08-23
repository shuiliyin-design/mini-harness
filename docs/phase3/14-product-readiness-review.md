# Product Readiness Review

> Implementation checkpoint：本 review 记录 slice 开始前发现；其中 application façade、versioned
> Subscription lifecycle、Digest query、入口级 Product E2E 与最小 reserved/terminal recovery 已由
> [`15-application-facade-and-run-lifecycle.md`](15-application-facade-and-run-lifecycle.md) 完成。HTTP/Web UI、
> safe startup readiness 与 thin CLI 又由
> [`16-cli-bootstrap-and-readiness.md`](16-cli-bootstrap-and-readiness.md) 完成。当前后续项是 ambiguous run
> reconciliation，以及可选的 loopback HTTP/极薄 Web UI。安全的 application-level admin inspection/
> action allowlist 已由 [`17-application-admin-recovery.md`](17-application-admin-recovery.md) 完成，但它不会
> 猜测 unresolved Harness effect。

本评审最初以 2026-08-23 slice 起点的 repository 为事实来源，评估 AI Digest Subscription Agent 是否已经
成为“小而完整、可实际运行的订阅型应用”，不是继续扩展 Harness。当时结论是领域与安全闭环接近完整，
但用户产品闭环尚未形成。后续章节保留这份历史 gap analysis；当前 release 判断以紧接其后的 Evidence
Matrix 为准。

## Phase 3 Demo Release Evidence Matrix

下表是当前 release checkpoint 的证据命名；它覆盖并更新本文后续保留的早期 readiness 判断，但不改写
历史失败 run。

| Gate | Evidence | What it proves | What it does NOT prove | Current status |
|---|---|---|---|---|
| Offline Deterministic Correctness Gate | `python -m unittest -q`、`git diff --check`、`python mini_harness.py --self-check` | 离线 deterministic domain/adapter/workflow/HTTP façade、安全与 Harness authority invariants | 当前真实 gateway、Brave 或手机浏览器可用 | PASS |
| Real Vertex Provider Compatibility Gate | 五个脱敏场景各两次，workflow retry disabled；逐层统计 transport/envelope/parse/schema/refs/contract | 当前 gateway/model/route 的完整 provider wire → canonical Output Contract 链兼容 | native schema enforcement、未来 gateway 行为或 Brave/UI | PASS，10/10 |
| Real Brave + Vertex HTTP Product Integration Journey | 连续 3/3；每轮两次真实 generation，使用标准库 `http.client` | 真实 Brave/Vertex 经 loopback HTTP、Digest、Feedback/Profile 的产品集成路径 | Browser JS、DOM、真实设备交互或 browser automation | PASS，3/3 |
| Manual Mobile Browser Acceptance | 用户真实手机浏览器操作；application run `7500de417cde44aabaa855b52be9368a` → Harness run `f0643ea853a34f339f76f7764b6f97e2` → Digest `1dbf926baf084e8fab33fe3bd14bb611` | 当前手机/浏览器/服务时点可完成真实 journey，并有 durable lineage 佐证 | 自动化重复性、其他浏览器或未来环境 | PASS |
| Automated Browser-Engine E2E | 仓库没有 Playwright/Selenium/WebDriver/browser-engine runner | 明确当前没有该类证据 | 任何 browser automation PASS 结论 | NOT IMPLEMENTED / NOT RUN |

历史 run `fa31f8edf20c46a6b6c7fd74a54290ab` 及其他真实失败/成功 durable runs 保持不变，继续作为
requested strict output 不等于 verified structured output 的 release evidence。

## 用户旅程逆向审查

| 用户步骤 | 当前真实实现 | 可达性与缺口 |
|---|---|---|
| 创建自然语言订阅 | `DigestApplication.create_subscription` → service + SQLite | façade 可达；parser 仍只支持有限中文模式 |
| 查看订阅 | façade `get/list` | 已按 local user scope 投影 application DTO |
| 修改、暂停、恢复、删除订阅 | versioned update/enable/disable | CAS 与历史 snapshot 已实现；V1 明确不 hard delete |
| Run now | façade `run_subscription(subscription_id, idempotency_key)` | 同步入口、稳定 identity 与 safe state 已有；尚无 CLI/HTTP transport |
| 搜索真实内容 | app-owned `BraveSearchClient` | adapter 与安全 taxonomy 已有；仅 opt-in smoke 装配真实配置 |
| 个性化排序 | deterministic `rank_candidates` + persisted Profile projection | production-shaped，绑定 version/breakdown，并可经 façade 查看 safe Profile |
| Real LLM Digest | app-owned `VertexDigestProvider` + deterministic Output Contract | production-shaped adapter；真实路径仅 smoke 装配，失败返回 application reason code |
| 查看 Digest | façade `list/get_digest` | ownership 与 DTO 已封装；没有 transport/UI |
| Delivery | façade → `DeliveryService` + Fake/Termux adapters | 可显式触发且幂等；真实 Termux 仍需 authorized dispatcher 装配 |
| Feedback | façade → `FeedbackService.record` | ownership、幂等、原子 Profile 更新均可从 public boundary 触达 |
| Profile 改变 | SQLite atomic interaction/profile update | production-shaped，并由 façade 返回 safe Profile DTO |
| 下一次 Digest 改变 | 下一 run 读取新 projection | façade Product E2E 已证明 explainable `profile_weight` 改变 |

因此，business journey 已收敛到 `DigestApplication`；当前仍缺的“用户入口”是 CLI/HTTP/UI transport，
而不是另一个业务 service。`tools/brave_search_smoke.py` 和 `tools/vertex_digest_smoke.py` 仍是集成诊断，
不是产品入口。

## 生命周期与边界判断

### Application façade

当前已有 composition boundary `DigestApplication`，提供按 local user 隔离的 create/list/update/disable、
run/recover、Digest query、delivery、feedback 与 profile use cases。它把内部状态映射成 safe application
reason，且 public DTO 不含 Harness Result/Artifact/Evidence/audit/SQLite row。未来 transport 只负责解析与
序列化，不能重新拼装 repository/workflow。

HTTP 不是领域正确性的前提，但若目标是“用户实际操作”，loopback-only HTTP 是最薄、最通用的 façade；
也可以先用同一 façade 做 CLI。不要让 route handler 直接拼装 repository 和 workflow。

### Subscription 与 Digest lifecycle

Subscription lifecycle 已覆盖 create/get/list/update/enable/disable 与 user scope；硬删除明确不做，使用
disable 保留历史 Digest。更新递增 `version`、更新时间、重新验证 normalized fields，并用 expected version
检测并发冲突。

Digest generation 有 durable run reservation 和 terminal Digest，但 query side 不完整：缺 list by user/
subscription、get with ownership、run/delivery summary。Digest 应保持 immutable；V1 不需要 edit/delete。
同一 `(subscription_id, period_key)` 会复用已有 run，适合 scheduled period，却意味着同一天用户点击
“Run again”不会重新搜索。产品 façade 必须明确 Run now 的 request identity：默认幂等重放，或显式新建
manual period key；不能让 UI 随机制造重复执行。

### Duplicate、idempotency 与事务

基础设计总体健康：run 由 `(subscription_id, period_key)` 唯一约束；候选按 URL/title 去重；Feedback
由稳定 event key 派生 identity；Delivery 由 `(digest_id, channel)` 唯一，unknown 禁止盲重试；retry
attempt 有 compare-and-swap 风格检查。

SQLite 的局部 transaction boundary 也健康：Digest + seen content + run terminal state 同事务；Feedback
event + weights + profile version + update 同事务；Delivery reservation/retry/状态迁移分别短事务，外部 dispatch
不在数据库事务内，并在 dispatch 前持久化 unknown crash fence。

仍有两个 release 风险：

1. Unbound reserved、bound/no-event 与 terminal Result projection 已有显式 recovery；有 Harness durable
   events 但无 Result 时仍只能 `recovery_required`，尚无 reconciliation UI/operation。
2. Recovery 是 SQLite 单实例、显式 operator 语义，不是 distributed lease；Demo 必须避免多进程同时管理
   同一 database。

SQLite 单进程、低并发 Demo 足够；当前没有必要换数据库。后续 façade 应统一 connection error 映射，并为
subscription update 增加原子版本检查。

## 配置、失败与用户体验

Real Brave/Vertex 配置目前是安全但偏开发者体验：环境变量缺失会 fail closed，secret 不进入日志，adapter
有稳定 taxonomy；但没有启动时 readiness check、非敏感配置摘要或“搜索/模型是否可用”的管理状态。
Demo 启动应只报告 provider/model identity、endpoint host、配置 present/invalid，不显示 key；生产路径不应要求
用户运行 smoke script 才知道配置错误。

应用用户应看到可行动的状态，而不是 Harness 细节：

| Safe product state | 用户呈现 | 内部映射示例 |
|---|---|---|
| `configuration_required` | 管理员需配置 Search/LLM | `CONFIGURATION_ERROR`, `AUTH_FAILED` |
| `temporarily_unavailable` | 暂时无法生成，可稍后重试 | timeout/rate/network |
| `no_content` | 本期没有满足条件的内容 | empty/no ranked candidates |
| `content_rejected` | 内容未通过质量约束 | invalid model response/Output Contract |
| `generated` | Digest 可查看 | authoritative completed + persisted Digest |
| `delivery_failed` / `delivery_unknown` | Digest 仍可查看；分别允许显式重试/要求人工确认 | Delivery certainty |

API 可以返回 opaque run/digest IDs 和 safe reason；详细 Harness Result、Evidence、Artifact path、prompt、raw
provider response 只进入本地诊断/admin view。Generation incomplete 不能伪装为成功空 Digest，delivery failure
也不能反向改写 completed generation。

## Scheduler、HTTP 与 Web UI

- **Scheduler：当前不需要。** Run now 足以定义一个诚实 Demo Release，并更容易展示完整 Agent loop。先把
  lifecycle、入口和失败恢复做完整；scheduler 只是在稳定 use case 上产生 run request，不能补产品缺口。
- **HTTP API：需要 application façade，但不绝对要求 HTTP。** 为了浏览器 UI 和可复现演示，推荐 stdlib、
  loopback-only、单用户的薄 HTTP transport；authority 和业务规则全部留在 application services。
- **极薄 Web UI：推荐但不是第一步。** façade 稳定后，一页 UI 即可让用户创建/暂停订阅、Run now、查看
  Digest/source、投递、liked/dismissed 并看到下一次排序变化。不做前端框架、登录或实时推送。

## Harness 边界

没有发现需要为 Demo Release 修改 `mini_harness_core` 的产品理由。Search/Vertex adapters、ranking、Feedback、
Delivery 和 SQLite 都在 app 层；Harness 继续拥有 action policy、Evidence、Artifact/Output Contract 和 Result
completion authority，这是正确边界。

现有 application workflow 直接导入多个 Harness store/dispatch primitive，属于 integration code 而不是业务
规则泄漏；但这些类型若直接出现在未来 handler/JSON 就会造成 Harness 细节泄漏。新增 façade 应包住
`ApplicationResult`，只输出产品 DTO 和 safe diagnostic reference。不要为了 HTTP/UI 在 Harness 增加
subscription、delivery 或 user session 概念。

## 下一阶段候选排序

> 2026-08-23 checkpoint：A 的 application boundary、B、C，以及 H 的 startup readiness 已完成；A 中的
> HTTP transport 与 H 中的 ambiguous reconciliation 未做。以下表格保留 review 时的优先级依据。

评分 1–5：产品/教学/工程/必要性越高越好；复杂度/风险越高表示成本越大。

| 排名 | 候选 | 产品完整度 | 教学价值 | 工程价值 | 复杂度 | 风险 | 当前必要性 | 判断 |
|---:|---|---:|---:|---:|---:|---:|---:|---|
| 1 | A. HTTP/Application façade | 5 | 4 | 5 | 3 | 3 | 5 | 让已有能力成为稳定 use cases；HTTP 仅作 loopback transport |
| 2 | B. Subscription lifecycle | 5 | 3 | 5 | 2 | 2 | 5 | 补 update/disable/user scope/version，才能称订阅产品 |
| 3 | C. Product E2E | 5 | 5 | 5 | 3 | 2 | 5 | 从用户入口验证 feedback 后下一 Digest 改变，是 release gate |
| 4 | H. Readiness/recovery slice | 4 | 5 | 5 | 3 | 4 | 4 | startup config status、stale reserved run 与 projection recovery |
| 5 | E. Minimal Web UI | 4 | 3 | 3 | 3 | 2 | 显著提升演示性，但应消费稳定 façade |
| 6 | F. Observability/admin | 3 | 4 | 4 | 3 | 3 | 先做 safe health/run status，不做完整 dashboard |
| 7 | D. Scheduler | 3 | 3 | 3 | 4 | 4 | 2 | Run now 已足够；过早加入会放大恢复与重复语义问题 |
| 8 | G. More Agent capabilities | 1 | 2 | 2 | 5 | 5 | 1 | 当前瓶颈不是模型能力，不应增加 routing/tools/autonomy |

该建议 slice、thin CLI/safe readiness 与安全 admin recovery operation 已完成。真正 ambiguous Harness
effect 仍必须留给更低层 reconciliation；产品下一自然 slice 可复用 bootstrap/façade 增加 loopback-only
HTTP 与极薄 Web UI。

## 最小 Demo Release 定义

用户通过 loopback 本地入口可以：

1. 输入自然语言创建订阅，查看、修改、暂停或恢复自己的订阅；
2. 点击 Run now，看到 running/generated/no-content/temporarily-unavailable 等安全状态；
3. 用 Real Brave + Real Vertex 生成并查看带 source links 的 contract-valid Digest；
4. 手动发送一次本地 notification，并查看 accepted/failed/unknown；
5. 对 Digest item 点 liked/dismissed，查看可解释 Profile weight 变化；
6. 用新的 manual period 生成下一份 Digest，并看到 deterministic ranking 因 Feedback 改变；
7. 重复提交 run/feedback/delivery 时不会产生未声明的重复效果。

Demo Release 明确不做：登录/多租户、互联网暴露、scheduler/background worker、streaming、第二 Search/LLM
provider、模型路由、prompt optimization framework、复杂前端、移动端 App、自动处理 unknown delivery、生产级
HA/迁移/备份。真实外部服务只提供 integration confidence；Fake Search/FakeProvider 仍是 deterministic
correctness gate。

## Release 判断

上述 **85% / 55%** 是 loopback HTTP/Web UI 完成前的历史估算，不再代表当前 checkpoint。当前 Phase 3
Demo 已具备 loopback Web journey；release evidence closure 以本页矩阵中的五类证据为准。Automated
Browser-Engine E2E 仍明确不在本次 release gate 内，scheduler 与更多 Agent 能力也不是封板门槛。

上一页：[`13-first-vertical-slice.md`](13-first-vertical-slice.md) · 返回：[`README.md`](README.md)
