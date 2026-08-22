import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mini_harness_core.agent import run_agent
from mini_harness_core.audit import AuditWriter
from mini_harness_core.bridge_harness_worker import run_bridge_harness_worker_once
from mini_harness_core.bridge_inspector import COMPLETED, inspect_bridge_task
from mini_harness_core.bridge_publisher import publish_bridge_task
from mini_harness_core.dispatch import dispatch_authorized_action
from mini_harness_core.evidence import EvidenceStore, validate_evidence
from mini_harness_core.policy_composition import (
    ALLOW, ASK, CAPABILITY_PROFILES, GLOBAL_SECURITY_POLICY,
    NEUTRAL_DELEGATED_CEILING, ZONE_POLICIES, StaticPolicyLayer,
)
from mini_harness_core.policy_snapshot import (
    PolicyBinding, build_policy_snapshot, policy_fingerprint, replay_policy_events,
)
from mini_harness_core.result import ResultStore
from mini_harness_core.termux_capabilities import LOGICAL_CAPABILITY
from mini_harness_core.environment_registry import (
    ENVIRONMENT_REGISTRY, classify_environment_capability,
)
from mini_harness_core.environment_adapters import EnvironmentAdapterResult
from tests.helpers.bridge import ScriptedFakeProvider


SESSION = "1" * 32
RUN = "2" * 32
EMPTY_SHA = "0" * 64


class FakeBatteryAdapter:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [self.success()])
        self.calls = []

    @staticmethod
    def success(percentage=77):
        return EnvironmentAdapterResult(
            LOGICAL_CAPABILITY, "succeeded", "read_only", "no_side_effect", {
                "percentage": percentage, "status": "DISCHARGING",
                "plugged": "UNPLUGGED",
            }, 0, 100, EMPTY_SHA, 0, EMPTY_SHA,
        )

    def __call__(self, name):
        self.calls.append(name)
        return self.outcomes.pop(0)


def binding_with_external(disposition=ALLOW):
    zones = dict(ZONE_POLICIES)
    old = zones["external"]
    zones["external"] = StaticPolicyLayer(
        "zone", disposition, old.allowed_tools, old.max_effect,
        old.can_write_workspace, old.can_use_mcp,
    )
    snapshot = build_policy_snapshot(zone_policies=zones)
    return PolicyBinding(snapshot, policy_fingerprint(snapshot))


class TermuxHarnessIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.audit = self.root / "audit"
        self.messages = []

    def tearDown(self):
        self.temp.cleanup()

    def execute(self, adapter=None, binding=None, decisions=None, **kwargs):
        adapter = adapter or FakeBatteryAdapter()
        provider = ScriptedFakeProvider(decisions or [
            {"type": "tool_call", "tool": LOGICAL_CAPABILITY, "arguments": {}},
            {"type": "final_answer", "final_answer": "当前电量 77%"},
        ])
        with mock.patch(
            "mini_harness_core.environment_registry.invoke_termux_capability",
            side_effect=adapter,
        ):
            result = run_agent(
                "告诉我当前电量", provider, messages=self.messages,
                audit_writer=AuditWriter(SESSION, RUN, self.audit),
                evidence_store=EvidenceStore(self.audit / "evidence"),
                result_store=ResultStore(self.audit / "results"),
                return_result=True, policy_binding=binding,
                retry_sleeper=lambda _delay: None, **kwargs,
            )
        return result, adapter

    def test_allow_path_authorized_observation_evidence_and_result(self):
        result, adapter = self.execute()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(adapter.calls, ["battery_status"])
        tool = json.loads(next(
            message["content"] for message in reversed(self.messages)
            if message["role"] == "tool"
        ))
        self.assertEqual(tool["structured"]["percentage"], 77)
        self.assertEqual(set(tool["structured"]),
                         {"percentage", "status", "plugged"})
        evidence = json.loads(next((self.audit / "evidence").iterdir()).read_text())
        validate_evidence(evidence)
        self.assertTrue(evidence["verification"]["accepted"])
        self.assertEqual(
            evidence["content_identity"]["safe_observation"]["percentage"], 77)

    def test_ask_read_only_requires_approval_without_verification(self):
        with mock.patch("mini_harness_core.agent.request_approval",
                        return_value=True) as approval:
            result, adapter = self.execute(binding=binding_with_external(ASK))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(approval.call_count, 1)
        self.assertEqual(len(adapter.calls), 1)
        self.assertFalse(any(e["event_type"] == "verification_state_changed"
                             for e in self.events()))

    def test_deny_and_removed_delegation_call_adapter_zero_before_approval(self):
        delegated = {
            "policy": "ALLOW", "allowed_tools": ["builtin", "mcp", "shell"],
            "max_effect": "side_effecting", "can_write_workspace": True,
            "can_use_mcp": True,
        }
        adapter = FakeBatteryAdapter()
        with mock.patch("mini_harness_core.agent.request_approval") as approval:
            self.execute(adapter=adapter, termux_delegated_ceiling=delegated)
        self.assertEqual(adapter.calls, [])
        approval.assert_not_called()

    def test_unknown_termux_fails_closed(self):
        adapter = FakeBatteryAdapter()
        self.execute(adapter=adapter, decisions=[
            {"type": "tool_call", "tool": "termux:unknown", "arguments": {}},
            {"type": "final_answer", "final_answer": "blocked"},
        ])
        self.assertEqual(adapter.calls, [])

    def test_direct_dispatch_requires_sealed_authorized_action(self):
        with self.assertRaises(PermissionError):
            dispatch_authorized_action(
                {"capability": LOGICAL_CAPABILITY}, {},
                persist_checkpoint=lambda _value: None,
                executor=lambda _args: FakeBatteryAdapter.success(),
            )

    def test_raw_output_and_executable_absent_everywhere(self):
        self.execute()
        corpus = "\n".join(
            path.read_text(encoding="utf-8")
            for path in self.audit.rglob("*") if path.is_file()
        ) + json.dumps(self.messages)
        self.assertNotIn("raw stdout body", corpus)
        self.assertNotIn("/data/data/com.termux", corpus)
        self.assertNotIn('"stdout":', corpus)

    def test_timeout_is_bounded_retry_and_never_unknown_effect(self):
        timeout = EnvironmentAdapterResult(
            LOGICAL_CAPABILITY, "failed", "read_only", "no_side_effect", {},
            None, 0, EMPTY_SHA, 0, EMPTY_SHA, "TIMEOUT",
        )
        adapter = FakeBatteryAdapter([timeout, timeout, FakeBatteryAdapter.success()])
        result, _ = self.execute(adapter=adapter)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(adapter.calls), 3)
        self.assertFalse(any(e.get("outcome") == "unknown" for e in self.events()))

    def test_invalid_response_is_not_reconciled(self):
        invalid = EnvironmentAdapterResult(
            LOGICAL_CAPABILITY, "failed", "read_only", "no_side_effect", {},
            1, 0, EMPTY_SHA, 0, EMPTY_SHA, "INVALID_RESPONSE",
        )
        adapter = FakeBatteryAdapter([invalid])
        self.execute(adapter=adapter)
        self.assertEqual(len(adapter.calls), 1)
        self.assertFalse(any("reconcil" in e["event_type"] for e in self.events()))

    def test_historical_evidence_is_not_current_reality(self):
        self.execute()
        evidence = json.loads(next((self.audit / "evidence").iterdir()).read_text())
        self.assertEqual(evidence["freshness"]["run_id"], RUN)
        self.assertNotEqual(evidence["freshness"]["run_id"], "3" * 32)

    def test_registry_manifest_identity_is_stable_and_safe(self):
        self.execute()
        manifest = json.loads(next((self.audit / "manifests").iterdir()).read_text())
        identity = manifest["configuration"]["capabilities"]["termux_capability_registry"]
        self.assertEqual(identity, ENVIRONMENT_REGISTRY.identity())
        self.assertNotIn("executable", json.dumps(identity))

    def test_replay_policy_invokes_adapter_zero_times(self):
        self.execute()
        adapter = FakeBatteryAdapter()
        replayed = replay_policy_events(self.events(), binding_with_external().snapshot)
        self.assertTrue(replayed[0]["match"])
        self.assertEqual(adapter.calls, [])

    def test_mcp_cannot_impersonate_harness_termux_registry(self):
        policy = classify_environment_capability(
            "mcp:fake:termux:battery_status", binding_with_external().snapshot)
        self.assertEqual(policy["action"], "DENY")

    def events(self):
        return [json.loads(line) for line in
                (self.audit / (RUN + ".jsonl")).read_text().splitlines()]


class TermuxBridgeE2ETests(unittest.TestCase):
    def test_bridge_claim_does_not_bypass_harness_authority(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / "bridge"
            audit = Path(base) / "audit"
            sessions = Path(base) / "sessions"
            root.mkdir()
            for name in ("inbox", "claims", "reconciliations", "outbox", "locks"):
                (root / name).mkdir()
            publish_bridge_task(root, "task-bridge-battery",
                                "bridge_harness_task",
                                {"request": "告诉我当前电量"}, "test")
            adapter = FakeBatteryAdapter()
            provider = ScriptedFakeProvider([
                {"type": "tool_call", "tool": LOGICAL_CAPABILITY, "arguments": {}},
                {"type": "final_answer", "final_answer": "当前电量 77%"},
            ])

            def runner(task, provider, **kwargs):
                with mock.patch(
                    "mini_harness_core.environment_registry.invoke_termux_capability",
                    side_effect=adapter,
                ):
                    return run_agent(task, provider, **kwargs)

            result = run_bridge_harness_worker_once(
                root, "consumer", provider, audit_directory=audit,
                session_directory=sessions, harness_runner=runner,
            )
            self.assertEqual(result.final_state, COMPLETED)
            self.assertEqual(adapter.calls, ["battery_status"])
            events = [json.loads(line) for line in
                      (audit / (result.harness_run_id + ".jsonl")).read_text().splitlines()]
            self.assertTrue(any(e["event_type"] == "policy_decision" for e in events))
            self.assertTrue(any(e["event_type"] == "action_state_changed" and
                                e["outcome"] == "started" for e in events))
            self.assertEqual(inspect_bridge_task(root, "task-bridge-battery").state,
                             COMPLETED)


if __name__ == "__main__":
    unittest.main()
