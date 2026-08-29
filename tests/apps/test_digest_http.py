from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import http.client
import json
import os
import re
import tempfile
import threading
import time
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
                "question": "重点关注哪些方向？",
            }, {
                "protocol_version": 1, "type": "NEXT_QUESTION",
                "question": "只关注重大变化，还是也关注日常案例？",
            }, {
                "protocol_version": 1, "type": "NEXT_QUESTION",
                "question": "更关心全球动态，还是国内应用？",
            }, {
                "protocol_version": 1, "type": "DONE",
                "definition": {
                    "topic": "AI 行业动态", "language": "zh-CN",
                    "cadence": "daily", "max_chars": 600,
                    "max_items": 5, "focus_topics": ["Agent"],
                    "delivery_preference": "none",
                },
            }, {
                "protocol_version": 1, "type": "DONE",
                "definition": {
                    "topic": "AI 行业动态", "language": "zh-CN",
                    "cadence": "daily", "max_chars": 800,
                    "max_items": 3, "focus_topics": ["Agent", "开发工具"],
                    "delivery_preference": "termux_notification",
                },
            }]))
        status, first, _ = self.start_conversation("帮我订阅 AI 行业动态")
        self.assertEqual(
            (status, first["status"], first["turn_count"]),
            (201, "WAITING_FOR_ANSWER", 1),
        )
        path = f"/conversations/{first['conversation_id']}/messages"
        answers = ("Agent", "只关注重大变化", "全球动态")
        terminal = None
        for number, answer in enumerate(answers, 1):
            status, terminal, _ = self.client.request(
                "POST", path, {"message": answer},
                headers={"Idempotency-Key": f"conversation-answer-{number}"},
            )
            self.assertEqual(status, 200)
        self.assertEqual(terminal["turn_count"], 4)
        self.assertEqual((status, terminal["status"]),
                         (200, "DEFINITION_ACCEPTED"))
        status, restored, _ = self.client.request(
            "GET", f"/conversations/{first['conversation_id']}",
        )
        self.assertEqual((status, restored["definition"]["max_chars"]),
                         (200, 600))
        status, subscriptions, _ = self.client.request("GET", "/subscriptions")
        self.assertEqual((status, subscriptions), (200, []))
        repository = self.fixture.server.application.repository
        with repository.connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM user_subscriptions",
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM briefing_reservations",
            ).fetchone()[0], 0)
        rendered = json.dumps(terminal).casefold()
        for forbidden in (
            "harness_run_id", "provider", "evidence", "artifact",
            "checkpoint", "raw_response",
        ):
            self.assertNotIn(forbidden, rendered)

        adjustment_path = (
            f"/conversations/{first['conversation_id']}/adjustments"
        )
        status, adjusted, _ = self.client.request(
            "POST", adjustment_path,
            {"message": "改为 800 字、3 条，并开启本机通知"},
            headers={"Idempotency-Key": "adjust-once"},
        )
        self.assertEqual(
            (status, adjusted["status"], adjusted["definition"]["max_chars"],
             adjusted["definition"]["max_items"],
             adjusted["definition"]["delivery_preference"]),
            (200, "DEFINITION_ACCEPTED", 800, 3, "termux_notification"),
        )
        status, replayed_adjustment, _ = self.client.request(
            "POST", adjustment_path,
            {"message": "改为 800 字、3 条，并开启本机通知"},
            headers={"Idempotency-Key": "adjust-once"},
        )
        self.assertEqual((status, replayed_adjustment["reused"]), (200, True))
        self.assertEqual(repository.list_subscriptions(), ())

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
        self.assertNotIn("relation_event", committed)
        self.assertNotIn("harness_run_id", committed)
        relation_event = repository.get_relation_event_for_relation(
            committed["user_subscription_id"],
        )
        relation_publisher = (
            self.fixture.server.application.relation_events.publisher
        )
        self.assertEqual(
            (relation_event.status, relation_event.event_type,
             relation_publisher.calls),
            ("pending", "USER_SUBSCRIPTION_CREATED", []),
        )
        status, replay, _ = self.client.request(
            "POST", commit_path, {},
        )
        self.assertEqual((status, replay["reused"]), (200, True))
        self.assertEqual(replay["subscription_id"], committed["subscription_id"])
        status, rejected_adjustment, _ = self.client.request(
            "POST", adjustment_path, {"message": "再改一次"},
            headers={"Idempotency-Key": "too-late"},
        )
        self.assertEqual(
            (status, rejected_adjustment["error"]["code"]),
            (409, "conversation_already_committed"),
        )
        status, subscriptions, _ = self.client.request("GET", "/subscriptions")
        self.assertEqual((status, len(subscriptions)), (200, 1))
        self.assertEqual(
            (subscriptions[0]["product_kind"],
             subscriptions[0]["product_status"],
             subscriptions[0]["definition_id"]),
            ("product", "ACTIVE", committed["definition_id"]),
        )
        generation = self.fixture.server.application.generation
        briefing_path = (
            f"/subscriptions/{committed['subscription_id']}/briefings/latest"
        )
        deadline = time.monotonic() + 5
        ready = None
        while time.monotonic() < deadline:
            status, ready, _ = self.client.request("GET", briefing_path)
            if ready["status"] == "READY":
                break
            time.sleep(0.02)
        self.assertEqual(
            (status, ready["subscription_status"], ready["status"]),
            (200, "ACTIVE", "READY"),
        )
        self.assertEqual((len(generation.search_client.calls),
                          len(generation.provider.calls)), (1, 1))
        status, page, _ = self.client.request("GET", "/")
        rendered_page = page.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("最新内容", rendered_page)
        self.assertIn("AI 行业动态", rendered_page)
        self.assertIn("2 条内容", rendered_page)

    def test_conversation_http_idempotency_and_exact_fields(self):
        status, first, _ = self.start_conversation(
            "帮我关注深圳往返武汉的机票优惠", "same-conversation",
        )
        status2, duplicate, _ = self.start_conversation(
            "帮我关注深圳往返武汉的机票优惠", "same-conversation",
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

    def test_intent_confirmation_keeps_flight_semantics_and_defaults_separate(self):
        status, proposal, _ = self.start_conversation(
            "关注深圳到武汉 9 月往返机票，低于 800 元提醒我",
            "intent-flight",
        )
        self.assertEqual(
            (status, proposal["status"], proposal["turn_count"]),
            (201, "DEFINITION_ACCEPTED", 1),
        )
        definition = proposal["definition"]
        self.assertEqual(
            (definition["topic"], definition["time_window"],
             definition["constraints"], definition["trigger"]),
            ("深圳往返武汉的机票优惠", "9 月", ["低于800元"],
             "票价低于800元时提醒"),
        )
        self.assertEqual(
            (definition["provenance"]["topic"],
             definition["provenance"]["max_chars"],
             definition["provenance"]["cadence"]),
            ("USER_EXPLICIT", "PRODUCT_DEFAULT", "PRODUCT_DEFAULT"),
        )
        self.assertEqual(
            (definition["cadence"], definition["resolved_time_window"]),
            ("6h", "2026 年 9 月"),
        )
        self.assertNotIn("AI 行业动态", json.dumps(
            proposal, ensure_ascii=False,
        ))
        self.assertNotRegex(proposal.get("question") or "", r"字|条")
        _, subscriptions, _ = self.client.request("GET", "/subscriptions")
        self.assertEqual(subscriptions, [])

        status, page, _ = self.client.request("GET", "/create")
        rendered = page.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("你告诉我的", rendered)
        self.assertIn("系统默认设置", rendered)
        self.assertIn("关键条件", rendered)
        self.assertIn("提醒条件", rendered)
        self.assertIn("深圳到武汉 9 月往返机票", rendered)
        self.assertNotIn("AI 行业动态", rendered)

        status, committed, _ = self.client.request(
            "POST",
            f"/conversations/{proposal['conversation_id']}/subscription", {},
        )
        self.assertEqual((status, committed["status"]), (201, "ACTIVE"))
        self.assertEqual(committed["workflow_kind"], "CONDITION")
        self.assertIsNone(committed["first_briefing_application_run_id"])
        self.assertIsNone(committed["first_briefing_status"])
        _, subscription, _ = self.client.request(
            "GET", f"/subscriptions/{committed['subscription_id']}",
        )
        self.assertEqual(
            subscription["topic"], "深圳往返武汉的机票优惠",
        )
        self.assertNotEqual(subscription["topic"], "AI 行业动态")
        status, detail, _ = self.client.request(
            "GET", f"/api/feeds/{committed['subscription_id']}",
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            (detail["current_definition"]["time_window"],
             detail["current_definition"]["constraints"],
             detail["current_definition"]["trigger"],
             detail["current_definition"]["locations"]),
            ("9 月", ["低于800元"], "票价低于800元时提醒", ["深圳", "武汉"]),
        )
        status, feed_page, _ = self.client.request(
            "GET", f"/feeds/{committed['subscription_id']}",
        )
        feed_html = feed_page.decode("utf-8")
        self.assertEqual(status, 200)
        for value in (
                "9 月", "2026 年 9 月", "每 6 小时", "低于800元",
                "票价低于800元时提醒", "深圳 → 武汉"):
            self.assertIn(value, feed_html)
        self.assertNotIn("AI 行业动态", feed_html)
        deadline = time.monotonic() + 5
        monitoring = None
        while time.monotonic() < deadline:
            _, detail, _ = self.client.request(
                "GET", f"/api/feeds/{committed['subscription_id']}",
            )
            monitoring = detail["condition_monitoring"]
            if monitoring["status"] != "MONITORING":
                break
            time.sleep(0.02)
        self.assertEqual(
            (monitoring["status"], monitoring["latest_price"],
             monitoring["threshold"], monitoring["condition_met"],
             monitoring["lifecycle_status"], monitoring["cadence_seconds"],
             monitoring["travel_year"]),
            ("NO_UPDATE", 920, 800, False, "ACTIVE", 21600, 2026),
        )
        status, missing, _ = self.client.request(
            "GET",
            f"/subscriptions/{committed['subscription_id']}/briefings/latest",
        )
        self.assertEqual((status, missing["error"]["code"]), (404, "not_found"))
        generation = self.fixture.server.application.generation
        self.assertEqual(
            (len(generation.search_client.calls), len(generation.provider.calls)),
            (0, 0),
        )
        repository = self.fixture.server.application.repository
        with repository.connect() as connection:
            counts = tuple(connection.execute(
                f"SELECT COUNT(*) FROM {table}",
            ).fetchone()[0] for table in (
                "briefing_reservations", "application_outbox",
                "tracking_updates", "update_distributions",
            ))
        self.assertEqual(counts, (0, 0, 0, 0))

    def test_verified_event_http_and_ui_hide_internal_verification_details(self):
        timestamp = (datetime.now(timezone.utc) + timedelta(minutes=1))
        published = timestamp.isoformat().replace("+00:00", "Z")
        self.fixture.server.application.activations.clock = lambda: published
        self.fixture.server.application.events.clock = lambda: published
        self.fixture.server.application.events.source.clock = lambda: published
        self.fixture.server.application.events.source.enqueue({
            "retrieved_at": published,
            "results": [{
                "source_ref": "official",
                "canonical_url": "https://openai.com/index/model-x",
                "publisher": "OpenAI", "source_kind": "official_primary",
                "title": "OpenAI released Model X",
                "snippet": "Model X is now available.",
                "published_at": published,
            }],
        })
        status, proposal, _ = self.start_conversation(
            "OpenAI 发布新模型时告诉我。", "event-start",
        )
        self.assertEqual((status, proposal["status"]), (201, "DEFINITION_ACCEPTED"))
        status, committed, _ = self.client.request(
            "POST", f"/conversations/{proposal['conversation_id']}/subscription", {},
        )
        self.assertEqual(
            (status, committed["workflow_kind"],
             committed["first_briefing_application_run_id"]),
            (201, "EVENT", None),
        )
        deadline = time.monotonic() + 5
        detail = None
        while time.monotonic() < deadline:
            _, detail, _ = self.client.request(
                "GET", f"/api/feeds/{committed['subscription_id']}",
            )
            if detail["event_monitoring"]["status"] != "MONITORING":
                break
            time.sleep(0.02)
        self.assertEqual(
            (detail["workflow_kind"], detail["event_monitoring"]["status"],
             detail["history"][0]["model_name"]),
            ("EVENT", "VERIFIED", "Model X"),
        )
        encoded = json.dumps(detail, ensure_ascii=False)
        for hidden in (
                "candidate_id", "evidence_id", "verification_id",
                "logical_event_identity", "harness_run_id"):
            self.assertNotIn(hidden, encoded)
        status, page, _ = self.client.request(
            "GET", f"/feeds/{committed['subscription_id']}",
        )
        html = page.decode("utf-8")
        self.assertEqual(status, 200)
        for visible in (
                "正在关注 OpenAI 新模型", "发现并验证了 OpenAI 新模型发布",
                "Model X", "官方来源"):
            self.assertIn(visible, html)
        self.assertNotIn("logical_event_identity", html)

    def test_matched_flight_condition_is_projected_as_update_not_briefing(self):
        conditions = self.fixture.server.application.conditions
        conditions.provider.price = 760
        conditions.provider.source_signal_id = (
            "fake:SZX:WUH:2026-09:http-matched"
        )
        status, proposal, _ = self.start_conversation(
            "关注深圳—武汉 9 月往返机票，低于 800 元提醒我。",
            "flight-http-matched",
        )
        self.assertEqual((status, proposal["status"]),
                         (201, "DEFINITION_ACCEPTED"))
        status, committed, _ = self.client.request(
            "POST",
            f"/conversations/{proposal['conversation_id']}/subscription", {},
        )
        self.assertEqual(
            (status, committed["workflow_kind"],
             committed["first_briefing_status"]),
            (201, "CONDITION", None),
        )
        feed_path = f"/api/feeds/{committed['subscription_id']}"
        deadline = time.monotonic() + 5
        detail = None
        while time.monotonic() < deadline:
            _, detail, _ = self.client.request("GET", feed_path)
            if detail["condition_monitoring"]["status"] != "MONITORING":
                break
            time.sleep(0.02)
        monitoring = detail["condition_monitoring"]
        self.assertEqual(
            (monitoring["status"], monitoring["latest_price"],
             monitoring["threshold"], monitoring["condition_met"],
             len(detail["history"]), detail["history"][0]["update_kind"]),
            ("MATCHED", 760, 800, True, 1, "CONDITION"),
        )
        status, page, _ = self.client.request(
            "GET", f"/feeds/{committed['subscription_id']}",
        )
        rendered = page.decode("utf-8")
        self.assertEqual(status, 200)
        for text in (
                "当前监测状态", "最近价格 ¥760", "已达到低于 ¥800",
                "更新历史", "达到提醒条件"):
            self.assertIn(text, rendered)
        for hidden in (
                "Evidence", "predicate engine", "Harness Run",
                "Observation ID", "observation_id", "evidence_id"):
            self.assertNotIn(hidden, rendered)
        repository = self.fixture.server.application.repository
        with repository.connect() as connection:
            counts = tuple(connection.execute(
                f"SELECT COUNT(*) FROM {table}",
            ).fetchone()[0] for table in (
                "tracking_updates", "update_distributions",
                "briefing_reservations", "application_outbox",
            ))
        self.assertEqual(counts, (1, 1, 0, 0))

    def test_flight_condition_pause_resume_has_safe_http_projection(self):
        status, proposal, _ = self.start_conversation(
            "持续关注深圳—武汉 9 月往返机票，低于 800 元提醒我。",
            "flight-http-pause-resume",
        )
        self.assertEqual((status, proposal["status"]),
                         (201, "DEFINITION_ACCEPTED"))
        status, committed, _ = self.client.request(
            "POST",
            f"/conversations/{proposal['conversation_id']}/subscription", {},
        )
        self.assertEqual((status, committed["workflow_kind"]),
                         (201, "CONDITION"))
        subscription_id = committed["subscription_id"]

        status, paused, _ = self.client.request(
            "POST", f"/subscriptions/{subscription_id}/disable",
            {"expected_version": 1},
        )
        self.assertEqual(
            (status, paused["product_status"], paused["enabled"]),
            (200, "PAUSED", False),
        )
        status, detail, _ = self.client.request(
            "GET", f"/api/feeds/{subscription_id}",
        )
        self.assertEqual(
            (status, detail["feed_state"],
             detail["condition_monitoring"]["status"],
             detail["condition_monitoring"]["next_due_at"]),
            (200, "paused", "PAUSED", None),
        )
        status, page, _ = self.client.request("GET", "/following")
        rendered = page.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("已暂停", rendered)
        self.assertIn('data-action="enable"', rendered)
        self.assertNotIn("evidence_id", rendered)

        status, resumed, _ = self.client.request(
            "POST", f"/subscriptions/{subscription_id}/enable",
            {"expected_version": 2},
        )
        self.assertEqual(
            (status, resumed["product_status"], resumed["enabled"]),
            (200, "ACTIVE", True),
        )
        repository = self.fixture.server.application.repository
        cycles = repository.list_condition_cycles(subscription_id)
        self.assertEqual([item.cycle_kind for item in cycles][-1], "RESUME")

    def test_distribution_notification_is_safe_in_feed_http_projection(self):
        conditions = self.fixture.server.application.conditions
        conditions.provider.price = 760
        conditions.provider.source_signal_id = (
            "fake:SZX:WUH:2026-09:http-notification"
        )
        status, proposal, _ = self.start_conversation(
            "持续关注深圳—武汉 9 月往返机票，低于 800 元提醒我，并使用本机通知。",
            "flight-http-notification",
        )
        self.assertEqual((status, proposal["status"]),
                         (201, "DEFINITION_ACCEPTED"))
        self.assertEqual(
            proposal["definition"]["delivery_preference"],
            "termux_notification",
        )
        status, committed, _ = self.client.request(
            "POST",
            f"/conversations/{proposal['conversation_id']}/subscription", {},
        )
        self.assertEqual((status, committed["workflow_kind"]),
                         (201, "CONDITION"))
        path = f"/api/feeds/{committed['subscription_id']}"
        deadline = time.monotonic() + 5
        detail = None
        while time.monotonic() < deadline:
            _, detail, _ = self.client.request("GET", path)
            if (detail["history"]
                    and detail["history"][0]["notification_status"] == "SENT"):
                break
            time.sleep(0.02)
        self.assertEqual(
            (detail["update_state"], len(detail["history"]),
             detail["history"][0]["notification_status"],
             detail["history"][0]["notification_message"]),
            ("ready", 1, "SENT", "通知请求已发送。"),
        )
        status, page, _ = self.client.request(
            "GET", f"/feeds/{committed['subscription_id']}",
        )
        rendered = page.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("通知请求已发送", rendered)
        for hidden in (
                "effect_certainty", "AuthorizedAction", "attempt_id",
                "termux-notification", "Outbox", "evidence_id"):
            self.assertNotIn(hidden, rendered)
        adapter = self.fixture.server.application.deliveries.adapters[
            "termux_notification"
        ]
        self.assertEqual(len(adapter.calls), 1)

    def test_unsupported_nonflight_condition_fails_closed_before_briefing(self):
        status, proposal, _ = self.start_conversation(
            "当黄金价格低于 700 元时提醒我",
            "unsupported-gold-condition",
        )
        self.assertEqual(
            (status, proposal["status"]),
            (201, "DEFINITION_ACCEPTED"),
        )
        status, rejected, _ = self.client.request(
            "POST",
            f"/conversations/{proposal['conversation_id']}/subscription", {},
        )
        self.assertEqual(
            (status, rejected["error"]["code"]),
            (400, "unsupported_tracking_intent"),
        )
        repository = self.fixture.server.application.repository
        with repository.connect() as connection:
            counts = tuple(connection.execute(
                f"SELECT COUNT(*) FROM {table}",
            ).fetchone()[0] for table in (
                "subscription_aggregates", "briefing_reservations",
                "application_outbox", "digest_runs", "tracking_updates",
                "update_distributions",
            ))
        self.assertEqual(counts, (0, 0, 0, 0, 0, 0))
        generation = self.fixture.server.application.generation
        self.assertEqual(
            (len(generation.search_client.calls), len(generation.provider.calls)),
            (0, 0),
        )

    def test_unsupported_event_fails_closed_before_briefing(self):
        self.fixture.server.application.conversations.provider = (
            FakeDefinitionAgentAdapter([{
                "protocol_version": 2, "type": "DONE", "intent": {
                    "topic": {"value": "Anthropic 新模型发布", "source_turn": 1},
                    "constraints": [], "goal": None,
                    "trigger": {"value": "出现新模型时提醒", "source_turn": 1},
                    "time_window": None, "locations": [],
                    "focus_topics": [], "preferences": {},
                },
            }])
        )
        status, proposal, _ = self.start_conversation(
            "关注 Anthropic 新模型发布，有新模型就提醒我",
            "unsupported-event",
        )
        self.assertEqual(
            (status, proposal["status"]),
            (201, "DEFINITION_ACCEPTED"),
        )
        status, rejected, _ = self.client.request(
            "POST",
            f"/conversations/{proposal['conversation_id']}/subscription", {},
        )
        self.assertEqual(
            (status, rejected["error"]["code"]),
            (400, "unsupported_tracking_intent"),
        )
        repository = self.fixture.server.application.repository
        with repository.connect() as connection:
            counts = tuple(connection.execute(
                f"SELECT COUNT(*) FROM {table}",
            ).fetchone()[0] for table in (
                "subscription_aggregates", "briefing_reservations",
                "application_outbox", "digest_runs", "tracking_updates",
                "update_distributions",
            ))
        self.assertEqual(counts, (0, 0, 0, 0, 0, 0))
        generation = self.fixture.server.application.generation
        self.assertEqual(
            (len(generation.search_client.calls), len(generation.provider.calls)),
            (0, 0),
        )

    def test_pending_confirmation_and_first_briefing_survive_web_restart(self):
        with tempfile.TemporaryDirectory() as root:
            config = DigestAppConfig(
                os.path.join(root, "digest.db"),
                os.path.join(root, "workspace"),
                os.path.join(root, "audit"), user_id=USER,
            )
            first_server = create_http_server(
                config, port=0, auto_first_briefing=False,
            )
            first_thread = threading.Thread(target=first_server.serve_forever)
            first_thread.start()
            first_client = HTTPClient(first_server)
            try:
                first_server.application.conversations.provider = (
                    FakeDefinitionAgentAdapter([{
                        "protocol_version": 1, "type": "DONE",
                        "definition": {
                            "topic": "AI 行业动态", "language": "zh-CN",
                            "cadence": "daily", "max_chars": 600,
                            "max_items": 5, "focus_topics": ["Agent"],
                            "delivery_preference": "none",
                        },
                    }])
                )
                status, proposal, _ = first_client.request(
                    "POST", "/conversations", {"message": "关注 AI Agent"},
                    headers={"Idempotency-Key": "restart-proposal"},
                )
                self.assertEqual(
                    (status, proposal["status"]),
                    (201, "DEFINITION_ACCEPTED"),
                )
            finally:
                first_server.shutdown()
                first_server.server_close()
                first_thread.join(5)

            second_server = create_http_server(config, port=0)
            second_thread = threading.Thread(target=second_server.serve_forever)
            second_thread.start()
            second_client = HTTPClient(second_server)
            try:
                path = f"/conversations/{proposal['conversation_id']}"
                status, restored, _ = second_client.request("GET", path)
                self.assertEqual(
                    (status, restored["status"], restored["definition"]["topic"]),
                    (200, "DEFINITION_ACCEPTED", "AI 行业动态"),
                )
                status, listed, _ = second_client.request(
                    "GET", "/subscriptions",
                )
                self.assertEqual((status, listed), (200, []))

                status, committed, _ = second_client.request(
                    "POST", path + "/subscription", {},
                )
                self.assertEqual(
                    (status, committed["status"],
                     committed["first_briefing_status"]),
                    (201, "ACTIVE", "PENDING"),
                )
                briefing_path = (
                    f"/subscriptions/{committed['subscription_id']}"
                    "/briefings/latest"
                )
                deadline = time.monotonic() + 5
                briefing = None
                while time.monotonic() < deadline:
                    _, briefing, _ = second_client.request(
                        "GET", briefing_path,
                    )
                    if briefing["status"] == "READY":
                        break
                    time.sleep(0.02)
                self.assertEqual(briefing["status"], "READY")
            finally:
                second_server.shutdown()
                second_server.server_close()
                second_thread.join(5)

    def test_updates_and_feed_detail_product_journey_is_read_only_and_sealed(self):
        with tempfile.TemporaryDirectory() as root:
            config = DigestAppConfig(
                os.path.join(root, "digest.db"),
                os.path.join(root, "workspace"),
                os.path.join(root, "audit"), user_id=USER,
            )
            server = create_http_server(
                config, port=0, auto_first_briefing=False,
            )
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            client = HTTPClient(server)
            definitions = []
            for topic, focus in (
                ("AI 行业动态", ["Agent"]),
                ("AI 行业动态", ["模型发布"]),
                ("AI 行业动态", ["Agent"]),
            ):
                definitions.append({
                    "protocol_version": 1, "type": "DONE",
                    "definition": {
                        "topic": topic, "language": "zh-CN",
                        "cadence": "daily", "max_chars": 600,
                        "max_items": 2, "focus_topics": focus,
                        "delivery_preference": "none",
                    },
                })
            server.application.conversations.provider = (
                FakeDefinitionAgentAdapter(definitions)
            )

            def commit(message, key):
                status, proposal, _ = client.request(
                    "POST", "/conversations", {"message": message},
                    headers={"Idempotency-Key": key},
                )
                self.assertEqual(
                    (status, proposal["status"]),
                    (201, "DEFINITION_ACCEPTED"),
                )
                status, value, _ = client.request(
                    "POST",
                    f"/conversations/{proposal['conversation_id']}/subscription",
                    {},
                )
                self.assertEqual(
                    (status, value["first_briefing_status"]),
                    (201, "PENDING"),
                )
                return value

            try:
                ready = commit("关注 AI Agent", "p42-ready")
                completed = server.application.run_outbox_once()
                self.assertEqual(completed.first_briefing_status, "READY")

                failed_provider = FakeDigestProvider("overlong")
                server.application.generation.provider = failed_provider
                failed = commit("关注模型发布", "p42-failed")
                incomplete = server.application.run_outbox_once()
                self.assertEqual(incomplete.first_briefing_status, "INCOMPLETE")

                server.application.generation.provider = FakeDigestProvider()
                preparing = commit("关注 Python 工具", "p42-preparing")

                search = server.application.generation.search_client
                delivery = server.application.deliveries.adapters["fake"]
                before_calls = (
                    len(search.calls), len(failed_provider.calls),
                    len(server.application.generation.provider.calls),
                    len(delivery.calls),
                )
                before_work = server.application.repository.list_application_outbox()

                status, home, _ = client.request("GET", "/api/updates")
                self.assertEqual(status, 200)
                self.assertEqual(
                    (home["ready_updates"][0]["feed_id"],
                     home["needs_attention"][0]["feed_id"],
                     home["preparing"][0]["feed_id"]),
                    (ready["subscription_id"], failed["subscription_id"],
                     preparing["subscription_id"]),
                )
                self.assertEqual(
                    (home["ready_updates"][0]["update_state"],
                     home["needs_attention"][0]["update_state"],
                     home["needs_attention"][0]["feed_state"],
                     home["preparing"][0]["update_state"]),
                    ("ready", "failed", "active", "preparing"),
                )
                self.assertIn("可以稍后再看",
                              home["needs_attention"][0]["message"])

                status, page, _ = client.request("GET", "/")
                rendered = page.decode("utf-8")
                self.assertEqual(status, 200)
                self.assertLess(rendered.index("最新内容"),
                                rendered.index("正在准备"))
                self.assertIn("需要留意", rendered)
                self.assertNotIn("overlong", rendered.casefold())

                detail_path = f"/api/feeds/{ready['subscription_id']}"
                status, detail, _ = client.request("GET", detail_path)
                self.assertEqual(
                    (status, detail["feed_state"], detail["update_state"],
                     detail["current_definition"]["topic"]),
                    (200, "active", "ready", "AI 行业动态"),
                )
                item = detail["history"][0]["items"][0]
                self.assertTrue(item["sources"])
                self.assertTrue(item["why_recommended"])
                self.assertEqual(item["sources"][0]["domain"], "example.test")
                status, detail_page, _ = client.request(
                    "GET", f"/feeds/{ready['subscription_id']}",
                )
                detail_html = detail_page.decode("utf-8")
                for label in (
                    "资讯历史", "为什么推荐", "当前关注范围",
                    "本期采用的关注范围", "原始来源",
                ):
                    if label == "原始来源":
                        continue
                    self.assertIn(label, detail_html)

                serialized = json.dumps(
                    {"home": home, "detail": detail},
                    ensure_ascii=False,
                ).casefold()
                for forbidden in (
                    "harness", "evidence", "outbox", "provider",
                    "application_run", "failure_stage", "failure_code",
                ):
                    self.assertNotIn(forbidden, serialized)
                for html in (rendered, detail_html):
                    lowered = html.casefold()
                    for forbidden in (
                        "run", "digest", "outbox", "provider", "stage",
                        "cli", "evidence", "harness",
                    ):
                        self.assertIsNone(re.search(
                            rf"\b{re.escape(forbidden)}\b", lowered,
                        ))

                self.assertEqual(
                    server.application.repository.list_application_outbox(),
                    before_work,
                )
                self.assertEqual(
                    (len(search.calls), len(failed_provider.calls),
                     len(server.application.generation.provider.calls),
                     len(delivery.calls)),
                    before_calls,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)

    def test_web_ui_uses_server_conversation_state_without_one_turn_limit(self):
        status, page, _ = self.client.request("GET", "/create")
        self.assertEqual(status, 200)
        text = page.decode("utf-8")
        self.assertIn("/conversations", text)
        self.assertIn("/subscription", text)
        self.assertIn("/adjustments", text)
        for label in (
            "你告诉我的", "系统默认设置", "关注对象", "重点方向",
            "语言", "更新频率",
            "每期条数", "长度", "通知", "确认订阅", "继续调整",
            "正在处理上一次回答", "重新描述",
        ):
            self.assertIn(label, text)
        self.assertIn("订阅成功，正在准备首篇资讯。", text)
        self.assertIn("WAITING_FOR_ANSWER", text)
        self.assertNotIn("asked_once", text)
        self.assertNotIn("定义已接受，正在提交订阅", text)
        self.assertNotIn("Run now", text)
        self.assertNotIn("Definition ·", text)

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

    def test_product_e2e_changes_ranking_after_likes(self):
        created = self.create(
            "帮我订阅 AI 行业动态，每次 600 字以内，重点关注 Agent、模型发布。",
        )
        _, run1, _ = self.run_digest(created["subscription_id"], "first-run")
        _, digest1, _ = self.client.request("GET", f"/digests/{run1['digest_id']}")
        order1 = [item["item_id"] for item in digest1["content"]["items"]]
        target = digest1["content"]["items"][1]
        for event_key in ("like-model-1", "like-model-2"):
            status, feedback, _ = self.client.request(
                "POST", f"/digests/{run1['digest_id']}/feedback", {
                    "type": "liked", "event_key": event_key,
                    "item_id": target["item_id"],
                },
            )
            self.assertEqual((status, feedback["applied"]), (200, True))
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

    def test_feed_detail_renders_items_sources_history_and_explanation(self):
        created = self.create()
        _, run, _ = self.run_digest(created["subscription_id"], "web-content")
        _, digest, _ = self.client.request("GET", f"/digests/{run['digest_id']}")
        _, page, _ = self.client.request(
            "GET", f"/feeds/{created['subscription_id']}",
        )
        rendered = page.decode("utf-8")
        for item in digest["content"]["items"]:
            text = item["text"].split(" [S", 1)[0]
            title, summary = text.split("：", 1)
            self.assertIn(title, rendered)
            self.assertIn(summary, rendered)
        self.assertIn("资讯历史", rendered)
        self.assertIn("为什么推荐", rendered)
        self.assertIn("本期采用的关注范围", rendered)
        self.assertIn("example.test", rendered)
        self.assertNotIn("evidence", rendered.casefold())

    def test_public_http_never_exposes_harness_internals(self):
        created = self.create()
        _, run, _ = self.run_digest(created["subscription_id"], "sealed")
        payloads = []
        for path in (
            "/subscriptions", f"/runs/{run['application_run_id']}",
            "/digests", f"/digests/{run['digest_id']}", "/profile",
            "/api/updates", f"/api/feeds/{created['subscription_id']}",
        ):
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

    def test_generation_timeout_api_is_precise_but_home_seals_run_details(self):
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
        self.assertIn("暂时没有新内容", rendered)
        self.assertNotIn("Model request timed out", rendered)
        self.assertNotIn("Stage: Generation", rendered)

    def test_generation_schema_subtype_is_safe_in_api_and_sealed_from_home(self):
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
        rendered = page.decode("utf-8")
        self.assertIn("暂时没有新内容", rendered)
        self.assertNotIn("tool_calls", rendered)

    def test_contract_rejection_api_is_precise_but_home_seals_run_details(self):
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
        self.assertIn("暂时没有新内容", rendered)
        self.assertNotIn("Digest exceeded", rendered)
        self.assertNotIn("Stage: Contract", rendered)


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
