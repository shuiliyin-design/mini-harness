import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mini_harness_core.agent import run_agent
from mini_harness_core.audit import read_events
from mini_harness_core.bridge_adapter import (
    HARNESS_RECOVERY_REQUIRED, BridgeBindingStore, run_bound_bridge_request,
)
from mini_harness_core.bridge_harness_worker import (
    IDLE, run_bridge_harness_worker_once,
)
from mini_harness_core.bridge_inspector import COMPLETED
from mini_harness_core.bridge_publisher import publish_bridge_task
from mini_harness_core.environment_adapters import EnvironmentAdapterResult
from mini_harness_core.evidence import EvidenceStore, create_evidence
from mini_harness_core.fault_injection import (
    DeterministicFaultInjector, InjectedFault,
)
from mini_harness_core.mobile_orchestration import (
    BATTERY_CAPABILITY, MobileWorkflowError, MobileWorkflowOutputStore,
    evaluate_battery_condition, replay_mobile_workflow_output,
)
from mini_harness_core.result import result_integrity_check
from mini_harness_core.run_envelope import (
    RunEnvelopeStore, harness_replay_check,
)
from tests.helpers.bridge import CONSUMER, ScriptedFakeProvider


SHA = "0" * 64
NOTIFICATION_ARGS = {
    "title": "低电量提醒", "content": "当前电量低于阈值",
}


class FakeBattery:
    def __init__(self, percentage=80, results=None):
        self.percentage = percentage
        self.results = list(results or [])
        self.calls = 0

    @staticmethod
    def timeout():
        return EnvironmentAdapterResult(
            BATTERY_CAPABILITY, "failed", "read_only", "no_side_effect", {},
            None, 0, SHA, 0, SHA, "TIMEOUT",
        )

    def __call__(self, name):
        self.calls += 1
        if self.results:
            return self.results.pop(0)
        return EnvironmentAdapterResult(
            BATTERY_CAPABILITY, "succeeded", "read_only", "no_side_effect",
            {"percentage": self.percentage, "status": "DISCHARGING"},
            0, 0, SHA, 0, SHA,
        )


class FakeNotification:
    def __init__(self, result=None):
        self.result = result or EnvironmentAdapterResult(
            "termux:notification", "succeeded", "side_effecting",
            "known_applied", {"notification_requested": True,
                              "request_accepted": True},
            0, 0, SHA, 0, SHA,
        )
        self.calls = []

    @staticmethod
    def timeout():
        return EnvironmentAdapterResult(
            "termux:notification", "failed", "side_effecting", "unknown",
            {}, None, 0, SHA, 0, SHA, "TIMEOUT",
        )

    def __call__(self, title, content):
        self.calls.append((title, content))
        return self.result


class MobileAgentOrchestrationTests(unittest.TestCase):
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

    def publish(self, task_id="mobile-task", threshold=30):
        return publish_bridge_task(
            self.bridge, task_id, "bridge_harness_task",
            {"request": "检查当前电量，如果低于阈值就发通知",
             "threshold": threshold},
            "mobile-test",
        )

    def worker(self, provider, battery, notification, **kwargs):
        with mock.patch(
            "mini_harness_core.environment_registry.invoke_termux_capability",
            side_effect=battery,
        ), mock.patch(
            "mini_harness_core.environment_registry.invoke_termux_notification",
            side_effect=notification,
        ):
            return run_bridge_harness_worker_once(
                self.bridge, CONSUMER, provider,
                audit_directory=self.audit, session_directory=self.sessions,
                harness_runner=run_agent, max_steps=5, **kwargs,
            )

    def output(self, run_id):
        return MobileWorkflowOutputStore(
            self.audit / "mobile_workflow_outputs",
        ).load(run_id)

    def only_binding(self):
        path = next((self.audit / "bridge_bindings").glob("*/*.json"))
        raw = json.loads(path.read_text(encoding="utf-8"))
        return BridgeBindingStore(self.audit).load(
            raw["task_id"], raw["claim_nonce"],
        )

    def assert_result_match(self, run_id):
        self.assertTrue(result_integrity_check(
            run_id,
            result_directory=self.audit / "results",
            evidence_directory=self.audit / "evidence",
            audit_directory=self.audit,
        ))
        events = read_events(run_id, self.audit)
        finalized = [event for event in events if (
            event["event_type"] == "authoritative_candidate_finalized"
        )]
        self.assertEqual(len(finalized), 1)
        emitted = [event for event in events
                   if event["event_type"] == "final_result_emitted"]
        self.assertEqual(len(emitted), 1)
        self.assertLess(finalized[0]["sequence"], emitted[0]["sequence"])
        return events, finalized[0]

    def test_battery_80_completes_without_notification(self):
        self.publish()
        battery, notification = FakeBattery(80), FakeNotification()
        result = self.worker(ScriptedFakeProvider([
            {"type": "tool_call", "tool": BATTERY_CAPABILITY,
             "arguments": {}},
            {"type": "final_answer", "final_answer": "model summary"},
        ]), battery, notification)
        self.assertEqual(result.harness_result_status, "completed")
        self.assertEqual(battery.calls, 1)
        self.assertEqual(notification.calls, [])
        output = self.output(result.harness_run_id)
        self.assertEqual(
            (output["battery_percentage"], output["notification_required"],
             output["notification_request_accepted"], output["branch"]),
            (80, False, None, "not_required"),
        )
        events, finalized = self.assert_result_match(result.harness_run_id)
        model = next(event for event in events
                     if event["event_type"] == "model_final_candidate_received")
        self.assertNotEqual(
            model["references"]["candidate_digest"],
            finalized["references"]["candidate_digest"],
        )
        self.assertEqual(
            finalized["references"]["normalization_source"],
            "mobile_output_contract",
        )

    def test_battery_20_asks_then_accepts_notification(self):
        self.publish()
        battery, notification = FakeBattery(20), FakeNotification()
        provider = ScriptedFakeProvider([
            {"type": "tool_call", "tool": BATTERY_CAPABILITY,
             "arguments": {}},
            {"type": "tool_call", "tool": "termux:notification",
             "arguments": dict(NOTIFICATION_ARGS)},
            {"type": "final_answer", "final_answer": "model summary"},
        ])
        with mock.patch(
            "mini_harness_core.agent.request_approval", return_value=True,
        ) as approval:
            result = self.worker(provider, battery, notification)
        approval.assert_called_once()
        self.assertEqual(result.harness_result_status, "completed")
        self.assertEqual(len(notification.calls), 1)
        output = self.output(result.harness_run_id)
        self.assertEqual(
            (output["notification_required"],
             output["notification_request_accepted"], output["branch"]),
            (True, True, "accepted"),
        )
        self.assertEqual(len(output["evidence_ids"]), 3)
        events, finalized = self.assert_result_match(result.harness_run_id)
        model = next(event for event in events
                     if event["event_type"] == "model_final_candidate_received")
        self.assertNotEqual(
            model["references"]["candidate_digest"],
            finalized["references"]["candidate_digest"],
        )

    def test_approval_denied_is_authoritative_incomplete(self):
        self.publish()
        battery, notification = FakeBattery(20), FakeNotification()
        provider = ScriptedFakeProvider([
            {"type": "tool_call", "tool": BATTERY_CAPABILITY,
             "arguments": {}},
            {"type": "tool_call", "tool": "termux:notification",
             "arguments": dict(NOTIFICATION_ARGS)},
        ])
        with mock.patch(
            "mini_harness_core.agent.request_approval", return_value=False,
        ):
            result = self.worker(provider, battery, notification)
        self.assertEqual(result.harness_result_status, "incomplete")
        self.assertEqual(notification.calls, [])
        output = self.output(result.harness_run_id)
        self.assertEqual(output["branch"], "approval_denied")
        self.assertEqual(
            output["unsatisfied_requirements"],
            ["notification_not_authorized"],
        )
        projection = json.loads((
            self.bridge / "outbox/result-mobile-task.json"
        ).read_text(encoding="utf-8"))
        self.assertEqual(projection["status"], "completed")
        self.assertEqual(
            projection["result"]["harness_result_status"], "incomplete",
        )
        self.assertFalse(projection["result"]["workflow_output"]["satisfied"])
        events, finalized = self.assert_result_match(result.harness_run_id)
        self.assertFalse(any(
            event["event_type"] == "model_final_candidate_received"
            for event in events
        ))
        self.assertEqual(
            finalized["references"]["mobile_contract_satisfied"], False,
        )

    def _battery_evidence(self, run_id, percentage, scope="run"):
        return create_evidence(
            run_id, "termux_observation",
            {"kind": "capability", "target": BATTERY_CAPABILITY,
             "claim": "environment_observation_recorded"},
            source={"capability": BATTERY_CAPABILITY, "action_id": "a" * 32,
                    "observation_event_id": "event"},
            verification={"accepted": True, "effect": "read_only",
                          "effect_certainty": "no_side_effect"},
            freshness={"scope": scope, "observed_at": "now", "run_id": run_id},
            content_identity={"safe_observation": {"percentage": percentage}},
        )

    def test_stale_and_historical_battery_evidence_cannot_drive_condition(self):
        run_id = "1" * 32
        stale = self._battery_evidence(run_id, 20, scope="historical")
        with self.assertRaisesRegex(MobileWorkflowError, "fresh current-run"):
            evaluate_battery_condition(stale, 30, run_id)
        historical = self._battery_evidence("2" * 32, 20)
        with self.assertRaisesRegex(MobileWorkflowError, "fresh current-run"):
            evaluate_battery_condition(historical, 30, run_id)

    def test_battery_timeout_retries_bounded_then_succeeds(self):
        self.publish()
        battery = FakeBattery(80, [FakeBattery.timeout()])
        result = self.worker(ScriptedFakeProvider([
            {"type": "tool_call", "tool": BATTERY_CAPABILITY,
             "arguments": {}},
            {"type": "final_answer", "final_answer": "done"},
        ]), battery, FakeNotification())
        self.assertEqual(result.harness_result_status, "completed")
        self.assertEqual(battery.calls, 2)

    def test_notification_timeout_is_unknown_blocked_without_retry(self):
        self.publish()
        notification = FakeNotification(FakeNotification.timeout())
        provider = ScriptedFakeProvider([
            {"type": "tool_call", "tool": BATTERY_CAPABILITY,
             "arguments": {}},
            {"type": "tool_call", "tool": "termux:notification",
             "arguments": dict(NOTIFICATION_ARGS)},
        ])
        with mock.patch(
            "mini_harness_core.agent.request_approval", return_value=True,
        ):
            result = self.worker(provider, FakeBattery(20), notification)
        self.assertEqual(result.harness_result_status, "blocked")
        self.assertEqual(len(notification.calls), 1)
        self.assertEqual(self.output(result.harness_run_id)["branch"], "unknown")

    def test_crash_after_battery_evidence_resumes_without_battery_recall(self):
        self.publish()
        battery, notification = FakeBattery(80), FakeNotification()
        provider = ScriptedFakeProvider([
            {"type": "tool_call", "tool": BATTERY_CAPABILITY,
             "arguments": {}},
            {"type": "final_answer", "final_answer": "done"},
        ])
        fault = DeterministicFaultInjector([
            "after_battery_evidence_before_condition",
        ])
        with self.assertRaises(InjectedFault):
            self.worker(provider, battery, notification,
                        harness_fault_injector=fault)
        binding = self.only_binding()
        with mock.patch(
            "mini_harness_core.environment_registry.invoke_termux_capability",
            side_effect=battery,
        ), mock.patch(
            "mini_harness_core.environment_registry.invoke_termux_notification",
            side_effect=notification,
        ):
            resumed = run_bound_bridge_request(
                self.bridge, self.audit, self.sessions, binding, CONSUMER,
                provider, run_agent,
            )
        self.assertEqual(resumed.harness_run_id, binding["harness_run_id"])
        self.assertEqual(resumed.harness_result_status, "completed")
        self.assertEqual(battery.calls, 1)

    def test_crash_before_notification_approval_requires_fresh_approval(self):
        self.publish()
        battery, notification = FakeBattery(20), FakeNotification()
        provider = ScriptedFakeProvider([
            {"type": "tool_call", "tool": BATTERY_CAPABILITY,
             "arguments": {}},
            {"type": "tool_call", "tool": "termux:notification",
             "arguments": dict(NOTIFICATION_ARGS)},
            {"type": "tool_call", "tool": "termux:notification",
             "arguments": dict(NOTIFICATION_ARGS)},
            {"type": "final_answer", "final_answer": "done"},
        ])
        fault = DeterministicFaultInjector([
            "after_condition_before_notification_approval",
        ])
        with mock.patch(
            "mini_harness_core.agent.request_approval",
        ) as first_approval, self.assertRaises(InjectedFault):
            self.worker(provider, battery, notification,
                        harness_fault_injector=fault)
        first_approval.assert_not_called()
        binding = self.only_binding()
        with mock.patch(
            "mini_harness_core.environment_registry.invoke_termux_capability",
            side_effect=battery,
        ), mock.patch(
            "mini_harness_core.environment_registry.invoke_termux_notification",
            side_effect=notification,
        ), mock.patch(
            "mini_harness_core.agent.request_approval", return_value=True,
        ) as fresh_approval:
            resumed = run_bound_bridge_request(
                self.bridge, self.audit, self.sessions, binding, CONSUMER,
                provider, run_agent,
            )
        fresh_approval.assert_called_once()
        self.assertEqual(resumed.harness_result_status, "completed")
        self.assertEqual(len(notification.calls), 1)

    def test_crash_after_notification_evidence_resumes_without_resend(self):
        self.publish()
        battery, notification = FakeBattery(20), FakeNotification()
        provider = ScriptedFakeProvider([
            {"type": "tool_call", "tool": BATTERY_CAPABILITY,
             "arguments": {}},
            {"type": "tool_call", "tool": "termux:notification",
             "arguments": dict(NOTIFICATION_ARGS)},
            {"type": "final_answer", "final_answer": "done"},
        ])
        fault = DeterministicFaultInjector([
            "after_notification_evidence_before_result",
        ])
        with mock.patch(
            "mini_harness_core.agent.request_approval", return_value=True,
        ), self.assertRaises(InjectedFault):
            self.worker(provider, battery, notification,
                        harness_fault_injector=fault)
        binding = self.only_binding()
        with mock.patch(
            "mini_harness_core.environment_registry.invoke_termux_capability",
            side_effect=battery,
        ), mock.patch(
            "mini_harness_core.environment_registry.invoke_termux_notification",
            side_effect=notification,
        ):
            resumed = run_bound_bridge_request(
                self.bridge, self.audit, self.sessions, binding, CONSUMER,
                provider, run_agent,
            )
        self.assertEqual(resumed.harness_result_status, "completed")
        self.assertEqual(len(notification.calls), 1)

    def test_crash_while_notification_executing_becomes_unknown_no_resend(self):
        self.publish()
        battery, notification = FakeBattery(20), FakeNotification()
        provider = ScriptedFakeProvider([
            {"type": "tool_call", "tool": BATTERY_CAPABILITY,
             "arguments": {}},
            {"type": "tool_call", "tool": "termux:notification",
             "arguments": dict(NOTIFICATION_ARGS)},
        ])
        fault = DeterministicFaultInjector([
            "after_notification_dispatch_before_checkpoint",
        ])
        with mock.patch(
            "mini_harness_core.agent.request_approval", return_value=True,
        ), self.assertRaises(InjectedFault):
            self.worker(provider, battery, notification,
                        harness_fault_injector=fault)
        binding = self.only_binding()
        with mock.patch(
            "mini_harness_core.environment_registry.invoke_termux_capability",
            side_effect=battery,
        ), mock.patch(
            "mini_harness_core.environment_registry.invoke_termux_notification",
            side_effect=notification,
        ):
            resumed = run_bound_bridge_request(
                self.bridge, self.audit, self.sessions, binding, CONSUMER,
                provider, run_agent,
            )
        self.assertEqual(resumed.harness_result_status, "blocked")
        self.assertEqual(len(notification.calls), 1)
        self.assertEqual(self.output(resumed.harness_run_id)["branch"], "unknown")

    def test_duplicate_bridge_entry_reuses_transport_and_no_notification(self):
        self.publish()
        battery, notification = FakeBattery(20), FakeNotification()
        provider = ScriptedFakeProvider([
            {"type": "tool_call", "tool": BATTERY_CAPABILITY,
             "arguments": {}},
            {"type": "tool_call", "tool": "termux:notification",
             "arguments": dict(NOTIFICATION_ARGS)},
            {"type": "final_answer", "final_answer": "done"},
        ])
        with mock.patch(
            "mini_harness_core.agent.request_approval", return_value=True,
        ):
            first = self.worker(provider, battery, notification)
        self.publish()
        second = self.worker(ScriptedFakeProvider([]), battery, notification)
        self.assertEqual(first.final_state, COMPLETED)
        self.assertEqual(second.action, IDLE)
        self.assertEqual(len(notification.calls), 1)
        self.assertEqual(
            len(list((self.audit / "bridge_bindings").glob("*/*.json"))), 1,
        )

    def test_historical_replay_calls_both_capabilities_zero_times(self):
        self.publish()
        battery, notification = FakeBattery(20), FakeNotification()
        provider = ScriptedFakeProvider([
            {"type": "tool_call", "tool": BATTERY_CAPABILITY,
             "arguments": {}},
            {"type": "tool_call", "tool": "termux:notification",
             "arguments": dict(NOTIFICATION_ARGS)},
            {"type": "final_answer", "final_answer": "done"},
        ])
        with mock.patch(
            "mini_harness_core.agent.request_approval", return_value=True,
        ):
            result = self.worker(provider, battery, notification)
        before = (battery.calls, len(notification.calls))
        output = self.output(result.harness_run_id)
        self.assertEqual(
            replay_mobile_workflow_output(
                output, EvidenceStore(self.audit / "evidence"),
            ),
            "MATCH",
        )
        envelope = RunEnvelopeStore(self.audit / "envelopes").load(
            result.harness_run_id,
        )
        self.assertTrue(harness_replay_check(
            envelope, self.audit,
        )["match"])
        self.assert_result_match(result.harness_run_id)
        self.assertEqual((battery.calls, len(notification.calls)), before)

    def test_bridge_projection_waits_for_result_integrity(self):
        self.publish()
        battery, notification = FakeBattery(80), FakeNotification()
        provider = ScriptedFakeProvider([
            {"type": "tool_call", "tool": BATTERY_CAPABILITY,
             "arguments": {}},
            {"type": "final_answer", "final_answer": "model summary"},
        ])
        with mock.patch(
            "mini_harness_core.bridge_adapter._harness_result_integrity_valid",
            return_value=False,
        ):
            result = self.worker(provider, battery, notification)
        self.assertEqual(result.reason, HARNESS_RECOVERY_REQUIRED)
        self.assertFalse((
            self.bridge / "outbox/result-mobile-task.json"
        ).exists())
        self.assertFalse((
            self.bridge / "outbox/result-mobile-task.ready"
        ).exists())


if __name__ == "__main__":
    unittest.main()
