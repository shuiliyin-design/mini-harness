import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mini_harness_core.agent import run_agent
from mini_harness_core.audit import AuditWriter
from mini_harness_core.integrations.bridge_worker import run_bridge_harness_worker_once
from mini_harness_core.bridge.inspector import COMPLETED, inspect_bridge_task
from mini_harness_core.bridge.publisher import publish_bridge_task
from mini_harness_core.evidence import EvidenceStore, validate_evidence
from mini_harness_core.policy_composition import (
    ALLOW, CAPABILITY_PROFILES, CapabilityProfile, SIDE_EFFECTING,
)
from mini_harness_core.policy_snapshot import (
    PolicyBinding, build_policy_snapshot, policy_fingerprint, replay_policy_events,
)
from mini_harness_core.result import ResultStore
from mini_harness_core.environment.termux import NOTIFICATION_LOGICAL_CAPABILITY
from mini_harness_core.environment.contracts import EnvironmentAdapterResult
from tests.helpers.bridge import ScriptedFakeProvider


SESSION = "4" * 32
RUN = "5" * 32
SHA = "0" * 64
ARGS = {"title": "Phase 2", "content": "hello"}


class FakeNotification:
    def __init__(self, result=None):
        self.result = result or self.success()
        self.calls = []

    @staticmethod
    def success():
        return EnvironmentAdapterResult(
            NOTIFICATION_LOGICAL_CAPABILITY, "succeeded", "side_effecting",
            "known_applied", {"notification_requested": True,
                              "request_accepted": True},
            0, 0, SHA, 0, SHA,
        )

    @staticmethod
    def timeout():
        return EnvironmentAdapterResult(
            NOTIFICATION_LOGICAL_CAPABILITY, "failed", "side_effecting",
            "unknown", {}, None, 0, SHA, 0, SHA, "TIMEOUT",
        )

    def __call__(self, title, content):
        self.calls.append((title, content))
        return self.result


def allow_binding():
    profiles = dict(CAPABILITY_PROFILES)
    profiles["external-actor"] = CapabilityProfile(
        "external-actor", ALLOW, frozenset({"termux"}),
        SIDE_EFFECTING, False, False,
    )
    snapshot = build_policy_snapshot(capability_profiles=profiles)
    return PolicyBinding(snapshot, policy_fingerprint(snapshot))


class NotificationHarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.audit = Path(self.temp.name) / "audit"
        self.messages = []

    def tearDown(self):
        self.temp.cleanup()

    def execute(self, adapter=None, decisions=None, binding=None, **kwargs):
        adapter = adapter or FakeNotification()
        provider = ScriptedFakeProvider(decisions or [
            {"type": "tool_call", "tool": NOTIFICATION_LOGICAL_CAPABILITY,
             "arguments": dict(ARGS)},
            {"type": "final_answer", "final_answer": "通知请求已接受"},
        ])
        with mock.patch(
            "mini_harness_core.environment.registry.invoke_termux_notification",
            side_effect=adapter,
        ):
            result = run_agent(
                "发送通知", provider, messages=self.messages,
                audit_writer=AuditWriter(SESSION, RUN, self.audit),
                evidence_store=EvidenceStore(self.audit / "evidence"),
                result_store=ResultStore(self.audit / "results"),
                return_result=True, policy_binding=binding, **kwargs,
            )
        return result, adapter

    def events(self):
        return [json.loads(line) for line in
                (self.audit / (RUN + ".jsonl")).read_text().splitlines()]

    def test_ask_approval_success_durable_action_and_evidence(self):
        checkpoints = []
        with mock.patch("mini_harness_core.agent.request_approval",
                        return_value=True) as approval:
            result, adapter = self.execute(
                save_action_checkpoint=lambda value: checkpoints.append(value),
            )
        self.assertEqual(result["status"], "completed")
        approval.assert_called_once()
        self.assertEqual(adapter.calls, [("Phase 2", "hello")])
        states = [value["state"] for value in checkpoints]
        self.assertLess(states.index("prepared"), states.index("executing"))
        self.assertLess(states.index("executing"), states.index("succeeded"))
        evidence = json.loads(next((self.audit / "evidence").iterdir()).read_text())
        validate_evidence(evidence)
        self.assertTrue(evidence["content_identity"]["safe_observation"]
                        ["request_accepted"])
        self.assertNotIn("user_seen", json.dumps(evidence))

    def test_ask_without_approval_calls_adapter_zero(self):
        adapter = FakeNotification()
        with mock.patch("mini_harness_core.agent.request_approval",
                        return_value=False):
            self.execute(adapter=adapter)
        self.assertEqual(adapter.calls, [])

    def test_allow_still_uses_durability_and_semantic_verification(self):
        checkpoints = []
        result, adapter = self.execute(
            adapter=FakeNotification(), binding=allow_binding(),
            save_action_checkpoint=lambda value: checkpoints.append(value),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(adapter.calls), 1)
        self.assertIn("executing", [item["state"] for item in checkpoints])
        self.assertTrue(any(e["event_type"] == "verification_state_changed"
                            and e["outcome"] == "accepted" for e in self.events()))

    def test_executing_persist_failure_calls_adapter_zero(self):
        adapter = FakeNotification()
        calls = []

        def fail(value):
            calls.append(value["state"])
            if value["state"] == "executing":
                raise OSError("persist failed")

        with mock.patch("mini_harness_core.agent.request_approval",
                        return_value=True), self.assertRaises(OSError):
            self.execute(adapter=adapter, save_action_checkpoint=fail)
        self.assertEqual(adapter.calls, [])

    def test_timeout_is_unknown_blocked_and_never_retried(self):
        adapter = FakeNotification(FakeNotification.timeout())
        with mock.patch("mini_harness_core.agent.request_approval",
                        return_value=True):
            result, _ = self.execute(adapter=adapter)
        self.assertIn(result["status"], {"blocked", "incomplete"})
        self.assertEqual(len(adapter.calls), 1)
        self.assertTrue(any(e["outcome"] == "unknown" for e in self.events()
                            if e["event_type"] == "observation_recorded"))
        self.assertFalse(any("reconciliation" in e["event_type"]
                             for e in self.events()))

    def test_explicit_not_started_failure_is_not_unknown(self):
        failure = EnvironmentAdapterResult(
            NOTIFICATION_LOGICAL_CAPABILITY, "failed", "side_effecting",
            "not_started", {}, None, 0, SHA, 0, SHA,
            "CAPABILITY_NOT_INSTALLED",
        )
        adapter = FakeNotification(failure)
        with mock.patch("mini_harness_core.agent.request_approval",
                        return_value=True):
            self.execute(adapter=adapter)
        self.assertFalse(any(e["outcome"] == "unknown" for e in self.events()
                             if e["event_type"] == "observation_recorded"))

    def test_delegation_removes_notification_before_approval(self):
        delegated = {
            "policy": "ALLOW", "allowed_tools": ["termux"],
            "max_effect": "read_only", "can_write_workspace": False,
            "can_use_mcp": False,
        }
        adapter = FakeNotification()
        with mock.patch("mini_harness_core.agent.request_approval") as approval:
            self.execute(adapter=adapter, termux_delegated_ceiling=delegated)
        approval.assert_not_called()
        self.assertEqual(adapter.calls, [])

    def test_audit_has_digests_not_title_content_or_executable(self):
        with mock.patch("mini_harness_core.agent.request_approval", return_value=True):
            self.execute()
        audit = (self.audit / (RUN + ".jsonl")).read_text()
        self.assertNotIn("Phase 2", audit)
        self.assertNotIn("hello", audit)
        self.assertNotIn("/data/data/com.termux", audit)
        self.assertIn("title_sha256", audit)

    def test_replay_and_historical_evidence_do_not_send_notification(self):
        with mock.patch("mini_harness_core.agent.request_approval", return_value=True):
            self.execute()
        adapter = FakeNotification()
        replay = replay_policy_events(self.events(),
                                      build_policy_snapshot())
        self.assertTrue(replay[0]["match"])
        self.assertEqual(adapter.calls, [])
        evidence = json.loads(next((self.audit / "evidence").iterdir()).read_text())
        self.assertEqual(evidence["freshness"]["run_id"], RUN)
        self.assertNotEqual(evidence["freshness"]["run_id"], "6" * 32)

    def test_secret_argument_is_rejected_before_envelope_and_adapter(self):
        adapter = FakeNotification()
        self.execute(adapter=adapter, decisions=[
            {"type": "tool_call", "tool": NOTIFICATION_LOGICAL_CAPABILITY,
             "arguments": {"title": "Authorization Bearer secret",
                           "content": "hello"}},
            {"type": "final_answer", "final_answer": "denied"},
        ])
        self.assertEqual(adapter.calls, [])
        corpus = "\n".join(path.read_text() for path in self.audit.rglob("*")
                           if path.is_file())
        self.assertNotIn("Authorization Bearer secret", corpus)


class NotificationBridgeE2ETests(unittest.TestCase):
    def test_bridge_claim_cannot_replace_harness_approval(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / "bridge"
            audit = Path(base) / "audit"
            sessions = Path(base) / "sessions"
            root.mkdir()
            for name in ("inbox", "claims", "reconciliations", "outbox", "locks"):
                (root / name).mkdir()
            publish_bridge_task(root, "task-notification", "bridge_harness_task",
                                {"request": "发送通知 Phase 2 / hello"}, "test")
            adapter = FakeNotification()
            provider = ScriptedFakeProvider([
                {"type": "tool_call", "tool": NOTIFICATION_LOGICAL_CAPABILITY,
                 "arguments": dict(ARGS)},
                {"type": "final_answer", "final_answer": "通知请求已接受"},
            ])

            def runner(task, provider, **kwargs):
                with mock.patch(
                    "mini_harness_core.environment.registry.invoke_termux_notification",
                    side_effect=adapter,
                ):
                    return run_agent(task, provider, **kwargs)

            with mock.patch("mini_harness_core.agent.request_approval",
                            return_value=True) as approval:
                result = run_bridge_harness_worker_once(
                    root, "consumer", provider, audit_directory=audit,
                    session_directory=sessions, harness_runner=runner,
                )
            approval.assert_called_once()
            self.assertEqual(adapter.calls, [("Phase 2", "hello")])
            self.assertEqual(result.final_state, COMPLETED)
            self.assertEqual(inspect_bridge_task(root, "task-notification").state,
                             COMPLETED)


if __name__ == "__main__":
    unittest.main()
