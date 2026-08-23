# AI Digest Subscription Agent

这是 Phase 3 离线应用垂直切片。完整设计从
[`docs/phase3/README.md`](../../docs/phase3/README.md) 开始。

当前实现：

```text
apps/digest_agent/
  README.md
  __init__.py
  domain.py                 Subscription、Feedback/Profile、deterministic rules
  contracts.py              Subscription / Digest I/O validation
  repositories.py           repository protocols
  services.py               Subscription、Feedback、Delivery application services
  workflows.py              generate_digest orchestration
  adapters/
    sqlite.py               stdlib sqlite3 repository + forward schema v3
    search.py               offline MCP-shaped Fake Search
    provider.py             deterministic FakeDigestProvider
    delivery.py             Fake + authorized Termux delivery mapping
    workspace.py            fixed Artifact materialize/observe adapter
```

依赖方向固定为：

```text
adapters -> services / workflows -> domain / contracts
                         |
                         +-> Mini Harness public façade
```

当前闭环覆盖：natural-language Subscription → SQLite → manual run → Fake Search Observation →
Harness verification Evidence → deterministic ranking/synthesis contract → workspace Artifact →
authoritative Result → SQLite Digest。

第二条闭环覆盖：Digest → stable Feedback → atomic SQLite Profile update → safe projection →
下一次 deterministic ranking change。每条 Digest 保存原 profile projection identity 和固定五分量
score breakdown；Profile state 不冒充 Evidence。

第三条闭环覆盖：completed Digest → durable Delivery attempt → Fake accepted/failed/unknown。稳定
digest+channel identity 阻止重复 dispatch；只有 `failed/not_started` 可显式创建下一 attempt，
unknown 不盲重试。Termux adapter 只映射 safe preview 和既有 Environment certainty。

本切片没有 Brave/network 或 HTTP，也没有修改
`mini_harness_core`。运行应用测试：

```bash
python -m unittest discover -s tests/apps -q
```
