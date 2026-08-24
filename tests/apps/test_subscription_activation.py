from dataclasses import asdict
import json
import os
import tempfile
import threading
import unittest

from apps.digest_agent.activation import (
    ActivationError, SubscriptionActivationService,
)
from apps.digest_agent.adapters.definition import FakeDefinitionAgentAdapter
from apps.digest_agent.adapters.sqlite import SQLiteDigestRepository
from apps.digest_agent.application import DigestApplication
from apps.digest_agent.conversation import DefinitionConversationWorkflow


NOW = "2026-08-24T12:00:00Z"
USER = "a" * 32
PRODUCT_TABLES = (
    "subscription_definitions", "subscriptions", "subscription_aggregates",
    "user_subscriptions", "briefing_reservations", "application_outbox",
    "subscription_activations",
)


def done():
    return {
        "protocol_version": 1, "type": "DONE",
        "definition": {
            "topic": "AI 行业动态", "language": "zh-CN",
            "cadence": "daily", "max_chars": 600, "max_items": 5,
            "focus_topics": ["Agent", "模型发布"],
            "delivery_preference": "none",
        },
    }


class IdFactory:
    def __init__(self, start):
        self.value = start
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            value = f"{self.value:032x}"
            self.value += 1
            return value


class ForbiddenDependency:
    def __getattr__(self, name):
        raise AssertionError(f"external dependency accessed: {name}")


class SubscriptionActivationTests(unittest.TestCase):
    def prepare(self, root, outcome=None, *, database=None):
        repository = SQLiteDigestRepository(
            database or os.path.join(root, "digest.db"),
        )
        provider = FakeDefinitionAgentAdapter([outcome or done()])
        workflow = DefinitionConversationWorkflow(
            repository, provider, os.path.join(root, "audit"),
            id_factory=IdFactory(1), clock=lambda: NOW,
            owner_id="f" * 32,
        )
        conversation_app = DigestApplication(
            repository, None, None, None, None, workflow,
        )
        view = conversation_app.start_subscription_conversation(
            USER, "帮我订阅 AI 行业动态，每次 600 字以内", "start",
        )
        return repository, provider, view

    def app(self, repository, *, start=100, fault_injector=None):
        activation = SubscriptionActivationService(
            repository, id_factory=IdFactory(start), clock=lambda: NOW,
            fault_injector=fault_injector,
        )
        forbidden = ForbiddenDependency()
        return DigestApplication(
            repository, forbidden, forbidden, forbidden, forbidden,
            None, activation,
        )

    @staticmethod
    def counts(repository):
        with repository.connect() as connection:
            return {
                name: connection.execute(
                    f"SELECT COUNT(*) FROM {name}",
                ).fetchone()[0]
                for name in PRODUCT_TABLES
            }

    def test_commit_creates_active_truth_and_pending_handoff_atomically(self):
        with tempfile.TemporaryDirectory() as root:
            repository, provider, conversation = self.prepare(root)
            app = self.app(repository)
            committed = app.commit_subscription_from_definition(
                USER, conversation.conversation_id,
            )
            self.assertEqual(
                (committed.status, committed.relation_status,
                 committed.first_briefing_status, committed.message),
                ("ACTIVE", "ACTIVE", "PENDING",
                 "订阅成功，正在准备首篇资讯。"),
            )
            self.assertEqual(len(provider.calls), 1)
            counts = self.counts(repository)
            self.assertTrue(all(count == 1 for count in counts.values()))
            with repository.connect() as connection:
                digest_run_count = connection.execute(
                    "SELECT COUNT(*) FROM digest_runs",
                ).fetchone()[0]
                digest_count = connection.execute(
                    "SELECT COUNT(*) FROM digests",
                ).fetchone()[0]
                row = connection.execute(
                    "SELECT harness_run_id FROM briefing_reservations",
                ).fetchone()
                foreign_key_errors = connection.execute(
                    "PRAGMA foreign_key_check",
                ).fetchall()
            self.assertEqual((digest_run_count, digest_count, row[0]),
                             (0, 0, None))
            self.assertEqual(foreign_key_errors, [])
            stored = repository.get_subscription_commit_for_outcome(
                committed.definition_outcome_id,
            )
            identities = {
                stored.definition.definition_id,
                stored.subscription.subscription_id,
                stored.relation.user_subscription_id,
                stored.briefing.application_run_id,
                stored.outbox.outbox_id,
                stored.activation.activation_id,
            }
            self.assertEqual(len(identities), 6)
            self.assertEqual(stored.outbox.event_type,
                             "FIRST_BRIEFING_REQUESTED")
            self.assertEqual(stored.outbox.attempt_number, 0)
            self.assertNotIn(
                "帮我订阅",
                json.dumps(stored.outbox.payload_refs, ensure_ascii=False),
            )
            public = json.dumps(asdict(committed), ensure_ascii=False)
            for hidden in ("outbox_id", "payload_refs", "harness_run_id"):
                self.assertNotIn(hidden, public)

    def test_only_accepted_done_outcome_can_commit(self):
        cases = ({
            "protocol_version": 1, "type": "NEXT_QUESTION",
            "question": "每篇多少字？",
        }, {
            "protocol_version": 1, "type": "REJECT",
            "reason": "当前不能创建该订阅。",
        })
        for index, outcome in enumerate(cases):
            with self.subTest(outcome=outcome["type"]), tempfile.TemporaryDirectory() as root:
                repository, _provider, conversation = self.prepare(
                    root, outcome, database=os.path.join(root, f"{index}.db"),
                )
                with self.assertRaisesRegex(
                        ActivationError, "definition_not_accepted"):
                    SubscriptionActivationService(
                        repository, id_factory=IdFactory(100),
                        clock=lambda: NOW,
                    ).commit(USER, conversation.conversation_id)
                self.assertTrue(all(
                    count == 0 for count in self.counts(repository).values()
                ))

    def test_duplicate_commit_reuses_every_product_identity(self):
        with tempfile.TemporaryDirectory() as root:
            repository, _provider, conversation = self.prepare(root)
            first = self.app(repository, start=100).commit_subscription_from_definition(
                USER, conversation.conversation_id,
            )
            second = self.app(repository, start=200).commit_subscription_from_definition(
                USER, conversation.conversation_id,
            )
            self.assertFalse(first.reused)
            self.assertTrue(second.reused)
            self.assertEqual(
                (first.definition_id, first.subscription_id,
                 first.user_subscription_id,
                 first.first_briefing_application_run_id),
                (second.definition_id, second.subscription_id,
                 second.user_subscription_id,
                 second.first_briefing_application_run_id),
            )
            stored = repository.get_subscription_commit_for_outcome(
                first.definition_outcome_id,
            )
            self.assertEqual(stored.outbox.status, "pending")
            self.assertTrue(all(
                count == 1 for count in self.counts(repository).values()
            ))

    def test_concurrent_commit_creates_one_product_truth(self):
        with tempfile.TemporaryDirectory() as root:
            repository, _provider, conversation = self.prepare(root)
            apps = [self.app(repository, start=100),
                    self.app(repository, start=200)]
            barrier = threading.Barrier(2)
            values = []
            errors = []

            def commit(app):
                try:
                    barrier.wait(5)
                    values.append(app.commit_subscription_from_definition(
                        USER, conversation.conversation_id,
                    ))
                except Exception as error:  # pragma: no cover - assertion aid
                    errors.append(error)

            threads = [threading.Thread(target=commit, args=(app,)) for app in apps]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            self.assertEqual(errors, [])
            self.assertEqual(len(values), 2)
            self.assertEqual(len({value.subscription_id for value in values}), 1)
            self.assertEqual(sorted(value.reused for value in values), [False, True])
            self.assertTrue(all(
                count == 1 for count in self.counts(repository).values()
            ))

    def test_each_precommit_failure_rolls_back_every_product_row(self):
        stages = (
            "after_definition", "after_subscription", "after_relation",
            "after_briefing_reservation", "after_outbox",
            "after_activation_binding",
        )
        for index, target in enumerate(stages):
            with self.subTest(stage=target), tempfile.TemporaryDirectory() as root:
                repository, _provider, conversation = self.prepare(
                    root, database=os.path.join(root, f"{index}.db"),
                )

                def fail(stage, _value):
                    if stage == target:
                        raise RuntimeError("synthetic transaction failure")

                application = self.app(
                    repository, start=100, fault_injector=fail,
                )
                with self.assertRaisesRegex(RuntimeError, "transaction failure"):
                    application.commit_subscription_from_definition(
                        USER, conversation.conversation_id,
                    )
                self.assertTrue(all(
                    count == 0 for count in self.counts(repository).values()
                ))
                self.assertEqual(application.list_subscriptions(USER), ())
                recovered = self.app(
                    repository, start=200,
                ).commit_subscription_from_definition(
                    USER, conversation.conversation_id,
                )
                self.assertEqual((recovered.status, recovered.first_briefing_status),
                                 ("ACTIVE", "PENDING"))

    def test_crash_after_commit_keeps_active_relation_and_pending_outbox(self):
        with tempfile.TemporaryDirectory() as root:
            repository, _provider, conversation = self.prepare(root)

            def crash(stage, _value):
                if stage == "after_commit":
                    raise RuntimeError("synthetic response loss")

            with self.assertRaisesRegex(RuntimeError, "response loss"):
                self.app(
                    repository, start=100, fault_injector=crash,
                ).commit_subscription_from_definition(
                    USER, conversation.conversation_id,
                )
            replay = self.app(
                repository, start=200,
            ).commit_subscription_from_definition(
                USER, conversation.conversation_id,
            )
            self.assertTrue(replay.reused)
            stored = repository.get_subscription_commit_for_outcome(
                replay.definition_outcome_id,
            )
            self.assertEqual(
                (stored.subscription.status, stored.relation.status,
                 stored.briefing.status, stored.outbox.status),
                ("ACTIVE", "ACTIVE", "PENDING", "pending"),
            )

    def test_postcommit_brave_failure_cannot_roll_back_subscription_truth(self):
        with tempfile.TemporaryDirectory() as root:
            repository, _provider, conversation = self.prepare(root)
            committed = self.app(repository).commit_subscription_from_definition(
                USER, conversation.conversation_id,
            )
            # Simulate the later worker's bounded downstream failure projection.
            with repository.connect() as connection:
                connection.execute("""
                    UPDATE application_outbox
                    SET status='failed', last_error_code='BRAVE_UNAVAILABLE',
                        version=version+1, updated_at=?
                    WHERE outbox_id=(SELECT outbox_id FROM subscription_activations
                                     WHERE definition_outcome_id=?)
                """, (NOW, committed.definition_outcome_id))
            stored = repository.get_subscription_commit_for_outcome(
                committed.definition_outcome_id,
            )
            self.assertEqual(
                (stored.subscription.status, stored.relation.status,
                 stored.briefing.status, stored.outbox.status,
                 stored.outbox.last_error_code),
                ("ACTIVE", "ACTIVE", "PENDING", "failed",
                 "BRAVE_UNAVAILABLE"),
            )

    def test_product_disable_updates_aggregate_and_relation_not_briefing(self):
        with tempfile.TemporaryDirectory() as root:
            repository, _provider, conversation = self.prepare(root)
            app = self.app(repository)
            committed = app.commit_subscription_from_definition(
                USER, conversation.conversation_id,
            )
            # Use the real legacy-compatible service only for lifecycle toggle.
            from apps.digest_agent.services import SubscriptionService
            lifecycle_app = DigestApplication(
                repository, SubscriptionService(
                    repository, clock=lambda: NOW,
                ), None, None, None,
            )
            disabled = lifecycle_app.disable_subscription(
                USER, committed.subscription_id, 1,
            )
            stored = repository.get_subscription_commit_for_outcome(
                committed.definition_outcome_id,
            )
            self.assertEqual(
                (disabled.product_status, stored.subscription.status,
                 stored.relation.status, stored.briefing.status,
                 stored.outbox.status),
                ("DISABLED", "DISABLED", "DISABLED", "PENDING", "pending"),
            )


if __name__ == "__main__":
    unittest.main()
