from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock
from urllib import error as urllib_error
from urllib.parse import parse_qs, urlsplit

from apps.digest_agent.adapters.provider import FakeDigestProvider
from apps.digest_agent.adapters import search
from apps.digest_agent.adapters.search import (
    BRAVE_SEARCH_API_KEY, BRAVE_SEARCH_ENDPOINT, BraveSearchClient,
    SearchAdapterError, SearchHTTPResponse, UrllibSearchTransport,
    normalize_search_query,
    search_query_identity,
)
from apps.digest_agent.adapters.sqlite import SQLiteDigestRepository
from apps.digest_agent.services import SubscriptionService
from apps.digest_agent.workflows import DigestGenerationWorkflow
from mini_harness_core.evidence import EvidenceStore
from tools import brave_search_smoke


NOW = "2026-08-23T12:00:00Z"
KEY = "brave-fixture-credential-123456"
QUERY = "AI agent engineering latest developments"
RAW_MARKER = "raw-provider-field-must-not-persist-7f4315"


class IdFactory:
    def __init__(self):
        self.value = 500

    def __call__(self):
        value = f"{self.value:032x}"
        self.value += 1
        return value


class FakeHTTPTransport:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def get(self, url, headers, timeout, maximum_bytes):
        self.calls.append({
            "url": url, "headers": dict(headers), "timeout": timeout,
            "maximum_bytes": maximum_bytes,
        })
        if self.error is not None:
            raise self.error
        return self.response


def response(results, status=200, headers=None, extra=None):
    payload = {"web": {"results": results}}
    if extra:
        payload.update(extra)
    return SearchHTTPResponse(
        status, headers or {},
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )


def result(url="https://example.test/agent?utm_source=brave",
           title="Agent engineering update"):
    return {
        "title": title,
        "url": url,
        "description": "Agent tooling and model release details.",
        "age": "2026-08-23T10:00:00Z",
        "profile": {"long_name": "must be ignored"},
        "unknown_provider_field": RAW_MARKER,
    }


def client_for(http_response=None, error=None, **kwargs):
    transport = FakeHTTPTransport(http_response, error)
    client = BraveSearchClient(
        transport=transport, environ={BRAVE_SEARCH_API_KEY: KEY}, **kwargs,
    )
    return client, transport


class BraveAdapterTests(unittest.TestCase):
    def test_success_normalizes_and_uses_fixed_request_boundary(self):
        client, transport = client_for(response([
            result(),
            result("https://example.test/agent", "duplicate canonical URL"),
            result("https://NEWS.example.test/model", "Model release"),
        ], extra={"query": {"raw": "ignored"}}))

        safe = client.call_tool(
            "web_search", {"query": QUERY, "max_results": 3},
        )

        self.assertEqual((safe["provider"], safe["result_count"]), ("brave", 2))
        self.assertEqual(safe["query_identity"], search_query_identity(QUERY))
        self.assertEqual(safe["results"][0]["url"], "https://example.test/agent")
        self.assertEqual(
            set(safe["results"][0]),
            {"source_id", "title", "url", "snippet", "topic_tags",
             "published_at"},
        )
        self.assertNotIn("profile", json.dumps(safe))
        call = transport.calls[0]
        self.assertEqual(urlsplit(call["url"])._replace(query="").geturl(),
                         BRAVE_SEARCH_ENDPOINT)
        self.assertEqual(parse_qs(urlsplit(call["url"]).query), {
            "q": [QUERY], "count": ["3"], "safesearch": ["moderate"],
        })
        self.assertEqual(call["headers"]["X-Subscription-Token"], KEY)
        self.assertEqual(set(call["headers"]), {
            "Accept", "User-Agent", "X-Subscription-Token",
        })

    def test_source_and_observation_identities_are_deterministic(self):
        rows = [result(), result("https://example.test/model", "Model")]
        first, _ = client_for(response(rows))
        second, _ = client_for(response(list(reversed(rows))))
        left = first.call_tool("web_search", {"query": QUERY, "max_results": 2})
        right = second.call_tool("web_search", {"query": QUERY, "max_results": 2})
        self.assertEqual(
            {item["url"]: item["source_id"] for item in left["results"]},
            {item["url"]: item["source_id"] for item in right["results"]},
        )
        repeated, _ = client_for(response(rows))
        again = repeated.call_tool(
            "web_search", {"query": QUERY, "max_results": 2},
        )
        self.assertEqual(left["observation_identity"],
                         again["observation_identity"])

    def test_truncated_snippet_is_stable_across_application_revalidation(self):
        boundary = result()
        boundary["description"] = "a" * 319 + " " + "b"
        client, _transport = client_for(response([boundary]))
        safe = client.call_tool(
            "web_search", {"query": QUERY, "max_results": 1},
        )

        validated = search.validate_safe_search_result(safe, QUERY, 1)

        self.assertEqual(validated, safe)
        self.assertFalse(validated["results"][0]["snippet"].endswith(" "))

    def test_candidate_count_is_bounded(self):
        rows = [result(f"https://example.test/{index}", f"Item {index}")
                for index in range(5)]
        rows[0]["title"] = "T" * 300
        rows[0]["description"] = "D" * 500
        client, _ = client_for(response(rows))
        safe = client.call_tool(
            "web_search", {"query": QUERY, "max_results": 2},
        )
        self.assertEqual(safe["result_count"], 2)
        self.assertEqual(len(safe["results"][0]["title"]), 240)
        self.assertEqual(len(safe["results"][0]["snippet"]), 320)

    def test_missing_credential_is_configuration_error(self):
        client = BraveSearchClient(
            transport=FakeHTTPTransport(response([result()])), environ={},
        )
        with self.assertRaises(SearchAdapterError) as caught:
            client.call_tool(
                "web_search", {"query": QUERY, "max_results": 2},
            )
        self.assertEqual(str(caught.exception), "CONFIGURATION_ERROR")
        self.assertFalse(caught.exception.retryable)

    def test_timeout_and_network_errors_are_retryable_but_not_retried(self):
        for code in ("TIMEOUT", "NETWORK_ERROR"):
            with self.subTest(code=code):
                client, transport = client_for(
                    error=SearchAdapterError(code),
                )
                with self.assertRaises(SearchAdapterError) as caught:
                    client.call_tool(
                        "web_search", {"query": QUERY, "max_results": 2},
                    )
                self.assertEqual(caught.exception.code, code)
                self.assertTrue(caught.exception.retryable)
                self.assertEqual(len(transport.calls), 1)

    def test_stdlib_transport_maps_timeout_and_network_without_details(self):
        class RaisingOpener:
            def __init__(self, error):
                self.error = error

            def open(self, _request, timeout):
                raise self.error

        cases = (
            (socket.timeout("raw timeout detail"), "TIMEOUT"),
            (urllib_error.URLError("raw network detail"), "NETWORK_ERROR"),
        )
        for error, code in cases:
            with self.subTest(code=code):
                transport = UrllibSearchTransport(RaisingOpener(error))
                with self.assertRaises(SearchAdapterError) as caught:
                    transport.get(
                        BRAVE_SEARCH_ENDPOINT, {}, 1.0, 1024,
                    )
                self.assertEqual(str(caught.exception), code)
                self.assertNotIn("raw", str(caught.exception))

    def test_rate_limit_keeps_only_bounded_retry_after(self):
        client, transport = client_for(SearchHTTPResponse(
            429, {"Retry-After": "99999", "X-Raw": "ignored"}, b"",
        ))
        with self.assertRaises(SearchAdapterError) as caught:
            client.call_tool(
                "web_search", {"query": QUERY, "max_results": 2},
            )
        self.assertEqual(caught.exception.code, "RATE_LIMITED")
        self.assertEqual(caught.exception.retry_after_seconds, 3600)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(client.last_error["retry_after_seconds"], 3600)

    def test_unauthorized_and_forbidden_are_auth_failed(self):
        for status in (401, 403):
            with self.subTest(status=status):
                client, _ = client_for(SearchHTTPResponse(status, {}, b""))
                with self.assertRaises(SearchAdapterError) as caught:
                    client.call_tool(
                        "web_search", {"query": QUERY, "max_results": 2},
                    )
                self.assertEqual(caught.exception.code, "AUTH_FAILED")
                self.assertFalse(caught.exception.retryable)

    def test_malformed_and_oversized_responses_are_distinct(self):
        malformed, _ = client_for(SearchHTTPResponse(200, {}, b"not-json"))
        with self.assertRaises(SearchAdapterError) as caught:
            malformed.call_tool(
                "web_search", {"query": QUERY, "max_results": 2},
            )
        self.assertEqual(caught.exception.code, "INVALID_RESPONSE")

        oversized, _ = client_for(
            SearchHTTPResponse(200, {}, b"x" * 1025),
            maximum_response_bytes=1024,
        )
        with self.assertRaises(SearchAdapterError) as caught:
            oversized.call_tool(
                "web_search", {"query": QUERY, "max_results": 2},
            )
        self.assertEqual(caught.exception.code, "OVERSIZED_RESPONSE")

    def test_safe_diagnostics_distinguish_json_schema_and_normalization(self):
        schema, _ = client_for(SearchHTTPResponse(
            200, {"Content-Type": "application/json"},
            json.dumps({"type": "search", "mixed": {"main": []}}).encode(),
        ))
        with self.assertRaises(SearchAdapterError):
            schema.call_tool("web_search", {"query": QUERY, "max_results": 3})
        self.assertEqual(schema.last_error["layer"], "BRAVE_SCHEMA")
        self.assertEqual(schema.last_diagnostics, {
            "layer": "BRAVE_SCHEMA",
            "endpoint_identity": search.BRAVE_ENDPOINT_IDENTITY,
            "query_identity": search_query_identity(QUERY),
            "result_limit": 3,
            "timeout_seconds": 5.0,
            "request_header_names": (
                "Accept", "User-Agent", "X-Subscription-Token:SET",
            ),
            "http_status": 200,
            "content_type": "application/json",
            "response_bytes": len(json.dumps({
                "type": "search", "mixed": {"main": []},
            }).encode()),
            "response_sha256": hashlib.sha256(json.dumps({
                "type": "search", "mixed": {"main": []},
            }).encode()).hexdigest(),
            "top_level_json_keys": ("mixed", "type"),
            "other_top_level_key_count": 0,
            "web_object_present": False,
            "results_list_present": False,
            "raw_result_count": None,
            "normalized_error_code": "INVALID_RESPONSE",
        })

        normalization, _ = client_for(response([{
            "title": "missing URL", "description": "safe",
        }], headers={"Content-Type": "application/json"}))
        with self.assertRaises(SearchAdapterError):
            normalization.call_tool(
                "web_search", {"query": QUERY, "max_results": 3},
            )
        self.assertEqual(normalization.last_error["layer"], "NORMALIZATION")
        self.assertEqual(
            normalization.last_diagnostics["normalized_error_code"],
            "EMPTY_RESULTS",
        )

    def test_empty_results_is_explicit(self):
        client, _ = client_for(response([]))
        with self.assertRaises(SearchAdapterError) as caught:
            client.call_tool(
                "web_search", {"query": QUERY, "max_results": 2},
            )
        self.assertEqual(caught.exception.code, "EMPTY_RESULTS")

    def test_query_and_options_are_strictly_validated(self):
        invalid = [
            "bad\nquery", "x" * 401, " ".join(["word"] * 51),
            "use api_key=not-allowed",
        ]
        for query in invalid:
            with self.subTest(query=query[:20]), self.assertRaises(SearchAdapterError):
                normalize_search_query(query)
        client, transport = client_for(response([result()]))
        with self.assertRaises(SearchAdapterError):
            client.call_tool("web_search", {
                "query": QUERY, "max_results": 2,
                "endpoint": "https://attacker.test/",
            })
        self.assertEqual(transport.calls, [])
        for count in (True, 0, 21):
            with self.subTest(count=count), self.assertRaises(SearchAdapterError):
                client.call_tool(
                    "web_search", {"query": QUERY, "max_results": count},
                )

    def test_secret_is_absent_from_safe_state_and_errors(self):
        client, _ = client_for(response([result()]))
        safe = client.call_tool(
            "web_search", {"query": QUERY, "max_results": 2},
        )
        serialized = json.dumps({
            "safe": safe, "calls": client.calls,
            "last_safe_result": client.last_safe_result,
        })
        self.assertNotIn(KEY, serialized)
        self.assertNotIn(QUERY, serialized)


class BraveWorkflowBoundaryTests(unittest.TestCase):
    def make(self, root, transport, request=None):
        ids = IdFactory()
        repository = SQLiteDigestRepository(os.path.join(root, "digest.db"))
        subscription = SubscriptionService(
            repository, id_factory=ids, clock=lambda: NOW,
        ).create_from_natural_language(
            "a" * 32,
            request or (
                "帮我订阅 AI Agent engineering，每天一份，600 字以内，"
                "重点关注 Agent、模型发布和开发工具。"
            ),
        )
        search = BraveSearchClient(
            transport=transport,
            environ={BRAVE_SEARCH_API_KEY: KEY},
        )
        provider = FakeDigestProvider()
        workflow = DigestGenerationWorkflow(
            repository, search, provider, os.path.join(root, "workspace"),
            os.path.join(root, "audit"), id_factory=ids, clock=lambda: NOW,
        )
        return repository, subscription, search, provider, workflow

    def test_real_shaped_search_and_fake_provider_use_shared_evidence_path(self):
        with tempfile.TemporaryDirectory() as root:
            transport = FakeHTTPTransport(response([
                result(title="Agent engineering release"),
                result("https://example.test/model", "模型发布 engineering"),
            ]))
            repository, subscription, search, provider, workflow = self.make(
                root, transport,
            )
            outcome = workflow.run(subscription.subscription_id, "2026-08-23")
            self.assertEqual(outcome.status, "completed")
            digest = repository.get_digest(outcome.digest_id)
            self.assertLessEqual(
                digest.payload["character_count"], subscription.max_chars,
            )
            candidate_ids = {
                item["candidate_id"] for item in digest.payload["items"]
            }
            self.assertEqual(
                candidate_ids,
                {item["candidate_id"] for item in digest.payload["source_refs"]},
            )
            self.assertEqual(provider.calls[0]["candidate_ids"], [
                item["candidate_id"] for item in digest.payload["items"]
            ])
            self.assertEqual(search.last_safe_result["provider"], "brave")

            evidence = EvidenceStore(os.path.join(root, "audit", "evidence"))
            records = [evidence.load(path.stem)
                       for path in Path(evidence.directory).glob("*.json")]
            accepted = next(
                item for item in records
                if item["subject"]["kind"] == "search_candidate_set"
            )
            self.assertTrue(accepted["verification"]["accepted"])
            self.assertEqual(
                {ref["evidence_id"] for ref in digest.payload["source_refs"]},
                {accepted["evidence_id"]},
            )
            for path in Path(root).rglob("*"):
                if path.is_file():
                    self.assertNotIn(KEY.encode(), path.read_bytes(), path)
                    self.assertNotIn(RAW_MARKER.encode(), path.read_bytes(), path)

    def test_query_relevant_real_shape_gets_subscription_topic_provenance(self):
        with tempfile.TemporaryDirectory() as root:
            transport = FakeHTTPTransport(response([result(
                "https://example.test/state-of-agent-engineering",
                "State of Agent Engineering",
            )]))
            repository, subscription, _search, provider, workflow = self.make(
                root, transport,
                request=(
                    f"帮我订阅 {QUERY}，每天一份，600 字以内，"
                    "最多 1 条。"
                ),
            )

            outcome = workflow.run(subscription.subscription_id, "2026-08-23")

            self.assertEqual(outcome.status, "completed")
            digest = repository.get_digest(outcome.digest_id)
            self.assertIn(
                subscription.topic.casefold(),
                digest.payload["items"][0]["topic_tags"],
            )
            self.assertEqual(len(provider.calls), 1)

    def test_timeout_and_rate_limit_create_no_accepted_evidence_or_digest(self):
        for code in ("TIMEOUT", "RATE_LIMITED"):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as root:
                transport = FakeHTTPTransport(error=SearchAdapterError(code))
                repository, subscription, _search, provider, workflow = self.make(
                    root, transport,
                )
                outcome = workflow.run(subscription.subscription_id, "2026-08-23")
                self.assertEqual(outcome.status, "incomplete")
                self.assertEqual(outcome.reason, code)
                self.assertIsNone(outcome.digest_id)
                self.assertEqual(provider.calls, [])
                evidence = EvidenceStore(os.path.join(root, "audit", "evidence"))
                records = [evidence.load(path.stem)
                           for path in Path(evidence.directory).glob("*.json")]
                verification = next(
                    item for item in records
                    if item["subject"]["kind"] == "search_candidate_set"
                )
                self.assertFalse(verification["verification"]["accepted"])
                self.assertNotIn(
                    verification["evidence_id"],
                    outcome.harness_result["evidence_ids"],
                )


class BraveSmokeTests(unittest.TestCase):
    def test_incomplete_workflow_reports_safe_reason_without_digest_access(self):
        search, _transport = client_for(response([result(
            "https://example.test/unrelated",
            "Unrelated result",
        )]))
        output = io.StringIO()
        with mock.patch.dict(
            os.environ, {BRAVE_SEARCH_API_KEY: "fixture-present"}, clear=False,
        ), mock.patch.object(
            brave_search_smoke.BraveSearchClient, "from_environment",
            return_value=search,
        ), redirect_stdout(output):
            exit_code = brave_search_smoke.main()

        self.assertEqual(exit_code, 1)
        self.assertIn("digest_error=topic_focus_mismatch", output.getvalue())
        self.assertNotIn(KEY, output.getvalue())


if __name__ == "__main__":
    unittest.main()
