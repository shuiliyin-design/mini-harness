# Mini Harness V16

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

运行离线测试：

```bash
python -m unittest -v
```
