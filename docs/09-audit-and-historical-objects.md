# Audit 与 Historical Objects

## 读完你应该理解什么

- Policy Snapshot、Manifest、Envelope、Audit、Evidence、Artifact、Result、Bundle 各自记录哪一种历史事实。
- integrity/replay 能证明什么，以及为什么都不能自动证明 Current Reality 或授予新 Authority。
- 一条 completed Run 如何形成可追踪、可检查、可移植的 reference closure。

## Scope / Not Scope

需求中称“七类历史对象”，但实际枚举了八类；本篇按真实八类说明。Bundle closure 还可能包含 Output
Contract，它在 [`10-evidence-artifact-result.md`](10-evidence-artifact-result.md) 单独讲解。

本篇覆盖历史 metadata、provenance、integrity 和 deterministic replay；不把 Bundle 当 Session backup，不从
历史对象恢复执行 Authority，也不声称记录 raw Tool output、完整 task、完整 context 或 hidden reasoning。

## 真实模块与关键函数

- [`policy_snapshot.py`](../mini_harness_core/policy_snapshot.py)：`build_policy_snapshot`、
  `persist_snapshot`、`replay_policy_events`、`policy_drift`。
- [`run_manifest.py`](../mini_harness_core/run_manifest.py)：`build_manifest`、`integrity_check`、
  `manifest_differences`。
- [`run_envelope.py`](../mini_harness_core/run_envelope.py)：`build_envelope`、`RunEnvelopeStore`、
  `envelope_integrity_check`、`harness_replay_check`。
- [`audit.py`](../mini_harness_core/audit.py)：`AuditWriter.append`、`read_events`、`format_timeline`、
  `explain_events`。
- [`evidence.py`](../mini_harness_core/evidence.py)：`EvidenceStore`、`evidence_integrity_check`、
  `evidence_trace`。
- [`artifacts.py`](../mini_harness_core/artifacts.py)：`ArtifactStore`、`artifact_integrity_check`、
  `artifact_trace`。
- [`result.py`](../mini_harness_core/result.py)：`ResultStore`、`result_integrity_check`。
- [`run_bundle.py`](../mini_harness_core/run_bundle.py)：`collect_reference_closure`、`export_run_bundle`、
  `check_bundle`、`replay_bundle`。

## 核心状态/数据结构：Truth Source 分类

这里的 “Authoritative for recovery” 特指：能否单独决定 action 是否安全继续/重放。Action checkpoint 与 live
Run Control 才拥有这类 runtime recovery truth；历史可检查不等于恢复授权。

| Object | Primary purpose | Mutable? | Authoritative for recovery? | Historical integrity? | Can prove Current Reality? | Can grant execution Authority? |
|---|---|---|---|---|---|---|
| Policy Snapshot | `policy_snapshot.py` 记录当时 Harness-owned static definitions | content-addressed immutable publish | 否；只能重放 historical composition | whole Snapshot fingerprint + replay/drift | 否 | 否；Historical Policy 不激活为 Current Policy |
| Run Manifest | `run_manifest.py` 记录 provider/policy/project/memory/context strategy 的 safe configuration identity | immutable per Run | 否 | configuration fingerprint + Policy binding check/differences | 否 | 否 |
| Run Envelope | `run_envelope.py` 记录 initial input identity、Provider request/decision digests 与 pure transition input/output | live 时可 append/update；导出后只作为历史读取 | 否；不是 Session/action checkpoint | initial-input identity + schema/reference checks + deterministic transition replay | 否 | 否 |
| Audit Trace | `audit.py` 记录 ordered safe events、actors、outcomes 与 references | append-only API；live JSONL 持续增长 | **否**；事件记录不能替代 action checkpoint | event schema/sequence；进入 Bundle 后另有 exact-byte hash | 否 | 否 |
| Evidence | `evidence.py` 记录 claim、provenance、freshness scope、Observation identity 与 Harness acceptance | immutable record | 否；可支持 gate，但不能单独决定 action recovery | semantic fingerprint + reference/trace checks | Historical Evidence 否；same-run eligibility 仍不替代 fresh Observation/current workspace gate | 否 |
| Artifact | `artifacts.py` 记录某一 deliverable version 的 path/content identity、producer、Evidence 与 contract result | immutable version | 否 | semantic fingerprint + Evidence/contract/trace checks | 否；`current_output_contract_gate` 会重新观察 workspace | 否 |
| Result | `result.py` 记录 Harness 绑定的 terminal status、safe answer 与 accepted refs | immutable per Run | 否；terminal history 不是继续执行凭证 | logical whole-Result fingerprint + binding/integrity replay | 否 | 否；它只对**该历史 Run 的 outcome** authoritative |
| Bundle | `run_bundle.py` 移植 required indexed closure，供 offline show/check/replay | 导出目标拒绝 conflicting overwrite | 否；不是 resume package | manifest fingerprint + 每个 vendored file 的 sha256/size + relationship checks | 否 | **永远不能** |

“immutable”表示相应 store 拒绝同 ID 的 conflicting republish，不表示磁盘字节不可被外部修改，也不表示每种
对象都有 whole-record fingerprint。Envelope 与 Audit 在 live Run 期间都会增长。

## Fingerprint / integrity 精度

| Object | Native fingerprint 实际覆盖范围 | 没有覆盖或不能证明的内容 |
|---|---|---|
| Policy Snapshot | `policy_fingerprint` 对 canonical Snapshot 全体计算 | 不证明它是 Current Policy，也不包含 Approval/runtime outcome |
| Run Manifest | `configuration_fingerprint` 只覆盖 `configuration` | 不覆盖 `run_id`、`session_id`、`created_at`；它不是 whole Manifest fingerprint |
| Run Envelope | `envelope_fingerprint` 只覆盖 immutable initial `inputs` | 不覆盖后来追加的 `requests`/`transitions`；这些靠 schema、sequence、各自 digest/reference 和 replay 检查 |
| Audit Trace | 本地 JSONL 没有 whole-file fingerprint 或 event hash chain；`event_id` 也不是内容 hash。event references 可携带 Evidence/Artifact/Result 等对象 fingerprint，用于交叉核对 | `read_events` 主要校验 `run_id`/sequence，并允许忽略 torn final line；对象 fingerprint 不会反过来 hash 整条 Audit；Bundle 导出后才用 index sha256 绑定 exact bytes |
| Evidence | `evidence_fingerprint` 覆盖稳定语义字段 | 排除 `evidence_id`、`created_at` 与 fingerprint 自身；integrity 不是 freshness |
| Artifact | `artifact_fingerprint` 覆盖稳定 Artifact 语义字段 | 排除 `artifact_id`、`created_at` 与 fingerprint 自身；不读取当前文件 |
| Result | `result_fingerprint` 覆盖除 fingerprint 自身外的 logical Result record | 只证明历史 binding record；不证明当前 workspace |
| Bundle | `bundle_fingerprint` 覆盖 schema/run/status/root/object index，不覆盖 `created_at`；index 为每个对象保存 exact-byte sha256/size | 没有签名、来源认证或 Current Reality 证明 |

因此不能笼统说“所有 Historical Objects 都有完整 fingerprint”。本地对象的 native identity、引用关系检查、
deterministic replay 与 Bundle exact-byte hash 是不同层次。

## Reference graph：不是线性 pipeline

```text
Run Manifest ---------------- policy_fingerprint ----------------> Policy Snapshot
     ^                                                               ^
     | manifest configuration identity                               |
     |                                                               |
Run Envelope ---------------- policy_fingerprint --------------------+
     |
     | request/decision digests + pure transition identities
     |
     +<............ shared ids/digests ............> Audit Trace
     |                                                   ^
     | Envelope 与 Audit 是并行 observability planes：    |
     | 前者记录 replay input/output identity，             |
     | 后者记录 ordered safe events；不是父子 truth chain。|
                                                         |
Evidence ---------------- Observation/action/event references ------+
   ^
   | evidence_ids
Artifact
   ^
   | accepted artifact/evidence refs
Result ----------------------------------------------------> Evidence

Bundle --vendors required indexed closure--> Policy/Manifest/Envelope/Audit/
                                             Evidence/Artifact/Output Contract/Result

Session -- continuity only；不是 Audit，也不作为 Bundle resume state
```

箭头表示 reference，不表示“前一个对象生成后一个对象”。例如 Envelope 不生成 Evidence，Audit 也不向
Envelope 授予 truth；它们只是从不同平面记录并交叉引用同一个 Run。

## Worked Trace：completed Run 的 historical closure

```text
run_id=R
  -> Policy Snapshot P persisted
  -> Manifest M binds P + configuration identity
  -> Envelope E binds M/P + request/decision digests
  -> Audit A appends policy/approval/action/verification/result events
  -> Evidence V references R, action_id, observation_event_id
  -> Artifact F references V and producer action
  -> Result T references accepted V/F and result_binding transition
  -> export_run_bundle(R)
  -> collect_reference_closure follows required typed refs
  -> Bundle index stores each object's sha256/size/path
  -> local .audit unavailable
  -> BundleHistoricalResolver loads only indexed regular files
  -> check_bundle=MATCH
  -> replay_bundle=MATCH
```

Audit event 中发现的部分引用按 optional 方式收集，forensic Bundle 也可能显式缺少完整 trace。MATCH 只说明
Bundle 所携带的 indexed bytes、required relationships 与 recorded transition outputs 自洽且可重算；没有数字
签名，不说明来源真实性、当前 workspace 仍等于 Run R，也不创建可 resume 的 action checkpoint。

## Key Invariants

1. 所有 lineage reference 必须能回到同一 `run_id` 或显式标记 vendored cross-run source。
2. historical object 只记录 safe identity/metadata，不保存 raw secret-bearing output。
3. Historical Policy 只用于历史 replay，不激活为 Current Policy。
4. Historical Evidence/Artifact integrity 不等于 Current Reality freshness。
5. Result 是 Harness terminal binding，不是 Model final answer 的别名。
6. Bundle resolver 必须 `historical_read_only=True`，replay 不执行 Provider/Tool/Approval。
7. missing/corrupt strong reference 不能降级成 MATCH。

## Failure / Edge Cases

- Audit 最后一行因 crash torn：`read_events` 忽略无法解析的最后残行，但中间 sequence 错误会拒绝。
- Envelope 缺 Manifest/Policy Snapshot 或 transition dependency：identity MISMATCH/transition UNAVAILABLE。
- same-run Result closure 缺少 required Audit/Evidence/Artifact provenance：Bundle/result integrity 失败或
  unavailable。对于显式 vendored cross-run immutable Evidence/Artifact，resolver 缺少 source Audit 时，当前
  helper 允许有限 integrity check 跳过 Audit linking；这不等于 full provenance 已验证。
- Result 引用 superseded/rejected Artifact：Result integrity 失败。
- forensic Bundle 可没有完整 Result closure；状态与 trace availability 必须显式报告，不能伪装 result Bundle。
- Bundle 多文件被加、删、改、symlink 或 path escape：`check_bundle` MISMATCH。
- 本地历史缺失但 Bundle closure 完整：Bundle check/replay 仍可 MATCH；它不回填本地 Session。

## Review Anchors

- 各 store 的 duplicate/conflict 行为：是否拒绝同 ID 不同内容。
- `RunEnvelopeStore`：request bind 和 transition sequence 是否保持追加顺序。
- `evidence_integrity_check`/`artifact_integrity_check`/`result_integrity_check`：是否只检查历史 closure，是否误读 current files。
- `collect_reference_closure`：强引用是否完整，cross-run object 是否显式标注 owner。
- `BundleHistoricalResolver`：是否只访问 index 内 regular file，是否允许 symlink/path escape。
- 所有 replay function：import/call graph 中是否出现 Provider、Tool、MCP、Subagent 或 Approval。

## Common Misreadings

- **“Audit 就是 Session。”错误。** Audit 是 safe ordered event trace；Session 是 continuity state/messages。
- **“Manifest 就是 Envelope。”错误。** Manifest 记录配置身份；Envelope 记录请求和 deterministic transition 输入。
- **“Evidence 就是 Artifact。”错误。** Evidence 支持 claim；Artifact 表示交付物版本。
- **“Result 就是模型 final answer。”错误。** 模型只提供 candidate；Harness 绑定 status/refs。
- **“Bundle 是 resume package。”错误。** Bundle 无 Session、executor 或 reusable Approval/Authority。
- **“integrity MATCH 表示当前文件没变。”错误。** 它只验证历史对象。
- **“每个对象都有 whole-record fingerprint。”错误。** Manifest、Envelope、Audit 和 Bundle 的 identity
  覆盖范围各不相同。
- **“Audit 是 recovery truth source。”错误。** Audit 是 event trace；action recovery 读取 checkpoint 与
  current control state。

## 离线历史检查 CLI

这些命令读取历史记录或重算 deterministic check，不恢复执行，也不授予 Authority：

```bash
python mini_harness.py --audit-list
python mini_harness.py --audit-show RUN_ID
python mini_harness.py --audit-why RUN_ID
python mini_harness.py --audit-json RUN_ID
python mini_harness.py --policy-status RUN_ID
python mini_harness.py --policy-diff RUN_ID
python mini_harness.py --policy-replay RUN_ID
python mini_harness.py --manifest-show RUN_ID
python mini_harness.py --manifest-status RUN_ID
python mini_harness.py --manifest-diff RUN_ID
python mini_harness.py --manifest-check RUN_ID
python mini_harness.py --manifest-reconstruct RUN_ID
python mini_harness.py --envelope-show RUN_ID
python mini_harness.py --envelope-check RUN_ID
python mini_harness.py --replay-check RUN_ID
```

`show` 回答“记录了什么”，`diff/status` 回答“identity 是否漂移”，`check/replay` 回答“历史闭包能否按既定规则验证”。三者都不回答当前 workspace 是否仍相同。

## Deep Review Questions

1. 哪些 Historical Object 在 live Run 中仍会变化，它们的 fingerprint 实际覆盖哪些字段？
2. Manifest、Envelope 与 Audit 为什么应画成并行 reference planes，而不是线性 pipeline？
3. Evidence integrity、Evidence gate eligibility 与 Current Reality observation 分别由哪些 helper 判断？
4. 为什么 Bundle replay MATCH 既不授予 Execution Authority，也不证明当前 workspace 未漂移？
5. Bundle resolver 缺少 vendored cross-run Evidence/Artifact 的 Audit 时，实际 integrity 行为是什么？

## 与其他文档的链接

- Session 与 Audit 区别：[`07-session-memory-context.md`](07-session-memory-context.md)
- Evidence/Artifact/Result 深入：[`10-evidence-artifact-result.md`](10-evidence-artifact-result.md)
- Replay/Bundle：[`11-replay-and-bundles.md`](11-replay-and-bundles.md)
- Security boundary：[`12-security-boundaries.md`](12-security-boundaries.md)
- Failure semantics：[`13-failure-semantics.md`](13-failure-semantics.md)

## Navigation

- Previous: [`08-mcp-and-subagents.md`](08-mcp-and-subagents.md)
- Next: [`10-evidence-artifact-result.md`](10-evidence-artifact-result.md)
- Related: [`11-replay-and-bundles.md`](11-replay-and-bundles.md), [`17-glossary-and-state-reference.md`](17-glossary-and-state-reference.md)
