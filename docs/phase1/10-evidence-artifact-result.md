# Observation、Evidence、Artifact 与 Authoritative Result

## 读完你应该理解什么

- 一次 Tool Observation 如何成为可追踪 Evidence，再支持 Artifact、Output Contract 和 Authoritative Result。
- integrity、freshness、Current Reality satisfaction 为什么是不同判断。
- 模型的 Final Answer 为什么只能提供 candidate presentation，不能决定 completion。

## Scope / Not Scope

本篇覆盖 workspace file Artifact、Verification/Reconciliation Evidence、Output Contract 和 Result Binding。

本篇不保存文件正文或 raw Tool output，不把历史 Artifact 当当前 filesystem snapshot，也不支持模型降低 Output
Contract 要求。

## 真实模块与关键函数

- [`observation.py`](../../mini_harness_core/observation.py)：`observation_digest`、
  `persisted_safe_observation`、`model_context_observation`。
- [`verification.py`](../../mini_harness_core/verification.py)：`verification_observation_identity`、
  `replay_verification_transition`。
- [`evidence.py`](../../mini_harness_core/evidence.py)：`create_verification_evidence`、
  `create_tool_observation_evidence`、`evidence_gate`、`evidence_integrity_check`。
- [`artifacts.py`](../../mini_harness_core/artifacts.py)：`observe_workspace_file`、`create_artifact`、
  `evaluate_artifact_contract`、`current_output_contract_gate`、`select_supersession`。
- [`result.py`](../../mini_harness_core/result.py)：`normalize_final_candidate`、
  `build_authoritative_result_state`、`bind_final_result`、`result_integrity_check`。
- [`agent.py`](../../mini_harness_core/agent.py)：`_process_observation`、`_finalize_runtime_artifact`、
  `_handle_final_candidate`、`_emit_runtime_result`。

## 核心状态/数据结构

### Observation

raw Observation 是一次环境返回值。跨 persistence/context boundary 前，它变成 safe identity：exit code、
stdout/stderr length+SHA-256、allowlisted `cwd/path/status` 等；raw bytes 不进入 Session、Evidence 或 Bundle。

### Evidence

Evidence 是 immutable provenance record：subject claim、source action/request/event、Harness verification
decision、freshness scope、Observation/Artifact identity。`EVIDENCE_TYPES` 包括 tool observation、verification、
reconciliation、subagent return、MCP observation、reasoning result。

### Artifact

Artifact 是某个 workspace file version 的 immutable metadata：safe relative path、SHA-256/size、producer、
Evidence IDs、contract outcome 和 optional `supersedes_artifact_id`。状态只有 `proposed`、`materialized`、
`verified`、`accepted`、`rejected`。

### Output Contract

Output Contract 是 Harness-owned required artifacts 列表。每项可要求 `exists`、`non_empty`、
`content_identity`、`verified`，并可绑定 Plan step。模型不能删除或降低 requirements。

### Authoritative Result

Result status 只有 `completed`、`blocked`、`failed`、`cancelled`、`incomplete`。它绑定 safe answer、accepted
Artifact/Evidence IDs、Plan identity、reason 和模型 candidate identity/contradiction。

## 从 Observation 到 completion

```text
raw Observation
  -> safe Observation identity
  -> Verification transition
  -> immutable Evidence
  -> Artifact version + producer/evidence links
  -> Output Contract evaluation
  -> Current Reality gate
  -> Result Binding
  -> Authoritative Result

Model final_answer --------------------^ candidate only
```

三组不可替换的判断：

```text
Evidence Integrity != Evidence Freshness
Artifact Integrity != Current Output Satisfaction
Final Answer        != Completion Authority
```

## Worked Trace：`report.md` 完成交付

```text
Output Contract
  report.md requires exists + non_empty + content_identity + verified
    |
    v
Model Intent: echo hello > report.md
  -> ASK + Human Approval
  -> AuthorizedAction
  -> write succeeds
  -> pending Artifact content identity captured
  -> requires_verification=true
    |
    v
Model Intent: cat report.md
  -> read_only + related target
  -> Fresh Observation stdout="hello\n"
  -> replay_verification_transition accepted=true
  -> Verification Evidence binds source action + observation event
    |
    v
_finalize_runtime_artifact
  -> Evidence identity matches pending file identity
  -> Artifact status=accepted
  -> artifact contract transition recorded
    |
    v
current_output_contract_gate
  -> re-observe report.md identity
  -> requirements satisfied
    |
    v
Model final candidate claimed_status=completed
  -> build_authoritative_result_state
  -> bind_final_result
  -> Result status=completed
```

如果 `cat` 成功但内容 identity 与 pending Artifact 不匹配，Verification/contract 不应接受旧 candidate；Tool
success 本身不足以完成交付。

## Drift Worked Trace：A1 不再满足 Current Reality

```text
Run A
  Artifact A1(report.md, sha256=H1, accepted)
  -> artifact_integrity_check(A1)=MATCH

Current workspace
  report.md changed to sha256=H2
  -> A1 historical integrity still MATCH
  -> current_output_contract_gate re-observes H2
  -> H2 != H1
  -> Current Contract unsatisfied: current_content_identity

Run B / new grounding
  -> Fresh Observation of report.md
  -> new Verification Evidence for H2
  -> Artifact A2(path=report.md, sha256=H2)
  -> select_supersession chooses A1
  -> A2.supersedes_artifact_id=A1
  -> A2 accepted only after its own contract checks
```

A1 没有被修改或变成 MISMATCH；它仍诚实描述 Historical Reality。A2 通过 immutable version link 表示新的
现实版本。Supersession 要求 same path、different identity、old Artifact integrity valid 且无 cycle。

## Key Invariants

1. raw Observation 在 persistence/context boundary 前必须投影。
2. accepted filesystem Evidence 必须来自 relevant read-only Fresh Observation。
3. cross-run/historical Evidence 不能通过 `evidence_gate(..., current_reality=True)`。
4. Artifact 保存 identity/provenance，不保存文件正文。
5. Artifact acceptance 必须匹配 exact Output Contract path/requirements 和 Evidence identity。
6. Current Reality mismatch 创建新 Observation/Evidence/Artifact，不修改历史 Artifact。
7. Result status 由 Harness state binding；模型 claimed status 只用于 contradiction detection。
8. completed Result 引用的 Artifact/Evidence 必须通过历史 integrity closure。

## Failure / Edge Cases

- Observation 含 secret：projection 只保留 digest/length，raw marker 不进入 historical objects。
- Verification command read-only 但 target unrelated：Verification Quality gate 拒绝。
- Evidence fingerprint MATCH 但来自旧 Run：可证明历史完整性，不能证明 Current Reality。
- Artifact path 为 absolute、escape、symlink、secret filename 或 `.audit`：拒绝。
- Artifact contract pure replay MATCH，但当前文件已删除/改变：current gate unsatisfied。
- Output Contract 缺 required Artifact：Result 倾向 `incomplete`，模型 completed claim 形成 contradiction。
- Result persistence 失败：不能重做 Tool；返回 safe incomplete/blocked semantics 并保留已有 forward truth。

## Review Anchors

- `persisted_safe_observation`：allowlist 是否可能带出 raw result/secret。
- `evidence_gate`：current run、freshness scope、subject relevance 和 accepted decision 是否同时检查。
- `replay_artifact_contract_transition`：是否只消费 immutable identities，不访问当前文件。
- `current_output_contract_gate`：是否明确重新读取 Current Reality，且不修改旧 Artifact。
- `validate_supersession`：same path/different identity/integrity/no-cycle 是否齐全。
- `build_authoritative_result_state`/`bind_final_result`：Model candidate 是否可能覆盖 run control、Plan、Verification 或 Contract。

## Common Misreadings

- **“Evidence fingerprint MATCH，所以证据仍新鲜。”错误。** integrity 与 freshness 分离。
- **“Artifact accepted，所以当前文件仍满足要求。”错误。** 需要 Current Reality gate。
- **“cat exit 0 就完成 Verification。”错误。** 还需 target related 和 identity match。
- **“Model final answer 写 completed，Result 就 completed。”错误。** Harness binding 决定 status。
- **“更新文件就修改 A1。”错误。** 历史版本 immutable；创建 A2 并 supersede。

## 交付对象 CLI

下面的命令只检查或展示已持久化对象；它们不会重新执行 Tool：

```bash
python mini_harness.py --evidence-show EVIDENCE_ID
python mini_harness.py --evidence-trace EVIDENCE_ID
python mini_harness.py --evidence-check EVIDENCE_ID
python mini_harness.py --artifact-show ARTIFACT_ID
python mini_harness.py --artifact-trace ARTIFACT_ID
python mini_harness.py --artifact-check ARTIFACT_ID
python mini_harness.py --outputs RUN_ID
python mini_harness.py --result-show RUN_ID
python mini_harness.py --result-check RUN_ID
```

`*-check` 的 MATCH 是 historical integrity 结论。若要判断当前文件是否仍满足 Output Contract，仍需 fresh Observation 和 current gate。

## 与其他文档的链接

- Agent finalization：[`02-agent-loop.md`](02-agent-loop.md)
- Durability/Reconciliation：[`06-durability-and-recovery.md`](06-durability-and-recovery.md)
- Historical object map：[`09-audit-and-historical-objects.md`](09-audit-and-historical-objects.md)
- Replay levels：[`11-replay-and-bundles.md`](11-replay-and-bundles.md)
- Failure semantics：[`13-failure-semantics.md`](13-failure-semantics.md)

## Navigation

- Previous: [`09-audit-and-historical-objects.md`](09-audit-and-historical-objects.md)
- Next: [`11-replay-and-bundles.md`](11-replay-and-bundles.md)
- Related: [`13-failure-semantics.md`](13-failure-semantics.md), [`17-glossary-and-state-reference.md`](17-glossary-and-state-reference.md)
