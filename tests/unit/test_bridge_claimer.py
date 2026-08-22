import json
import os
import tempfile
import unittest
from pathlib import Path

from mini_harness_core.bridge.claimer import (
    CLAIMED,
    CLAIM_NONCE_EXISTS,
    CLAIM_NOT_ALLOWED,
    TASK_LOCKED,
    claim_bridge_task,
)
from mini_harness_core.bridge.inspector import (
    CLAIMED_UNKNOWN,
    READY_TO_CLAIM,
    SAFE_TO_RECLAIM_WITH_NEW_NONCE,
    inspect_bridge_task,
)


TASK = "task-004"


class BridgeClaimerTests(unittest.TestCase):
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

    def task(self, ready=True):
        self.write(f"inbox/{TASK}.json", {
            "task_schema_version": 1,
            "task_id": TASK,
            "task_type": "bridge_test",
            "payload": {"message": "hello"},
            "publisher_id": "test",
            "published_at": "2026-08-22T00:00:00+00:00",
        })
        if ready:
            self.write(f"inbox/{TASK}.ready")

    def claim_record(self, nonce="claim-004-a", attempt=1, previous=None):
        self.write(f"claims/{TASK}/{nonce}.json", {
            "claim_schema_version": 1,
            "task_id": TASK,
            "consumer_id": "consumer-a",
            "claim_nonce": nonce,
            "attempt_number": attempt,
            "previous_claim_nonce": previous,
            "claimed_at": "2026-08-22T01:00:00+00:00",
        })

    def reconciliation(self, result):
        self.write(f"reconciliations/{TASK}/claim-004-a.json", {
            "reconciliation_schema_version": 1,
            "task_id": TASK,
            "claim_nonce": "claim-004-a",
            "result": result,
        })

    def claim(self, nonce="claim-004-a", consumer="consumer-a"):
        return claim_bridge_task(self.root, TASK, consumer, nonce)

    def test_ready_task_claims_attempt_one(self):
        self.task()
        result = self.claim()
        self.assertEqual(result.status, CLAIMED)
        self.assertEqual(result.attempt_number, 1)
        self.assertIsNone(result.previous_claim_nonce)
        self.assertEqual(inspect_bridge_task(self.root, TASK).state, CLAIMED_UNKNOWN)

    def test_claim_record_schema(self):
        self.task()
        self.claim()
        record = json.loads(
            (self.root / f"claims/{TASK}/claim-004-a.json").read_text(encoding="utf-8")
        )
        self.assertEqual(set(record), {
            "claim_schema_version", "task_id", "consumer_id", "claim_nonce",
            "attempt_number", "previous_claim_nonce", "claimed_at",
        })
        self.assertEqual(record["claim_schema_version"], 1)
        self.assertEqual(record["consumer_id"], "consumer-a")
        self.assertTrue(record["claimed_at"])

    def test_duplicate_nonce_rejected(self):
        self.task()
        self.claim_record()
        original = (self.root / f"claims/{TASK}/claim-004-a.json").read_bytes()
        self.assertEqual(self.claim().status, CLAIM_NONCE_EXISTS)
        self.assertEqual(
            (self.root / f"claims/{TASK}/claim-004-a.json").read_bytes(), original,
        )

    def test_second_consumer_while_lock_held(self):
        self.task()
        (self.root / f"locks/{TASK}.lock").mkdir()
        result = self.claim(consumer="consumer-b")
        self.assertEqual(result.status, TASK_LOCKED)
        self.assertTrue((self.root / f"locks/{TASK}.lock").is_dir())

    def test_claimed_unknown_cannot_reclaim_with_new_nonce(self):
        self.task()
        self.claim_record()
        result = self.claim("claim-004-b")
        self.assertEqual(result.status, CLAIM_NOT_ALLOWED)
        self.assertEqual(result.task_state, CLAIMED_UNKNOWN)

    def test_not_applied_allows_attempt_two_with_previous(self):
        self.task()
        self.claim_record()
        self.reconciliation("not_applied")
        self.assertEqual(inspect_bridge_task(self.root, TASK).state,
                         SAFE_TO_RECLAIM_WITH_NEW_NONCE)
        result = self.claim("claim-004-b")
        self.assertEqual(result.status, CLAIMED)
        self.assertEqual(result.attempt_number, 2)
        self.assertEqual(result.previous_claim_nonce, "claim-004-a")
        record = json.loads(
            (self.root / f"claims/{TASK}/claim-004-b.json").read_text(encoding="utf-8")
        )
        self.assertEqual(record["attempt_number"], 2)
        self.assertEqual(record["previous_claim_nonce"], "claim-004-a")

    def test_applied_and_uncertain_block_new_claim(self):
        for reconciliation in ("applied", "uncertain"):
            with self.subTest(reconciliation=reconciliation):
                with tempfile.TemporaryDirectory() as directory:
                    other = BridgeClaimerTests(methodName="runTest")
                    other.root = Path(directory)
                    for name in ("inbox", "claims", "reconciliations", "outbox", "locks"):
                        (other.root / name).mkdir()
                    other.task()
                    other.claim_record()
                    other.reconciliation(reconciliation)
                    result = claim_bridge_task(
                        other.root, TASK, "consumer-b", "claim-004-b",
                    )
                    self.assertEqual(result.status, CLAIM_NOT_ALLOWED)

    def test_completed_blocks_claim(self):
        self.task()
        self.claim_record()
        self.write(f"outbox/result-{TASK}.json", {
            "result_schema_version": 1, "task_id": TASK,
            "claim_nonce": "claim-004-a", "status": "completed",
        })
        self.write(f"outbox/result-{TASK}.ready")
        self.assertEqual(self.claim("claim-004-b").status, CLAIM_NOT_ALLOWED)

    def test_invalid_history_blocks_claim(self):
        self.write(f"inbox/{TASK}.ready")
        self.assertEqual(self.claim().status, CLAIM_NOT_ALLOWED)

    def test_not_ready_blocks_claim_and_releases_lock(self):
        self.task(ready=False)
        self.assertEqual(inspect_bridge_task(self.root, TASK).state, "NOT_READY")
        self.assertEqual(self.claim().status, CLAIM_NOT_ALLOWED)
        self.assertFalse((self.root / f"locks/{TASK}.lock").exists())

    def test_tmp_claim_is_ignored_by_inspector(self):
        self.task()
        self.write(f"claims/{TASK}/.claim-crashed.json.tmp", "{")
        self.assertEqual(inspect_bridge_task(self.root, TASK).state, READY_TO_CLAIM)

    def test_stale_lock_is_not_auto_removed(self):
        self.task()
        stale = self.root / f"locks/{TASK}.lock"
        stale.mkdir()
        self.assertEqual(self.claim().status, TASK_LOCKED)
        self.assertTrue(stale.is_dir())

    def test_claim_before_lock_release_remains_valid(self):
        self.task()
        self.claim_record()
        (self.root / f"locks/{TASK}.lock").mkdir()
        self.assertEqual(inspect_bridge_task(self.root, TASK).state, CLAIMED_UNKNOWN)
        self.assertEqual(self.claim("claim-004-b").status, TASK_LOCKED)

    def test_does_not_modify_task_or_result_areas(self):
        self.task()
        task_before = (self.root / f"inbox/{TASK}.json").read_bytes()
        ready_before = (self.root / f"inbox/{TASK}.ready").read_bytes()
        self.claim()
        self.assertEqual((self.root / f"inbox/{TASK}.json").read_bytes(), task_before)
        self.assertEqual((self.root / f"inbox/{TASK}.ready").read_bytes(), ready_before)
        self.assertEqual(list((self.root / "outbox").iterdir()), [])
        self.assertEqual(list((self.root / "reconciliations").iterdir()), [])

    def test_path_traversal_rejected(self):
        self.task()
        for task_id, nonce in (("../task", "nonce"), (TASK, "../nonce")):
            with self.subTest(task_id=task_id, nonce=nonce), self.assertRaises(ValueError):
                claim_bridge_task(self.root, task_id, "consumer", nonce)

    def test_symlink_escape_rejected(self):
        self.task()
        with tempfile.TemporaryDirectory() as outside:
            os.rmdir(self.root / "locks")
            os.symlink(outside, self.root / "locks")
            with self.assertRaisesRegex(ValueError, "symlink escapes"):
                self.claim()


if __name__ == "__main__":
    unittest.main()
