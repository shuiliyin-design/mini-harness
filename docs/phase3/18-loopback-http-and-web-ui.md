# Loopback HTTP API + Minimal Web UI

## Boundary and implementation

```text
Browser -> stdlib HTTP -> DigestApplication DTOs -> existing services/workflow -> Harness
```

`apps/digest_agent/web.py` imports only the application façade and shared bootstrap/readiness seam. It does
not import repositories, workflows, SQLite, Evidence, Artifact, Result, Audit or Harness code. Endpoints own
HTTP parsing/status/rendering only; business validation, versioning and idempotency remain in `DigestApplication`.

The server uses Python `ThreadingHTTPServer`: no third-party dependency, npm project or serialization stack is
needed for this small teaching surface. It rejects every bind except `127.0.0.1`.

## API and execution

| Method/path | Application operation |
|---|---|
| `GET /health`, `GET /ready` | liveness / existing passive readiness |
| `POST/GET /subscriptions` | natural-language create / list |
| `GET/PATCH /subscriptions/{id}` | get / versioned update |
| `POST /subscriptions/{id}/enable|disable` | versioned lifecycle |
| `POST /subscriptions/{id}/runs` | idempotent synchronous demo run |
| `GET /runs/{id}` | safe application status |
| `GET /digests[/{id}]` | safe Digest projections |
| `POST /digests/{id}/deliver` | existing idempotent delivery |
| `POST /digests/{id}/feedback` | existing stable feedback event |
| `GET /profile` | safe topic weights and rule version |

Run execution is synchronous in V1 because the façade already implements one bounded synchronous operation.
Brave/Vertex adapters have finite timeouts; HTTP does not invent a queue, worker or state machine. The response
returns the application projection, and `GET /runs/{id}` supports refresh. `Idempotency-Key` is mandatory, so
a double click reuses one logical Run/Harness binding/Digest.

## Minimal product UI

`GET /` is responsive server-rendered HTML with small inline vanilla JS/CSS. A phone browser can create a
natural-language subscription, enable/disable it, Run now, read Digest text/source links/character count,
Like/Dismiss/Save/Opened an item, request fake delivery and inspect bounded Profile weights. Run failures use
stable application reasons. `recovery_required` points to the admin CLI; execute controls are not rendered.

HTML text and attributes are escaped. Source links render only bounded absolute `http`/`https` URLs and use
`noopener noreferrer`. Product JSON contains only application DTO fields: no Harness identity, Evidence,
Artifact, checkpoint, Audit, provider body or traceback.

## Failure and loopback security

- Mutations require a per-process CSRF token delivered in same-origin HTML and sent in a custom header.
- JSON bodies are capped at 64 KiB, must be objects, and each route has an exact field allowlist.
- CSP, `nosniff`, `no-referrer` and `no-store` headers reduce accidental exposure.
- Unexpected exceptions become `internal_error`; allowlisted application codes get short safe messages.
- HTTP codes describe transport shape and do not replace application run status.
- Provider selection remains explicit bootstrap config. `/ready` never calls Brave, Vertex or Delivery.
- The UI cannot change provider configuration or credentials.

This is loopback convenience, not auth or a multi-user security boundary. Internet exposure, HTTPS, scheduler,
daemon, background worker, WebSocket and admin recovery UI remain out of scope.

## Product traces

HTTP tests run a real ephemeral loopback server over the all-fake bootstrap. The Fake Product E2E uses HTTP only:

```text
natural-language create -> Run -> Digest -> Like prior rank #2
-> second Run -> deterministic rank order changes -> Delivery accepted
```

No repository/helper call manufactures product state. `tools.digest_http_smoke` performs the separate opt-in
trace `HTTP -> Real Brave -> Real Vertex -> Digest -> feedback -> second Run`, with fake Delivery. Real external
services are integration confidence; Fake Search/FakeProvider/FakeDelivery are the deterministic correctness gate.

2026-08-23 manual real HTTP smoke 确认 loopback create/run 请求进入真实 Brave+Vertex 链路；外部结果未稳定
完成：Vertex 两次返回 strict candidate schema/JSON 不合格并被 `INVALID_RESPONSE` fail-closed，随后 Brave
一次投影为 `search_unavailable`。针对 literal newline 形状先增加离线 prompt regression，再收紧单行 JSON
要求，未放宽 parser。该结果如实记为 integration incomplete，不影响全 fake correctness gate，也不冒充
成功的 Product E2E。

随后 focused Brave investigation 用 safe status/content-type/bytes/SHA-256/key-presence diagnostics 比较 direct、
application 与 HTTP-derived query。三者的 env、mode、endpoint identity、headers、timeout 与 Brave
`web.results` schema 相同；真实差异仅为 query/count。application query 的 320-char snippet 恰在 normalized
separator 上截断，adapter 首次结果以空格结尾，而 workflow revalidation 再次 `.strip()`，使 normalization
不是 fixed point，failure layer 实为 `APPLICATION` revalidation。脱敏 `319*a + space + b` fixture 先复现
旧失败；最小修复是在截断后 `rstrip()`。修复后三种 query 均通过二次 validation，direct Brave、Real
Brave+Vertex application、以及 focused single-run HTTP smoke 均 completed。Search failure regression 仍证明
失败不会产生 fake Evidence/Digest，只投影 `search_unavailable` + authoritative incomplete。

同日人工浏览器验收又发现 UI 把“Brave succeeded + Evidence accepted + Vertex TIMEOUT”的最新 run 显示为
`search_unavailable`。修复不是改变 incomplete，而是 schema v6 durable failure provenance；Run DTO/HTTP
新增 `failure_stage/failure_code`，顶部显示 Status/Stage/Reason。旧真实失败 run 不回写，重启后安全显示
Unknown stage / Legacy failure；新 generation timeout 显示 Generation / Model request timed out。

最终 flat-scalar/rank-1 provider wire 收敛后，Real Brave + Vertex HTTP Product Integration Journey 连续
3/3 通过。每轮由标准库 `http.client` 驱动 ephemeral loopback server，覆盖订阅、首次生成、Digest read、
Like/Profile 与第二次生成；它不执行浏览器 JavaScript 或 DOM，因此不是 Automated Browser-Engine E2E。

随后 Manual Mobile Browser Acceptance 由用户在真实手机浏览器完成，durable lineage 为 application run
`7500de417cde44aabaa855b52be9368a`、Harness run `f0643ea853a34f339f76f7764b6f97e2`、Digest
`1dbf926baf084e8fab33fe3bd14bb611`，状态为 PASS。Automated Browser-Engine E2E 当前明确为
NOT IMPLEMENTED / NOT RUN。
