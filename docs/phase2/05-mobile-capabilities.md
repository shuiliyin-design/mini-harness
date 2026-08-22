# Implemented Mobile Capabilities

本页只记录当前已经实现并测试的两个 logical capability，不为 SMS、GUI、camera、location 等未实现能力预留虚构语义。

## `termux:battery_status`

| Property | Value |
|---|---|
| Effect | `read_only` |
| Zone | `external` |
| Arguments | `{}` |
| Success certainty | `no_side_effect` |
| Adapter observation | battery safe-field allowlist |

Adapter 使用固定 executable、`shell=False`、timeout、output limit 和 strict JSON parsing。只保留诸如 `percentage`、`status`、`plugged`、`temperature`、`health` 等 structured fields；unexpected fields 被忽略，raw JSON stdout 不进入 Harness。

Harness 中的 fresh Observation 可以形成最小 Evidence，例如“在该 observation identity 对应的时刻，percentage=N”。历史 battery Evidence 可以通过 integrity replay，但不能证明之后的 Current Reality。

Battery timeout 是 read-only failure，可以受 bounded Retry、deadline、budget 与 governance 限制；它不进入 side-effect reconciliation。

## `termux:notification`

| Property | Value |
|---|---|
| Effect | `side_effecting` |
| Zone | `external` |
| Arguments | `title`, `content` strings |
| Success certainty | `known_applied` |
| Adapter observation | `notification_requested=true`, `request_accepted=true` |

Adapter 自己构造固定 argv：

```text
termux-notification --title <safe_title> --content <safe_content>
```

调用者不能提供 command、argv、shell fragment 或 executable override。title/content 经过类型、UTF-8、size、control-character 和 secret screening。

`exit 0` 只支持以下 claim：

> notification request was accepted/requested

它不支持 `user_seen=true`、user read、click/open 或当前通知栏状态。要求“确认用户看到通知”的 task 在 V1 中不受支持。

正常 Harness 路径通常由 external zone 产生 ASK，必须使用当前 Run 的 Human Approval。即使静态 disposition 是 ALLOW，side-effecting action 仍要先持久化 prepared/executing checkpoint。

Timeout 或 transport ambiguity 返回 `effect_certainty=unknown`。它不能普通重试；当前没有 notification listener/reconciliation capability，因此 Harness 应 block/incomplete。

## 共同边界

- Logical capability existence 不提供 permission。
- `read_only` 不自动等于 ALLOW。
- Bridge claim、publisher、consumer 或 Binding 不提供 Termux Authority。
- Subagent 必须受到 delegated capability ceiling；MCP metadata 不能冒充 registry capability。
- Historical replay 和 Bundle replay 都不会重新调用 Android。
- Adapter raw stdout/stderr 永不进入 Session、Context、Audit、Evidence、Result、Envelope 或 Bundle。

真实 Android smoke 只提供 environment confidence；deterministic fake-subprocess 和 Harness integration tests 才是 correctness gate。
