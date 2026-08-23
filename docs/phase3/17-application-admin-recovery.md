# Application Admin Recovery

## Principle

**Admin recovery chooses safe paths already proven by durable state; it does not create truth.** 管理员不能输入
任意目标状态，不能 force-complete/force-fail、assume-not-applied、rerun-anyway 或创建新 Harness run。

`DigestApplication.inspect_run_recovery` 返回 `RecoveryInspection`，只含 application run/status/reason、
binding status、bounded Harness status、terminal Result availability、safe action allowlist 与 blocking reason。
它不返回 Harness run ID、Result/Evidence/Observation/Audit payload、checkpoint、provider body、secret 或 traceback。

## Durable fact classes

| Durable facts | Binding | Harness durable state | Effect certainty | Safe action |
|---|---|---|---|---|
| reserved，零 event/Result | unbound | not started | `not_started` | `resume_original_run` |
| running，binding 已提交，零 event/Result | bound | bound, not started | `not_started` | `resume_bound_run` |
| terminal authoritative Result，应用投影缺失 | bound | terminal | authoritative terminal | `repair_projection` |
| Harness events 存在但无 terminal Result | bound | started, nonterminal | `unknown` | none |
| terminal record 无法验证或事实不足 | any | invalid/unknown | `unknown` | none |

前三条只能复用原 `application_run_id` 与预分配/已绑定 `harness_run_id`。Projection repair 读取 immutable
Result/Artifact 并写 SQLite Digest；不 Search、不调用 LLM、不执行 Harness、不触发 Delivery。后两条返回
`NO_SAFE_AUTOMATIC_RECOVERY`，保持 `recovery_required`，等待更低层 Harness reconciliation。

## Admin operation and concurrency

`execute_run_recovery(application_run_id, action)` 先重新 inspection；action 必须属于当前 allowlist。SQLite
schema v5 的 `recovery_operations` 用 `SHA256(application_run_id, action)` stable identity 和 unique
`(application_run_id, action)` claim 单实例 owner。重复 completed action 返回 `recovered`；并发 follower 返回
`already_recovering`，不会产生第二个 Run、Harness binding、Digest、Search/LLM 或 Delivery。

最小 application recovery audit 只保存 operation identity、application run、selected action、before/after state、
started/completed timestamp、safe status/error code。它不复制 Harness Audit 或 terminal Result body。

若 projection repair 再失败，operation 记 safe `recovery_operation_failed`，application run 保持
`recovery_required`；原 Harness Result/Artifact truth 不被覆盖。失败的 stable operation 仍幂等，不用新 key
绕过事实。

## Worked traces

```text
A. reserved/unbound -> inspect(resume_original_run)
   -> claim -> original logical/Harness identity -> completed once

B. terminal Result + missing SQLite projection
   -> inspect(repair_projection) -> Result/Artifact verify -> Digest commit
   -> Search calls 0, Provider calls 0, Delivery calls 0

C. bound + Harness event + no terminal Result
   -> inspect(actions=[]) -> NO_SAFE_AUTOMATIC_RECOVERY
   -> remains recovery_required; no blind rerun
```

Admin CLI 使用：

```bash
python -m apps.digest_agent.cli --json run-recovery-inspect \
  --application-run-id RUN_ID
python -m apps.digest_agent.cli --json run-recovery-execute \
  --application-run-id RUN_ID --action repair_projection
```

普通 `run-status` 只显示 `recovery_required` 与短 reason，不展示 admin action。HTTP/Web 第一版无需暴露 admin
control。本 slice 不实现通用 recovery engine、distributed lock、scheduler 或新 Harness recovery semantics。

上一页：[`16-cli-bootstrap-and-readiness.md`](16-cli-bootstrap-and-readiness.md) · 返回：[`README.md`](README.md)
