import json
import os
import tempfile
import threading
import unittest

from apps.digest_agent.activation import SubscriptionActivationService
from apps.digest_agent.adapters.definition import FakeDefinitionAgentAdapter
from apps.digest_agent.adapters.sqlite import SQLiteDigestRepository
from apps.digest_agent.application import DigestApplication
from apps.digest_agent.conversation import DefinitionConversationWorkflow
from apps.digest_agent.relation_events import (
    FakeRelationEventPublisher, RelationEventPublisherService,
    RelationPublishOutcome,
)


NOW = "2026-08-24T14:00:00Z"
USER = "a" * 32


class IdFactory:
    def __init__(self, start=1):
        self.value = start
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            value = f"{self.value:032x}"
            self.value += 1
            return value


def definition():
    return {
        "protocol_version": 1, "type": "DONE",
        "definition": {
            "topic": "AI 行业动态", "language": "zh-CN",
            "cadence": "daily", "max_chars": 600, "max_items": 5,
            "focus_topics": ["Agent", "模型发布"],
            "delivery_preference": "none",
        },
    }


class RelationEventPublisherTests(unittest.TestCase):
    def committed(self, root, *, activation_fault=None):
        repository = SQLiteDigestRepository(os.path.join(root, "digest.db"))
        conversation = DefinitionConversationWorkflow(
            repository, FakeDefinitionAgentAdapter([definition()]),
            os.path.join(root, "audit"), id_factory=IdFactory(1),
            clock=lambda: NOW, owner_id="f" * 32,
        ).start(USER, "订阅 AI 行业动态，每天 600 字，最多五条", "start")
        activation = SubscriptionActivationService(
            repository, id_factory=IdFactory(100), clock=lambda: NOW,
            fault_injector=activation_fault,
        )
        commit = activation.commit(
            USER, conversation.conversation.conversation_id,
        )
        return repository, commit

    @staticmethod
    def service(repository, outcomes=(), fault=None):
        publisher = FakeRelationEventPublisher(outcomes)
        service = RelationEventPublisherService(
            repository, publisher, clock=lambda: NOW,
            fault_injector=fault,
        )
        return service, publisher

    def test_commit_has_two_independent_durable_promises(self):
        with tempfile.TemporaryDirectory() as root:
            repository, commit = self.committed(root)
            relation_event = repository.get_relation_event_for_relation(
                commit.relation.user_subscription_id,
            )
            briefing = repository.get_application_outbox_for_run(
                commit.briefing.application_run_id,
            )
            self.assertEqual(
                (commit.relation.status, relation_event.event_type,
                 relation_event.status, briefing.event_type, briefing.status),
                ("ACTIVE", "USER_SUBSCRIPTION_CREATED", "pending",
                 "FIRST_BRIEFING_REQUESTED", "pending"),
            )
            with repository.connect() as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM digest_runs",
                ).fetchone()[0], 0)

    def test_pending_publish_accepted_and_success_never_republishes(self):
        with tempfile.TemporaryDirectory() as root:
            repository, commit = self.committed(root)
            service, publisher = self.service(repository)
            first = service.run_once()
            second = service.run_once()
            relation = repository.get_user_subscription_for_subscription(
                commit.subscription.subscription_id,
            )
            event = repository.get_relation_event(first.event_id)
            attempt = repository.get_current_relation_event_attempt(first.event_id)
            self.assertEqual(
                (first.worker_status, first.publication_status,
                 second.worker_status, event.status, attempt.status,
                 attempt.effect_certainty, relation.status, len(publisher.calls)),
                ("SUCCEEDED", "SUCCEEDED", "NO_WORK", "completed",
                 "accepted", "known_applied", "ACTIVE", 1),
            )

    def test_explicit_failure_is_retryable_without_changing_relation(self):
        with tempfile.TemporaryDirectory() as root:
            repository, commit = self.committed(root)
            service, publisher = self.service(repository, (
                RelationPublishOutcome(
                    "explicit_failure", "PUBLISH_NOT_APPLIED",
                ),
                RelationPublishOutcome("accepted"),
            ))
            failed = service.run_once()
            event_id = failed.event_id
            briefing_before = repository.get_application_outbox_for_run(
                commit.briefing.application_run_id,
            )
            succeeded = service.run_once()
            relation = repository.get_user_subscription_for_subscription(
                commit.subscription.subscription_id,
            )
            attempts = repository.list_relation_event_attempts(event_id)
            self.assertEqual(
                (failed.publication_status, succeeded.publication_status,
                 relation.status, briefing_before.status,
                 len(attempts), len(publisher.calls)),
                ("RETRYABLE", "SUCCEEDED", "ACTIVE", "pending", 2, 2),
            )
            self.assertEqual({call["event_id"] for call in publisher.calls},
                             {event_id})

    def test_timeout_unknown_blocks_and_never_blind_retries(self):
        with tempfile.TemporaryDirectory() as root:
            repository, commit = self.committed(root)
            service, publisher = self.service(repository, (
                RelationPublishOutcome(
                    "timeout_unknown", "TRANSPORT_TIMEOUT",
                ),
            ))
            blocked = service.run_once()
            no_work = service.run_once()
            inspection = service.inspect(blocked.event_id)
            relation = repository.get_user_subscription_for_subscription(
                commit.subscription.subscription_id,
            )
            self.assertEqual(
                (blocked.worker_status, blocked.publication_status,
                 no_work.worker_status, inspection.publication_status,
                 inspection.blocking_reason, inspection.safe_recovery_actions,
                 relation.status, len(publisher.calls)),
                ("BLOCKED", "UNKNOWN", "NO_WORK", "UNKNOWN",
                 "PUBLICATION_UNKNOWN", (), "ACTIVE", 1),
            )

    def test_claim_then_crash_fails_closed_and_safe_release_reuses_event(self):
        with tempfile.TemporaryDirectory() as root:
            repository, commit = self.committed(root)

            def crash(stage, _value):
                if stage == "after_claim":
                    raise RuntimeError("claim crash")

            crashing, publisher = self.service(repository, fault=crash)
            with self.assertRaisesRegex(RuntimeError, "claim crash"):
                crashing.run_once()
            event_id = commit.relation_event.event_id
            inspection = crashing.inspect(event_id)
            self.assertEqual(
                (inspection.publication_status,
                 inspection.safe_recovery_actions, len(publisher.calls)),
                ("CLAIMED", ("release_not_started",), 0),
            )
            crashing.recover(event_id, "release_not_started")
            resumed, publisher2 = self.service(repository)
            result = resumed.run_once()
            self.assertEqual(
                (result.publication_status, result.event_id,
                 len(publisher2.calls)),
                ("SUCCEEDED", event_id, 1),
            )

    def test_accepted_then_crash_before_persistence_remains_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            repository, commit = self.committed(root)

            def crash(stage, _value):
                if stage == "after_publish":
                    raise RuntimeError("persistence crash")

            service, publisher = self.service(repository, fault=crash)
            with self.assertRaisesRegex(RuntimeError, "persistence crash"):
                service.run_once()
            event_id = commit.relation_event.event_id
            inspection = service.inspect(event_id)
            self.assertEqual(
                (inspection.publication_status,
                 inspection.effect_certainty,
                 inspection.safe_recovery_actions, len(publisher.calls)),
                ("UNKNOWN", "unknown", ("block_unknown",), 1),
            )
            self.assertEqual(service.run_once().worker_status, "NO_WORK")
            recovered = service.recover(event_id, "block_unknown")
            self.assertEqual(
                (recovered.publication_status, recovered.relation_status,
                 service.run_once().worker_status, len(publisher.calls)),
                ("UNKNOWN", "ACTIVE", "NO_WORK", 1),
            )

    def test_concurrent_ticks_create_one_publication_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            repository, _commit = self.committed(root)
            publisher = FakeRelationEventPublisher()
            services = [
                RelationEventPublisherService(
                    repository, publisher, clock=lambda: NOW,
                ) for _index in range(2)
            ]
            barrier = threading.Barrier(2)
            results, errors = [], []

            def run(service):
                try:
                    barrier.wait(5)
                    results.append(service.run_once())
                except Exception as error:  # pragma: no cover - assertion aid
                    errors.append(error)

            threads = [threading.Thread(target=run, args=(service,))
                       for service in services]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            self.assertEqual(errors, [])
            self.assertEqual(
                sorted(value.worker_status for value in results),
                ["NO_WORK", "SUCCEEDED"],
            )
            self.assertEqual(len(publisher.calls), 1)

    def test_event_payload_is_stable_minimal_and_secret_free(self):
        with tempfile.TemporaryDirectory() as root:
            repository, commit = self.committed(root)
            event = commit.relation_event
            self.assertEqual(set(event.payload), {
                "event_id", "event_type", "user_subscription_id", "user_id",
                "subscription_id", "relation_version", "relation_identity",
                "created_at",
            })
            rendered = json.dumps(event.payload, ensure_ascii=False).lower()
            for forbidden in (
                    "conversation", "definition", "prompt", "evidence",
                    "harness", "credential", "profile", "api_key"):
                self.assertNotIn(forbidden, rendered)
            replay = SubscriptionActivationService(
                repository, id_factory=IdFactory(500), clock=lambda: NOW,
            ).commit(USER, commit.activation.conversation_id)
            self.assertTrue(replay.reused)
            self.assertEqual(replay.relation_event.event_id, event.event_id)
            self.assertEqual(len(repository.list_relation_events()), 1)

    def test_drain_is_bounded_and_no_eligible_work_is_safe(self):
        with tempfile.TemporaryDirectory() as root:
            repository = SQLiteDigestRepository(os.path.join(root, "digest.db"))
            service, publisher = self.service(repository)
            self.assertEqual(service.run_once().worker_status, "NO_WORK")
            self.assertEqual(service.drain(3), ())
            self.assertEqual(publisher.calls, [])


if __name__ == "__main__":
    unittest.main()
