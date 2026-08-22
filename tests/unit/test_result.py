import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from mini_harness_core.agent import run_agent
from mini_harness_core.artifacts import (
    ArtifactStore, create_artifact, create_producer,
)
from mini_harness_core.audit import AuditWriter, read_events
from mini_harness_core.cli import main
from mini_harness_core.evidence import EvidenceStore, create_evidence
from mini_harness_core.result import (
    ResultError, ResultStore, bind_final_result,
    build_authoritative_result_state, evaluate_result_contract,
    normalize_final_candidate, result_fingerprint, result_integrity_check,
)
from mini_harness_core.providers import RealProvider
from mini_harness_core.run_control import create_run_control, request_cancel
from mini_harness_core.run_envelope import RunEnvelopeStore, harness_replay_check


SESSION = "1" * 32
RUN = "2" * 32
DIGEST = "a" * 64


class Provider:
    def __init__(self, decisions):
        self.decisions = list(decisions)

    def complete(self, messages):
        return self.decisions.pop(0)


def candidate(answer="done", **claims):
    return normalize_final_candidate({
        "type": "final_answer", "answer": answer, **claims,
    })["metadata"]


def binding_input(**changes):
    value = {
        "run_id": RUN,
        "run_control": {"state": "running", "reason": None},
        "terminal_failure": None,
        "blocking_reason": None,
        "plan": None,
        "output_contract": None,
        "verification_required": False,
        "accepted_artifacts": [],
        "accepted_evidence": [],
        "candidate": candidate(),
    }
    value.update(changes)
    return value


class ResultStatusTests(unittest.TestCase):
    def test_completed_reactive_compatibility(self):
        output = evaluate_result_contract(binding_input())
        self.assertEqual(output["authoritative_status"], "completed")
        self.assertFalse(output["contradiction"])

    def test_cancelled_overrides_model_success(self):
        output = evaluate_result_contract(binding_input(
            run_control={"state": "cancelled", "reason": "user cancelled"},
            candidate=candidate(claimed_status="completed"),
        ))
        self.assertEqual(output["authoritative_status"], "cancelled")
        self.assertTrue(output["contradiction"])

    def test_failed_blocked_and_incomplete_are_deterministic(self):
        failed = evaluate_result_contract(binding_input(
            terminal_failure="provider terminal failure",
        ))
        blocked = evaluate_result_contract(binding_input(
            blocking_reason="run deadline exceeded",
        ))
        incomplete = evaluate_result_contract(binding_input(
            plan={"plan_id": "plan-1", "status": "active",
                  "completed_step_ids": []},
            candidate=candidate(claimed_status="completed"),
        ))
        self.assertEqual(failed["authoritative_status"], "failed")
        self.assertEqual(blocked["authoritative_status"], "blocked")
        self.assertEqual(incomplete["authoritative_status"], "incomplete")
        self.assertTrue(incomplete["contradiction"])

    def test_failed_plan_is_authoritative_failure(self):
        output = evaluate_result_contract(binding_input(
            plan={"plan_id": "plan-1", "status": "failed",
                  "completed_step_ids": []},
            candidate=candidate(claimed_status="completed"),
        ))
        self.assertEqual(output["authoritative_status"], "failed")
        self.assertEqual(output["reason"], "plan failed")

    def test_plan_completed_does_not_override_unsatisfied_contract(self):
        output = evaluate_result_contract(binding_input(
            plan={"plan_id": "plan-1", "status": "completed",
                  "completed_step_ids": ["step-1"]},
            output_contract={
                "satisfied": False, "contract_fingerprint": DIGEST,
                "accepted_artifact_ids": [],
            },
            candidate=candidate(claimed_status="completed"),
        ))
        self.assertEqual(output["authoritative_status"], "incomplete")
        self.assertEqual(output["reason"], "output contract unsatisfied")

    def test_pending_verification_is_incomplete(self):
        output = evaluate_result_contract(binding_input(
            verification_required=True,
        ))
        self.assertEqual(output["authoritative_status"], "incomplete")
        self.assertEqual(output["reason"], "required evidence unsatisfied")


class ResultClaimAndSecurityTests(unittest.TestCase):
    def test_provider_accepts_old_and_structured_candidate_forms(self):
        old = RealProvider._parse_decision(json.dumps({
            "type": "final_answer", "final_answer": "old",
        }))
        structured = RealProvider._parse_decision(json.dumps({
            "type": "final_answer", "answer": "new",
            "claimed_status": "completed", "artifact_refs": ["3" * 32],
            "evidence_refs": ["4" * 32],
        }))
        self.assertEqual(old["final_answer"], "old")
        self.assertEqual(structured["answer"], "new")
        self.assertEqual(structured["claimed_status"], "completed")

    def test_invalid_artifact_and_evidence_refs_are_rejected(self):
        output = evaluate_result_contract(binding_input(candidate=candidate(
            artifact_refs=["3" * 32], evidence_refs=["4" * 32],
        )))
        self.assertEqual(output["accepted_artifact_ids"], [])
        self.assertEqual(output["accepted_evidence_ids"], [])
        self.assertTrue(output["contradiction"])
        self.assertIn("invalid artifact reference", output["reason"])
        self.assertIn("invalid evidence reference", output["reason"])

    def test_valid_claim_refs_bind_only_accepted_identities(self):
        output = evaluate_result_contract(binding_input(
            accepted_artifacts=[{
                "artifact_id": "3" * 32,
                "artifact_fingerprint": DIGEST,
            }],
            accepted_evidence=[{
                "evidence_id": "4" * 32,
                "evidence_fingerprint": DIGEST,
            }],
            candidate=candidate(
                artifact_refs=["3" * 32], evidence_refs=["4" * 32],
            ),
        ))
        self.assertEqual(output["accepted_artifact_ids"], ["3" * 32])
        self.assertEqual(output["accepted_evidence_ids"], ["4" * 32])
        self.assertFalse(output["contradiction"])

    def test_secret_answer_is_never_persisted_or_returned(self):
        normalized = normalize_final_candidate({
            "type": "final_answer", "answer": "password=leaked-value",
            "claimed_status": "completed",
        })
        state = binding_input(candidate=normalized["metadata"])
        result, output = bind_final_result(state, normalized)
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertTrue(output["contradiction"])
        self.assertNotIn("leaked-value", encoded)
        self.assertNotIn("password=", encoded)

    def test_conflicting_text_is_replaced_by_safe_summary(self):
        normalized = normalize_final_candidate({
            "type": "final_answer", "answer": "everything succeeded",
            "claimed_status": "completed",
        })
        state = binding_input(
            output_contract={
                "satisfied": False, "contract_fingerprint": DIGEST,
                "accepted_artifact_ids": [],
            },
            candidate=normalized["metadata"],
        )
        result, _output = bind_final_result(state, normalized)
        self.assertEqual(result["status"], "incomplete")
        self.assertNotIn("everything succeeded", result["answer"])
        self.assertIn("output contract unsatisfied", result["answer"])


class ResultArtifactBindingTests(unittest.TestCase):
    def artifact_event(self, writer, record):
        writer.append(
            "artifact_accepted" if record["status"] == "accepted"
            else "artifact_rejected",
            "harness", "artifact", record["status"],
            references={
                "artifact_id": record["artifact_id"],
                "artifact_fingerprint": record["artifact_fingerprint"],
                "path": record["path"], "status": record["status"],
                "evidence_ids": record["evidence_ids"],
            },
        )

    def make_artifact(self, status, identity, artifact_id,
                      supersedes=None):
        return create_artifact(
            RUN, "report.md", status, identity,
            create_producer(
                RUN, action_id="action-1", capability="shell", tool="shell",
            ),
            artifact_id=artifact_id,
            supersedes_artifact_id=supersedes,
        )

    def test_only_current_accepted_lineage_is_eligible(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = AuditWriter(SESSION, RUN, directory)
            writer.append("run_started", "harness", "run", "running")
            store = ArtifactStore(os.path.join(directory, "artifacts"))
            old = self.make_artifact(
                "accepted", {"sha256": "b" * 64, "size": 1}, "3" * 32,
            )
            store.save(old); self.artifact_event(writer, old)
            new = self.make_artifact(
                "accepted", {"sha256": "c" * 64, "size": 2}, "4" * 32,
                old["artifact_id"],
            )
            store.save(new); self.artifact_event(writer, new)
            rejected = create_artifact(
                RUN, "rejected.md", "rejected",
                {"sha256": "d" * 64, "size": 3},
                create_producer(
                    RUN, action_id="action-2", capability="shell", tool="shell",
                ),
                artifact_id="5" * 32,
            )
            store.save(rejected); self.artifact_event(writer, rejected)
            state, _ = build_authoritative_result_state(
                RUN, {"type": "final_answer", "answer": "done"},
                artifact_store=store, audit_directory=directory,
            )
            self.assertEqual(
                [item["artifact_id"] for item in state["accepted_artifacts"]],
                [new["artifact_id"]],
            )

    def test_corrupted_artifact_is_not_eligible(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = AuditWriter(SESSION, RUN, directory)
            writer.append("run_started", "harness", "run", "running")
            store = ArtifactStore(os.path.join(directory, "artifacts"))
            record = self.make_artifact(
                "accepted", {"sha256": "b" * 64, "size": 1}, "3" * 32,
            )
            store.save(record); self.artifact_event(writer, record)
            with open(store._path(record["artifact_id"]), "r+",
                      encoding="utf-8") as stream:
                changed = json.load(stream)
                changed["path"] = "changed.md"
                stream.seek(0); json.dump(changed, stream); stream.truncate()
            state, _ = build_authoritative_result_state(
                RUN, {"type": "final_answer", "answer": "done"},
                artifact_store=store, audit_directory=directory,
            )
            self.assertEqual(state["accepted_artifacts"], [])


class ResultEvidenceBindingTests(unittest.TestCase):
    def record(self, run_id, evidence_type, subject, source, verification,
               freshness=None):
        return create_evidence(
            run_id, evidence_type, subject, source=source,
            verification=verification,
            freshness=freshness,
        )

    def test_only_fresh_relevant_main_accepted_evidence_is_eligible(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EvidenceStore(os.path.join(directory, "evidence"))
            base_source = {
                "action_id": "action-1", "logical_action_id": "logical-1",
                "attempt": 1, "observation_event_id": "event-1",
                "tool": "shell", "run_id": RUN,
            }
            accepted = self.record(
                RUN, "tool_observation",
                {"kind": "plan_step", "target": "step-1",
                 "claim": "observation_recorded"},
                base_source, {"accepted": True, "read_only": True},
            )
            unrelated = self.record(
                RUN, "tool_observation",
                {"kind": "plan_step", "target": "step-2",
                 "claim": "observation_recorded"},
                {**base_source, "action_id": "action-2"},
                {"accepted": True, "read_only": True},
            )
            stale = self.record(
                RUN, "tool_observation",
                {"kind": "plan_step", "target": "step-1",
                 "claim": "observation_recorded"},
                {**base_source, "action_id": "action-3"},
                {"accepted": True, "read_only": True},
                {"scope": "historical", "observed_at": "old", "run_id": RUN},
            )
            reasoning = self.record(
                RUN, "reasoning_result",
                {"kind": "plan_step", "target": "step-1",
                 "claim": "reasoning_completed"},
                {"model_decision_event_id": "event-2",
                 "decision_digest": "b" * 64},
                {"environment_grounded": False},
            )
            subagent = self.record(
                RUN, "subagent_return",
                {"kind": "plan_step", "target": "step-1",
                 "claim": "subagent_candidate"},
                {"handoff_id": "handoff-1", "subagent_run_id": "9" * 32,
                 "return_status": "completed"},
                {"candidate": True, "accepted_by_main": False},
            )
            mcp = self.record(
                RUN, "mcp_observation",
                {"kind": "plan_step", "target": "step-1",
                 "claim": "mcp_success"},
                {"server": "demo", "tool": "echo",
                 "observation_event_id": "event-3", "action_id": "action-4"},
                {"untrusted_external": True},
            )
            for record in (accepted, unrelated, stale, reasoning, subagent, mcp):
                store.save(record)
            plan = {
                "plan_id": "plan-1", "status": "completed", "steps": [
                    {"id": "step-1", "status": "completed"},
                    {"id": "step-2", "status": "pending"},
                ],
            }
            with patch(
                "mini_harness_core.result.evidence_integrity_check",
                return_value=True,
            ):
                state, _ = build_authoritative_result_state(
                    RUN, {"type": "final_answer", "answer": "done"},
                    plan=plan, evidence_store=store,
                    audit_directory=directory,
                )
                filesystem_state, _ = build_authoritative_result_state(
                    RUN, {"type": "final_answer", "answer": "done"},
                    plan=plan, verification_required=True,
                    evidence_store=store, audit_directory=directory,
                )
            ids = {item["evidence_id"] for item in state["accepted_evidence"]}
            self.assertEqual(ids, {accepted["evidence_id"], reasoning["evidence_id"]})
            filesystem_ids = {
                item["evidence_id"]
                for item in filesystem_state["accepted_evidence"]
            }
            self.assertEqual(filesystem_ids, {accepted["evidence_id"]})


class ResultPersistenceReplayAndAuditTests(unittest.TestCase):
    def test_store_is_immutable_and_fingerprint_is_checked(self):
        normalized = normalize_final_candidate({
            "type": "final_answer", "answer": "done",
        })
        result, _ = bind_final_result(
            binding_input(candidate=normalized["metadata"]), normalized,
        )
        with tempfile.TemporaryDirectory() as directory:
            store = ResultStore(directory)
            store.save(result)
            changed = copy.deepcopy(result)
            changed["answer"] = "changed"
            changed["candidate"].update(normalize_final_candidate({
                "type": "final_answer", "answer": "changed",
            })["metadata"])
            changed["result_fingerprint"] = result_fingerprint(changed)
            with self.assertRaisesRegex(ResultError, "immutable"):
                store.save(changed)
            with open(store._path(RUN), "r+", encoding="utf-8") as stream:
                damaged = json.load(stream)
                damaged["status"] = "failed"
                stream.seek(0); json.dump(damaged, stream); stream.truncate()
            with self.assertRaisesRegex(ResultError, "fingerprint"):
                store.load(RUN)

    def test_agent_audit_replay_and_integrity_match_without_answer_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = AuditWriter(SESSION, RUN, directory)
            result = run_agent(
                "reactive", Provider([{
                    "type": "final_answer", "answer": "safe answer",
                    "claimed_status": "completed", "artifact_refs": [],
                    "evidence_refs": [],
                }]), audit_writer=writer, return_result=True,
            )
            self.assertEqual(result["status"], "completed")
            events = read_events(RUN, directory)
            self.assertIn("final_candidate_received",
                          [item["event_type"] for item in events])
            self.assertIn("final_result_emitted",
                          [item["event_type"] for item in events])
            self.assertNotIn("safe answer", json.dumps(events))
            envelope = RunEnvelopeStore(os.path.join(
                directory, "envelopes",
            )).load(RUN)
            self.assertNotIn("safe answer", json.dumps(envelope))
            self.assertTrue(harness_replay_check(envelope, directory)["match"])
            self.assertTrue(result_integrity_check(
                RUN, os.path.join(directory, "results"),
                os.path.join(directory, "artifacts"),
                os.path.join(directory, "evidence"), directory,
            ))

    def test_corrupted_recorded_output_is_replay_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = AuditWriter(SESSION, RUN, directory)
            run_agent("reactive", Provider([{
                "type": "final_answer", "answer": "done",
            }]), audit_writer=writer)
            store = RunEnvelopeStore(os.path.join(directory, "envelopes"))
            path = store._path(RUN)
            with open(path, "r+", encoding="utf-8") as stream:
                envelope = json.load(stream)
                transition = next(item for item in envelope["transitions"]
                                  if item["transition_type"] == "result_binding")
                transition["recorded_output"]["authoritative_status"] = "failed"
                stream.seek(0); json.dump(envelope, stream); stream.truncate()
            replay = harness_replay_check(store.load(RUN), directory)
            result_transition = next(
                item for item in replay["transitions"]
                if item["transition_type"] == "result_binding"
            )
            self.assertEqual(result_transition["status"], "MISMATCH")

    def test_current_filesystem_change_does_not_affect_historical_result(self):
        requirement = {
            "name": "report", "artifact_type": "workspace_file",
            "path": "report.md",
            "requirements": ["exists", "non_empty", "verified"],
        }
        with tempfile.TemporaryDirectory() as root:
            workspace = os.path.join(root, "workspace")
            audit = os.path.join(root, "audit")
            os.mkdir(workspace)
            old = os.getcwd()
            try:
                os.chdir(workspace)
                writer = AuditWriter(SESSION, RUN, audit)
                with patch("mini_harness_core.agent.request_approval",
                           return_value=True):
                    result = run_agent(
                        "create report", Provider([
                            {"type": "tool_call", "command":
                             "echo hello > report.md"},
                            {"type": "tool_call", "command": "cat report.md"},
                            {"type": "final_answer", "answer": "done"},
                        ]), max_steps=3, audit_writer=writer,
                        output_contract={"required_artifacts": [requirement]},
                        return_result=True,
                    )
                self.assertEqual(result["status"], "completed")
                with open("report.md", "w", encoding="utf-8") as stream:
                    stream.write("changed later")
                envelope = RunEnvelopeStore(os.path.join(
                    audit, "envelopes",
                )).load(RUN)
                self.assertTrue(harness_replay_check(envelope, audit)["match"])
                self.assertTrue(result_integrity_check(
                    RUN, os.path.join(audit, "results"),
                    os.path.join(audit, "artifacts"),
                    os.path.join(audit, "evidence"), audit,
                ))
            finally:
                os.chdir(old)


class ResultCLITests(unittest.TestCase):
    def test_result_show_check_and_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = AuditWriter(SESSION, RUN, directory)
            run_agent("reactive", Provider([{
                "type": "final_answer", "answer": "not printed by show",
            }]), audit_writer=writer)
            with patch("mini_harness_core.cli.RESULT_DIR",
                       os.path.join(directory, "results")), patch(
                "mini_harness_core.cli.ARTIFACT_DIR",
                os.path.join(directory, "artifacts"),
            ), patch("mini_harness_core.cli.EVIDENCE_DIR",
                     os.path.join(directory, "evidence")), patch(
                "mini_harness_core.cli.AUDIT_DIR", directory,
            ):
                output = io.StringIO()
                with patch("sys.argv", ["mini_harness.py", "--result-show", RUN]), \
                        contextlib.redirect_stdout(output):
                    main()
                self.assertIn("Status: completed", output.getvalue())
                self.assertNotIn("not printed by show", output.getvalue())
                output = io.StringIO()
                with patch("sys.argv", ["mini_harness.py", "--result-check", RUN]), \
                        contextlib.redirect_stdout(output):
                    main()
                self.assertEqual(output.getvalue().strip(), "RESULT CHECK MATCH")
                output = io.StringIO()
                with patch("sys.argv", ["mini_harness.py", "--result-check", "f" * 32]), \
                        contextlib.redirect_stdout(output):
                    main()
                self.assertEqual(output.getvalue().strip(), "RESULT CHECK MISMATCH")


if __name__ == "__main__":
    unittest.main()
