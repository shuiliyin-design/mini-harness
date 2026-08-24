import json
import os
import tempfile
import threading
import unittest

from apps.digest_agent.activation import SubscriptionActivationService
from apps.digest_agent.adapters.definition import FakeDefinitionAgentAdapter
from apps.digest_agent.adapters.provider import FakeDigestProvider
from apps.digest_agent.adapters.search import FakeSearchClient
from apps.digest_agent.adapters.sqlite import SQLiteDigestRepository
from apps.digest_agent.application import DigestApplication
from apps.digest_agent.conversation import DefinitionConversationWorkflow
from apps.digest_agent.outbox import DurableOutboxWorker
from apps.digest_agent.workflows import DigestGenerationWorkflow
from tools.async_first_briefing_smoke import _reserved_identity_reused


NOW = "2026-08-24T12:00:00Z"
USER = "a" * 32


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


def search_rows():
    return [{
        "url": "https://example.test/agent",
        "title": "Agent Runtime 发布",
        "snippet": "一个新的 Agent Runtime 与开发工具发布。",
        "published_at": "2026-08-24T10:00:00Z",
        "topic_tags": ["AI 行业动态", "Agent"],
    }]


class IdFactory:
    def __init__(self, start):
        self.value = start
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            value = f"{self.value:032x}"
            self.value += 1
            return value


class RaiseOnce:
    def __init__(self, stage):
        self.stage = stage
        self.raised = False

    def __call__(self, stage, _value):
        if stage == self.stage and not self.raised:
            self.raised = True
            raise RuntimeError(stage)


class OutboxWorkerTests(unittest.TestCase):
    def prepare(self, root, *, rows=None, provider_mode="valid",
                workflow_fault=None, worker_fault=None):
        repository = SQLiteDigestRepository(os.path.join(root, "digest.db"))
        conversation = DefinitionConversationWorkflow(
            repository, FakeDefinitionAgentAdapter([done()]),
            os.path.join(root, "audit"), id_factory=IdFactory(1),
            clock=lambda: NOW, owner_id="f" * 32,
        )
        activation = SubscriptionActivationService(
            repository, id_factory=IdFactory(100), clock=lambda: NOW,
        )
        search = FakeSearchClient(search_rows() if rows is None else rows)
        provider = FakeDigestProvider(provider_mode)
        generation = DigestGenerationWorkflow(
            repository, search, provider, os.path.join(root, "workspace"),
            os.path.join(root, "audit"), id_factory=IdFactory(300),
            clock=lambda: NOW, fault_injector=workflow_fault,
        )
        worker = DurableOutboxWorker(
            repository, generation, clock=lambda: NOW,
            fault_injector=worker_fault,
        )
        app = DigestApplication(
            repository, None, generation, None, None,
            conversation, activation, worker,
        )
        conversation_view = app.start_subscription_conversation(
            USER, "帮我订阅 AI 行业动态，每篇 600 字以内", "start",
        )
        committed = app.commit_subscription_from_definition(
            USER, conversation_view.conversation_id,
        )
        outbox = repository.get_application_outbox_for_run(
            committed.first_briefing_application_run_id,
        )
        return repository, search, provider, generation, worker, app, committed, outbox

    def test_happy_path_reuses_reserved_run_and_duplicate_entry_has_no_work(self):
        with tempfile.TemporaryDirectory() as root:
            (repository, search, provider, _generation, _worker, app,
             committed, outbox) = self.prepare(root)
            before = app.get_first_briefing(USER, committed.subscription_id)
            self.assertEqual(
                (committed.status, committed.first_briefing_status,
                 before.status, repository.list_digests(USER)),
                ("ACTIVE", "PENDING", "PENDING", ()),
            )
            result = app.run_outbox_once()
            self.assertEqual(
                (result.worker_status, result.outbox_status,
                 result.first_briefing_status, result.application_run_id),
                ("PROCESSED", "SUCCEEDED", "READY",
                 committed.first_briefing_application_run_id),
            )
            run = repository.get_digest_run(result.application_run_id)
            reservation = repository.get_briefing_reservation(
                result.application_run_id,
            )
            self.assertTrue(_reserved_identity_reused(reservation, run))
            self.assertEqual(reservation.harness_run_id, run.harness_run_id)
            self.assertEqual((len(search.calls), len(provider.calls)), (1, 1))
            self.assertEqual(len(repository.list_digests(USER)), 1)
            self.assertEqual(app.run_outbox_once().worker_status, "NO_WORK")
            self.assertEqual((len(search.calls), len(provider.calls)), (1, 1))
            self.assertEqual(
                repository.get_application_outbox(outbox.outbox_id).status,
                "completed",
            )

    def test_concurrent_ticks_claim_once_and_call_external_dependencies_once(self):
        with tempfile.TemporaryDirectory() as root:
            entered = threading.Event()
            release = threading.Event()

            def pause(stage, _value):
                if stage == "after_claim":
                    entered.set()
                    release.wait(5)

            (repository, search, provider, generation, _worker, _app,
             _committed, outbox) = self.prepare(root, worker_fault=pause)
            first_worker = DurableOutboxWorker(
                repository, generation, clock=lambda: NOW,
                fault_injector=pause,
            )
            second_worker = DurableOutboxWorker(
                repository, generation, clock=lambda: NOW,
            )
            values = []
            thread = threading.Thread(target=lambda: values.append(
                first_worker.run_once()
            ))
            thread.start()
            self.assertTrue(entered.wait(5))
            second = second_worker.run_once()
            release.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(second.worker_status, "NO_WORK")
            self.assertEqual(values[0].worker_status, "PROCESSED")
            self.assertEqual((len(search.calls), len(provider.calls)), (1, 1))
            self.assertEqual(len(repository.list_digests(USER)), 1)
            self.assertEqual(
                repository.get_application_outbox(outbox.outbox_id).status,
                "completed",
            )

    def test_claim_then_crash_fails_closed_until_explicit_release(self):
        with tempfile.TemporaryDirectory() as root:
            fault = RaiseOnce("after_claim")
            (repository, search, provider, generation, worker, _app,
             committed, outbox) = self.prepare(root, worker_fault=fault)
            with self.assertRaisesRegex(RuntimeError, "after_claim"):
                worker.run_once()
            self.assertEqual(worker.run_once().worker_status, "NO_WORK")
            facts = worker.inspect(outbox.outbox_id)
            self.assertEqual(
                (facts.outbox_status, facts.safe_recovery_actions),
                ("CLAIMED", ("release_not_started",)),
            )
            worker.recover(outbox.outbox_id, "release_not_started")
            result = worker.run_once()
            self.assertEqual(result.application_run_id,
                             committed.first_briefing_application_run_id)
            self.assertEqual((len(search.calls), len(provider.calls)), (1, 1))
            self.assertEqual(len(repository.list_digests(USER)), 1)

    def test_bound_before_crash_reuses_harness_binding(self):
        with tempfile.TemporaryDirectory() as root:
            fault = RaiseOnce("after_harness_binding")
            (repository, search, provider, _generation, worker, _app,
             committed, outbox) = self.prepare(root, workflow_fault=fault)
            with self.assertRaisesRegex(RuntimeError, "after_harness_binding"):
                worker.run_once()
            run = repository.get_digest_run(
                committed.first_briefing_application_run_id,
            )
            harness_run_id = run.harness_run_id
            self.assertEqual(worker.inspect(outbox.outbox_id).
                             safe_recovery_actions, ("resume_bound_run",))
            result = worker.recover(outbox.outbox_id, "resume_bound_run")
            current = repository.get_digest_run(run.digest_run_id)
            self.assertEqual((result.briefing_status,
                              current.harness_run_id), ("READY", harness_run_id))
            self.assertEqual((len(search.calls), len(provider.calls)), (1, 1))
            self.assertEqual(len(repository.list_digests(USER)), 1)

    def test_started_nonterminal_run_is_blocked_without_double_retry(self):
        with tempfile.TemporaryDirectory() as root:
            fault = RaiseOnce("after_search_evidence")
            (repository, search, provider, _generation, worker, app,
             committed, outbox) = self.prepare(root, workflow_fault=fault)
            with self.assertRaisesRegex(RuntimeError, "after_search_evidence"):
                worker.run_once()
            facts = worker.inspect(outbox.outbox_id)
            self.assertEqual(facts.safe_recovery_actions,
                             ("block_ambiguous_run",))
            result = worker.recover(outbox.outbox_id, "block_ambiguous_run")
            briefing = app.get_first_briefing(USER, committed.subscription_id)
            relation = repository.get_user_subscription_for_subscription(
                committed.subscription_id,
            )
            self.assertEqual(
                (result.outbox_status, briefing.subscription_status,
                 briefing.relation_status, briefing.status),
                ("BLOCKED", "ACTIVE", "ACTIVE", "BLOCKED"),
            )
            self.assertEqual((len(search.calls), len(provider.calls)), (1, 0))
            self.assertEqual(repository.list_digests(USER), ())

    def test_digest_durable_before_outbox_success_is_mark_only_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            fault = RaiseOnce("after_execution")
            (repository, search, provider, _generation, worker, _app,
             _committed, outbox) = self.prepare(root, worker_fault=fault)
            with self.assertRaisesRegex(RuntimeError, "after_execution"):
                worker.run_once()
            digest = repository.list_digests(USER)[0]
            self.assertEqual(worker.inspect(outbox.outbox_id).
                             safe_recovery_actions,
                             ("finalize_terminal_outcome",))
            result = worker.recover(
                outbox.outbox_id, "finalize_terminal_outcome",
            )
            self.assertEqual(
                (result.outbox_status, result.digest_id),
                ("SUCCEEDED", digest.digest_id),
            )
            self.assertEqual((len(search.calls), len(provider.calls)), (1, 1))
            self.assertEqual(repository.list_digests(USER)[0].digest_id,
                             digest.digest_id)
            self.assertEqual(len(repository.list_digests(USER)), 1)

    def test_authoritative_incomplete_does_not_rollback_subscription(self):
        with tempfile.TemporaryDirectory() as root:
            (repository, search, provider, _generation, _worker, app,
             committed, _outbox) = self.prepare(root, rows=[])
            result = app.run_outbox_once()
            briefing = app.get_first_briefing(USER, committed.subscription_id)
            relation = repository.get_user_subscription_for_subscription(
                committed.subscription_id,
            )
            self.assertEqual(
                (result.outbox_status, result.first_briefing_status,
                 briefing.subscription_status, relation.status),
                ("SUCCEEDED", "INCOMPLETE", "ACTIVE", "ACTIVE"),
            )
            self.assertEqual((len(search.calls), len(provider.calls)), (1, 0))
            self.assertEqual(repository.list_digests(USER), ())

    def test_outbox_payload_contains_only_durable_refs(self):
        with tempfile.TemporaryDirectory() as root:
            (_repository, _search, _provider, _generation, worker, _app,
             committed, outbox) = self.prepare(root)
            self.assertEqual(set(outbox.payload_refs), {
                "activation_id", "application_run_id", "definition_id",
                "definition_version",
            })
            encoded = json.dumps(outbox.payload_refs, ensure_ascii=False)
            for forbidden in ("conversation", "prompt", "vertex", "brave",
                              "credential", "evidence", "response"):
                self.assertNotIn(forbidden, encoded.lower())
            self.assertEqual(
                outbox.payload_refs["application_run_id"],
                committed.first_briefing_application_run_id,
            )
            self.assertEqual(worker.inspect(outbox.outbox_id).outbox_status,
                             "PENDING")

    def test_outbox_success_is_visible_even_if_worker_crashes_after_mark(self):
        with tempfile.TemporaryDirectory() as root:
            fault = RaiseOnce("after_outbox_success")
            (repository, search, provider, _generation, worker, app,
             committed, outbox) = self.prepare(root, worker_fault=fault)
            with self.assertRaisesRegex(RuntimeError, "after_outbox_success"):
                worker.run_once()
            briefing = app.get_first_briefing(USER, committed.subscription_id)
            self.assertEqual(
                (repository.get_application_outbox(outbox.outbox_id).status,
                 briefing.status, worker.run_once().worker_status),
                ("completed", "READY", "NO_WORK"),
            )
            self.assertEqual((len(search.calls), len(provider.calls)), (1, 1))
            self.assertEqual(len(repository.list_digests(USER)), 1)

    def test_outbox_cannot_succeed_before_terminal_product_outcome(self):
        with tempfile.TemporaryDirectory() as root:
            (repository, _search, _provider, _generation, _worker, _app,
             _committed, outbox) = self.prepare(root)
            claimed = repository.claim_application_outbox(NOW)
            with self.assertRaisesRegex(ValueError,
                                        "completion lacks terminal run"):
                repository.finalize_application_outbox(
                    claimed.outbox_id, claimed.version, "completed", None,
                    NOW, NOW,
                )
            current = repository.get_application_outbox(outbox.outbox_id)
            self.assertEqual(current.status, "claimed")
            self.assertEqual(repository.list_digests(USER), ())

    def test_bounded_drain_and_invalid_limit(self):
        with tempfile.TemporaryDirectory() as root:
            (_repository, _search, _provider, _generation, _worker, app,
             _committed, _outbox) = self.prepare(root)
            values = app.drain_outbox(1)
            self.assertEqual(len(values), 1)
            self.assertEqual(values[0].first_briefing_status, "READY")
            self.assertEqual(app.drain_outbox(1), ())
            with self.assertRaisesRegex(ValueError, "invalid_drain_limit"):
                app.drain_outbox(0)


if __name__ == "__main__":
    unittest.main()
