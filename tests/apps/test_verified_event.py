import os
import tempfile
import threading
import unittest

from apps.digest_agent.activation import SubscriptionActivationService
from apps.digest_agent.adapters.definition import FakeDefinitionAgentAdapter
from apps.digest_agent.adapters.delivery import FakeDeliveryAdapter
from apps.digest_agent.adapters.event import (
    FakeEventCandidateAgent, FakeOpenAIEventSource,
)
from apps.digest_agent.adapters.flight import FakeClock
from apps.digest_agent.adapters.sqlite import SQLiteDigestRepository
from apps.digest_agent.application import DigestApplication
from apps.digest_agent.conversation import DefinitionConversationWorkflow
from apps.digest_agent.events import build_verified_event_service
from apps.digest_agent.services import SubscriptionService, build_delivery_service


USER = "a" * 32
START = "2026-08-29T08:00:00Z"


class IdFactory:
    def __init__(self, start=1):
        self.value = start
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            value = f"{self.value:032x}"
            self.value += 1
            return value


def official(model, published=START, *, extra=()):
    return {
        "retrieved_at": published,
        "results": [{
            "source_ref": "official",
            "canonical_url": f"https://openai.com/index/{model.casefold().replace(' ', '-')}",
            "publisher": "OpenAI", "source_kind": "official_primary",
            "title": f"OpenAI released {model}",
            "snippet": f"{model} is now available.",
            "published_at": published,
        }, *extra],
    }


def third_party(model, published=START):
    return {
        "retrieved_at": published,
        "results": [{
            "source_ref": "report",
            "canonical_url": "https://news.example/model",
            "publisher": "Example News", "source_kind": "secondary",
            "title": f"OpenAI released {model}",
            "snippet": f"{model} is now available.",
            "published_at": published,
        }],
    }


class VerifiedEventTests(unittest.TestCase):
    def prepare(self, root, fixtures=(), *, notify=False, fault=None):
        clock = FakeClock(START)
        repository = SQLiteDigestRepository(os.path.join(root, "digest.db"))
        definition = FakeDefinitionAgentAdapter()
        workflow = DefinitionConversationWorkflow(
            repository, definition, os.path.join(root, "audit"),
            id_factory=IdFactory(1), clock=clock, owner_id="f" * 32,
        )
        activation = SubscriptionActivationService(
            repository, id_factory=IdFactory(100), clock=clock,
        )
        source = FakeOpenAIEventSource(fixtures, clock=clock)
        candidate_agent = FakeEventCandidateAgent()
        events = build_verified_event_service(
            repository, source, candidate_agent, os.path.join(root, "audit"),
            clock=clock, fault_injector=fault,
        )
        adapter = FakeDeliveryAdapter(channel="termux_notification")
        deliveries = build_delivery_service(
            repository, [adapter], os.path.join(root, "audit"), clock=clock,
        )
        app = DigestApplication(
            repository, SubscriptionService(repository, clock=clock),
            None, deliveries, None, conversation_workflow=workflow,
            activation_service=activation, event_service=events,
        )
        request = "OpenAI 发布新模型时告诉我。"
        if notify:
            request += " 用本机通知提醒我。"
        proposal = app.start_subscription_conversation(USER, request, "event")
        committed = app.commit_subscription_from_definition(
            USER, proposal.conversation_id,
        )
        return repository, source, candidate_agent, adapter, app, clock, committed

    @staticmethod
    def counts(repository):
        with repository.connect() as connection:
            return {
                name: connection.execute(
                    f"SELECT COUNT(*) FROM {name}",
                ).fetchone()[0]
                for name in (
                    "event_source_observations", "event_candidates",
                    "event_verifications", "verified_events",
                    "tracking_updates", "update_distributions",
                    "briefing_reservations", "application_outbox",
                )
            }

    def test_no_event_is_successful_no_update_and_never_briefing(self):
        with tempfile.TemporaryDirectory() as root:
            repository, _source, _agent, _adapter, app, _clock, committed = (
                self.prepare(root, [{}])
            )
            result = app.tick_event_observations()
            self.assertEqual(
                [(item["worker_status"], item["outcome"], item["reason_code"])
                 for item in result],
                [("NO_UPDATE", "NO_UPDATE", "NO_EVENT_FOUND")],
            )
            counts = self.counts(repository)
            self.assertEqual((counts["tracking_updates"], counts["update_distributions"]), (0, 0))
            self.assertEqual((counts["briefing_reservations"], counts["application_outbox"]), (0, 0))
            detail = app.get_feed_detail(USER, committed.subscription_id)
            self.assertEqual(
                (detail.workflow_kind, detail.event_monitoring.status,
                 detail.event_monitoring.message),
                ("EVENT", "NO_UPDATE", "暂无新动态。"),
            )

    def test_model_x_duplicate_then_model_y_commits_once_each(self):
        with tempfile.TemporaryDirectory() as root:
            repository, source, agent, adapter, app, clock, committed = (
                self.prepare(root, [official("Model X")], notify=True)
            )
            first = app.tick_event_observations()[0]
            clock.advance(hours=6)
            source.enqueue(official("Model X", clock()))
            duplicate = app.tick_event_observations()[0]
            clock.advance(hours=6)
            source.enqueue(official("Model Y", clock()))
            second = app.tick_event_observations()[0]
            self.assertEqual(
                [(item["worker_status"], item["reason_code"])
                 for item in (first, duplicate, second)],
                [("UPDATE_CREATED", "VERIFIED_NEW_EVENT"),
                 ("NO_UPDATE", "DUPLICATE_VERIFIED_EVENT"),
                 ("UPDATE_CREATED", "VERIFIED_NEW_EVENT")],
            )
            counts = self.counts(repository)
            self.assertEqual(
                (counts["verified_events"], counts["tracking_updates"],
                 counts["update_distributions"]), (2, 2, 2),
            )
            self.assertEqual(len(adapter.calls), 2)
            self.assertEqual(len(agent.calls), 3)
            detail = app.get_feed_detail(USER, committed.subscription_id)
            self.assertEqual(
                [item.model_name for item in detail.history],
                ["Model Y", "Model X"],
            )

    def test_multiple_sources_same_model_are_one_logical_event(self):
        with tempfile.TemporaryDirectory() as root:
            extra = ({
                "source_ref": "report", "canonical_url": "https://news.example/x",
                "publisher": "Example News", "source_kind": "secondary",
                "title": "OpenAI released Model X",
                "snippet": "Model X is now available.",
                "published_at": START,
            },)
            repository, *_ = self.prepare(root, [official("Model X", extra=extra)])
            app = _[-3]
            result = app.tick_event_observations()
            self.assertEqual(result[0]["worker_status"], "UPDATE_CREATED")
            self.assertEqual(self.counts(repository)["verified_events"], 1)

    def test_missing_official_conflict_and_truncation_are_incomplete(self):
        cases = [
            (third_party("Model X"), "INSUFFICIENT_OFFICIAL_SUPPORT"),
            ({
                "retrieved_at": START,
                "results": [{
                    "source_ref": "official",
                    "canonical_url": "https://openai.com/index/model-x",
                    "publisher": "OpenAI", "source_kind": "official_primary",
                    "title": "OpenAI released Model X",
                    "snippet": "Model X is now available.",
                    "published_at": None,
                }],
            }, "SOURCE_TIME_UNCONFIRMED"),
            (official("Model X", extra=({
                "source_ref": "conflict",
                "canonical_url": "https://news.example/conflict",
                "publisher": "Example News", "source_kind": "secondary",
                "title": "Model X not released",
                "snippet": "The report is unconfirmed.",
                "published_at": START,
            },)), "CONFLICTING_EVIDENCE"),
            ({"coverage_complete": False, "truncated": True},
             "COVERAGE_INCOMPLETE"),
        ]
        for fixture, reason in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as root:
                repository, _source, _agent, adapter, app, _clock, _commit = (
                    self.prepare(root, [fixture], notify=True)
                )
                result = app.tick_event_observations()[0]
                self.assertEqual(
                    (result["worker_status"], result["outcome"], result["reason_code"]),
                    ("VERIFICATION_INCOMPLETE", "VERIFICATION_INCOMPLETE", reason),
                )
                self.assertEqual(self.counts(repository)["tracking_updates"], 0)
                self.assertEqual(len(adapter.calls), 0)

    def test_concurrent_workers_claim_one_cycle_and_commit_once(self):
        with tempfile.TemporaryDirectory() as root:
            repository, source, agent, adapter, app, clock, committed = (
                self.prepare(root, [official("Model X")], notify=True)
            )
            second = build_verified_event_service(
                repository, source, agent, os.path.join(root, "audit"), clock=clock,
            )
            barrier = threading.Barrier(2)
            values = []
            errors = []

            def run(service):
                try:
                    barrier.wait(5)
                    values.append(service.run_once())
                except Exception as error:  # pragma: no cover - assertion aid
                    errors.append(error)

            threads = [threading.Thread(target=run, args=(service,))
                       for service in (app.events, second)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            self.assertEqual(errors, [])
            self.assertEqual(
                sorted(item.worker_status for item in values),
                ["NO_WORK", "UPDATE_CREATED"],
            )
            app.drain_distribution_notifications()
            self.assertEqual(len(adapter.calls), 1)
            self.assertEqual(
                (self.counts(repository)["tracking_updates"],
                 self.counts(repository)["update_distributions"]), (1, 1),
            )

    def test_restart_reuses_durable_result_and_duplicate_cycle_is_safe(self):
        class CrashOnce:
            def __init__(self):
                self.called = False

            def __call__(self, stage, _value):
                if stage == "after_verification_evidence" and not self.called:
                    self.called = True
                    raise RuntimeError("crash")

        with tempfile.TemporaryDirectory() as root:
            crash = CrashOnce()
            repository, source, agent, adapter, app, clock, committed = (
                self.prepare(root, [official("Model X")], notify=True, fault=crash)
            )
            with self.assertRaisesRegex(RuntimeError, "crash"):
                app.tick_event_observations()
            self.assertEqual(len(agent.calls), 1)
            restarted = build_verified_event_service(
                repository, source, agent, os.path.join(root, "audit"), clock=clock,
            )
            app.events = restarted
            source.enqueue(official("Model X"))
            result = app.tick_event_observations()
            self.assertEqual(result[0]["worker_status"], "UPDATE_CREATED")
            self.assertEqual(len(agent.calls), 1)
            self.assertEqual(len(adapter.calls), 1)
            self.assertEqual(
                (self.counts(repository)["tracking_updates"],
                 self.counts(repository)["update_distributions"]), (1, 1),
            )

    def test_pause_resume_coalesces_without_backfill(self):
        with tempfile.TemporaryDirectory() as root:
            repository, source, _agent, _adapter, app, clock, committed = (
                self.prepare(root, [{}])
            )
            app.tick_event_observations()
            paused = app.disable_subscription(
                USER, committed.subscription_id, 1,
            )
            clock.advance(hours=18)
            source.enqueue(official("Model X", clock()))
            self.assertEqual(app.tick_event_observations(), ())
            resumed = app.enable_subscription(
                USER, committed.subscription_id, paused.version,
            )
            result = app.tick_event_observations()
            self.assertEqual(result[0]["worker_status"], "UPDATE_CREATED")
            state = repository.get_event_temporal_state(committed.subscription_id)
            self.assertEqual((resumed.enabled, state.lifecycle_status), (True, "ACTIVE"))
            cycles = []
            with repository.connect() as connection:
                cycles = connection.execute("""
                    SELECT cycle_kind, coalesced_count FROM event_observation_cycles
                    ORDER BY created_at, cycle_id
                """).fetchall()
            self.assertEqual(cycles[-1][0], "RESUME")
            self.assertTrue(all(row[1] == 1 for row in cycles))


if __name__ == "__main__":
    unittest.main()
