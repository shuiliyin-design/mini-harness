# Thin CLI, Bootstrap and Safe Readiness

## Boundary

`apps.digest_agent.cli` 是 `DigestApplication` 的薄 argparse transport。它只 import application error/DTO
语义与 bootstrap seam，不 import repository、workflow、Harness、Evidence/Artifact/Result store、Bridge 或
Environment。所有 dependency composition 位于 `bootstrap.py`，未来 HTTP transport 必须复用该 seam。

CLI commands：

```text
subscription-create/list/get/update/enable/disable
run / run-status / run-recover
outbox-run-once / outbox-drain / outbox-inspect / outbox-recover
digest-list / digest-get
deliver / feedback / profile
readiness
```

默认 human-readable；全局 `--json` 输出 application DTO。Digest public projection 保留内容、source URL、
ranking/profile explanation，但移除 Evidence correlation 与 profile projection identity。内部 Harness 教学输出
被 transport 捕获并丢弃；错误只输出 `ERROR code=<stable_application_code>`，不打印 traceback/raw provider data。

## Explicit app configuration

`DigestAppConfig` 只包含 database/workspace/audit paths、local user ID 和三个显式 mode：

```text
search_provider   = fake | brave
llm_provider      = fake | vertex
delivery_provider = fake | termux
```

默认是全 fake。即使 process environment 已有 key，也不会自动切换 real。Brave/Vertex credential 仍由 adapter
从 environment 读取，不进入 config DTO、readiness report、CLI output、SQLite 或 log。Termux 必须由 bootstrap
caller 注入 authorized dispatcher；CLI 默认没有，因此显式 termux mode 会 NOT_READY。

所有 application entrypoint统一调用 bootstrap-owned `load_application_environment`：未显式传 mapping时读取项目
`.env.local`，process environment优先且文件内后续内容不能覆盖；显式 `environ={}` 完全隔离本机配置，供 offline
tests使用。CLI、Web、Conversation smoke与async first-Briefing smoke不再各自解析或预检 `os.environ`。

## Bootstrap

```text
DigestAppConfig
  -> unified environment loading (.env.local + process override)
  -> safe readiness
  -> local directories + SQLite schema v13 migration
  -> explicit Search/LLM/Delivery adapters
  -> services + generation workflow
  -> DigestApplication
```

CLI 不自行拼 dependency。SQLite migration 是 local startup step；Search、LLM 和 notification 均在实际
application command 才可能调用。

## Readiness is not liveness

Readiness 只回答“当前配置是否具备接受工作条件”：path shape/writability、existing schema version、adapter mode、
required environment variable `SET/MISSING`、Termux dispatcher 是否 available。结果只含
`READY/NOT_READY/SET/MISSING`，不含 credential value 或 endpoint probe。

Readiness **不会**调用 Brave、Vertex 或 notification。因此 READY 不保证真实 service、network、quota 或 auth
在下一次 Run 仍可用；实际失败继续由 adapter taxonomy、application failure projection 与 Harness truth 处理。

## Tests and smokes

Offline tests 覆盖 all-fake READY/no external calls、Brave/Vertex missing、Termux unavailable、invalid paths、
schema migration、secret non-disclosure、explicit selection/no implicit switch、stable CLI failure 与完整 fake CLI
journey。Architecture test 固定 CLI/bootstrap 不 import Harness internals。

2026-08-23 manual CLI integration smoke 显式使用 `brave + vertex + fake delivery`：readiness READY，create
Subscription 后 Run completed，Digest 可经 CLI 读取。真实服务结果仅提供 integration confidence；全 fake CLI
journey 才是 deterministic correctness gate。

2026-08-24 regression以临时 `.env.local` 证明五项 real配置被同一 loader投影为 SET、process override优先、
readiness不 probe外部服务，并断言 CLI/Web/Conversation/Outbox smoke只依赖 formal bootstrap contract。

上一页：[`15-application-facade-and-run-lifecycle.md`](15-application-facade-and-run-lifecycle.md) · 返回：[`README.md`](README.md)
