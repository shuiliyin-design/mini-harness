"""Offline vertical validation for the frozen Bridge Protocol v1 composition."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mini_harness_core.bridge_claimer import (
    CLAIMED,
    CLAIM_NOT_ALLOWED,
    TASK_LOCKED,
    claim_bridge_task,
)
from mini_harness_core.bridge_executor import (
    ALREADY_COMPLETED,
    EXECUTED,
    EXECUTION_NOT_ALLOWED,
    RESULT_PUBLISH_INCOMPLETE,
    execute_bridge_task,
)
from mini_harness_core.bridge_inspector import (
    BLOCKED_UNCERTAIN_EFFECT,
    CLAIMED_BY_SELF_UNKNOWN,
    COMPLETED,
    EFFECT_APPLIED_NEEDS_RESULT_REPAIR,
    READY_TO_CLAIM,
    SAFE_TO_RECLAIM_WITH_NEW_NONCE,
    inspect_bridge_task,
)
from mini_harness_core.bridge_publisher import PUBLISHED, publish_bridge_task
from mini_harness_core.bridge_reconciler import RECONCILED, reconcile_bridge_claim
from mini_harness_core.bridge_result_repairer import RESULT_REPAIRED, repair_bridge_result
from mini_harness_core.bridge_worker import (
    BLOCKED,
    IDLE,
    NEEDS_RECONCILIATION,
    run_bridge_worker_once,
)


CONSUMER = "codex-proot"


class BridgeEndToEndTests(unittest.TestCase):
    """Eight scenarios that exercise protocol boundaries across tools."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for name in ("inbox", "claims", "reconciliations", "outbox", "locks"):
            (self.root / name).mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def publish(self, task_id):
        return publish_bridge_task(
            self.root, task_id, "bridge_test", {"message": task_id}, "publisher",
        )

    def claim(self, task_id, nonce, consumer=CONSUMER):
        return claim_bridge_task(self.root, task_id, consumer, nonce)

    def reconcile(self, task_id, nonce, result):
        return reconcile_bridge_claim(
            self.root, task_id, nonce, result, "operator", "manual_inspection",
        )

    def inspect(self, task_id, nonce=None, consumer=CONSUMER):
        return inspect_bridge_task(
            self.root, task_id, consumer_id=consumer, claim_nonce=nonce,
        )

    def test_01_happy_path(self):
        task_id, nonce = "task-happy", "claim-happy-a"
        self.assertEqual(self.publish(task_id).status, PUBLISHED)
        self.assertEqual(self.inspect(task_id).state, READY_TO_CLAIM)
        self.assertEqual(list((self.root / "claims").iterdir()), [])  # publish != claim

        claim = self.claim(task_id, nonce)
        self.assertEqual(claim.status, CLAIMED)
        self.assertEqual(claim.attempt_number, 1)
        self.assertEqual(self.inspect(task_id, nonce).state, CLAIMED_BY_SELF_UNKNOWN)
        self.assertFalse((self.root / f"outbox/result-{task_id}.ready").exists())

        execution = execute_bridge_task(self.root, task_id, CONSUMER, nonce)
        self.assertEqual(execution.status, EXECUTED)
        self.assertEqual(self.inspect(task_id, nonce).state, COMPLETED)

    def test_02_duplicate_consumption_is_terminal(self):
        from mini_harness_core import bridge_executor

        task_id, nonce = "task-terminal", "claim-terminal-a"
        self.publish(task_id)
        self.claim(task_id, nonce)
        execute_bridge_task(self.root, task_id, CONSUMER, nonce)
        claims_before = list((self.root / f"claims/{task_id}").glob("*.json"))

        self.assertEqual(
            self.claim(task_id, "claim-terminal-b").status, CLAIM_NOT_ALLOWED,
        )
        with mock.patch.object(bridge_executor, "_execute_bridge_test") as execute:
            again = execute_bridge_task(self.root, task_id, CONSUMER, nonce)
        self.assertEqual(again.status, ALREADY_COMPLETED)
        execute.assert_not_called()
        self.assertEqual(run_bridge_worker_once(self.root, CONSUMER).action, IDLE)
        self.assertEqual(
            list((self.root / f"claims/{task_id}").glob("*.json")), claims_before,
        )

    def test_03_claim_crash_requires_reconciliation(self):
        task_id, nonce = "task-claim-crash", "claim-crash-a"
        self.publish(task_id)
        self.claim(task_id, nonce)  # Process stops here: no cross-process memory.
        with mock.patch(
            "mini_harness_core.bridge_worker.execute_bridge_task",
        ) as execute:
            worker = run_bridge_worker_once(self.root, CONSUMER)
        self.assertEqual(worker.action, NEEDS_RECONCILIATION)
        self.assertEqual(worker.initial_state, CLAIMED_BY_SELF_UNKNOWN)
        execute.assert_not_called()
        self.assertFalse((self.root / f"outbox/result-{task_id}.json").exists())

    def test_04_not_applied_creates_new_attempt_never_reuses(self):
        task_id = "task-not-applied"
        first_nonce, second_nonce = "claim-na-a", "claim-na-b"
        self.publish(task_id)
        self.claim(task_id, first_nonce)
        first_path = self.root / f"claims/{task_id}/{first_nonce}.json"
        first_bytes = first_path.read_bytes()
        self.reconcile(task_id, first_nonce, "not_applied")
        self.assertEqual(self.inspect(task_id).state, SAFE_TO_RECLAIM_WITH_NEW_NONCE)

        second = self.claim(task_id, second_nonce)
        self.assertEqual(second.status, CLAIMED)
        self.assertEqual(second.attempt_number, 2)
        self.assertEqual(second.previous_claim_nonce, first_nonce)
        self.assertNotEqual(second_nonce, first_nonce)
        self.assertEqual(first_path.read_bytes(), first_bytes)

    def test_05_applied_repairs_result_without_execution(self):
        from mini_harness_core import bridge_executor

        task_id, nonce = "task-applied", "claim-applied-a"
        self.publish(task_id)
        self.claim(task_id, nonce)
        self.reconcile(task_id, nonce, "applied")
        self.assertEqual(
            self.inspect(task_id).state, EFFECT_APPLIED_NEEDS_RESULT_REPAIR,
        )
        with mock.patch.object(bridge_executor, "execute_bridge_task") as execute:
            repaired = repair_bridge_result(
                self.root, task_id, nonce, CONSUMER,
                {"message": "effect already applied"},
            )
        self.assertEqual(repaired.status, RESULT_REPAIRED)
        self.assertEqual(repaired.protocol_state, COMPLETED)
        execute.assert_not_called()
        self.assertEqual(len(list((self.root / f"claims/{task_id}").glob("*.json"))), 1)

    def test_06_uncertain_blocks_all_progress(self):
        task_id, nonce = "task-uncertain", "claim-uncertain-a"
        self.publish(task_id)
        self.claim(task_id, nonce)
        self.reconcile(task_id, nonce, "uncertain")
        self.assertEqual(self.inspect(task_id).state, BLOCKED_UNCERTAIN_EFFECT)
        self.assertEqual(
            self.claim(task_id, "claim-uncertain-b").status, CLAIM_NOT_ALLOWED,
        )
        self.assertEqual(
            execute_bridge_task(self.root, task_id, CONSUMER, nonce).status,
            EXECUTION_NOT_ALLOWED,
        )
        self.assertEqual(run_bridge_worker_once(self.root, CONSUMER).action, BLOCKED)

    def test_07_result_publish_crash_never_reexecutes(self):
        from mini_harness_core import bridge_executor

        task_id, nonce = "task-result-crash", "claim-result-crash-a"
        self.publish(task_id)
        self.claim(task_id, nonce)
        actual_publish = bridge_executor._publish_file
        publish_count = 0

        def crash_before_ready(directory, destination, content, publish_nonce):
            nonlocal publish_count
            publish_count += 1
            if publish_count == 2:
                raise SystemExit("crash before result.ready")
            actual_publish(directory, destination, content, publish_nonce)

        with mock.patch.object(
            bridge_executor, "_publish_file", crash_before_ready,
        ), self.assertRaises(SystemExit):
            execute_bridge_task(self.root, task_id, CONSUMER, nonce)
        self.assertTrue((self.root / f"outbox/result-{task_id}.json").exists())
        self.assertFalse((self.root / f"outbox/result-{task_id}.ready").exists())

        with mock.patch.object(bridge_executor, "_execute_bridge_test") as execute:
            retry = execute_bridge_task(self.root, task_id, CONSUMER, nonce)
        self.assertEqual(retry.status, RESULT_PUBLISH_INCOMPLETE)
        execute.assert_not_called()

    def test_08_competition_has_one_attempt_one_root(self):
        task_id = "task-race"
        self.publish(task_id)
        lock = self.root / f"locks/{task_id}.lock"
        lock.mkdir()
        loser = self.claim(task_id, "claim-race-b", consumer="consumer-b")
        self.assertEqual(loser.status, TASK_LOCKED)
        lock.rmdir()

        winner = self.claim(task_id, "claim-race-a", consumer="consumer-a")
        self.assertEqual(winner.status, CLAIMED)
        changed = self.claim(task_id, "claim-race-b", consumer="consumer-b")
        self.assertEqual(changed.status, CLAIM_NOT_ALLOWED)
        records = list((self.root / f"claims/{task_id}").glob("*.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual(record["attempt_number"], 1)
        self.assertIsNone(record["previous_claim_nonce"])


if __name__ == "__main__":
    unittest.main()
