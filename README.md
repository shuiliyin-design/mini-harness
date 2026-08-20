# Mini Harness V5

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
