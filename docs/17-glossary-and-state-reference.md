# Glossary 与 State Reference

## 读完你应该理解什么

这是一份源码速查表，不是新的统一状态机。相同英文词可能出现在不同 owner 中；review 时必须同时写出层级，
例如 Action `failed`、Plan `failed` 或 Result `failed`。

## 核心对象与边界术语

| Term | 属于哪一层 | 精确定义 | 最容易混淆 |
|---|---|---|---|
| Run | Runtime / lineage | 一次 `run_agent` 执行生命周期及其 `run_id`；Audit、Manifest、Envelope、Evidence、Artifact、Result 以它关联 | Session；一个 Session 可承载多次运行连续性，Run 不是聊天历史 |
| Session | Continuity persistence | 保存完整 conversation messages 与可恢复 runtime projections 的版本化记录 | Runtime truth；Session 可能落后，不能代替 fresh Observation |
| Plan | Planning | 有 version、goal、status、dependency steps 与 bounded replan count 的 Harness-owned任务结构 | Model intent；模型不能直接推进 Plan 状态 |
| Step | Planning | Plan 中具有 `pending/in_progress/completed/failed/blocked` 状态与依赖的最小工作单元 | Action；一个 Step 可包含多个 Action/Attempt |
| Action | Execution lifecycle | 一个绑定 exact capability、arguments、Effect、Plan identity 和 `action_id` 的执行单位 | Attempt；Retry 会为同一 logical action 创建新 attempt/action checkpoint |
| Attempt | Retry | `logical_action_id` 下的一次执行尝试，由 `attempt_count/max_attempts` 约束 | Replay；Attempt 会执行，Replay 不执行外部工作 |
| Checkpoint | Durability | 本文通常指 Action checkpoint：在 side effect 前后持久化 `prepared/executing/...` 与 safe Observation identity | Audit Event；Audit 可解释，但不是 action recovery truth source |
| Observation | Environment boundary | Tool/MCP/Subagent 返回的外部事实；raw 只在 handler 内短暂存在，跨边界前投影为 safe identity | Evidence；Observation 是输入事实，Evidence 是 Harness 保存的 claim/provenance record |
| Evidence | Historical claim | immutable claim、source、verification、freshness 与 content identity record | Current Reality；integrity/freshness metadata 不自动证明环境仍未变化 |
| Artifact | Delivery history | 某个 deliverable version 的 path/content identity、producer、Evidence 和 contract status；不保存文件正文 | workspace file；Artifact 是历史身份，不是当前 bytes |
| Output Contract | Delivery acceptance | required Artifact 类型/path/requirements，以及 deterministic acceptance/current gate | Plan completion；Plan completed 仍可能 contract unsatisfied |
| Result | Terminal delivery | Harness 绑定的 `completed/blocked/failed/cancelled/incomplete`、safe answer 与 accepted refs | Model final answer；后者只是 candidate |
| Audit Event | Observability | 一个有 actor、event type、outcome、safe references 和 sequence 的 append-only JSONL event | Session/checkpoint；Audit 不是恢复状态机或 raw log |
| Policy Snapshot | Historical Authority definition | content-addressed、immutable 的当时 static Policy definitions，用于 drift/replay | Current Policy；历史 Snapshot 不能授权新执行 |
| Manifest | Historical configuration identity | per-Run provider/policy/project/memory/context/capability configuration identity | Envelope；Manifest 不记录 request/decision transitions |
| Envelope | Historical replay identity | initial input identities、Provider request/decision digests 和 pure Harness transition records | raw prompt archive；fingerprint 只覆盖 initial inputs |
| Bundle | Portable history | indexed historical closure 的只读导出，可 offline show/check/replay | resume package；Bundle 不含 reusable Approval、Session authority 或 executor |
| Handoff | Delegation contract | Main 为 Subagent 创建的 goal、allowed tools、authority ceiling、verification 与 return contract | shared Plan；Handoff 不转移 Main Plan ownership |
| Subagent | Delegated runtime | 使用隔离 messages/steps/Observation/Verification 和衰减 Authority 的子执行体 | trusted zone；Subagent metadata/角色名不提升 Authority |
| MCP Capability | External capability mapping | `mcp:server:tool` 引用、discovered schema 与 Harness-owned local Effect/Policy mapping | MCP metadata authority；server description/effect claim 不授权执行 |
| AuthorizedAction | Final dispatch capability | `authorize_action` 私有 seal 创建、绑定 exact prepared checkpoint/current authorization 的内存对象 | Approval/Policy decision；它们是必要输入，但都不能单独执行 |
| Effect | Classification / durability | `read_only/side_effecting/unknown`，决定 replay safety 与 Verification 义务 | Policy disposition；`ASK` 不等于 side-effecting |
| Approval | Human gate | ASK action 在当前 attempt 上的 fresh human decision | reusable Authority；旧 Approval 不继承到 resume/new attempt |
| Reconciliation | Recovery | 对 unknown effect 做 targeted read-only observation，得出 applied/not-applied/uncertain | Retry/Verification；它不重做原 action，也不等于普通 step verification |
| Current Reality | Live environment | 当前 filesystem/runtime 的 fresh Observation 与相应 gate 结果 | Historical integrity；MATCH 只能说明旧记录自洽 |
| Historical Integrity | Historical check | fingerprint、reference closure、schema 或 deterministic replay 对历史对象的自洽检查 | authenticity/freshness；它既不是签名，也不是 Current Reality authority |

## 状态术语

| State term | 属于哪一层 | 精确定义 | 最容易混淆 |
|---|---|---|---|
| `prepared` | Action checkpoint | intent/checkpoint 已固定，executor 尚未开始；Approval/runtime facts仍可能过期 | authorized/executing；prepared 不代表永久许可 |
| `executing` | Action checkpoint | dispatch 已跨过 pre-tool durable boundary，副作用即将或可能已经发生 | succeeded；它不能证明 outcome |
| `succeeded` | Action checkpoint | definite terminal success 的 safe Observation 已进入 terminal checkpoint | Step/Run completed；后者还需 Verification/Plan/Contract/Result gates |
| `failed` | Action / Plan / Result，各自 owner | Action：definite nonzero terminal Observation；Plan：step/plan terminal failure；Result：terminal failure precedence | blocked/incomplete；side-effecting nonzero 也未必证明“未产生效果” |
| `unknown` | Action checkpoint / Effect certainty | Harness 不能确定外部 effect applied 或 not-applied；也可作为保守 Effect classification | **Run Result**；Result schema 没有 `unknown` status |
| `pending` | Plan Step | step 尚未开始，等待 dependency/selection | prepared；pending 是任务计划状态，不是 action durability |
| `in_progress` | Plan Step | step 已被 `start_step` 选中，尚未完成/失败/阻止 | executing；Plan step 可跨多个 action |
| `active` | Plan | Plan 仍可选择/推进 step | Run Control `running`；两者 owner 不同 |
| `completed` | Plan / Retry / Result | 各 owner 的成功终态；Result completed 还要求 authoritative completion gates 全部满足 | Tool succeeded 或 Model claimed completed |
| `blocked` | Retry / Plan / Result | owner 当前不允许继续正常工作，通常由 safety/governance/policy/replan limit 产生 | `failed`；blocked 不要求可靠 terminal Tool failure |
| `pause_requested` | Run Control | pause 已请求，in-flight action 需先到 cooperative reliable boundary | paused；请求尚未 settle |
| `paused` | Run Control | cooperative pause 已 settle，可通过 explicit `resume_run` 回到 running | terminal failure；paused 可恢复且 budget 不重置 |
| `cancel_requested` | Run Control | cancel 已请求，in-flight action 先 truthful settle | cancelled；尚处于请求边界 |
| `cancelled` | Run Control / Result | terminal user/run-control cancellation，不可 resume；Result precedence 独立于 failure | failed；cancel 是控制终止，不是 Tool failure |
| `incomplete` | Result | required Plan/Output Contract/Evidence/candidate completion gate 未满足 | failed；它表示没完成，不一定发生错误 |
| `degraded` | Runtime Verification/persistence flag | live Harness 明确捕获 persistence/observability failure，知道记录能力受损并限制 side effect | Action state/crash；它不是第六个 checkpoint state，InjectedFault 不自动写它 |
| `reconciled` | Checkpoint Observation metadata | fresh targeted read-only evidence 确认 unknown file write 已 applied；Action transition 到 `succeeded` | Action state；`reconciled` 不在 `ACTION_STATES` 中 |
| `reconciled_not_applied` | Checkpoint Observation metadata | strict absence Observation 确认 unknown file write 未应用；Action transition 到 `failed` | ordinary failure；它是唯一可支持 reopening retry gate 的特殊 recovery fact |

## 三个必须记住的区分

```text
unknown    = Action effect certainty，不是 Run Result
incomplete = Result completion gate 未满足，不是 Tool failure
blocked    = 当前不允许 normal work，不等于 failed
```

## Navigation

- Previous: [`16-design-decisions.md`](16-design-decisions.md)
- Next: [`18-version-learning-map.md`](18-version-learning-map.md)
- Related: [`04-action-lifecycle.md`](04-action-lifecycle.md)、[`09-audit-and-historical-objects.md`](09-audit-and-historical-objects.md)、
  [`13-failure-semantics.md`](13-failure-semantics.md)
