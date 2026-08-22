import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mini_harness_core.bridge.inspector import COMPLETED, inspect_bridge_task
from mini_harness_core.bridge.result_repairer import (
    ALREADY_COMPLETED,
    REPAIR_NOT_ALLOWED,
    RESULT_CONFLICT,
    RESULT_READY_REPAIRED,
    RESULT_REPAIRED,
    repair_bridge_result,
)


TASK = "task-009"
CLAIM = "claim-009-a"
CONSUMER = "codex-proot"
PAYLOAD = {"message": "effect already applied"}


class BridgeResultRepairerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for name in ("inbox", "claims", "reconciliations", "outbox", "locks"):
            (self.root / name).mkdir()
        self.task()
        self.claim()
        self.reconciliation("applied")

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
            "task_type": "bridge_test", "payload": {"message": "never execute"},
            "publisher_id": "test", "published_at": "audit-only",
        })
        self.write(f"inbox/{TASK}.ready")

    def claim(self, nonce=CLAIM, consumer=CONSUMER, attempt=1, previous=None):
        self.write(f"claims/{TASK}/{nonce}.json", {
            "claim_schema_version": 1, "task_id": TASK,
            "consumer_id": consumer, "claim_nonce": nonce,
            "attempt_number": attempt, "previous_claim_nonce": previous,
            "claimed_at": "audit-only",
        })

    def reconciliation(self, result):
        path = self.root / f"reconciliations/{TASK}/{CLAIM}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "reconciliation_schema_version": 1, "task_id": TASK,
            "claim_nonce": CLAIM, "result": result,
        }), encoding="utf-8")

    def partial(self, payload=PAYLOAD, consumer=CONSUMER, source=True):
        record = {
            "result_schema_version": 1, "task_id": TASK,
            "claim_nonce": CLAIM, "consumer_id": consumer,
            "status": "completed", "result": payload,
            "artifact_refs": [], "completed_at": "audit-only",
        }
        if source:
            record["completion_source"] = "reconciliation_repair"
        self.write(f"outbox/result-{TASK}.json", record)
        return record

    def repair(self, **changes):
        arguments = {
            "bridge_root": self.root, "task_id": TASK,
            "claim_nonce": CLAIM, "consumer_id": CONSUMER,
            "result_payload": PAYLOAD, "artifact_refs": [],
        }
        arguments.update(changes)
        return repair_bridge_result(**arguments)

    def test_applied_repairs_to_completed_without_executor(self):
        with mock.patch(
            "mini_harness_core.bridge.executor.execute_bridge_task",
        ) as execute:
            outcome = self.repair()
        self.assertEqual(outcome.status, RESULT_REPAIRED)
        self.assertEqual(outcome.protocol_state, COMPLETED)
        self.assertEqual(inspect_bridge_task(self.root, TASK).state, COMPLETED)
        execute.assert_not_called()
        record = json.loads(Path(outcome.result_path).read_text(encoding="utf-8"))
        self.assertEqual(record["completion_source"], "reconciliation_repair")
        self.assertEqual(record["result"], PAYLOAD)

    def test_non_applied_states_rejected(self):
        for result in ("not_applied", "uncertain"):
            with self.subTest(result=result):
                self.reconciliation(result)
                self.assertEqual(self.repair().status, REPAIR_NOT_ALLOWED)

    def test_claimed_unknown_rejected(self):
        os.unlink(self.root / f"reconciliations/{TASK}/{CLAIM}.json")
        self.assertEqual(self.repair().status, REPAIR_NOT_ALLOWED)

    def test_completed_is_already_completed(self):
        self.partial()
        self.write(f"outbox/result-{TASK}.ready")
        self.assertEqual(self.repair().status, ALREADY_COMPLETED)

    def test_wrong_claim_and_consumer_rejected(self):
        for claim, consumer in (("wrong-claim", CONSUMER), (CLAIM, "other")):
            with self.subTest(claim=claim, consumer=consumer):
                self.assertEqual(self.repair(
                    claim_nonce=claim, consumer_id=consumer,
                ).status, REPAIR_NOT_ALLOWED)

    def test_invalid_history_rejected(self):
        self.claim("fork", CONSUMER, 1, None)
        self.assertEqual(self.repair().status, REPAIR_NOT_ALLOWED)

    def test_matching_partial_only_publishes_ready(self):
        original = self.partial(source=False)
        path = self.root / f"outbox/result-{TASK}.json"
        before = path.read_bytes()
        outcome = self.repair()
        self.assertEqual(outcome.status, RESULT_READY_REPAIRED)
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn("completion_source", original)
        self.assertEqual(outcome.protocol_state, COMPLETED)

    def test_conflicting_partial_is_not_overwritten(self):
        self.partial(payload={"message": "different"})
        path = self.root / f"outbox/result-{TASK}.json"
        before = path.read_bytes()
        self.assertEqual(self.repair().status, RESULT_CONFLICT)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.root / f"outbox/result-{TASK}.ready").exists())

    def test_secret_result_and_artifact_rejected(self):
        requests = (
            {"result_payload": {"message": "Bearer secret"}},
            {"result_payload": {"api_key": "secret"}},
            {"artifact_refs": [{"raw_tool_output": "secret"}]},
            {"artifact_refs": ["-----BEGIN PRIVATE KEY-----"]},
        )
        for changes in requests:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.repair(**changes)

    def test_no_protocol_inputs_are_modified_and_no_claim_added(self):
        watched = [
            self.root / f"inbox/{TASK}.json",
            self.root / f"inbox/{TASK}.ready",
            self.root / f"claims/{TASK}/{CLAIM}.json",
            self.root / f"reconciliations/{TASK}/{CLAIM}.json",
        ]
        before = {path: path.read_bytes() for path in watched}
        self.repair()
        self.assertEqual({path: path.read_bytes() for path in watched}, before)
        self.assertEqual(len(list((self.root / f"claims/{TASK}").glob("*.json"))), 1)
        self.assertEqual(
            len(list((self.root / f"reconciliations/{TASK}").glob("*.json"))), 1,
        )

    def test_ready_is_published_last(self):
        from mini_harness_core.bridge import result_repairer as bridge_result_repairer

        actual = bridge_result_repairer._publish_file
        destinations = []

        def recording_publish(directory, destination, content, nonce):
            destinations.append(Path(destination).name)
            actual(directory, destination, content, nonce)

        with mock.patch.object(
            bridge_result_repairer, "_publish_file", recording_publish,
        ):
            self.repair()
        self.assertEqual(destinations, [f"result-{TASK}.json", f"result-{TASK}.ready"])

    def test_traversal_rejected(self):
        for task_id, claim in (("../task", CLAIM), (TASK, "../claim")):
            with self.subTest(task_id=task_id, claim=claim), self.assertRaises(ValueError):
                self.repair(task_id=task_id, claim_nonce=claim)

    def test_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            os.rmdir(self.root / "outbox")
            os.symlink(outside, self.root / "outbox")
            with self.assertRaisesRegex(ValueError, "symlink escapes"):
                self.repair()


if __name__ == "__main__":
    unittest.main()
