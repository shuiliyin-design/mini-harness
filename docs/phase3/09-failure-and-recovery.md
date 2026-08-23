# Failure and Recovery

## Truth ownership

Application failure status 描述业务 lifecycle；Harness Result 描述该 Run 的 authoritative truth。
应用必须保存 `harness_run_id/status/reason`，不能把 `blocked` 改写成 `failed`，也不能因 delivery
失败把 completed generation 改写为未生成。

## Failure matrix

| Case | Bounded retry | Terminal semantics | Persistence/recovery |
|---|---|---|---|
| Search network/5xx | policy 可选择；当前 slice 不自动 retry | 当前 Run `incomplete` | 无 accepted candidates，不生成 Digest |
| Search timeout | read-only 可 fresh retry；当前 slice 不自动 retry | 当前 Run `incomplete` | 保留 safe observation identity |
| No search results / no fresh candidates | 否 | `incomplete` (`no_content`) | 保存 DigestRun，不造空 Digest |
| LLM timeout / structured parse failure | 最多一次 fresh generation retry，总 deadline 125s | exhausted -> `incomplete` | safe attempt metadata；无 Artifact/Digest |
| Output too long | 否；属于 deterministic contract rejection | `incomplete` | 保存 safe rejection reason |
| Invalid candidate/source refs | 否；属于 deterministic contract rejection | `incomplete` | 不 materialize/accept Artifact |
| Delivery explicit failure | 仅显式 retry | generation 仍 completed；delivery `failed/not_started` | 保留 Digest，建 attempt N+1 |
| Delivery timeout/crash after dispatch | 禁止 blind retry | delivery `unknown` / operation `blocked` | 先 reconciliation 或人工决定 |
| Duplicate manual run | 否 | 返回 existing，不是 failure | `(subscription_id, period_key)` unique |
| SQLite failure before Digest commit | 是，读取 Result/Artifact 后幂等投影 | app `failed` until recovered | 不重跑 Search/LLM |
| Commit succeeded, response lost | 否 | 返回 existing Digest | unique run/artifact keys 判定已提交 |
| Digest committed, delivery pending | 只恢复 delivery | `generated_not_delivered` | 不重做 generation |
| Feedback SQLite transaction failure | 可用同 event key 重试 | feedback request failed | Interaction/ProfileUpdate/weights 全部回滚；旧 Digest/Result 不变 |

## Brave Search error taxonomy

Adapter 只暴露 allowlisted safe code，不保存 error body。它自己不 sleep、不 retry；retryability
只是交给 Harness/application policy 的候选事实：

| Code | Trigger | Retry candidate | Generation outcome |
|---|---|---:|---|
| `CONFIGURATION_ERROR` | env key 缺失/无效 | 否 | authoritative incomplete；无 accepted Evidence/Digest |
| `TIMEOUT` | bounded HTTP timeout | 是 | 当前 Run incomplete；未来预算内可 fresh retry |
| `RATE_LIMITED` | HTTP 429 | 是 | incomplete；只保留 bounded `retry_after_seconds`，adapter 不 sleep |
| `AUTH_FAILED` | HTTP 401/403 | 否 | authoritative incomplete；先修 credential |
| `NETWORK_ERROR` | DNS/TLS/socket/other transport error 或 5xx | 是 | 当前 attempt incomplete |
| `INVALID_RESPONSE` | status/schema/UTF-8/JSON/type 无效 | 通常否 | incomplete；不创建 accepted Evidence |
| `OVERSIZED_RESPONSE` | body 超固定 byte cap | 否 | incomplete；raw body 丢弃 |
| `EMPTY_RESULTS` | valid response 但 normalized results 为空 | 否 | expected incomplete/no content |

3xx 不跟随并映射 `INVALID_RESPONSE`。只有 429 的 `Retry-After` 若能安全解析为非负、bounded
seconds 才进入 response metadata；其他 response headers 全部丢弃。任一失败都可以留下 untrusted
observation identity 供诊断，但不会产生 accepted candidate-set Evidence、Artifact 或成功 Digest。

表中的 retry candidate 是 taxonomy，不表示 adapter 或当前 workflow 已实现 retry；adapter 从不 sleep，
当前 fixed workflow 每次 manual run 只 dispatch 一次。`blocked` 用于需要外部事实/人工决定且无法安全继续的情况，尤其 side-effect outcome unknown。
`incomplete` 表示执行诚实结束但产品 contract 未满足。`failed` 表示执行/基础设施错误在允许
retry 后仍失败。应用不把“内容质量一般”随意提升成 Harness failure。

## Vertex Provider error taxonomy

Vertex adapter 同样只暴露 allowlisted code；原始 model/error body 不保存，adapter 不 sleep、不 retry。
Application workflow 可以在 125 秒总 deadline 内进行最多一次 generation retry：

| Code | Trigger | Retry candidate | Current outcome |
|---|---|---:|---|
| `CONFIGURATION_ERROR` | `LLM_*` 缺失、mode/HTTPS config 无效 | 否 | authoritative incomplete |
| `AUTH_FAILED` | HTTP 401/403 | 否 | incomplete；先修 credential |
| `TIMEOUT` | local timeout 或 HTTP 408/504 | 是 | incomplete |
| `RATE_LIMITED` | HTTP 429 | 是 | incomplete；只保留 bounded Retry-After |
| `NETWORK_ERROR` | DNS/TLS/socket/5xx | 是 | incomplete |
| `INVALID_RESPONSE` | envelope/UTF-8/JSON/exact candidate schema 无效 | 仅 allowlisted structured subtype | incomplete；无 Artifact |
| `MODEL_REFUSAL` | content filter/safety/refusal finish | 否 | incomplete |
| `EMPTY_OUTPUT` | stop 但无文本 | 通常否 | incomplete |

`INVALID_RESPONSE` 进一步 durable 区分 `NON_JSON/JSON_PARSE/SCHEMA_MISMATCH`；timeout 记录
`MODEL_TIMEOUT`。Malformed JSON、Markdown/prose wrapper 在 adapter fail closed；too-long、duplicate item、unknown/reordered
candidate/source refs 则进入现有 deterministic Output Contract 并得到 authoritative incomplete。首次真实
Fake Search + Vertex smoke 暴露 fenced JSON；先加入 regression 保持 parser 严格，再把 completions
prompt 改为仓库已有 RealProvider 验证过的 assistant-prefill 形式，没有通过剥围栏放宽规则。

schema v7 的 `generation_attempts` 只保存 request/response length、SHA-256、status、finish reason、parse/schema
flags、safe subtype、token/latency metadata；JSON syntax 只额外保存六种 allowlisted lexical subtype 与
line/column。历史未记录 lexical subtype 的 run 保持 unknown，不从位置或摘要猜正文。Prompt、raw output
与 provider envelope 均不保存。Application 只对
`TIMEOUT` 与 `NON_JSON/JSON_PARSE/SCHEMA_MISMATCH` 进行一次同输入 fresh attempt，不 sleep；auth、refusal、
empty output 与 Output Contract rejection 不自动 retry。详见
[`19-llm-structured-output-reliability.md`](19-llm-structured-output-reliability.md)。

Output Contract rejection 不复用上述 retry。schema v8 在 application run 保存一个 deterministic primary
subtype（too-long/items、content/source ref、duplicate、topic/focus、required field、marker 或 other）和
bounded counts/limits/rule identity。它仍保持 `status=incomplete`、`stage=contract`、
`code=output_contract_failed`；旧 contract failure 没有 subtype 时继续 generic 展示，绝不从旧 reason 猜测。

## Real Brave smoke finding

2026-08-23 首次 credential-dependent smoke 的 Search normalization 成功并得到 3 条 safe rows，
但 Real Search + FakeProvider E2E 为 `incomplete`：Brave rows 没有 topic tags，而旧 domain 规则只接受
完整订阅主题逐字出现在 title/snippet 中，真实标题只命中 `Agent Engineering`，最终触发
`topic_focus_mismatch`。Smoke 随后又错误解引用不存在的 Digest，产生 `AttributeError`。

修复顺序是先加入两个 deterministic regressions，再实现保守多词 lexical match，并让 smoke 对
合法 incomplete 安全输出 reason 后返回 non-zero。相同 credential 重跑得到 3 条 normalized rows、
`completed` Harness Result、1 个 source ref 与 `228 <= 600` 的 Digest contract。该记录只说明当前
credential/network/API 时点可用；离线 regressions 才是长期 correctness gate。

## Duplicate and transaction design

1. 在调用 Harness 前事务插入 `digest_runs(reserved)`，唯一键是 subscription + idempotency key；period
   key 是内容周期，不承担 API request identity。
2. 冲突时加载已有 DigestRun；running/terminal 都不创建第二个 Run。
3. Harness Run ID 预分配但只有 `harness_bound_at` CAS 后才算绑定；recover 永远使用同一 identity。
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

Application run recovery 已由 generation integration seam 实现最小 truth table：unbound reserved 可显式
接管；bound 且没有 Harness durable event 可用同一 Harness ID 启动；terminal Result 只修 SQLite projection；
已有 event 但没有 terminal Result 一律 `recovery_required`。普通重复 run 不自动触发 recovery。

Admin recovery 不接受目标状态，只能执行 inspection 从 durable facts 派生的 `resume_original_run`、
`resume_bound_run` 或 `repair_projection`。歧义 effect/invalid terminal record 返回空 action 集与
`NO_SAFE_AUTOMATIC_RECOVERY`。schema v5 `recovery_operations` 提供 stable operation identity、单实例 claim
和最小 before/after audit；repair failure 不改写原 Harness terminal truth。

## Failure status is not failure provenance

人工 browser acceptance 发现两个 `incomplete` Run 的 Search Observation 与 candidate Evidence 都已接受，
但 Vertex 分别返回 `INVALID_RESPONSE` 与 `TIMEOUT`；旧 façade 只读取无 stage 的通用 `reason`，把所有
`TIMEOUT/RATE_LIMITED/NETWORK_ERROR` 猜成 `search_unavailable`。因此 status truth 正确，用户归因错误。

schema v6 在 application `digest_runs` 增加 nullable `failure_stage/failure_code`。Workflow 在仍知道 owner
时原子写入：Search failure 为 `search_*`，Provider failure 为 `generation_*`，contract failure 为
`contract/output_contract_failed`。`status` 仍由 authoritative outcome 决定，不因 provenance 改成 completed
或 failed。旧 row 两列保持 NULL；读取时只显示 `unknown_stage/legacy_failure`，不从旧通用 code 猜测或回写。

关键映射：Vertex timeout/invalid/rate-limit 分别为 `generation_timeout`、
`generation_invalid_response`、`generation_rate_limited`；auth/config 为 configuration stage 的
`generation_configuration_error`。Search timeout/network/invalid/empty 分别保持 search stage。Search succeeded
+ Generation failed 必须永远不投影成 search failure。

上一页：[`08-delivery-and-feedback.md`](08-delivery-and-feedback.md) · 下一篇：
[`10-testing-and-e2e.md`](10-testing-and-e2e.md)
