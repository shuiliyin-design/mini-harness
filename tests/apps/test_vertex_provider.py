import copy
import json
import os
import tempfile
import unittest

from apps.digest_agent.adapters.provider import (
    LLM_API_KEY, LLM_API_MODE, LLM_ENDPOINT, LLM_MODEL,
    VERTEX_TOOL_CANDIDATE_SCHEMA, FakeDigestProvider, ProviderAdapterError,
    VertexDigestProvider, VertexHTTPResponse,
)
from apps.digest_agent.adapters.search import FakeSearchClient
from apps.digest_agent.adapters.sqlite import SQLiteDigestRepository
from apps.digest_agent.contracts import evaluate_digest_contract
from apps.digest_agent.domain import (
    InterestProfile, SearchObservation, Subscription, normalize_candidates,
    project_profile, rank_candidates,
)
from apps.digest_agent.services import SubscriptionService
from apps.digest_agent.workflows import DigestGenerationWorkflow
from tools.vertex_reliability_smoke import (
    EXPECTED_MECHANISM, PROVIDER_GATE_CRITERIA,
    provider_compatibility_gate_passes,
)


NOW = "2026-08-23T12:00:00Z"
EVIDENCE = "e" * 32
KEY = "vertex-fixture-credential-123456"
ENDPOINT = "https://vertex-gateway.example.test/v1"
MODEL = "sonnet-4.6"


class VertexCompatibilityGateTests(unittest.TestCase):
    @staticmethod
    def passing_result():
        return {
            **{name: True for name in PROVIDER_GATE_CRITERIA},
            "safe_ledger": True,
            "mechanism": EXPECTED_MECHANISM,
        }

    def test_contract_failure_cannot_pass_provider_gate(self):
        result = self.passing_result()
        result["contract"] = False

        self.assertFalse(provider_compatibility_gate_passes([result]))

    def test_complete_provider_chain_passes_provider_gate(self):
        self.assertTrue(provider_compatibility_gate_passes([
            self.passing_result(), self.passing_result(),
        ]))


class IdFactory:
    def __init__(self):
        self.value = 900

    def __call__(self):
        value = f"{self.value:032x}"
        self.value += 1
        return value


class FakeVertexTransport:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, headers, body, timeout, maximum_bytes):
        self.calls.append({
            "url": url, "headers": dict(headers), "body": body,
            "timeout": timeout, "maximum_bytes": maximum_bytes,
        })
        if self.error is not None:
            raise self.error
        return self.response


def subscription():
    return Subscription(
        subscription_id="1" * 32, user_id="2" * 32,
        topic="AI 行业动态",
        natural_language_request=(
            "订阅 AI 行业动态；不可见 marker=raw-interaction-history"
        ),
        cadence="daily", language="zh-CN", max_chars=600, max_items=2,
        focus_topics=("Agent", "模型发布"), delivery_channel="none",
        enabled=True, version=1, created_at=NOW, updated_at=NOW,
    )


def ranked_candidates():
    observation = SearchObservation(
        "3" * 32, "AI Agent", NOW, ({
            "url": "https://example.test/agent",
            "title": "Agent Runtime 发布",
            "snippet": "Agent Runtime 与开发工具更新。",
            "published_at": "2026-08-23T10:00:00Z",
            "topic_tags": ["AI 行业动态", "Agent"],
        }, {
            "url": "https://example.test/model",
            "title": "模型发布",
            "snippet": "新的教学模型正式发布。",
            "published_at": "2026-08-23T09:00:00Z",
            "topic_tags": ["AI 行业动态", "模型发布"],
        }),
    )
    return rank_candidates(
        normalize_candidates(observation, EVIDENCE), subscription(), NOW,
    )


def structured_candidate(selected=None):
    selected = selected or ranked_candidates()
    items, refs = [], []
    for index, ranked in enumerate(selected, 1):
        candidate = ranked.candidate
        source_ref_id = f"S{index}"
        items.append({
            "candidate_id": candidate.candidate_id,
            "content_identity": candidate.content_identity,
            "content": f"{candidate.title}：{candidate.snippet}",
            "recommendation_reason": "与订阅主题相关",
            "source_ref_ids": [source_ref_id],
        })
        refs.append({
            "source_ref_id": source_ref_id,
            "candidate_id": candidate.candidate_id,
        })
    return {
        "summary": "今日 AI Agent 与模型发布摘要。",
        "items": items,
        "selected_source_refs": refs,
    }


def tool_candidate(candidate=None):
    candidate = copy.deepcopy(candidate or structured_candidate())
    if "items" not in candidate:
        return candidate
    items = candidate.pop("items")
    refs = candidate.pop("selected_source_refs")
    item = items[0] if isinstance(items, list) and items else items
    ref = refs[0] if isinstance(refs, list) and refs else refs
    return {
        "summary": candidate.get("summary"),
        "candidate_id": (
            item.get("candidate_id") if isinstance(item, dict) else item
        ),
        "content_identity": (
            item.get("content_identity") if isinstance(item, dict) else None
        ),
        "content": item.get("content") if isinstance(item, dict) else None,
        "recommendation_reason": (
            item.get("recommendation_reason")
            if isinstance(item, dict) else None
        ),
        "source_ref_id": (
            ref.get("source_ref_id") if isinstance(ref, dict) else ref
        ),
    }


def response(candidate=None, *, status=200, headers=None, text=None,
             model_version="vertex-sonnet-4.6", chat=False):
    if text is None:
        value = candidate or structured_candidate()
        if chat:
            value = tool_candidate(value)
        text = json.dumps(
            value, ensure_ascii=False,
            separators=(",", ":"),
        )
    choice = (
        {
            "message": {
                "content": None,
                "tool_calls": [{
                    "type": "function",
                    "function": {
                        "name": "submit_digest_candidate",
                        "arguments": text,
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }
        if chat else {"text": text, "finish_reason": "stop"}
    )
    payload = {
        "id": "response-must-not-persist",
        "model": model_version,
        "choices": [choice],
        "raw_provider_field": "must-not-cross-adapter",
    }
    return VertexHTTPResponse(
        status, headers or {},
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )


def provider_for(http_response=None, error=None, *, mode="completions",
                 **kwargs):
    transport = FakeVertexTransport(http_response, error)
    environment = {
        LLM_API_KEY: KEY, LLM_API_MODE: mode,
        LLM_ENDPOINT: ENDPOINT, LLM_MODEL: MODEL,
    }
    provider = VertexDigestProvider(
        transport=transport, environ=environment, **kwargs,
    )
    return provider, transport


def chat_envelope(*, arguments=None, content=None,
                  function_name="submit_digest_candidate",
                  include_tool=True, include_arguments=True,
                  finish_reason="tool_calls"):
    message = {"content": content}
    if include_tool:
        function = {"name": function_name}
        if include_arguments:
            function["arguments"] = (
                json.dumps(tool_candidate(), ensure_ascii=False)
                if arguments is None else arguments
            )
        message["tool_calls"] = [{
            "type": "function", "function": function,
        }]
    payload = {
        "choices": [{
            "message": message, "finish_reason": finish_reason,
        }],
    }
    return VertexHTTPResponse(
        200, {}, json.dumps(payload, ensure_ascii=False).encode(),
    )


class VertexAdapterTests(unittest.TestCase):
    def synthesize(self, provider, selected=None, sub=None):
        selected = selected or ranked_candidates()
        sub = sub or subscription()
        profile = project_profile(
            InterestProfile.empty(sub.user_id, NOW), sub,
        )
        return provider.synthesize(
            sub, selected, "2026-08-23", "4" * 32, profile,
        ), selected, profile

    def contract(self, payload, selected, profile, sub=None):
        return evaluate_digest_contract(
            payload, sub or subscription(), selected, {EVIDENCE}, profile,
        )

    def test_failure_subtype_is_allowlisted(self):
        with self.assertRaises(ValueError):
            ProviderAdapterError(
                "INVALID_RESPONSE", subtype="raw-provider-detail",
            )

    def test_valid_structured_output_uses_safe_request_and_shared_contract(self):
        provider, transport = provider_for(response())

        payload, selected, profile = self.synthesize(provider)

        self.assertTrue(self.contract(payload, selected, profile).satisfied)
        self.assertEqual(provider.provider_identity, "vertex")
        self.assertEqual(provider.model_identity, MODEL)
        self.assertEqual(provider.last_model_identity, "vertex-sonnet-4.6")
        call = transport.calls[0]
        self.assertEqual(call["url"], ENDPOINT + "/completions")
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {KEY}")
        request = json.loads(call["body"].decode("utf-8"))
        self.assertEqual(request["model"], MODEL)
        self.assertTrue(request["prompt"].endswith("ASSISTANT: {"))
        self.assertIn("string value must be single-line", request["prompt"])
        self.assertIn("escaped \\n sequences", request["prompt"])
        self.assertIn("exactly one physical line", request["prompt"])
        self.assertIn(EVIDENCE, request["prompt"])
        self.assertNotIn(subscription().user_id, request["prompt"])
        self.assertNotIn("raw-interaction-history", request["prompt"])
        self.assertNotIn(KEY, request["prompt"])

    def test_chat_mode_requests_strict_tool_contract(self):
        provider, transport = provider_for(
            response(chat=True), mode="chat-completions",
        )

        payload, selected, profile = self.synthesize(provider)

        self.assertTrue(self.contract(payload, selected, profile).satisfied)
        request = json.loads(transport.calls[0]["body"].decode("utf-8"))
        self.assertNotIn("response_format", request)
        self.assertEqual(request["tool_choice"], {
            "type": "function",
            "function": {"name": "submit_digest_candidate"},
        })
        self.assertEqual(request["tools"], [{
            "type": "function",
            "function": {
                "name": "submit_digest_candidate",
                "description": "Submit the final Digest synthesis candidate",
                "strict": True,
                "parameters": VERTEX_TOOL_CANDIDATE_SCHEMA,
            },
        }])
        wire_item_schema = request["tools"][0]["function"]["parameters"][
            "properties"
        ]
        self.assertNotIn(
            "items", request["tools"][0]["function"]["parameters"][
                "properties"
            ],
        )
        self.assertEqual(set(wire_item_schema), {
            "summary", "candidate_id", "content_identity", "content",
            "recommendation_reason", "source_ref_id",
        })
        prompt = request["messages"][0]["content"]
        self.assertNotIn("first character has already been emitted", prompt)
        self.assertNotIn("ASSISTANT: {", prompt)
        self.assertIn("exactly six top-level string fields", prompt)
        self.assertIn("no nested objects or arrays", prompt)
        self.assertIn("provided rank-1 candidate", prompt)
        self.assertIn(selected[0].candidate.candidate_id, prompt)
        self.assertNotIn(selected[1].candidate.candidate_id, prompt)
        self.assertEqual(
            provider.describe_attempt(
                subscription(), selected, "2026-08-23", profile,
            )["structured_output_mechanism"],
            "strict_flat_scalar_tool_requested_prompt_reinforced",
        )
        self.assertEqual(
            provider.describe_attempt(
                subscription(), selected, "2026-08-23", profile,
            )["temperature"],
            0,
        )
        self.assertEqual(
            provider.describe_attempt(
                subscription(), selected, "2026-08-23", profile,
            )["candidate_count"],
            1,
        )
        self.assertEqual(provider.last_attempt["payload_source"], "tool_arguments")
        self.assertEqual(provider.last_attempt["choice_count"], 1)
        self.assertEqual(provider.last_attempt["message_type"], "object")
        self.assertEqual(provider.last_attempt["content_type"], "null")
        self.assertEqual(provider.last_attempt["tool_call_count"], 1)
        self.assertTrue(provider.last_attempt["function_name_match"])
        self.assertEqual(provider.last_attempt["arguments_type"], "string")
        self.assertEqual(provider.last_attempt["payload_top_type"], "object")
        self.assertEqual(provider.last_attempt["payload_items_type"], "string")
        self.assertEqual(
            provider.last_attempt["payload_selected_source_refs_type"],
            "string",
        )
        self.assertEqual(
            payload["items"][0]["source_ref_ids"], ["S1"],
        )

    def test_chat_schema_failure_diagnostics_use_flat_wire_item_shape(self):
        candidate = tool_candidate()
        candidate["source_ref_id"] = ["wrong-shape"]
        provider, _transport = provider_for(
            response(candidate, chat=True), mode="chat-completions",
        )

        with self.assertRaises(ProviderAdapterError):
            self.synthesize(provider)

        self.assertTrue(provider.last_error["diagnostics"]["exact_item_shapes"])
        self.assertEqual(
            provider.last_attempt["schema_mismatch_rule"],
            "ITEM_STRING_TYPE",
        )

    def test_chat_mode_requires_the_declared_tool_call(self):
        provider, _transport = provider_for(
            chat_envelope(
                content=json.dumps(structured_candidate()),
                include_tool=False, finish_reason="stop",
            ),
            mode="chat-completions",
        )

        with self.assertRaises(ProviderAdapterError) as caught:
            self.synthesize(provider)

        self.assertEqual(caught.exception.subtype, "ENVELOPE_EXTRACTION")
        self.assertEqual(
            provider.last_attempt["envelope_error"], "MISSING_TOOL_CALL",
        )

    def test_content_and_tool_call_ambiguity_is_rejected(self):
        provider, _transport = provider_for(
            chat_envelope(
                content=json.dumps(structured_candidate()),
            ), mode="chat-completions",
        )

        with self.assertRaises(ProviderAdapterError) as caught:
            self.synthesize(provider)

        self.assertEqual(caught.exception.subtype, "ENVELOPE_EXTRACTION")
        self.assertEqual(
            provider.last_attempt["envelope_error"],
            "CONTENT_TOOL_AMBIGUITY",
        )

    def test_missing_or_wrong_arguments_are_envelope_failures(self):
        cases = (
            (chat_envelope(include_arguments=False), "MISSING_ARGUMENTS"),
            (chat_envelope(arguments={"safe": "shape"}), "ARGUMENTS_TYPE"),
        )
        for envelope, safe_error in cases:
            with self.subTest(safe_error=safe_error):
                provider, _transport = provider_for(
                    envelope, mode="chat-completions",
                )
                with self.assertRaises(ProviderAdapterError) as caught:
                    self.synthesize(provider)
                self.assertEqual(
                    caught.exception.subtype, "ENVELOPE_EXTRACTION",
                )
                self.assertEqual(
                    provider.last_attempt["envelope_error"], safe_error,
                )

    def test_wrong_tool_name_cannot_bypass_envelope_validation(self):
        provider, _transport = provider_for(
            chat_envelope(function_name="synthetic_other_tool"),
            mode="chat-completions",
        )

        with self.assertRaises(ProviderAdapterError) as caught:
            self.synthesize(provider)

        self.assertEqual(caught.exception.subtype, "ENVELOPE_EXTRACTION")
        self.assertEqual(
            provider.last_attempt["envelope_error"], "TOOL_NAME_MISMATCH",
        )

    def test_malformed_json_and_extra_prose_fail_closed(self):
        encoded = json.dumps(structured_candidate())
        for output in (
            "not-json", "prefix " + encoded,
            "```json\n" + encoded + "\n```",
        ):
            with self.subTest(output=output[:10]):
                provider, _transport = provider_for(response(text=output))
                with self.assertRaises(ProviderAdapterError) as caught:
                    self.synthesize(provider)
                self.assertEqual(caught.exception.code, "INVALID_RESPONSE")

    def test_harmless_json_whitespace_is_accepted(self):
        encoded = json.dumps(structured_candidate(), ensure_ascii=False, indent=2)
        provider, _transport = provider_for(response(text=" \n\t" + encoded + "\r\n "))
        payload, selected, profile = self.synthesize(provider)
        self.assertTrue(self.contract(payload, selected, profile).satisfied)
        self.assertTrue(provider.last_attempt["json_parse_succeeded"])
        self.assertTrue(provider.last_attempt["schema_validation_succeeded"])

    def test_parse_and_schema_failures_have_exact_safe_subtypes(self):
        cases = (
            ('{"summary":"truncated"', "JSON_PARSE"),
            (json.dumps({"summary": "safe", "items": []}), "SCHEMA_MISMATCH"),
            ("prose " + json.dumps(structured_candidate()), "NON_JSON"),
        )
        for output, subtype in cases:
            with self.subTest(subtype=subtype):
                provider, _transport = provider_for(response(text=output))
                with self.assertRaises(ProviderAdapterError) as caught:
                    self.synthesize(provider)
                self.assertEqual(caught.exception.subtype, subtype)
                self.assertEqual(provider.last_error["subtype"], subtype)
                serialized = json.dumps(provider.last_attempt)
                self.assertNotIn(output, serialized)

    def test_json_parse_lexical_subtypes_are_allowlisted(self):
        cases = (
            ('{"summary":"safe" "items":[],"selected_source_refs":[]}',
             "EXPECTING_COMMA"),
            ('{"summary":"unterminated',
             "UNTERMINATED_STRING"),
            ('{"summary":"invalid\\q","items":[],"selected_source_refs":[]}',
             "INVALID_ESCAPE"),
            ('{"summary":"safe",items:[],"selected_source_refs":[]}',
             "EXPECTING_PROPERTY_NAME"),
            ('{"summary":"safe","items":[],"selected_source_refs":[]}{}',
             "EXTRA_DATA"),
            ('{"summary":,"items":[],"selected_source_refs":[]}',
             "OTHER_JSON_SYNTAX"),
        )
        for output, lexical_subtype in cases:
            with self.subTest(lexical_subtype=lexical_subtype):
                provider, _transport = provider_for(response(text=output))
                with self.assertRaises(ProviderAdapterError) as caught:
                    self.synthesize(provider)
                self.assertEqual(caught.exception.subtype, "JSON_PARSE")
                self.assertEqual(
                    provider.last_attempt["json_lexical_subtype"],
                    lexical_subtype,
                )
                self.assertEqual(
                    provider.last_error["diagnostics"][
                        "json_lexical_subtype"
                    ],
                    lexical_subtype,
                )
                self.assertNotIn(output, json.dumps(provider.last_attempt))

    def test_real_failure_safe_shape_reports_expecting_comma(self):
        # Mirrors durable facts from the browser failure without retaining any
        # real model/search text: one physical line, complete object markers,
        # normal response envelope, and an interior error near column 1416.
        output = (
            '{"summary":"' + ("中" * 1400)
            + '" "items":[],"selected_source_refs":[]}'
        )
        provider, _transport = provider_for(response(text=output))

        with self.assertRaises(ProviderAdapterError):
            self.synthesize(provider)

        self.assertEqual(
            provider.last_attempt["json_lexical_subtype"],
            "EXPECTING_COMMA",
        )
        self.assertEqual(provider.last_attempt["parse_error_line"], 1)
        self.assertGreaterEqual(
            provider.last_attempt["parse_error_column"], 1400,
        )
        self.assertTrue(provider.last_attempt["starts_with_object"])
        self.assertTrue(provider.last_attempt["ends_with_object"])
        self.assertNotIn(output, json.dumps(provider.last_attempt))

    def test_schema_failure_diagnostics_only_report_bounded_shape(self):
        candidate = structured_candidate()
        candidate["unexpected-secret-token-key"] = "must-not-escape"
        provider, _transport = provider_for(response(candidate))
        with self.assertRaises(ProviderAdapterError):
            self.synthesize(provider)
        diagnostics = provider.last_error["diagnostics"]
        self.assertFalse(diagnostics["exact_top_level"])
        self.assertTrue(diagnostics["exact_item_shapes"])
        rendered = json.dumps(diagnostics)
        self.assertNotIn("unexpected-secret", rendered)
        self.assertNotIn("must-not-escape", rendered)

    def test_schema_mismatch_reports_safe_exact_rule_without_content(self):
        cases = (
            ("ITEM_STRING_CONTROL", "content", "line one\nline two"),
            ("ITEM_STRING_TOO_LONG", "recommendation_reason", "理" * 501),
        )
        for rule, field, value in cases:
            with self.subTest(rule=rule):
                candidate = structured_candidate()
                candidate["items"][0][field] = value
                provider, _transport = provider_for(response(candidate))

                with self.assertRaises(ProviderAdapterError):
                    self.synthesize(provider)

                self.assertEqual(
                    provider.last_attempt["schema_mismatch_rule"], rule,
                )
                self.assertEqual(
                    provider.last_attempt["schema_mismatch_field"], field,
                )
                self.assertEqual(
                    provider.last_error["diagnostics"][
                        "schema_mismatch_rule"
                    ],
                    rule,
                )
                self.assertNotIn(value, json.dumps(provider.last_attempt))

    def test_real_items_object_shape_reports_items_type(self):
        candidate = tool_candidate()
        candidate["candidate_id"] = {
            "synthetic": "shape-only fixture; not real model content",
        }
        provider, _transport = provider_for(
            response(candidate, chat=True), mode="chat-completions",
        )

        with self.assertRaises(ProviderAdapterError):
            self.synthesize(provider)

        self.assertEqual(
            provider.last_attempt["schema_mismatch_rule"], "ITEM_STRING_TYPE",
        )
        self.assertEqual(
            provider.last_attempt["schema_mismatch_field"], "candidate_id",
        )
        self.assertEqual(provider.last_attempt["payload_items_type"], "object")
        self.assertNotIn("shape-only fixture", json.dumps(provider.last_attempt))

    def test_too_long_output_reaches_deterministic_contract(self):
        candidate = structured_candidate()
        candidate["items"][0]["content"] = "超" * 700
        provider, _transport = provider_for(response(candidate))
        payload, selected, profile = self.synthesize(provider)
        result = self.contract(payload, selected, profile)
        self.assertFalse(result.satisfied)
        self.assertIn("max_chars_exceeded", result.violations)
        self.assertEqual(result.failure_subtype, "too_long")
        self.assertEqual(len(_transport.calls), 1)

    def test_invalid_source_ref_reaches_deterministic_contract(self):
        candidate = structured_candidate()
        candidate["items"][0]["source_ref_ids"] = ["S99"]
        provider, _transport = provider_for(response(candidate))
        payload, selected, profile = self.synthesize(provider)
        result = self.contract(payload, selected, profile)
        self.assertFalse(result.satisfied)
        self.assertTrue({
            "missing_source_ref", "orphan_source_ref",
        } & set(result.violations))
        self.assertEqual(result.failure_subtype, "invalid_source_ref")
        self.assertEqual(len(_transport.calls), 1)

    def test_duplicate_item_reaches_deterministic_contract(self):
        candidate = structured_candidate()
        candidate["items"] = [
            dict(candidate["items"][0]), dict(candidate["items"][0]),
        ]
        candidate["selected_source_refs"] = [
            candidate["selected_source_refs"][0],
        ]
        provider, _transport = provider_for(response(candidate))
        payload, selected, profile = self.synthesize(provider)
        result = self.contract(payload, selected, profile)
        self.assertFalse(result.satisfied)
        self.assertIn("duplicate_item", result.violations)
        self.assertEqual(result.failure_subtype, "duplicate_item")
        self.assertEqual(len(_transport.calls), 1)

    def test_unsupported_item_without_source_fails_contract(self):
        candidate = structured_candidate()
        candidate["items"][0]["source_ref_ids"] = []
        provider, _transport = provider_for(response(candidate))
        payload, selected, profile = self.synthesize(provider)
        result = self.contract(payload, selected, profile)
        self.assertFalse(result.satisfied)
        self.assertIn("item_source_required", result.violations)

    def test_timeout_is_safe_retryable_and_not_retried_by_adapter(self):
        for code in ("TIMEOUT", "NETWORK_ERROR"):
            with self.subTest(code=code):
                provider, transport = provider_for(
                    error=ProviderAdapterError(code),
                )
                with self.assertRaises(ProviderAdapterError) as caught:
                    self.synthesize(provider)
                self.assertEqual(caught.exception.code, code)
                self.assertTrue(caught.exception.retryable)
                self.assertEqual(len(transport.calls), 1)

    def test_missing_configuration_is_safe_and_does_not_dispatch(self):
        transport = FakeVertexTransport(response())
        provider = VertexDigestProvider(transport=transport, environ={})
        with self.assertRaises(ProviderAdapterError) as caught:
            self.synthesize(provider)
        self.assertEqual(caught.exception.code, "CONFIGURATION_ERROR")
        self.assertEqual(transport.calls, [])

    def test_auth_failure_and_rate_limit_are_classified(self):
        cases = (
            (VertexHTTPResponse(401, {}, b""), "AUTH_FAILED", None),
            (VertexHTTPResponse(403, {}, b""), "AUTH_FAILED", None),
            (VertexHTTPResponse(429, {"Retry-After": "9"}, b""),
             "RATE_LIMITED", 9),
        )
        for http_response, code, retry_after in cases:
            with self.subTest(code=code, status=http_response.status):
                provider, _transport = provider_for(http_response)
                with self.assertRaises(ProviderAdapterError) as caught:
                    self.synthesize(provider)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(
                    caught.exception.retry_after_seconds, retry_after,
                )

    def test_refusal_and_empty_output_are_distinct(self):
        refusal = VertexHTTPResponse(200, {}, json.dumps({
            "choices": [{"text": "", "finish_reason": "content_filter"}],
        }).encode())
        empty = VertexHTTPResponse(200, {}, json.dumps({
            "choices": [{"text": "", "finish_reason": "stop"}],
        }).encode())
        for http_response, code in (
            (refusal, "MODEL_REFUSAL"), (empty, "EMPTY_OUTPUT"),
        ):
            with self.subTest(code=code):
                provider, _transport = provider_for(http_response)
                with self.assertRaises(ProviderAdapterError) as caught:
                    self.synthesize(provider)
                self.assertEqual(caught.exception.code, code)

    def test_secret_and_raw_response_are_absent_from_safe_state(self):
        provider, _transport = provider_for(response())
        payload, _selected, _profile = self.synthesize(provider)
        serialized = json.dumps({
            "calls": provider.calls, "last_error": provider.last_error,
            "last_model_identity": provider.last_model_identity,
            "payload": payload,
        }, ensure_ascii=False)
        self.assertNotIn(KEY, serialized)
        self.assertNotIn("response-must-not-persist", serialized)
        self.assertNotIn("must-not-cross-adapter", serialized)

    def test_fake_and_vertex_candidates_share_downstream_contract(self):
        selected = ranked_candidates()
        sub = subscription()
        profile = project_profile(
            InterestProfile.empty(sub.user_id, NOW), sub,
        )
        fake = FakeDigestProvider().synthesize(
            sub, selected, "2026-08-23", "4" * 32, profile,
        )
        vertex, _transport = provider_for(response())
        real = vertex.synthesize(
            sub, selected, "2026-08-23", "5" * 32, profile,
        )
        for name, payload in (("fake", fake), ("vertex", real)):
            with self.subTest(provider=name):
                result = evaluate_digest_contract(
                    payload, sub, selected, {EVIDENCE}, profile,
                )
                self.assertTrue(result.satisfied, result.violations)


class VertexWorkflowTests(unittest.TestCase):
    @staticmethod
    def rows():
        return [{
            "url": "https://example.test/agent",
            "title": "Agent Runtime 发布",
            "snippet": "Agent Runtime 与开发工具更新。",
            "topic_tags": ["AI 行业动态", "Agent"],
        }]

    def make(self, root, provider):
        ids = IdFactory()
        repository = SQLiteDigestRepository(os.path.join(root, "digest.db"))
        subscription_value = SubscriptionService(
            repository, id_factory=ids, clock=lambda: NOW,
        ).create_from_natural_language(
            "a" * 32,
            "帮我订阅 AI 行业动态，每天一份，600 字以内，"
            "最多 1 条，重点关注 Agent。",
        )
        workflow = DigestGenerationWorkflow(
            repository, FakeSearchClient(self.rows()), provider,
            os.path.join(root, "workspace"), os.path.join(root, "audit"),
            id_factory=ids, clock=lambda: NOW,
        )
        return repository, subscription_value, workflow

    def test_non_allowlisted_json_lexical_detail_is_not_persistable(self):
        self.assertEqual(
            DigestGenerationWorkflow._safe_attempt_metadata(
                {"json_lexical_subtype": "raw-model-detail"},
                {"json_lexical_subtype"},
            ),
            {},
        )

    def test_provider_error_becomes_authoritative_incomplete(self):
        provider, _transport = provider_for(
            error=ProviderAdapterError("TIMEOUT"),
        )
        with tempfile.TemporaryDirectory() as root:
            repository, sub, workflow = self.make(root, provider)
            outcome = workflow.run(sub.subscription_id, "2026-08-23")
            self.assertEqual((outcome.status, outcome.reason), (
                "incomplete", "TIMEOUT",
            ))
            self.assertIsNone(outcome.digest_id)
            self.assertEqual(outcome.harness_result["artifact_ids"], [])
            self.assertIsNone(
                repository.get_digest_run(outcome.digest_run_id).digest_id,
            )
            attempts = repository.list_generation_attempts(
                outcome.digest_run_id,
            )
            self.assertEqual(len(attempts), 2)
            self.assertEqual(
                tuple(item.failure_subtype for item in attempts),
                ("MODEL_TIMEOUT", "MODEL_TIMEOUT"),
            )
            self.assertTrue(all(
                "raw" not in json.dumps(item.response_metadata)
                for item in attempts
            ))

    def test_malformed_model_json_attempts_persist_safe_diagnostics_only(self):
        malformed = '{"summary":"safe","items":['
        provider, _transport = provider_for(response(text=malformed))
        with tempfile.TemporaryDirectory() as root:
            repository, sub, workflow = self.make(root, provider)
            outcome = workflow.run(sub.subscription_id, "malformed-json")
            attempts = repository.list_generation_attempts(
                outcome.digest_run_id,
            )

            self.assertEqual(
                (outcome.status, outcome.failure_stage,
                 outcome.failure_code, outcome.digest_id),
                ("incomplete", "generation", "generation_json_parse", None),
            )
            self.assertEqual(len(attempts), 2)
            for attempt in attempts:
                self.assertEqual(attempt.failure_subtype, "JSON_PARSE")
                self.assertEqual(attempt.response_metadata["http_status"], 200)
                self.assertFalse(
                    attempt.response_metadata["json_parse_succeeded"],
                )
                rendered = json.dumps(attempt.response_metadata)
                self.assertNotIn(malformed, rendered)
                self.assertNotIn("response-must-not-persist", rendered)

    def test_json_lexical_subtype_persists_without_raw_output(self):
        malformed = '{"summary":"safe" "items":[],"selected_source_refs":[]}'
        provider, _transport = provider_for(response(text=malformed))
        with tempfile.TemporaryDirectory() as root:
            repository, sub, workflow = self.make(root, provider)
            outcome = workflow.run(sub.subscription_id, "lexical-json")
            attempts = repository.list_generation_attempts(
                outcome.digest_run_id,
            )

            self.assertEqual(len(attempts), 2)
            self.assertTrue(all(
                item.response_metadata.get("json_lexical_subtype")
                == "EXPECTING_COMMA" for item in attempts
            ))
            self.assertNotIn(malformed, json.dumps([
                item.response_metadata for item in attempts
            ]))
            reopened = SQLiteDigestRepository(os.path.join(root, "digest.db"))
            reopened_attempts = reopened.list_generation_attempts(
                outcome.digest_run_id,
            )
            self.assertEqual(
                tuple(item.response_metadata["json_lexical_subtype"]
                      for item in reopened_attempts),
                ("EXPECTING_COMMA", "EXPECTING_COMMA"),
            )

    def test_schema_mismatch_rule_persists_without_candidate(self):
        candidate = structured_candidate()
        candidate["items"][0]["content"] = "safe\nsecond-line"
        provider, _transport = provider_for(response(candidate))
        with tempfile.TemporaryDirectory() as root:
            repository, sub, workflow = self.make(root, provider)
            outcome = workflow.run(sub.subscription_id, "schema-rule")
            reopened = SQLiteDigestRepository(os.path.join(root, "digest.db"))
            attempts = reopened.list_generation_attempts(outcome.digest_run_id)

            self.assertEqual(len(attempts), 2)
            self.assertTrue(all(
                item.response_metadata.get("schema_mismatch_rule")
                == "ITEM_STRING_CONTROL" for item in attempts
            ))
            persisted = json.dumps([
                item.response_metadata for item in attempts
            ])
            self.assertNotIn("second-line", persisted)


if __name__ == "__main__":
    unittest.main()
