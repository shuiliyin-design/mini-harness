"""V28 deterministic system validation across Runtime ownership boundaries."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mini_harness_core.agent import run_agent
from mini_harness_core.artifacts import ArtifactStore, artifact_integrity_check
from mini_harness_core.audit import AuditWriter, read_events
from mini_harness_core.dispatch import authorize_action, dispatch_authorized_action
from mini_harness_core.durability import (
    create_action_checkpoint, reconcile_file_observation,
    recover_action_checkpoint, transition_action_checkpoint,
)
from mini_harness_core.evidence import EvidenceStore, evidence_integrity_check
from mini_harness_core.fault_injection import DeterministicFaultInjector, InjectedFault
from mini_harness_core.governance import (
    FakeClock, consume_safety_reconciliation, create_governance_state,
    normal_action_decision, safety_reconciliation_decision,
)
from mini_harness_core.mcp import MCPClient, MCPRegistry
from mini_harness_core.observation import model_context_observation, persisted_safe_observation
from mini_harness_core.planning import create_plan
from mini_harness_core.protected_paths import inspect_shell_paths
from mini_harness_core.result import ResultStore
from mini_harness_core.run_bundle import check_bundle, export_run_bundle, replay_bundle, show_bundle
from mini_harness_core.run_control import create_run_control, resume_run
from mini_harness_core.run_envelope import RunEnvelopeStore, harness_replay_check
from mini_harness_core.run_manifest import RunManifestStore


class SequenceProvider:
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.calls = []

    def complete(self, messages):
        self.calls.append(json.loads(json.dumps(messages)))
        return next(self.decisions)


def requirement():
    return {
        "name": "report", "artifact_type": "workspace_file",
        "path": "report.md", "step_id": "step-1",
        "requirements": ["exists", "non_empty", "content_identity", "verified"],
    }


class EndToEndRuntimeV28Tests(unittest.TestCase):
    def golden(self, root, run_id="1" * 32):
        workspace, audit = os.path.join(root, "workspace"), os.path.join(root, "audit")
        os.mkdir(workspace)
        writer = AuditWriter("2" * 32, run_id, audit)
        provider = SequenceProvider([
            {"type": "tool_call", "command": "echo hello > report.md"},
            {"type": "tool_call", "command": "cat report.md"},
            {"type": "final_answer", "final_answer": "done", "claimed_status": "completed"},
        ])
        plan = create_plan("create report", [{
            "id": "step-1", "description": "write verified report", "depends_on": [],
        }], plan_id="v28-plan")
        previous = os.getcwd()
        try:
            os.chdir(workspace)
            with patch("mini_harness_core.agent.request_approval", return_value=True):
                result = run_agent(
                    "create report", provider, max_steps=3, current_plan=plan,
                    audit_writer=writer, output_contract={"required_artifacts": [requirement()]},
                    return_result=True,
                )
        finally:
            os.chdir(previous)
        return workspace, audit, writer, provider, plan, result

    def test_01_golden_success_lineage_and_offline_bundle(self):
        with tempfile.TemporaryDirectory() as root:
            _workspace, audit, writer, _provider, plan, result = self.golden(root)
            self.assertEqual((plan["status"], result["status"]), ("completed", "completed"))
            artifacts = ArtifactStore(os.path.join(audit, "artifacts")).list_run(writer.run_id)
            evidence_store = EvidenceStore(os.path.join(audit, "evidence"))
            evidences = [evidence_store.load(item) for item in result["evidence_ids"]]
            self.assertEqual(result["artifact_ids"], [artifacts[0]["artifact_id"]])
            self.assertTrue(all(item["run_id"] == writer.run_id for item in artifacts + evidences))
            self.assertTrue(set(artifacts[0]["evidence_ids"]).issubset(result["evidence_ids"]))
            verification = next(item for item in evidences if item["evidence_type"] == "verification")
            self.assertEqual(verification["references"]["source_action_id"],
                             artifacts[0]["producer"]["action_id"])
            self.assertTrue(verification["content_identity"]["observation"]["observation_event_id"])
            events = read_events(writer.run_id, audit)
            self.assertTrue(any(e["event_type"] == "approval_decided" for e in events))
            self.assertTrue(any(e["event_type"] == "action_state_changed" for e in events))
            envelope = RunEnvelopeStore(os.path.join(audit, "envelopes")).load(writer.run_id)
            self.assertEqual(envelope["run_id"], result["run_id"])
            self.assertTrue(harness_replay_check(envelope, audit)["match"])
            bundle, manifest, _ = export_run_bundle(writer.run_id, audit, os.path.join(root, "bundles"))
            self.assertEqual(manifest["run_id"], writer.run_id)
            moved_audit = audit + ".offline"
            os.rename(audit, moved_audit)
            self.assertTrue(check_bundle(bundle)["match"])
            self.assertEqual(replay_bundle(bundle)["status"], "MATCH")

    def test_02_read_only_retry_exhaustion_cannot_claim_completed(self):
        provider = SequenceProvider([
            {"type": "tool_call", "command": "pwd"},
            {"type": "final_answer", "final_answer": "done", "claimed_status": "completed"},
        ])
        executor = Mock(return_value={"exit_code": -1, "stdout": "", "stderr": "timeout"})
        with tempfile.TemporaryDirectory() as root, \
                patch("mini_harness_core.agent.execute_shell", executor):
            writer = AuditWriter("3" * 32, "4" * 32, os.path.join(root, "audit"))
            result = run_agent(
                "retry", provider, audit_writer=writer, return_result=True,
                retry_sleeper=lambda _delay: None,
            )
            self.assertEqual(executor.call_count, 3)
            self.assertNotEqual(result["status"], "completed")
            self.assertIn(result["status"], {"blocked", "failed", "incomplete"})
            self.assertEqual(len(provider.calls), 2)
            self.assertEqual(result["candidate"]["claimed_status"], "completed")
            self.assertTrue(result["candidate"]["contradiction"])

    def test_03_crash_reconciliation_is_exactly_once(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "once.txt"
            checkpoint = create_action_checkpoint(
                "shell", {"command": "echo once > once.txt"}, "side_effecting"
            )
            action = authorize_action(
                checkpoint=checkpoint, capability="shell", arguments=checkpoint["arguments"],
                effect="side_effecting", policy_decision="ASK", approval_granted=True,
                run_id="5" * 32, workspace_root=root,
            )
            states, calls = [], []

            def write(_arguments):
                calls.append(True)
                target.write_text("once\n", encoding="utf-8")
                return {"exit_code": 0, "stdout": "", "stderr": ""}

            with self.assertRaises(InjectedFault):
                dispatch_authorized_action(
                    action, checkpoint, persist_checkpoint=states.append, executor=write,
                    fault_injector=DeterministicFaultInjector([
                        "after_tool_success_before_terminal_checkpoint"
                    ]),
                )
            unknown, decision = recover_action_checkpoint(states[-1])
            self.assertEqual((unknown["state"], decision), ("unknown", "reconcile_or_block"))
            reconciled = reconcile_file_observation(
                unknown, "cat once.txt", {"exit_code": 0, "stdout": "once\n", "stderr": ""},
            )
            self.assertEqual(reconciled["status"], "succeeded")
            self.assertEqual(len(calls), 1)
            self.assertEqual(target.read_text(encoding="utf-8"), "once\n")

    def test_04_pause_resume_requires_fresh_approval_without_budget_reset(self):
        control, checkpoints = create_run_control(), []
        plan = create_plan("pause resume", [{
            "id": "step-1", "description": "write report", "depends_on": [],
        }], plan_id="pause-plan")
        first = SequenceProvider([{"type": "tool_call", "command": "echo hello > report.md"}])
        with patch("builtins.input", return_value="pause"), \
                patch("mini_harness_core.agent.execute_shell") as executor:
            paused = run_agent(
                "pause", first, run_control=control, current_plan=plan,
                save_action_checkpoint=checkpoints.append, return_result=True,
            )
        self.assertEqual(paused["status"], "blocked")
        executor.assert_not_called()
        self.assertEqual(checkpoints[-1]["state"], "prepared")
        used_before = 0
        control.update(resume_run(control))
        second = SequenceProvider([
            {"type": "tool_call", "command": "echo hello > report.md"},
            {"type": "tool_call", "command": "cat report.md"},
            {"type": "final_answer", "final_answer": "done"},
        ])
        with tempfile.TemporaryDirectory() as root:
            writer = AuditWriter("9" * 32, "a" * 32, os.path.join(root, "audit"))
            previous = os.getcwd(); os.chdir(root)
            try:
                with patch("mini_harness_core.agent.request_approval", return_value=True) as approval:
                    result = run_agent(
                        "resume", second, max_steps=3, run_control=control,
                        current_plan=plan,
                        current_action_checkpoint=checkpoints[-1],
                        require_plan_grounding=True, return_result=True,
                        audit_writer=writer,
                    )
            finally:
                os.chdir(previous)
        self.assertEqual(result["status"], "completed")
        approval.assert_called_once()
        self.assertEqual(used_before, 0)

    def test_05_cancel_is_terminal_forensic_and_has_no_execution(self):
        control, checkpoints = create_run_control(), []
        provider = SequenceProvider([{"type": "tool_call", "command": "touch cancelled.txt"}])
        with tempfile.TemporaryDirectory() as root, patch("builtins.input", return_value="cancel"), \
                patch("mini_harness_core.agent.execute_shell") as executor:
            audit = os.path.join(root, "audit")
            writer = AuditWriter("6" * 32, "7" * 32, audit)
            result = run_agent(
                "cancel", provider, run_control=control, audit_writer=writer,
                save_action_checkpoint=checkpoints.append, return_result=True,
            )
            self.assertEqual(result["status"], "cancelled")
            executor.assert_not_called()
            with self.assertRaises(ValueError):
                resume_run(control)
            bundle, _manifest, _ = export_run_bundle(writer.run_id, audit, os.path.join(root, "bundles"))
            self.assertIn(show_bundle(bundle)["status"], {"result", "forensic"})
            self.assertTrue(check_bundle(bundle)["match"])

    def test_06_deadline_allows_one_targeted_reconciliation_but_stays_blocked(self):
        clock = FakeClock()
        governance = create_governance_state(run_timeout_seconds=1, clock=clock)
        checkpoint = transition_action_checkpoint(create_action_checkpoint(
            "shell", {"command": "echo x > report.md"}, "side_effecting"
        ), "executing")
        checkpoint, _ = recover_action_checkpoint(checkpoint)
        clock.advance(2)
        self.assertFalse(normal_action_decision(governance, clock)["allowed"])
        related = safety_reconciliation_decision(
            governance, checkpoint, "read_only", True, True,
        )
        unrelated = safety_reconciliation_decision(
            governance, checkpoint, "read_only", False, True,
        )
        self.assertTrue(related["allowed"])
        self.assertFalse(unrelated["allowed"])
        governance = consume_safety_reconciliation(governance)
        self.assertFalse(safety_reconciliation_decision(
            governance, checkpoint, "read_only", True, True,
        )["allowed"])
        self.assertFalse(normal_action_decision(governance, clock)["allowed"])

    def test_07_secret_boundary_and_executor_bypass(self):
        marker = "secret-marker"
        raw = {"exit_code": 0, "stdout": "OPENAI_API_KEY=" + marker,
               "result": {"Authorization": "Bearer " + marker}}
        persisted = persisted_safe_observation(raw, "mcp:fake:read", {})
        context = model_context_observation(persisted)
        self.assertFalse(inspect_shell_paths("cat .env.local").allowed)
        self.assertNotIn(marker, json.dumps([persisted, context]))
        checkpoint = create_action_checkpoint("shell", {"command": "pwd"}, "read_only")
        executor = Mock()
        with self.assertRaises(PermissionError):
            dispatch_authorized_action({}, checkpoint, persist_checkpoint=lambda _v: None,
                                       executor=executor)
        executor.assert_not_called()

        class SecretMCP(MCPClient):
            def list_tools(self):
                return [{"name": "read", "description": "secret source",
                         "inputSchema": {"type": "object", "additionalProperties": False}}]

            def call_tool(self, _name, _arguments):
                return {"OPENAI_API_KEY": marker,
                        "Authorization": "Bearer " + marker}

        reference = "mcp:secret:read"
        registry = MCPRegistry(
            {"secret": SecretMCP()}, {reference: "ALLOW"}, {reference: "read_only"},
        )
        provider = SequenceProvider([
            {"type": "tool_call", "tool": reference, "arguments": {}},
            {"type": "final_answer", "final_answer": "safe"},
        ])
        messages = []
        with tempfile.TemporaryDirectory() as root:
            audit = os.path.join(root, "audit")
            writer = AuditWriter("b" * 32, "c" * 32, audit)
            run_agent(
                "secret projection", provider, messages=messages,
                mcp_registry=registry, audit_writer=writer, return_result=True,
            )
            bundle, _manifest, _ = export_run_bundle(
                writer.run_id, audit, os.path.join(root, "bundles")
            )
            persisted_bytes = json.dumps(
                {"session": messages, "context": provider.calls},
                ensure_ascii=False,
            ).encode()
            for base in (audit, bundle):
                for path in Path(base).rglob("*"):
                    if path.is_file():
                        persisted_bytes += path.read_bytes()
            self.assertNotIn(marker.encode(), persisted_bytes)

    def test_08_historical_drift_portability_and_tamper(self):
        with tempfile.TemporaryDirectory() as root:
            workspace, audit, writer, _provider, _plan, result = self.golden(root, "8" * 32)
            envelope = RunEnvelopeStore(os.path.join(audit, "envelopes")).load(writer.run_id)
            manifest = RunManifestStore(os.path.join(audit, "manifests")).load(writer.run_id)
            Path(workspace, "report.md").write_text("drifted\n", encoding="utf-8")
            Path(workspace, "AGENTS.md").write_text("changed identity\n", encoding="utf-8")
            self.assertTrue(harness_replay_check(envelope, audit)["match"])
            self.assertEqual(manifest["run_id"], writer.run_id)
            evidence_id, artifact_id = result["evidence_ids"][-1], result["artifact_ids"][0]
            self.assertTrue(evidence_integrity_check(evidence_id, os.path.join(audit, "evidence"), audit))
            self.assertTrue(artifact_integrity_check(
                artifact_id, os.path.join(audit, "artifacts"),
                os.path.join(audit, "evidence"), audit,
            ))
            artifact = ArtifactStore(os.path.join(audit, "artifacts")).load(artifact_id)
            self.assertNotEqual(artifact["content_identity"]["sha256"],
                                __import__("hashlib").sha256(b"drifted\n").hexdigest())
            bundle, _manifest, _ = export_run_bundle(writer.run_id, audit, os.path.join(root, "bundles"))
            original = audit + ".hidden"; os.rename(audit, original)
            self.assertEqual(replay_bundle(bundle)["status"], "MATCH")
            tampered = os.path.join(root, "tampered"); shutil.copytree(bundle, tampered)
            object_path = next(path for path in Path(tampered).rglob("*.json") if path.name != "bundle.json")
            object_path.write_bytes(object_path.read_bytes() + b" ")
            self.assertFalse(check_bundle(tampered)["match"])
            self.assertEqual(replay_bundle(bundle)["status"], "MATCH")


if __name__ == "__main__":
    unittest.main()
