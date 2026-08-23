# Real Vertex Provider Acceptance

## Why parser tests were not enough

Fake-transport tests proved that malformed model text fails closed, but did not prove that the configured
Vertex/Claude protocol reliably emits the Digest candidate schema. Browser acceptance exposed that gap: two
historical `completions` responses reached HTTP 200 and normal `stop`, yet failed JSON parsing. Search and Evidence
had already succeeded, so these were Generation failures rather than Search or Output Contract failures.

Those historical rows predate lexical classification. Because raw output is intentionally never persisted, their
exact JSON syntax cannot be reconstructed and remains `UNKNOWN`, not guessed. A synthetic regression mirrors only
the durable safe shape—single-line object markers, normal finish, below token cap, interior error near column 1416—
and proves that a missing delimiter is classified as `EXPECTING_COMMA`; it is not presented as the historical fact.

## Safe lexical diagnostics

`JSONDecodeError.msg` is reduced immediately to one fixed category:

- `EXPECTING_COMMA`
- `UNTERMINATED_STRING`
- `INVALID_ESCAPE`
- `EXPECTING_PROPERTY_NAME`
- `EXTRA_DATA`
- `OTHER_JSON_SYNTAX`

The generation-attempt ledger may persist that category plus line/column, content length/hash, object-marker flags,
finish reason, token count and latency. It never persists prompt, model text, provider envelope, Search content,
headers, credential or traceback. Restart tests reopen SQLite and recover the same allowlisted category.

## Requested protocol and observed compatibility

The configured endpoint is a Cloud Run LiteLLM-compatible gateway exposing both `/v1/completions` and
`/v1/chat/completions`; it is not a Google `:rawPredict` URL. The audited request settings are:

| Setting | Accepted product mode |
|---|---|
| model | `sonnet-4.6` |
| API route | `/v1/chat/completions` |
| messages | one safe user message; no assistant prefill |
| temperature | `0` |
| max output tokens | `2048` |
| timeout | `60s`, workflow deadline `125s` |
| structured mechanism | required `submit_digest_candidate` function, `strict=true` |
| function parameters | six top-level scalar strings; no nested collections |
| response extraction | exactly one matching `tool_call.function.arguments` |

Google documents native Claude structured output on its publisher `:rawPredict` API as
`output_config.format.type=json_schema`. That field is not copied into the gateway blindly. Instead, the gateway's
own OpenAPI surface was inspected. A first trivial `response_format={type: json_schema, ...}` probe succeeded, but
browser acceptance then returned parseable JSON whose `items` value was an object rather than the declared array on
both attempts. Safe diagnostics classified both as `ITEMS_TYPE`. This proved that accepting the parameter—and a
model complying with a trivial `{ok,label}` prompt—did not prove grammar enforcement on this route.

The gateway's required strict-tool path was then requested. Run
`fa31f8edf20c46a6b6c7fd74a54290ab` proved why the wording matters: both responses had
`finish_reason=tool_calls`, parseable function arguments and exact top-level keys, but `items` was an object rather
than the declared array. Both attempts were correctly rejected as `ITEMS_TYPE`.

The first compatibility fix removed nested per-item refs, but a real HTTP journey still encoded the entry collection
as an object. Renaming that field did not help. A singleton nested object was then returned as a JSON string while a
neighboring ref remained an object. These observations show that this gateway/model route does not provide stable
nested collection/object encoding for this contract.

The accepted wire contract therefore uses exactly six top-level strings: summary, candidate/content identities,
content, recommendation reason and source-ref ID. For chat mode, Harness ranking Authority projects only rank 1 into
the Model input; the Model cannot choose a non-prefix candidate. After exact scalar validation, the adapter
deterministically constructs singleton canonical `items` and `selected_source_refs` lists, then applies the unchanged
full candidate validator and Output Contract. Attempt metadata records this honestly as
`strict_flat_scalar_tool_requested_prompt_reinforced`, not native schema enforcement.

Therefore requested structured output != verified structured output. The earlier synthetic 8/8 sample and small
array probe are false confidence: they show that the model can comply with those fixtures, not that the real
gateway/model route enforces the schema. Missing/wrong tools and invalid arguments continue to fail closed while the
real response envelope is audited.

Legacy `LLM_API_MODE=completions` remains a direct adapter compatibility path using temperature 0, strict prompt,
and deterministic `{` prefill. It remains fail-closed and never scans prose for a JSON substring, but it is only
`prompt_strict_json`; application readiness now requires explicit `chat-completions` for a real Vertex product
configuration. No key-based implicit mode switch occurs.

## Authority and retry boundary

Even if a gateway eventually proves schema enforcement, that only constrains the Provider candidate shape.
Candidate membership, source availability, duplicates,
topic/focus, `max_items`, `max_chars`, Artifact acceptance and authoritative Result remain the independent
deterministic Output Contract/Harness boundary:

```text
Structured-output validity != Output Contract validity != authoritative completion
```

The existing application-owned bounded retry is unchanged: at most two attempts, no adapter sleep, same accepted
Evidence/candidate set/projections and one total deadline. Provider timeout or structured parse/schema failure can
retry once. Output Contract rejection never retries and remains authoritative `incomplete`.

## Release evidence layers

1. **Offline Deterministic Correctness Gate:** offline fake transport/provider/search tests cover strict parsing, all lexical
   categories, schema mismatch, invalid refs, duplicates, length, timeout, retry bounds, restart provenance and
   absence of raw/secret persistence.
2. **Real Vertex Provider Compatibility Gate:** five anonymized scenarios—2 candidates with focus, 5 without focus, long
   snippets, Chinese/many refs, and the browser subscription shape—run twice each with retry disabled, so ten
   results represent ten independent Provider calls.
3. **Real Brave + Vertex HTTP Product Integration Journey:** loopback HTTP uses Real Brave + Real Vertex + fake
   Delivery and must persist a contract-passing Digest readable through the public HTTP boundary. It uses
   `http.client`, not a browser engine.
4. **Manual Mobile Browser Acceptance:** a user operates the live service in a real phone browser; the observed
   success is corroborated by durable application/Harness/Digest lineage.
5. **Automated Browser-Engine E2E:** NOT IMPLEMENTED / NOT RUN. HTTP and manual-browser evidence cannot be promoted
   to browser automation evidence.

The earlier synthetic strict-tool samples are retained only as historical false-confidence lessons. With the final
flat-scalar/rank-1 contract, the repeated real compatibility gate was 10/10: transport, envelope, parse, wire schema,
canonical refs and Output Contract all passed, with no timeout or lexical/schema/envelope subtype. The Real Brave +
Real Vertex HTTP/Product journey then passed 3/3 consecutive repetitions; each repetition covered subscription,
first generation, Digest read, Like/Profile update and second generation, for six successful real generation calls.
Every temporary server was closed after its run. Offline tests remain the deterministic correctness gate; external
success remains bounded integration confidence rather than a native schema guarantee.

上一篇：[`18-loopback-http-and-web-ui.md`](18-loopback-http-and-web-ui.md)
