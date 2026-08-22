import ast
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests._paths import REPO_ROOT

from mini_harness import (
    AuditWriter, DeterministicFaultInjector, FAULT_POINTS, InjectedFault,
    LateMCPCompletionJournal, MCPClient, MCPRegistry,
    authorize_action, create_action_checkpoint, dispatch_authorized_action,
    run_agent,
)
from mini_harness_core.durability import recover_action_checkpoint
from mini_harness_core.mcp import execute_mcp_tool
from mini_harness_core.run_control import (
    create_run_control, request_cancel, request_pause, settle_control_boundary,
)
from mini_harness_core.run_bundle import export_run_bundle


class FailureSemanticsV26Tests(unittest.TestCase):
    def authorized(self, effect="side_effecting"):
        checkpoint = create_action_checkpoint(
            "shell", {"command": "touch result.txt"}, effect,
        )
        action = authorize_action(
            checkpoint=checkpoint, capability="shell",
            arguments=checkpoint["arguments"], effect=effect,
            policy_decision="ALLOW", approval_granted=False,
            run_id="a" * 32,
        )
        return checkpoint, action

    def test_dispatch_crash_points_preserve_forward_truth(self):
        for point, expected_state, audit_calls in (
            ("after_tool_success_before_terminal_checkpoint", "executing", 0),
            ("after_terminal_checkpoint_before_audit", "succeeded", 0),
            ("after_audit_before_session", "succeeded", 1),
        ):
            with self.subTest(point=point):
                checkpoint, action = self.authorized()
                states, audits = [], []
                tool = Mock(return_value={"exit_code": 0, "stdout": "", "stderr": ""})
                with self.assertRaises(InjectedFault):
                    dispatch_authorized_action(
                        action, checkpoint,
                        persist_checkpoint=lambda value: states.append(value),
                        executor=tool,
                        after_dispatch=lambda *_args: audits.append(True),
                        fault_injector=DeterministicFaultInjector([point]),
                    )
                tool.assert_called_once()
                self.assertEqual(states[-1]["state"], expected_state)
                self.assertEqual(len(audits), audit_calls)
                recovered, decision = recover_action_checkpoint(states[-1])
                if expected_state == "executing":
                    self.assertEqual(recovered["state"], "unknown")
                    self.assertEqual(decision, "reconcile_or_block")
                else:
                    self.assertEqual(decision, "continue_with_observation")

    def test_all_six_points_are_deterministic_one_shot(self):
        for point in sorted(FAULT_POINTS):
            injector = DeterministicFaultInjector([point])
            with self.assertRaises(InjectedFault):
                injector.trigger(point)
            injector.trigger(point)
            self.assertEqual(injector.hits, [point])

    def test_audit_then_session_failure_keeps_succeeded_checkpoint(self):
        checkpoint, action = self.authorized()
        audit = Mock()
        outcome = dispatch_authorized_action(
            action, checkpoint, persist_checkpoint=lambda _value: None,
            executor=lambda _args: {"exit_code": 0},
            after_dispatch=audit,
            persist_session=lambda _value: (_ for _ in ()).throw(OSError("ENOSPC")),
        )
        audit.assert_called_once()
        self.assertEqual(outcome.checkpoint["state"], "succeeded")
        self.assertTrue(outcome.degraded)
        self.assertEqual(outcome.degraded_stage, "session")

    def test_pause_and_cancel_settle_only_after_authorized_action(self):
        for requester, expected in ((request_pause, "paused"),
                                    (request_cancel, "cancelled")):
            with self.subTest(expected=expected):
                checkpoint, action = self.authorized("read_only")
                control = create_run_control()

                def execute(_args):
                    updated = requester(control)
                    control.clear(); control.update(updated)
                    return {"exit_code": 0}

                outcome = dispatch_authorized_action(
                    action, checkpoint, persist_checkpoint=lambda _value: None,
                    executor=execute,
                )
                self.assertEqual(outcome.checkpoint["state"], "succeeded")
                settled = settle_control_boundary(control)
                self.assertEqual(settled["state"], expected)


class SlowMCP(MCPClient):
    def __init__(self):
        self.release = threading.Event()
        self.calls = 0

    def list_tools(self):
        return [{"name": "write", "description": "fake", "inputSchema": {
            "type": "object", "additionalProperties": False,
        }}]

    def call_tool(self, name, arguments):
        self.calls += 1
        self.release.wait(1)
        return {"status": "late-success", "Authorization": "Bearer secret-marker"}


class LateMCPV26Tests(unittest.TestCase):
    def test_timeout_late_completion_is_historical_only_and_never_recalled(self):
        client = SlowMCP()
        reference = "mcp:slow:write"
        registry = MCPRegistry(
            {"slow": client}, {reference: "ALLOW"},
            {reference: "side_effecting"},
        )
        journal = LateMCPCompletionJournal()
        observation = execute_mcp_tool(
            registry, reference, {}, timeout=0.01,
            late_completion_journal=journal,
            action_id="action-1", call_id="call-1", run_state="cancelled",
        )
        self.assertEqual(observation["exit_code"], -1)
        client.release.set()
        deadline = time.monotonic() + 1
        while not journal.list() and time.monotonic() < deadline:
            time.sleep(0.001)
        records = journal.list()
        self.assertEqual(client.calls, 1)
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["historical_only"])
        self.assertFalse(records[0]["reconciliation_candidate"])
        self.assertNotIn("secret-marker", json.dumps(records))


class DependencyBoundaryV26Tests(unittest.TestCase):
    def test_result_and_envelope_have_no_direct_or_lazy_import_cycle(self):
        root = REPO_ROOT / "mini_harness_core"
        imports = {}
        for name in ("result", "run_envelope"):
            tree = ast.parse((root / f"{name}.py").read_text(encoding="utf-8"))
            imports[name] = {
                node.module.split(".")[-1]
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            }
        self.assertNotIn("run_envelope", imports["result"])
        self.assertNotIn("result", imports["run_envelope"])


class SequenceProvider:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.calls = []

    def complete(self, messages):
        self.calls.append(json.loads(json.dumps(messages)))
        return self.decisions.pop(0)


class SecretResultMCP(MCPClient):
    def list_tools(self):
        return [{"name": "read", "description": "fake", "inputSchema": {
            "type": "object", "additionalProperties": False,
        }}]

    def call_tool(self, name, arguments):
        return {"Authorization": "Bearer secret-marker", "status": "ok"}


class CrossStoreProjectionV26Tests(unittest.TestCase):
    def test_raw_shell_and_mcp_secrets_never_cross_historical_boundaries(self):
        reference = "mcp:secret:read"
        registry = MCPRegistry(
            {"secret": SecretResultMCP()}, {reference: "ALLOW"},
            {reference: "read_only"},
        )
        provider = SequenceProvider([
            {"type": "tool_call", "command": "pwd"},
            {"type": "tool_call", "tool": reference, "arguments": {}},
            {"type": "final_answer", "answer": "safe done"},
        ])
        messages = []
        with tempfile.TemporaryDirectory() as root:
            audit = os.path.join(root, "audit")
            writer = AuditWriter("b" * 32, "c" * 32, audit)
            with patch("mini_harness_core.agent.execute_shell", return_value={
                "stdout": "OPENAI_API_KEY=secret-marker",
                "stderr": "", "exit_code": 0,
            }):
                run_agent(
                    "projection", provider, messages=messages,
                    mcp_registry=registry, audit_writer=writer,
                    return_result=True,
                )
            bundle_root = os.path.join(root, "bundles")
            bundle, _manifest, _reused = export_run_bundle(
                writer.run_id, audit, bundle_root,
            )
            persisted = json.dumps({
                "session": messages, "context": provider.calls,
            }, ensure_ascii=False).encode()
            for base in (audit, bundle):
                for path in Path(base).rglob("*"):
                    if path.is_file():
                        persisted += path.read_bytes()
            self.assertNotIn(b"secret-marker", persisted)
            self.assertNotIn(b"OPENAI_API_KEY", persisted)
            self.assertNotIn(b"Authorization", persisted)


if __name__ == "__main__":
    unittest.main()
