import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mini_harness_core.bridge.claimer import BridgeClaimResult, TASK_LOCKED
from mini_harness_core.bridge.inspector import (
    CLAIMED_BY_SELF_UNKNOWN,
    COMPLETED,
    READY_TO_CLAIM,
    inspect_bridge_task,
)
from mini_harness_core.bridge.worker import (
    BLOCKED,
    CLAIM_AND_EXECUTE,
    CLAIM_FAILED,
    IDLE,
    NEEDS_RECLAIM,
    NEEDS_RECONCILIATION,
    NEEDS_RESULT_REPAIR,
    SKIPPED_INVALID,
    WOULD_CLAIM_AND_EXECUTE,
    run_bridge_worker_once,
)


CONSUMER = "codex-proot"


class BridgeWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for name in ("inbox", "claims", "reconciliations", "outbox", "locks"):
            (self.root / name).mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, relative, value=""):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value) if isinstance(value, dict) else value,
            encoding="utf-8",
        )
        return path

    def task(self, task_id, task_type="bridge_test", payload=None, ready=True):
        self.write(f"inbox/{task_id}.json", {
            "task_schema_version": 1, "task_id": task_id,
            "task_type": task_type,
            "payload": {"message": task_id} if payload is None else payload,
            "publisher_id": "test", "published_at": "audit-only",
        })
        if ready:
            self.write(f"inbox/{task_id}.ready")

    def claim(self, task_id, nonce="old-claim", consumer=CONSUMER):
        self.write(f"claims/{task_id}/{nonce}.json", {
            "claim_schema_version": 1, "task_id": task_id,
            "consumer_id": consumer, "claim_nonce": nonce,
            "attempt_number": 1, "previous_claim_nonce": None,
            "claimed_at": "audit-only",
        })

    def reconciliation(self, task_id, result):
        self.write(f"reconciliations/{task_id}/old-claim.json", {
            "reconciliation_schema_version": 1, "task_id": task_id,
            "claim_nonce": "old-claim", "result": result,
        })

    def completed(self, task_id):
        self.write(f"outbox/result-{task_id}.json", {
            "result_schema_version": 1, "task_id": task_id,
            "claim_nonce": "old-claim", "consumer_id": CONSUMER,
            "status": "completed", "result": {"echo": task_id},
            "artifact_refs": [], "completed_at": "audit-only",
        })
        self.write(f"outbox/result-{task_id}.ready")

    def worker_once(self, dry_run=False):
        return run_bridge_worker_once(self.root, CONSUMER, dry_run)

    def test_no_task_is_idle(self):
        result = self.worker_once()
        self.assertEqual(result.action, IDLE)
        self.assertIsNone(result.task_id)

    def test_one_ready_claims_executes_and_completes(self):
        self.task("task-006")
        result = self.worker_once()
        self.assertEqual(result.action, CLAIM_AND_EXECUTE)
        self.assertEqual(result.initial_state, READY_TO_CLAIM)
        self.assertEqual(result.final_state, COMPLETED)
        self.assertEqual(inspect_bridge_task(self.root, "task-006").state, COMPLETED)

    def test_completed_is_skipped(self):
        self.task("task-done")
        self.claim("task-done")
        self.completed("task-done")
        self.assertEqual(self.worker_once().action, IDLE)

    def test_old_self_or_other_claim_is_not_executed(self):
        for consumer in (CONSUMER, "other"):
            with self.subTest(consumer=consumer):
                with tempfile.TemporaryDirectory() as directory:
                    fixture = BridgeWorkerTests(methodName="runTest")
                    fixture.root = Path(directory)
                    for name in ("inbox", "claims", "reconciliations", "outbox", "locks"):
                        (fixture.root / name).mkdir()
                    fixture.task("task-claimed")
                    fixture.claim("task-claimed", consumer=consumer)
                    with mock.patch(
                        "mini_harness_core.bridge.worker.execute_bridge_task",
                    ) as execute:
                        result = fixture.worker_once()
                    self.assertEqual(result.action, NEEDS_RECONCILIATION)
                    execute.assert_not_called()

    def test_safe_reclaim_is_not_automatic(self):
        self.task("task-reclaim")
        self.claim("task-reclaim")
        self.reconciliation("task-reclaim", "not_applied")
        self.assertEqual(self.worker_once().action, NEEDS_RECLAIM)
        self.assertEqual(len(list((self.root / "claims/task-reclaim").glob("*.json"))), 1)

    def test_uncertain_is_blocked(self):
        self.task("task-uncertain")
        self.claim("task-uncertain")
        self.reconciliation("task-uncertain", "uncertain")
        self.assertEqual(self.worker_once().action, BLOCKED)

    def test_applied_waits_for_result_repair_tool(self):
        self.task("task-applied")
        self.claim("task-applied")
        self.reconciliation("task-applied", "applied")
        self.assertEqual(self.worker_once().action, NEEDS_RESULT_REPAIR)
        self.assertFalse((self.root / "outbox/result-task-applied.json").exists())

    def test_invalid_history_is_skipped_with_warning(self):
        self.write("inbox/task-a.json", "{")
        self.write("inbox/task-a.ready")
        self.task("task-b")
        result = self.worker_once()
        self.assertEqual(result.task_id, "task-b")
        self.assertEqual(result.final_state, COMPLETED)
        self.assertIn("task-a", result.reason)

    def test_only_invalid_history_is_reported(self):
        self.write("inbox/task-invalid.json", "{")
        self.write("inbox/task-invalid.ready")
        result = self.worker_once()
        self.assertEqual(result.action, SKIPPED_INVALID)
        self.assertEqual(result.task_id, "task-invalid")

    def test_multiple_tasks_choose_lexical_first_and_only_one(self):
        self.task("task-007")
        self.task("task-006")
        first = self.worker_once()
        self.assertEqual(first.task_id, "task-006")
        self.assertEqual(inspect_bridge_task(self.root, "task-006").state, COMPLETED)
        self.assertEqual(inspect_bridge_task(self.root, "task-007").state, READY_TO_CLAIM)

    def test_competition_loss_does_not_execute(self):
        self.task("task-race")
        lost = BridgeClaimResult(
            "task-race", "claim-race", None, None, TASK_LOCKED,
        )
        with mock.patch(
            "mini_harness_core.bridge.worker.claim_bridge_task", return_value=lost,
        ), mock.patch(
            "mini_harness_core.bridge.worker.execute_bridge_task",
        ) as execute:
            result = self.worker_once()
        self.assertEqual(result.action, CLAIM_FAILED)
        execute.assert_not_called()

    def test_crash_after_claim_does_not_continue_on_next_run(self):
        self.task("task-crash")
        with mock.patch(
            "mini_harness_core.bridge.worker.execute_bridge_task",
            side_effect=SystemExit("simulated crash"),
        ), self.assertRaises(SystemExit):
            self.worker_once()
        self.assertEqual(
            inspect_bridge_task(self.root, "task-crash", consumer_id=CONSUMER).state,
            CLAIMED_BY_SELF_UNKNOWN,
        )
        with mock.patch(
            "mini_harness_core.bridge.worker.execute_bridge_task",
        ) as execute:
            result = self.worker_once()
        self.assertEqual(result.action, NEEDS_RECONCILIATION)
        execute.assert_not_called()

    def test_unsupported_and_secret_tasks_are_not_executed(self):
        for task_type, payload in (
            ("dangerous", {"message": "hello"}),
            ("bridge_test", {"message": "Bearer secret"}),
        ):
            with self.subTest(task_type=task_type):
                with tempfile.TemporaryDirectory() as directory:
                    fixture = BridgeWorkerTests(methodName="runTest")
                    fixture.root = Path(directory)
                    for name in ("inbox", "claims", "reconciliations", "outbox", "locks"):
                        (fixture.root / name).mkdir()
                    fixture.task("task-bad", task_type, payload)
                    result = fixture.worker_once()
                    self.assertEqual(result.action, CLAIM_AND_EXECUTE)
                    self.assertNotEqual(result.final_state, COMPLETED)
                    self.assertFalse((fixture.root / "outbox/result-task-bad.json").exists())

    def test_dry_run_has_no_modifications(self):
        self.task("task-dry")
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        result = self.worker_once(dry_run=True)
        after = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        self.assertEqual(result.action, WOULD_CLAIM_AND_EXECUTE)
        self.assertEqual(before, after)

    def test_tmp_unready_hidden_and_legacy_are_ignored(self):
        self.write("inbox/.hidden.json")
        self.write("inbox/.hidden.ready")
        self.write("inbox/task-tmp.json.tmp", "{")
        self.write("inbox/task-unready.json", {})
        self.write("inbox/task-legacy.md", "legacy")
        self.assertEqual(self.worker_once().action, IDLE)

    def test_no_reconciliation_created(self):
        self.task("task-clean")
        self.worker_once()
        self.assertEqual(list((self.root / "reconciliations").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
