import copy
import hashlib
import json
import os
import tempfile
import unittest

from mini_harness_core.audit import AuditWriter, read_events
from mini_harness_core.agent import run_agent
from mini_harness_core.evidence import (
    EvidenceError, EvidenceStore, artifact_ref, create_evidence,
    create_mcp_observation_evidence, create_reasoning_evidence,
    create_reconciliation_evidence, create_subagent_return_evidence,
    create_tool_observation_evidence, create_verification_evidence,
    evidence_fingerprint, evidence_gate, evidence_integrity_check,
    evidence_trace, validate_evidence,
)
from mini_harness_core.planning import complete_step, create_plan, start_step
from mini_harness_core.run_envelope import _replay_transition
from mini_harness_core.verification import replay_verification_transition


RUN = "1" * 32
OTHER_RUN = "2" * 32
EVENT = "3" * 32
ACTION = "4" * 32
SUBJECT = {"kind": "plan_step", "target": "step-1", "claim": "current_reality_verified"}
OBS = {"exit_code": 0, "stdout": "hello", "stderr": ""}


class EvidenceTests(unittest.TestCase):
    class Provider:
        def __init__(self, decisions):
            self.decisions = iter(decisions)

        def complete(self, messages):
            return next(self.decisions)

    def test_all_evidence_types_and_observation_identity(self):
        source = {"action_id": ACTION, "logical_action_id": "logical-1",
                  "attempt": 1, "tool": "shell"}
        tool = create_tool_observation_evidence(
            RUN, SUBJECT, source, OBS, EVENT, accepted=True, read_only=True,
        )
        verification = create_verification_evidence(
            RUN, SUBJECT, {"target_type": "file", "path": "foo.txt"},
            ACTION, OBS, EVENT, True,
        )
        rejected = create_verification_evidence(
            RUN, SUBJECT, None, ACTION, {**OBS, "exit_code": 1}, EVENT, False,
            "verification failed",
        )
        reconciliations = [create_reconciliation_evidence(
            RUN, SUBJECT, ACTION, {"path": "foo.txt"}, result, OBS, EVENT,
        ) for result in ("applied", "not_applied", "uncertain")]
        subagent = create_subagent_return_evidence(
            RUN, SUBJECT, "handoff-1", OTHER_RUN, "completed",
        )
        mcp = create_mcp_observation_evidence(
            RUN, SUBJECT, "demo", "mcp:demo:read", OBS, EVENT,
            call_id="call-1",
        )
        reasoning = create_reasoning_evidence(
            RUN, {**SUBJECT, "claim": "reasoning_completed"}, EVENT,
            "a" * 64, {"status": "completed"},
        )
        self.assertEqual(
            {item["evidence_type"] for item in
             [tool, verification, *reconciliations, subagent, mcp, reasoning]},
            {"tool_observation", "verification", "reconciliation",
             "subagent_return", "mcp_observation", "reasoning_result"},
        )
        identity = tool["content_identity"]["observation"]
        self.assertEqual(identity["stdout_length"], 5)
        self.assertEqual(identity["stdout_sha256"], hashlib.sha256(b"hello").hexdigest())
        self.assertNotIn('"stdout":', json.dumps(tool))
        self.assertFalse(rejected["verification"]["accepted"])

    def test_fingerprint_excludes_id_and_created_at_but_detects_mutation(self):
        one = create_reasoning_evidence(RUN, SUBJECT, EVENT, "a" * 64,
                                        evidence_id="5" * 32, created_at="one",
                                        freshness={"scope": "run", "observed_at": "same", "run_id": RUN})
        two = create_reasoning_evidence(RUN, SUBJECT, EVENT, "a" * 64,
                                        evidence_id="6" * 32, created_at="two",
                                        freshness={"scope": "run", "observed_at": "same", "run_id": RUN})
        self.assertEqual(one["evidence_fingerprint"], two["evidence_fingerprint"])
        changed = copy.deepcopy(one)
        changed["subject"]["claim"] = "changed"
        self.assertNotEqual(changed["evidence_fingerprint"], evidence_fingerprint(changed))
        with self.assertRaisesRegex(EvidenceError, "fingerprint mismatch"):
            validate_evidence(changed)

    def test_store_atomic_immutable_and_duplicate_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EvidenceStore(directory)
            record = create_reasoning_evidence(RUN, SUBJECT, EVENT, "a" * 64)
            store.save(record)
            store.save(copy.deepcopy(record))
            changed = copy.deepcopy(record)
            changed["created_at"] = "different"
            with self.assertRaisesRegex(EvidenceError, "immutable"):
                store.save(changed)
            self.assertFalse(any(name.startswith(".tmp-") for name in os.listdir(directory)))

    def test_artifact_ref_is_historical_and_path_safe(self):
        ref = artifact_ref("dir/foo.txt", "a" * 64, 5)
        evidence = create_verification_evidence(
            RUN, SUBJECT, None, ACTION, OBS, EVENT, True, artifact=ref,
        )
        snapshot = copy.deepcopy(evidence)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "foo.txt")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("changed")
        self.assertEqual(evidence, snapshot)
        for unsafe in ("../foo", "/tmp/foo", "a/../../foo", "~/foo"):
            with self.assertRaises(EvidenceError):
                artifact_ref(unsafe, "a" * 64, 1)

    def test_freshness_and_plan_gate(self):
        current = create_verification_evidence(RUN, SUBJECT, None, ACTION, OBS, EVENT, True)
        self.assertEqual(evidence_gate(current, "step-1", RUN), (True, None))
        historical = create_verification_evidence(
            OTHER_RUN, SUBJECT, None, ACTION, OBS, EVENT, True,
            freshness={"scope": "historical", "observed_at": "then", "run_id": OTHER_RUN},
        )
        self.assertFalse(evidence_gate(historical, "step-1", RUN)[0])
        # Historical metadata remains usable for non-current replay decisions.
        self.assertTrue(evidence_gate(historical, "step-1", RUN, False)[0])

    def test_planning_loads_references_and_rejects_bad_evidence(self):
        plan = start_step(create_plan("goal", [
            {"id": "step-1", "description": "verify", "depends_on": []},
        ]))
        with tempfile.TemporaryDirectory() as directory:
            store = EvidenceStore(directory)
            accepted = store.save(create_verification_evidence(
                RUN, SUBJECT, None, ACTION, OBS, EVENT, True,
            ))
            completed = complete_step(
                plan, "step-1", [accepted["evidence_id"]], store, RUN,
            )
            self.assertEqual(completed["steps"][0]["evidence_ids"], [accepted["evidence_id"]])
            irrelevant = store.save(create_verification_evidence(
                RUN, {**SUBJECT, "target": "step-2"}, None, ACTION, OBS, EVENT, True,
            ))
            with self.assertRaisesRegex(ValueError, "irrelevant"):
                complete_step(plan, "step-1", [irrelevant["evidence_id"]], store, RUN)
            corrupt_path = store._path(accepted["evidence_id"])
            with open(corrupt_path, "r+", encoding="utf-8") as stream:
                value = json.load(stream); value["subject"]["claim"] = "corrupt"
                stream.seek(0); json.dump(value, stream); stream.truncate()
            with self.assertRaises(EvidenceError):
                complete_step(plan, "step-1", [accepted["evidence_id"]], store, RUN)

    def test_reasoning_subagent_and_mcp_cannot_claim_environment(self):
        reasoning = create_reasoning_evidence(RUN, {**SUBJECT, "claim": "reasoning_completed"}, EVENT, "a" * 64)
        self.assertTrue(evidence_gate(reasoning, "step-1", RUN, False)[0])
        self.assertFalse(evidence_gate(reasoning, "step-1", RUN, True)[0])
        subagent = create_subagent_return_evidence(RUN, SUBJECT, "handoff-1", OTHER_RUN, "completed")
        candidate_snapshot = copy.deepcopy(subagent)
        self.assertFalse(evidence_gate(subagent, "step-1", RUN, False)[0])
        main_acceptance = create_verification_evidence(
            RUN, SUBJECT, None, ACTION, OBS, EVENT, True,
            references={"candidate_evidence_id": subagent["evidence_id"]},
        )
        self.assertTrue(evidence_gate(main_acceptance, "step-1", RUN)[0])
        self.assertEqual(subagent, candidate_snapshot)
        mcp = create_mcp_observation_evidence(RUN, SUBJECT, "demo", "mcp:demo:x", OBS, EVENT, call_id="call")
        self.assertTrue(mcp["verification"]["untrusted_external"])
        self.assertFalse(evidence_gate(mcp, "step-1", RUN, True)[0])
        mcp["verification"]["verified"] = True
        mcp["evidence_fingerprint"] = evidence_fingerprint(mcp)
        with self.assertRaisesRegex(EvidenceError, "verified"):
            validate_evidence(mcp)

    def test_integrity_and_trace_use_audit_references(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence_dir = os.path.join(directory, "evidence")
            writer = AuditWriter("7" * 32, RUN, directory)
            writer.append("action_state_changed", "tool", "shell", "started",
                          references={"action_id": ACTION})
            observation = writer.append("action_state_changed", "environment", "shell", "succeeded",
                                        references={"action_id": ACTION})
            record = create_verification_evidence(
                RUN, SUBJECT, None, ACTION, OBS, observation["event_id"], True,
            )
            EvidenceStore(evidence_dir).save(record)
            self.assertTrue(evidence_integrity_check(record["evidence_id"], evidence_dir, directory))
            trace = "\n".join(evidence_trace(record, directory))
            self.assertIn("Observation Event", trace)
            self.assertIn("unavailable", trace)
            record["source"]["observation_event_id"] = "8" * 32
            record["evidence_fingerprint"] = evidence_fingerprint(record)
            EvidenceStore(evidence_dir).save(create_reasoning_evidence(
                RUN, SUBJECT, observation["event_id"], "b" * 64,
            ))

    def test_historical_evidence_replays_without_current_filesystem(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence_dir = os.path.join(directory, "evidence")
            writer = AuditWriter("7" * 32, RUN, directory)
            writer.append("action_state_changed", "tool", "shell", "started",
                          references={"action_id": ACTION})
            event = writer.append("action_state_changed", "environment", "shell", "succeeded",
                                  references={"action_id": ACTION})
            record = create_verification_evidence(
                RUN, SUBJECT, None, ACTION, OBS, event["event_id"], True,
            )
            EvidenceStore(evidence_dir).save(record)
            inputs = {
                "requires_verification": True, "verification_target": None,
                "action_effect": "read_only", "evidence_related": True,
                "historical_recorded_observation": True,
                "observation": record["content_identity"]["observation"],
            }
            output = replay_verification_transition(inputs)
            transition = {
                "transition_type": "verification",
                "input": {**inputs, "evidence_id": record["evidence_id"],
                          "evidence_fingerprint": record["evidence_fingerprint"]},
                "recorded_output": output,
            }
            with open(os.path.join(directory, "unrelated-current-file"), "w") as stream:
                stream.write("changed")
            self.assertEqual(_replay_transition(transition, {}, directory)[0], "MATCH")

    def test_agent_audit_planning_uses_evidence_id(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = AuditWriter("7" * 32, RUN, directory)
            plan = create_plan("inspect", [
                {"id": "step-1", "description": "inspect cwd", "depends_on": []},
            ])
            provider = self.Provider([
                {"type": "tool_call", "command": "pwd"},
                {"type": "final_answer", "final_answer": "done"},
            ])
            self.assertEqual(run_agent(
                "inspect", provider, current_plan=plan, audit_writer=writer,
            ), "done")
            evidence_ids = plan["steps"][0]["evidence_ids"]
            self.assertEqual(len(evidence_ids), 1)
            self.assertTrue(evidence_integrity_check(
                evidence_ids[0], os.path.join(directory, "evidence"), directory,
            ))
            event_types = [event["event_type"] for event in
                           read_events(RUN, directory)]
            self.assertIn("evidence_created", event_types)

    def test_security_and_unknown_schema(self):
        with self.assertRaises(EvidenceError):
            create_reasoning_evidence(RUN, SUBJECT, EVENT, "a" * 64,
                                      metadata={"raw_stdout": "secret"})
        with self.assertRaises(EvidenceError):
            create_reasoning_evidence(
                RUN, SUBJECT, EVENT, "a" * 64,
                references={"note": "Authorization: Bearer abcdefghijk"},
            )
        record = create_reasoning_evidence(RUN, SUBJECT, EVENT, "a" * 64)
        record["evidence_schema_version"] = 2
        with self.assertRaisesRegex(EvidenceError, "unsupported historical evidence schema"):
            validate_evidence(record)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(EvidenceError, "unknown evidence"):
                EvidenceStore(directory).load("f" * 32)


if __name__ == "__main__":
    unittest.main()
