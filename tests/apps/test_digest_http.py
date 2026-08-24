from dataclasses import dataclass
import http.client
import json
import os
import tempfile
import threading
import unittest

from apps.digest_agent.application import ApplicationError, RunView
from apps.digest_agent.adapters.provider import (
    FakeDigestProvider, ProviderAdapterError,
)
from apps.digest_agent.adapters.definition import FakeDefinitionAgentAdapter
from apps.digest_agent.bootstrap import DigestAppConfig
from apps.digest_agent.adapters.search import FakeSearchClient, SearchAdapterError
from apps.digest_agent.web import DigestHTTPServer, create_http_server


USER = "a" * 32


class HTTPClient:
    def __init__(self, server):
        self.server = server

    def request(self, method, path, body=None, headers=None, csrf=True):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=10,
        )
        values = dict(headers or {})
        if csrf and method in {"POST", "PATCH"}:
            values["X-Digest-CSRF"] = self.server.csrf_token
        encoded = None
        if body is not None:
            encoded = json.dumps(body).encode("utf-8")
            values["Content-Type"] = "application/json"
        connection.request(method, path, body=encoded, headers=values)
        response = connection.getresponse()
        payload = response.read()
        content_type = response.getheader("Content-Type")
        connection.close()
        if content_type.startswith("application/json"):
            payload = json.loads(payload)
        return response.status, payload, content_type


class ServerFixture:
    def __init__(self, case):
        self.case = case
        self.temp = tempfile.TemporaryDirectory()
        root = self.temp.name
        self.config = DigestAppConfig(
            os.path.join(root, "digest.db"), os.path.join(root, "workspace"),
            os.path.join(root, "audit"), user_id=USER,
        )
        self.server = create_http_server(self.config, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.client = HTTPClient(self.server)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.temp.cleanup()


class SchemaInvalidToolProvider:
    provider_identity = "vertex"

    def __init__(self):
        self.calls = []
        self.last_attempt = None

    def synthesize(self, *_arguments):
        self.calls.append(True)
        self.last_attempt = {
            "http_status": 200, "finish_reason": "tool_calls",
            "json_parse_succeeded": True,
            "schema_validation_succeeded": False,
            "schema_mismatch_rule": "ITEMS_TYPE",
            "schema_mismatch_field": "items",
            "payload_source": "tool_arguments",
            "payload_top_type": "object", "payload_items_type": "object",
        }
        raise ProviderAdapterError(
            "INVALID_RESPONSE", subtype="SCHEMA_MISMATCH",
        )


class DigestHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ServerFixture(self)
        self.client = self.fixture.client

    def tearDown(self):
        self.fixture.close()

    def create(self, request="帮我订阅 AI 行业动态，每次 600 字以内，每天一份"):
        status, value, _ = self.client.request(
            "POST", "/subscriptions", {"request": request},
        )
        self.assertEqual(status, 201)
        return value

    def run_digest(self, subscription_id, key):
        return self.client.request(
            "POST", f"/subscriptions/{subscription_id}/runs", {},
            headers={"Idempotency-Key": key},
        )

    def start_conversation(self, message, key="conversation-start"):
        return self.client.request(
            "POST", "/conversations", {"message": message},
            headers={"Idempotency-Key": key},
        )

    def test_conversation_http_is_multi_turn_durable_and_creates_no_subscription(self):
        self.fixture.server.application.conversations.provider = (
            FakeDefinitionAgentAdapter([{
                "protocol_version": 1, "type": "NEXT_QUESTION",
                "question": "每篇希望控制在多少字以内？",
            }, {
                "protocol_version": 1, "type": "NEXT_QUESTION",
                "question": "需要本地通知吗？",
            }, {
                "protocol_version": 1, "type": "DONE",
                "definition": {
                    "topic": "AI 行业动态", "language": "zh-CN",
                    "cadence": "daily", "max_chars": 600,
                    "max_items": 5, "focus_topics": ["Agent"],
                    "delivery_preference": "none",
                },
            }]))
        status, first, _ = self.start_conversation("帮我订阅 AI 行业动态")
        self.assertEqual(
            (status, first["status"], first["turn_count"]),
            (201, "WAITING_FOR_ANSWER", 1),
        )
        path = f"/conversations/{first['conversation_id']}/messages"
        status, second, _ = self.client.request(
            "POST", path, {"message": "600 字以内"},
            headers={"Idempotency-Key": "conversation-answer-1"},
        )
        self.assertEqual(
            (status, second["status"], second["turn_count"]),
            (200, "WAITING_FOR_ANSWER", 2),
        )
        status, terminal, _ = self.client.request(
            "POST", path, {"message": "暂时不用"},
            headers={"Idempotency-Key": "conversation-answer-2"},
        )
        self.assertEqual((status, terminal["status"]),
                         (200, "DEFINITION_ACCEPTED"))
        status, restored, _ = self.client.request(
            "GET", f"/conversations/{first['conversation_id']}",
        )
        self.assertEqual((status, restored["definition"]["max_chars"]),
                         (200, 600))
        status, subscriptions, _ = self.client.request("GET", "/subscriptions")
        self.assertEqual((status, subscriptions), (200, []))
        rendered = json.dumps(terminal).casefold()
        for forbidden in (
            "harness_run_id", "provider", "evidence", "artifact",
            "checkpoint", "raw_response",
        ):
            self.assertNotIn(forbidden, rendered)

        commit_path = (
            f"/conversations/{first['conversation_id']}/subscription"
        )
        status, committed, _ = self.client.request(
            "POST", commit_path, {},
        )
        self.assertEqual(
            (status, committed["status"],
             committed["first_briefing_status"], committed["message"]),
            (201, "ACTIVE", "PENDING",
             "订阅成功，正在准备首篇资讯。"),
        )
        self.assertNotIn("outbox_id", committed)
        self.assertNotIn("harness_run_id", committed)
        status, replay, _ = self.client.request(
            "POST", commit_path, {},
        )
        self.assertEqual((status, replay["reused"]), (200, True))
        self.assertEqual(replay["subscription_id"], committed["subscription_id"])
        status, subscriptions, _ = self.client.request("GET", "/subscriptions")
        self.assertEqual((status, len(subscriptions)), (200, 1))
        self.assertEqual(
            (subscriptions[0]["product_kind"],
             subscriptions[0]["product_status"],
             subscriptions[0]["definition_id"]),
            ("product", "ACTIVE", committed["definition_id"]),
        )
        self.assertEqual(
            self.fixture.server.application.repository.list_digests(USER), (),
        )
        generation = self.fixture.server.application.generation
        self.assertEqual((generation.search_client.calls,
                          generation.provider.calls), ([], []))
        briefing_path = (
            f"/subscriptions/{committed['subscription_id']}/briefings/latest"
        )
        status, pending, _ = self.client.request("GET", briefing_path)
        self.assertEqual(
            (status, pending["subscription_status"], pending["status"],
             pending["digest_id"]),
            (200, "ACTIVE", "PENDING", None),
        )
        worked = self.fixture.server.application.run_outbox_once()
        self.assertEqual(
            (worked.outbox_status, worked.first_briefing_status),
            ("SUCCEEDED", "READY"),
        )
        status, ready, _ = self.client.request("GET", briefing_path)
        self.assertEqual(
            (status, ready["subscription_status"], ready["status"]),
            (200, "ACTIVE", "READY"),
        )
        self.assertEqual((len(generation.search_client.calls),
                          len(generation.provider.calls)), (1, 1))
        status, page, _ = self.client.request("GET", "/")
        rendered_page = page.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("Subscription: Successful", rendered_page)
        self.assertIn("First briefing: READY", rendered_page)

    def test_conversation_http_idempotency_and_exact_fields(self):
        status, first, _ = self.start_conversation(
            "帮我订阅 AI 行业动态", "same-conversation",
        )
        status2, duplicate, _ = self.start_conversation(
            "帮我订阅 AI 行业动态", "same-conversation",
        )
        self.assertEqual((status, status2), (201, 200))
        self.assertEqual(first["conversation_id"], duplicate["conversation_id"])
        status, error, _ = self.client.request(
            "POST", "/conversations", {"message": "订阅 AI"},
        )
        self.assertEqual((status, error["error"]["code"]),
                         (400, "invalid_request"))
        status, error, _ = self.client.request(
            "POST", f"/conversations/{first['conversation_id']}/subscription",
            {"definition": {}},
        )
        self.assertEqual((status, error["error"]["code"]),
                         (400, "invalid_request"))
        status, error, _ = self.client.request(
            "POST", f"/conversations/{first['conversation_id']}/subscription",
            {},
        )
        self.assertEqual((status, error["error"]["code"]),
                         (409, "definition_not_accepted"))
        status, error, _ = self.client.request(
            "POST", "/conversations", {"message": "订阅 AI", "extra": 1},
            headers={"Idempotency-Key": "bad-shape"},
        )
        self.assertEqual((status, error["error"]["code"]),
                         (400, "invalid_request"))

    def test_web_ui_uses_server_conversation_state_without_one_turn_limit(self):
        status, page, _ = self.client.request("GET", "/")
        self.assertEqual(status, 200)
        text = page.decode("utf-8")
        self.assertIn("/conversations", text)
        self.assertIn("/subscription", text)
        self.assertIn("订阅成功，正在准备首篇资讯。", text)
        self.assertIn("WAITING_FOR_ANSWER", text)
        self.assertNotIn("asked_once", text)

    def test_health_and_readiness_are_passive_safe_projections(self):
        status, health, _ = self.client.request("GET", "/health")
        self.assertEqual((status, health), (200, {"status": "alive"}))
        status, ready, _ = self.client.request("GET", "/ready")
        self.assertEqual((status, ready["status"]), (200, "READY"))
        rendered = json.dumps(ready)
        self.assertNotIn("credential", rendered.casefold())
        self.assertNotIn("api_key\": \"", rendered.casefold())

    def test_subscription_create_list_patch_disable_enable(self):
        created = self.create()
        self.assertEqual(
            (created["product_kind"], created["product_status"],
             created["definition_id"], created["user_subscription_id"]),
            ("legacy", None, None, None),
        )
        status, listed, _ = self.client.request("GET", "/subscriptions")
        self.assertEqual((status, len(listed)), (200, 1))
        status, updated, _ = self.client.request(
            "PATCH", f"/subscriptions/{created['subscription_id']}",
            {"expected_version": 1, "max_chars": 700, "focus_topics": ["Agent"]},
        )
        self.assertEqual((status, updated["version"], updated["max_chars"]), (200, 2, 700))
        status, disabled, _ = self.client.request(
            "POST", f"/subscriptions/{created['subscription_id']}/disable",
            {"expected_version": 2},
        )
        self.assertFalse(disabled["enabled"])
        status, rejected, _ = self.run_digest(created["subscription_id"], "disabled")
        self.assertEqual((status, rejected["failure_reason"]), (200, "subscription_disabled"))
        status, enabled, _ = self.client.request(
            "POST", f"/subscriptions/{created['subscription_id']}/enable",
            {"expected_version": 3},
        )
        self.assertTrue(enabled["enabled"])

    def test_run_idempotency_status_and_digest_queries(self):
        created = self.create()
        status, first, _ = self.run_digest(created["subscription_id"], "double-click")
        self.assertEqual((status, first["status"]), (200, "completed"))
        status, second, _ = self.run_digest(created["subscription_id"], "double-click")
        self.assertEqual(first["application_run_id"], second["application_run_id"])
        self.assertTrue(second["reused"])
        status, run, _ = self.client.request("GET", f"/runs/{first['application_run_id']}")
        self.assertEqual((status, run["digest_id"]), (200, first["digest_id"]))
        status, digests, _ = self.client.request("GET", "/digests")
        self.assertEqual((status, len(digests)), (200, 1))
        status, digest, _ = self.client.request("GET", f"/digests/{first['digest_id']}")
        self.assertEqual((status, digest["digest_id"]), (200, first["digest_id"]))

    def test_feedback_profile_and_delivery_are_idempotent(self):
        created = self.create()
        _, run, _ = self.run_digest(created["subscription_id"], "journey")
        _, digest, _ = self.client.request("GET", f"/digests/{run['digest_id']}")
        item_id = digest["content"]["items"][0]["item_id"]
        body = {"type": "liked", "event_key": "tap-1", "item_id": item_id}
        status, first, _ = self.client.request("POST", f"/digests/{run['digest_id']}/feedback", body)
        status2, duplicate, _ = self.client.request("POST", f"/digests/{run['digest_id']}/feedback", body)
        self.assertEqual((status, status2, first["applied"], duplicate["applied"]), (200, 200, True, False))
        status, profile, _ = self.client.request("GET", "/profile")
        self.assertEqual((status, profile["version"]), (200, 1))
        delivery = {"channel": "fake"}
        _, one, _ = self.client.request("POST", f"/digests/{run['digest_id']}/deliver", delivery)
        _, two, _ = self.client.request("POST", f"/digests/{run['digest_id']}/deliver", delivery)
        self.assertEqual(one["delivery_id"], two["delivery_id"])
        self.assertEqual(one["attempt_number"], 1)

    def test_product_e2e_changes_ranking_after_like(self):
        created = self.create(
            "帮我订阅 AI 行业动态，每次 600 字以内，重点关注 Agent、模型发布。",
        )
        _, run1, _ = self.run_digest(created["subscription_id"], "first-run")
        _, digest1, _ = self.client.request("GET", f"/digests/{run1['digest_id']}")
        order1 = [item["item_id"] for item in digest1["content"]["items"]]
        target = digest1["content"]["items"][1]
        self.client.request("POST", f"/digests/{run1['digest_id']}/feedback", {
            "type": "liked", "event_key": "like-model", "item_id": target["item_id"],
        })
        _, run2, _ = self.run_digest(created["subscription_id"], "second-run")
        _, digest2, _ = self.client.request("GET", f"/digests/{run2['digest_id']}")
        order2 = [item["item_id"] for item in digest2["content"]["items"]]
        self.assertNotEqual(order1, order2)
        _, delivery, _ = self.client.request("POST", f"/digests/{run2['digest_id']}/deliver", {"channel": "fake"})
        self.assertEqual(delivery["status"], "accepted")

    def test_request_validation_csrf_and_body_limit_fail_closed(self):
        status, value, _ = self.client.request("POST", "/subscriptions", {"request": "ok", "extra": 1})
        self.assertEqual((status, value["error"]["code"]), (400, "invalid_request"))
        status, value, _ = self.client.request("POST", "/subscriptions", {"request": "ok"}, csrf=False)
        self.assertEqual((status, value["error"]["code"]), (403, "csrf_failed"))
        connection = http.client.HTTPConnection("127.0.0.1", self.fixture.server.server_port)
        connection.request("POST", "/subscriptions", body=b"{}", headers={
            "Content-Length": str(64 * 1024 + 1),
            "X-Digest-CSRF": self.fixture.server.csrf_token,
        })
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        self.assertEqual((response.status, payload["error"]["code"]), (413, "request_too_large"))

    def test_html_escaping_and_security_headers(self):
        self.create("帮我订阅 <script>alert(1)</script> AI 行业动态，每次 600 字以内")
        status, page, content_type = self.client.request("GET", "/")
        self.assertEqual((status, content_type), (200, "text/html; charset=utf-8"))
        text = page.decode("utf-8")
        self.assertNotIn("<script>alert(1)</script>", text)
        self.assertIn("&lt;script&gt;", text)

    def test_web_page_renders_digest_text_and_character_count(self):
        created = self.create()
        _, run, _ = self.run_digest(created["subscription_id"], "web-content")
        _, digest, _ = self.client.request("GET", f"/digests/{run['digest_id']}")
        _, page, _ = self.client.request("GET", "/")
        rendered = page.decode("utf-8")
        content = digest["content"]["rendered_text"]
        self.assertIn(content, rendered)
        self.assertIn(f"{len(content)} 字", rendered)

    def test_public_http_never_exposes_harness_internals(self):
        created = self.create()
        _, run, _ = self.run_digest(created["subscription_id"], "sealed")
        payloads = []
        for path in ("/subscriptions", f"/runs/{run['application_run_id']}", "/digests", f"/digests/{run['digest_id']}", "/profile"):
            _, value, _ = self.client.request("GET", path)
            payloads.append(json.dumps(value).casefold())
        forbidden = ("harness_run_id", "evidence_id", "artifact_id", "checkpoint", "authorizedaction", "traceback", "provider_response")
        self.assertTrue(all(word not in "".join(payloads) for word in forbidden))

    def test_search_failure_remains_safe_incomplete_without_digest(self):
        class FailingSearch(FakeSearchClient):
            def call_tool(self, name, arguments):
                raise SearchAdapterError("NETWORK_ERROR")

        self.fixture.server.application.generation.search_client = FailingSearch([])
        created = self.create()
        status, run, _ = self.run_digest(created["subscription_id"], "search-fails")
        self.assertEqual(
            (status, run["status"], run["failure_reason"], run["digest_id"]),
            (200, "incomplete", "search_unavailable", None),
        )
        _, digests, _ = self.client.request("GET", "/digests")
        self.assertEqual(digests, [])

    def test_generation_timeout_api_and_ui_never_say_search_unavailable(self):
        class TimeoutProvider:
            def synthesize(self, *_arguments):
                raise ProviderAdapterError("TIMEOUT")

        self.fixture.server.application.generation.provider = TimeoutProvider()
        created = self.create()
        status, run, _ = self.run_digest(created["subscription_id"], "model-timeout")
        self.assertEqual(
            (status, run["status"], run["failure_stage"], run["failure_code"]),
            (200, "incomplete", "generation", "generation_timeout"),
        )
        _, persisted, _ = self.client.request(
            "GET", f"/runs/{run['application_run_id']}",
        )
        self.assertEqual(
            (persisted["failure_stage"], persisted["failure_code"]),
            ("generation", "generation_timeout"),
        )
        _, page, _ = self.client.request(
            "GET", f"/?last_run={run['application_run_id']}",
        )
        rendered = page.decode("utf-8")
        status_line = rendered.split('<div id="status">', 1)[1].split("</div>", 1)[0]
        self.assertIn("Stage: Generation", status_line)
        self.assertIn("Model request timed out", status_line)
        self.assertNotIn("Search unavailable", status_line)

    def test_generation_schema_subtype_is_safe_in_api_and_ui(self):
        provider = SchemaInvalidToolProvider()
        self.fixture.server.application.generation.provider = provider
        created = self.create()
        status, run, _ = self.run_digest(
            created["subscription_id"], "schema-items-object",
        )
        self.assertEqual(
            (status, run["status"], run["failure_stage"],
             run["failure_code"], run["failure_subtype"], run["digest_id"]),
            (200, "incomplete", "generation",
             "generation_invalid_response", "ITEMS_TYPE", None),
        )
        self.assertEqual(len(provider.calls), 2)
        _, persisted, _ = self.client.request(
            "GET", f"/runs/{run['application_run_id']}",
        )
        self.assertEqual(persisted["failure_subtype"], "ITEMS_TYPE")
        _, page, _ = self.client.request(
            "GET", f"/?last_run={run['application_run_id']}",
        )
        status_line = page.decode("utf-8").split(
            '<div id="status">', 1,
        )[1].split("</div>", 1)[0]
        self.assertIn("Stage: Generation", status_line)
        self.assertIn("invalid items shape", status_line)
        self.assertNotIn("tool_calls", status_line)

    def test_contract_rejection_api_and_ui_show_safe_precise_reason(self):
        provider = FakeDigestProvider("overlong")
        self.fixture.server.application.generation.provider = provider
        created = self.create()
        status, run, _ = self.run_digest(
            created["subscription_id"], "contract-too-long",
        )
        self.assertEqual(
            (status, run["status"], run["failure_stage"],
             run["failure_code"], run["failure_subtype"]),
            (200, "incomplete", "contract", "output_contract_failed",
             "too_long"),
        )
        self.assertEqual(
            run["failure_diagnostics"]["expected_max_chars"], 600,
        )
        self.assertNotIn("rendered_text", json.dumps(run))
        self.assertEqual(len(provider.calls), 1)

        _, persisted, _ = self.client.request(
            "GET", f"/runs/{run['application_run_id']}",
        )
        self.assertEqual(persisted["failure_subtype"], "too_long")
        _, page, _ = self.client.request(
            "GET", f"/?last_run={run['application_run_id']}",
        )
        rendered = page.decode("utf-8")
        status_line = rendered.split(
            '<div id="status">', 1,
        )[1].split("</div>", 1)[0]
        self.assertIn("Stage: Contract", status_line)
        self.assertIn("Digest exceeded the 600-character limit", status_line)
        self.assertNotIn("max_chars_exceeded", status_line)


class FailureApplication:
    def list_subscriptions(self, _user): return ()
    def list_digests(self, _user): return ()
    def get_profile(self, _user):
        from apps.digest_agent.application import ProfileView
        return ProfileView(0, 1, (), "1970-01-01T00:00:00Z")
    def get_run(self, _user, _run):
        return RunView("b" * 32, "c" * 32, "key", "recovery_required", "recovery_required", None, 1, True)
    def create_subscription(self, _user, _request):
        raise RuntimeError("secret-token traceback provider body")


class DigestHTTPFailureTests(unittest.TestCase):
    def test_recovery_and_unexpected_errors_are_sanitized(self):
        with tempfile.TemporaryDirectory() as root:
            config = DigestAppConfig(os.path.join(root, "x.db"), root, root, user_id=USER)
            server = DigestHTTPServer(("127.0.0.1", 0), FailureApplication(), config)
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                client = HTTPClient(server)
                status, run, _ = client.request("GET", "/runs/" + "b" * 32)
                self.assertEqual((status, run["status"], run["failure_reason"]), (200, "recovery_required", "recovery_required"))
                status, error, _ = client.request("POST", "/subscriptions", {"request": "safe"})
                rendered = json.dumps(error)
                self.assertEqual((status, error["error"]["code"]), (500, "internal_error"))
                self.assertNotIn("secret-token", rendered)
                self.assertNotIn("traceback", rendered)
            finally:
                server.shutdown(); server.server_close(); worker.join(5)

    def test_non_loopback_bind_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            config = DigestAppConfig(os.path.join(root, "x.db"), root, root, user_id=USER)
            with self.assertRaises(ValueError):
                DigestHTTPServer(("0.0.0.0", 0), FailureApplication(), config)


if __name__ == "__main__":
    unittest.main()
