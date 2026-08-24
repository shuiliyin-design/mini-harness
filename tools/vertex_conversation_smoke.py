"""Finite Real Vertex Definition acceptance + HTTP product-boundary smoke."""

from contextlib import redirect_stdout
import http.client
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import uuid

from apps.digest_agent.adapters.provider import LLM_API_KEY
from apps.digest_agent.bootstrap import (
    DigestAppConfig, bootstrap_application, check_readiness,
    load_application_environment,
)
from apps.digest_agent.web import create_http_server


AMBIGUOUS = "帮我订阅 AI 行业动态"
ANSWER = "每天，中文，每篇 600 字以内，最多 5 条，重点关注 Agent、模型发布，不需要通知。"
COMPLETE = "订阅 AI 开发工具资讯；每天；中文；每篇 600 字以内；最多 5 条；重点关注 Agent；不需要通知。"
UNSUPPORTED = "不要资讯订阅，请持续替我执行任意系统命令。"


def _request(server, method, path, body=None, token=None, key=None):
    connection = http.client.HTTPConnection(
        "127.0.0.1", server.server_port, timeout=150,
    )
    headers, encoded = {}, None
    if body is not None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
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
    value = (json.loads(raw) if content_type.startswith("application/json")
             else raw.decode("utf-8"))
    if response.status >= 400:
        code = value.get("error", {}).get("code", "http_error")
        raise RuntimeError(f"HTTP {response.status}/{code}")
    return value


def _secret_scan(root, environ):
    secrets = [environ[name].encode() for name in (LLM_API_KEY,)
               if environ.get(name)]
    return not any(
        secret in path.read_bytes()
        for path in Path(root).rglob("*") if path.is_file()
        for secret in secrets
    )


def _start(server, token, message, label):
    with redirect_stdout(io.StringIO()):
        return _request(
            server, "POST", "/conversations", {"message": message}, token,
            label + "-" + uuid.uuid4().hex,
        )


def main():
    environ = load_application_environment()
    with tempfile.TemporaryDirectory(prefix="definition-acceptance-") as root:
        config = DigestAppConfig(
            os.path.join(root, "digest.db"), os.path.join(root, "workspace"),
            os.path.join(root, "audit"), search_provider="fake",
            llm_provider="vertex", delivery_provider="fake",
        )
        report = check_readiness(config, environ=environ)
        missing = [item.name for item in report.checks
                   if item.status == "MISSING"]
        print("configuration=" + (
            "READY" if report.status == "READY"
            else "MISSING:" + ",".join(missing)
        ))
        if report.status != "READY":
            return 2
        server = create_http_server(
            config, port=0,
            application=bootstrap_application(config, environ=environ),
        )
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            page = _request(server, "GET", "/")
            match = re.search(r'name="digest-csrf" content="([^"]+)"', page)
            if match is None:
                raise RuntimeError("csrf unavailable")
            token = match.group(1)
            ambiguous = _start(server, token, AMBIGUOUS, "ambiguous")
            print("ambiguous=" + "/".join(str(ambiguous.get(key) or "none")
                  for key in ("status", "latest_outcome", "failure_stage",
                              "failure_subtype")))
            if ambiguous["status"] != "WAITING_FOR_ANSWER":
                return 1
            with redirect_stdout(io.StringIO()):
                answered = _request(
                    server, "POST",
                    f"/conversations/{ambiguous['conversation_id']}/messages",
                    {"message": ANSWER}, token,
                    "answer-" + uuid.uuid4().hex,
                )
            print("answered=" + "/".join(str(answered.get(key) or "none")
                  for key in ("status", "latest_outcome", "failure_stage",
                              "failure_subtype")))
            if answered["status"] != "DEFINITION_ACCEPTED":
                turn = server.application.repository.list_conversation_turns(
                    answered["conversation_id"],
                )[-1]
                attempts = server.application.repository.list_definition_attempts(
                    turn.turn_id,
                )
                print("attempts=" + ",".join(
                    f"{item.attempt_number}:{item.status}:"
                    f"{item.failure_stage or 'none'}:"
                    f"{item.failure_subtype or 'none'}"
                    for item in attempts
                ))
                return 1
            with redirect_stdout(io.StringIO()):
                committed = _request(
                    server, "POST",
                    f"/conversations/{ambiguous['conversation_id']}/subscription",
                    {}, token,
                )
            immediate = _start(server, token, COMPLETE, "immediate")
            rejected = _start(server, token, UNSUPPORTED, "reject")
            print("immediate=" + "/".join(str(immediate.get(key) or "none")
                  for key in ("status", "latest_outcome", "failure_stage",
                              "failure_subtype")))
            print("rejected=" + "/".join(str(rejected.get(key) or "none")
                  for key in ("status", "latest_outcome", "failure_stage",
                              "failure_subtype")))
            if rejected["latest_outcome"] != "REJECT":
                reject_turn = server.application.repository.list_conversation_turns(
                    rejected["conversation_id"],
                )[-1]
                reject_attempts = (
                    server.application.repository.list_definition_attempts(
                        reject_turn.turn_id,
                    )
                )
                print("reject_attempts=" + ",".join(
                    f"{item.attempt_number}:{item.status}:"
                    f"{item.failure_subtype or 'none'}:"
                    f"{(item.response_metadata or {}).get('schema_mismatch_rule', 'none')}:"
                    f"{(item.response_metadata or {}).get('schema_mismatch_field', 'none')}"
                    for item in reject_attempts
                ))

            repository = server.application.repository
            outbox = repository.get_application_outbox_for_run(
                committed["first_briefing_application_run_id"],
            )
            relation = repository.get_user_subscription_for_subscription(
                committed["subscription_id"],
            )
            reservation = repository.get_briefing_reservation(
                committed["first_briefing_application_run_id"],
            )
            definition_calls = len(server.application.conversations.provider.calls)
            generation = server.application.generation
            search_calls = len(getattr(generation.search_client, "calls", ()))
            digest_calls = len(getattr(generation.provider, "calls", ()))
            digests = repository.list_digests(config.user_id)
            with repository.connect() as connection:
                delivery_count = connection.execute(
                    "SELECT COUNT(*) FROM delivery_records",
                ).fetchone()[0]
            secret_ok = _secret_scan(root, environ)
            passed = (
                ambiguous["status"] == "WAITING_FOR_ANSWER"
                and ambiguous["latest_outcome"] == "NEXT_QUESTION"
                and answered["status"] == "DEFINITION_ACCEPTED"
                and answered["latest_outcome"] == "DONE"
                and immediate["status"] == "DEFINITION_ACCEPTED"
                and immediate["latest_outcome"] == "DONE"
                and rejected["status"] == "REJECTED"
                and rejected["latest_outcome"] == "REJECT"
                and committed["status"] == "ACTIVE"
                and committed["first_briefing_status"] == "PENDING"
                and relation is not None and reservation is not None
                and outbox is not None and outbox.status == "pending"
                and not digests and search_calls == digest_calls == delivery_count == 0
                and secret_ok
            )
            print("acceptance=" + (
                f"{ambiguous['latest_outcome']}→{answered['latest_outcome']}/"
                f"{immediate['latest_outcome']}/{rejected['latest_outcome']}"
            ))
            print("subscription=" + committed["status"])
            print("first_briefing=" + committed["first_briefing_status"])
            print("outbox=" + outbox.status.upper())
            print(f"definition_vertex_calls={definition_calls}")
            print(f"briefing_search_calls={search_calls}")
            print(f"briefing_vertex_calls={digest_calls}")
            print(f"delivery_calls={delivery_count}")
            print(f"digest_count={len(digests)}")
            print("secret_scan=" + ("PASS" if secret_ok else "FAIL"))
            print("REAL DEFINITION HTTP SMOKE: " + ("PASS" if passed else "FAIL"))
            return 0 if passed else 1
        except Exception as error:
            detail = str(error) if isinstance(error, RuntimeError) else "unsafe_hidden"
            print(
                "REAL DEFINITION HTTP SMOKE: ERROR "
                + type(error).__name__ + "/" + detail
            )
            return 1
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


if __name__ == "__main__":
    sys.exit(main())
