import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mini_harness_core.bridge_inspector import NOT_READY, READY_TO_CLAIM, inspect_bridge_task
from mini_harness_core.bridge_publisher import (
    PUBLISHED,
    TASK_ALREADY_EXISTS,
    publish_bridge_task,
)


TASK = "task-004"


class BridgePublisherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for name in ("inbox", "claims", "reconciliations", "outbox"):
            (self.root / name).mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def publish(self, payload=None):
        return publish_bridge_task(
            self.root, TASK, "bridge_test",
            {"message": "hello"} if payload is None else payload,
            "test-publisher",
        )

    def test_happy_publish(self):
        result = self.publish()
        self.assertEqual(result.status, PUBLISHED)
        self.assertTrue(Path(result.task_path).is_file())
        self.assertTrue(Path(result.ready_path).is_file())
        self.assertEqual(inspect_bridge_task(self.root, TASK).state, READY_TO_CLAIM)

    def test_duplicate_rejected_without_overwrite(self):
        first = self.publish()
        original = Path(first.task_path).read_bytes()
        second = self.publish({"message": "replacement"})
        self.assertEqual(second.status, TASK_ALREADY_EXISTS)
        self.assertEqual(Path(first.task_path).read_bytes(), original)

    def test_partial_tmp_is_not_ready(self):
        (self.root / "inbox" / f".{TASK}.json.tmp.crash").write_text("{", encoding="utf-8")
        self.assertEqual(inspect_bridge_task(self.root, TASK).state, NOT_READY)

    def test_task_json_without_ready_is_not_ready(self):
        (self.root / "inbox" / f"{TASK}.json").write_text(json.dumps({
            "task_schema_version": 1, "task_id": TASK,
        }), encoding="utf-8")
        self.assertEqual(inspect_bridge_task(self.root, TASK).state, NOT_READY)

    def test_ready_is_published_last(self):
        from mini_harness_core import bridge_publisher

        actual = bridge_publisher._publish_file
        destinations = []

        def recording_publish(directory, destination, content, nonce):
            destinations.append(Path(destination).name)
            actual(directory, destination, content, nonce)

        with mock.patch.object(bridge_publisher, "_publish_file", recording_publish):
            self.publish()
        self.assertEqual(destinations, [f"{TASK}.json", f"{TASK}.ready"])

    def test_traversal_rejected(self):
        for task_id in ("../task", "a/b", "a\\b", "/absolute", ".."):
            with self.subTest(task_id=task_id):
                with self.assertRaises(ValueError):
                    publish_bridge_task(self.root, task_id, "test", {})

    def test_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            os.rmdir(self.root / "inbox")
            os.symlink(outside, self.root / "inbox")
            with self.assertRaisesRegex(ValueError, "symlink escapes"):
                self.publish()

    def test_secret_payload_rejected(self):
        payloads = (
            {"Authorization": "secret"},
            {"message": "Bearer token-value"},
            {"api_key": "secret"},
            {"private_key": "secret"},
            {"environment": {"PATH": "/bin"}},
            {"hidden_reasoning": "secret"},
            {"key": "-----BEGIN PRIVATE KEY-----\nsecret"},
        )
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.publish(payload)

    def test_json_payload_is_preserved(self):
        payload = {"message": "你好", "nested": [1, True, None, {"x": 2.5}]}
        result = self.publish(payload)
        record = json.loads(Path(result.task_path).read_text(encoding="utf-8"))
        self.assertEqual(record["payload"], payload)
        self.assertEqual(record["publisher_id"], "test-publisher")
        self.assertEqual(record["task_type"], "bridge_test")
        self.assertTrue(record["published_at"])

    def test_does_not_create_claims_or_results(self):
        self.publish()
        self.assertEqual(list((self.root / "claims").iterdir()), [])
        self.assertEqual(list((self.root / "reconciliations").iterdir()), [])
        self.assertEqual(list((self.root / "outbox").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
