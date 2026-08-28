from dataclasses import asdict
import json
import os
import tempfile
import threading
import unittest

from apps.digest_agent.activation import SubscriptionActivationService
from apps.digest_agent.adapters.definition import FakeDefinitionAgentAdapter
from apps.digest_agent.adapters.flight import FakeFlightPriceProvider
from apps.digest_agent.adapters.sqlite import SQLiteDigestRepository
from apps.digest_agent.application import ApplicationError, DigestApplication
from apps.digest_agent.conditions import (
    FlightConditionService, build_flight_condition_service,
)
from apps.digest_agent.conversation import DefinitionConversationWorkflow


NOW = "2026-08-27T12:00:00Z"
OBSERVED = "2026-08-27T11:00:00Z"
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


def flight_done(**intent_changes):
    intent = {
        "topic": {"value": "深圳往返武汉的机票优惠", "source_turn": 1},
        "constraints": [{"value": "低于800元", "source_turn": 1}],
        "goal": {"value": "寻找深圳往返武汉的机票优惠", "source_turn": 1},
        "trigger": {"value": "票价低于800元时提醒", "source_turn": 1},
        "time_window": {"value": "9 月", "source_turn": 1},
        "locations": [
            {"value": "深圳", "source_turn": 1},
            {"value": "武汉", "source_turn": 1},
        ],
        "focus_topics": [],
        "preferences": {},
    }
    intent.update(intent_changes)
    return {"protocol_version": 2, "type": "DONE", "intent": intent}


class FlightConditionTests(unittest.TestCase):
    def prepare(self, root, *, price=920, observed_at=OBSERVED,
                overrides=None, outcome=None, activation_fault=None,
                condition_fault=None):
        repository = SQLiteDigestRepository(os.path.join(root, "digest.db"))
        workflow = DefinitionConversationWorkflow(
            repository, FakeDefinitionAgentAdapter([outcome or flight_done()]),
            os.path.join(root, "audit"), id_factory=IdFactory(1),
            clock=lambda: NOW, owner_id="f" * 32,
        )
        activation = SubscriptionActivationService(
            repository, id_factory=IdFactory(100), clock=lambda: NOW,
            fault_injector=activation_fault,
        )
        provider = FakeFlightPriceProvider(
            price, observed_at=observed_at, overrides=overrides,
            source_signal_id="fake:SZX:WUH:2026-09:round-trip",
            clock=lambda: NOW,
        )
        conditions = build_flight_condition_service(
            repository, provider, os.path.join(root, "audit"),
            clock=lambda: NOW, fault_injector=condition_fault,
        )
        app = DigestApplication(
            repository, None, None, None, None,
            conversation_workflow=workflow,
            activation_service=activation,
            condition_service=conditions,
        )
        proposal = app.start_subscription_conversation(
            USER, "关注深圳—武汉 9 月往返机票，低于 800 元提醒我。",
            "flight-start",
        )
        return repository, provider, conditions, app, proposal

    @staticmethod
    def counts(repository):
        tables = (
            "briefing_reservations", "application_outbox", "digest_runs",
            "tracking_definitions", "tracking_policy_snapshots",
            "condition_observation_requests", "flight_price_observations",
            "condition_evaluations", "tracking_updates",
            "update_distributions",
        )
        with repository.connect() as connection:
            return {table: connection.execute(
                f"SELECT COUNT(*) FROM {table}",
            ).fetchone()[0] for table in tables}

    def test_920_is_normal_no_update_and_subscription_stays_active(self):
        with tempfile.TemporaryDirectory() as root:
            repository, provider, _conditions, app, proposal = self.prepare(root)
            committed = app.commit_subscription_from_definition(
                USER, proposal.conversation_id,
            )
            self.assertEqual(
                (committed.status, committed.workflow_kind,
                 committed.first_briefing_status),
                ("ACTIVE", "CONDITION", None),
            )
            before = self.counts(repository)
            self.assertEqual(
                (before["briefing_reservations"],
                 before["application_outbox"], before["digest_runs"]),
                (0, 0, 0),
            )
            result = app.run_condition_once()
            self.assertEqual(
                (result.worker_status, result.monitoring_status,
                 result.update_id, result.distribution_id),
                ("NO_UPDATE", "NO_UPDATE", None, None),
            )
            product = repository.get_product_subscription(
                committed.subscription_id,
            )
            relation = repository.get_user_subscription_for_subscription(
                committed.subscription_id,
            )
            self.assertEqual((product.status, relation.status), ("ACTIVE", "ACTIVE"))
            self.assertEqual(len(provider.calls), 1)
            after = self.counts(repository)
            self.assertEqual(
                (after["flight_price_observations"],
                 after["condition_evaluations"], after["tracking_updates"],
                 after["update_distributions"]),
                (1, 1, 0, 0),
            )
            request = repository.get_latest_condition_request_for_subscription(
                committed.subscription_id,
            )
            evaluation = (
                repository.get_latest_condition_evaluation_for_subscription(
                    committed.subscription_id,
                )
            )
            self.assertEqual(
                (request.status, request.failure_code, evaluation.result),
                ("EVALUATED", None, "NO_UPDATE"),
            )
            detail = app.get_feed_detail(USER, committed.subscription_id)
            self.assertEqual(
                (detail.condition_monitoring.latest_price,
                 detail.condition_monitoring.threshold,
                 detail.condition_monitoring.condition_met,
                 detail.condition_monitoring.status, detail.history),
                (920, 800, False, "NO_UPDATE", ()),
            )

    def test_760_creates_one_versioned_update_and_one_distribution(self):
        with tempfile.TemporaryDirectory() as root:
            repository, _provider, conditions, app, proposal = self.prepare(
                root, price=760,
            )
            committed = app.commit_subscription_from_definition(
                USER, proposal.conversation_id,
            )
            result = app.run_condition_once()
            self.assertEqual(
                (result.worker_status, result.monitoring_status),
                ("UPDATE_CREATED", "MATCHED"),
            )
            update = repository.get_update_for_evaluation(
                repository.get_latest_condition_evaluation_for_subscription(
                    committed.subscription_id,
                ).evaluation_id,
            )
            distribution = repository.get_distribution_for_update(
                update.update_id,
            )
            tracking = repository.get_tracking_definition(
                committed.definition_id, committed.definition_version,
            )
            self.assertEqual(
                (update.definition_id, update.definition_version,
                 update.payload["observed_price"], update.payload["threshold"],
                 update.evidence_id),
                (tracking.definition_id, tracking.definition_version,
                 760, 800, update.evidence_id),
            )
            self.assertEqual(
                distribution.user_subscription_id,
                committed.user_subscription_id,
            )
            evidence = conditions.evidence_store.load(update.evidence_id)
            self.assertEqual(
                evidence["content_identity"]["claim"]["price"], 760,
            )
            self.assertEqual(evidence["verification"]["accepted"], True)
            counts = self.counts(repository)
            self.assertEqual(
                (counts["tracking_updates"], counts["update_distributions"]),
                (1, 1),
            )
            public = json.dumps(asdict(
                app.get_feed_detail(USER, committed.subscription_id),
            ), ensure_ascii=False)
            for hidden in ("evidence_id", "observation_id", "evaluation_id",
                           "predicate", "Harness Run"):
                self.assertNotIn(hidden, public)

    def test_repeated_logical_signal_reuses_evaluation_update_distribution(self):
        with tempfile.TemporaryDirectory() as root:
            repository, _provider, _conditions, app, proposal = self.prepare(
                root, price=760,
            )
            committed = app.commit_subscription_from_definition(
                USER, proposal.conversation_id,
            )
            first = app.run_condition_once()
            app.request_condition_check(
                USER, committed.subscription_id, "same-signal-retry",
            )
            second = app.run_condition_once()
            self.assertEqual(
                (second.worker_status, second.reused, second.update_id,
                 second.distribution_id),
                ("REUSED", True, first.update_id, first.distribution_id),
            )
            counts = self.counts(repository)
            self.assertEqual(
                (counts["condition_observation_requests"],
                 counts["flight_price_observations"],
                 counts["condition_evaluations"], counts["tracking_updates"],
                 counts["update_distributions"]),
                (2, 1, 1, 1, 1),
            )

    def test_condition_commit_is_atomic_and_response_loss_is_idempotent(self):
        commit_tables = (
            "subscription_definitions", "subscriptions",
            "subscription_aggregates", "user_subscriptions",
            "relation_event_outbox", "tracking_definitions",
            "tracking_policy_snapshots", "condition_observation_requests",
            "condition_subscription_activations", "briefing_reservations",
            "application_outbox",
        )
        for stage in (
                "after_definition", "after_relation_event",
                "after_tracking_definition", "after_condition_request",
                "after_activation_binding"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as root:
                def fail(name, _value):
                    if name == stage:
                        raise RuntimeError("synthetic commit crash")

                repository, _provider, _conditions, app, proposal = self.prepare(
                    root, activation_fault=fail,
                )
                with self.assertRaisesRegex(RuntimeError, "commit crash"):
                    app.commit_subscription_from_definition(
                        USER, proposal.conversation_id,
                    )
                with repository.connect() as connection:
                    counts = [connection.execute(
                        f"SELECT COUNT(*) FROM {table}",
                    ).fetchone()[0] for table in commit_tables]
                self.assertTrue(all(count == 0 for count in counts))

        with tempfile.TemporaryDirectory() as root:
            fired = {"value": False}

            def lose_response(stage, _value):
                if stage == "after_commit" and not fired["value"]:
                    fired["value"] = True
                    raise RuntimeError("response lost")

            repository, _provider, _conditions, app, proposal = self.prepare(
                root, activation_fault=lose_response,
            )
            with self.assertRaisesRegex(RuntimeError, "response lost"):
                app.commit_subscription_from_definition(
                    USER, proposal.conversation_id,
                )
            recovered = SubscriptionActivationService(
                repository, id_factory=IdFactory(500), clock=lambda: NOW,
            ).commit(USER, proposal.conversation_id)
            self.assertTrue(recovered.reused)
            self.assertEqual(
                repository.get_subscription_commit_for_outcome(
                    recovered.activation.definition_outcome_id,
                ).subscription.subscription_id,
                recovered.subscription.subscription_id,
            )

    def test_evaluation_crash_rolls_back_and_retry_reuses_immutable_evidence(self):
        for stage in (
                "after_observation", "after_evaluation", "after_update",
                "after_distribution", "after_request_completion"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as root:
                def fail(name, _value):
                    if name == stage:
                        raise RuntimeError("synthetic evaluation crash")

                repository, provider, conditions, app, proposal = self.prepare(
                    root, price=760, condition_fault=fail,
                )
                committed = app.commit_subscription_from_definition(
                    USER, proposal.conversation_id,
                )
                with self.assertRaisesRegex(RuntimeError, "evaluation crash"):
                    app.run_condition_once()
                counts = self.counts(repository)
                self.assertEqual(
                    (counts["flight_price_observations"],
                     counts["condition_evaluations"],
                     counts["tracking_updates"],
                     counts["update_distributions"]),
                    (0, 0, 0, 0),
                )
                request = repository.get_latest_condition_request_for_subscription(
                    committed.subscription_id,
                )
                self.assertEqual(request.status, "PENDING")
                clean = FlightConditionService(
                    repository, provider, conditions.evidence_store,
                    clock=lambda: NOW,
                )
                retried = clean.run_once()
                self.assertEqual(retried.worker_status, "UPDATE_CREATED")
                counts = self.counts(repository)
                self.assertEqual(
                    (counts["flight_price_observations"],
                     counts["condition_evaluations"],
                     counts["tracking_updates"],
                     counts["update_distributions"]),
                    (1, 1, 1, 1),
                )

    def test_invalid_and_stale_observations_fail_closed_without_deactivation(self):
        cases = (
            ({"currency": "USD"}, OBSERVED, "INVALID_OBSERVATION"),
            ({"origin": "广州"}, OBSERVED, "INVALID_OBSERVATION"),
            ({"travel_month": 10}, OBSERVED, "INVALID_OBSERVATION"),
            (None, "2026-08-25T00:00:00Z", "STALE_OBSERVATION"),
        )
        for overrides, observed_at, code in cases:
            with self.subTest(code=code, overrides=overrides), tempfile.TemporaryDirectory() as root:
                repository, _provider, _conditions, app, proposal = self.prepare(
                    root, observed_at=observed_at, overrides=overrides,
                )
                committed = app.commit_subscription_from_definition(
                    USER, proposal.conversation_id,
                )
                result = app.run_condition_once()
                self.assertEqual(
                    (result.worker_status, result.failure_reason),
                    ("FAILED", code),
                )
                self.assertEqual(
                    repository.get_product_subscription(
                        committed.subscription_id,
                    ).status,
                    "ACTIVE",
                )
                counts = self.counts(repository)
                self.assertEqual(
                    (counts["flight_price_observations"],
                     counts["condition_evaluations"],
                     counts["tracking_updates"],
                     counts["update_distributions"]),
                    (0, 0, 0, 0),
                )

    def test_unsupported_or_ambiguous_condition_never_falls_back_to_briefing(self):
        cases = (
            {"time_window": None},
            {"constraints": [{"value": "800元左右", "source_turn": 1}]},
            {"locations": [{"value": "深圳", "source_turn": 1}]},
        )
        for changes in cases:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as root:
                repository, _provider, _conditions, app, proposal = self.prepare(
                    root, outcome=flight_done(**changes),
                )
                with self.assertRaises(ApplicationError) as raised:
                    app.commit_subscription_from_definition(
                        USER, proposal.conversation_id,
                    )
                self.assertEqual(
                    raised.exception.code, "unsupported_tracking_intent",
                )
                counts = self.counts(repository)
                self.assertTrue(all(value == 0 for value in counts.values()))


if __name__ == "__main__":
    unittest.main()
