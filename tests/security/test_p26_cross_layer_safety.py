"""P2.6 cross-layer ordering, concurrency, and fail-closed recovery tests."""

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from mini_harness_core.agent import run_agent
from mini_harness_core.integrations.bridge_adapter import (
    BINDING_LOCKED,
    EVIDENCE_REPAIR_REQUIRED,
    EVIDENCE_REPAIRED,
    HARNESS_RESULT_REPAIR_REQUIRED,
    OBSERVATION_RECOVERY_REQUIRED,
    PROJECTION_REPAIRED,
    AdapterInjectedFault,
    BridgeBindingStore,
    DeterministicAdapterFaults,
    bind_bridge_attempt,
    recover_environment_evidence,
    repair_bridge_harness_projection,
    run_bound_bridge_request,
)
from mini_harness_core.bridge.attempt_fence import acquire_bridge_attempt_fence
from mini_harness_core.bridge.claimer import claim_bridge_task
from mini_harness_core.bridge.executor import ATTEMPT_LOCKED as EXECUTOR_LOCKED
from mini_harness_core.bridge.executor import execute_bridge_task
from mini_harness_core.integrations.bridge_worker import run_bridge_harness_worker_once
from mini_harness_core.bridge.inspector import COMPLETED, inspect_bridge_task
from mini_harness_core.bridge.publisher import publish_bridge_task
from mini_harness_core.bridge.reconciler import (
    ATTEMPT_LOCKED as RECONCILER_LOCKED,
    RECONCILIATION_NOT_ALLOWED,
    reconcile_bridge_claim,
)
from mini_harness_core.bridge.result_repairer import (
    INTEGRATION_REPAIR_REQUIRED,
    repair_bridge_result,
)
from mini_harness_core.dispatch import environment_checkpoint_outcome
from mini_harness_core.environment.contracts import EnvironmentAdapterResult
from mini_harness_core.environment.registry import EnvironmentCapabilityRegistry
from mini_harness_core.evidence import EvidenceStore
from mini_harness_core.fault_injection import (
    DeterministicFaultInjector, InjectedFault,
)
from mini_harness_core.environment.termux import (
    LOGICAL_CAPABILITY, NOTIFICATION_LOGICAL_CAPABILITY,
)
from mini_harness_core.result import ResultStore, result_integrity_check
from tests.helpers.bridge import CONSUMER, ScriptedFakeProvider


SHA = hashlib.sha256(b"").hexdigest()


def battery_result(status="succeeded", certainty="no_side_effect", code=0):
    return EnvironmentAdapterResult(
        LOGICAL_CAPABILITY, status, "read_only", certainty,
        {"percentage": 73} if status == "succeeded" else {},
        code, 0, SHA, 0, SHA, None if status == "succeeded" else "TIMEOUT",
    )


def notification_result(status="succeeded", certainty="known_applied", code=0):
    return EnvironmentAdapterResult(
        NOTIFICATION_LOGICAL_CAPABILITY, status, "side_effecting", certainty,
        {"notification_requested": True, "request_accepted": True}
        if status == "succeeded" else {},
        code, 0, SHA, 0, SHA, None if status == "succeeded" else "TIMEOUT",
    )


class P26CrossLayerSafetyTests(unittest.TestCase):
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

    def prepare(self, task="task-p26", task_type="bridge_harness_task"):
        payload = (
            {"request": "inspect mobile environment"}
            if task_type == "bridge_harness_task" else {"message": "hello"}
        )
        publish_bridge_task(self.bridge, task, task_type, payload, "publisher")
        claim = "claim-p26-a"
        claim_bridge_task(self.bridge, task, CONSUMER, claim)
        if task_type != "bridge_harness_task":
            return None, claim
        from mini_harness_core.integrations.bridge_adapter import read_bridge_harness_task
        adapted = read_bridge_harness_task(self.bridge, task)
        binding, _status = bind_bridge_attempt(
            self.bridge, self.audit, task, claim, CONSUMER,
            expected_source_fingerprint=adapted.source_fingerprint,
        )
        return binding, claim

    def test_bridge_environment_evidence_is_durable_before_projection(self):
        for capability, arguments, adapter_target, adapter_result in (
            (LOGICAL_CAPABILITY, {},
             "mini_harness_core.environment.registry.invoke_termux_capability",
             battery_result()),
            (NOTIFICATION_LOGICAL_CAPABILITY,
             {"title": "P2.6", "content": "durability"},
             "mini_harness_core.environment.registry.invoke_termux_notification",
             notification_result()),
        ):
            with self.subTest(capability=capability), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                self.bridge = root / "bridge"
                self.audit = root / "audit"
                self.sessions = root / "sessions"
                self.bridge.mkdir()
                for name in ("inbox", "claims", "reconciliations", "outbox", "locks"):
                    (self.bridge / name).mkdir()
                binding, _claim = self.prepare()
                provider = ScriptedFakeProvider([
                    {"type": "tool_call", "tool": capability,
                     "arguments": arguments},
                    {"type": "final_answer", "final_answer": "done"},
                ])
                approval = mock.patch(
                    "mini_harness_core.agent.request_approval", return_value=True,
                )
                with mock.patch(adapter_target, return_value=adapter_result), approval:
                    outcome = run_bound_bridge_request(
                        self.bridge, self.audit, self.sessions, binding,
                        CONSUMER, provider, run_agent,
                    )
                evidence = list((self.audit / "evidence").glob("*.json"))
                self.assertEqual(len(evidence), 1)
                terminal = json.loads((
                    self.audit / "results" / f"{binding['harness_run_id']}.json"
                ).read_text())
                self.assertIn(
                    json.loads(evidence[0].read_text())["evidence_id"],
                    terminal["evidence_ids"],
                )
                self.assertEqual(outcome.bridge_state, COMPLETED)

    def test_environment_evidence_failure_degrades_without_reexecution(self):
        binding, _claim = self.prepare()
        calls = []
        provider = ScriptedFakeProvider([
            {"type": "tool_call", "tool": LOGICAL_CAPABILITY, "arguments": {}},
            {"type": "final_answer", "final_answer": "battery done"},
        ])

        def invoke(_name):
            calls.append("battery")
            return battery_result()

        with mock.patch(
            "mini_harness_core.environment.registry.invoke_termux_capability",
            side_effect=invoke,
        ), mock.patch.object(EvidenceStore, "save", side_effect=OSError("disk")):
            first = run_bound_bridge_request(
                self.bridge, self.audit, self.sessions, binding, CONSUMER,
                provider, run_agent,
            )
        self.assertEqual(first.status, EVIDENCE_REPAIR_REQUIRED)
        self.assertFalse((
            self.bridge / "outbox/result-task-p26.json"
        ).exists())
        repaired = recover_environment_evidence(
            self.bridge, self.audit, self.sessions, "task-p26",
            binding["claim_nonce"], CONSUMER,
        )
        self.assertEqual(repaired.status, EVIDENCE_REPAIRED)
        self.assertEqual(repaired.harness_result_status, "incomplete")
        second = run_bound_bridge_request(
            self.bridge, self.audit, self.sessions, binding, CONSUMER,
            ScriptedFakeProvider([]), run_agent,
        )
        self.assertEqual(second.harness_run_id, binding["harness_run_id"])
        self.assertEqual(calls, ["battery"])

    def test_battery_and_notification_evidence_only_recovery_call_once(self):
        cases = (
            ("task-evidence-battery", LOGICAL_CAPABILITY, {},
             "mini_harness_core.environment.registry.invoke_termux_capability",
             battery_result()),
            ("task-evidence-notification", NOTIFICATION_LOGICAL_CAPABILITY,
             {"title": "P2.6.1", "content": "evidence recovery"},
             "mini_harness_core.environment.registry.invoke_termux_notification",
             notification_result()),
        )
        for task_id, capability, arguments, target, result in cases:
            with self.subTest(capability=capability), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                self.bridge = root / "bridge"
                self.audit = root / "audit"
                self.sessions = root / "sessions"
                self.bridge.mkdir()
                for name in ("inbox", "claims", "reconciliations", "outbox", "locks"):
                    (self.bridge / name).mkdir()
                binding, claim = self.prepare(task=task_id)
                calls = []

                def invoke(*_args, **_kwargs):
                    calls.append(capability)
                    return result

                provider = ScriptedFakeProvider([
                    {"type": "tool_call", "tool": capability,
                     "arguments": arguments},
                    {"type": "final_answer", "final_answer": "done"},
                ])
                with mock.patch(target, side_effect=invoke), mock.patch(
                    "mini_harness_core.agent.request_approval", return_value=True,
                ), mock.patch.object(
                    EvidenceStore, "save", side_effect=OSError("disk"),
                ):
                    first = run_bound_bridge_request(
                        self.bridge, self.audit, self.sessions, binding,
                        CONSUMER, provider, run_agent,
                    )
                self.assertEqual(first.status, EVIDENCE_REPAIR_REQUIRED)
                self.assertFalse((self.bridge / f"outbox/result-{task_id}.json").exists())
                repaired = recover_environment_evidence(
                    self.bridge, self.audit, self.sessions, task_id, claim,
                    CONSUMER,
                )
                self.assertEqual(repaired.status, EVIDENCE_REPAIRED)
                self.assertEqual(calls, [capability])
                terminal = json.loads((
                    self.audit / "results" / f"{binding['harness_run_id']}.json"
                ).read_text())
                self.assertEqual(len(terminal["evidence_ids"]), 1)
                self.assertTrue(result_integrity_check(
                    binding["harness_run_id"],
                    result_directory=self.audit / "results",
                    evidence_directory=self.audit / "evidence",
                    audit_directory=self.audit,
                ))
                self.assertEqual(
                    json.loads((self.audit / "evidence" /
                                f"{terminal['evidence_ids'][0]}.json").read_text())
                    ["source"]["action_id"],
                    json.loads((self.sessions /
                                f"{binding['harness_session_id']}.json").read_text())
                    ["current_action_checkpoint"]["action_id"],
                )

    def test_evidence_repair_then_result_failure_resumes_result_only(self):
        binding, claim = self.prepare()
        calls = []

        def invoke(_name):
            calls.append("battery")
            return battery_result()

        with mock.patch(
            "mini_harness_core.environment.registry.invoke_termux_capability",
            side_effect=invoke,
        ), mock.patch.object(
            EvidenceStore, "save", side_effect=OSError("evidence disk"),
        ):
            first = run_bound_bridge_request(
                self.bridge, self.audit, self.sessions, binding, CONSUMER,
                ScriptedFakeProvider([
                    {"type": "tool_call", "tool": LOGICAL_CAPABILITY,
                     "arguments": {}},
                    {"type": "final_answer", "final_answer": "done"},
                ]), run_agent,
            )
        self.assertEqual(first.status, EVIDENCE_REPAIR_REQUIRED)
        with mock.patch.object(
            ResultStore, "save", side_effect=OSError("result disk"),
        ):
            result_failed = recover_environment_evidence(
                self.bridge, self.audit, self.sessions, "task-p26", claim,
                CONSUMER,
            )
        self.assertEqual(result_failed.status, HARNESS_RESULT_REPAIR_REQUIRED)
        self.assertEqual(len(list((self.audit / "evidence").glob("*.json"))), 1)
        repaired = recover_environment_evidence(
            self.bridge, self.audit, self.sessions, "task-p26", claim,
            CONSUMER,
        )
        self.assertEqual(repaired.status, PROJECTION_REPAIRED)
        self.assertEqual(calls, ["battery"])

    def test_evidence_recovery_fails_closed_without_durable_observation(self):
        binding, claim = self.prepare()
        with mock.patch(
            "mini_harness_core.environment.registry.invoke_termux_capability",
            return_value=battery_result(),
        ), mock.patch.object(
            EvidenceStore, "save", side_effect=OSError("disk"),
        ):
            run_bound_bridge_request(
                self.bridge, self.audit, self.sessions, binding, CONSUMER,
                ScriptedFakeProvider([
                    {"type": "tool_call", "tool": LOGICAL_CAPABILITY,
                     "arguments": {}},
                    {"type": "final_answer", "final_answer": "done"},
                ]), run_agent,
            )
        audit_path = self.audit / f"{binding['harness_run_id']}.jsonl"
        events = [json.loads(line) for line in audit_path.read_text().splitlines()]
        for event in events:
            if event["event_type"] == "action_state_changed" and (
                event["actor"] == "environment"
            ):
                event["summary"] = None
        audit_path.write_text("".join(
            json.dumps(event, separators=(",", ":")) + "\n" for event in events
        ))
        recovered = recover_environment_evidence(
            self.bridge, self.audit, self.sessions, "task-p26", claim,
            CONSUMER,
        )
        self.assertEqual(recovered.status, OBSERVATION_RECOVERY_REQUIRED)
        self.assertFalse((self.bridge / "outbox/result-task-p26.ready").exists())

    def test_generic_repair_rejects_harness_integration_task(self):
        _binding, claim = self.prepare()
        reconcile_bridge_claim(
            self.bridge, "task-p26", claim, "applied", "operator",
            "manual_inspection",
        )
        outcome = repair_bridge_result(
            self.bridge, "task-p26", claim, CONSUMER,
            {"forged": "caller supplied"}, [],
        )
        self.assertEqual(outcome.status, INTEGRATION_REPAIR_REQUIRED)
        self.assertFalse((self.bridge / "outbox/result-task-p26.ready").exists())

    def test_projection_repair_uses_durable_result_and_zero_environment_calls(self):
        binding, claim = self.prepare()
        provider = ScriptedFakeProvider([
            {"type": "final_answer", "final_answer": "terminal"},
        ])
        with mock.patch(
            "mini_harness_core.integrations.bridge_adapter.project_harness_result_to_bridge",
            side_effect=SystemExit("projection crash"),
        ), self.assertRaises(SystemExit):
            run_bound_bridge_request(
                self.bridge, self.audit, self.sessions, binding, CONSUMER,
                provider, run_agent,
            )
        with mock.patch.object(
            EnvironmentCapabilityRegistry, "invoke",
        ) as environment:
            repaired = repair_bridge_harness_projection(
                self.bridge, self.audit, "task-p26", claim, CONSUMER,
            )
        self.assertEqual(repaired.status, PROJECTION_REPAIRED)
        self.assertEqual(repaired.bridge_state, COMPLETED)
        environment.assert_not_called()

    def test_same_binding_concurrent_entry_starts_one_harness_run(self):
        binding, _claim = self.prepare()
        entered = threading.Event()
        release = threading.Event()
        calls = []

        class Provider:
            def complete(self, _messages):
                calls.append("run")
                entered.set()
                release.wait(5)
                return {"type": "final_answer", "final_answer": "done"}

        first_result = []

        def first():
            first_result.append(run_bound_bridge_request(
                self.bridge, self.audit, self.sessions, binding, CONSUMER,
                Provider(), run_agent,
            ))

        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(entered.wait(5))
        second = run_bound_bridge_request(
            self.bridge, self.audit, self.sessions, binding, CONSUMER,
            ScriptedFakeProvider([]), run_agent,
        )
        self.assertEqual(second.status, BINDING_LOCKED)
        release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, ["run"])
        self.assertEqual(first_result[0].bridge_state, COMPLETED)

    def test_attempt_fence_blocks_executor_and_reconciler_and_is_not_stolen(self):
        _binding, claim = self.prepare(task_type="bridge_test")
        fence, _status = acquire_bridge_attempt_fence(
            self.bridge, "task-p26", claim,
        )
        self.assertEqual(
            execute_bridge_task(self.bridge, "task-p26", CONSUMER, claim).status,
            EXECUTOR_LOCKED,
        )
        self.assertEqual(reconcile_bridge_claim(
            self.bridge, "task-p26", claim, "uncertain", "operator",
            "manual_inspection",
        ).status, RECONCILER_LOCKED)
        self.assertTrue(Path(fence.path).is_dir())
        fence.release()
        self.assertFalse(Path(fence.path).exists())

    def test_certainty_to_checkpoint_mapping_is_single_and_fail_closed(self):
        self.assertEqual(environment_checkpoint_outcome(
            "read_only", battery_result(),
        ), "succeeded")
        self.assertEqual(environment_checkpoint_outcome(
            "read_only", battery_result("failed", "no_side_effect", None),
        ), "failed")
        self.assertEqual(environment_checkpoint_outcome(
            "side_effecting", notification_result(),
        ), "succeeded")
        self.assertEqual(environment_checkpoint_outcome(
            "side_effecting",
            notification_result("failed", "unknown", 9),
        ), "unknown")

    def test_run_fence_crash_residue_fails_closed_without_stale_takeover(self):
        binding, _claim = self.prepare()
        fault = DeterministicAdapterFaults([
            "after_run_fence_before_run_start",
        ])
        with self.assertRaises(AdapterInjectedFault):
            run_bound_bridge_request(
                self.bridge, self.audit, self.sessions, binding, CONSUMER,
                ScriptedFakeProvider([]), run_agent, fault_injector=fault,
            )
        second = run_bound_bridge_request(
            self.bridge, self.audit, self.sessions, binding, CONSUMER,
            ScriptedFakeProvider([]), run_agent,
        )
        self.assertEqual(second.status, BINDING_LOCKED)
        self.assertEqual(list((self.audit / "results").glob("*.json")), [])

    def test_environment_crash_before_evidence_never_reinvokes_capability(self):
        binding, _claim = self.prepare()
        calls = []

        def invoke(_name):
            calls.append("battery")
            return battery_result()

        provider = ScriptedFakeProvider([
            {"type": "tool_call", "tool": LOGICAL_CAPABILITY, "arguments": {}},
        ])
        fault = DeterministicFaultInjector([
            "after_environment_success_before_evidence",
        ])
        with mock.patch(
            "mini_harness_core.environment.registry.invoke_termux_capability",
            side_effect=invoke,
        ), self.assertRaises(InjectedFault):
            run_bound_bridge_request(
                self.bridge, self.audit, self.sessions, binding, CONSUMER,
                provider, run_agent, harness_fault_injector=fault,
            )
        recovered = run_bound_bridge_request(
            self.bridge, self.audit, self.sessions, binding, CONSUMER,
            ScriptedFakeProvider([]), run_agent,
        )
        self.assertEqual(recovered.status, EVIDENCE_REPAIR_REQUIRED)
        repaired = recover_environment_evidence(
            self.bridge, self.audit, self.sessions, "task-p26",
            binding["claim_nonce"], CONSUMER,
        )
        self.assertEqual(repaired.status, EVIDENCE_REPAIRED)
        self.assertEqual(calls, ["battery"])
        self.assertTrue((self.bridge / "outbox/result-task-p26.ready").exists())

    def test_evidence_durable_then_result_failure_does_not_project_or_reexecute(self):
        binding, _claim = self.prepare()
        calls = []

        def invoke(_name):
            calls.append("battery")
            return battery_result()

        provider = ScriptedFakeProvider([
            {"type": "tool_call", "tool": LOGICAL_CAPABILITY, "arguments": {}},
            {"type": "final_answer", "final_answer": "done"},
        ])
        with mock.patch(
            "mini_harness_core.environment.registry.invoke_termux_capability",
            side_effect=invoke,
        ), mock.patch.object(ResultStore, "save", side_effect=OSError("disk")):
            outcome = run_bound_bridge_request(
                self.bridge, self.audit, self.sessions, binding, CONSUMER,
                provider, run_agent,
            )
        self.assertEqual(outcome.status, "HARNESS_RECOVERY_REQUIRED")
        self.assertEqual(len(list((self.audit / "evidence").glob("*.json"))), 1)
        self.assertFalse((self.bridge / "outbox/result-task-p26.json").exists())
        retry = run_bound_bridge_request(
            self.bridge, self.audit, self.sessions, binding, CONSUMER,
            ScriptedFakeProvider([]), run_agent,
        )
        self.assertEqual(retry.status, "HARNESS_RECOVERY_REQUIRED")
        self.assertEqual(calls, ["battery"])

    def test_after_evidence_fault_leaves_evidence_but_no_result_or_projection(self):
        binding, _claim = self.prepare()
        calls = []

        def invoke(_name):
            calls.append("battery")
            return battery_result()

        fault = DeterministicFaultInjector([
            "after_evidence_before_harness_result",
        ])
        with mock.patch(
            "mini_harness_core.environment.registry.invoke_termux_capability",
            side_effect=invoke,
        ), self.assertRaises(InjectedFault):
            run_bound_bridge_request(
                self.bridge, self.audit, self.sessions, binding, CONSUMER,
                ScriptedFakeProvider([
                    {"type": "tool_call", "tool": LOGICAL_CAPABILITY,
                     "arguments": {}},
                ]), run_agent, harness_fault_injector=fault,
            )
        self.assertEqual(len(list((self.audit / "evidence").glob("*.json"))), 1)
        self.assertFalse((self.audit / "results").exists())
        self.assertFalse((self.bridge / "outbox/result-task-p26.json").exists())
        self.assertEqual(calls, ["battery"])

    def test_projection_json_crash_repairs_ready_without_harness_run(self):
        binding, claim = self.prepare()
        fault = DeterministicAdapterFaults([
            "after_bridge_result_json_before_ready",
        ])
        with self.assertRaises(AdapterInjectedFault):
            run_bound_bridge_request(
                self.bridge, self.audit, self.sessions, binding, CONSUMER,
                ScriptedFakeProvider([
                    {"type": "final_answer", "final_answer": "done"},
                ]), run_agent, fault_injector=fault,
            )
        self.assertTrue((self.bridge / "outbox/result-task-p26.json").is_file())
        self.assertFalse((self.bridge / "outbox/result-task-p26.ready").exists())
        with mock.patch.object(
            EnvironmentCapabilityRegistry, "invoke",
        ) as environment:
            repaired = repair_bridge_harness_projection(
                self.bridge, self.audit, "task-p26", claim, CONSUMER,
            )
        self.assertEqual(repaired.bridge_state, COMPLETED)
        environment.assert_not_called()

    def test_terminal_truth_excludes_reconciler_until_projection_repair(self):
        binding, claim = self.prepare()
        fault = DeterministicAdapterFaults([
            "after_harness_result_before_bridge_projection",
        ])
        with self.assertRaises(AdapterInjectedFault):
            run_bound_bridge_request(
                self.bridge, self.audit, self.sessions, binding, CONSUMER,
                ScriptedFakeProvider([
                    {"type": "final_answer", "final_answer": "terminal"},
                ]), run_agent, fault_injector=fault,
            )
        denied = reconcile_bridge_claim(
            self.bridge, "task-p26", claim, "not_applied", "operator",
            "manual_inspection",
        )
        self.assertEqual(denied.status, RECONCILIATION_NOT_ALLOWED)
        self.assertFalse((
            self.bridge / f"reconciliations/task-p26/{claim}.json"
        ).exists())
        with mock.patch.object(EnvironmentCapabilityRegistry, "invoke") as adapter:
            repaired = repair_bridge_harness_projection(
                self.bridge, self.audit, "task-p26", claim, CONSUMER,
            )
        self.assertEqual(repaired.bridge_state, COMPLETED)
        adapter.assert_not_called()

    def test_projection_owner_holds_fence_against_second_worker(self):
        binding, claim = self.prepare()
        terminal_fault = DeterministicAdapterFaults([
            "after_harness_result_before_bridge_projection",
        ])
        with self.assertRaises(AdapterInjectedFault):
            run_bound_bridge_request(
                self.bridge, self.audit, self.sessions, binding, CONSUMER,
                ScriptedFakeProvider([
                    {"type": "final_answer", "final_answer": "terminal"},
                ]), run_agent, fault_injector=terminal_fault,
            )
        entered = threading.Event()
        release = threading.Event()
        original_publish = __import__(
            "mini_harness_core.integrations.bridge_adapter", fromlist=["_publish_file"],
        )._publish_file
        publications = []

        def paused_publish(directory, final_path, content, nonce):
            publications.append(str(final_path))
            if str(final_path).endswith("result-task-p26.json"):
                entered.set()
                release.wait(5)
            return original_publish(directory, final_path, content, nonce)

        first = []

        def project_first():
            first.append(repair_bridge_harness_projection(
                self.bridge, self.audit, "task-p26", claim, CONSUMER,
            ))

        with mock.patch(
            "mini_harness_core.integrations.bridge_adapter._publish_file",
            side_effect=paused_publish,
        ), mock.patch.object(EnvironmentCapabilityRegistry, "invoke") as adapter:
            thread = threading.Thread(target=project_first)
            thread.start()
            self.assertTrue(entered.wait(5))
            second = run_bound_bridge_request(
                self.bridge, self.audit, self.sessions, binding, CONSUMER,
                ScriptedFakeProvider([]), run_agent,
            )
            competing_projection = repair_bridge_harness_projection(
                self.bridge, self.audit, "task-p26", claim, CONSUMER,
            )
            reconciler = reconcile_bridge_claim(
                self.bridge, "task-p26", claim, "not_applied", "operator",
                "manual_inspection",
            )
            self.assertEqual(second.status, BINDING_LOCKED)
            self.assertEqual(competing_projection.status, BINDING_LOCKED)
            self.assertEqual(reconciler.status, RECONCILIATION_NOT_ALLOWED)
            adapter.assert_not_called()
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(first[0].bridge_state, COMPLETED)
        self.assertEqual(
            sum(path.endswith("result-task-p26.json") for path in publications),
            1,
        )
        self.assertTrue((self.bridge / "outbox/result-task-p26.ready").is_file())

    def test_remaining_cross_layer_fault_boundaries_are_deterministic(self):
        # Claim exists but neither Binding nor Run exists.
        publish_bridge_task(
            self.bridge, "task-worker-fault", "bridge_harness_task",
            {"request": "status"}, "publisher",
        )
        claim_fault = DeterministicAdapterFaults([
            "after_bridge_claim_before_binding",
        ])
        with self.assertRaises(AdapterInjectedFault):
            run_bridge_harness_worker_once(
                self.bridge, CONSUMER, ScriptedFakeProvider([]),
                audit_directory=self.audit, harness_runner=run_agent,
                session_directory=self.sessions, fault_injector=claim_fault,
            )
        self.assertFalse((self.audit / "bridge_bindings").exists())

        # Binding is durable, but the Run fence has not yet been acquired.
        binding, _claim = self.prepare(task="task-binding-fault")
        binding_fault = DeterministicAdapterFaults([
            "after_binding_before_run_fence",
        ])
        with self.assertRaises(AdapterInjectedFault):
            run_bound_bridge_request(
                self.bridge, self.audit, self.sessions, binding, CONSUMER,
                ScriptedFakeProvider([]), run_agent,
                fault_injector=binding_fault,
            )
        self.assertEqual(list((self.bridge / "locks").glob("*.attempt.lock")), [])

        # Harness Result is durable, while Bridge projection remains absent.
        terminal_fault = DeterministicAdapterFaults([
            "after_harness_result_before_bridge_projection",
        ])
        with self.assertRaises(AdapterInjectedFault):
            run_bound_bridge_request(
                self.bridge, self.audit, self.sessions, binding, CONSUMER,
                ScriptedFakeProvider([
                    {"type": "final_answer", "final_answer": "done"},
                ]), run_agent, fault_injector=terminal_fault,
            )
        self.assertTrue((
            self.audit / "results" / f"{binding['harness_run_id']}.json"
        ).is_file())
        self.assertFalse(
            (self.bridge / "outbox/result-task-binding-fault.json").exists()
        )


if __name__ == "__main__":
    unittest.main()
