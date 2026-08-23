# Phase 3 Overview

## 产品链路

```text
Natural-language request
  -> validated Subscription
  -> manual run reservation
  -> query proposal
  -> Brave Search Observation
  -> accepted Evidence + normalized candidates
  -> deterministic freshness/dedup/ranking
  -> model DigestDraft
  -> deterministic validation
  -> workspace Digest Artifact
  -> authoritative Harness Result
  -> SQLite Digest projection
  -> DeliveryRecord / Termux notification
  -> Feedback / Interaction
  -> deterministic InterestProfile update
  -> next run safe profile projection
```

Brave、LLM 与 Termux 都是可替换边界，不是 domain object。Model 只提出解析、query、摘要
与解释候选；Application/Harness 决定结构是否合法、哪些候选可选、是否满足 contract、是否
允许执行、Result 是什么状态。

## 推荐 app tree

V1 从少量模块开始，不创建 plugin framework 或通用 workflow DSL：

```text
apps/digest_agent/
  README.md
  domain.py
  contracts.py
  repositories.py
  services.py
  workflows.py
  adapters/
    sqlite.py
    search.py
    provider.py
    workspace.py
```

- `domain.py` 无 Harness、HTTP、SQLite、Brave 或 Termux import。
- `contracts.py` 放纯验证和序列化规则，不放 prompt。
- `repositories.py` 只声明应用需要的 ports。
- `services.py` 处理 Subscription CRUD 与 application-owned Feedback/Profile update。
- `workflows.py` 组合一个 Digest generation Harness Run，不复制 Agent loop。
- `adapters/` 实现 ports；真实/假的实现使用同一输入输出结构。
- Delivery adapter 已实现 Fake correctness 与 authorized Termux mapping；HTTP 尚未实现。

## 三层 ownership

| 层 | Owns | 不拥有 |
|---|---|---|
| Application | User、Subscription、Profile、Candidate、Digest lifecycle、Delivery、Feedback、SQLite、acceptance rules | Tool Authority、Evidence persistence、Harness Result truth |
| Harness | Agent execution、Policy/Approval、tool invocation、Plan/Retry、Observation/Evidence、Artifact acceptance、Result | 用户/订阅业务状态、推荐策略、数据库 schema |
| Model | language parse/query/synthesis/explanation candidate | persistence、ranking authority、contract verdict、delivery truth |

Phase 1 不依赖本应用；Phase 2 Mobile/Termux 只是可选的真实 delivery integration，不是产品
方向。依赖只能从 app 向下，不能由 core、Bridge 或 Environment 反向导入 app。

## V1 execution split

普通 CRUD 不进入 Agent Run。`create subscription` 可以调用 parser 得到结构候选，但由应用
validator 与用户输入决定是否提交；它仍是普通 application transaction。

`generate_digest(subscription_id)` 才进入 Harness：需要外部搜索、模型决策、可恢复执行、
Evidence、Artifact 与 Authoritative Result。成功 Result 之后，应用才把 Artifact 投影为 SQLite
Digest。Delivery 单独记录，不能反写生成 Result。

第一条切片由固定 application workflow 调用现有 sealed Authority/dispatch，然后使用 Harness
Evidence constructors/store 完成 normalization acceptance；因此无需修改 core。若下一条切片要求
真实 Brave 由通用 Agent loop 自主调用，仍需重新评估 post-observation verifier seam。

下一篇：[`01-product-scope.md`](01-product-scope.md)
