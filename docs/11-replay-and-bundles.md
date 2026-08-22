# Replay 与 Bundle：重算历史，不重做外部动作

## 读完你应该理解什么

- V19/V20/V21/V25 分别检查哪一层历史身份。
- Identity Check、Deterministic Harness Replay 与外部重执行的边界。
- Local/Bundle resolver 如何让同一套历史检查既能读取 `.audit`，也能离线读取 portable Bundle。

## Scope / Not Scope

本篇只描述已实现的 historical check/replay。Replay 重算 deterministic Harness decision，不调用 Provider、
Tool、MCP、Subagent、Approval，也不读取 Current Reality 来“验证当时”。

Level 3 External Re-execution 明确不支持；本轮没有新 replay level。

## 真实模块与关键函数

- V19 [`policy_snapshot.py`](../mini_harness_core/policy_snapshot.py)：`replay_policy_events`、
  `compose_from_snapshot`、`policy_drift`。
- V20 [`run_manifest.py`](../mini_harness_core/run_manifest.py)：`integrity_check`、
  `manifest_differences`、`rebuild_configuration_for_status`。
- V21 [`run_envelope.py`](../mini_harness_core/run_envelope.py)：`envelope_integrity_check`、
  `_replay_transition`、`harness_replay_check`。
- Result replay [`result_replay.py`](../mini_harness_core/result_replay.py)：`replay_result_binding`。
- V25 [`run_bundle.py`](../mini_harness_core/run_bundle.py)：`HistoricalObjectResolver`、
  `LocalHistoricalResolver`、`BundleHistoricalResolver`、`check_bundle`、`replay_bundle`。

## 核心状态/数据结构

### 三个 replay level

| Level | 当前支持 | 输入 | 做什么 | 不做什么 |
|---|---|---|---|---|
| Level 1 — Identity Check | 是 | fingerprint、hash/size、typed references | 验 schema、identity、closure 与 tamper | 不重算业务 transition，不读 Current Reality |
| Level 2 — Deterministic Harness Replay | 是 | historical snapshot + recorded pure inputs | 重算 Policy/planning/retry/verification/artifact/result outputs并比较 | 不调用外部 adapter |
| Level 3 — External Re-execution | **NOT SUPPORTED** | would require current Provider/Tool/environment | 无实现 | 不重发命令、不重走 Approval、不声称复现现实 |

常见结果：

- `MATCH`：可用输入重算结果与 recorded output 一致。
- `MISMATCH`：身份、closure 或重算结果冲突。
- `UNAVAILABLE`：依赖的历史对象/transition contract 缺失或不支持；不能降级成 MATCH。

### V19–V25 层次

```text
V19 Policy Snapshot
  historical definitions -> replay recorded policy decisions

V20 Run Manifest
  configuration identity -> integrity + compare with rebuilt current identity

V21 Run Envelope
  request/decision identities + pure transitions -> Harness replay

V25 Run Bundle
  typed reference closure -> portable identity check + Envelope replay
```

Manifest drift 比较 historical configuration 与重新观察的 current configuration；Envelope replay 则必须使用
historical Policy Snapshot。Current Policy 不应替换历史 snapshot，Historical Policy 也不应激活为 Current
Policy。

## Resolver boundary

`HistoricalObjectResolver` 只有 read/list/load/audit-events 接口，并标记 `historical_read_only=True`。

- `LocalHistoricalResolver`：从一个显式 `.audit` root 读取 regular files。
- `BundleHistoricalResolver`：只读取 `bundle.json` index 内、hash/size 匹配、位于 Bundle root 内且非 symlink
  的 regular files。

`harness_replay_check` 接受 resolver，因此 pure replay 不需要知道对象来自本机还是 Bundle。resolver 没有
Session save、Approval、Provider、Tool 或 workspace mutation 方法。

## Worked Trace：historical cwd 与 current cwd 不同

```text
Historical Run R
  pwd Observation identity records cwd=/root/mini-harness
  Envelope records verification/result transition inputs
  Bundle exports immutable historical closure

Current process
  cwd=/root
  local .audit temporarily unavailable
    |
    v
BundleHistoricalResolver
  -> validates indexed bytes
  -> harness_replay_check replays recorded pure inputs
  -> recorded output == replayed output
  -> MATCH
```

Replay 没有调用 `pwd`，所以 MATCH 的含义是“当时的 record 和 deterministic Harness transition 自洽”，不是
“当前 cwd 仍为 `/root/mini-harness`”。若要确认当前 cwd，必须发起新的 authorized read-only action，得到
Fresh Observation。

## Worked Trace：portable Bundle 与 tamper

```text
completed Run
  -> export_run_bundle
  -> collect typed strong closure
  -> write objects + bundle.json atomically into new directory
  -> self check MATCH

move/hide Local .audit
  -> check_bundle=MATCH
  -> replay_bundle=MATCH

copy Bundle and append one byte to an indexed object
  -> hash/size mismatch
  -> check_bundle=MISMATCH

original Bundle
  -> remains MATCH
```

```text
portable history != portable authority
```

Bundle 不包含 reusable Approval、live Session、executor connection 或 `AuthorizedAction` private seal，因此不可用于
resume 或执行。

## Key Invariants

1. Replay 只消费 historical safe identities 和 pure transition inputs。
2. Historical replay 必须使用当时 Policy Snapshot，不能偷偷换 Current Policy。
3. Replay MATCH 不证明 Current Reality，也不提供 Authority。
4. Bundle replay 外部执行 call count 必须为零。
5. resolver 必须 read-only；Bundle path/hash/size/symlink checks fail closed。
6. MISMATCH、UNAVAILABLE、PARTIAL 必须保持不同语义。
7. Bundle integrity 不能被解释成 Output Contract 当前仍 satisfied。

## Failure / Edge Cases

- Snapshot fingerprint 与 Manifest/Envelope reference 不一致：identity MISMATCH。
- transition 需要的 Evidence/Artifact/Output Contract 缺失：UNAVAILABLE 或 Bundle closure MISMATCH。
- governance transition 没有命名 pure replay contract：Envelope 返回 UNAVAILABLE，不假装重算。
- forensic Bundle 缺 Envelope：check 可报告可读历史，但 replay 返回 UNAVAILABLE。
- Bundle 有 extra/unindexed file、missing file、symlink、escape path、hash/size mismatch：MISMATCH。
- Current AGENTS/Policy/workspace drift：Manifest 可报告 differences；historical Policy/Envelope replay仍可 MATCH。
- Bundle MATCH 后用于 Current Reality gate：不允许；必须 fresh observe。

## Review Anchors

- `_replay_transition`：每个 transition 是否只调用 pure helper。
- `harness_replay_check`：resolver 是否强制 `historical_read_only=True`，UNAVAILABLE 是否被保留。
- `manifest_differences`：比较 identity，不输出/读取 secret-bearing body。
- `collect_reference_closure`：Result/Envelope/Audit strong refs 与 cross-run vendoring 是否完整。
- `_safe_index_path`/`BundleHistoricalResolver.read_bytes`：path、symlink、hash、size 检查顺序。
- `replay_bundle`：call graph 中是否可能到达 Provider、Approval 或 executor。

## Common Misreadings

- **“Replay 会重新运行 Tool。”错误。** 只重算 Harness transition。
- **“MATCH 说明当前环境没漂移。”错误。** Current Reality 不在历史 replay 范围。
- **“Manifest DRIFT 会让历史 Envelope MISMATCH。”错误。** 两者回答不同问题。
- **“Bundle 可携带 Authority 到另一台机器。”错误。** 它只携带 history。
- **“UNAVAILABLE 可以当作 MATCH。”错误。** 缺少 replay contract/依赖不能证明一致。

## Portable history CLI

```bash
python mini_harness.py --bundle-export RUN_ID
python mini_harness.py --bundle-show BUNDLE_PATH
python mini_harness.py --bundle-check BUNDLE_PATH
python mini_harness.py --bundle-replay BUNDLE_PATH
```

`export` vendoring immutable closure；其余命令只读 Bundle。`bundle-replay` 不连接 Provider、Approval 或 executor，也不能用于 `--resume`。

## 与其他文档的链接

- Historical object map：[`09-audit-and-historical-objects.md`](09-audit-and-historical-objects.md)
- Evidence freshness：[`10-evidence-artifact-result.md`](10-evidence-artifact-result.md)
- Security read-only boundary：[`12-security-boundaries.md`](12-security-boundaries.md)
- Testing：[`14-testing-strategy.md`](14-testing-strategy.md)

## Navigation

- Previous: [`10-evidence-artifact-result.md`](10-evidence-artifact-result.md)
- Next: [`12-security-boundaries.md`](12-security-boundaries.md)
- Related: [`09-audit-and-historical-objects.md`](09-audit-and-historical-objects.md), [`14-testing-strategy.md`](14-testing-strategy.md)
