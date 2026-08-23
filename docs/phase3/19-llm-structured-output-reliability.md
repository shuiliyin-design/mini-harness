# LLM Structured-Output Reliability

## Finding

2026-08-23 的人工 browser acceptance 中，Search、candidate-set Evidence 与 deterministic ranking
已经成功，Vertex `sonnet-4.6` 返回 HTTP/provider success 后，模型正文却不能通过 strict JSON parser。
此前只保存 `INVALID_RESPONSE`，因此历史 row 无法事后还原 response length/hash/finish reason；运行时的
safe parser diagnostic 将真实 subtype 定位为 `JSON_PARSE`。这不是 Search、HTTP bootstrap 或 Output
Contract failure。

安全重建 request metadata 后，两条路径的主要差异是：

| Metadata | Successful scripted smoke | Failed browser run |
|---|---:|---:|
| ranked candidate count | 2 | 5 |
| prompt characters | 2791 | 5523 |
| `max_chars` / `max_items` | 600 / 2 | 600 / 5 |
| focus topic count | 3 | 0 |
| model / mode | `sonnet-4.6` / `completions` | same |
| timeout / max output tokens | 60s / 2048 | same |
| candidate schema identity | `e98795…854e6` | same |

Prompt/request SHA-256 只用于 identity，不保存 prompt。旧 response metadata 不存在，不能用猜测补写。
差异说明较大的真实 request 更容易暴露 JSON 可靠性问题，但不是因果证明。

## Structured-output boundary

当前已验证 endpoint 是 OpenAI-compatible `completions` gateway。其 request 使用 temperature 0、
assistant `{` prefill 与 strict single-line JSON instruction；它没有发送 Vertex `rawPredict`
`output_config.format.type=json_schema`。Chat-completions mode 只使用 `response_format=json_object`，也不是
完整 JSON Schema enforcement。

Google 的 [Claude-on-Vertex structured-output 文档](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude/structured-outputs?hl=en)
把 native schema 放在 `rawPredict` request 的 `output_config.format`；
[Gemini controlled output](https://cloud.google.com/vertex-ai/generative-ai/docs/reference/rest/v1beta1/GenerationConfig)
则使用 `responseMimeType/responseSchema`。两者都不能
未经 endpoint/protocol 验证直接塞进当前 completions gateway。因此本 slice 如实记录 mechanism 为
`prompt_strict_json`，不冒充 native schema mode，也不切模型或 endpoint。

Provider parser 只接受一个完整 JSON object。前后 whitespace 合法；completions prefill 可确定性补回
唯一缺少的开头 `{`；Markdown fence、leading/trailing prose、任意 substring extraction 与 truncated JSON
均拒绝。不会从 prose 中猜答案，也不会在本地修补模型内容。

## Safe generation attempt ledger

schema v7 的 `generation_attempts` 在每次 external call 前保存 fresh attempt identity，并只持久化 allowlisted
metadata：

- request: provider/model/mode、prompt/request length/hash、candidate count、schema identity、mechanism、
  timeout、max output tokens；
- response: HTTP status、body/content length/hash、finish reason、safe token count、parse/schema booleans、
  parse line/column、duration、failure subtype；
- lifecycle: application run ID、attempt number、started/completed timestamp、status。

不保存 prompt、model output、provider envelope、search content、headers、credential 或 traceback。Subtype
也有固定 allowlist，不能把 provider detail 当 code 持久化。该 ledger 是 application diagnostics，不是
Harness Audit，也不进入 public DTO/HTTP/UI。

## Failure taxonomy and retry

Generation safe subtype 分层为 `TRANSPORT`、`MODEL_TIMEOUT`、`EMPTY_RESPONSE`、`NON_JSON`、
`JSON_PARSE`、`SCHEMA_MISMATCH`、`MODEL_REFUSAL` 与 `OTHER_SAFE_CODE`。Invalid content/source ref、duplicate
item、output too long 仍是 deterministic Output Contract rejection；parser success 不代表 Digest success。

schema v8 现在为这些 contract rejections 保存独立 safe subtype/limits/counts；它不改变 schema v7 provider
attempt ledger，也不把 contract rejection送回 provider retry：

```text
Structured-output validity != Output Contract validity != authoritative completion
```

应用最多执行两个 generation attempts，不 sleep；总 deadline 125 秒，每个 provider call timeout 60 秒。
只对无外部 side effect 的 `TIMEOUT`，以及 `NON_JSON/JSON_PARSE/SCHEMA_MISMATCH` 做一次 fresh regeneration。
第二次使用完全相同的 accepted Evidence、ranked candidates、Subscription/Profile projection 与 Output
Contract。耗尽后仍 authoritative `incomplete`，并投影精确 generation code。Auth/refusal/empty output、
contract rejection不自动重试。

## Correctness and confidence

Fake transport fixtures 是 correctness gate：strict JSON、Unicode/whitespace、fence/prose、truncation、schema
shape、invalid refs、duplicate/too-long、timeout、retry exhaustion、restart persistence 与 raw-output absence。
真实 Vertex 的少量连续 runs 只报告 success/invalid/timeout/latency safe summary；不为了 100% 成功继续
重试，也不改变 deterministic contract。

Real external LLM = integration confidence. Deterministic FakeProvider/fake transport = correctness gate.

2026-08-23 的固定三次 Fake Search + Real Vertex reliability smoke 得到：2/3 logical runs completed，
0 invalid-response attempts，0 timeout attempts；attempt latency 为 13.965s、14.102s、44.993s
（min/median/max = 13.965/14.102/44.993s）。第 3 次 provider parse/schema success 后仍被 deterministic
Output Contract 以 `output_contract_failed` 拒绝；没有追加第 4 次 run。该结果同时证明 60 秒 call timeout
高于本次成功 latency，但单次观测不足以把它当 SLA 或永久调高/调低 timeout 的依据。

schema v8 diagnostics 完成后的第二组固定三次 smoke 得到 1/3 completed；两个 5-candidate run 均是
provider parse/schema success 后 `too_long`，safe diagnostics 都是 actual 630 / expected 600。三次各只有
一个 provider attempt，invalid-response=0、timeout=0，latency min/median/max 为
12.742/44.630/48.961 秒。结果在三次后停止，证明 contract rejection 不触发 structured provider retry。

上一篇：[`18-loopback-http-and-web-ui.md`](18-loopback-http-and-web-ui.md)
