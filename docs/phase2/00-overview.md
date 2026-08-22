# Phase 2 Documentation Baseline

Phase 2 把 Mini Harness 从单一 PRoot 进程扩展到 Android、Termux、PRoot 与共享存储共同参与的教学环境。本目录以当前源码、deterministic tests 和已经完成的本机 smoke 为准；它不是 Codex 对话记录，也不把 P2.6 计划描述成已实现能力。

> 当前结论：Phase 2 的分层骨架健康；P2.6 已关闭 cross-layer fault/concurrency
> safety gaps，P2.7 已实现同一 Run 内的 battery-threshold-notification orchestration。
> 该结论不扩张 shared-storage、stale lock 或 Android filesystem 的保证边界。

## 工程主线

```text
Android
  ↓ Termux:API companion / shared storage
Termux
  ↓ host userspace visible from PRoot
PRoot / Codex
  ↓ committed files
Bridge Protocol
  ↓ untrusted textual input + durable Binding
Mini Harness
  ↓ Policy / Approval / AuthorizedAction
Environment Adapter Registry
  ↓ fixed execution mechanics
Android Capability
```

大文本、结构化任务和结果通过 file/artifact data plane 传递；TUI 只适合短控制输入，不是可靠 transport。

## 三层模型

| Layer | Owns | Does not own |
|---|---|---|
| Transport | Bridge Task、Claim、Reconciliation、Result commitment | Harness Policy、Approval、Android execution authority |
| Authority | Intent、Classification、Policy、Runtime Gate、Approval、AuthorizedAction、Durability、Evidence、Authoritative Result | Android CLI mechanics、Bridge filesystem commitment |
| Environment | fixed adapter invocation、safe observation、effect certainty | disposition、retry permission、Evidence acceptance、Result authority |

允许的方向：

```text
Bridge → Bridge Adapter → Harness → AuthorizedAction
                                  ↓
                         Environment Registry → Adapter → Android

Android safe observation → Harness Observation/Evidence/Result
Harness terminal Result  → Bridge result projection
```

禁止的反向方向：

- Bridge 不直接调用 Environment Adapter。
- Adapter 不创建 Policy、Approval 或 AuthorizedAction。
- Bridge Result 不成为 Harness Evidence。
- Replay 不重新 claim、调用 Android 或发布 Bridge Result。
- Bridge、MCP、Subagent metadata 不注册或修改 Environment capability。

## 当前实现范围

- Bridge Protocol v1 与七个 primitive。
- `bridge_harness_task` 到 fresh Harness Run 的 immutable Binding。
- 静态 Environment Adapter Contract 与 registry。
- `termux:battery_status`。
- `termux:notification`。
- 单 Bridge claim / 单 Harness Run 的 battery threshold conditional workflow。
- Harness-owned deterministic condition、fresh Evidence dependency、conditional
  Output Contract 与 crash-safe no-duplicate notification recovery。

明确暂缓 GUI、daemon、第三个 capability、通用或 multi-run orchestration、并发 mobile
workflow、scheduler/background agent、动态 adapter、远程 producer identity 和自动
stale-lock cleanup。

## 阅读顺序

1. [Mobile Environment](01-mobile-environment.md)
2. [Bridge Protocol v1](02-bridge-protocol-v1.md)
3. [Harness ↔ Bridge Adapter](03-harness-bridge-adapter.md)
4. [Environment Adapter Contract](04-environment-adapter-contract.md)
5. [Mobile Capabilities](05-mobile-capabilities.md)
6. [Recovery and Failure Semantics](06-recovery-and-failure-semantics.md)
7. [Testing and E2E](07-testing-and-e2e.md)
8. [Design Decisions](08-design-decisions.md)
9. [Review Guide](09-review-guide.md)
10. [Mobile Agent Orchestration](10-mobile-agent-orchestration.md)

Phase 1 的 Authority、durability、Evidence 与 replay 规范仍是基础；见 [Phase 1 documentation map](../README.md)。
