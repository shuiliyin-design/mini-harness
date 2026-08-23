"""Loopback-only HTTP and server-rendered UI over DigestApplication."""

from dataclasses import asdict, is_dataclass
import argparse
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
from urllib.parse import parse_qs, urlsplit

from .application import ApplicationError
from .bootstrap import bootstrap_application, check_readiness


MAX_REQUEST_BYTES = 64 * 1024
JSON_TYPE = "application/json; charset=utf-8"
HTML_TYPE = "text/html; charset=utf-8"
SAFE_FAILURE_MESSAGES = {
    "configuration_error": "服务配置尚未就绪。",
    "search_unavailable": "搜索服务暂时不可用，请稍后重试。",
    "generation_incomplete": "本次摘要未能完整生成。",
    "generation_timeout": "Model request timed out",
    "generation_invalid_response": "Model response was invalid",
    "generation_rate_limited": "Model service is rate limited",
    "generation_unavailable": "Model service is unavailable",
    "generation_configuration_error": "Generation configuration is not ready",
    "generation_refusal": "Model declined this request",
    "generation_empty_output": "Model returned no usable output",
    "output_contract_failed": "Generated content failed the output contract",
    "search_timeout": "Search request timed out",
    "search_rate_limited": "Search service is rate limited",
    "search_invalid_response": "Search response was invalid",
    "search_empty_results": "Search returned no usable results",
    "search_configuration_error": "Search configuration is not ready",
    "legacy_failure": "Legacy run failure; stage is unknown",
    "delivery_unknown": "交付结果暂时无法确认，请勿重复发送。",
    "subscription_disabled": "该订阅已停用。",
    "recovery_required": "运行需要管理员通过 CLI 安全恢复。",
    "run_already_active": "该运行正在处理中。",
    "invalid_request": "请求格式无效。",
    "invalid_subscription": "订阅内容无效。",
    "invalid_feedback": "反馈内容无效。",
    "delivery_rejected": "无法交付该摘要。",
    "version_conflict": "订阅已被更新，请刷新后重试。",
    "not_found": "未找到请求的内容。",
}
CONTRACT_FAILURE_MESSAGES = {
    "too_long": "Digest exceeded its character limit",
    "too_many_items": "Digest contained too many items",
    "invalid_content_ref": "Generated digest referenced unavailable content",
    "invalid_source_ref": "Generated digest referenced an unavailable source",
    "duplicate_item": "Generated digest contained a duplicate item",
    "topic_focus_mismatch": "Generated digest did not match the subscription focus",
    "missing_required_field": "Generated digest omitted a required field",
    "invalid_marker": "Generated digest contained invalid source markers",
    "other_contract_failure": "Generated content failed the output contract",
}
GENERATION_FAILURE_MESSAGES = {
    "ITEMS_TYPE": "Model returned an invalid items shape",
    "ENVELOPE_EXTRACTION": "Model response envelope was invalid",
}


def _projection(value):
    if is_dataclass(value):
        return {key: _projection(item) for key, item in asdict(value).items()}
    if isinstance(value, tuple):
        return [_projection(item) for item in value]
    if isinstance(value, list):
        return [_projection(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _projection(item) for key, item in value.items()}
    return value


def _safe_source_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _run_status_text(run):
    if run is None:
        return "Ready"
    stage = (run.failure_stage or "none").replace("_", " ").title()
    reason = _failure_message(run)
    return (
        f"Last run · Status: {run.status.title()} · "
        f"Stage: {stage} · Reason: {reason}"
    )


def _failure_message(run):
    if run.failure_stage == "contract" and run.failure_subtype is not None:
        if run.failure_subtype == "too_long":
            diagnostics = run.failure_diagnostics or {}
            expected = diagnostics.get("expected_max_chars")
            if type(expected) is int:
                return f"Digest exceeded the {expected}-character limit"
        return CONTRACT_FAILURE_MESSAGES.get(
            run.failure_subtype,
            SAFE_FAILURE_MESSAGES["output_contract_failed"],
        )
    if run.failure_stage == "generation" and run.failure_subtype is not None:
        return GENERATION_FAILURE_MESSAGES.get(
            run.failure_subtype,
            SAFE_FAILURE_MESSAGES.get(
                run.failure_code, "Model response was invalid",
            ),
        )
    return SAFE_FAILURE_MESSAGES.get(
        run.failure_code, run.failure_code or "None",
    )


def _render_page(application, user_id, csrf_token, last_run=None):
    subscriptions = application.list_subscriptions(user_id)
    digests = application.list_digests(user_id)
    profile = application.get_profile(user_id)
    subscription_rows = []
    for item in subscriptions:
        action = "disable" if item.enabled else "enable"
        subscription_rows.append(f"""
        <article class="card">
          <h3>{escape(item.topic)}</h3>
          <p>{escape(item.natural_language_request)}</p>
          <p>状态：{'启用' if item.enabled else '停用'} · 上限 {item.max_chars} 字 · v{item.version}</p>
          <button data-action="toggle" data-id="{item.subscription_id}"
                  data-version="{item.version}" data-value="{action}">{'停用' if item.enabled else '启用'}</button>
          <button data-action="run" data-id="{item.subscription_id}" {' ' if item.enabled else 'disabled'}>Run now</button>
        </article>""")
    digest_rows = []
    for digest in digests:
        content = digest.content
        text = str(content.get("rendered_text") or "")
        links = []
        for source in content.get("source_refs", []):
            url = _safe_source_url(source.get("url") or source.get("canonical_url"))
            if url:
                label = source.get("title") or url
                links.append(
                    f'<li><a href="{escape(url, quote=True)}" rel="noopener noreferrer" '
                    f'target="_blank">{escape(str(label))}</a></li>'
                )
        items = content.get("items", [])
        feedback = "".join(
            f'<button data-action="feedback" data-digest="{digest.digest_id}" '
            f'data-item="{escape(str(item.get("item_id", "")), quote=True)}" '
            f'data-value="{kind}">{label}</button>'
            for item in items for kind, label in (
                ("liked", "Like"), ("dismissed", "Dismiss"), ("saved", "Save"),
            )
        )
        digest_rows.append(f"""
        <article class="card">
          <h3>Digest · {escape(digest.created_at)}</h3>
          <p class="digest">{escape(text)}</p>
          <p>{len(text)} 字</p><ul>{''.join(links)}</ul>
          <div>{feedback}</div>
          <button data-action="opened" data-digest="{digest.digest_id}">Opened</button>
          <button data-action="deliver" data-digest="{digest.digest_id}">Deliver</button>
        </article>""")
    weights = "".join(
        f"<li>{escape(topic)}: {weight:+d}</li>"
        for topic, weight in profile.topic_weights
    ) or "<li>尚无反馈权重</li>"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="digest-csrf" content="{escape(csrf_token, quote=True)}"><title>AI Digest</title>
<style>body{{font:16px system-ui;margin:auto;max-width:760px;padding:16px;background:#f5f6f8;color:#17202a}}h1{{margin-bottom:4px}}.card{{background:white;border-radius:12px;padding:14px;margin:12px 0;box-shadow:0 1px 4px #ccd}}textarea{{box-sizing:border-box;width:100%;min-height:92px}}button{{margin:4px;padding:9px 12px}}.digest{{white-space:pre-wrap}}#status{{position:sticky;top:0;background:#17202a;color:white;padding:8px;border-radius:8px}}</style></head>
<body><h1>AI Digest</h1><p>本机订阅式 Agent Demo</p><div id="status">{escape(_run_status_text(last_run))}</div>
<section><h2>创建订阅</h2><form id="create"><textarea name="request" maxlength="2000" required placeholder="帮我订阅 AI 行业动态，每次 600 字以内，重点关注 Agent、模型发布和开发工具。"></textarea><button>创建</button></form></section>
<section><h2>Subscriptions</h2>{''.join(subscription_rows) or '<p>尚无订阅</p>'}</section>
<section><h2>Recent Digests</h2>{''.join(digest_rows) or '<p>尚无 Digest</p>'}</section>
<section><h2>Profile</h2><p>规则 v{profile.rule_version} · Profile v{profile.version}</p><ul>{weights}</ul></section>
<script>
const token=document.querySelector('meta[name=digest-csrf]').content;
const status=document.querySelector('#status');
async function call(path, body, extra={{}}){{
 status.textContent='Working…';
 const response=await fetch(path,{{method:extra.method||'POST',headers:{{'Content-Type':'application/json','X-Digest-CSRF':token,...extra.headers}},body:body===undefined?undefined:JSON.stringify(body)}});
 const result=await response.json(); status.textContent=result.error?.message||result.status||'完成';
 if(result.application_run_id) localStorage.setItem('digest-last-run',result.application_run_id);
 if(response.ok) setTimeout(()=>{{
   location.href=result.application_run_id?`/?last_run=${{result.application_run_id}}`:'/'
 }},350); return result;
}}
document.querySelector('#create').onsubmit=e=>{{e.preventDefault();call('/subscriptions',{{request:new FormData(e.target).get('request')}})}};
document.body.onclick=e=>{{const b=e.target.closest('button[data-action]');if(!b)return;
 const id=b.dataset.id,digest=b.dataset.digest,item=b.dataset.item||null,action=b.dataset.action;
 if(action==='toggle')call(`/subscriptions/${{id}}/${{b.dataset.value}}`,{{expected_version:Number(b.dataset.version)}});
 if(action==='run')call(`/subscriptions/${{id}}/runs`,{{}},{{headers:{{'Idempotency-Key':crypto.randomUUID()}}}});
 if(action==='feedback')call(`/digests/${{digest}}/feedback`,{{type:b.dataset.value,item_id:item,event_key:crypto.randomUUID()}});
 if(action==='opened')call(`/digests/${{digest}}/feedback`,{{type:'opened',event_key:crypto.randomUUID()}});
 if(action==='deliver')call(`/digests/${{digest}}/deliver`,{{channel:'fake'}});
}};
const lastRun=localStorage.getItem('digest-last-run');
if(lastRun)fetch(`/runs/${{lastRun}}`).then(r=>r.json()).then(v=>{{
 const stages={{generation:'Generation',search:'Search',contract:'Contract',configuration:'Configuration',persistence:'Persistence',delivery:'Delivery',recovery:'Recovery',unknown_stage:'Unknown stage'}};
 const reasons={json.dumps(SAFE_FAILURE_MESSAGES, ensure_ascii=False, sort_keys=True)};
 const contractReasons={json.dumps(CONTRACT_FAILURE_MESSAGES, ensure_ascii=False, sort_keys=True)};
 const generationReasons={json.dumps(GENERATION_FAILURE_MESSAGES, ensure_ascii=False, sort_keys=True)};
 let reason=reasons[v.failure_code]||v.failure_code||'None';
 if(v.failure_stage==='contract'&&v.failure_subtype){{
   reason=contractReasons[v.failure_subtype]||reasons.output_contract_failed;
   if(v.failure_subtype==='too_long'&&Number.isInteger(v.failure_diagnostics?.expected_max_chars))
     reason=`Digest exceeded the ${{v.failure_diagnostics.expected_max_chars}}-character limit`;
 }}
 if(v.failure_stage==='generation'&&v.failure_subtype)
   reason=generationReasons[v.failure_subtype]||reason;
 status.textContent=v.error?.message||`Last run · Status: ${{v.status}} · Stage: ${{stages[v.failure_stage]||'None'}} · Reason: ${{reason}}`;
}});
</script></body></html>""".encode("utf-8")


class DigestHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address, application, config, readiness=None):
        host, _port = address
        if host != "127.0.0.1":
            raise ValueError("Digest HTTP server must bind 127.0.0.1")
        self.application = application
        self.config = config
        self.readiness = readiness or (lambda: check_readiness(config))
        self.csrf_token = secrets.token_urlsafe(24)
        super().__init__(address, DigestHTTPRequestHandler)


class DigestHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "DigestDemo/1"

    def log_message(self, _format, *_args):
        return

    def _send(self, status, payload, content_type=JSON_TYPE, headers=None):
        body = payload if isinstance(payload, bytes) else json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self):
        raw_length = self.headers.get("Content-Length")
        if raw_length is None or not raw_length.isdigit():
            raise ApplicationError("invalid_request")
        length = int(raw_length)
        if length > MAX_REQUEST_BYTES:
            raise ApplicationError("request_too_large")
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ApplicationError("invalid_request") from error
        if not isinstance(value, dict):
            raise ApplicationError("invalid_request")
        return value

    @staticmethod
    def _exact(body, allowed, required=()):
        if not set(body) <= set(allowed) or not set(required) <= set(body):
            raise ApplicationError("invalid_request")

    def _csrf(self):
        if not secrets.compare_digest(
                self.headers.get("X-Digest-CSRF", ""), self.server.csrf_token):
            raise ApplicationError("csrf_failed")

    def _error(self, error):
        code = error.code if isinstance(error, ApplicationError) else "internal_error"
        status = {
            "not_found": HTTPStatus.NOT_FOUND,
            "version_conflict": HTTPStatus.CONFLICT,
            "request_too_large": HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            "csrf_failed": HTTPStatus.FORBIDDEN,
        }.get(code, HTTPStatus.BAD_REQUEST if code != "internal_error" else HTTPStatus.INTERNAL_SERVER_ERROR)
        message = SAFE_FAILURE_MESSAGES.get(code, "请求未能安全完成。")
        self._send(status, {"error": {"code": code, "message": message}})

    @property
    def _user(self):
        return self.server.config.user_id

    def do_GET(self):
        try:
            path = urlsplit(self.path).path
            if path == "/health":
                return self._send(HTTPStatus.OK, {"status": "alive"})
            if path == "/ready":
                report = self.server.readiness()
                status = HTTPStatus.OK if report.status == "READY" else HTTPStatus.SERVICE_UNAVAILABLE
                return self._send(status, _projection(report))
            if path == "/":
                query = parse_qs(urlsplit(self.path).query)
                last_id = (query.get("last_run") or [None])[0]
                last_run = (
                    self.server.application.get_run(self._user, last_id)
                    if last_id is not None else None
                )
                page = _render_page(
                    self.server.application, self._user,
                    self.server.csrf_token, last_run,
                )
                return self._send(HTTPStatus.OK, page, HTML_TYPE)
            if path == "/subscriptions":
                value = self.server.application.list_subscriptions(self._user)
            elif path.startswith("/subscriptions/"):
                value = self.server.application.get_subscription(self._user, path.split("/")[2])
            elif path.startswith("/runs/"):
                value = self.server.application.get_run(self._user, path.split("/")[2])
            elif path == "/digests":
                query = parse_qs(urlsplit(self.path).query)
                value = self.server.application.list_digests(self._user, (query.get("subscription_id") or [None])[0])
            elif path.startswith("/digests/"):
                value = self.server.application.get_digest(self._user, path.split("/")[2])
            elif path == "/profile":
                value = self.server.application.get_profile(self._user)
            else:
                raise ApplicationError("not_found")
            self._send(HTTPStatus.OK, _projection(value))
        except Exception as error:
            self._error(error)

    def do_POST(self):
        try:
            self._csrf()
            path = urlsplit(self.path).path
            body = self._json()
            parts = path.strip("/").split("/")
            app = self.server.application
            if path == "/subscriptions":
                self._exact(body, {"request"}, {"request"})
                value = app.create_subscription(self._user, body["request"])
                status = HTTPStatus.CREATED
            elif len(parts) == 3 and parts[0] == "subscriptions" and parts[2] in {"enable", "disable"}:
                self._exact(body, {"expected_version"}, {"expected_version"})
                method = app.enable_subscription if parts[2] == "enable" else app.disable_subscription
                value = method(self._user, parts[1], body["expected_version"])
                status = HTTPStatus.OK
            elif len(parts) == 3 and parts[0] == "subscriptions" and parts[2] == "runs":
                self._exact(body, {"period_key"})
                key = self.headers.get("Idempotency-Key")
                if key is None:
                    raise ApplicationError("invalid_request")
                value = app.run_subscription(self._user, parts[1], key, body.get("period_key"))
                status = HTTPStatus.OK
            elif len(parts) == 3 and parts[0] == "digests" and parts[2] == "deliver":
                self._exact(body, {"channel"}, {"channel"})
                value = app.deliver_digest(self._user, parts[1], body["channel"])
                status = HTTPStatus.OK
            elif len(parts) == 3 and parts[0] == "digests" and parts[2] == "feedback":
                self._exact(body, {"type", "event_key", "item_id"}, {"type", "event_key"})
                value = app.record_feedback(self._user, parts[1], body["type"], body["event_key"], body.get("item_id"))
                status = HTTPStatus.OK
            else:
                raise ApplicationError("not_found")
            self._send(status, _projection(value))
        except Exception as error:
            self._error(error)

    def do_PATCH(self):
        try:
            self._csrf()
            path = urlsplit(self.path).path
            parts = path.strip("/").split("/")
            if len(parts) != 2 or parts[0] != "subscriptions":
                raise ApplicationError("not_found")
            body = self._json()
            allowed = {
                "expected_version", "topic", "natural_language_request", "cadence",
                "language", "max_chars", "max_items", "focus_topics", "delivery_preference",
            }
            self._exact(body, allowed, {"expected_version"})
            expected = body.pop("expected_version")
            value = self.server.application.update_subscription(
                self._user, parts[1], expected, **body,
            )
            self._send(HTTPStatus.OK, _projection(value))
        except Exception as error:
            self._error(error)


def create_http_server(config, host="127.0.0.1", port=8765, application=None):
    """Compose the existing app once; transports never assemble services."""
    app = application or bootstrap_application(config)
    return DigestHTTPServer((host, port), app, config)


def main(argv=None):
    from .bootstrap import DigestAppConfig

    parser = argparse.ArgumentParser(description="Loopback AI Digest Demo")
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1",))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--database", default=".digest-demo/digest.db")
    parser.add_argument("--workspace", default=".digest-demo/workspace")
    parser.add_argument("--audit", default=".digest-demo/audit")
    parser.add_argument("--search-provider", choices=("fake", "brave"), default="fake")
    parser.add_argument("--llm-provider", choices=("fake", "vertex"), default="fake")
    parser.add_argument("--delivery-provider", choices=("fake",), default="fake")
    args = parser.parse_args(argv)
    config = DigestAppConfig(
        args.database, args.workspace, args.audit, args.search_provider,
        args.llm_provider, args.delivery_provider,
    )
    server = create_http_server(config, args.host, args.port)
    print(f"AI Digest Demo: http://{args.host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
