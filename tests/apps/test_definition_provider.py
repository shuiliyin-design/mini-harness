import json
import unittest

from apps.digest_agent.adapters.definition import (
    DEFINITION_TOOL_NAME, DEFINITION_WIRE_FIELDS,
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
    value = {name: "" for name in DEFINITION_WIRE_FIELDS}
    value.update(changes)
    return value


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
        rendered = json.dumps(vertex.calls)
        self.assertNotIn("safe-test-key", rendered)
        self.assertNotIn("帮我订阅", rendered)

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


if __name__ == "__main__":
    unittest.main()
