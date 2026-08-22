import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mini_harness_core.agent import run_agent
from mini_harness_core.audit import read_events
from mini_harness_core.integrations.bridge_adapter import (
    BOUND_NOT_STARTED,
    DONE,
    INTEGRATION_UNKNOWN,
    AdapterInjectedFault,
    BridgeBindingStore,
    DeterministicAdapterFaults,
    inspect_bridge_binding,
    run_bound_bridge_request,
)
from mini_harness_core.integrations.bridge_worker import (
    CLAIM_AND_RUN,
    IDLE,
    run_bridge_harness_worker_once,
)
from mini_harness_core.bridge.inspector import (
    CLAIMED_BY_SELF_UNKNOWN,
    COMPLETED,
    READY_TO_CLAIM,
    inspect_bridge_task,
)
from mini_harness_core.bridge.publisher import publish_bridge_task
from mini_harness_core.bridge.worker import run_bridge_worker_once

from tests.helpers.bridge import CONSUMER, ScriptedFakeProvider


class BridgeHarnessWorkerTests(unittest.TestCase):
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

    def publish(self, task_id, request="inspect current directory"):
        return publish_bridge_task(
            self.bridge, task_id, "bridge_harness_task",
            {"request": request}, "untrusted-publisher",
        )

    def worker_once(self, provider, fault_injector=None, max_steps=5):
        previous = os.getcwd()
        try:
            os.chdir(self.base)
            return run_bridge_harness_worker_once(
                self.bridge, CONSUMER, provider,
                audit_directory=self.audit,
                harness_runner=run_agent,
                session_directory=self.sessions,
                max_steps=max_steps,
                fault_injector=fault_injector,
            )
        finally:
            os.chdir(previous)

    def only_binding(self):
        paths = list((self.audit / "bridge_bindings").glob("*/*.json"))
        self.assertEqual(len(paths), 1)
        value = json.loads(paths[0].read_text(encoding="utf-8"))
        return BridgeBindingStore(self.audit).load(
            value["task_id"], value["claim_nonce"],
        )

    def test_e2e_01_read_only_request_commits_bridge_result(self):
        self.publish("task-read")
        provider = ScriptedFakeProvider([
            {"type": "tool_call", "command": "pwd"},
            {"type": "final_answer", "final_answer": "read-only done"},
        ])
        result = self.worker_once(provider, max_steps=2)
        self.assertEqual(result.action, CLAIM_AND_RUN)
        self.assertEqual(result.harness_result_status, "completed")
        self.assertEqual(result.final_state, COMPLETED)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(len(list((self.audit / "bridge_bindings").glob("*/*.json"))), 1)

    def test_e2e_02_ask_requires_current_harness_approval(self):
        self.publish("task-ask", "create a teaching marker")
        provider = ScriptedFakeProvider([
            {"type": "tool_call", "command": "touch adapter-ask.txt"},
            {"type": "tool_call", "command": "cat adapter-ask.txt"},
            {"type": "final_answer", "final_answer": "approved and verified"},
        ])
        with mock.patch(
            "mini_harness_core.agent.request_approval", return_value=True,
        ) as approval:
            result = self.worker_once(provider, max_steps=3)
        self.assertEqual(result.final_state, COMPLETED)
        approval.assert_called_once()
        binding = self.only_binding()
        events = read_events(binding["harness_run_id"], self.audit)
        policy = next(event for event in events
                      if event["event_type"] == "policy_decision")
        self.assertEqual(policy["outcome"], "ASK")

    def test_e2e_03_deny_remains_deny_but_terminal_result_is_committed(self):
        self.publish("task-deny", "request an unsafe deletion")
        provider = ScriptedFakeProvider([
            {"type": "tool_call", "command": "rm -rf forbidden"},
            {"type": "final_answer", "final_answer": "request denied safely"},
        ])
        with mock.patch("mini_harness_core.agent.execute_shell") as execute:
            result = self.worker_once(provider, max_steps=2)
        execute.assert_not_called()
        self.assertEqual(result.final_state, COMPLETED)
        binding = self.only_binding()
        events = read_events(binding["harness_run_id"], self.audit)
        policy = next(event for event in events
                      if event["event_type"] == "policy_decision")
        self.assertEqual(policy["outcome"], "DENY")
        projection = json.loads(
            (self.bridge / "outbox/result-task-deny.json").read_text(encoding="utf-8")
        )
        self.assertEqual(projection["status"], "completed")
        self.assertEqual(
            projection["result"]["harness_result_status"],
            result.harness_result_status,
        )

    def test_e2e_04_crash_after_binding_reuses_same_run(self):
        self.publish("task-bound-crash")
        fault = DeterministicAdapterFaults(["after_binding_before_run_create"])
        with self.assertRaises(AdapterInjectedFault):
            self.worker_once(ScriptedFakeProvider([]), fault)
        binding = self.only_binding()
        original_run_id = binding["harness_run_id"]
        self.assertEqual(inspect_bridge_binding(
            self.bridge, self.audit, binding["task_id"], binding["claim_nonce"],
        ), BOUND_NOT_STARTED)

        provider = ScriptedFakeProvider([
            {"type": "final_answer", "final_answer": "recovered"},
        ])
        recovered = run_bound_bridge_request(
            self.bridge, self.audit, self.sessions, binding, CONSUMER, provider,
            run_agent,
        )
        self.assertEqual(recovered.harness_run_id, original_run_id)
        self.assertEqual(recovered.bridge_state, COMPLETED)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(list((self.audit / "results").glob("*.json"))), 1)

    def test_crash_after_claim_is_not_automatically_resumed(self):
        self.publish("task-claim-crash")
        fault = DeterministicAdapterFaults(["after_claim_before_binding"])
        with self.assertRaises(AdapterInjectedFault):
            self.worker_once(ScriptedFakeProvider([]), fault)
        state = inspect_bridge_task(
            self.bridge, "task-claim-crash", consumer_id=CONSUMER,
        )
        self.assertEqual(state.state, CLAIMED_BY_SELF_UNKNOWN)
        self.assertFalse((self.audit / "bridge_bindings").exists())
        provider = ScriptedFakeProvider([])
        second = self.worker_once(provider)
        self.assertEqual(second.action, INTEGRATION_UNKNOWN)
        self.assertEqual(provider.calls, [])

    def test_crash_after_terminal_retries_projection_only(self):
        self.publish("task-projection-crash")
        fault = DeterministicAdapterFaults([
            "after_harness_terminal_before_bridge_result",
        ])
        provider = ScriptedFakeProvider([
            {"type": "final_answer", "final_answer": "terminal"},
        ])
        with self.assertRaises(AdapterInjectedFault):
            self.worker_once(provider, fault)
        binding = self.only_binding()
        self.assertFalse(
            (self.bridge / "outbox/result-task-projection-crash.ready").exists()
        )
        retry_provider = ScriptedFakeProvider([])
        recovered = run_bound_bridge_request(
            self.bridge, self.audit, self.sessions, binding, CONSUMER,
            retry_provider, run_agent,
        )
        self.assertEqual(recovered.bridge_state, COMPLETED)
        self.assertEqual(retry_provider.calls, [])

    def test_metadata_never_uplifts_policy_or_effect(self):
        self.publish("task-authority", "delete forbidden data")
        provider = ScriptedFakeProvider([
            {"type": "tool_call", "command": "rm -rf forbidden"},
            {"type": "final_answer", "final_answer": "denied"},
        ])
        with mock.patch("mini_harness_core.agent.execute_shell") as execute:
            self.worker_once(provider, max_steps=2)
        execute.assert_not_called()
        binding = self.only_binding()
        event = next(
            item for item in read_events(binding["harness_run_id"], self.audit)
            if item["event_type"] == "policy_decision"
        )
        self.assertEqual(event["outcome"], "DENY")
        references = event["references"]
        self.assertNotIn("publisher_id", references)
        self.assertNotIn("claim_nonce", references)

    def test_old_bridge_test_worker_semantics_are_unchanged(self):
        publish_bridge_task(
            self.bridge, "task-legacy-worker", "bridge_test",
            {"message": "hello"}, "publisher",
        )
        new_worker = self.worker_once(ScriptedFakeProvider([]))
        self.assertEqual(new_worker.action, IDLE)
        self.assertEqual(
            inspect_bridge_task(self.bridge, "task-legacy-worker").state,
            READY_TO_CLAIM,
        )
        old_worker = run_bridge_worker_once(self.bridge, CONSUMER)
        self.assertEqual(old_worker.final_state, COMPLETED)


if __name__ == "__main__":
    unittest.main()
