import json
import os
import tempfile
import unittest
from pathlib import Path

from mini_harness_core.bridge_inspector import (
    BLOCKED_UNCERTAIN_EFFECT,
    CLAIMED_BY_OTHER,
    CLAIMED_BY_SELF_UNKNOWN,
    CLAIMED_UNKNOWN,
    COMPLETED,
    EFFECT_APPLIED_NEEDS_RESULT_REPAIR,
    INVALID_HISTORY,
    NOT_READY,
    READY_TO_CLAIM,
    SAFE_TO_RECLAIM_WITH_NEW_NONCE,
    inspect_bridge_task,
)


TASK = "task-003"


class BridgeFixture:
    def __init__(self, root):
        self.root = Path(root)
        for name in ("inbox", "claims", "reconciliations", "outbox"):
            (self.root / name).mkdir()

    def write(self, relative, value=""):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, dict):
            path.write_text(json.dumps(value), encoding="utf-8")
        else:
            path.write_text(value, encoding="utf-8")
        return path

    def task(self, task_id=TASK, ready=True, **changes):
        value = {"task_schema_version": 1, "task_id": task_id,
                 "payload": {"ignored": True}}
        value.update(changes)
        self.write(f"inbox/{task_id}.json", value)
        if ready:
            self.write(f"inbox/{task_id}.ready")

    def claim(self, nonce="claim-003-a", attempt=1, previous=None,
              consumer="codex-proot", task_id=TASK, **changes):
        value = {
            "claim_schema_version": 1,
            "task_id": task_id,
            "consumer_id": consumer,
            "claim_nonce": nonce,
            "attempt_number": attempt,
            "previous_claim_nonce": previous,
            "claimed_at": "2026-08-22T20:00:00+08:00",
        }
        value.update(changes)
        self.write(f"claims/{task_id}/{nonce}-{attempt}.json", value)

    def reconciliation(self, nonce="claim-003-a", result="applied",
                       task_id=TASK, filename=None, **changes):
        value = {
            "reconciliation_schema_version": 1,
            "task_id": task_id,
            "claim_nonce": nonce,
            "result": result,
        }
        value.update(changes)
        self.write(
            f"reconciliations/{task_id}/{filename or nonce}.json", value,
        )

    def result(self, nonce="claim-003-a", status="completed", ready=True,
               task_id=TASK, **changes):
        value = {
            "result_schema_version": 1,
            "task_id": task_id,
            "claim_nonce": nonce,
            "status": status,
        }
        value.update(changes)
        self.write(f"outbox/result-{task_id}.json", value)
        if ready:
            self.write(f"outbox/result-{task_id}.ready")


class BridgeInspectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = BridgeFixture(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def inspect(self, **kwargs):
        return inspect_bridge_task(self.temporary.name, TASK, **kwargs)

    def test_task_003_completed_equivalent_fixture(self):
        self.fixture.task()
        self.fixture.claim()
        self.fixture.result()
        state = self.inspect(consumer_id="codex-proot", claim_nonce="claim-003-a")
        self.assertEqual(state.state, COMPLETED)
        self.assertEqual(state.latest_claim_nonce, "claim-003-a")
        self.assertEqual(state.result_status, "completed")

    def test_not_ready(self):
        self.fixture.task(ready=False)
        self.assertEqual(self.inspect().state, NOT_READY)

    def test_ready_to_claim(self):
        self.fixture.task()
        self.assertEqual(self.inspect().state, READY_TO_CLAIM)

    def test_claimed_unknown_without_observer_identity(self):
        self.fixture.task()
        self.fixture.claim()
        self.assertEqual(self.inspect().state, CLAIMED_UNKNOWN)

    def test_claimed_by_self_unknown(self):
        self.fixture.task()
        self.fixture.claim()
        self.assertEqual(self.inspect(
            consumer_id="codex-proot", claim_nonce="claim-003-a",
        ).state, CLAIMED_BY_SELF_UNKNOWN)

    def test_claimed_by_other_for_consumer_or_nonce_mismatch(self):
        self.fixture.task()
        self.fixture.claim()
        for kwargs in (
            {"consumer_id": "other"},
            {"claim_nonce": "other-claim"},
            {"consumer_id": "codex-proot", "claim_nonce": "other-claim"},
        ):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.inspect(**kwargs).state, CLAIMED_BY_OTHER)

    def test_latest_reconciliation_derives_recovery_state(self):
        expected = {
            "not_applied": SAFE_TO_RECLAIM_WITH_NEW_NONCE,
            "applied": EFFECT_APPLIED_NEEDS_RESULT_REPAIR,
            "uncertain": BLOCKED_UNCERTAIN_EFFECT,
        }
        for result, state in expected.items():
            with self.subTest(result=result):
                with tempfile.TemporaryDirectory() as directory:
                    fixture = BridgeFixture(directory)
                    fixture.task()
                    fixture.claim()
                    fixture.reconciliation(result=result)
                    self.assertEqual(
                        inspect_bridge_task(directory, TASK).state, state,
                    )

    def test_fork_is_invalid(self):
        self.fixture.task()
        self.fixture.claim("claim-a", 1)
        self.fixture.claim("claim-b", 2, "claim-a")
        self.fixture.claim("claim-c", 2, "claim-a")
        self.assertEqual(self.inspect().state, INVALID_HISTORY)

    def test_gap_is_invalid(self):
        self.fixture.task()
        self.fixture.claim("claim-a", 1)
        self.fixture.claim("claim-c", 3, "claim-a")
        self.assertEqual(self.inspect().state, INVALID_HISTORY)

    def test_duplicate_nonce_is_invalid(self):
        self.fixture.task()
        self.fixture.claim("same", 1)
        value = {
            "claim_schema_version": 1, "task_id": TASK,
            "consumer_id": "codex-proot", "claim_nonce": "same",
            "attempt_number": 2, "previous_claim_nonce": "same",
            "claimed_at": "later",
        }
        self.fixture.write(f"claims/{TASK}/duplicate.json", value)
        self.assertEqual(self.inspect().state, INVALID_HISTORY)

    def test_bad_claim_schema_is_invalid(self):
        self.fixture.task()
        self.fixture.claim(claim_schema_version=2)
        self.assertEqual(self.inspect().state, INVALID_HISTORY)

    def test_bad_reconciliation_is_invalid(self):
        self.fixture.task()
        self.fixture.claim()
        self.fixture.reconciliation(result="maybe")
        self.assertEqual(self.inspect().state, INVALID_HISTORY)

    def test_successor_requires_prior_not_applied(self):
        for reconciliation in (None, "applied", "uncertain"):
            with self.subTest(reconciliation=reconciliation):
                with tempfile.TemporaryDirectory() as directory:
                    fixture = BridgeFixture(directory)
                    fixture.task()
                    fixture.claim("claim-a", 1)
                    if reconciliation:
                        fixture.reconciliation("claim-a", reconciliation)
                    fixture.claim("claim-b", 2, "claim-a")
                    self.assertEqual(
                        inspect_bridge_task(directory, TASK).state,
                        INVALID_HISTORY,
                    )

    def test_not_applied_allows_successor(self):
        self.fixture.task()
        self.fixture.claim("claim-a", 1)
        self.fixture.reconciliation("claim-a", "not_applied")
        self.fixture.claim("claim-b", 2, "claim-a")
        self.assertEqual(self.inspect().state, CLAIMED_UNKNOWN)
        self.assertEqual(self.inspect().latest_claim_nonce, "claim-b")

    def test_result_ready_without_result_is_invalid(self):
        self.fixture.task()
        self.fixture.claim()
        self.fixture.write(f"outbox/result-{TASK}.ready")
        self.assertEqual(self.inspect().state, INVALID_HISTORY)

    def test_result_bound_to_wrong_claim_is_invalid(self):
        self.fixture.task()
        self.fixture.claim()
        self.fixture.result(nonce="unknown")
        self.assertEqual(self.inspect().state, INVALID_HISTORY)

    def test_ready_result_cannot_bypass_missing_inbox_ready(self):
        self.fixture.task(ready=False)
        self.fixture.claim()
        self.fixture.result()
        self.assertEqual(self.inspect().state, INVALID_HISTORY)

    def test_task_id_traversal_is_rejected(self):
        for task_id in ("../task", "a/b", "a\\b", "/absolute", "bad\x00id", ".."):
            with self.subTest(task_id=task_id):
                state = inspect_bridge_task(self.temporary.name, task_id)
                self.assertEqual(state.state, INVALID_HISTORY)

    def test_symlink_escape_is_rejected(self):
        self.fixture.task()
        with tempfile.TemporaryDirectory() as outside:
            outside_claims = Path(outside) / TASK
            outside_claims.mkdir()
            os.rmdir(Path(self.temporary.name) / "claims")
            os.symlink(outside, Path(self.temporary.name) / "claims")
            state = self.inspect()
        self.assertEqual(state.state, INVALID_HISTORY)
        self.assertTrue(any("symlink escapes" in error for error in state.validation_errors))

    def test_claimed_at_does_not_order_chain(self):
        self.fixture.task()
        self.fixture.claim("claim-a", 1, claimed_at="later")
        self.fixture.reconciliation("claim-a", "not_applied")
        self.fixture.claim("claim-b", 2, "claim-a", claimed_at="earlier")
        self.assertEqual(self.inspect().latest_claim_nonce, "claim-b")


if __name__ == "__main__":
    unittest.main()
