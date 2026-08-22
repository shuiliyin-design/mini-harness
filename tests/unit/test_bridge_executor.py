import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mini_harness_core.bridge_executor import (
    ALREADY_COMPLETED,
    EXECUTED,
    EXECUTION_NOT_ALLOWED,
    INVALID_TASK,
    RESULT_PUBLISH_INCOMPLETE,
    UNSUPPORTED_TASK_TYPE,
    execute_bridge_task,
)
from mini_harness_core.bridge_inspector import (
    CLAIMED_BY_SELF_UNKNOWN,
    COMPLETED,
    inspect_bridge_task,
)


TASK = "task-005"
CLAIM = "claim-005-a"
CONSUMER = "codex-proot"


class BridgeExecutorTests(unittest.TestCase):
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

    def task(self, task_type="bridge_test", payload=None, ready=True):
        self.write(f"inbox/{TASK}.json", {
            "task_schema_version": 1, "task_id": TASK,
            "task_type": task_type,
            "payload": {"message": "hello"} if payload is None else payload,
            "publisher_id": "test", "published_at": "audit-only",
        })
        if ready:
            self.write(f"inbox/{TASK}.ready")

    def claim(self, nonce=CLAIM, consumer=CONSUMER, attempt=1, previous=None):
        self.write(f"claims/{TASK}/{nonce}.json", {
            "claim_schema_version": 1, "task_id": TASK,
            "consumer_id": consumer, "claim_nonce": nonce,
            "attempt_number": attempt, "previous_claim_nonce": previous,
            "claimed_at": "audit-only",
        })

    def reconciliation(self, result):
        self.write(f"reconciliations/{TASK}/{CLAIM}.json", {
            "reconciliation_schema_version": 1, "task_id": TASK,
            "claim_nonce": CLAIM, "result": result,
        })

    def result(self, ready=True):
        self.write(f"outbox/result-{TASK}.json", {
            "result_schema_version": 1, "task_id": TASK,
            "claim_nonce": CLAIM, "consumer_id": CONSUMER,
            "status": "completed", "result": {"echo": "hello"},
            "artifact_refs": [], "completed_at": "audit-only",
        })
        if ready:
            self.write(f"outbox/result-{TASK}.ready")

    def execute(self, consumer=CONSUMER, nonce=CLAIM):
        return execute_bridge_task(self.root, TASK, consumer, nonce)

    def test_valid_bridge_test_executes_once(self):
        from mini_harness_core import bridge_executor

        actual = bridge_executor._execute_bridge_test
        with mock.patch.object(
            bridge_executor, "_execute_bridge_test", wraps=actual,
        ) as execute:
            outcome = self.execute()
        self.assertEqual(outcome.status, EXECUTED)
        self.assertEqual(outcome.protocol_state, COMPLETED)
        execute.assert_called_once_with({"message": "hello"})
        record = json.loads(Path(outcome.result_path).read_text(encoding="utf-8"))
        self.assertEqual(record["result"], {"echo": "hello"})
        self.assertEqual(record["artifact_refs"], [])
        self.assertEqual(record["consumer_id"], CONSUMER)

    def test_wrong_consumer_and_nonce_rejected(self):
        for consumer, nonce in (("other", CLAIM), (CONSUMER, "other-claim")):
            with self.subTest(consumer=consumer, nonce=nonce):
                self.assertEqual(
                    self.execute(consumer, nonce).status, EXECUTION_NOT_ALLOWED,
                )

    def test_ready_to_claim_rejected(self):
        os.unlink(self.root / f"claims/{TASK}/{CLAIM}.json")
        self.assertEqual(self.execute().status, EXECUTION_NOT_ALLOWED)

    def test_claimed_by_other_rejected(self):
        record_path = self.root / f"claims/{TASK}/{CLAIM}.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["consumer_id"] = "other"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(self.execute().status, EXECUTION_NOT_ALLOWED)

    def test_reconciled_states_rejected(self):
        for result in ("not_applied", "applied", "uncertain"):
            with self.subTest(result=result):
                with tempfile.TemporaryDirectory() as directory:
                    fixture = BridgeExecutorTests(methodName="runTest")
                    fixture.root = Path(directory)
                    for name in ("inbox", "claims", "reconciliations", "outbox", "locks"):
                        (fixture.root / name).mkdir()
                    fixture.task()
                    fixture.claim()
                    fixture.reconciliation(result)
                    self.assertEqual(
                        fixture.execute().status, EXECUTION_NOT_ALLOWED,
                    )

    def test_completed_does_not_reexecute(self):
        from mini_harness_core import bridge_executor

        self.result()
        with mock.patch.object(bridge_executor, "_execute_bridge_test") as execute:
            outcome = self.execute()
        self.assertEqual(outcome.status, ALREADY_COMPLETED)
        execute.assert_not_called()

    def test_invalid_history_rejected(self):
        self.claim("fork", CONSUMER, 1, None)
        self.assertEqual(self.execute().status, EXECUTION_NOT_ALLOWED)

    def test_unsupported_task_type_rejected(self):
        self.task("dangerous_task")
        self.assertEqual(self.execute().status, UNSUPPORTED_TASK_TYPE)

    def test_secret_payload_rejected(self):
        payloads = (
            {"message": "Bearer secret"},
            {"message": "api_key=secret"},
            {"message": "-----BEGIN PRIVATE KEY-----"},
            {"message": "hello", "api_key": "secret"},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                self.task(payload=payload)
                self.assertEqual(self.execute().status, INVALID_TASK)

    def test_result_tmp_crash_is_not_committed(self):
        self.write(f"outbox/.result-{TASK}.json.tmp.crash", "{")
        state = inspect_bridge_task(
            self.root, TASK, consumer_id=CONSUMER, claim_nonce=CLAIM,
        )
        self.assertEqual(state.state, CLAIMED_BY_SELF_UNKNOWN)

    def test_result_json_without_ready_fails_closed(self):
        from mini_harness_core import bridge_executor

        self.result(ready=False)
        with mock.patch.object(bridge_executor, "_execute_bridge_test") as execute:
            outcome = self.execute()
        self.assertEqual(outcome.status, RESULT_PUBLISH_INCOMPLETE)
        execute.assert_not_called()

    def test_ready_is_published_last(self):
        from mini_harness_core import bridge_executor

        actual = bridge_executor._publish_file
        destinations = []

        def recording_publish(directory, destination, content, nonce):
            destinations.append(Path(destination).name)
            actual(directory, destination, content, nonce)

        with mock.patch.object(bridge_executor, "_publish_file", recording_publish):
            self.execute()
        self.assertEqual(destinations, [f"result-{TASK}.json", f"result-{TASK}.ready"])

    def test_no_claim_reconciliation_or_task_mutation(self):
        watched = [
            self.root / f"inbox/{TASK}.json",
            self.root / f"inbox/{TASK}.ready",
            self.root / f"claims/{TASK}/{CLAIM}.json",
        ]
        before = {path: path.read_bytes() for path in watched}
        self.execute()
        self.assertEqual({path: path.read_bytes() for path in watched}, before)
        self.assertEqual(list((self.root / "reconciliations").iterdir()), [])
        self.assertEqual(len(list((self.root / f"claims/{TASK}").iterdir())), 1)

    def test_traversal_rejected(self):
        for task_id, nonce in (("../task", CLAIM), (TASK, "../claim")):
            with self.subTest(task_id=task_id, nonce=nonce), self.assertRaises(ValueError):
                execute_bridge_task(self.root, task_id, CONSUMER, nonce)

    def test_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            os.rmdir(self.root / "outbox")
            os.symlink(outside, self.root / "outbox")
            with self.assertRaisesRegex(ValueError, "symlink escapes"):
                self.execute()


if __name__ == "__main__":
    unittest.main()
