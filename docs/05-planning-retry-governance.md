# Planning、Retry 与 Governance

## 读完你应该理解什么

- Planning、Retry、Governance 为什么是三套独立语义。
- Retry、Replay、Replan 的区别。
- Deadline、Cancel、Pause 如何与 attempt/action budget 共同限制执行。

相关实现：[`planning.py`](../mini_harness_core/planning.py)、
[`retry.py`](../mini_harness_core/retry.py)、
[`governance.py`](../mini_harness_core/governance.py)、
[`run_control.py`](../mini_harness_core/run_control.py) 和
[`durability.py`](../mini_harness_core/durability.py)。

## 三个不同问题

### Planning：任务下一步是什么

Plan 包含 goal、version、status 和有依赖关系的 steps。`select_ready_step` 找到依赖已完成的 pending
step，`start_step` 将其设为 `in_progress`，`complete_step` 只接受满足 evidence gate 的完成。

Planning 不决定 Tool 是否有权限，也不决定同一 action 是否可以再次尝试。

### Retry：明确失败后是否允许新 attempt

Retry state 记录 `logical_action_id`、`attempt_count`、`max_attempts`、failure class、policy、backoff 和
状态。`decide_retry` 回答的是：已有一个 definite failed attempt，是否可以安排另一个 attempt。

新 attempt 使用新的 action checkpoint/action id。ASK action 需要新的 Approval。

### Governance：当前时间/预算是否允许继续

Governance 管理 run/step deadline、tool timeout、normal action count、subagent count，以及 pause 时 active
time freeze。即使 Plan 有 ready step、Retry 允许新 attempt，`normal_action_decision` 仍可因为 expiry 或
budget exhausted 拒绝执行。

## 相互关系图

```text
                 +-----------------------------+
                 | Planning                    |
                 | What should happen next?    |
                 | step / dependency / replan  |
                 +--------------+--------------+
                                |
                                v
Model proposes action ---> Policy / Authority gates
                                |
                                v
                 +--------------+--------------+
                 | Governance                  |
                 | May work happen now?        |
                 | deadline / action budget    |
                 +--------------+--------------+
                                |
                                v
                           Attempt executes
                                |
                     +----------+----------+
                     |                     |
                  success             definite failure
                     |                     |
                     v                     v
              Verification         +------+------+
                                   | Retry       |
                                   | another     |
                                   | attempt?    |
                                   +------+------+
                                          |
                           +--------------+--------------+
                           |                             |
                       retry allowed              replan / block
                           |                             |
                           +----> Governance again <----+

Unknown side effect bypasses ordinary Retry:
Durability -> reconciliation-or-block
```

## Retry ≠ Replay

Retry 创建一个新的 attempt，并重新检查 current Policy、runtime gates、budget 和 Approval。Replay 则是
历史检查：使用 recorded inputs 重算 pure Harness transition，不执行 Tool。

```text
Retry:  new execution, new action id, Current Reality, current Authority
Replay: no execution, historical inputs, historical identity comparison
```

因此 Historical Approval 或 Historical Policy replay 都不能授权 Retry。

## Replan ≠ Retry

Retry 保持 logical action strategy，只改变 attempt。Replan 表示该策略不再合适，需要改变 Plan step 或
action strategy。

`planning.retry_exhausted_outcome` 在 attempt exhaustion 后请求 replan；达到 Plan 的 bounded replan limit
则 block。无 Plan 的 reactive run 如果在 exhaustion 后只收到模型的 completed claim，Result Binding
仍会输出 blocked。

## Deadline ≠ Cancel

- Deadline 是 Harness clock/budget fact；到期后 normal scheduling 被阻断。
- Cancel 是用户控制状态；`cancelled` 是 terminal，不能 resume。
- Pause 是可恢复控制状态；active deadline remaining 会冻结，但 action count 不会重置。

三者可能产生相似的“不能继续执行”，但来源、可恢复性和历史解释不同。

## Retry decision precedence

`retry.decide_retry` 按安全优先级判断：

1. paused/cancelled run → `block`；
2. policy/user rejection → `no_retry`；
3. `never_auto_retry` → `block`；
4. side-effecting/unknown Effect → `reconcile_before_retry`；
5. unknown failure → `block`；
6. permanent failure 或 attempts exhausted → `replan`；
7. 其余明确 read-only transient failure → `retry_with_backoff`。

这体现 durability 优先于 retry convenience。timeout 对 read-only action 可 transient retry；timeout 对
side-effecting action 不能证明副作用未发生。

## Backoff 与 budget

`record_failure` 计算 deterministic exponential delay；`cooperative_backoff` 分片等待并响应 pause/cancel。
真正 sleep 前，`governance.backoff_decision` 确认剩余 deadline 能容纳 delay。

每次实际 invocation 前才由 `consume_action` 增加 action count。replan、retry、resume 都复用同一
governance state，所以不会恢复已消耗预算。

## Deadline 后的 safety reconciliation

如果 deadline 到期时存在 unknown side effect，完全拒绝 observation 会永久留下不确定性。因此
`safety_reconciliation_decision` 允许一个窄例外，但同时要求：

- checkpoint 是 side-effecting/unknown 的 `unknown`；
- capability Effect 是 `read_only`；
- observation 与原 target 相关；
- Security/Policy 没有 DENY；
- safety reconciliation budget 未耗尽。

它使用独立 counter，不恢复 normal action budget，不启动 retry，也不推进正常工作。reconciliation 后
Run 仍 blocked。

## 一个对比例子

读取 `pwd` 连续 timeout：

```text
read_only + transient + attempts remain
-> retry_with_backoff
-> Governance preflight
-> new attempt
```

写入 `echo hello > report.md` timeout：

```text
side_effecting + timeout
-> reconcile_before_retry
-> cat/ls report.md as fresh read-only observation
-> applied / not_applied / uncertain
-> only not_applied may reopen retry
```

下一步阅读：[`04-action-lifecycle.md`](04-action-lifecycle.md) 和
[`02-agent-loop.md`](02-agent-loop.md)。

## Navigation

- Previous: [`04-action-lifecycle.md`](04-action-lifecycle.md)
- Next: [`06-durability-and-recovery.md`](06-durability-and-recovery.md)
- Related: [`13-failure-semantics.md`](13-failure-semantics.md), [`16-design-decisions.md`](16-design-decisions.md)
