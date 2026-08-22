# Phase 2 Design Decisions

以下 ADR-style 条目记录已经稳定的方向。`Where implemented` 指向当前主要实现位置；它不表示相邻的 [P2.6 gaps](06-recovery-and-failure-semantics.md#known-p26-safety-gaps) 已修复。

| # | Decision | Why | Alternative rejected | Consequence | Where implemented |
|---:|---|---|---|---|---|
| 1 | Transport identity ≠ Authority | task/claim 只能说明 transport ownership | 用 publisher/consumer metadata 提升信任 | Bridge 输入始终进入 external/untrusted Harness path | [bridge_adapter.py](../../mini_harness_core/bridge_adapter.py) |
| 2 | Visibility ≠ Commitment | crash 可能留下 tmp 或 partial JSON | 看见文件就消费 | ready marker 必须最后发布 | [bridge_publisher.py](../../mini_harness_core/bridge_publisher.py)、[bridge_inspector.py](../../mini_harness_core/bridge_inspector.py) |
| 3 | Claim ≠ Execution Authority | ownership 不能授予 shell/Android 权限 | Claim 后直接执行任意 payload | Executor 只有 tiny allowlist；Harness 仍重新授权 | [bridge_claimer.py](../../mini_harness_core/bridge_claimer.py)、[bridge_executor.py](../../mini_harness_core/bridge_executor.py) |
| 4 | History immutable | overwrite 会抹掉 crash/retry truth | 更新单个 mutable task state | records append/no-replace；状态由历史推导 | [bridge_paths.py](../../mini_harness_core/bridge_paths.py)、[integrity.py](../../mini_harness_core/integrity.py) |
| 5 | State derived from history | mtime/Session/timestamp 不可靠 | Worker 持有 mutable queue state | Inspector 是 Bridge state 的单一推导点 | [bridge_inspector.py](../../mini_harness_core/bridge_inspector.py) |
| 6 | Timestamp ≠ lease authority | wall clock 不可信且跨环境漂移 | 以 claimed_at 超时抢占 | V1 stale lock 只人工 recovery | [bridge_claimer.py](../../mini_harness_core/bridge_claimer.py) |
| 7 | Binding before Run | Run 必须先拥有 durable transport↔run identity | Run 后补 Binding | crash 后复用 fixed session/run IDs | [bridge_adapter.py](../../mini_harness_core/bridge_adapter.py) |
| 8 | Bridge Result ≠ Harness Evidence | transport output 不是执行真实性证明 | 把 Bridge Result 自动 accepted | 只允许 Harness Result 单向投影 | [bridge_adapter.py](../../mini_harness_core/bridge_adapter.py)、[evidence.py](../../mini_harness_core/evidence.py) |
| 9 | Bridge completed ≠ Harness completed | Bridge outer status 表示 transport commitment | 把外层 completed 当业务成功 | projection 携带独立 `harness_result_status` | [bridge_adapter.py](../../mini_harness_core/bridge_adapter.py) |
| 10 | Environment capability existence ≠ permission | 本机 executable 存在不授予 Agent Authority | Registry entry 自动 ALLOW | capability 仍经过完整 Harness chain | [environment_registry.py](../../mini_harness_core/environment_registry.py)、[agent.py](../../mini_harness_core/agent.py) |
| 11 | Effect ≠ disposition | read-only/side-effecting 是分类，不是 Policy 结果 | read-only 自动 ALLOW、side effect 自动 DENY | Global/Zone/Profile/Delegation composition 决定 ALLOW/ASK/DENY | [policy_composition.py](../../mini_harness_core/policy_composition.py) |
| 12 | Adapter Result ≠ Evidence | Adapter 只知道 mechanics 与 safe observation | Adapter 自己接受 Evidence | Harness 创建、验证并持久化 Evidence | [environment_adapters.py](../../mini_harness_core/environment_adapters.py)、[agent.py](../../mini_harness_core/agent.py) |
| 13 | Request accepted ≠ user seen | notification exit 0 不能证明用户感知 | 生成 `user_seen=true` | Evidence 只支持 request acceptance | [termux_capabilities.py](../../mini_harness_core/termux_capabilities.py) |
| 14 | Unknown side effect ≠ retry permission | 重试可能重复真实 Android effect | timeout 统一普通 retry | unknown 进入 reconcile/block；read-only 才可 bounded retry | [durability.py](../../mini_harness_core/durability.py)、[retry.py](../../mini_harness_core/retry.py) |
| 15 | Registry Harness-owned | Model/MCP/Subagent metadata 不可信 | 动态 plugin/discovery 或外部注册 | static closed registry、unknown fail closed、manifest fingerprint | [environment_registry.py](../../mini_harness_core/environment_registry.py)、[run_manifest.py](../../mini_harness_core/run_manifest.py) |
| 16 | Attempt execution and reconciliation are mutually exclusive | Bridge history alone cannot distinguish live execution from crash recovery | 允许 operator 与 executor 同时推进 | atomic attempt fence；残留锁人工恢复 | [bridge_attempt_fence.py](../../mini_harness_core/bridge_attempt_fence.py) |
| 17 | Environment certainty drives checkpoint truth | exit code 不能表达 side-effect ambiguity | 每个 handler 自己猜 outcome | 单一 certainty→checkpoint mapping，unknown 永不降为 failed | [dispatch.py](../../mini_harness_core/dispatch.py) |
| 18 | Integration projection requires durable Harness truth | caller repair payload 不拥有 Harness Result Authority | generic repair 完成 bridge_harness_task | generic repair fail closed；projection-only repair读取 Binding/Result | [bridge_adapter.py](../../mini_harness_core/bridge_adapter.py)、[bridge_result_repairer.py](../../mini_harness_core/bridge_result_repairer.py) |
| 19 | Observation truth survives Evidence-store failure | persistence failure 不能反写已完成的 Environment action，也不能授权重调 capability | 把 Evidence failure 当 action failure/retry | deterministic Evidence-only repair 绑定 durable checkpoint + safe Audit Observation；缺一 fail closed | [bridge_adapter.py](../../mini_harness_core/bridge_adapter.py)、[evidence.py](../../mini_harness_core/evidence.py) |
| 20 | Attempt fence covers projection commitment | terminal Harness Result 与 Bridge ready 之间仍有并发 recovery 窗口 | Result durable 后立即释放 Run fence | fence 持有至 ready；immutable terminal marker 永久排除 Reconciler | [bridge_attempt_fence.py](../../mini_harness_core/bridge_attempt_fence.py)、[bridge_adapter.py](../../mini_harness_core/bridge_adapter.py)、[bridge_reconciler.py](../../mini_harness_core/bridge_reconciler.py) |

## Additional consequences

- Worker composition convenience 不增加 Authority。
- Schema evolution 可以改变 reader，但不能重写 task-001/002 历史。
- Historical replay 只能检查历史 identity/decision，不读取 Current Bridge 或重调 Android。
- Registry fingerprint 记录 capability specs/adapter versions，不记录当前 battery state 或 Android permission state。
- shared-storage 行为是设备实验前提，不是跨 Android 形式保证。
- Evidence-only recovery 不凭历史 digest 构造缺失的 Observation；没有 safe
  Observation identity 时必须人工处理。
- attempt fence 是 concurrency protection，不是 Policy、Approval、lease 或 execution Authority。

## Change discipline

改变上述决策必须同时更新：

1. 对应 schema/contract；
2. Authority 与 crash invariants；
3. deterministic regression/E2E；
4. Manifest/replay compatibility；
5. 本目录与 Phase 1 相关文档。

不能仅为了通过测试降低 fail-closed 行为。
