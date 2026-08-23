# AI Digest Subscription Agent

这是 Phase 3 离线应用垂直切片。完整设计从
[`docs/phase3/README.md`](../../docs/phase3/README.md) 开始。

当前实现：

```text
apps/digest_agent/
  README.md
  __init__.py
  domain.py                 Subscription、Feedback/Profile、deterministic rules
  application.py            DigestApplication façade + public DTOs
  bootstrap.py              explicit config + safe startup readiness + wiring
  cli.py                    thin human/JSON application transport
  web.py                    loopback HTTP API + server-rendered mobile UI
  contracts.py              Subscription / Digest I/O validation
  repositories.py           repository protocols
  services.py               Subscription、Feedback、Delivery application services
  workflows.py              generate_digest orchestration
  adapters/
    sqlite.py               stdlib sqlite3 repository + forward schema v4
    search.py               shared safe contract + Fake/real Brave Search
    provider.py             deterministic Fake + real Vertex synthesis
    delivery.py             Fake + authorized Termux delivery mapping
    workspace.py            fixed Artifact materialize/observe adapter
```

依赖方向固定为：

```text
adapters -> services / workflows -> domain / contracts
                         |
                         +-> Mini Harness public façade
```

当前闭环覆盖：natural-language Subscription → SQLite → manual run → Fake/Brave Search Observation →
Harness verification Evidence → deterministic ranking/synthesis contract → workspace Artifact →
authoritative Result → SQLite Digest。

`DigestApplication` 现在是 CLI/未来 HTTP/UI/tests 的稳定业务入口，提供 versioned Subscription lifecycle、
幂等 Run/recovery、Digest query、Delivery、Feedback/Profile；公开 DTO 不暴露 Harness/Artifact/Evidence/SQLite
内部对象。SQLite schema v6 保存 idempotency identity、独立 Harness binding、Subscription snapshot/version、
application run timestamps、safe failure provenance，以及不复制 Harness Audit 的最小 admin recovery operation。

Loopback Web transport 只消费 `DigestApplication` DTO，固定监听 `127.0.0.1`，提供自然语言创建、Run now、
Digest、Feedback/Profile 与 fake Delivery 的手机页面。它不 import repository/Harness，也不提供 admin recovery。

第二条闭环覆盖：Digest → stable Feedback → atomic SQLite Profile update → safe projection →
下一次 deterministic ranking change。每条 Digest 保存原 profile projection identity 和固定五分量
score breakdown；Profile state 不冒充 Evidence。

第三条闭环覆盖：completed Digest → durable Delivery attempt → Fake accepted/failed/unknown。稳定
digest+channel identity 阻止重复 dispatch；只有 `failed/not_started` 可显式创建下一 attempt，
unknown 不盲重试。Termux adapter 只映射 safe preview 和既有 Environment certainty。

真实 Brave 使用 stdlib `urllib`、固定 HTTPS/timeout/body/count boundary，并且只从
`BRAVE_SEARCH_API_KEY` 读取 credential；raw response 不越过 adapter。它是 opt-in manual smoke，
correctness 仍由 fake HTTP/FakeProvider 离线证明。多词 topic 使用保守 lexical provenance；合法的
incomplete smoke 会输出 safe reason 并返回 non-zero。本切片没有修改 `mini_harness_core`。运行应用测试：

`VertexDigestProvider` 复用当前 `LLM_*` Vertex-backed LiteLLM HTTPS 配置。Prompt 只包含
Subscription/candidate/Evidence/Profile safe projections；模型输出是 strict structured candidate，
最终 length/source/ranking/Artifact/Result 仍由原 deterministic contract 与 Harness 决定。

```bash
python -m unittest discover -s tests/apps -q
```

显式真实搜索 + FakeProvider smoke：

```bash
BRAVE_SEARCH_API_KEY=... python -m tools.brave_search_smoke
```

显式 Fake Search + Real Vertex，随后 Real Brave + Real Vertex smoke：

```bash
python -m tools.vertex_digest_smoke
```

最短本地 Demo（默认显式全 fake，不读取 key 自动切换）：

```bash
python -m apps.digest_agent.cli readiness
python -m apps.digest_agent.cli subscription-create --request "订阅 AI 行业动态，600 字以内"
python -m apps.digest_agent.cli subscription-list
```

手机浏览器本地 Demo：

```bash
python -m apps.digest_agent.web --search-provider fake --llm-provider fake --delivery-provider fake
# 打开 http://127.0.0.1:8765/
```
