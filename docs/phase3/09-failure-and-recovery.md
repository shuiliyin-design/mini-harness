# Failure and Recovery

## Truth ownership

Application failure status 描述业务 lifecycle；Harness Result 描述该 Run 的 authoritative truth。
应用必须保存 `harness_run_id/status/reason`，不能把 `blocked` 改写成 `failed`，也不能因 delivery
失败把 completed generation 改写为未生成。

## Failure matrix

| Case | Bounded retry | Terminal semantics | Persistence/recovery |
|---|---|---|---|
| Search network/5xx | 是，同 action/retry policy 内 | exhausted -> `failed` | 无 accepted candidates，不生成 Digest |
| Search timeout/outcome ambiguous | 仅 read-only 可 fresh retry | exhausted -> `failed` | 保留 attempts/observations |
| No search results / no fresh candidates | 否 | `incomplete` (`no_content`) | 保存 DigestRun，不造空 Digest |
| LLM provider transient failure | 是，固定预算 | exhausted -> `failed` | 保留 Run truth，无 Artifact |
| Output too long | 最多固定 regeneration 次数 | exhausted -> `incomplete` | 保存 rejection reasons |
| Invalid candidate/source refs | 最多固定 regeneration 次数 | exhausted -> `incomplete` | 不 materialize/accept Artifact |
| Delivery explicit failure | 仅显式 retry | generation 仍 completed；delivery `failed/not_started` | 保留 Digest，建 attempt N+1 |
| Delivery timeout/crash after dispatch | 禁止 blind retry | delivery `unknown` / operation `blocked` | 先 reconciliation 或人工决定 |
| Duplicate manual run | 否 | 返回 existing，不是 failure | `(subscription_id, period_key)` unique |
| SQLite failure before Digest commit | 是，读取 Result/Artifact 后幂等投影 | app `failed` until recovered | 不重跑 Search/LLM |
| Commit succeeded, response lost | 否 | 返回 existing Digest | unique run/artifact keys 判定已提交 |
| Digest committed, delivery pending | 只恢复 delivery | `generated_not_delivered` | 不重做 generation |
| Feedback SQLite transaction failure | 可用同 event key 重试 | feedback request failed | Interaction/ProfileUpdate/weights 全部回滚；旧 Digest/Result 不变 |

`blocked` 用于需要外部事实/人工决定且无法安全继续的情况，尤其 side-effect outcome unknown。
`incomplete` 表示执行诚实结束但产品 contract 未满足。`failed` 表示执行/基础设施错误在允许
retry 后仍失败。应用不把“内容质量一般”随意提升成 Harness failure。

## Duplicate and transaction design

1. 在调用 Harness 前事务插入 `digest_runs(reserved)`，唯一键是 subscription + period key。
2. 冲突时加载已有 DigestRun；running/terminal 都不创建第二个 Run。
3. Harness Run ID 一旦绑定就不可替换；resume 使用同一 identity。
4. completed Result 之后，用一个 SQLite transaction 写 Digest、Items、SourceRefs、seen content，
   并把 DigestRun 标为 generated。
5. projection transaction 可安全重试，因为 `harness_run_id`、`artifact_id`、`digest_id` 均唯一。
6. DeliveryRecord/attempt 在独立 transaction 中创建，避免外部通知位于 SQLite transaction 内。
7. dispatch 前把 attempt 从 `pending/not_started` 持久化为 `unknown/unknown`；terminal write 失败时
   保留 unknown，不能通过普通 retry 重发。

不使用自动 outbox worker。教学 V1 可在 request 尾部同步 drain 一次 pending delivery，并提供
显式 resume/manual retry；SQLite 仍保留恢复所需 durable intent。

## Partial persistence safety

SQLite 开启 foreign keys；每个 aggregate update 使用 explicit transaction。若 Artifact 已 accepted
而 SQLite 不可写，Application 只重读 immutable Result/Artifact 并重试 projection，绝不重新搜索
或生成。若 notification 已 dispatch 但 DeliveryRecord 终态未保存，读取 Harness action/Evidence
做 reconciliation；无法证明 applied/not_applied 就保持 unknown。

当前 Delivery slice 不实现 reconciliation engine。`unknown` 只能查询和人工处理；只有 adapter 明确
返回 `not_started` 的 `failed` 才允许显式新建下一 attempt。Delivery failure/unknown 从不更新
`digest_runs.harness_result_json`、Digest Artifact 或 canonical payload。

Feedback transaction 也遵守相同原则：stable `feedback_id` 允许安全重放；只有 Interaction、全部
topic weights、Profile head 与 ProfileUpdate 一起 commit 后，应用才返回 `applied=true`。

上一页：[`08-delivery-and-feedback.md`](08-delivery-and-feedback.md) · 下一篇：
[`10-testing-and-e2e.md`](10-testing-and-e2e.md)
