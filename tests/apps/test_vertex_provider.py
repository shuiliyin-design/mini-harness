import json
import os
import tempfile
import unittest

from apps.digest_agent.adapters.provider import (
    LLM_API_KEY, LLM_API_MODE, LLM_ENDPOINT, LLM_MODEL,
    FakeDigestProvider, ProviderAdapterError, VertexDigestProvider,
    VertexHTTPResponse,
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


NOW = "2026-08-23T12:00:00Z"
EVIDENCE = "e" * 32
KEY = "vertex-fixture-credential-123456"
ENDPOINT = "https://vertex-gateway.example.test/v1"
MODEL = "sonnet-4.6"


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


def response(candidate=None, *, status=200, headers=None, text=None,
             model_version="vertex-sonnet-4.6"):
    if text is None:
        text = json.dumps(
            candidate or structured_candidate(), ensure_ascii=False,
            separators=(",", ":"),
        )
    payload = {
        "id": "response-must-not-persist",
        "model": model_version,
        "choices": [{"text": text, "finish_reason": "stop"}],
        "raw_provider_field": "must-not-cross-adapter",
    }
    return VertexHTTPResponse(
        status, headers or {},
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )


def provider_for(http_response=None, error=None, **kwargs):
    transport = FakeVertexTransport(http_response, error)
    environment = {
        LLM_API_KEY: KEY, LLM_API_MODE: "completions",
        LLM_ENDPOINT: ENDPOINT, LLM_MODEL: MODEL,
    }
    provider = VertexDigestProvider(
        transport=transport, environ=environment, **kwargs,
    )
    return provider, transport


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


if __name__ == "__main__":
    unittest.main()
