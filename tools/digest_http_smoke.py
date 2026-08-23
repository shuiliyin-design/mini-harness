"""Opt-in Real Brave + Vertex product smoke over the loopback HTTP boundary."""

import argparse
from contextlib import redirect_stdout
import http.client
import io
import json
import re
import tempfile
import threading

from apps.digest_agent.bootstrap import DigestAppConfig, check_readiness
from apps.digest_agent.web import create_http_server


def _safe_failure_diagnostics(server):
    workflow = server.application.generation
    search = workflow.search_client
    provider = workflow.provider
    print("search_diagnostics=" + json.dumps(
        getattr(search, "last_diagnostics", None),
        ensure_ascii=False, sort_keys=True,
    ))
    provider_error = getattr(provider, "last_error", None)
    print("provider_error=" + json.dumps(
        provider_error, ensure_ascii=False, sort_keys=True,
    ))


def _safe_run_failure(run):
    return {
        key: run.get(key) for key in (
            "status", "failure_reason", "failure_stage", "failure_code",
            "failure_subtype", "failure_diagnostics",
        ) if run.get(key) is not None
    }


def _request(server, method, path, body=None, token=None, key=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=90)
    headers = {}
    encoded = None
    if body is not None:
        encoded = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Digest-CSRF"] = token
    if key:
        headers["Idempotency-Key"] = key
    connection.request(method, path, encoded, headers)
    response = connection.getresponse()
    raw = response.read()
    content_type = response.getheader("Content-Type", "")
    connection.close()
    value = json.loads(raw) if content_type.startswith("application/json") else raw.decode("utf-8")
    if response.status >= 400:
        code = value.get("error", {}).get("code", "http_error") if isinstance(value, dict) else "http_error"
        raise RuntimeError(f"HTTP smoke failed: {response.status}/{code}")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--single-run", action="store_true")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="digest-http-smoke-") as root:
        config = DigestAppConfig(
            root + "/digest.db", root + "/workspace", root + "/audit",
            "brave", "vertex", "fake",
        )
        report = check_readiness(config)
        if report.status != "READY":
            print("REAL HTTP SMOKE: NOT_READY")
            return 2
        server = create_http_server(config, port=0)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            page = _request(server, "GET", "/")
            match = re.search(r'name="digest-csrf" content="([^"]+)"', page)
            if match is None:
                raise RuntimeError("HTTP smoke failed: csrf token unavailable")
            token = match.group(1)
            subscription = _request(server, "POST", "/subscriptions", {
                "request": "帮我订阅 AI 行业动态，每次 600 字以内，最多 2 条，重点关注 Agent、模型发布和开发工具。",
            }, token)
            with redirect_stdout(io.StringIO()):
                run1 = _request(
                    server, "POST",
                    f"/subscriptions/{subscription['subscription_id']}/runs",
                    {}, token, "real-http-first",
                )
            if run1["status"] != "completed":
                print("REAL HTTP SMOKE: INCOMPLETE", run1["failure_reason"])
                _safe_failure_diagnostics(server)
                return 3
            digest1 = _request(server, "GET", f"/digests/{run1['digest_id']}")
            items = digest1["content"].get("items", [])
            if not items:
                raise RuntimeError("HTTP smoke failed: empty digest")
            if args.single_run:
                print("REAL HTTP SMOKE: PASS")
                print("providers: brave/vertex/fake")
                print("first_run:", run1["status"], "items:", len(items))
                return 0
            _request(server, "POST", f"/digests/{run1['digest_id']}/feedback", {
                "type": "liked", "event_key": "real-http-like",
                "item_id": items[0]["item_id"],
            }, token)
            with redirect_stdout(io.StringIO()):
                run2 = _request(
                    server, "POST",
                    f"/subscriptions/{subscription['subscription_id']}/runs",
                    {}, token, "real-http-second",
                )
            print("providers: brave/vertex/fake")
            print("first_run:", run1["status"], "items:", len(items))
            print("second_run:", run2["status"])
            if run2["status"] != "completed":
                print("REAL HTTP SMOKE: INCOMPLETE generation_incomplete")
                print("run_failure=" + json.dumps(
                    _safe_run_failure(run2), ensure_ascii=False,
                    sort_keys=True,
                ))
                _safe_failure_diagnostics(server)
            else:
                print("REAL HTTP SMOKE: PASS")
            print("profile_version:", _request(server, "GET", "/profile")["version"])
            return 0 if run2["status"] == "completed" else 3
        finally:
            server.shutdown()
            server.server_close()
            worker.join(5)


if __name__ == "__main__":
    raise SystemExit(main())
