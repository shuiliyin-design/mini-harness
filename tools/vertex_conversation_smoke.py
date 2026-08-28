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


AI = "帮我关注 AI Agent 行业动态"
AI_ANSWER = "我更关心技术进展。"
FLIGHT = "帮我关注深圳往返武汉的机票优惠"
FLIGHT_ANSWER = "9 月往返，低于 800 元时提醒我。"
EXPLICIT_FLIGHT = "关注深圳到武汉 9 月往返机票，低于 800 元提醒我"
EVENT_TRIGGER = "关注 OpenAI 新模型发布，有新模型就提醒我"
SCHEMA_QUESTION = re.compile(r"(?:最多.{0,8}(?:条|项|篇)|\d+\s*字|字数|条数|schema|config)", re.I)


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


def _answer(server, token, conversation_id, message, label):
    with redirect_stdout(io.StringIO()):
        return _request(
            server, "POST", f"/conversations/{conversation_id}/messages",
            {"message": message}, token, label + "-" + uuid.uuid4().hex,
        )


def _definition(value):
    return value.get("definition") if isinstance(value, dict) else None


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
            auto_first_briefing=False,
        )
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            page = _request(server, "GET", "/create")
            match = re.search(r'name="app-csrf" content="([^"]+)"', page)
            if match is None:
                raise RuntimeError("csrf unavailable")
            token = match.group(1)
            ai = _start(server, token, AI, "ai")
            if (ai["status"] == "WAITING_FOR_ANSWER"
                    and not SCHEMA_QUESTION.search(ai.get("question") or "")):
                ai = _answer(
                    server, token, ai["conversation_id"], AI_ANSWER,
                    "ai-answer",
                )
            print("ai=" + "/".join(str(ai.get(key) or "none")
                  for key in ("status", "latest_outcome")))
            flight = _start(server, token, FLIGHT, "flight")
            flight_question = flight.get("question") or ""
            print("flight=" + "/".join((
                str(flight.get("status") or "none"),
                str(flight.get("latest_outcome") or "none"),
                "schema-question" if SCHEMA_QUESTION.search(flight_question)
                else "intent-question",
            )))
            if (flight["status"] != "WAITING_FOR_ANSWER"
                    or SCHEMA_QUESTION.search(flight_question)):
                return 1
            answered_flight = _answer(
                server, token, flight["conversation_id"], FLIGHT_ANSWER,
                "flight-answer",
            )
            explicit = _start(
                server, token, EXPLICIT_FLIGHT, "explicit-flight",
            )
            event = _start(server, token, EVENT_TRIGGER, "event")
            print("explicit=" + "/".join(str(explicit.get(key) or "none")
                  for key in ("status", "latest_outcome")))
            print("event=" + "/".join(str(event.get(key) or "none")
                  for key in ("status", "latest_outcome")))
            if explicit["status"] != "DEFINITION_ACCEPTED":
                turn = server.application.repository.list_conversation_turns(
                    explicit["conversation_id"],
                )[-1]
                attempts = server.application.repository.list_definition_attempts(
                    turn.turn_id,
                )
                print("explicit_attempts=" + ",".join(
                    f"{item.attempt_number}:{item.status}:"
                    f"{item.failure_stage or 'none'}:"
                    f"{item.failure_subtype or 'none'}:"
                    f"{(item.response_metadata or {}).get('schema_mismatch_rule', 'none')}:"
                    f"{(item.response_metadata or {}).get('schema_mismatch_field', 'none')}"
                    for item in attempts
                ))
                return 1
            with redirect_stdout(io.StringIO()):
                committed = _request(
                    server, "POST",
                    f"/conversations/{explicit['conversation_id']}/subscription",
                    {}, token,
                )

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
                ai["status"] == "DEFINITION_ACCEPTED"
                and answered_flight["status"] == "DEFINITION_ACCEPTED"
                and explicit["status"] == "DEFINITION_ACCEPTED"
                and event["status"] == "DEFINITION_ACCEPTED"
                and "AI" in _definition(ai)["topic"]
                and "机票" in _definition(answered_flight)["topic"]
                and "AI 行业动态" not in _definition(answered_flight)["topic"]
                and "800" in json.dumps(
                    _definition(explicit), ensure_ascii=False,
                )
                and _definition(explicit)["provenance"]["max_chars"]
                == "PRODUCT_DEFAULT"
                and "OpenAI" in _definition(event)["topic"]
                and committed["status"] == "ACTIVE"
                and committed["first_briefing_status"] == "PENDING"
                and relation is not None and reservation is not None
                and outbox is not None and outbox.status == "pending"
                and not digests and search_calls == digest_calls == delivery_count == 0
                and secret_ok
            )
            print("acceptance=" + (
                f"ai:{ai['latest_outcome']}/"
                f"flight:NEXT_QUESTION→{answered_flight['latest_outcome']}/"
                f"explicit:{explicit['latest_outcome']}/"
                f"event:{event['latest_outcome']}"
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
