# Phase 3 Offline Application Baseline

本文封存 external-service integration 之前的 checkpoint：generation、Feedback/Profile/
Explainable Ranking 与 Delivery 三条 slice 均已离线实现。真实 Brave、真实 LLM、HTTP、scheduler
和真实设备 execution 不属于该 baseline。

## Implemented tree

```text
apps/digest_agent/
  __init__.py
  domain.py
  contracts.py
  repositories.py
  services.py
  workflows.py
  adapters/
    __init__.py
    search.py
    provider.py
    delivery.py
    sqlite.py
    workspace.py
tests/apps/
  test_digest_architecture.py
  test_digest_delivery.py
  test_digest_domain.py
  test_digest_feedback.py
  test_digest_repository.py
  test_digest_workflow.py
```

没有创建空 `api.py`、Brave client、真实 Provider 或 scheduler。Profile rules 留在 application
domain/service，Delivery adapters 独立于 generation workflow。

## Implemented flow

```text
SubscriptionService.parse_request
  -> deterministic Subscription validation
  -> SQLite subscription commit                 # no Harness Run

DigestGenerationWorkflow.run
  -> SQLite unique period reservation
  -> Harness sealed Fake Search dispatch
  -> SearchObservation (ephemeral raw rows)
  -> mcp_observation Evidence (untrusted)
  -> normalize / canonical URL / exact dedup
  -> verification Evidence (accepted candidate-set identity)
  -> deterministic score + max_items selection
  -> FakeDigestProvider Digest candidate
  -> evaluate_digest_contract
       fail -> no Artifact
       pass -> sealed materialize + read-only observe
            -> file verification Evidence
            -> accepted workspace Artifact
  -> run_agent final candidate / OutputContract / authoritative Result
  -> completed only: SQLite Digest projection
```

Provider 决定的是 payload candidate；它不能自行声明 length、source、Artifact 或 completed。应用
contract 失败时，provider 仍声明 completed 也只能得到 Harness `incomplete`。

## Search Observation → Evidence

Fake Search 是 MCP-shaped adapter，但返回对象先是 `SearchObservation`。Workflow 保存的第一条
Harness record 是 `mcp_observation`，其 `verification.untrusted_external=true`，不会进入 Result 的
accepted Evidence IDs。Normalization 完成后，candidate-set canonical identity 由 SHA-256 固定；
Harness `verification` Evidence 引用原 observation Evidence/action，并保存 accepted decision。
ContentCandidate/SourceRef 只能引用后者。

这证明了 ordering，但不是通用 Agent-loop extension。真实 Brave 下一切片必须先决定：固定
integration 是否仍足够；若 Model 自主 search 后需要回调 application verifier，才实现
default-off typed observation acceptor。

## SQLite schema v3

```text
schema_migrations
subscriptions
digest_runs          UNIQUE(subscription_id, period_key)
content_candidates   normalized safe payload only
digests              UNIQUE(harness_run_id), UNIQUE(artifact_id)
interest_profiles
profile_topic_weights
interactions
profile_updates
seen_content
delivery_records     UNIQUE(digest_id, channel)
delivery_attempts    UNIQUE(delivery_id, attempt_number)
```

Digest payload 以 canonical JSON 原子保存，绑定 `digest_run_id/harness_run_id/artifact_id`。失败或
incomplete Run 不写 Digest。重复 period 返回已有 ApplicationResult，不重新 Search/Model。v2 从 v1
前向增加 profile/feedback/seen-content 与 DigestRun profile projection；v3 再增加 delivery head 和
immutable attempt history，三个 migration 都记录在 `schema_migrations`。

## Feedback/Profile/Explainable Ranking

opened/liked/dismissed/saved 以稳定 feedback identity 幂等写入；Interaction、bounded topic weights、
Profile head 与 ProfileUpdate 在同一 SQLite transaction 提交。每次 generation 保存 safe profile
projection identity，排序保存 subscription/focus/profile/freshness/already-seen 五分量 breakdown。
liked/dismissed 的下一期升降由固定整数规则决定，不由 Model 授权。

## Delivery

completed Digest 可创建稳定 DeliveryRecord 与 attempt。Fake adapter 覆盖 accepted、explicit failure
和 unknown；unknown 禁止 blind retry，只有 failed/not_started 可显式创建下一 attempt。Termux adapter
只映射 safe preview 与既有 authorized Environment dispatcher 返回的 effect certainty；notification
accepted 不表示用户 opened。

## Output Contract

Application contract 确定性检查：exact schema、computed Unicode character count、max chars/items、
unique items、selected candidate membership、source URL/Evidence、marker closure 与 topic/focus tags。
PASS 后才注册 payload SHA-256 给固定 workspace adapter。Harness OutputContract 再检查文件
exists/non-empty/content identity/verification，并独立决定 Result status。

## Tests

- 48 个 application tests，全部 stdlib、无网络、无 Android、无真实 LLM；
- generation 覆盖 valid、overlong、unknown source、no results、duplicate period 与 Evidence ordering；
- feedback/profile 覆盖 stable identity、delta/clamp/rollback/safe projection 与下一期可解释升降；
- delivery 覆盖 accepted/failed/unknown、dedup、safe retry、persistence fence 与 Termux mapping；
- architecture 覆盖 core 不导入 app、domain 不导入 Harness/infrastructure、无 HTTP/network module。

## Core impact

三条 slice 都没有修改 `mini_harness_core`，也没有新增 Evidence/Artifact/Result schema、capability registry、
state machine 或 plugin framework。应用只向下调用现有 Harness seams，core 不 import app。

上一页：[`12-review-guide.md`](12-review-guide.md) · 回到：[`Phase 3 map`](README.md)
