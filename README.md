# Mini Harness V7

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

运行离线测试：

```bash
python -m unittest -v
```
