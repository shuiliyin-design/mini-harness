# Mini Harness V2

默认使用 `FakeProvider`，无需网络或 API Key：

```bash
python mini_harness.py
```

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

运行离线测试：

```bash
python -m unittest -v
```
