import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mini_harness import (
    AuditWriter, MCPClient, MCPRegistry, MCP_EFFECT_READ_ONLY,
    AuthorizedAction, authorize_action, classify_shell,
    create_action_checkpoint, create_handoff, dispatch_authorized_action,
    inspect_mcp_paths, inspect_subagent_paths, persisted_safe_observation,
    run_agent,
)


class SequenceProvider:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.calls = []

    def complete(self, messages):
        self.calls.append(json.loads(json.dumps(messages)))
        return self.decisions.pop(0)


class SecretMCPClient(MCPClient):
    def list_tools(self):
        return [{
            "name": "lookup", "description": "test",
            "inputSchema": {"type": "object", "additionalProperties": False},
        }]

    def call_tool(self, name, arguments):
        return {"Authorization": "Bearer super-secret-value", "status": "ok"}


class ProtectedPathCeilingTests(unittest.TestCase):
    def test_protected_reads_writes_escape_and_symlink_are_denied(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                for name in (".env", ".env.local", "credentials.json", "token.txt",
                             "secret.key", "id_rsa"):
                    with open(name, "w", encoding="utf-8") as stream:
                        stream.write("not-read")
                with open("README.md", "w", encoding="utf-8") as stream:
                    stream.write("safe")
                os.symlink(".env.local", "linked.txt")
                denied = (
                    "cat .env.local", "cat ./.env.local",
                    "cat foo/../.env.local", "cat .audit/../.env.local",
                    "grep value .env", "head .env.local",
                    "tail token.txt", "sed -n 1p credentials.json",
                    "awk '{print}' secret.key", "echo x > .env",
                    "grep -R value .", "grep -f .env.local README.md",
                    "cat linked.txt", "cat ../outside", "cat /etc/passwd",
                    "cat .sessions/session.json", "cat CREDENTIALS.JSON",
                    "cat ToKeN.data", "cat SECRET-notes",
                )
                for command in denied:
                    with self.subTest(command=command):
                        self.assertEqual(classify_shell(command)["action"], "DENY")
                self.assertEqual(classify_shell("cat README.md")["action"], "ALLOW")
            finally:
                os.chdir(original)

    def test_mcp_and_subagent_path_fields_share_ceiling(self):
        self.assertFalse(inspect_mcp_paths({"path": ".env.local"}).allowed)
        handoff = create_handoff("task")
        handoff["workspace"]["relevant_paths"] = ["token.json"]
        self.assertFalse(inspect_subagent_paths(handoff).allowed)


class ObservationProjectionTests(unittest.TestCase):
    def test_shell_secret_stdout_is_not_in_session_or_model_context(self):
        secret = "Authorization: Bearer super-secret-value"
        messages = []
        provider = SequenceProvider([
            {"type": "tool_call", "command": "pwd"},
            {"type": "final_answer", "final_answer": "done"},
        ])
        with patch("mini_harness_core.agent.execute_shell", return_value={
            "stdout": secret, "stderr": "", "exit_code": 0,
        }):
            run_agent("task", provider, messages=messages)
        self.assertNotIn(secret, json.dumps(messages, ensure_ascii=False))
        self.assertNotIn(secret, json.dumps(provider.calls, ensure_ascii=False))
        stored = json.loads(next(
            message["content"] for message in messages
            if message["role"] == "tool"
        ))
        self.assertEqual(stored["stdout_length"], len(secret))
        self.assertEqual(stored["stdout_sha256"], hashlib.sha256(secret.encode()).hexdigest())

    def test_mcp_secret_body_is_not_in_session_or_context(self):
        reference = "mcp:secrets:lookup"
        registry = MCPRegistry(
            {"secrets": SecretMCPClient()},
            {reference: "ALLOW"}, {reference: MCP_EFFECT_READ_ONLY},
        )
        messages = []
        provider = SequenceProvider([
            {"type": "tool_call", "tool": reference, "arguments": {}},
            {"type": "final_answer", "final_answer": "done"},
        ])
        run_agent("task", provider, messages=messages, mcp_registry=registry)
        encoded = json.dumps({"session": messages, "calls": provider.calls}, ensure_ascii=False)
        self.assertNotIn("super-secret-value", encoded)
        stored = json.loads(next(
            message["content"] for message in messages
            if message["role"] == "tool"
        ))
        self.assertIn("result_sha256", stored)
        self.assertEqual(stored.get("structured"), {"status": "ok"})
        model_observation = json.loads(provider.calls[1][-1]["content"])
        self.assertEqual(model_observation.get("structured"), {"status": "ok"})

    def test_safe_cwd_is_structured_not_raw_stdout(self):
        safe = persisted_safe_observation(
            {"stdout": "/workspace\n", "stderr": "", "exit_code": 0},
            "shell", {"command": "pwd"},
        )
        self.assertEqual(safe["cwd"], "/workspace")
        self.assertNotIn("stdout", safe)

    def test_audit_and_evidence_persist_only_secret_digest(self):
        secret = "Authorization: Bearer persisted-secret-value"
        provider = SequenceProvider([
            {"type": "tool_call", "command": "pwd"},
            {"type": "final_answer", "final_answer": "done"},
        ])
        with tempfile.TemporaryDirectory() as directory:
            writer = AuditWriter("a" * 32, directory=directory)
            with patch("mini_harness_core.agent.execute_shell", return_value={
                "stdout": secret, "stderr": "", "exit_code": 0,
            }):
                run_agent("task", provider, audit_writer=writer)
            persisted = "".join(
                Path(root, name).read_text(encoding="utf-8")
                for root, _dirs, names in os.walk(directory)
                for name in names
            )
        self.assertNotIn(secret, persisted)
        self.assertIn(hashlib.sha256(secret.encode()).hexdigest(), persisted)


class AuthorizedDispatchTests(unittest.TestCase):
    def make_authorized(self, capability="shell", arguments=None, effect="read_only"):
        arguments = arguments or {"command": "pwd"}
        checkpoint = create_action_checkpoint(capability, arguments, effect)
        action = authorize_action(
            checkpoint=checkpoint, capability=capability, arguments=arguments,
            effect=effect, policy_decision="ALLOW", approval_granted=False,
            run_id="a" * 32,
        )
        return checkpoint, action

    def test_plain_dict_cannot_dispatch_shell_mcp_or_subagent(self):
        checkpoint, _ = self.make_authorized()
        for capability in ("shell", "mcp:demo:echo", "subagent"):
            with self.subTest(capability=capability), self.assertRaises(PermissionError):
                dispatch_authorized_action(
                    {"capability": capability}, checkpoint,
                    persist_checkpoint=lambda value: None,
                    executor=lambda arguments: {"exit_code": 0},
                )

    def test_forged_dataclass_and_approved_protected_path_are_rejected(self):
        checkpoint, action = self.make_authorized()
        forged = AuthorizedAction(
            action.action_id, action.capability,
            action.normalized_arguments_json, action.effect,
            action.policy_decision, action.approval_status,
            action.run_id, action.checkpoint_id, object(),
        )
        with self.assertRaises(PermissionError):
            dispatch_authorized_action(
                forged, checkpoint, persist_checkpoint=lambda value: None,
                executor=lambda arguments: {"exit_code": 0},
            )
        protected = create_action_checkpoint(
            "shell", {"command": "cat .env.local"}, "side_effecting"
        )
        with self.assertRaises(PermissionError):
            authorize_action(
                checkpoint=protected, capability="shell",
                arguments={"command": "cat .env.local"},
                effect="side_effecting", policy_decision="ASK",
                approval_granted=True, run_id="a" * 32,
            )

    def test_approved_authorized_action_persists_lifecycle_and_succeeds(self):
        checkpoint, action = self.make_authorized()
        states = []
        outcome = dispatch_authorized_action(
            action, checkpoint, persist_checkpoint=lambda value: states.append(value["state"]),
            executor=lambda arguments: {"stdout": "/workspace\n", "stderr": "", "exit_code": 0},
        )
        self.assertEqual(states, ["prepared", "executing", "succeeded"])
        self.assertEqual(outcome.checkpoint["state"], "succeeded")

    def test_executing_persist_failure_prevents_tool_start(self):
        checkpoint, action = self.make_authorized(effect="side_effecting")
        tool = Mock(return_value={"exit_code": 0})
        calls = []
        def persist(value):
            calls.append(value["state"])
            if value["state"] == "executing":
                raise OSError("ENOSPC")
        with self.assertRaises(OSError):
            dispatch_authorized_action(
                action, checkpoint, persist_checkpoint=persist, executor=tool,
            )
        tool.assert_not_called()
        self.assertEqual(calls, ["prepared", "executing"])

    def test_success_then_session_failure_becomes_unknown_degraded(self):
        checkpoint, action = self.make_authorized(effect="side_effecting")
        tool = Mock(return_value={"stdout": "", "stderr": "", "exit_code": 0})
        def persist(value):
            if value["state"] == "succeeded":
                raise OSError("ENOSPC")
        outcome = dispatch_authorized_action(
            action, checkpoint, persist_checkpoint=persist, executor=tool,
        )
        self.assertTrue(outcome.degraded)
        self.assertEqual(outcome.checkpoint["state"], "unknown")
        tool.assert_called_once()

    def test_success_then_audit_failure_keeps_terminal_success(self):
        checkpoint, action = self.make_authorized(effect="side_effecting")
        outcome = dispatch_authorized_action(
            action, checkpoint, persist_checkpoint=lambda value: None,
            executor=lambda arguments: {"stdout": "", "stderr": "", "exit_code": 0},
            after_dispatch=lambda *args: (_ for _ in ()).throw(OSError("audit")),
        )
        self.assertTrue(outcome.degraded)
        self.assertEqual(outcome.checkpoint["state"], "succeeded")

    def test_degraded_run_blocks_side_effect_before_approval_or_execution(self):
        provider = SequenceProvider([
            {"type": "tool_call", "command": "touch blocked.txt"},
            {"type": "final_answer", "final_answer": "done"},
        ])
        verification = {
            "requires_verification": False,
            "latest_write_command": None,
            "verification_target": None,
            "degraded": True,
            "degraded_reason": "audit unavailable",
        }
        with patch("mini_harness_core.agent.execute_shell") as tool, patch(
            "mini_harness_core.agent.request_approval"
        ) as approval:
            run_agent("task", provider, verification=verification)
        tool.assert_not_called()
        approval.assert_not_called()
        observation = json.loads(provider.calls[1][-1]["content"])
        self.assertEqual(observation["denied_by"], "persistence_gate")

    def test_degraded_run_allows_read_only_reconciliation(self):
        provider = SequenceProvider([
            {"type": "tool_call", "command": "pwd"},
            {"type": "final_answer", "final_answer": "done"},
        ])
        verification = {
            "requires_verification": False,
            "latest_write_command": None,
            "verification_target": None,
            "degraded": True,
            "degraded_reason": "session unavailable",
        }
        with patch("mini_harness_core.agent.execute_shell", return_value={
            "stdout": "/workspace\n", "stderr": "", "exit_code": 0,
        }) as tool:
            run_agent("task", provider, verification=verification)
        tool.assert_called_once()


class PostToolPersistenceFailureTests(unittest.TestCase):
    def test_agent_session_failure_after_success_keeps_unknown_and_no_replay(self):
        provider = SequenceProvider([{"type": "tool_call", "command": "touch x"}])
        verification = {
            "requires_verification": False, "latest_write_command": None,
            "verification_target": None,
        }
        states = []
        failed = [False]
        def persist(value):
            states.append(value["state"])
            if value["state"] == "succeeded" and not failed[0]:
                failed[0] = True
                raise OSError("session unavailable")
        with patch("mini_harness_core.agent.request_approval", return_value=True), patch(
            "mini_harness_core.agent.execute_shell",
            return_value={"stdout": "", "stderr": "", "exit_code": 0},
        ) as tool:
            run_agent(
                "task", provider, max_steps=1, verification=verification,
                save_action_checkpoint=persist, return_result=True,
            )
        tool.assert_called_once()
        self.assertIn("unknown", states)
        self.assertTrue(verification["degraded"])

    def test_agent_audit_failure_after_success_keeps_terminal_checkpoint(self):
        class FailingOutcomeAudit(AuditWriter):
            def append(self, event_type, actor, subject=None, outcome=None,
                       reason=None, references=None, summary=None):
                if (event_type == "action_state_changed"
                        and actor == "environment" and outcome == "succeeded"):
                    raise OSError("audit unavailable")
                return super().append(
                    event_type, actor, subject, outcome, reason, references, summary
                )
        verification = {
            "requires_verification": False, "latest_write_command": None,
            "verification_target": None,
        }
        checkpoints = []
        with tempfile.TemporaryDirectory() as directory:
            writer = FailingOutcomeAudit("b" * 32, directory=directory)
            with patch("mini_harness_core.agent.execute_shell", return_value={
                "stdout": "/workspace\n", "stderr": "", "exit_code": 0,
            }) as tool:
                run_agent(
                    "task", SequenceProvider([{"type": "tool_call", "command": "pwd"}]),
                    max_steps=1, verification=verification,
                    save_action_checkpoint=checkpoints.append,
                    audit_writer=writer, return_result=True,
                )
        tool.assert_called_once()
        self.assertEqual(checkpoints[-1]["state"], "succeeded")
        self.assertTrue(verification["degraded"])

    def test_evidence_failure_after_tool_marks_degraded_without_rewrite(self):
        class FailingEvidenceStore:
            def __init__(self, directory):
                self.directory = directory
            def save(self, record):
                raise OSError("evidence unavailable")
        verification = {
            "requires_verification": False, "latest_write_command": None,
            "verification_target": None,
        }
        checkpoints = []
        with tempfile.TemporaryDirectory() as directory:
            writer = AuditWriter("c" * 32, directory=directory)
            with patch("mini_harness_core.agent.execute_shell", return_value={
                "stdout": "/workspace\n", "stderr": "", "exit_code": 0,
            }) as tool:
                run_agent(
                    "task", SequenceProvider([{"type": "tool_call", "command": "pwd"}]),
                    max_steps=1, verification=verification,
                    save_action_checkpoint=checkpoints.append,
                    audit_writer=writer,
                    evidence_store=FailingEvidenceStore(os.path.join(directory, "evidence")),
                    return_result=True,
                )
        tool.assert_called_once()
        self.assertEqual(checkpoints[-1]["state"], "succeeded")
        self.assertTrue(verification["degraded"])

    def test_result_failure_after_tool_returns_incomplete_without_replay(self):
        class FailingResultStore:
            def save(self, result):
                raise OSError("result unavailable")
        verification = {
            "requires_verification": False, "latest_write_command": None,
            "verification_target": None,
        }
        provider = SequenceProvider([
            {"type": "tool_call", "command": "pwd"},
            {"type": "final_answer", "final_answer": "done"},
        ])
        with tempfile.TemporaryDirectory() as directory:
            writer = AuditWriter("d" * 32, directory=directory)
            with patch("mini_harness_core.agent.execute_shell", return_value={
                "stdout": "/workspace\n", "stderr": "", "exit_code": 0,
            }) as tool:
                result = run_agent(
                    "task", provider, verification=verification,
                    audit_writer=writer, result_store=FailingResultStore(),
                    return_result=True,
                )
        tool.assert_called_once()
        self.assertNotEqual(result["status"], "completed")
        self.assertTrue(verification["degraded"])

    def test_artifact_failure_after_tool_marks_degraded_without_replay(self):
        class FailingArtifactStore:
            def __init__(self, directory):
                self.directory = directory
            def list_run(self, run_id):
                return []
            def list_all(self):
                return []
            def save(self, record):
                raise OSError("artifact unavailable")
        verification = {
            "requires_verification": False, "latest_write_command": None,
            "verification_target": None,
        }
        provider = SequenceProvider([
            {"type": "tool_call", "command": "touch report.md"},
            {"type": "tool_call", "command": "cat report.md"},
            {"type": "final_answer", "final_answer": "done"},
        ])
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(workspace)
                writer = AuditWriter("e" * 32, directory=directory)
                artifact_store = FailingArtifactStore(
                    os.path.join(directory, "artifacts")
                )
                calls = []
                def execute(command, timeout=None):
                    calls.append(command)
                    if command.startswith("touch "):
                        with open("report.md", "w", encoding="utf-8") as stream:
                            stream.write("hello\n")
                        return {"stdout": "", "stderr": "", "exit_code": 0}
                    return {"stdout": "hello\n", "stderr": "", "exit_code": 0}
                with patch("mini_harness_core.agent.request_approval", return_value=True), patch(
                    "mini_harness_core.agent.execute_shell", side_effect=execute,
                ):
                    run_agent(
                        "task", provider, verification=verification,
                        audit_writer=writer, artifact_store=artifact_store,
                        output_contract={"required_artifacts": [{
                            "name": "report", "artifact_type": "workspace_file",
                            "path": "report.md",
                            "requirements": [
                                "exists", "non_empty", "content_identity", "verified",
                            ],
                        }]},
                        return_result=True,
                    )
            finally:
                os.chdir(original)
        self.assertEqual(calls, ["touch report.md", "cat report.md"])
        self.assertTrue(verification["degraded"])


if __name__ == "__main__":
    unittest.main()
