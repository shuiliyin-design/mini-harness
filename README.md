# Mini Harness V24

## V24：Task Result Contract / Final Answer Binding

Model decides how to phrase a candidate. Harness decides what can truthfully
be claimed. **Final Answer ≠ Completion Authority.**

Provider 的 `final_answer` 只是 presentation candidate；可选的
`claimed_status`、`artifact_refs`、`evidence_refs` 也只是 Model claims。Harness
根据 Run Control、terminal state、Plan、Output Contract、accepted Artifact/Evidence
和 reconciliation 状态执行确定性的 `result_binding`，最终生成
`.audit/results/<run-id>.json`。即使结果是 blocked、failed、cancelled 或 incomplete，
仍会产生诚实的 Result Object。

Result 是完整 answer 的唯一交付存储；Audit 和 Run Envelope 只记录 answer 的长度、
SHA-256、状态、引用与冲突原因。`--result-show <run-id>` 显示摘要身份但默认不重复正文，
`--result-check <run-id>` 只检查历史 Result、Artifact/Evidence、Audit 和
`result_binding` replay，不读取当前 filesystem。

V24 不做 semantic fact checker、LLM judge、NLP entailment、second-model rewrite、
automatic citations、signed result、rich artifact UI 或 distributed Result Contract。

## V16：Execution Governance

V16 增加 Harness-owned 的 Tool Timeout、Step/Run Deadline 与 Run 级 execution
budget。UTC deadline 用于 Session/Crash Resume，monotonic clock 用于当前进程耗时；
User Pause 与 Approval waiting 冻结剩余 active duration。每次真正进入 invocation 前
消耗 action budget，Subagent 还消耗独立的委派计数。Deadline 和 budget 不属于
Model Intent，retry、replan、resume 或改写 command 都不能重置。

如果 deadline/cancel 后仍有 side-effecting/unknown checkpoint，Harness 只允许一次
与原 verification target 强绑定的只读 safety reconciliation。该例外不能绕过
Security/DENY、不能启动 retry、不能推进 Plan，完成后 Run 仍保持 blocked。V16 不做
token/cost、Provider quota、CPU/内存、分布式 deadline 或后台调度等资源治理。

## V15：Retry / Backoff / Failure Policy

V15 增加 Harness-owned 的 failure classification、有限 retry budget、确定性
backoff 与 retry state 持久化。明确失败不会自动等同于可重试：只有本地策略允许的
只读 transient failure 才会自动 retry；side-effecting/unknown effect 必须先走
V13 durability reconciliation，DENY、用户拒绝、run pause/cancel 与
`never_auto_retry` 均不能自动重试。默认总 attempts 为 3，等待采用可注入 sleeper，
因此离线测试不需要真实 sleep。

## 历史实现说明

## V9.2：Real MCP Transport / Local stdio Server

V9.2 是教学兼容实现，**明确固定 MCP `2025-11-25`**，用于展示传统
`initialize -> notifications/initialized -> tools/list -> tools/call` 生命周期；
它不声称兼容 latest MCP，也不实现 HTTP/SSE/Streamable HTTP。

`StdioMCPClient` 使用 Python 标准库启动独立的 `mcp_demo_server.py` 子进程。
双方通过 stdin/stdout 交换 UTF-8、逐行分隔的 JSON-RPC；server stdout 只承载
协议消息，日志只能写 stderr。进程在 Harness 运行期间常驻，并在退出路径关闭。

本地 registry 保留 fake `mcp:demo:echo`，同时注册真实
`mcp:demo-stdio:echo`。两者均由 Harness 本地配置为 `Policy=ASK`、
`Effect=read_only`；server metadata 不能覆盖 Policy、Approval、Effect 或
Verification。stdio child 使用显式环境 allowlist，不继承 `LLM_API_KEY` 或
`.env.local` 中的 Harness secrets。timeout、crash、EOF、坏 JSON、错 request id
和 JSON-RPC error 都会成为普通 MCP Observation，而不是击穿 Agent Loop。

本阶段不解决 resources、prompts、sampling、subscriptions、并发 multiplexing、
自动重连、OAuth、生产级 sandbox 或多协议版本协商。

## V9：MCP / External Capability Discovery

V9 第一阶段把 MCP 放在 Harness 的 Tool Executor 一侧，而不是 Provider 内部：

```text
Model -> tool_call -> Harness authority -> MCPRegistry/MCPClient
      -> MCP server -> result/error -> Observation -> Model
```

`MCPClient` 是最小 transport abstraction，提供 `list_tools()` 和
`call_tool(name, arguments)`，并为后续只读 resource discovery 预留
`list_resources()` / `read_resource(uri)`。它不决定是否允许调用。
`MCPRegistry` 负责 server 路由、discovery cache 和 Harness 本地 policy 配置；server
返回的 metadata 不能改变 Tool Policy、Approval、Verification 或 secret isolation。
未配置的 MCP tool 默认是 `ASK`，而不是自动信任。

模型继续使用统一的 `tool_call` 协议，不增加 `mcp_tool_call`：

```json
{"type":"tool_call","tool":"mcp:docs:lookup","arguments":{"query":"V9"}}
```

Harness 依次检查引用格式、server/tool 是否存在、arguments 是否符合详细
`inputSchema`，然后才进行本地 Policy/Approval 判断和调用。第一版只实现一个明确
的 JSON Schema 子集：object、array、string、number、integer、boolean、null、
required、properties、additionalProperties、items 和 enum；遇到不支持的 type
会拒绝，而不是宽松放行。调用异常会转换成带 `exit_code=1` 的 Observation，继续
Agent Loop，不等于 Agent failure。

Discovery 采用“两阶段暴露”：标准 MCP `tools/list` 本身已经返回详细
`inputSchema`，因此 Harness 首次 discovery 时接收并在 runtime 私下缓存完整定义，
但只把 `tool` 引用、`description` 和顶层参数的 type/required 摘要组成 compact
capability catalog 交给模型；模型选中某个 tool 后，Harness 才在验证路径中使用
完整 schema。这里的“按需”是模型
上下文与 Harness 使用层面的按需，不虚构 MCP 的单 tool schema endpoint。全量把
schema 暴露给模型实现更直接，也能让模型一次看到所有参数，但 tool 数量或 schema
变大时会在每轮重复占用 V6 context budget，并挤压任务、Observation 与 control
state。轻量 catalog 需要模型先选能力，却让模型上下文成本主要随实际使用的能力
增长，因此作为 V9 默认策略。catalog 与 schema cache 都是 runtime state，不追加
到 Full Session History；catalog 仍参与本轮 working context 的 V6 测量与
compaction。

MCP resources 第一版与 tools 分离：resource 被建模为只读 context source，不能
因为“读取”名义绕过 Harness 的 URI allowlist、大小限制、内容标记和 secret
筛查。V9 只保留 client abstraction，尚未把 resource 正文注入 working context；
这样不会把资源读取偷偷变成 tool action，也不会在权限模型尚未定义时扩大边界。

MCP Tool Policy 与 Tool Effect 是两个独立维度。Policy 决定 `ALLOW`、`ASK` 或
`DENY`；Effect 由 Harness 本地配置为 `read_only`、`side_effecting` 或 `unknown`，
且不采用 MCP server 自报的 effect metadata。未配置或配置非法的 effect 保守视为
`unknown`。成功的 `side_effecting` / `unknown` 调用进入 Verification Gate，成功的
`read_only` 调用不进入；已有 gate 时也只有 `read_only` MCP tool 可用于验证，是否
仍需用户批准则完全由其 Policy 决定。教学 demo 的 `mcp:demo:echo` 本地配置为
`Policy=ASK`、`Effect=read_only`，因此批准并成功执行后可直接返回结果。

默认使用 `FakeProvider`，无需网络或 API Key：

```bash
python mini_harness.py
```

每次不带参数启动都会创建一个唯一 session，并打印 32 位十六进制
`session_id`。对话与 Agent Loop 的 checkpoint 会原子写入项目本地的
`.sessions/<session-id>.json`。程序退出后可显式恢复：

```bash
python mini_harness.py --resume <session-id>
```

恢复会把原有 `messages` 交回 Provider，并在其后追加本次用户输入。这是显式
恢复单个会话，不是长期记忆：V5 不搜索其他 session，不做 RAG、embedding 或
向量数据库。`.sessions/` 已加入 `.gitignore`。

使用真实模型时，默认调用 Chat Completions 兼容接口。`LLM_ENDPOINT` 可以是
`/v1` base URL，也可以是完整 endpoint：

```bash
export MINI_HARNESS_PROVIDER=real
export LLM_ENDPOINT=https://你的服务地址/v1/chat/completions
export LLM_MODEL=模型名称
export LLM_API_KEY=可选的密钥
python mini_harness.py
```

兼容 OpenAI Completions 风格的网关时：

```bash
export MINI_HARNESS_PROVIDER=real
export LLM_ENDPOINT=https://你的服务地址/v1
export LLM_MODEL=sonnet-4.6
export LLM_API_MODE=completions
export LLM_API_KEY=从安全环境注入的密钥
python mini_harness.py
```

`LLM_API_MODE` 可选 `chat-completions`（默认）或 `completions`。后者调用
`/v1/completions`，将 system prompt、用户任务、历史 tool_call 和 Observation
序列化进纯文本 `prompt`，并读取 `choices[0].text`。不要把真实 API Key 写进
源码、文档或日志。

`RealProvider` 本身不依赖任何厂商 SDK。它只依赖一个实现了
`complete(messages) -> str` 的客户端，因此可以用其他客户端替换当前的
`OpenAICompatibleHTTPClient`，而无需改动 Agent Loop 或 Tool Executor。

V6 第一阶段会在每次 `RealProvider` 请求前显示最终待发送上下文的消息数、
字符数和教学级 token 粗估。估算方法是：CJK 统一汉字每个约 1 token，其余
文本整体按约 4 characters/token（向上取整）计算。它**不是模型真实 tokenizer
的结果**，不适合用于精确限额、计费或复现服务端 token usage。

可选环境变量 `MINI_HARNESS_CONTEXT_BUDGET` 必须是正整数，单位是上述估算
token。未设置或未超限时，完整 working context 原样发送；只有设置了预算且
估算超限时，`RealProvider` 才在内存中构造一次 compacted working context。
日志会依次显示：

```text
[Context] before: messages=... characters=... approx_tokens≈...
[Compaction] triggered
[Context] after: messages=... characters=... approx_tokens≈...
```

V6 第二阶段的策略是教学级且确定性的：始终保留 system instructions、当前用户
任务和最近 6 条消息。较老消息由一个 `deterministic_compacted_history`
system message 代替，最多列出最后 12 个旧条目的明确字段：用户文本或最终答案
只做 120 字符截断，tool call 保留 command，Observation 只保留 exit code、拒绝
来源/原因和 verification target，不复制 stdout/stderr。更早条目只记录数量。
它不调用 LLM，也不声称理解、归纳或推断历史。

未完成的 Verification Gate 是 runtime control state。每次请求前，Agent Loop
把 `requires_verification`、`verification_target` 和 `latest_write_command`
复制为仅供 working context 使用的 `active_control_state`；当前 denial / policy /
verification feedback 位于最近消息窗口中，不会被压缩掉。该临时消息和压缩摘要
都不会 append 回 session。

直接丢掉旧消息最省 token、实现也最简单，但模型无法区分“没有发生”与“发生过但
被删掉”，会丢失早期命令、失败和用户约束。确定性摘要略占 token，而且截断必然
损失细节，但至少明确告诉模型哪些历史被省略，并保留可审计的结构化事实；它仍然
不是语义摘要，不能保证保住隐含意图。

压缩后会重新测量一次。如果仍超过预算，本版只打印
`[Context Warning] compacted context still exceeds budget`，然后发送一次，不递归
压缩。Warning-and-continue 对粗略估算和很小的教学预算更宽容；hard error 能严格
执行上限、避免服务端拒绝，但会让过小预算直接阻断任务。这里选择前者，同时明确
暴露超限，不制造无限压缩循环。

Session JSON 始终保存完整原始 `messages`。恢复 session 后，每一轮都根据当时的
budget 从完整历史重新构造 working context；压缩结果不持久化、不覆盖历史，也不
改变 Session JSON schema。统计日志只含聚合数字，不含消息正文、API Key、
Authorization 或 `.env.local` 内容。

V7 在每次 `RealProvider` 请求前增加独立的 runtime context assembly。它从当前
项目根目录重新读取 `AGENTS.md`，扫描 `skills/*/SKILL.md`，并依次组合 Harness
system instructions、当前 project instructions、Skill catalog、至多一个 active
Skill body、Session working context 与 active control state。项目正文只存在于
本次 working context，不会追加或保存进 Session JSON；resume 后也以当前
filesystem 为准。

`SKILL.md` 使用不依赖 YAML 库的固定 frontmatter，目录名必须与 `name` 相同，
`name` 只允许小写字母、数字和连字符：

```text
---
name: python-testing
description: pytest Python tests
---
这里是按需加载的 Skill body。
```

Catalog 只包含 `name` 和 `description`。V7 先匹配任务中明确出现的完整 Skill
name，否则按 description 中以空白或标点分开的英文单词/连续中文词组做确定性
包含匹配；无匹配或最高分并列时不加载 body。这不是 semantic search。所有项目
context 都被标记为 untrusted，不能改变 Python Tool Policy、Approval、
Verification 或 secret isolation。解析后逃出项目根目录的文件也不会被读取。

Project instructions、Catalog 和 active Skill body 都在 assembly 后参与字符数、
近似 token 与 budget 测量。触发 compaction 时，这些当前 runtime project blocks
与 active Skill 会被保留；只有旧 Session history 继续按 V6 的确定性规则压缩。

V1 只接受模型输出以下两种 JSON，且只支持 `shell` 工具：

```json
{"type":"tool_call","tool":"shell","command":"pwd"}
```

```json
{"type":"final_answer","final_answer":"任务已经完成。"}
```

V2 在 Agent Loop 与 Tool Executor 之间加入教学级 Tool Policy：非常有限的
简单只读命令自动执行，workspace 内可接受的修改请求用户批准，明确危险命令
直接拒绝。复杂或无法可靠识别的 shell 命令默认请求批准。拒绝会作为结构化
Observation 返回模型，不会中断 Agent Loop。该策略不是生产级安全 sandbox。

V3 增加 Verification Gate：获批的 `ASK` 命令执行成功后，模型必须再成功
执行一次只读 `ALLOW` 命令，Harness 才接受最终答案。

V4 在该门禁上增加教学级 Verification Quality。Harness 能从严格、简单的
`echo '...' > file`、`touch file` 和 `mkdir dir` 中识别单一 workspace 相对
目标。文件只接受读取同一路径的 `cat`，目录只接受列出同一路径的 `ls`；
无关的 `ALLOW` 命令不会执行，也不会解除门禁。绝对路径、`..` 逃逸、多个
目标、shell 展开和复杂命令均不会被猜测。

如果成功的 `ASK` 写操作无法可靠提取目标，V4 会明确 fallback 到 V3：任意
成功的只读 `ALLOW` 命令都可解除门禁。这个 fallback 仅保留 V3 的可用性，
**不代表 evidence 相关性已经得到验证**。V4 不判断内容语义，不提供
diff/hash proof、多文件 effect tracing、测试语义判断或生产级 shell 解析。

V5 的 session 保存 `version`、`session_id`、UTC 创建/更新时间和完整消息历史。
它不保存 `step counter`，因为 step 是每次 Agent Loop 的防无限循环预算，恢复
后应重新计数。未完成的 Verification Gate 则是不能丢失的安全约束，因此以
一个 `verification` checkpoint 保存 `requires_verification`、
`verification_target` 与 `latest_write_command`；恢复后仍必须先取得合格的
只读证据。临时的重复回答检测等进程内细节不持久化。

## V18：Policy Composition / Capability Profiles / Trust Zones

V18 把静态 Authority ceiling 明确组合为 Global Security Policy、Harness
分配的 Trust Zone、Harness-owned Capability Profile 与可选的 Delegated
Authority。各维度只取交集：`DENY > ASK > ALLOW`，effect 取更严格上限，
tool allowlist 取交集，write/MCP capability 使用 logical AND。未知 zone、profile
或非法安全字段 fail closed。内建 profile 写在 Python 中，不从项目文件加载。

**Policy Composition ≠ Runtime Gate**：组合回答“静态能力上限是什么”；Run
Control、Deadline/Budget、Durability、Retry 与 Verification 仍独立回答“当前能否
继续”。静态 `ALLOW` 不跳过这些 gate，也不等于立即执行。

**Trust Zone ≠ trusted content**：`harness_local`、`workspace`、`external` 是
Harness 本地定义的权限边界，不是对内容真实性的判断。Model、MCP metadata、
Memory、Subagent、`AGENTS.md` 和 Skill 都不能声明或提升 zone。

**Capability Profile ≠ Project Skill**：Profile 是 Harness Python 中的安全上限；
项目 Instructions/Skill 只是标记为 untrusted 的 behavioral context，可以建议测试
方式，但不能授予 tool、write、MCP 或 approval authority。

**Safety Reconciliation Permit ≠ policy bypass**：它只是针对 unknown side
effect 的单目标、只读、一次性 runtime permit。它不恢复 cancelled run，不增加
budget/retry，也永远不能覆盖 Global DENY 或 Secret Policy。

## V19：Policy Snapshot / Drift / Replay

每个新 Run 会把当时的 Harness-owned Authority definitions 规范化为 sorted-key
canonical JSON，以 UTF-8 做 SHA-256，并绑定不可变 fingerprint。快照按内容地址
保存在 `.audit/policies/<fingerprint>.json`；Session、Project Instructions、Skill、
Memory、Observation、deadline/retry state、runtime permit 与 secret 都不进入快照。
Run 内不 hot reload，resume 则创建使用 Current Policy 的新 Run，不恢复旧权限。

历史解释不会用当前配置覆盖旧决策。以下命令分别比较 fingerprint、显示有限的
Authority 字段差异，以及用历史 snapshot 和 Audit composition inputs 重算静态策略：

```bash
python mini_harness.py --policy-status RUN_ID
python mini_harness.py --policy-diff RUN_ID
python mini_harness.py --policy-replay RUN_ID
```

缺少历史 binding/snapshot 或遇到未知 schema 会明确失败，不 fallback Current
Policy。`POLICY REPLAY` 只验证 Effective Static Policy 的可复现性，不调用 Model、
不执行 Tool，也不重放 Run Control、Deadline、Durability、Retry、Verification 或
Safety Reconciliation，因此它不等于 Final Authorization replay。

## V20：Run Manifest / Reproducible Execution Record

每个可审计 Run 在 `run_started` 之前生成不可变 Manifest，保存到
`.audit/manifests/<run-id>.json`。Manifest 只记录 Harness、Model、已绑定 Policy、
Project Context、Capability、选中 Memory 与 Context Strategy 的安全 identity；
不保存正文、raw endpoint、认证信息、Working Context、MCP schema/result 或模型推理。
`configuration_fingerprint` 只对 sorted-key canonical `configuration` 做 SHA-256，
不包含 run/session/time。Resume 会按当前 filesystem、Memory、MCP discovery、Model、
Context Strategy 和 Current Policy 生成新 Manifest，旧 Manifest 不会成为当前现实。

```bash
python mini_harness.py --manifest-show RUN_ID
python mini_harness.py --manifest-status RUN_ID
python mini_harness.py --manifest-diff RUN_ID
python mini_harness.py --manifest-check RUN_ID
python mini_harness.py --manifest-reconstruct RUN_ID
```

`status` 比较历史与当前 runtime identity，`diff` 只解释已知复现维度，`check`
只检查历史 Manifest 与其 Policy Snapshot 的内部完整性。`reconstruct` 仅作安全的
描述性重建，不调用 Model、不执行 Tool，也不承诺自然语言输出可确定性重放。

## V21：Deterministic Run Envelope / Replay Boundary

四种历史对象保持分工：Policy Snapshot 是 **Historical Authority Definition**，
Run Manifest 是 **Historical Runtime Configuration Identity**，Run Envelope 是
**Historical Execution Input Identity**，Audit 是 **Historical Event Trace**。
它们通过 fingerprint/reference 关联，不互相复制任务、Session、AGENTS、Skill、
Memory、MCP description 或模型自然语言正文。

每个 Run 在 `run_started` 前创建 `.audit/envelopes/<run-id>.json`。初始 `inputs`
只记录 task reference/length/digest、Full Session Source digest/count、Manifest 与
Policy fingerprint，以及 Project Context、Memory selection、Capability catalog、
Plan 和 Control State 的 identity。Envelope fingerprint 只覆盖初始 inputs，不覆盖
run/session/time 或执行中追加的 request/transition。原始用户消息仍以 Session Store
为事实源，Envelope 不是第二套 Session。

Provider transport 前会先记录 prepared messages 的 digest、数量与测量；返回后只绑定
decision event、type 和 digest。Envelope 不保存完整 messages 或自然语言输出：
**same Envelope ≠ same Model output**，也不保存 hidden reasoning、API key、Authorization、
Bearer、raw environment/header 或 `.env.local`。

```bash
python mini_harness.py --envelope-show RUN_ID
python mini_harness.py --envelope-check RUN_ID
python mini_harness.py --replay-check RUN_ID
```

`--envelope-check` 是 Level 1 Identity Check：校验 schema、fingerprint、Manifest、
Policy、request/transition identity 与 forbidden fields。`--replay-check` 再执行 Level 2：
只把 historical recorded inputs 交给已有 Harness 纯逻辑，重算已记录且证据完整的 policy、
planning、retry、verification、governance transition；证据或 adapter 不足时明确显示
`UNAVAILABLE`，不猜。
它不会调用 LLM、Tool、MCP、Subagent 或 Human Approval。

Historical Observation 只回答“给定当时记录的 observation，Harness 是否产生相同状态
转换”，不表示当前 filesystem/network reality 仍相同。Historical Approval 只能证明
历史 action 当时存在 approval event，不能生成批准或成为新 Run 的权限。Level 3 External
Re-execution 在 V21 明确为 **NOT SUPPORTED**；任何外部重执行必须创建新 Run，使用当前
Policy、当前 Approval 和当前 Reality。Replay ≠ Re-execution。

Verification transition 会把当时 read-only observation 的 event reference、exit code、
stdout/stderr length 与 SHA-256 记录为 `historical_recorded_observation=true` 的安全输入，
不保存 stdout/stderr 原文。Replay 仅用这些 recorded metadata 重算 Verification Gate；
不会重新 `cat`、读取 filesystem 或观察 cwd。它只能证明“当时 Harness 收到了该
Observation”，不能证明“Current Reality 仍然如此”。

同一 run_id 的 crash recovery 保留初始 Envelope，仅继续追加 records；Session resume
若创建新 Run，则必须创建新 Envelope，不能把旧输入 identity 当作当前现实。V21 不提供
deterministic LLM/model seed replay、network/HTTP archive、完整 stdout、approval simulation、
side-effect replay、VM/filesystem snapshot、full message duplication 或 cryptographic
attestation。

运行离线测试：

```bash
python -m unittest -v
```

## V22：Artifact / Evidence Provenance

Evidence 是 Harness-owned 的不可变 provenance object，保存于
`.audit/evidence/<evidence-id>.json`。它只记录明确的 Subject/Claim、Source、Harness
Verification、Freshness、Artifact/Observation identity 与引用；不保存 raw stdout/stderr、
完整 MCP result、Project Instructions、Skill、Memory 或 hidden reasoning。Fingerprint 使用
canonical JSON + SHA-256，并排除 evidence ID、创建时间和 fingerprint 字段本身。

`ArtifactRef` 只表示历史时点观察到的 workspace 相对路径、SHA-256 和 size。它不是 artifact
store，也不证明当前文件仍相同。Historical Evidence 可用于 deterministic replay，但涉及
Current Environment Reality 的新 Run step 必须取得 fresh Observation 并创建 New Evidence。

```bash
python mini_harness.py --evidence-show EVIDENCE_ID
python mini_harness.py --evidence-trace EVIDENCE_ID
python mini_harness.py --evidence-check EVIDENCE_ID
```

`show` 只显示安全 metadata，`trace` 沿已记录引用确定性追踪且对缺失层显示 `unavailable`，
`check` 只检查 Historical Integrity，不读取当前 filesystem 验证历史 file hash。V22 不提供
`--evidence-refresh`：Refresh = New Observation → New Evidence，而不是修改 Historical
Evidence。它提供教学级 provenance 和普通损坏检测，不是签名、不可抵赖证明或对本机写权限
攻击者的防护。

## V23：Artifact Lifecycle / Output Contract

V23 把“文件存在”和“本 Run 的可靠交付物”分开。Model 只能提出 candidate；Harness 在
写 action 成功后对明确识别出的 `workspace_file` 做 fresh observation，复用 V22
Verification Evidence，再用 Harness-owned Output Contract 做确定性 acceptance。第一版
Contract 不是 workflow DSL，只支持 `exists`、`non_empty`、`content_identity` 和
`verified`。`path` 字段天然采用精确匹配，因此不再增加重复的 `exact_path` requirement。

```json
{
  "required_artifacts": [{
    "name": "report",
    "artifact_type": "workspace_file",
    "path": "report.md",
    "requirements": ["exists", "non_empty", "content_identity", "verified"]
  }]
}
```

生命周期保持为 `proposed → materialized → verified → accepted/rejected`。Artifact
Record 是历史文件版本，保存在 `.audit/artifacts/<artifact-id>.json`，只含 path、SHA-256、
size、producer、Evidence IDs、Contract 和引用，不含文件正文或 raw stdout/stderr。Record
fingerprint 与内容 SHA-256 是两件事：前者覆盖稳定 record metadata，并排除 artifact ID、
创建时间与 fingerprint 自身；后者只标识当时的文件字节。

记录一经保存不可修改。同一路径产生新内容时创建新 Artifact，并用
`supersedes_artifact_id` 指向旧版本。旧版本仍保持历史 `accepted` 事实；“已被取代”是由
新记录关系推导的 current-view 状态，而不是回写旧记录的第六种可变状态。这样保留完整
provenance，同时避免状态爆炸。当前 filesystem 后来变化也不会改写旧 Artifact；若要满足
当前 Contract，必须 New Observation → New identity → New Evidence → New Artifact。

有 Output Contract 的 Run 只有在全部 required artifacts 都是 current accepted output 时，
`final_answer` 才能把 Run 标为 `completed`；否则记录
`incomplete / output contract unsatisfied`。没有 Contract 的 reactive Run 继续保持原行为。
Plan step 仍先经过 Evidence Gate；若显式提交 `output_artifact_ids`，还必须通过 Artifact
Gate。Subagent return 只能是 candidate，MCP external result 也不会自动成为 workspace
Artifact；两者都需要 Main Harness fresh grounding/accepted Evidence。

```bash
python mini_harness.py --artifact-show ARTIFACT_ID
python mini_harness.py --artifact-check ARTIFACT_ID
python mini_harness.py --artifact-trace ARTIFACT_ID
python mini_harness.py --outputs RUN_ID
```

`artifact-check` 只检查 Historical Record schema/fingerprint、Evidence/Audit references、
safe path、content identity 与 supersession relation，不读取 Current filesystem。
`artifact-trace` 展示 Artifact ← Evidence ← Verification/Observation ← Action ←
Model/Policy/Approval 的历史引用链。`outputs` 汇总 required、current accepted 和 unsatisfied
requirements。

`artifact_contract` 是 Run Envelope 中的新 deterministic transition。Replay 输入只含历史
Artifact identity、Evidence identities 和 Contract requirement，输出为 accepted/rejected、
reason 与 unsatisfied requirements；Historical Replay 从不读取当前文件。V23 只正式管理
`workspace_file`，不实现 blob store、上传下载、external URL、Git versioning、binary diff、
rollback、自动删除或 cloud storage。
