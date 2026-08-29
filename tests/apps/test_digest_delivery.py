import os
import sqlite3
import tempfile
import unittest

from apps.digest_agent.adapters.delivery import (
    FakeDeliveryAdapter, TermuxNotificationDeliveryAdapter,
)
from apps.digest_agent.adapters.provider import FakeDigestProvider
from apps.digest_agent.adapters.search import FakeSearchClient
from apps.digest_agent.adapters.sqlite import SQLiteDigestRepository
from apps.digest_agent.domain import (
    DeliveryOutcome, DeliveryRequest, DomainError, delivery_identity,
)
from apps.digest_agent.services import (
    DeliveryPersistenceError, DeliveryService, SubscriptionService,
)
from apps.digest_agent.workflows import DigestGenerationWorkflow


NOW = "2026-08-23T12:00:00Z"


class IdFactory:
    def __init__(self):
        self.value = 300

    def __call__(self):
        value = f"{self.value:032x}"
        self.value += 1
        return value


def rows():
    return [{
        "url": "https://example.test/delivery",
        "title": "Digest Delivery",
        "snippet": "用于离线交付测试的确定性内容。" * 12,
        "published_at": "2026-08-23T10:00:00Z",
        "topic_tags": ["AI 行业动态", "Agent"],
    }]


class DeliverySliceCase(unittest.TestCase):
    def make(self, root, mode="accepted", channel="fake", adapter=None):
        ids = IdFactory()
        repository = SQLiteDigestRepository(os.path.join(root, "digest.db"))
        subscriptions = SubscriptionService(
            repository, id_factory=ids, clock=lambda: NOW,
        )
        subscription = subscriptions.create_from_natural_language(
            "a" * 32,
            "帮我订阅 AI 行业动态，每天一份，600 字以内，最多 1 条，"
            "重点关注 Agent。",
        )
        workflow = DigestGenerationWorkflow(
            repository, FakeSearchClient(rows()), FakeDigestProvider(),
            os.path.join(root, "workspace"), os.path.join(root, "audit"),
            id_factory=ids, clock=lambda: NOW,
        )
        generation = workflow.run(subscription.subscription_id, "2026-08-23")
        self.assertEqual(generation.status, "completed")
        digest = repository.get_digest(generation.digest_id)
        adapter = adapter or FakeDeliveryAdapter(mode)
        service = DeliveryService(repository, [adapter], clock=lambda: NOW)
        return repository, subscription, generation, digest, adapter, service


class DeliveryDomainTests(unittest.TestCase):
    def test_status_and_certainty_combinations_are_closed(self):
        DeliveryOutcome(
            "accepted", "known_applied", safe_observation={
                "notification_requested": True,
                "request_accepted": True,
            },
        )
        DeliveryOutcome("failed", "not_started", error_code="REJECTED")
        DeliveryOutcome("unknown", "unknown", error_code="TIMEOUT")
        for args in (
            ("accepted", "unknown", None),
            ("failed", "known_applied", "FAILED"),
            ("unknown", "not_started", "TIMEOUT"),
        ):
            with self.subTest(args=args), self.assertRaises(DomainError):
                DeliveryOutcome(args[0], args[1], error_code=args[2])

    def test_delivery_identity_is_stable_per_digest_and_channel(self):
        digest_id = "d" * 32
        self.assertEqual(
            delivery_identity(digest_id, "fake"),
            delivery_identity(digest_id, "fake"),
        )
        self.assertNotEqual(
            delivery_identity(digest_id, "fake"),
            delivery_identity(digest_id, "termux_notification"),
        )


class DeliveryServiceTests(DeliverySliceCase):
    def test_e2e_a_completed_digest_delivery_is_accepted(self):
        with tempfile.TemporaryDirectory() as root:
            repository, sub, _gen, digest, adapter, service = self.make(root)
            record = service.deliver_digest(sub.user_id, digest.digest_id, "fake")
            self.assertEqual(
                (record.status, record.effect_certainty),
                ("accepted", "known_applied"),
            )
            self.assertEqual(len(adapter.calls), 1)
            self.assertEqual(repository.get_delivery(record.delivery_id), record)

    def test_explicit_failure_is_failed_not_started(self):
        with tempfile.TemporaryDirectory() as root:
            _repo, sub, _gen, digest, adapter, service = self.make(
                root, "explicit_failure",
            )
            record = service.deliver_digest(sub.user_id, digest.digest_id, "fake")
            self.assertEqual(
                (record.status, record.effect_certainty, record.error_code),
                ("failed", "not_started", "FAKE_REJECTED"),
            )
            self.assertEqual(len(adapter.calls), 1)

    def test_e2e_b_delivery_failure_does_not_change_digest_result(self):
        with tempfile.TemporaryDirectory() as root:
            repository, sub, generation, digest, _adapter, service = self.make(
                root, "explicit_failure",
            )
            original_digest = repository.get_digest(digest.digest_id)
            original_run = repository.get_digest_run(generation.digest_run_id)
            record = service.deliver_digest(sub.user_id, digest.digest_id, "fake")
            self.assertEqual(record.status, "failed")
            self.assertEqual(repository.get_digest(digest.digest_id), original_digest)
            self.assertEqual(repository.get_digest_run(generation.digest_run_id),
                             original_run)
            self.assertEqual(original_run.status, "completed")

    def test_timeout_is_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            _repo, sub, _gen, digest, adapter, service = self.make(
                root, "timeout_unknown",
            )
            record = service.deliver_digest(sub.user_id, digest.digest_id, "fake")
            self.assertEqual(
                (record.status, record.effect_certainty, record.error_code),
                ("unknown", "unknown", "TIMEOUT"),
            )
            self.assertEqual(len(adapter.calls), 1)

    def test_duplicate_request_does_not_duplicate_dispatch(self):
        with tempfile.TemporaryDirectory() as root:
            _repo, sub, _gen, digest, adapter, service = self.make(root)
            first = service.deliver_digest(sub.user_id, digest.digest_id, "fake")
            duplicate = service.deliver_digest(sub.user_id, digest.digest_id, "fake")
            self.assertEqual(duplicate, first)
            self.assertEqual(len(adapter.calls), 1)

    def test_e2e_c_unknown_does_not_blind_retry(self):
        with tempfile.TemporaryDirectory() as root:
            _repo, sub, _gen, digest, adapter, service = self.make(
                root, "timeout_unknown",
            )
            record = service.deliver_digest(sub.user_id, digest.digest_id, "fake")
            duplicate = service.deliver_digest(sub.user_id, digest.digest_id, "fake")
            self.assertEqual(duplicate.status, "unknown")
            with self.assertRaises(DomainError):
                service.retry_delivery(record.delivery_id)
            self.assertEqual(len(adapter.calls), 1)

    def test_explicit_safe_failure_may_create_next_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            repository, sub, _gen, digest, adapter, service = self.make(
                root, "explicit_failure",
            )
            first = service.deliver_digest(sub.user_id, digest.digest_id, "fake")
            second = service.retry_delivery(first.delivery_id)
            self.assertEqual(second.attempt_number, 2)
            self.assertNotEqual(second.attempt_id, first.attempt_id)
            self.assertEqual(len(adapter.calls), 2)
            with repository.connect() as connection:
                attempts = connection.execute(
                    "SELECT count(*) FROM delivery_attempts WHERE delivery_id=?",
                    (first.delivery_id,),
                ).fetchone()[0]
            self.assertEqual(attempts, 2)

    def test_persistence_failure_after_dispatch_keeps_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            repository, sub, _gen, digest, adapter, service = self.make(root)
            with repository.connect() as connection:
                connection.execute("""
                    CREATE TRIGGER fail_delivery_terminal
                    BEFORE UPDATE OF status ON delivery_attempts
                    WHEN NEW.status='accepted'
                    BEGIN SELECT RAISE(ABORT, 'terminal persistence failure'); END
                """)
            with self.assertRaises(DeliveryPersistenceError):
                service.deliver_digest(sub.user_id, digest.digest_id, "fake")
            record = repository.get_delivery_for_digest(digest.digest_id, "fake")
            self.assertEqual(
                (record.status, record.effect_certainty), ("unknown", "unknown"),
            )
            self.assertEqual(len(adapter.calls), 1)

    def test_raw_provider_output_is_not_persisted(self):
        with tempfile.TemporaryDirectory() as root:
            repository, sub, _gen, digest, adapter, service = self.make(root)
            service.deliver_digest(sub.user_id, digest.digest_id, "fake")
            self.assertIn("RAW_PROVIDER_RESPONSE_DO_NOT_PERSIST",
                          adapter.raw_responses[0]["provider_debug"])
            with open(repository.path, "rb") as database:
                stored = database.read()
            self.assertNotIn(b"RAW_PROVIDER_RESPONSE_DO_NOT_PERSIST", stored)

    def test_delivery_record_persists_without_user_consumption_claim(self):
        with tempfile.TemporaryDirectory() as root:
            repository, sub, _gen, digest, _adapter, service = self.make(root)
            record = service.deliver_digest(sub.user_id, digest.digest_id, "fake")
            reopened = SQLiteDigestRepository(repository.path)
            persisted = reopened.get_delivery(record.delivery_id)
            self.assertEqual(persisted, record)
            self.assertFalse(hasattr(persisted, "user_seen"))
            self.assertFalse(hasattr(persisted, "user_read"))
            with reopened.connect() as connection:
                interactions = connection.execute(
                    "SELECT count(*) FROM interactions"
                ).fetchone()[0]
            self.assertEqual(interactions, 0)


class TermuxDeliveryMappingTests(DeliverySliceCase):
    def test_termux_mapping_uses_safe_preview_and_existing_certainty(self):
        with tempfile.TemporaryDirectory() as root:
            calls = []

            def authorized_dispatcher(capability, arguments):
                calls.append((capability, arguments))
                return {
                    "status": "succeeded", "effect_certainty": "known_applied",
                    "error_code": None,
                    "safe_observation": {
                        "notification_requested": True,
                        "request_accepted": True,
                    },
                }

            adapter = TermuxNotificationDeliveryAdapter(authorized_dispatcher)
            _repo, sub, _gen, digest, _adapter, service = self.make(
                root, channel="termux_notification", adapter=adapter,
            )
            record = service.deliver_digest(
                sub.user_id, digest.digest_id, "termux_notification",
            )
            self.assertEqual(record.status, "accepted")
            capability, arguments = calls[0]
            self.assertEqual(capability, "termux:notification")
            self.assertEqual(arguments["title"], "AI Digest")
            self.assertLessEqual(len(arguments["content"]), 160)
            self.assertIn(digest.digest_id, arguments["content"])
            self.assertNotEqual(arguments["content"],
                                digest.payload["rendered_text"])
            self.assertNotIn("\n", arguments["content"])

    def test_termux_certainty_mapping_never_promotes_unknown(self):
        request = DeliveryRequest(
            "1" * 32, "2" * 32, "3" * 32, "termux_notification",
            "AI Digest", "short preview",
        )
        fixtures = (
            ({"status": "failed", "effect_certainty": "not_started",
              "error_code": "CAPABILITY_NOT_INSTALLED"},
             ("failed", "not_started")),
            ({"status": "failed", "effect_certainty": "unknown",
              "error_code": "TIMEOUT"}, ("unknown", "unknown")),
        )
        for result, expected in fixtures:
            with self.subTest(expected=expected):
                adapter = TermuxNotificationDeliveryAdapter(
                    lambda _capability, _arguments, value=result: value,
                )
                outcome = adapter.dispatch(request)
                self.assertEqual(
                    (outcome.status, outcome.effect_certainty), expected,
                )


if __name__ == "__main__":
    unittest.main()
