import json
import os
import tempfile
import unittest
from pathlib import Path

from mini_harness_core.bridge.inspector import (
    BLOCKED_UNCERTAIN_EFFECT,
    CLAIMED_UNKNOWN,
    EFFECT_APPLIED_NEEDS_RESULT_REPAIR,
    SAFE_TO_RECLAIM_WITH_NEW_NONCE,
    inspect_bridge_task,
)
from mini_harness_core.bridge.reconciler import (
    CLAIM_NOT_FOUND,
    RECONCILED,
    RECONCILIATION_EXISTS,
    RECONCILIATION_NOT_ALLOWED,
    reconcile_bridge_claim,
)


TASK = "task-004"
CLAIM = "claim-004-a"


class BridgeReconcilerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for name in ("inbox", "claims", "reconciliations", "outbox", "locks"):
            (self.root / name).mkdir()
        self.task()
        self.claim()

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

    def task(self):
        self.write(f"inbox/{TASK}.json", {
            "task_schema_version": 1, "task_id": TASK,
            "task_type": "bridge_test", "payload": {"do_not_infer": True},
            "publisher_id": "test", "published_at": "audit-only",
        })
        self.write(f"inbox/{TASK}.ready")

    def claim(self, nonce=CLAIM, attempt=1, previous=None):
        self.write(f"claims/{TASK}/{nonce}.json", {
            "claim_schema_version": 1, "task_id": TASK,
            "consumer_id": "codex-proot", "claim_nonce": nonce,
            "attempt_number": attempt, "previous_claim_nonce": previous,
            "claimed_at": "audit-only",
        })

    def reconcile(self, result="not_applied", nonce=CLAIM, method="read_only_file_check"):
        return reconcile_bridge_claim(
            self.root, TASK, nonce, result, "operator", method,
        )

    def test_each_result_derives_expected_state(self):
        expected = {
            "applied": EFFECT_APPLIED_NEEDS_RESULT_REPAIR,
            "not_applied": SAFE_TO_RECLAIM_WITH_NEW_NONCE,
            "uncertain": BLOCKED_UNCERTAIN_EFFECT,
        }
        for result, expected_state in expected.items():
            with self.subTest(result=result):
                with tempfile.TemporaryDirectory() as directory:
                    fixture = BridgeReconcilerTests(methodName="runTest")
                    fixture.root = Path(directory)
                    for name in ("inbox", "claims", "reconciliations", "outbox", "locks"):
                        (fixture.root / name).mkdir()
                    fixture.task()
                    fixture.claim()
                    outcome = fixture.reconcile(result)
                    self.assertEqual(outcome.status, RECONCILED)
                    self.assertEqual(
                        inspect_bridge_task(fixture.root, TASK).state, expected_state,
                    )

    def test_record_schema(self):
        self.assertEqual(self.reconcile("applied").status, RECONCILED)
        record = json.loads(
            (self.root / f"reconciliations/{TASK}/{CLAIM}.json").read_text(
                encoding="utf-8",
            )
        )
        self.assertEqual(set(record), {
            "reconciliation_schema_version", "task_id", "claim_nonce", "result",
            "checked_by", "method", "reconciled_at",
        })
        self.assertEqual(record["reconciliation_schema_version"], 1)
        self.assertEqual(record["checked_by"], "operator")
        self.assertEqual(record["method"], "read_only_file_check")

    def test_duplicate_is_rejected_and_immutable(self):
        self.reconcile("applied")
        path = self.root / f"reconciliations/{TASK}/{CLAIM}.json"
        original = path.read_bytes()
        result = self.reconcile("uncertain")
        self.assertEqual(result.status, RECONCILIATION_EXISTS)
        self.assertEqual(path.read_bytes(), original)

    def test_unknown_claim_rejected(self):
        empty_task = "task-empty"
        self.write(f"inbox/{empty_task}.json", {
            "task_schema_version": 1, "task_id": empty_task,
        })
        self.write(f"inbox/{empty_task}.ready")
        result = reconcile_bridge_claim(
            self.root, empty_task, "unknown", "applied", "operator", "manual_inspection",
        )
        self.assertEqual(result.status, CLAIM_NOT_FOUND)

    def test_non_latest_claim_rejected(self):
        self.write(f"reconciliations/{TASK}/{CLAIM}.json", {
            "reconciliation_schema_version": 1, "task_id": TASK,
            "claim_nonce": CLAIM, "result": "not_applied",
            "checked_by": "operator", "method": "manual_inspection",
            "reconciled_at": "audit-only",
        })
        self.claim("claim-004-b", 2, CLAIM)
        result = self.reconcile("applied", CLAIM)
        self.assertEqual(result.status, RECONCILIATION_NOT_ALLOWED)

    def test_completed_task_rejected(self):
        self.write(f"outbox/result-{TASK}.json", {
            "result_schema_version": 1, "task_id": TASK,
            "claim_nonce": CLAIM, "status": "completed",
        })
        self.write(f"outbox/result-{TASK}.ready")
        self.assertEqual(self.reconcile().status, RECONCILIATION_NOT_ALLOWED)

    def test_invalid_history_rejected(self):
        self.claim("fork", 1, None)
        self.assertEqual(self.reconcile().status, RECONCILIATION_NOT_ALLOWED)

    def test_bad_result_enum_rejected(self):
        with self.assertRaises(ValueError):
            self.reconcile("maybe")

    def test_secret_or_raw_method_rejected(self):
        for method in ("Bearer secret", "raw stdout", "-----BEGIN PRIVATE KEY-----"):
            with self.subTest(method=method), self.assertRaises(ValueError):
                self.reconcile(method=method)

    def test_tmp_is_ignored(self):
        self.write(f"reconciliations/{TASK}/.{CLAIM}.json.tmp", "{")
        self.assertEqual(inspect_bridge_task(self.root, TASK).state, CLAIMED_UNKNOWN)

    def test_no_claim_result_or_task_modification(self):
        watched = [
            self.root / f"inbox/{TASK}.json",
            self.root / f"inbox/{TASK}.ready",
            self.root / f"claims/{TASK}/{CLAIM}.json",
        ]
        before = {path: path.read_bytes() for path in watched}
        self.reconcile()
        self.assertEqual({path: path.read_bytes() for path in watched}, before)
        self.assertEqual(list((self.root / "outbox").iterdir()), [])
        self.assertEqual(len(list((self.root / f"claims/{TASK}").iterdir())), 1)

    def test_traversal_rejected(self):
        for task_id, nonce in (("../task", CLAIM), (TASK, "../claim")):
            with self.subTest(task_id=task_id, nonce=nonce), self.assertRaises(ValueError):
                reconcile_bridge_claim(
                    self.root, task_id, nonce, "applied", "operator", "manual_inspection",
                )

    def test_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            os.rmdir(self.root / "reconciliations")
            os.symlink(outside, self.root / "reconciliations")
            with self.assertRaisesRegex(ValueError, "symlink escapes"):
                self.reconcile()


if __name__ == "__main__":
    unittest.main()
