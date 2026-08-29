import os
import tempfile
import threading
import unittest

from apps.digest_agent.activation import SubscriptionActivationService
from apps.digest_agent.adapters.definition import FakeDefinitionAgentAdapter
from apps.digest_agent.adapters.flight import FakeClock, FakeFlightPriceProvider
from apps.digest_agent.adapters.sqlite import SQLiteDigestRepository
from apps.digest_agent.application import ApplicationError, DigestApplication
from apps.digest_agent.conditions import build_flight_condition_service
from apps.digest_agent.conversation import DefinitionConversationWorkflow
from apps.digest_agent.services import SubscriptionService


USER = "a" * 32
START = "2026-08-27T12:00:00Z"


class IdFactory:
    def __init__(self, start=1):
        self.value = start
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            value = f"{self.value:032x}"
            self.value += 1
            return value


def flight_done(preferences=None):
    return {
        "protocol_version": 2,
        "type": "DONE",
        "intent": {
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
            "preferences": preferences or {},
        },
    }


class ContinuousFlightConditionTests(unittest.TestCase):
    def prepare(self, root, *, price=920, outcome=None,
                condition_fault=None, provider=None, message=None):
        clock = FakeClock(START)
        repository = SQLiteDigestRepository(os.path.join(root, "digest.db"))
        workflow = DefinitionConversationWorkflow(
            repository, FakeDefinitionAgentAdapter([outcome or flight_done()]),
            os.path.join(root, "audit"), id_factory=IdFactory(1),
            clock=clock, owner_id="f" * 32,
        )
        activation = SubscriptionActivationService(
            repository, id_factory=IdFactory(100), clock=clock,
        )
        provider = provider or FakeFlightPriceProvider(price, clock=clock)
        conditions = build_flight_condition_service(
            repository, provider, os.path.join(root, "audit"), clock=clock,
            fault_injector=condition_fault,
        )
        app = DigestApplication(
            repository, SubscriptionService(repository, clock=clock),
            None, None, None, conversation_workflow=workflow,
            activation_service=activation, condition_service=conditions,
        )
        proposal = app.start_subscription_conversation(
            USER, message or (
                "持续关注深圳—武汉 9 月往返机票，低于 800 元提醒我。"
            ),
            "continuous-flight",
        )
        committed = app.commit_subscription_from_definition(
            USER, proposal.conversation_id,
        )
        return repository, provider, conditions, app, clock, committed, proposal

    @staticmethod
    def counts(repository):
        with repository.connect() as connection:
            return tuple(connection.execute(
                f"SELECT COUNT(*) FROM {table}",
            ).fetchone()[0] for table in (
                "condition_observation_cycles", "condition_evaluations",
                "tracking_updates", "update_distributions",
            ))

    def test_approved_trace_emits_only_760_and_rearmed_780(self):
        with tempfile.TemporaryDirectory() as root:
            repository, provider, _conditions, app, clock, committed, _ = (
                self.prepare(root)
            )
            results = [app.run_condition_once()]
            for price in (760, 750, 900, 780):
                clock.advance(hours=6)
                provider.price = price
                work = app.tick_condition_observations()
                self.assertEqual(len(work), 1)
                results.append(work[0])
            self.assertEqual(
                [(item.worker_status, item.emission_decision)
                 for item in results],
                [
                    ("NO_UPDATE", "SUPPRESS_FALSE"),
                    ("UPDATE_CREATED", "EMIT_THRESHOLD_CROSSING"),
                    ("NO_UPDATE", "SUPPRESS_STILL_MATCHED"),
                    ("NO_UPDATE", "SUPPRESS_REARMED"),
                    ("UPDATE_CREATED", "EMIT_THRESHOLD_CROSSING"),
                ],
            )
            self.assertEqual(self.counts(repository), (5, 5, 2, 2))
            updates = repository.list_tracking_updates(
                USER, committed.subscription_id,
            )
            self.assertEqual(
                [item.payload["observed_price"] for item in updates],
                [780, 760],
            )
            state = repository.get_condition_temporal_state(
                committed.subscription_id,
            )
            self.assertEqual((state.previous_truth, state.armed), ("TRUE", False))

    def test_explicit_cadence_overrides_product_default(self):
        with tempfile.TemporaryDirectory() as root:
            outcome = flight_done({
                "cadence": {"value": "12h", "source_turn": 1},
            })
            repository, _provider, _conditions, _app, _clock, committed, proposal = (
                self.prepare(
                    root, outcome=outcome,
                    message=(
                        "持续关注深圳—武汉 9 月往返机票，每 12 小时检查，"
                        "低于 800 元提醒我。"
                    ),
                )
            )
            self.assertEqual(
                (proposal.definition["cadence"],
                 proposal.definition["provenance"]["cadence"]),
                ("12h", "USER_EXPLICIT"),
            )
            state = repository.get_condition_temporal_state(
                committed.subscription_id,
            )
            self.assertEqual(
                (state.cadence_seconds, state.cadence_provenance,
                 state.next_due_at),
                (43200, "USER_EXPLICIT", "2026-08-28T00:00:00Z"),
            )

    def test_first_observation_760_emits_immediately_once(self):
        with tempfile.TemporaryDirectory() as root:
            repository, _provider, _conditions, app, _clock, committed, _ = (
                self.prepare(root, price=760)
            )
            result = app.run_condition_once()
            self.assertEqual(
                (result.worker_status, result.emission_decision),
                ("UPDATE_CREATED", "EMIT_FIRST_MATCH"),
            )
            self.assertEqual(self.counts(repository), (1, 1, 1, 1))
            self.assertEqual(
                app.get_feed_detail(USER, committed.subscription_id)
                .condition_monitoring.status,
                "MATCHED",
            )

    def test_duplicate_and_out_of_order_do_not_change_latch_or_emit(self):
        with tempfile.TemporaryDirectory() as root:
            repository, provider, _conditions, app, clock, committed, _ = (
                self.prepare(root, price=760)
            )
            provider.source_signal_id = "stable-provider-fact"
            provider.observed_at = clock()
            first = app.run_condition_once()
            clock.advance(hours=6)
            duplicate = app.tick_condition_observations()[0]
            self.assertEqual(
                (duplicate.worker_status, duplicate.emission_decision,
                 duplicate.update_id),
                ("REUSED", "DUPLICATE_OBSERVATION", first.update_id),
            )
            self.assertEqual(self.counts(repository), (2, 1, 1, 1))
            provider.source_signal_id = "older-provider-fact"
            provider.observed_at = "2026-08-27T11:59:00Z"
            clock.advance(hours=6)
            failed = app.tick_condition_observations()[0]
            self.assertEqual(
                (failed.worker_status, failed.failure_reason),
                ("FAILED", "OUT_OF_ORDER_OBSERVATION"),
            )
            state = repository.get_condition_temporal_state(
                committed.subscription_id,
            )
            self.assertEqual((state.previous_truth, state.armed), ("TRUE", False))
            self.assertEqual(self.counts(repository)[2:], (1, 1))

    def test_failed_cycle_continues_next_cadence(self):
        class TimeoutOnceProvider(FakeFlightPriceProvider):
            def __init__(self, clock):
                super().__init__(920, clock=clock)
                self.failed = False

            def observe(self, query):
                if not self.failed:
                    self.failed = True
                    raise TimeoutError("synthetic timeout")
                return super().observe(query)

        with tempfile.TemporaryDirectory() as root:
            clock = FakeClock(START)
            provider = TimeoutOnceProvider(clock)
            (repository, _provider, _conditions, app, real_clock,
             committed, _) = self.prepare(root, provider=provider)
            provider.clock = real_clock
            first = app.run_condition_once()
            self.assertEqual(
                (first.worker_status, first.failure_reason),
                ("FAILED", "PROVIDER_TIMEOUT"),
            )
            self.assertEqual(
                repository.get_condition_temporal_state(
                    committed.subscription_id,
                ).lifecycle_status,
                "ACTIVE",
            )
            real_clock.advance(hours=6)
            second = app.tick_condition_observations()[0]
            self.assertEqual(second.worker_status, "NO_UPDATE")

    def test_pause_resume_preserves_latch_and_does_not_backfill(self):
        with tempfile.TemporaryDirectory() as root:
            repository, provider, _conditions, app, clock, committed, _ = (
                self.prepare(root, price=760)
            )
            app.run_condition_once()
            paused = app.disable_subscription(
                USER, committed.subscription_id, 1,
            )
            self.assertEqual(paused.product_status, "PAUSED")
            calls = len(provider.calls)
            clock.advance(hours=18)
            self.assertEqual(app.tick_condition_observations(), ())
            self.assertEqual(len(provider.calls), calls)
            resumed = app.enable_subscription(
                USER, committed.subscription_id, 2,
            )
            self.assertEqual(resumed.product_status, "ACTIVE")
            provider.price = 750
            result = app.run_condition_once()
            self.assertEqual(
                (result.worker_status, result.emission_decision),
                ("NO_UPDATE", "SUPPRESS_STILL_MATCHED"),
            )
            cycles = repository.list_condition_cycles(
                committed.subscription_id,
            )
            self.assertEqual([item.cycle_kind for item in cycles],
                             ["INITIAL", "RESUME"])
            self.assertEqual(self.counts(repository)[2:], (1, 1))

    def test_eight_hour_downtime_runs_only_one_catch_up_cycle(self):
        with tempfile.TemporaryDirectory() as root:
            repository, provider, _conditions, app, clock, committed, _ = (
                self.prepare(root)
            )
            app.run_condition_once()
            calls = len(provider.calls)
            clock.advance(hours=8)
            work = app.tick_condition_observations()
            self.assertEqual(len(work), 1)
            self.assertEqual(len(provider.calls), calls + 1)
            catch_up = repository.list_condition_cycles(
                committed.subscription_id,
            )[-1]
            self.assertEqual(
                (catch_up.cycle_kind, catch_up.coalesced_count,
                 catch_up.scheduled_due_at),
                ("CATCH_UP", 1, "2026-08-27T18:00:00Z"),
            )

    def test_twenty_hour_downtime_coalesces_three_slots_to_one_cycle(self):
        with tempfile.TemporaryDirectory() as root:
            repository, provider, _conditions, app, clock, committed, _ = (
                self.prepare(root)
            )
            app.run_condition_once()
            calls = len(provider.calls)
            clock.advance(hours=20)
            work = app.tick_condition_observations()
            self.assertEqual(len(work), 1)
            self.assertEqual(len(provider.calls), calls + 1)
            cycles = repository.list_condition_cycles(
                committed.subscription_id,
            )
            catch_up = cycles[-1]
            self.assertEqual(
                (catch_up.cycle_kind, catch_up.coalesced_count),
                ("CATCH_UP", 3),
            )
            state = repository.get_condition_temporal_state(
                committed.subscription_id,
            )
            self.assertEqual(state.next_due_at, "2026-08-28T12:00:00Z")

    def test_window_expiry_is_completed_and_cannot_resume(self):
        with tempfile.TemporaryDirectory() as root:
            repository, provider, _conditions, app, clock, committed, _ = (
                self.prepare(root)
            )
            app.run_condition_once()
            calls = len(provider.calls)
            clock.set("2026-09-30T16:00:00Z")
            self.assertEqual(app.tick_condition_observations(), ())
            state = repository.get_condition_temporal_state(
                committed.subscription_id,
            )
            self.assertEqual(
                (state.lifecycle_status, state.completion_reason),
                ("COMPLETED", "TIME_WINDOW_ENDED"),
            )
            detail = app.get_feed_detail(USER, committed.subscription_id)
            self.assertEqual(
                (detail.feed_state, detail.condition_monitoring.status),
                ("completed", "COMPLETED"),
            )
            self.assertEqual(len(provider.calls), calls)
            with self.assertRaises(ApplicationError) as raised:
                app.enable_subscription(USER, committed.subscription_id, 2)
            self.assertEqual(raised.exception.code, "condition_completed")

    def test_claim_recovery_and_concurrent_workers_are_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            repository, provider, conditions, app, clock, committed, _ = (
                self.prepare(root)
            )
            barrier = threading.Barrier(3)
            results = []

            def worker():
                barrier.wait()
                results.append(app.run_condition_once())

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join()
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(
                sorted(item.worker_status for item in results),
                ["NO_UPDATE", "NO_WORK"],
            )

            clock.advance(hours=6)
            conditions.plan_due_cycles()
            claimed = repository.claim_condition_cycle(
                "b" * 32, clock(), "2026-01-01T00:00:00Z",
            )
            self.assertIsNotNone(claimed)
            clock.advance(minutes=6)
            recovered = app.run_condition_once()
            self.assertEqual(recovered.worker_status, "NO_UPDATE")
            self.assertEqual(self.counts(repository)[2:], (0, 0))

    def test_finalize_crash_rolls_back_and_same_cycle_recovers_once(self):
        def crash(stage, _evaluation):
            if stage == "after_distribution":
                raise RuntimeError("synthetic finalize crash")

        with tempfile.TemporaryDirectory() as root:
            repository, provider, conditions, app, _clock, committed, _ = (
                self.prepare(root, price=760, condition_fault=crash)
            )
            with self.assertRaisesRegex(RuntimeError, "finalize crash"):
                app.run_condition_once()
            self.assertEqual(self.counts(repository), (1, 0, 0, 0))
            cycle = repository.list_condition_cycles(
                committed.subscription_id,
            )[0]
            self.assertEqual(cycle.status, "PENDING")

            conditions.fault_injector = None
            recovered = app.run_condition_once()
            self.assertEqual(
                (recovered.worker_status, recovered.emission_decision),
                ("UPDATE_CREATED", "EMIT_FIRST_MATCH"),
            )
            self.assertEqual(len(provider.calls), 2)
            self.assertEqual(self.counts(repository), (1, 1, 1, 1))


if __name__ == "__main__":
    unittest.main()
