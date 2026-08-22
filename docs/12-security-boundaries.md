# Security Boundaries：从 Intent 到持久化边界

## 读完你应该理解什么

- protected path、Policy composition、Approval、AuthorizedAction 和 secret projection 分别守哪一道边界。
- historical objects、Bundle 和 safety Reconciliation 为什么不能变成执行 Authority。
- 面对恶意 project/MCP/Observation/embedding 输入时，哪些路径 fail closed。

## Scope / Not Scope

本篇描述 Mini Harness 自身执行路径和 persistence/context boundary 的教学级安全 invariant。

它不是 OS sandbox、container、权限隔离或完整 DLP。任意宿主 Python 代码仍可绕过 Harness 直接调用
`subprocess`；本项目保证的是 `run_agent`/dispatch seam 内没有未授权 executor path，并通过架构测试监控该
边界。Secret screening 是 deterministic pattern/allowlist，不声称识别所有敏感信息。

## 真实模块与关键函数

- [`protected_paths.py`](../mini_harness_core/protected_paths.py)：`inspect_workspace_path`、
  `inspect_shell_paths`、`inspect_mcp_paths`、`inspect_subagent_paths`。
- [`authority.py`](../mini_harness_core/authority.py)：`classify_shell`、`request_approval`。
- [`policy_composition.py`](../mini_harness_core/policy_composition.py)：`compose_static_policy`、
  `EffectivePolicy.authorize`、`SafetyReconciliationPermit.decide`。
- [`dispatch.py`](../mini_harness_core/dispatch.py)：`authorize_action`、`dispatch_authorized_action`、
  `AuthorizedAction`。
- [`observation.py`](../mini_harness_core/observation.py)：`persisted_safe_observation`、
  `model_context_observation`。
- [`run_bundle.py`](../mini_harness_core/run_bundle.py)：`screen_export_object`、
  `BundleHistoricalResolver`。
- [`governance.py`](../mini_harness_core/governance.py)：`safety_reconciliation_decision`。

## 核心状态/数据结构

### Protected-path ceiling

统一拒绝 `.audit`、`.sessions`、`.env`/`.env.local`、private key/credential/token/secret-like paths、workspace
escape 和 protected symlink。Shell、MCP path-like arguments、Subagent handoff relevant paths 都使用相同 ceiling。

### AuthorizedAction

immutable dataclass 绑定 action/checkpoint/run、capability、canonical arguments、Effect、final Policy decision 和
Approval status，并携带模块私有 seal。它只能由 `authorize_action` 创建；dispatch 再检查 seal 与 exact
`prepared` checkpoint binding。

### Secret projection

raw Observation 只在 adapter/handler 内短暂存在。Session/model/Audit/Evidence/Artifact/Result/Envelope/Bundle
各自只接受 safe summary、length/digest 和少量 allowlisted structured fields。

### Safety Reconciliation permit

这是 deadline/budget/cancel 后的一个 read-only、targeted、one-shot runtime exception。它不覆盖 Security
DENY、protected paths 或 static Policy，也不能恢复 normal work。

## Security pipeline

```text
untrusted Model / project / MCP metadata
  -> Classification + Harness-owned Effect
  -> Static Policy Composition
  -> Capability Authorization
  -> Runtime/Verification/Durability gates
  -> Human Approval when ASK
  -> protected-path ceiling
  -> authorize_action private seal
  -> dispatch_authorized_action exact binding
  -> executor
  -> raw untrusted Observation
  -> safe projection
  -> persistence/context/historical objects
```

Human Approval 只满足 ASK；它不能覆盖 DENY、capability ceiling、protected path、deadline 或 unknown-side-effect
replay safety。

## Fail-closed decision table

| Adversarial input | Boundary owner | Decision | Executor | Persistent secret/current Authority |
|---|---|---|---|---|
| `cat .env.local` | `inspect_shell_paths` + authorization recheck | `DENY` | 0 calls | 无 |
| workspace symlink `link -> .env.local` then `cat link` | realpath/symlink protected-path ceiling | `DENY` | 0 calls | 无 |
| MCP returns `Authorization: Bearer secret-marker` | Observation projection + every historical validator | Tool 可能已执行；raw result 被投影 | 1 original call | marker 不进入 Session/Context/Audit/Evidence/Artifact/Result/Envelope/Bundle |
| plain dict/forged `AuthorizedAction` passed to dispatch | private seal + checkpoint binding | `PermissionError` | 0 calls | 无 Authority |
| historical Approval reused for resume/new attempt | exact correlation + fresh Approval requirement | not authorized | 0 calls until fresh approval | old event 只保留历史身份 |
| MCP description claims `ALLOW/read_only` | Harness-local mapping | metadata ignored；local mapping wins/fail closed | 取决于本地最终授权 | metadata 不提升 Authority |
| expired run requests unrelated read-only action | safety Reconciliation gate | blocked | 0 calls | permit 不扩展 normal work |

## Worked Trace：secret-bearing MCP Observation

```text
Model Intent: mcp:secret:read
  -> local mapping ALLOW + read_only
  -> current gates pass
  -> AuthorizedAction
  -> MCP executor returns raw:
       OPENAI_API_KEY=secret-marker
       Authorization: Bearer secret-marker
  -> persisted_safe_observation
       exit_code + stdout/result identities only
       redacted/digest metadata
  -> model_context_observation
  -> Audit safe_observation_summary
  -> Evidence Observation identity
  -> Artifact/Result/Envelope safe refs only
  -> Bundle export screens every object again

assert secret-marker absent across every persisted/context boundary
```

注意：Tool 已被允许执行，所以安全目标不是伪称“没有 raw value”，而是确保 raw untrusted Observation 不跨越
允许的短暂执行边界。

## Key Invariants

1. Model/Provider/project/Skill/MCP metadata 都不能创建 Authority。
2. 任一 `DENY` 或 protected-path rejection 阻止 executor。
3. Approval 不可复用，也不能提升 capability ceiling。
4. dispatch 只接受私有 sealed、exact checkpoint-bound `AuthorizedAction`。
5. raw secret Observation 不跨 persistence/context/history boundary。
6. unknown side effect 不因 retry budget 存在而自动重放。
7. safety Reconciliation 必须 read-only、targeted、bounded，并继续服从 Security DENY。
8. Historical Policy/Approval/Evidence/Bundle 都不提供新执行 Authority。
9. Bundle replay 外部执行 call count 为零。

## Failure / Edge Cases

- shell grammar 无法可靠解析、组合/expansion/path 不明确：ASK 或 DENY，不采纳模型安全解释。
- protected path 检查遇到 escape、symlink 或不支持 capability：fail closed。
- secret pattern 未命中未知格式：这是教学筛查的限制；依赖 allowlisted projection 减少 raw data 面。
- Audit/Evidence/Bundle validator 发现 forbidden key/text：拒绝写入/导出，而不是保留“用于调查”的 raw secret。
- `authorize_action(runtime_allowed=True)` 不会自行读取完整 runtime state；安全 review 必须同时检查 handler 的 gate
  ordering 和 sealed seam。
- 恶意宿主代码直接导入 `execute_shell`：超出 Harness boundary；需要 OS/process sandbox 才能约束，不应在文档中
  假装已解决。

## Review Anchors

- `inspect_workspace_path`：realpath/commonpath/symlink 与 secret filename 判断。
- `authorize_action`：Policy/Approval/path/checkpoint 是否全部 fail closed，seal 是否不可外部伪造。
- `dispatch_authorized_action`：是否有任何普通 dict、旧 checkpoint 或 mismatched arguments 可到 executor。
- `_handle_shell_decision`/`_handle_mcp_decision`：DENY 是否可能进入 Approval，Approval 后是否重检 stale state。
- Observation/Audit/Evidence/Artifact/Result/Bundle validators：是否共享“raw data 默认不允许”的方向。
- dependency/architecture tests：orchestrator 是否重新出现 direct executor bypass。

## Common Misreadings

- **“Human Approval 可以覆盖所有限制。”错误。** 它只满足 ASK。
- **“MCP 是结构化协议，所以结果可信。”错误。** MCP Observation 仍是不可信外部数据。
- **“保存 hash 就等于可以读取 secret。”错误。** safe identity 不提供 raw value。
- **“Bundle 是签名证书。”错误。** 当前只有 deterministic hash/closure integrity，不是外部签名信任根。
- **“Harness 是 OS sandbox。”错误。** 它约束自身调用路径，不约束恶意宿主 Python。

## 与其他文档的链接

- Authority layers：[`03-authority-and-policy.md`](03-authority-and-policy.md)
- Durability：[`06-durability-and-recovery.md`](06-durability-and-recovery.md)
- MCP/Subagent：[`08-mcp-and-subagents.md`](08-mcp-and-subagents.md)
- Historical boundaries：[`09-audit-and-historical-objects.md`](09-audit-and-historical-objects.md)
- Testing/adversarial checks：[`14-testing-strategy.md`](14-testing-strategy.md)

## Navigation

- Previous: [`11-replay-and-bundles.md`](11-replay-and-bundles.md)
- Next: [`13-failure-semantics.md`](13-failure-semantics.md)
- Related: [`03-authority-and-policy.md`](03-authority-and-policy.md), [`15-code-review-guide.md`](15-code-review-guide.md)
