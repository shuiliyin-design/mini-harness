# Authority 与 Policy

## 读完你应该理解什么

- 为什么 Model Intent、Policy、Effect、Approval 和 Authority 是不同概念。
- `ALLOW` / `ASK` / `DENY` 如何组合，以及 capability 为什么只能衰减。
- `AuthorizedAction` 为什么是唯一可以进入 dispatch seam 的执行凭证。

核心实现位于 [`authority.py`](../mini_harness_core/authority.py)、
[`policy_composition.py`](../mini_harness_core/policy_composition.py)、
[`policy_snapshot.py`](../mini_harness_core/policy_snapshot.py)、
[`protected_paths.py`](../mini_harness_core/protected_paths.py) 和
[`dispatch.py`](../mini_harness_core/dispatch.py)。

## Intent ≠ Authority

Model 可以请求：

```json
{"type":"tool_call","tool":"shell","command":"echo hello > report.md"}
```

这只是 Intent。它不证明命令安全、当前 run 可执行、用户同意，也不证明 crash recovery 允许重试。
`agent._handle_shell_decision` 必须从当前 Harness state 重新建立所有条件。

同理，Plan description、Memory、`AGENTS.md`、Skill 和 MCP server metadata 都不是 Authority source。

## 六个不同的判断层

本项目不会把下面六层统称为“Policy”。它们回答不同问题，并由不同代码边界拥有：

1. **Action Classification**：请求是什么、当前 action 的 Effect 是什么；Shell 入口是
   `authority.classify_shell`。
2. **Static Policy Composition**：Global、Zone、Profile、Delegation 中最严格的静态 disposition 与 ceiling
   是什么；入口是 `policy_composition.compose_static_policy`。
3. **Capability Authorization**：给定 exact tool 与 Effect，静态 capability ceiling 是否允许这一次 action；
   由 `EffectivePolicy.authorize` 得到 per-action `ALLOW`/`ASK`/`DENY`。
4. **Runtime Gates**：当前 run control、deadline、budget、durability、persistence 与 Verification 是否允许继续；
   由各 owner 模块计算，agent handler 按时序组合。
5. **Human Approval**：只满足当前 `ASK` 请求，不改变 Effect、capability ceiling 或 `DENY`。
6. **AuthorizedAction**：前述条件在当前 `prepared` checkpoint 上成立后，由 `dispatch.authorize_action` 铸造的
   单次 sealed execution credential。

```text
Action Classification
          |
          v
Static Policy Composition
          |
          v
Capability Ceiling / per-action authorization
          |
          v
Runtime Gates
          |
          v
Human Approval when ASK
          |
          v
AuthorizedAction
          |
          v
Dispatch
```

这是概念上的 authority pipeline。真实 handler 会在入口和 Approval 后重复检查部分 runtime facts，防止等待
期间状态变旧；详见 [`02-agent-loop.md`](02-agent-loop.md)。

## Classification

`authority.classify_shell(command, policy_snapshot)` 先调用有限 shell classifier，再通过
`policy_composition.policy_for` 组合当前静态 Policy，并调用 `EffectivePolicy.authorize` 应用 capability
ceiling。返回结果至少包含：

- `action`: `ALLOW`、`ASK` 或 `DENY`；
- `effect`: `read_only` 或 `side_effecting`；
- `reason`；
- composition trace/inputs。

classifier 故意只理解一小组 shell grammar。未知、组合、重定向等请求不会因为模型解释“这是安全的”
而自动放行。

当前 Shell 实现先把有限 classifier 的初始 `ALLOW` 映射为 `read_only`，把初始 `ASK`/`DENY` 映射为
`side_effecting`；随后 static composition 可以收紧 disposition，但不会借此改写 Effect。因此最终
`action=ASK` 完全可以和 `effect=read_only` 同时出现。

## Policy 与 Effect 独立

Policy 回答“静态规则是否允许继续”；Effect 回答“执行可能如何改变外部现实”。二者不能合成一个布尔值。

例子：

| Policy | Effect | 含义 |
|---|---|---|
| `ALLOW` | `read_only` | 无需 Approval，但仍受 runtime/deadline/path gate。 |
| `ASK` | `read_only` | 需要 fresh Approval；成功后不会仅因 ASK 建立 Verification obligation。 |
| `ASK` | `side_effecting` | 需要 fresh Approval，成功后还需要 Verification。 |
| `ALLOW` | `side_effecting` | 无需 Approval，但 side-effect durability/Verification 不会消失。 |
| `DENY` | 任意 | 不执行；Effect 不能抵消 DENY。 |

MCP 的 Effect 来自 Harness-local mapping：`MCPRegistry.effect_for`。server 自报 metadata 不能把
`unknown` 或 side effect 降级成 read-only。

## `DENY > ASK > ALLOW`

`policy_composition.DECISION_RANK` 定义：

```text
ALLOW < ASK < DENY
```

`compose_static_policy` 组合多个层时选择最严格结果，而不是“最后配置覆盖前面配置”。因此：

- 任一安全层 `DENY`，最终就是 `DENY`；
- 没有 DENY 但任一层要求 `ASK`，最终就是 `ASK`；
- 所有层都允许时才是 `ALLOW`。

Approval 只能满足 `ASK`，不能把 `DENY` 改成 `ALLOW`。

## Static Policy Composition

当前静态输入包括：

- Global Security Policy；
- Trust Zone；
- Capability Profile；
- Delegated ceiling；
- 对 MCP capability 的 Harness-local mapping（它产生可信的 MCP disposition/Effect 输入，不是第五个
  `StaticPolicyLayer`）。

`StaticPolicyLayer` 表示一层决定；`CapabilityProfile` 表示 tool/write/MCP 等能力上限；
`EffectivePolicy` 保存 composed static disposition、`max_effect` ceiling、tool/write/MCP ceilings 和 limiting
trace。当前 action 的 Effect 不存放在 `EffectivePolicy` 中；`EffectivePolicy.authorize(tool, effect)` 才把
这些 ceiling 应用于一次具体 action，产生 per-action final authorization。

Policy composition 只处理静态 ceiling。它不读取 pause、deadline、checkpoint、retry 或
Verification，这些属于 runtime gates。

## Trust Zone 与 Capability Profile

Trust Zone 描述请求所在的信任区域，例如 workspace 与 external capability。区域决定可应用的静态
限制，但不会自动赋予 tool capability。

Capability Profile 是多个独立维度的集合，而不是一个 admin boolean。`delegated_ceiling` 和
`compose_subagent_policy` 对静态 capability 维度取交集；运行 Subagent 时，
`authority._effective_subagent_authority` 另外收紧 exact tool names 和 `max_steps`：

```text
effective_allowed_tools = requested ∩ parent_allowed_tools
effective_write          = requested_write AND parent_write
effective_mcp            = requested_mcp AND parent_mcp
effective_max_steps      = min(requested, parent)
```

因此 delegated authority 只能衰减，不能因为 child handoff 请求更多权限而提升。

## Worked Trace A：`pwd`，Zone 要求 ASK

这个例子对应
[`test_policy_composition.py`](../test_policy_composition.py) 中的
`test_zone_ask_readonly_uses_approval_without_verification`。测试把 `workspace` Zone 的 disposition 收紧为
`ASK`，其他条件保持允许。

```text
Model Intent
  shell: pwd
    |
    v
Action Classification
  _classify_shell("pwd") -> ALLOW
  effect=read_only
    |
    v
Trust Zone
  workspace
    |
    v
Static Composition
  Global=ALLOW
  Zone=ASK
  Profile(readonly-local)=ALLOW
  Delegation(neutral ceiling)=ALLOW
    |
    v
Effective Static Policy=ASK
  limiting_factor=zone
    |
    v
Capability Authorization
  EffectivePolicy.authorize("shell", "read_only") -> ASK
    |
    v
Runtime Gates
  run-control / governance / persistence / recovery / verification: allowed
    |
    v
persist prepared checkpoint
  -> Human Approval=granted
  -> post-Approval runtime re-check
    |
    v
authorize_action -> AuthorizedAction
  -> dispatch_authorized_action
  -> execute_shell("pwd")
    |
    v
Observation(exit_code=0, safe projection persisted)
  -> requires_verification=false
  -> next final_answer may pass Result Binding
  -> Result
```

关键点是 **ASK ≠ side_effecting**。`ASK` 来自 Zone disposition，回答“是否需要 Human Approval”；
`read_only` 来自 action Effect，回答“执行是否可能改变外部现实”。它们是正交维度，所以该 action 需要
Approval，但成功后不会仅因 ASK 建立 Verification obligation。

## Worked Trace B：Delegation 移除 workspace write

这个例子使用 delegated/subagent composition 路径。真实断言见
[`test_policy_composition.py`](../test_policy_composition.py) 的 `test_delegation_is_monotonic`：即使请求
`workspace-editor`，child 也不能取得 parent/handoff 没有授予的 write capability。

```text
Model Intent
  shell: echo hello > report.md
    |
    v
Action Classification
  effect=side_effecting
    |
    v
Capability Profile: workspace-editor
  policy=ASK
  can_write_workspace=true
    |
    v
Delegated Authority / handoff ceiling
  policy=ALLOW
  can_write_workspace=false
    |
    v
compose_subagent_policy / compose_static_policy
  Effective static disposition=ASK
  Effective capability write=false
  limiting_factor for write=delegation
    |
    v
Capability Authorization
  EffectivePolicy.authorize("shell", "side_effecting") -> DENY
    |
    +--> Human Approval: not requested
    +--> AuthorizedAction: not created
    +--> Executor call count=0
    |
    v
Final Authorization=DENY
```

这里 static disposition 仍可能是 `ASK`，但它不是最终的全部答案。`EffectivePolicy.authorize` 还必须应用
write/tool/effect ceiling；`can_write_workspace=false` 已经使这次 side-effecting shell action 成为 `DENY`。
Capability DENY 发生在 Approval 之前，Human Approval 不能补回 delegated ceiling 已移除的能力。
composition 层的 `DENY` 由上述测试直接断言；实际 Subagent handler `_run_subagent_once` 也在
`dispatch_child` 之前检查 `authority["can_write_workspace"]`，无 write authority 时直接返回 blocked。因此这里的
`Executor call count=0` 是执行边界结果，不是依赖 Tool 自己拒绝。

## Runtime Gates

静态 Policy 通过后，还必须满足当前运行事实：

- `run_control.can_schedule_action`；
- `governance.normal_action_decision`；
- Verification gate；
- persistence degraded gate；
- durability/recovery correlation；
- retry attempt/budget。

`RuntimeGateResult` 属于 policy composition 的窄值类型，但实际 gates 由各 owner 模块计算，agent
orchestrator 负责组合顺序。protected-path check 是独立的 fail-closed security ceiling：它会在
classification/authorization 边界参与判断，但不应被理解成可由 pause、deadline 等 runtime state 改写的
普通 gate。

## Approval

`authority.request_approval` 只处理 `ASK`。Approval 有三个重要性质：

1. 必须对应当前请求；
2. retry/resume 的新 attempt 不能继承旧 Approval；
3. Approval waiting 可冻结 governance active time，但不会重置 budget。

用户在 Approval prompt 输入 `pause` 或 `cancel` 时，prepared checkpoint 保留，executor 不启动，
run control 在可靠边界进入 `paused`/`cancelled`。

## Protected Path ceiling

`protected_paths.inspect_shell_paths`、`inspect_mcp_paths` 和 `inspect_subagent_paths` 是统一的 fail-closed
路径上限。即使 static Policy=`ALLOW` 或 Human Approval=granted，也不能读取 `.env.local`、Harness
内部持久化路径、workspace 外 escape 或受保护 symlink。

这个检查在 `authorize_action` 中再次执行，防止 handler 或外部 embedding 只传入伪造的 policy result。

## AuthorizedAction

`dispatch.authorize_action` 验证：

- checkpoint 必须是 `prepared`；
- capability、arguments、effect 与 checkpoint 完全一致；
- 调用方传入的 `runtime_allowed` 没有拒绝；
- Policy 是 `ALLOW`，或 `ASK` 且 fresh Approval 为真；
- protected path 允许。

当前 main agent handler 在调用 `_dispatch_shell_action`/`_dispatch_mcp_action` 前计算真实 runtime gates，且在
Approval 后重新检查；这两个 dispatch helper 调用 `authorize_action` 时使用默认
`runtime_allowed=True`。因此 `authorize_action` 不会重新读取整个 Run Control/Governance state：它封存调用方
传入的当前 action/checkpoint/Policy/Approval facts，并独立重检 checkpoint binding 和 protected path。
review 时必须同时检查 handler ordering 与 sealed dispatch seam，不能只看其中一个函数。

通过后返回带进程内私有 seal 的 `AuthorizedAction`。普通 dict、手工构造 dataclass 或历史 approval
不能通过 `dispatch_authorized_action`。

## Common Misreadings

- **“ASK means side effect。”错误。** ASK 控制 Approval；Effect 控制 durability 与 Verification。Trace A
  就是 `ASK + read_only`。
- **“Static Policy ALLOW means execution allowed。”错误。** capability ceiling、runtime gates、protected path
  和 exact checkpoint binding 仍可能拒绝执行。
- **“Approval grants reusable authority。”错误。** Approval 只满足当前 ASK；新 attempt/resume 不能继承，
  dispatch 前也必须重新满足当前 gates。
- **“Capability Profile allows write，所以 child 可以 write。”错误。** Delegation 与 parent ceiling 只能继续
  衰减 Profile；任一有效 write ceiling 为 false，side-effecting shell authorization 就是 DENY。

下一步阅读：[`04-action-lifecycle.md`](04-action-lifecycle.md) 和
[`05-planning-retry-governance.md`](05-planning-retry-governance.md)。

## Navigation

- Previous: [`02-agent-loop.md`](02-agent-loop.md)
- Next: [`04-action-lifecycle.md`](04-action-lifecycle.md)
- Related: [`12-security-boundaries.md`](12-security-boundaries.md), [`16-design-decisions.md`](16-design-decisions.md)
