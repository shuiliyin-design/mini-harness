from dataclasses import asdict, fields
import copy
import os
import tempfile
import threading
import unittest

from apps.digest_agent.application import (
    ApplicationError, DigestApplication, DigestView, RunView,
)
from apps.digest_agent.adapters.delivery import FakeDeliveryAdapter
from apps.digest_agent.adapters.provider import (
    FakeDigestProvider, ProviderAdapterError,
)
from apps.digest_agent.adapters.search import FakeSearchClient, SearchAdapterError
from apps.digest_agent.adapters.sqlite import SQLiteDigestRepository
from apps.digest_agent.domain import InterestProfile, project_profile
from apps.digest_agent.repositories import DigestRunRecord
from apps.digest_agent.services import (
    DeliveryService, FeedbackService, SubscriptionService,
)
from apps.digest_agent.workflows import DigestGenerationWorkflow
from mini_harness_core.audit import AuditWriter


NOW = "2026-08-23T12:00:00Z"
USER = "a" * 32


class IdFactory:
    def __init__(self):
        self.value = 800

    def __call__(self):
        value = f"{self.value:032x}"
        self.value += 1
        return value


def rows():
    return [{
        "url": "https://example.test/agent",
        "title": "Agent Runtime 发布",
        "snippet": "Agent 工具新增教学模式。",
        "published_at": "2026-08-23T10:00:00Z",
        "topic_tags": ["AI 行业动态", "Agent"],
    }, {
        "url": "https://example.test/model",
        "title": "模型发布更新",
        "snippet": "一个新模型发布。",
        "published_at": "2026-08-23T09:00:00Z",
        "topic_tags": ["AI 行业动态", "模型发布"],
    }]


class FailFinishOnceRepository:
    def __init__(self, inner):
        self.inner = inner
        self.failed = False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def finish_digest_run(self, record, digest=None):
        if not self.failed:
            self.failed = True
            raise OSError("synthetic projection failure")
        return self.inner.finish_digest_run(record, digest)


class BlockingSearchClient(FakeSearchClient):
    def __init__(self, results):
        super().__init__(results)
        self.entered = threading.Event()
        self.release = threading.Event()

    def call_tool(self, name, arguments):
        self.entered.set()
        if not self.release.wait(5):
            raise AssertionError("blocking search was not released")
        return super().call_tool(name, arguments)


class FailingProvider:
    def __init__(self, code):
        self.code = code
        self.calls = []

    def synthesize(self, *arguments):
        self.calls.append(True)
        raise ProviderAdapterError(self.code)


class FailingSearchClient(FakeSearchClient):
    def __init__(self, code):
        super().__init__(rows())
        self.code = code

    def call_tool(self, name, arguments):
        self.calls.append(True)
        raise SearchAdapterError(self.code)


class ScriptedProvider:
    provider_identity = "vertex"

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.valid = FakeDigestProvider()
        self.last_attempt = None

    def synthesize(self, *arguments):
        self.calls.append(True)
        outcome = self.outcomes.pop(0)
        self.last_attempt = {
            "http_status": 200, "response_bytes": 120,
            "response_sha256": "a" * 64, "response_chars": 100,
            "content_sha256": "b" * 64, "finish_reason": "stop",
            "json_parse_succeeded": not isinstance(outcome, Exception),
            "schema_validation_succeeded": not isinstance(outcome, Exception),
            "duration_ms": 10, "raw": "must-not-persist",
        }
        if isinstance(outcome, Exception):
            raise outcome
        return self.valid.synthesize(*arguments)


class SchemaInvalidToolProvider:
    provider_identity = "vertex"

    def __init__(self):
        self.calls = []
        self.last_attempt = None

    def synthesize(self, *_arguments):
        self.calls.append(True)
        self.last_attempt = {
            "http_status": 200,
            "finish_reason": "tool_calls",
            "json_parse_succeeded": True,
            "schema_validation_succeeded": False,
            "schema_mismatch_rule": "ITEMS_TYPE",
            "schema_mismatch_field": "items",
            "payload_source": "tool_arguments",
            "payload_top_type": "object",
            "payload_items_type": "object",
            "raw": "private-provider-payload",
        }
        raise ProviderAdapterError(
            "INVALID_RESPONSE", subtype="SCHEMA_MISMATCH",
        )


class ContractMutatingProvider:
    provider_identity = "fake"

    def __init__(self, mode):
        self.mode = mode
        self.calls = []
        self.marker = "private-synthesis-candidate-marker"

    def synthesize(self, *arguments):
        self.calls.append(True)
        payload = FakeDigestProvider().synthesize(*arguments)
        if self.mode == "duplicate":
            first = copy.deepcopy(payload["items"][0])
            payload["items"] = [first, copy.deepcopy(first)]
            payload["source_refs"] = [payload["source_refs"][0]]
            payload["rendered_text"] = "\n".join(
                item["text"] for item in payload["items"]
            )
        elif self.mode == "private_overlong":
            payload["rendered_text"] += self.marker * 30
        payload["character_count"] = len(payload["rendered_text"])
        return payload


class DigestApplicationTests(unittest.TestCase):
    def make(self, root, provider=None, repository=None, search=None):
        ids = IdFactory()
        repository = repository or SQLiteDigestRepository(
            os.path.join(root, "digest.db"),
        )
        subscriptions = SubscriptionService(
            repository, id_factory=ids, clock=lambda: NOW,
        )
        search = search or FakeSearchClient(rows())
        provider = provider or FakeDigestProvider()
        workflow = DigestGenerationWorkflow(
            repository, search, provider, os.path.join(root, "workspace"),
            os.path.join(root, "audit"), id_factory=ids, clock=lambda: NOW,
        )
        delivery = FakeDeliveryAdapter()
        app = DigestApplication(
            repository, subscriptions, workflow,
            DeliveryService(repository, [delivery], clock=lambda: NOW),
            FeedbackService(repository, clock=lambda: NOW),
        )
        return app, repository, workflow, search, provider, delivery

    @staticmethod
    def create(app):
        return app.create_subscription(
            USER,
            "帮我订阅 AI 行业动态，每天一份，600 字以内，最多 2 条，"
            "重点关注 Agent、模型发布。",
        )

    def reserve_only(self, app, repository, workflow, subscription, key):
        domain = repository.get_subscription(subscription.subscription_id)
        projection = project_profile(
            InterestProfile.empty(USER, NOW), domain,
        )
        snapshot = asdict(domain)
        snapshot["focus_topics"] = list(domain.focus_topics)
        record = DigestRunRecord(
            workflow.id_factory(), domain.subscription_id, key,
            workflow.id_factory(), "reserved", None, None, None, None,
            profile_version=projection.profile_version,
            profile_projection_id=projection.projection_id,
            profile_projection=projection.as_dict(), idempotency_key=key,
            subscription_version=domain.version,
            subscription_snapshot=snapshot, updated_at=NOW,
        )
        return repository.reserve_digest_run(record)[0]

    def test_subscription_create_update_disable_enable_and_user_scope(self):
        with tempfile.TemporaryDirectory() as root:
            app, _repo, _workflow, *_ = self.make(root)
            created = self.create(app)
            updated = app.update_subscription(
                USER, created.subscription_id, created.version,
                topic="Agent Engineering", max_chars=700, max_items=1,
                focus_topics=("Agent",), delivery_preference="none",
                natural_language_request="订阅 Agent Engineering，每日摘要",
                cadence="daily", language="zh-CN",
            )
            self.assertEqual((updated.version, updated.max_chars), (2, 700))
            disabled = app.disable_subscription(
                USER, created.subscription_id, updated.version,
            )
            rejected = app.run_subscription(
                USER, created.subscription_id, "disabled-run",
            )
            self.assertEqual(
                (disabled.enabled, rejected.status, rejected.failure_reason),
                (False, "rejected", "subscription_disabled"),
            )
            enabled = app.enable_subscription(
                USER, created.subscription_id, disabled.version,
            )
            self.assertTrue(enabled.enabled)
            self.assertEqual(app.list_subscriptions(USER), (enabled,))
            with self.assertRaises(ApplicationError) as caught:
                app.get_subscription("b" * 32, created.subscription_id)
            self.assertEqual(caught.exception.code, "not_found")

    def test_old_digest_retains_subscription_snapshot_and_public_dto_is_sealed(self):
        with tempfile.TemporaryDirectory() as root:
            app, repository, _workflow, *_ = self.make(root)
            created = self.create(app)
            run = app.run_subscription(USER, created.subscription_id, "old")
            app.update_subscription(
                USER, created.subscription_id, created.version, max_chars=900,
            )
            digest = app.get_digest(USER, run.digest_id)
            stored_run = repository.get_digest_run(run.application_run_id)
            self.assertEqual(digest.subscription_version, 1)
            self.assertEqual(stored_run.subscription_snapshot["max_chars"], 600)
            forbidden = {
                "harness_result", "harness_run_id", "artifact_id",
                "evidence", "checkpoint", "audit",
            }
            self.assertTrue(forbidden.isdisjoint(
                {field.name for field in fields(RunView)}
                | {field.name for field in fields(DigestView)}
            ))

    def test_duplicate_idempotency_key_creates_one_logical_and_harness_run(self):
        with tempfile.TemporaryDirectory() as root:
            app, repository, _workflow, search, provider, _delivery = self.make(root)
            sub = self.create(app)
            first = app.run_subscription(USER, sub.subscription_id, "same")
            second = app.run_subscription(USER, sub.subscription_id, "same")
            self.assertEqual(first.application_run_id, second.application_run_id)
            self.assertTrue(second.reused)
            self.assertEqual((len(search.calls), len(provider.calls)), (1, 1))
            with repository.connect() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM digest_runs",
                ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_near_simultaneous_requests_share_one_active_application_run(self):
        with tempfile.TemporaryDirectory() as root:
            search = BlockingSearchClient(rows())
            app, repository, _workflow, _search, provider, _delivery = self.make(
                root, search=search,
            )
            sub = self.create(app)
            outcomes, failures = [], []

            def first_request():
                try:
                    outcomes.append(app.run_subscription(
                        USER, sub.subscription_id, "concurrent",
                    ))
                except Exception as error:
                    failures.append(error)

            worker = threading.Thread(target=first_request)
            worker.start()
            self.assertTrue(search.entered.wait(5))
            duplicate = app.run_subscription(
                USER, sub.subscription_id, "concurrent",
            )
            self.assertEqual(
                (duplicate.status, duplicate.failure_reason),
                ("running", "run_already_active"),
            )
            search.release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(
                duplicate.application_run_id, outcomes[0].application_run_id,
            )
            self.assertEqual((len(search.calls), len(provider.calls)), (1, 1))
            with repository.connect() as connection:
                runs = connection.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT harness_run_id) "
                    "FROM digest_runs",
                ).fetchone()
                digests = connection.execute(
                    "SELECT COUNT(*) FROM digests",
                ).fetchone()[0]
            self.assertEqual(tuple(runs), (1, 1))
            self.assertEqual(digests, 1)

    def test_unbound_reserved_recovery_has_no_duplicate_run(self):
        with tempfile.TemporaryDirectory() as root:
            app, repository, workflow, search, *_ = self.make(root)
            sub = self.create(app)
            reserved = self.reserve_only(app, repository, workflow, sub, "crash-a")
            active = app.run_subscription(USER, sub.subscription_id, "crash-a")
            self.assertEqual(
                (active.status, active.failure_reason, len(search.calls)),
                ("reserved", "run_already_active", 0),
            )
            recovered = app.recover_run(USER, reserved.digest_run_id)
            duplicate = app.run_subscription(USER, sub.subscription_id, "crash-a")
            self.assertEqual(recovered.status, "completed")
            self.assertEqual(recovered.application_run_id,
                             duplicate.application_run_id)
            self.assertEqual(len(search.calls), 1)

    def test_bound_without_harness_events_reuses_same_harness_identity(self):
        with tempfile.TemporaryDirectory() as root:
            app, repository, workflow, search, provider, _delivery = self.make(root)
            sub = self.create(app)
            reserved = self.reserve_only(app, repository, workflow, sub, "crash-b")
            bound = repository.bind_digest_run(
                reserved.digest_run_id, reserved.harness_run_id, NOW,
            )
            recovered = app.recover_run(USER, bound.digest_run_id)
            final = repository.get_digest_run(recovered.application_run_id)
            self.assertEqual(final.harness_run_id, bound.harness_run_id)
            self.assertEqual(recovered.status, "completed")

    def test_terminal_harness_result_repairs_only_application_projection(self):
        with tempfile.TemporaryDirectory() as root:
            inner = SQLiteDigestRepository(os.path.join(root, "digest.db"))
            repository = FailFinishOnceRepository(inner)
            app, _repository, _workflow, search, provider, _delivery = self.make(
                root, repository=repository,
            )
            sub = self.create(app)
            with self.assertRaises(OSError):
                app.run_subscription(USER, sub.subscription_id, "repair")
            with inner.connect() as connection:
                run_id = connection.execute(
                    "SELECT digest_run_id FROM digest_runs",
                ).fetchone()[0]
            record = inner.get_digest_run(run_id)
            repaired = app.recover_run(USER, record.digest_run_id)
            self.assertEqual(repaired.status, "completed")
            self.assertEqual((len(search.calls), len(provider.calls)), (1, 1))
            self.assertIsNotNone(inner.get_digest(repaired.digest_id))

    def test_harness_events_without_result_fail_closed_recovery_required(self):
        with tempfile.TemporaryDirectory() as root:
            app, repository, workflow, search, provider, _delivery = self.make(root)
            sub = self.create(app)
            reserved = self.reserve_only(app, repository, workflow, sub, "ambiguous")
            bound = repository.bind_digest_run(
                reserved.digest_run_id, reserved.harness_run_id, NOW,
            )
            AuditWriter(USER, bound.harness_run_id, workflow.audit_directory).append(
                "tool_requested", "harness", "mcp:search:web_search", "requested",
            )
            recovered = app.recover_run(USER, bound.digest_run_id)
            self.assertEqual(
                (recovered.status, recovered.failure_reason),
                ("recovery_required", "recovery_required"),
            )
            repeated = app.recover_run(USER, bound.digest_run_id)
            self.assertEqual(repeated.status, "recovery_required")
            self.assertEqual((len(search.calls), len(provider.calls)), (0, 0))

    def test_list_get_delivery_feedback_profile_and_full_product_e2e(self):
        with tempfile.TemporaryDirectory() as root:
            app, _repository, _workflow, _search, _provider, delivery = self.make(root)
            sub = self.create(app)
            first = app.run_subscription(USER, sub.subscription_id, "journey-1")
            digest = app.get_digest(USER, first.digest_id)
            self.assertEqual(app.list_digests(USER), (digest,))
            sent = app.deliver_digest(USER, digest.digest_id, "fake")
            duplicate = app.deliver_digest(USER, digest.digest_id, "fake")
            self.assertEqual(sent.delivery_id, duplicate.delivery_id)
            self.assertEqual(len(delivery.calls), 1)
            item = digest.content["items"][0]
            candidate_id = item["candidate_id"]
            feedback = app.record_feedback(
                USER, digest.digest_id, "liked", "like-1", item["item_id"],
            )
            repeated = app.record_feedback(
                USER, digest.digest_id, "liked", "like-1", item["item_id"],
            )
            self.assertTrue(feedback.applied)
            self.assertFalse(repeated.applied)
            self.assertEqual(app.get_profile(USER).version, 1)
            second = app.run_subscription(USER, sub.subscription_id, "journey-2")
            next_digest = app.get_digest(USER, second.digest_id)
            before = dict(
                (part["component"], part["value"])
                for part in item["score_breakdown"]
            )
            after_item = next(value for value in next_digest.content["items"]
                              if value["candidate_id"] == candidate_id)
            after = {part["component"]: part["value"]
                     for part in after_item["score_breakdown"]}
            self.assertGreater(after["profile_weight"], before["profile_weight"])

    def test_failure_projection_is_safe_and_does_not_expose_provider_reason(self):
        with tempfile.TemporaryDirectory() as root:
            app, repository, *_ = self.make(
                root, provider=FakeDigestProvider("overlong"),
            )
            sub = self.create(app)
            result = app.run_subscription(USER, sub.subscription_id, "bad-model")
            self.assertEqual(
                (result.status, result.failure_stage, result.failure_code,
                 result.failure_subtype),
                ("incomplete", "contract", "output_contract_failed",
                 "too_long"),
            )
            self.assertEqual(
                result.failure_diagnostics["expected_max_chars"], 600,
            )
            self.assertNotIn("rendered_text", repr(result))
            self.assertEqual(
                len(repository.list_generation_attempts(
                    result.application_run_id,
                )),
                1,
            )

    def test_contract_subtypes_persist_without_provider_retry(self):
        cases = (
            (FakeDigestProvider("invalid_source"), None,
             "invalid_source_ref"),
            (ContractMutatingProvider("duplicate"), None,
             "duplicate_item"),
            (FakeDigestProvider(), FakeSearchClient([{
                "url": "https://example.test/unrelated",
                "title": "Unrelated current update",
                "snippet": "A current but unrelated item.",
                "published_at": "2026-08-23T10:00:00Z",
                "topic_tags": ["unrelated"],
            }]), "topic_focus_mismatch"),
        )
        for provider, search, subtype in cases:
            with self.subTest(subtype=subtype), tempfile.TemporaryDirectory() as root:
                app, repository, *_ = self.make(
                    root, provider=provider, search=search,
                )
                sub = self.create(app)
                run = app.run_subscription(
                    USER, sub.subscription_id, "contract-rejection",
                )
                reopened = SQLiteDigestRepository(repository.path)
                persisted = reopened.get_digest_run(run.application_run_id)
                restarted, *_ = self.make(root, repository=reopened)
                view = restarted.get_run(USER, run.application_run_id)
                self.assertEqual(
                    (run.status, run.failure_subtype, view.failure_subtype,
                     persisted.failure_subtype),
                    ("incomplete", subtype, subtype, subtype),
                )
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(
                    len(reopened.list_generation_attempts(
                        run.application_run_id,
                    )),
                    1,
                )

    def test_rejected_raw_synthesis_candidate_is_never_persisted(self):
        with tempfile.TemporaryDirectory() as root:
            provider = ContractMutatingProvider("private_overlong")
            app, _repository, *_ = self.make(root, provider=provider)
            sub = self.create(app)
            run = app.run_subscription(USER, sub.subscription_id, "raw-private")
            self.assertEqual(run.failure_subtype, "too_long")
            marker = provider.marker.encode("utf-8")
            for directory, _names, files in os.walk(root):
                for name in files:
                    with open(os.path.join(directory, name), "rb") as stream:
                        self.assertNotIn(marker, stream.read())

    def test_legacy_contract_failure_has_no_invented_subtype(self):
        with tempfile.TemporaryDirectory() as root:
            app, repository, workflow, *_ = self.make(root)
            sub = self.create(app)
            legacy = DigestRunRecord(
                workflow.id_factory(), sub.subscription_id, "legacy-contract",
                workflow.id_factory(), "incomplete", "max_chars_exceeded",
                None, None, None, idempotency_key="legacy-contract",
                updated_at=NOW, failure_stage="contract",
                failure_code="output_contract_failed",
            )
            repository.reserve_digest_run(legacy)
            repository.finish_digest_run(legacy)
            view = app.get_run(USER, legacy.digest_run_id)
            self.assertEqual(view.failure_code, "output_contract_failed")
            self.assertIsNone(view.failure_subtype)
            self.assertIsNone(view.failure_diagnostics)

    def test_vertex_errors_keep_generation_provenance_and_no_digest(self):
        for provider_code, public_code, expected_calls in (
            ("TIMEOUT", "generation_timeout", 2),
            ("INVALID_RESPONSE", "generation_invalid_response", 1),
            ("AUTH_FAILED", "generation_configuration_error", 1),
            ("RATE_LIMITED", "generation_rate_limited", 1),
        ):
            with self.subTest(provider_code=provider_code), tempfile.TemporaryDirectory() as root:
                provider = FailingProvider(provider_code)
                app, repository, _workflow, search, *_ = self.make(
                    root, provider=provider,
                )
                sub = self.create(app)
                result = app.run_subscription(USER, sub.subscription_id, "provider-failure")
                persisted = repository.get_digest_run(result.application_run_id)
                expected_stage = (
                    "configuration" if provider_code == "AUTH_FAILED"
                    else "generation"
                )
                self.assertEqual(
                    (result.status, result.failure_stage, result.failure_code,
                     result.digest_id, len(search.calls), len(provider.calls)),
                    ("incomplete", expected_stage, public_code, None, 1,
                     expected_calls),
                )
                self.assertEqual(
                    (persisted.failure_stage, persisted.failure_code),
                    (expected_stage, public_code),
                )

    def test_schema_invalid_tool_payload_has_durable_precise_provenance(self):
        with tempfile.TemporaryDirectory() as root:
            provider = SchemaInvalidToolProvider()
            app, repository, *_ = self.make(root, provider=provider)
            sub = self.create(app)
            run = app.run_subscription(
                USER, sub.subscription_id, "schema-invalid-tool",
            )
            self.assertEqual(
                (run.status, run.failure_stage, run.failure_code,
                 run.failure_subtype, run.digest_id),
                ("incomplete", "generation", "generation_invalid_response",
                 "ITEMS_TYPE", None),
            )
            self.assertEqual(len(provider.calls), 2)
            self.assertEqual(run.failure_diagnostics, {
                "schema_mismatch_field": "items",
                "payload_source": "tool_arguments",
                "payload_top_type": "object",
                "payload_items_type": "object",
            })
            reopened = SQLiteDigestRepository(repository.path)
            restarted, *_ = self.make(root, repository=reopened)
            persisted = restarted.get_run(USER, run.application_run_id)
            self.assertEqual(
                (persisted.failure_stage, persisted.failure_code,
                 persisted.failure_subtype, persisted.digest_id),
                ("generation", "generation_invalid_response",
                 "ITEMS_TYPE", None),
            )
            self.assertNotIn("private-provider-payload", repr(persisted))

    def test_search_timeout_never_calls_provider(self):
        with tempfile.TemporaryDirectory() as root:
            provider = FakeDigestProvider()
            search = FailingSearchClient("TIMEOUT")
            app, repository, *_ = self.make(
                root, provider=provider, search=search,
            )
            sub = self.create(app)
            result = app.run_subscription(USER, sub.subscription_id, "search-timeout")
            persisted = repository.get_digest_run(result.application_run_id)
            self.assertEqual(
                (result.status, result.failure_stage, result.failure_code,
                 result.digest_id, len(provider.calls)),
                ("incomplete", "search", "search_timeout", None, 0),
            )
            self.assertEqual(
                (persisted.failure_stage, persisted.failure_code),
                ("search", "search_timeout"),
            )

    def test_legacy_failure_is_read_without_stage_inference_or_rewrite(self):
        with tempfile.TemporaryDirectory() as root:
            app, repository, workflow, *_ = self.make(root)
            sub = self.create(app)
            legacy = DigestRunRecord(
                workflow.id_factory(), sub.subscription_id, "legacy",
                workflow.id_factory(), "incomplete", "TIMEOUT",
                None, None, None, idempotency_key="legacy", updated_at=NOW,
            )
            repository.reserve_digest_run(legacy)
            repository.finish_digest_run(legacy)

            view = app.get_run(USER, legacy.digest_run_id)
            stored = repository.get_digest_run(legacy.digest_run_id)

            self.assertEqual(
                (view.status, view.failure_stage, view.failure_code),
                ("incomplete", "unknown_stage", "legacy_failure"),
            )
            self.assertEqual((stored.failure_stage, stored.failure_code), (None, None))

    def test_structured_output_retry_succeeds_once_with_same_inputs(self):
        for first_error in (
            ProviderAdapterError("TIMEOUT"),
            ProviderAdapterError("INVALID_RESPONSE", subtype="JSON_PARSE"),
            ProviderAdapterError("INVALID_RESPONSE", subtype="SCHEMA_MISMATCH"),
        ):
            with self.subTest(subtype=first_error.subtype or first_error.code), tempfile.TemporaryDirectory() as root:
                provider = ScriptedProvider([first_error, "valid"])
                app, repository, *_ = self.make(root, provider=provider)
                sub = self.create(app)
                run = app.run_subscription(USER, sub.subscription_id, "retry-once")
                attempts = repository.list_generation_attempts(run.application_run_id)
                self.assertEqual((run.status, len(provider.calls)), ("completed", 2))
                self.assertEqual(
                    tuple(item.status for item in attempts),
                    ("failed", "succeeded"),
                )
                self.assertNotEqual(attempts[0].attempt_id, attempts[1].attempt_id)
                self.assertEqual(
                    attempts[0].request_metadata,
                    attempts[1].request_metadata,
                )
                self.assertNotIn(
                    "must-not-persist",
                    repr(repository.list_generation_attempts(
                        run.application_run_id,
                    )),
                )

    def test_structured_retry_exhaustion_is_durable_and_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            provider = ScriptedProvider([
                ProviderAdapterError("INVALID_RESPONSE", subtype="JSON_PARSE"),
                ProviderAdapterError("INVALID_RESPONSE", subtype="JSON_PARSE"),
            ])
            app, repository, *_ = self.make(root, provider=provider)
            sub = self.create(app)
            run = app.run_subscription(USER, sub.subscription_id, "retry-exhausted")
            reopened = SQLiteDigestRepository(repository.path)
            attempts = reopened.list_generation_attempts(run.application_run_id)
            persisted = reopened.get_digest_run(run.application_run_id)
            self.assertEqual(
                (run.status, run.failure_code, run.digest_id, len(provider.calls)),
                ("incomplete", "generation_json_parse", None, 2),
            )
            self.assertEqual(
                tuple(item.failure_subtype for item in attempts),
                ("JSON_PARSE", "JSON_PARSE"),
            )
            self.assertEqual(
                (persisted.failure_stage, persisted.failure_code),
                ("generation", "generation_json_parse"),
            )


if __name__ == "__main__":
    unittest.main()
