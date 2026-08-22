import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mini_harness_core.agent import run_agent
from mini_harness_core.audit import read_events
from mini_harness_core.bridge_adapter import (
    BINDING_CONFLICT,
    BINDING_CREATED,
    BINDING_REUSED,
    BOUND_NOT_STARTED,
    DONE,
    HARNESS_RECOVERY_REQUIRED,
    INTEGRATION_UNKNOWN,
    RESULT_PROJECTION_REQUIRED,
    BridgeAdapterError,
    BridgeBindingStore,
    bind_bridge_attempt,
    historical_bridge_binding_identity,
    inspect_bridge_binding,
    project_harness_result_to_bridge,
    read_bridge_harness_task,
    recover_bridge_binding,
    run_bound_bridge_request,
)
from mini_harness_core.bridge_claimer import claim_bridge_task
from mini_harness_core.bridge_inspector import COMPLETED, inspect_bridge_task
from mini_harness_core.bridge_publisher import publish_bridge_task
from mini_harness_core.bridge_reconciler import reconcile_bridge_claim
from mini_harness_core.integrity import sha256_identity
from mini_harness_core.result import result_fingerprint, validate_result
from tests.helpers.bridge import CONSUMER, ScriptedFakeProvider


def authoritative_result(run_id, status="completed", answer=None):
    answer = answer or ("done" if status == "completed" else f"run {status}")
    candidate = {
        "answer_length": 0,
        "answer_sha256": hashlib.sha256(b"").hexdigest(),
        "claimed_status": None,
        "artifact_refs": [],
        "evidence_refs": [],
        "answer_allowed": True,
        "answer_rejection_reason": None,
        "contradiction": False,
    }
    result = {
        "result_schema_version": 1,
        "run_id": run_id,
        "status": status,
        "answer": answer,
        "artifact_ids": [],
        "evidence_ids": [],
        "plan_id": None,
        "reason": None if status == "completed" else status,
        "candidate": candidate,
        "result_fingerprint": "",
    }
    result["result_fingerprint"] = result_fingerprint(result)
    return validate_result(result)


class BridgeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.bridge = self.base / "bridge"
        self.audit = self.base / "audit"
        self.sessions = self.base / "sessions"
        self.bridge.mkdir()
        for name in ("inbox", "claims", "reconciliations", "outbox", "locks"):
            (self.bridge / name).mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def publish(self, task_id="task-adapter", request="read status", **changes):
        payload = changes.pop("payload", {"request": request})
        return publish_bridge_task(
            self.bridge, task_id,
            changes.pop("task_type", "bridge_harness_task"), payload,
            changes.pop("publisher_id", "external-publisher"),
        )

    def prepare(self, task_id="task-adapter", nonce="claim-adapter-a"):
        self.publish(task_id)
        task = read_bridge_harness_task(self.bridge, task_id)
        claim_bridge_task(self.bridge, task_id, CONSUMER, nonce)
        binding, status = bind_bridge_attempt(
            self.bridge, self.audit, task_id, nonce, CONSUMER,
            expected_source_fingerprint=task.source_fingerprint,
        )
        self.assertEqual(status, BINDING_CREATED)
        return task, binding

    def test_valid_task_binds_then_creates_exactly_one_run(self):
        _task, binding = self.prepare()
        provider = ScriptedFakeProvider([
            {"type": "final_answer", "final_answer": "done"},
        ])
        result = run_bound_bridge_request(
            self.bridge, self.audit, self.sessions, binding, CONSUMER, provider,
            run_agent,
        )
        self.assertEqual(result.harness_run_id, binding["harness_run_id"])
        self.assertEqual(result.harness_result_status, "completed")
        self.assertEqual(inspect_bridge_task(self.bridge, "task-adapter").state, COMPLETED)
        self.assertEqual(len(provider.calls), 1)
        self.assertTrue((self.sessions / f"{binding['harness_session_id']}.json").is_file())

    def test_duplicate_same_claim_reuses_run_and_does_not_run_again(self):
        task, binding = self.prepare()
        first_provider = ScriptedFakeProvider([
            {"type": "final_answer", "final_answer": "done"},
        ])
        run_bound_bridge_request(
            self.bridge, self.audit, self.sessions, binding, CONSUMER,
            first_provider, run_agent,
        )
        duplicate, status = bind_bridge_attempt(
            self.bridge, self.audit, "task-adapter", "claim-adapter-a", CONSUMER,
            expected_source_fingerprint=task.source_fingerprint,
        )
        self.assertEqual(status, BINDING_REUSED)
        self.assertEqual(duplicate["harness_run_id"], binding["harness_run_id"])
        second_provider = ScriptedFakeProvider([])
        result = run_bound_bridge_request(
            self.bridge, self.audit, self.sessions, duplicate, CONSUMER,
            second_provider, run_agent,
        )
        self.assertEqual(result.harness_run_id, binding["harness_run_id"])
        self.assertEqual(second_provider.calls, [])

    def test_binding_conflict_is_fail_closed(self):
        _task, binding = self.prepare()
        conflict = dict(binding)
        conflict["harness_run_id"] = "f" * 32
        conflict["binding_fingerprint"] = ""
        stable = {key: value for key, value in conflict.items()
                  if key != "binding_fingerprint"}
        conflict["binding_fingerprint"] = sha256_identity(stable)
        with self.assertRaisesRegex(BridgeAdapterError, BINDING_CONFLICT):
            BridgeBindingStore(self.audit).publish(conflict)

    def test_source_fingerprint_drift_before_binding_conflicts(self):
        self.publish()
        original = read_bridge_harness_task(self.bridge, "task-adapter")
        claim_bridge_task(
            self.bridge, "task-adapter", CONSUMER, "claim-adapter-a",
        )
        task_path = self.bridge / "inbox/task-adapter.json"
        record = json.loads(task_path.read_text(encoding="utf-8"))
        record["payload"]["request"] = "changed after initial validation"
        task_path.write_text(json.dumps(record), encoding="utf-8")
        binding, status = bind_bridge_attempt(
            self.bridge, self.audit, "task-adapter", "claim-adapter-a", CONSUMER,
            expected_source_fingerprint=original.source_fingerprint,
        )
        self.assertIsNone(binding)
        self.assertEqual(status, BINDING_CONFLICT)

    def test_input_schema_secret_size_and_direct_action_fields_rejected(self):
        cases = (
            ({"request": "Bearer secret"}, None),
            ({"request": "x" * (16 * 1024 + 1)}, None),
            ({"request": "hello", "command": "pwd"}, None),
            ({"request": 3}, None),
        )
        for index, (payload, _unused) in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in ("inbox", "claims", "reconciliations", "outbox", "locks"):
                    (root / name).mkdir(parents=True, exist_ok=True)
                (root / "inbox/task-x.json").write_text(json.dumps({
                    "task_schema_version": 1, "task_id": "task-x",
                    "task_type": "bridge_harness_task", "payload": payload,
                    "publisher_id": "publisher", "published_at": "audit-only",
                }), encoding="utf-8")
                (root / "inbox/task-x.ready").write_text("", encoding="utf-8")
                with self.assertRaises(BridgeAdapterError):
                    read_bridge_harness_task(root, "task-x")

    def test_metadata_is_not_forwarded_as_harness_authority(self):
        self.publish(
            request="please inspect status", publisher_id="trusted-admin",
        )
        task = read_bridge_harness_task(self.bridge, "task-adapter")
        self.assertEqual(task.source_label, "untrusted_external_input")
        claim_bridge_task(
            self.bridge, "task-adapter", CONSUMER, "claim-adapter-a",
        )
        binding, _ = bind_bridge_attempt(
            self.bridge, self.audit, "task-adapter", "claim-adapter-a", CONSUMER,
            expected_source_fingerprint=task.source_fingerprint,
        )
        provider = ScriptedFakeProvider([
            {"type": "final_answer", "final_answer": "safe"},
        ])
        run_bound_bridge_request(
            self.bridge, self.audit, self.sessions, binding, CONSUMER, provider,
            run_agent,
        )
        model_input = json.dumps(provider.calls[0], ensure_ascii=False)
        self.assertIn("untrusted_external_input", model_input)
        self.assertNotIn("trusted-admin", model_input)
        self.assertNotIn("claim-adapter-a", model_input)

    def test_new_bridge_attempt_gets_fresh_run_and_no_old_approval(self):
        _task, first = self.prepare()
        from mini_harness_core.audit import AuditWriter

        AuditWriter(
            first["harness_session_id"], first["harness_run_id"], self.audit,
        ).append("approval_decided", "user", "shell", "granted")
        reconcile_bridge_claim(
            self.bridge, "task-adapter", "claim-adapter-a", "not_applied",
            "operator", "manual_inspection",
        )
        claim_bridge_task(
            self.bridge, "task-adapter", CONSUMER, "claim-adapter-b",
        )
        current = read_bridge_harness_task(self.bridge, "task-adapter")
        second, status = bind_bridge_attempt(
            self.bridge, self.audit, "task-adapter", "claim-adapter-b", CONSUMER,
            expected_source_fingerprint=current.source_fingerprint,
        )
        self.assertEqual(status, BINDING_CREATED)
        self.assertNotEqual(first["harness_session_id"], second["harness_session_id"])
        self.assertNotEqual(first["harness_run_id"], second["harness_run_id"])
        second_events = read_events(second["harness_run_id"], self.audit)
        self.assertFalse(any(
            event["event_type"] == "approval_decided" for event in second_events
        ))

    def test_all_terminal_statuses_project_as_transport_completed(self):
        for status in ("completed", "blocked", "failed", "cancelled", "incomplete"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                bridge = root / "bridge"
                bridge.mkdir()
                for name in ("inbox", "claims", "reconciliations", "outbox", "locks"):
                    (bridge / name).mkdir()
                publish_bridge_task(
                    bridge, "task-status", "bridge_harness_task",
                    {"request": "status"}, "publisher",
                )
                task = read_bridge_harness_task(bridge, "task-status")
                claim_bridge_task(bridge, "task-status", CONSUMER, "claim-status-a")
                binding, _ = bind_bridge_attempt(
                    bridge, root / "audit", "task-status", "claim-status-a", CONSUMER,
                    expected_source_fingerprint=task.source_fingerprint,
                    session_id="1" * 32, run_id="2" * 32,
                )
                harness_result = authoritative_result("2" * 32, status)
                self.assertEqual(
                    project_harness_result_to_bridge(
                        bridge, binding, CONSUMER, harness_result,
                    ),
                    DONE,
                )
                projection = json.loads(
                    (bridge / "outbox/result-task-status.json").read_text(
                        encoding="utf-8",
                    )
                )
                self.assertEqual(projection["status"], "completed")
                self.assertEqual(
                    projection["result"]["harness_result_status"], status,
                )
                self.assertNotIn("evidence_ids", json.dumps(projection))
                self.assertEqual(inspect_bridge_task(bridge, "task-status").state, COMPLETED)

    def test_recovery_state_detection(self):
        self.publish()
        claim_bridge_task(
            self.bridge, "task-adapter", CONSUMER, "claim-adapter-a",
        )
        self.assertEqual(inspect_bridge_binding(
            self.bridge, self.audit, "task-adapter", "claim-adapter-a",
        ), INTEGRATION_UNKNOWN)
        task = read_bridge_harness_task(self.bridge, "task-adapter")
        binding, _ = bind_bridge_attempt(
            self.bridge, self.audit, "task-adapter", "claim-adapter-a", CONSUMER,
            expected_source_fingerprint=task.source_fingerprint,
        )
        self.assertEqual(inspect_bridge_binding(
            self.bridge, self.audit, "task-adapter", "claim-adapter-a",
        ), BOUND_NOT_STARTED)
        provider = ScriptedFakeProvider([
            {"type": "final_answer", "final_answer": "done"},
        ])
        with mock.patch(
            "mini_harness_core.bridge_adapter.project_harness_result_to_bridge",
            side_effect=SystemExit("before projection"),
        ), self.assertRaises(SystemExit):
            run_bound_bridge_request(
                self.bridge, self.audit, self.sessions, binding, CONSUMER,
                provider, run_agent,
            )
        self.assertEqual(inspect_bridge_binding(
            self.bridge, self.audit, "task-adapter", "claim-adapter-a",
        ), RESULT_PROJECTION_REQUIRED)

    def test_missing_binding_requires_explicit_recovery_helper(self):
        self.publish()
        claim_bridge_task(
            self.bridge, "task-adapter", CONSUMER, "claim-adapter-a",
        )
        provider = ScriptedFakeProvider([
            {"type": "final_answer", "final_answer": "explicit recovery"},
        ])
        recovered = recover_bridge_binding(
            self.bridge, self.audit, self.sessions, "task-adapter",
            "claim-adapter-a", CONSUMER, provider, run_agent,
        )
        self.assertEqual(recovered.bridge_state, COMPLETED)
        self.assertEqual(len(provider.calls), 1)

    def test_started_nonterminal_run_delegates_to_harness_recovery(self):
        _task, binding = self.prepare()
        from mini_harness_core.audit import AuditWriter

        AuditWriter(
            binding["harness_session_id"], binding["harness_run_id"], self.audit,
        ).append("run_started", "harness", "run", "running")
        provider = ScriptedFakeProvider([])
        result = run_bound_bridge_request(
            self.bridge, self.audit, self.sessions, binding, CONSUMER, provider,
            run_agent,
        )
        self.assertEqual(result.status, HARNESS_RECOVERY_REQUIRED)
        self.assertEqual(provider.calls, [])

    def test_audit_contains_identity_but_not_request(self):
        request = "private teaching request body"
        self.publish(request=request)
        task = read_bridge_harness_task(self.bridge, "task-adapter")
        claim_bridge_task(
            self.bridge, "task-adapter", CONSUMER, "claim-adapter-a",
        )
        binding, _ = bind_bridge_attempt(
            self.bridge, self.audit, "task-adapter", "claim-adapter-a", CONSUMER,
            expected_source_fingerprint=task.source_fingerprint,
        )
        events = read_events(binding["harness_run_id"], self.audit)
        serialized = json.dumps(events, ensure_ascii=False)
        self.assertNotIn(request, serialized)
        self.assertIn(task.source_fingerprint, serialized)
        self.assertEqual(
            [event["event_type"] for event in events],
            ["bridge_task_received", "bridge_attempt_bound"],
        )

    def test_historical_replay_identity_performs_zero_bridge_actions(self):
        _task, binding = self.prepare()
        with mock.patch(
            "mini_harness_core.bridge_adapter.BridgePathReader",
        ) as bridge, mock.patch(
            "mini_harness_core.bridge_adapter._publish_file",
        ) as publish:
            identity = historical_bridge_binding_identity(binding)
        self.assertEqual(identity["harness_run_id"], binding["harness_run_id"])
        bridge.assert_not_called()
        publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
