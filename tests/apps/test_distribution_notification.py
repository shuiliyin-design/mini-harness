import os
import tempfile
import unittest

from mini_harness_core.evidence import EvidenceStore

from apps.digest_agent.adapters.delivery import FakeDeliveryAdapter
from apps.digest_agent.adapters.sqlite import SQLiteDigestRepository
from apps.digest_agent.domain import (
    DeliveryRecord, DomainError, delivery_attempt_identity,
    distribution_notification_identity,
)
from apps.digest_agent.services import DeliveryService
from tests.apps import test_continuous_flight_condition as p44


USER = p44.USER


class DistributionNotificationTests(unittest.TestCase):
    def prepare(self, root, *, price=920, mode="accepted", notify=True,
                attach=True):
        preferences = ({
            "delivery_preference": {
                "value": "termux_notification", "source_turn": 1,
            },
        } if notify else None)
        message = (
            "持续关注深圳—武汉 9 月往返机票，低于 800 元提醒我，"
            + ("并使用本机通知。" if notify else "只在产品内查看。")
        )
        base = p44.ContinuousFlightConditionTests()
        values = base.prepare(
            root, price=price, outcome=p44.flight_done(preferences),
            message=message,
        )
        repository, provider, conditions, app, clock, committed, proposal = values
        adapter = FakeDeliveryAdapter(
            mode, channel="termux_notification",
        )
        evidence = EvidenceStore(os.path.join(root, "notification-evidence"))
        service = DeliveryService(
            repository, [adapter], clock=clock, evidence_store=evidence,
        )
        if attach:
            app.deliveries = service
        return (*values, adapter, service, evidence)

    @staticmethod
    def delivery_counts(repository):
        with repository.connect() as connection:
            return tuple(connection.execute(
                f"SELECT COUNT(*) FROM {table}",
            ).fetchone()[0] for table in (
                "delivery_records", "delivery_attempts",
            ))

    def test_temporal_trace_notifies_only_two_new_distributions(self):
        with tempfile.TemporaryDirectory() as root:
            (repository, provider, _conditions, app, clock, committed, _proposal,
             adapter, _service, _evidence) = self.prepare(root)
            results = [app.run_condition_once()]
            for price in (760, 750, 900, 780):
                clock.advance(hours=6)
                provider.price = price
                results.append(app.tick_condition_observations()[0])
            self.assertEqual(
                [item.notification_status for item in results],
                [None, "SENT", None, None, "SENT"],
            )
            self.assertEqual(len(adapter.calls), 2)
            self.assertEqual(self.delivery_counts(repository), (2, 2))
            updates = repository.list_tracking_updates(
                USER, committed.subscription_id,
            )
            self.assertEqual(
                sorted(item.payload["observed_price"] for item in updates),
                [760, 780],
            )

    def test_duplicate_tick_and_distribution_do_not_redispatch(self):
        with tempfile.TemporaryDirectory() as root:
            (repository, provider, _conditions, app, clock, _committed, _proposal,
             adapter, service, _evidence) = self.prepare(root, price=760)
            provider.source_signal_id = "stable-notification-fact"
            provider.observed_at = clock()
            first = app.run_condition_once()
            self.assertEqual(first.notification_status, "SENT")
            distribution_id = first.distribution_id
            first_record = repository.get_delivery_for_distribution(
                distribution_id, "termux_notification",
            )
            duplicate = service.deliver_distribution(USER, distribution_id)
            self.assertEqual(duplicate, first_record)
            clock.advance(hours=6)
            repeated_fact = app.tick_condition_observations()[0]
            self.assertEqual(
                (repeated_fact.worker_status,
                 repeated_fact.emission_decision),
                ("REUSED", "DUPLICATE_OBSERVATION"),
            )
            self.assertEqual(len(adapter.calls), 1)
            self.assertEqual(self.delivery_counts(repository), (1, 1))

    def test_pending_intent_recovers_after_process_restart_once(self):
        with tempfile.TemporaryDirectory() as root:
            (repository, _provider, _conditions, app, clock, _committed,
             _proposal, _adapter, _service, _evidence) = self.prepare(
                root, price=760, attach=False,
            )
            work = app.run_condition_once()
            distribution = repository.get_update_distribution(
                work.distribution_id,
            )
            logical_id = distribution_notification_identity(
                distribution.distribution_id, "termux_notification",
            )
            pending = DeliveryRecord(
                logical_id, delivery_attempt_identity(logical_id, 1), None,
                USER, "termux_notification", "pending", 1, None, clock(),
                None, None, "not_started", distribution.distribution_id,
            )
            repository.reserve_delivery(pending)

            reopened = SQLiteDigestRepository(repository.path)
            adapter = FakeDeliveryAdapter(
                channel="termux_notification",
            )
            service = DeliveryService(
                reopened, [adapter], clock=clock,
                evidence_store=EvidenceStore(os.path.join(
                    root, "notification-evidence",
                )),
            )
            app.repository = reopened
            app.deliveries = service
            self.assertEqual(
                app.drain_distribution_notifications(),
                ((distribution.distribution_id, "SENT"),),
            )
            self.assertEqual(len(adapter.calls), 1)
            self.assertEqual(app.drain_distribution_notifications(), ())
            self.assertEqual(len(adapter.calls), 1)

    def test_accepted_persists_safe_evidence_without_seen_claim(self):
        with tempfile.TemporaryDirectory() as root:
            (repository, _provider, _conditions, app, _clock, _committed,
             _proposal, adapter, _service, evidence) = self.prepare(
                root, price=760,
            )
            work = app.run_condition_once()
            record = repository.get_delivery_for_distribution(
                work.distribution_id, "termux_notification",
            )
            self.assertEqual(
                (record.status, record.effect_certainty,
                 record.attempt_number, len(adapter.calls)),
                ("accepted", "known_applied", 1, 1),
            )
            accepted = evidence.load(record.evidence_id)
            self.assertEqual(
                accepted["content_identity"]["safe_observation"],
                {"notification_requested": True, "request_accepted": True},
            )
            rendered = str(accepted).lower()
            self.assertNotIn("stdout", rendered)
            self.assertNotIn("stderr", rendered)
            self.assertNotIn("user_seen", rendered)
            self.assertNotIn("user_read", rendered)

    def test_explicit_failure_allows_only_one_explicit_retry(self):
        with tempfile.TemporaryDirectory() as root:
            (repository, _provider, _conditions, app, _clock, _committed,
             _proposal, adapter, service, _evidence) = self.prepare(
                root, price=760, mode="explicit_failure",
            )
            work = app.run_condition_once()
            first = repository.get_delivery_for_distribution(
                work.distribution_id, "termux_notification",
            )
            self.assertEqual(
                (first.status, first.effect_certainty),
                ("failed", "not_started"),
            )
            second = service.retry_delivery(first.delivery_id)
            self.assertEqual(
                (second.status, second.attempt_number, len(adapter.calls)),
                ("failed", 2, 2),
            )
            with self.assertRaises(DomainError):
                service.retry_delivery(second.delivery_id)
            self.assertEqual(len(adapter.calls), 2)

    def test_unknown_survives_restart_and_never_blind_retries(self):
        with tempfile.TemporaryDirectory() as root:
            (repository, _provider, _conditions, app, clock, _committed,
             _proposal, adapter, _service, _evidence) = self.prepare(
                root, price=760, mode="timeout_unknown",
            )
            work = app.run_condition_once()
            first = repository.get_delivery_for_distribution(
                work.distribution_id, "termux_notification",
            )
            self.assertEqual(
                (first.status, first.effect_certainty, len(adapter.calls)),
                ("unknown", "unknown", 1),
            )
            reopened = SQLiteDigestRepository(repository.path)
            next_adapter = FakeDeliveryAdapter(
                channel="termux_notification",
            )
            app.repository = reopened
            app.deliveries = DeliveryService(
                reopened, [next_adapter], clock=clock,
                evidence_store=EvidenceStore(os.path.join(
                    root, "notification-evidence",
                )),
            )
            self.assertEqual(app.drain_distribution_notifications(), ())
            self.assertEqual(len(next_adapter.calls), 0)

    def test_paused_and_feed_only_distributions_are_not_eligible(self):
        with tempfile.TemporaryDirectory() as root:
            (repository, _provider, _conditions, app, _clock, committed,
             _proposal, adapter, service, _evidence) = self.prepare(
                root, price=760, attach=False,
            )
            work = app.run_condition_once()
            app.disable_subscription(USER, committed.subscription_id, 1)
            app.deliveries = service
            self.assertEqual(app.drain_distribution_notifications(), ())
            self.assertEqual(len(adapter.calls), 0)
            self.assertIsNotNone(repository.get_tracking_update(work.update_id))

        with tempfile.TemporaryDirectory() as root:
            (_repository, _provider, _conditions, app, _clock, _committed,
             _proposal, adapter, _service, _evidence) = self.prepare(
                root, price=760, notify=False,
            )
            work = app.run_condition_once()
            self.assertEqual(work.notification_status, "NOT_REQUESTED")
            self.assertEqual(len(adapter.calls), 0)

    def test_notification_failure_does_not_mutate_feed_update_or_distribution(self):
        with tempfile.TemporaryDirectory() as root:
            (repository, _provider, _conditions, app, _clock, committed,
             _proposal, _adapter, _service, _evidence) = self.prepare(
                root, price=760, mode="explicit_failure",
            )
            work = app.run_condition_once()
            update_before = repository.get_tracking_update(work.update_id)
            distribution_before = repository.get_update_distribution(
                work.distribution_id,
            )
            detail = app.get_feed_detail(USER, committed.subscription_id)
            self.assertEqual(
                (detail.update_state, len(detail.history),
                 detail.history[0].notification_status,
                 detail.history[0].notification_message),
                ("ready", 1, "UNAVAILABLE",
                 "通知暂不可用；这条更新仍可在这里查看。"),
            )
            self.assertEqual(
                repository.get_tracking_update(work.update_id), update_before,
            )
            self.assertEqual(
                repository.get_update_distribution(work.distribution_id),
                distribution_before,
            )


if __name__ == "__main__":
    unittest.main()
