import json
import unittest

from apps.digest_agent.adapters.definition import (
    DEFINITION_TOOL_NAME, DEFINITION_TOOL_SCHEMA_IDENTITY,
    DEFINITION_WIRE_FIELDS,
    FakeDefinitionAgentAdapter, VertexDefinitionAgentAdapter,
)
from apps.digest_agent.adapters.provider import (
    ProviderAdapterError, VertexHTTPResponse,
)


CONTEXT = {
    "conversation_id": "1" * 32,
    "turn_count": 1,
    "messages": [{"role": "user", "content": "帮我订阅 AI 行业动态"}],
}
ENV = {
    "LLM_API_KEY": "safe-test-key",
    "LLM_API_MODE": "chat-completions",
    "LLM_ENDPOINT": "https://example.test/v1",
    "LLM_MODEL": "test-model",
}


class Transport:
    def __init__(self, arguments):
        self.arguments = arguments
        self.calls = []

    def post(self, url, headers, body, timeout, maximum_bytes):
        self.calls.append({
            "url": url, "headers": headers, "body": json.loads(body),
            "timeout": timeout, "maximum_bytes": maximum_bytes,
        })
        payload = {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [{
                        "type": "function",
                        "function": {
                            "name": DEFINITION_TOOL_NAME,
                            "arguments": json.dumps(
                                self.arguments, ensure_ascii=False,
                            ),
                        },
                    }],
                },
            }],
        }
        return VertexHTTPResponse(
            200, {}, json.dumps(payload, ensure_ascii=False).encode(),
        )


def flat(**changes):
    outcome_type = changes.pop("type")
    if outcome_type == "NEXT_QUESTION":
        payload = {"question": changes.pop("question")}
    elif outcome_type == "REJECT":
        payload = {"reason": changes.pop("reason")}
    else:
        max_chars = changes.pop("max_chars")
        max_items = changes.pop("max_items")
        if isinstance(max_chars, str) and (
                max_chars == "0" or max_chars.isdigit()
                and not max_chars.startswith("0")):
            max_chars = int(max_chars)
        if isinstance(max_items, str) and (
                max_items == "0" or max_items.isdigit()
                and not max_items.startswith("0")):
            max_items = int(max_items)
        payload = {"definition": {
            "topic": changes.pop("topic"),
            "language": changes.pop("language"),
            "cadence": changes.pop("cadence"),
            "max_chars": max_chars,
            "max_items": max_items,
            "focus_topics": json.loads(changes.pop("focus_topics_json")),
            "delivery_preference": changes.pop("delivery_preference"),
        }}
    if changes:
        payload.update(changes)
    return {
        "type": outcome_type,
        "payload_json": json.dumps(payload, ensure_ascii=False),
    }


class DefinitionProviderParityTests(unittest.TestCase):
    def assert_parity(self, candidate, wire):
        fake = FakeDefinitionAgentAdapter([candidate])
        transport = Transport(wire)
        vertex = VertexDefinitionAgentAdapter(
            transport=transport, environ=ENV, monotonic=lambda: 1.0,
        )
        self.assertEqual(fake.propose(CONTEXT), vertex.propose(CONTEXT))
        call = transport.calls[0]
        self.assertEqual(call["url"], "https://example.test/v1/chat/completions")
        self.assertEqual(
            call["body"]["tools"][0]["function"]["name"],
            DEFINITION_TOOL_NAME,
        )
        schema = call["body"]["tools"][0]["function"]["parameters"]
        self.assertEqual(set(schema["properties"]), set(DEFINITION_WIRE_FIELDS))
        self.assertEqual(schema["properties"]["type"]["enum"], [
            "NEXT_QUESTION", "REJECT", "DONE",
        ])
        rendered = json.dumps(vertex.calls)
        self.assertNotIn("safe-test-key", rendered)
        self.assertNotIn("帮我订阅", rendered)
        prompt = call["body"]["messages"][0]["content"]
        self.assertIn("language is exactly zh-CN or en", prompt)
        self.assertIn("max_chars is 100..4000", prompt)
        self.assertIn("Never invent defaults", prompt)
        self.assertIn("single-line plain text", prompt)

    def test_fake_and_vertex_share_next_reject_done_boundary(self):
        cases = [(
            {
                "protocol_version": 1, "type": "NEXT_QUESTION",
                "question": "每篇希望控制在多少字以内？",
            },
            flat(
                type="NEXT_QUESTION",
                question="每篇希望控制在多少字以内？",
            ),
        ), (
            {
                "protocol_version": 1, "type": "REJECT",
                "reason": "当前只支持资讯订阅。",
            },
            flat(type="REJECT", reason="当前只支持资讯订阅。"),
        ), (
            {
                "protocol_version": 1, "type": "DONE",
                "definition": {
                    "topic": "AI 行业动态", "language": "zh-CN",
                    "cadence": "daily", "max_chars": 600,
                    "max_items": 5,
                    "focus_topics": ["Agent", "模型发布"],
                    "delivery_preference": "none",
                },
            },
            flat(
                type="DONE", topic="AI 行业动态", language="zh-CN",
                cadence="daily", max_chars="600", max_items="5",
                focus_topics_json='["Agent","模型发布"]',
                delivery_preference="none",
            ),
        )]
        for candidate, wire in cases:
            with self.subTest(candidate["type"]):
                self.assert_parity(candidate, wire)

    def test_vertex_wire_is_strict_and_does_not_repair_extra_fields(self):
        invalid = flat(type="NEXT_QUESTION", question="问题")
        invalid["extra"] = "not allowed"
        adapter = VertexDefinitionAgentAdapter(
            transport=Transport(invalid), environ=ENV,
        )
        with self.assertRaisesRegex(ProviderAdapterError, "INVALID_RESPONSE"):
            adapter.propose(CONTEXT)

    def test_vertex_done_keeps_business_invalid_value_for_application_gate(self):
        adapter = VertexDefinitionAgentAdapter(
            transport=Transport(flat(
                type="DONE", topic="AI", language="zh-CN",
                cadence="daily", max_chars="99", max_items="5",
                focus_topics_json="[]", delivery_preference="none",
            )),
            environ=ENV,
        )
        candidate = adapter.propose(CONTEXT)
        self.assertEqual(candidate["definition"]["max_chars"], 99)

    def test_vertex_rejects_noncanonical_decimal_wire_values(self):
        adapter = VertexDefinitionAgentAdapter(
            transport=Transport(flat(
                type="DONE", topic="AI", language="zh-CN",
                cadence="daily", max_chars="0600", max_items="5",
                focus_topics_json="[]", delivery_preference="none",
            )),
            environ=ENV,
        )
        with self.assertRaisesRegex(ProviderAdapterError, "INVALID_RESPONSE"):
            adapter.propose(CONTEXT)

    def test_definition_uses_shared_strict_tool_and_safe_diagnostics(self):
        invalid = flat(type="NEXT_QUESTION", question="问题", reason="冲突")
        adapter = VertexDefinitionAgentAdapter(
            transport=Transport(invalid), environ=ENV,
        )
        with self.assertRaises(ProviderAdapterError) as caught:
            adapter.propose(CONTEXT)
        self.assertEqual(caught.exception.subtype, "SCHEMA_MISMATCH")
        self.assertEqual(
            adapter.last_attempt["schema_mismatch_rule"], "VARIANT_FIELDS",
        )
        described = adapter.describe_attempt(CONTEXT)
        self.assertEqual(
            described["schema_identity"], DEFINITION_TOOL_SCHEMA_IDENTITY,
        )
        self.assertEqual(
            described["structured_output_mechanism"],
            "strict_flat_scalar_tool_requested_prompt_reinforced",
        )
        rendered = json.dumps(adapter.last_attempt)
        self.assertNotIn("safe-test-key", rendered)
        self.assertNotIn("冲突", rendered)

    def test_definition_fails_closed_without_strict_chat_tool_mode(self):
        environ = dict(ENV, LLM_API_MODE="completions")
        adapter = VertexDefinitionAgentAdapter(
            transport=Transport(flat(type="REJECT", reason="拒绝")),
            environ=environ,
        )
        with self.assertRaisesRegex(ProviderAdapterError, "CONFIGURATION_ERROR"):
            adapter.propose(CONTEXT)


if __name__ == "__main__":
    unittest.main()
