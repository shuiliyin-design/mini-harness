# Environment Adapter Contract

P2.5 把 battery 与 notification 的共同 execution mechanics 收敛到 [environment_adapters.py](../../mini_harness_core/environment_adapters.py)，没有增加 capability surface，也没有建立 plugin framework。

## 最小 Contract

### `EnvironmentCapabilitySpec`

稳定字段：

- `logical_name`
- `effect`
- `zone`
- `adapter_id`
- `adapter_version`
- `input_schema_identity`

Spec 不包含 Policy disposition、Approval、Retry permission、Evidence semantics 或 Result semantics。

### `EnvironmentInvocation`

字段：

- `logical_capability`
- `normalized_args`
- `action_id`
- `run_id`

Invocation 只能从 sealed `AuthorizedAction` 构造。调用者不能注入 executable、raw argv、path 或 shell fragment。

### `EnvironmentAdapterResult`

字段：

- `status`
- `effect`
- `effect_certainty`
- `safe_observation`
- `exit_code`
- stdout/stderr length 与 SHA-256
- `error_code`

Contract 不携带 raw stdout/stderr。

## Effect certainty

| Value | Meaning |
|---|---|
| `no_side_effect` | 调用不产生外部 side effect；失败也不进入 side-effect reconciliation |
| `known_applied` | Adapter 可以证明本次定义的 effect 已发生，例如 notification request accepted |
| `not_started` | execution mechanics 明确没有开始，例如 executable 不存在或 pre-dispatch validation 失败 |
| `unknown` | dispatch/transport 已可能开始，但结果不确定 |

Effect certainty 是环境事实，不是 retry permission。Harness 根据 effect、checkpoint、retry policy、deadline 和 governance 决定后续。

## Error taxonomy

- `CAPABILITY_NOT_INSTALLED`
- `COMPANION_UNAVAILABLE`
- `TIMEOUT`
- `INVALID_RESPONSE`
- `EXECUTION_FAILED`
- `INVALID_ARGUMENT`
- `UNSUPPORTED_CAPABILITY`

Adapter 不把这些枚举直接转换为 `ALLOW`、retry、blocked、Evidence 或 Result。

## Static registry

[environment_registry.py](../../mini_harness_core/environment_registry.py) 是 Harness-owned 静态 registry，目前只绑定：

- `termux:battery_status`
- `termux:notification`

Registry 提供：

- immutable logical-name lookup；
- fixed adapter callable；
- argument normalization；
- safe Model catalog；
- sorted specs 的 canonical JSON + SHA-256 registry fingerprint。

未知 `termux:*` fail closed。没有动态 discovery、用户注册、plugin loading 或 executable override。

## Dispatch seam

```text
Model Intent
→ Classification / Static Policy / Capability Authorization
→ Runtime Gates / Approval
→ AuthorizedAction
→ EnvironmentInvocation
→ static registry
→ fixed adapter
→ EnvironmentAdapterResult
```

`agent.py` 不再包含 battery/notification 名称分支；capability-specific 逻辑留在 [termux_capabilities.py](../../mini_harness_core/termux_capabilities.py)。

## Adapter Does Not Own

- Policy composition；
- Approval；
- AuthorizedAction creation；
- Retry permission；
- Reconciliation；
- Evidence acceptance；
- Authoritative Result；
- Bridge、Provider 或 Subagent orchestration。

## Capability-specific logic

公共层不理解 battery percentage 或 notification title/content：

- battery adapter 负责 JSON parse、battery field allowlist 与 response validation；
- notification adapter 负责 title/content normalization、fixed argv 与 request-accepted semantics。

这种拆分目前是健康抽象：新增同类 capability 理论上只需要 capability-specific adapter、registry entry、policy/profile mapping 和 tests，而不修改 `run_agent`、Bridge、Provider、Evidence schema 或 Result schema。
