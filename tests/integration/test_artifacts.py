import contextlib
import copy
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from mini_harness_core.agent import run_agent
from mini_harness_core.artifacts import (
    ArtifactError, ArtifactStore, OutputContractStore,
    artifact_contract_transition_input, artifact_fingerprint,
    artifact_integrity_check, artifact_trace, create_artifact,
    create_output_contract, create_producer, current_artifacts,
    current_output_contract_gate,
    evaluate_artifact_contract, observe_workspace_file, outputs_status,
    replay_artifact_contract_transition, select_supersession,
    validate_artifact, validate_artifact_path, validate_supersession,
)
from mini_harness_core.audit import (
    AuditWriter, explain_events, read_events, safe_shell_command_identity,
)
from mini_harness_core.evidence import (
    EvidenceStore, artifact_ref, create_mcp_observation_evidence,
    create_reasoning_evidence, create_subagent_return_evidence,
    create_verification_evidence,
)
from mini_harness_core.planning import complete_step, create_plan, start_step
from mini_harness_core.run_envelope import (
    RunEnvelopeStore, _replay_transition, harness_replay_check,
)


RUN = "1" * 32
SESSION = "2" * 32
ACTION = "action-1"
EVENT = "3" * 32
PATH = "report.md"
CONTENT = b"hello\n"
IDENTITY = {"sha256": hashlib.sha256(CONTENT).hexdigest(), "size": len(CONTENT)}


class Provider:
    def __init__(self, decisions):
        self.decisions = iter(decisions)

    def complete(self, messages):
        return next(self.decisions)


def producer(run_id=RUN, **changes):
    values = {
        "action_id": ACTION, "capability": "shell", "step_id": None,
        "model_request_id": "request-1", "model_decision_event_id": EVENT,
        "tool": "shell",
    }
    values.update(changes)
    return create_producer(run_id, **values)


def requirement(requirements=None, path=PATH, name="report", step_id=None):
    value = {
        "name": name, "artifact_type": "workspace_file", "path": path,
        "requirements": requirements or [
            "exists", "non_empty", "content_identity", "verified",
        ],
    }
    if step_id is not None:
        value["step_id"] = step_id
    return value


def verification(run_id=RUN, identity=IDENTITY, path=PATH, accepted=True):
    return create_verification_evidence(
        run_id,
        {"kind": "workspace_file", "target": path, "claim": "content_verified"},
        {"target_type": "file", "path": path}, ACTION,
        {"exit_code": 0 if accepted else 1, "stdout": "hello\n", "stderr": ""},
        EVENT, accepted, None if accepted else "failed",
        artifact=artifact_ref(path, identity["sha256"], identity["size"]),
    )


class ArtifactLifecycleTests(unittest.TestCase):
    def test_lifecycle_records_and_minimal_supersession_relation(self):
        proposed = create_artifact(
            RUN, PATH, "proposed", {"sha256": None, "size": None}, producer()
        )
        records = [proposed]
        for status in ("materialized", "verified", "accepted", "rejected"):
            records.append(create_artifact(RUN, PATH, status, IDENTITY, producer()))
        self.assertEqual(
            {record["status"] for record in records},
            {"proposed", "materialized", "verified", "accepted", "rejected"},
        )

        first = create_artifact(RUN, PATH, "accepted", IDENTITY, producer())
        second_identity = {"sha256": "b" * 64, "size": 7}
        second = create_artifact(
            RUN, PATH, "accepted", second_identity, producer(),
            supersedes_artifact_id=first["artifact_id"],
        )
        self.assertEqual(current_artifacts([first, second]), [second])
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["supersedes_artifact_id"], first["artifact_id"])

    def test_content_identity_and_record_fingerprint_are_distinct(self):
        one = create_artifact(
            RUN, PATH, "materialized", IDENTITY, producer(),
            artifact_id="4" * 32, created_at="one",
        )
        two = create_artifact(
            RUN, PATH, "materialized", IDENTITY, producer(),
            artifact_id="5" * 32, created_at="two",
        )
        self.assertEqual(one["artifact_fingerprint"], two["artifact_fingerprint"])
        self.assertNotEqual(one["artifact_fingerprint"], IDENTITY["sha256"])
        changed = copy.deepcopy(one)
        changed["content_identity"]["size"] += 1
        self.assertNotEqual(changed["artifact_fingerprint"], artifact_fingerprint(changed))
        with self.assertRaisesRegex(ArtifactError, "fingerprint mismatch"):
            validate_artifact(changed)

    def test_fresh_observation_creates_new_identity_without_mutating_history(self):
        with tempfile.TemporaryDirectory() as workspace:
            path = os.path.join(workspace, PATH)
            with open(path, "wb") as stream:
                stream.write(CONTENT)
            old_identity = observe_workspace_file(PATH, workspace)
            old = create_artifact(RUN, PATH, "accepted", old_identity, producer())
            snapshot = copy.deepcopy(old)
            with open(path, "wb") as stream:
                stream.write(b"changed")
            new_identity = observe_workspace_file(PATH, workspace)
            self.assertNotEqual(old_identity, new_identity)
            self.assertEqual(old, snapshot)
            new = create_artifact(
                RUN, PATH, "materialized", new_identity, producer(),
                supersedes_artifact_id=old["artifact_id"],
            )
            self.assertEqual(new["supersedes_artifact_id"], old["artifact_id"])

    def test_store_is_atomic_immutable_and_does_not_store_file_body(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            record = create_artifact(RUN, PATH, "materialized", IDENTITY, producer())
            store.save(record)
            store.save(copy.deepcopy(record))
            with open(store._path(record["artifact_id"]), encoding="utf-8") as stream:
                payload = stream.read()
            self.assertNotIn("hello", payload)
            changed = copy.deepcopy(record)
            changed["created_at"] = "changed"
            with self.assertRaisesRegex(ArtifactError, "immutable"):
                store.save(changed)
            with self.assertRaisesRegex(ArtifactError, "正文"):
                create_artifact(
                    RUN, PATH, "materialized", IDENTITY, producer(),
                    references={"raw_stdout": "hello"},
                )
            self.assertFalse(any(name.startswith(".tmp-") for name in os.listdir(directory)))

    def test_security_rejects_escape_secret_files_and_symlink(self):
        for path in ("../report", "/tmp/report", ".env", ".env.local",
                     "keys/private-key.pem", ".audit/x.json"):
            with self.assertRaises(ArtifactError):
                validate_artifact_path(path)
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as outside:
            outside_file = os.path.join(outside, "report")
            with open(outside_file, "w", encoding="utf-8") as stream:
                stream.write("x")
            os.symlink(outside_file, os.path.join(workspace, PATH))
            with self.assertRaises(ArtifactError):
                observe_workspace_file(PATH, workspace)

    def test_cross_run_supersession_and_immutability(self):
        old_run, new_run = "8" * 32, "9" * 32
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(os.path.join(directory, "artifacts"))
            old = create_artifact(
                old_run, PATH, "accepted", IDENTITY, producer(old_run)
            )
            store.save(old)
            AuditWriter(SESSION, old_run, directory).append(
                "artifact_accepted", "harness", "artifact", "accepted",
                references={"artifact_id": old["artifact_id"],
                            "artifact_fingerprint": old["artifact_fingerprint"],
                            "path": old["path"], "status": old["status"],
                            "evidence_ids": []},
            )
            snapshot = copy.deepcopy(old)
            new_identity = {"sha256": "b" * 64, "size": 7}
            new = create_artifact(
                new_run, PATH, "materialized", new_identity, producer(new_run),
                supersedes_artifact_id=old["artifact_id"],
            )
            self.assertEqual(validate_supersession(
                new, new_run, store, audit_directory=directory
            )["artifact_id"], old["artifact_id"])
            with self.assertRaisesRegex(ArtifactError, "current Run"):
                validate_supersession(new, "7" * 32, store,
                                      audit_directory=directory)
            self.assertEqual(old, snapshot)
            unlinked = create_artifact(
                new_run, PATH, "materialized", new_identity, producer(new_run)
            )
            self.assertEqual(select_supersession(
                unlinked, new_run, store, audit_directory=directory
            )["artifact_id"], old["artifact_id"])

            wrong_path = create_artifact(
                new_run, "other.md", "materialized", new_identity,
                producer(new_run), supersedes_artifact_id=old["artifact_id"],
            )
            with self.assertRaisesRegex(ArtifactError, "path"):
                validate_supersession(wrong_path, new_run, store,
                                      audit_directory=directory)
            same = create_artifact(
                new_run, PATH, "materialized", IDENTITY, producer(new_run),
                supersedes_artifact_id=old["artifact_id"],
            )
            with self.assertRaisesRegex(ArtifactError, "不需要"):
                validate_supersession(same, new_run, store,
                                      audit_directory=directory)
            same_unlinked = create_artifact(
                new_run, PATH, "materialized", IDENTITY, producer(new_run)
            )
            self.assertIsNone(select_supersession(
                same_unlinked, new_run, store, audit_directory=directory
            ))
            missing = create_artifact(
                new_run, PATH, "materialized", new_identity, producer(new_run),
                supersedes_artifact_id="a" * 32,
            )
            with self.assertRaisesRegex(ArtifactError, "不存在"):
                validate_supersession(missing, new_run, store,
                                      audit_directory=directory)
            with self.assertRaises(ArtifactError):
                create_artifact(
                    new_run, PATH, "materialized", new_identity,
                    producer(new_run), artifact_id="c" * 32,
                    supersedes_artifact_id="c" * 32,
                )

    def test_corrupted_old_and_cycle_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(os.path.join(directory, "artifacts"))
            old = create_artifact(RUN, PATH, "accepted", IDENTITY, producer())
            store.save(old)
            AuditWriter(SESSION, RUN, directory).append(
                "artifact_accepted", "harness", "artifact", "accepted",
                references={"artifact_id": old["artifact_id"],
                            "artifact_fingerprint": old["artifact_fingerprint"],
                            "path": old["path"], "status": old["status"],
                            "evidence_ids": []},
            )
            candidate = create_artifact(
                "9" * 32, PATH, "materialized", {"sha256": "b" * 64, "size": 2},
                producer("9" * 32), supersedes_artifact_id=old["artifact_id"],
            )
            with open(store._path(old["artifact_id"]), "r+", encoding="utf-8") as stream:
                value = json.load(stream); value["path"] = "corrupt.md"
                stream.seek(0); json.dump(value, stream); stream.truncate()
            with self.assertRaisesRegex(ArtifactError, "损坏"):
                validate_supersession(candidate, "9" * 32, store,
                                      audit_directory=directory)

        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(os.path.join(directory, "artifacts"))
            one_id, two_id = "d" * 32, "e" * 32
            records = [
                create_artifact(
                    RUN, PATH, "accepted", IDENTITY, producer(),
                    artifact_id=one_id, supersedes_artifact_id=two_id,
                ),
                create_artifact(
                    "9" * 32, PATH, "accepted",
                    {"sha256": "b" * 64, "size": 2}, producer("9" * 32),
                    artifact_id=two_id, supersedes_artifact_id=one_id,
                ),
            ]
            for record in records:
                store.save(record)
                AuditWriter(SESSION, record["run_id"], directory).append(
                    "artifact_accepted", "harness", "artifact", "accepted",
                    references={"artifact_id": record["artifact_id"],
                                "artifact_fingerprint": record["artifact_fingerprint"],
                                "path": record["path"], "status": record["status"],
                                "evidence_ids": []},
                )
            candidate = create_artifact(
                "7" * 32, PATH, "materialized", {"sha256": "c" * 64, "size": 3},
                producer("7" * 32), supersedes_artifact_id=one_id,
            )
            with self.assertRaisesRegex(ArtifactError, "integrity"):
                validate_supersession(candidate, "7" * 32, store,
                                      audit_directory=directory)


class OutputContractTests(unittest.TestCase):
    def setUp(self):
        self.evidence = verification()
        self.artifact = create_artifact(
            RUN, PATH, "materialized", IDENTITY, producer(),
            evidence_ids=[self.evidence["evidence_id"]],
        )

    def test_all_requirements_and_exact_path_semantics(self):
        inputs, result = evaluate_artifact_contract(
            self.artifact, [self.evidence], requirement()
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(replay_artifact_contract_transition(inputs), result)

        empty = create_artifact(
            RUN, PATH, "materialized",
            {"sha256": hashlib.sha256(b"").hexdigest(), "size": 0}, producer(),
        )
        _inputs, rejected = evaluate_artifact_contract(
            empty, [], requirement(["exists", "non_empty", "verified"])
        )
        self.assertEqual(set(rejected["unsatisfied_requirements"]), {"non_empty", "verified"})
        _inputs, wrong_path = evaluate_artifact_contract(
            self.artifact, [self.evidence], requirement(path="other.md")
        )
        self.assertIn("exact_path", wrong_path["unsatisfied_requirements"])
        _inputs, failed = evaluate_artifact_contract(
            self.artifact, [verification(accepted=False)],
            requirement(["exists"]),
        )
        self.assertIn("verification_failed", failed["unsatisfied_requirements"])

    def test_irrelevant_reasoning_subagent_and_mcp_evidence_cannot_verify(self):
        reasoning = create_reasoning_evidence(
            RUN, {"kind": "workspace_file", "target": PATH, "claim": "done"},
            EVENT, "a" * 64,
        )
        subagent = create_subagent_return_evidence(
            RUN, {"kind": "workspace_file", "target": PATH, "claim": "candidate"},
            "handoff-1", "6" * 32, "completed",
        )
        mcp = create_mcp_observation_evidence(
            RUN, {"kind": "workspace_file", "target": PATH, "claim": "external"},
            "demo", "mcp:demo:get", {"exit_code": 0, "result": "x"}, EVENT,
            call_id="call-1",
        )
        for evidence in (reasoning, subagent, mcp, verification(path="other.md")):
            _inputs, result = evaluate_artifact_contract(
                self.artifact, [evidence], requirement(["verified"])
            )
            self.assertEqual(result["unsatisfied_requirements"], ["verified"])

    def test_subagent_candidate_requires_main_grounding(self):
        candidate_producer = create_producer(
            RUN, kind="subagent_candidate", subagent_run_id="6" * 32,
            handoff_id="handoff-1",
        )
        candidate = create_artifact(
            RUN, PATH, "verified", IDENTITY, candidate_producer,
            evidence_ids=[self.evidence["evidence_id"]],
        )
        _inputs, result = evaluate_artifact_contract(
            candidate, [self.evidence], requirement()
        )
        self.assertIn("main_acceptance", result["unsatisfied_requirements"])
        with self.assertRaisesRegex(ArtifactError, "Subagent candidate"):
            create_artifact(RUN, PATH, "accepted", IDENTITY, candidate_producer)

    def test_output_contract_store_status_and_historical_replay_ignore_filesystem(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact_store = ArtifactStore(os.path.join(directory, "artifacts"))
            evidence_store = EvidenceStore(os.path.join(directory, "evidence"))
            contract_store = OutputContractStore(os.path.join(directory, "contracts"))
            contract = contract_store.save(create_output_contract(
                RUN, {"required_artifacts": [requirement()]}
            ))
            evidence_store.save(self.evidence)
            accepted = create_artifact(
                RUN, PATH, "accepted", IDENTITY, producer(),
                [self.evidence["evidence_id"]], contract["required_artifacts"][0],
            )
            artifact_store.save(accepted)
            status = outputs_status(
                RUN, contract_store, artifact_store, evidence_store
            )
            self.assertTrue(status["satisfied"])

            workspace = os.path.join(directory, "workspace")
            os.mkdir(workspace)
            with open(os.path.join(workspace, PATH), "wb") as stream:
                stream.write(CONTENT)
            self.assertTrue(current_output_contract_gate(
                RUN, contract_store, artifact_store, evidence_store, workspace
            )["satisfied"])
            with open(os.path.join(workspace, PATH), "wb") as stream:
                stream.write(b"changed after acceptance")
            current_gate = current_output_contract_gate(
                RUN, contract_store, artifact_store, evidence_store, workspace
            )
            self.assertFalse(current_gate["satisfied"])
            self.assertIn(
                "current_content_identity",
                current_gate["required_artifacts"][0]["unsatisfied_requirements"],
            )
            self.assertEqual(artifact_store.load(accepted["artifact_id"]), accepted)

            transition = {
                "transition_type": "artifact_contract",
                "input": artifact_contract_transition_input(
                    accepted, [self.evidence], contract["required_artifacts"][0]
                ),
                "recorded_output": evaluate_artifact_contract(
                    accepted, [self.evidence], contract["required_artifacts"][0]
                )[1],
            }
            with open(os.path.join(directory, PATH), "w", encoding="utf-8") as stream:
                stream.write("current filesystem changed")
            self.assertEqual(_replay_transition(transition, {}, directory)[0], "MATCH")

    def test_harness_owned_contract_cannot_be_lowered_by_model_metadata(self):
        contract = create_output_contract(
            RUN, {"required_artifacts": [requirement(["verified"])]}
        )
        self.assertEqual(contract["required_artifacts"][0]["requirements"], ["verified"])
        _inputs, result = evaluate_artifact_contract(
            self.artifact, [], contract["required_artifacts"][0]
        )
        self.assertFalse(result["accepted"])


class ArtifactAuditAndRunTests(unittest.TestCase):
    def test_shell_approval_audit_persists_only_command_identity(self):
        cases = [
            ("echo 'secret payload' > file.txt", "secret payload", "file.txt", "cat file.txt"),
            ("cat <<'EOF' > heredoc.txt\nheredoc secret marker\nEOF",
             "heredoc secret marker", "heredoc.txt", "pwd"),
        ]
        for index, (command, marker, target, verification_command) in enumerate(cases, 3):
            identity = safe_shell_command_identity(command)
            self.assertEqual(identity["command_length"], len(command))
            self.assertEqual(identity["command_sha256"], hashlib.sha256(
                command.encode("utf-8")
            ).hexdigest())
            self.assertEqual(identity["target"], target)
            self.assertTrue(identity["has_redirection"])
            with tempfile.TemporaryDirectory() as directory:
                run_id = str(index) * 32
                writer = AuditWriter(SESSION, run_id, directory)
                provider = Provider([
                    {"type": "tool_call", "tool": "shell", "command": command},
                    {"type": "tool_call", "tool": "shell",
                     "command": verification_command},
                    {"type": "final_answer", "final_answer": "done"},
                ])
                previous_cwd = os.getcwd()
                try:
                    os.chdir(directory)
                    with patch("mini_harness_core.agent.request_approval",
                               return_value=True):
                        self.assertEqual(run_agent(
                            "audit sanitization", provider, max_steps=3,
                            audit_writer=writer,
                        ), "done")
                finally:
                    os.chdir(previous_cwd)
                with open(writer.path, encoding="utf-8") as stream:
                    persisted = stream.read()
                self.assertNotIn(marker, persisted)
                self.assertNotIn(command, persisted)
                approvals = [event for event in read_events(run_id, directory)
                             if event["event_type"] in {
                                 "approval_requested", "approval_decided",
                             }]
                self.assertTrue(approvals)
                self.assertTrue(all(isinstance(event["subject"], dict)
                                    for event in approvals))
                explanation = explain_events(read_events(run_id, directory))
                self.assertIn("FINAL AUTHORIZATION: ASK", explanation)
                self.assertIn("Human Approval=granted", explanation)
                self.assertIn("action_state_changed", explanation)

        sensitive_command = (
            "echo 'Authorization: Bearer audit secret marker' > auth.txt"
        )
        with tempfile.TemporaryDirectory() as directory:
            run_id = "5" * 32
            writer = AuditWriter(SESSION, run_id, directory)
            for event_type in (
                "tool_requested", "approval_requested", "approval_decided",
                "action_state_changed",
            ):
                writer.append(event_type, "harness", sensitive_command,
                              "granted")
            with open(writer.path, encoding="utf-8") as stream:
                persisted = stream.read()
            self.assertNotIn("Authorization", persisted)
            self.assertNotIn("Bearer", persisted)
            self.assertNotIn("audit secret marker", persisted)
            events = read_events(run_id, directory)
            self.assertTrue(all(event["subject"]["target"] == "auth.txt"
                                for event in events))
            self.assertTrue(all(
                event["subject"]["command_sha256"]
                == hashlib.sha256(sensitive_command.encode("utf-8")).hexdigest()
                for event in events
            ))

    def test_legacy_raw_command_audit_remains_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            run_id = "6" * 32
            event = {
                "version": 1, "event_id": "7" * 32,
                "timestamp": "2026-01-01T00:00:00Z", "sequence": 1,
                "run_id": run_id, "session_id": SESSION,
                "event_type": "approval_decided", "actor": "user",
                "subject": "echo legacy > file.txt", "outcome": "granted",
                "reason": None, "references": {}, "summary": None,
            }
            path = os.path.join(directory, f"{run_id}.jsonl")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(event) + "\n")
            loaded = read_events(run_id, directory)
            self.assertEqual(loaded[0]["subject"], event["subject"])
            self.assertIn("approval_decided", explain_events(loaded))

    def test_integrity_trace_and_supersession_audit_references(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence_dir = os.path.join(directory, "evidence")
            artifact_dir = os.path.join(directory, "artifacts")
            writer = AuditWriter(SESSION, RUN, directory)
            writer.append("action_state_changed", "tool", "shell", "started",
                          references={"action_id": ACTION})
            writer.append("action_state_changed", "environment", "shell", "succeeded",
                          references={"action_id": ACTION})
            evidence = verification()
            EvidenceStore(evidence_dir).save(evidence)
            record = create_artifact(
                RUN, PATH, "accepted", IDENTITY, producer(),
                [evidence["evidence_id"]], requirement(),
            )
            ArtifactStore(artifact_dir).save(record)
            refs = {
                "artifact_id": record["artifact_id"],
                "artifact_fingerprint": record["artifact_fingerprint"],
                "path": record["path"], "status": record["status"],
                "evidence_ids": record["evidence_ids"],
            }
            writer.append("artifact_accepted", "harness", "artifact", "accepted",
                          references=refs)
            self.assertTrue(artifact_integrity_check(
                record["artifact_id"], artifact_dir, evidence_dir, directory
            ))
            trace = "\n".join(artifact_trace(
                record, EvidenceStore(evidence_dir), directory
            ))
            self.assertIn("Evidence", trace)
            self.assertIn("Producer Action", trace)
            with open(ArtifactStore(artifact_dir)._path(record["artifact_id"]),
                      "r+", encoding="utf-8") as stream:
                value = json.load(stream)
                value["path"] = "changed.md"
                stream.seek(0); json.dump(value, stream); stream.truncate()
            self.assertFalse(artifact_integrity_check(
                record["artifact_id"], artifact_dir, evidence_dir, directory
            ))

    def test_final_answer_unsatisfied_completed_and_no_contract_compatibility(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = AuditWriter(SESSION, RUN, directory)
            result = run_agent(
                "create report", Provider([{"type": "final_answer", "final_answer": "done"}]),
                audit_writer=writer,
                output_contract={"required_artifacts": [requirement()]},
            )
            self.assertEqual(result, "incomplete: output contract unsatisfied")
            state = [item for item in read_events(RUN, directory)
                     if item["event_type"] == "run_state_changed"][-1]
            self.assertEqual(state["outcome"], "incomplete")
            self.assertEqual(state["reason"], "output contract unsatisfied")

        with tempfile.TemporaryDirectory() as directory:
            writer = AuditWriter(SESSION, "7" * 32, directory)
            result = run_agent(
                "reactive", Provider([{"type": "final_answer", "final_answer": "old"}]),
                audit_writer=writer,
            )
            self.assertEqual(result, "old")
            state = [item for item in read_events("7" * 32, directory)
                     if item["event_type"] == "run_state_changed"][-1]
            self.assertEqual(state["outcome"], "completed")

    def test_real_write_verification_accepts_artifact_and_completes_run(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = os.path.join(directory, "workspace")
            audit_dir = os.path.join(directory, "audit")
            os.mkdir(workspace)
            writer = AuditWriter(SESSION, RUN, audit_dir)
            decisions = [
                {"type": "tool_call", "tool": "shell", "command": "echo hello > report.md"},
                {"type": "tool_call", "tool": "shell", "command": "cat report.md"},
                {"type": "final_answer", "final_answer": "done"},
            ]
            previous = os.getcwd()
            try:
                os.chdir(workspace)
                with patch("mini_harness_core.agent.request_approval", return_value=True):
                    result = run_agent(
                        "create report", Provider(decisions), max_steps=3,
                        audit_writer=writer,
                        output_contract={"required_artifacts": [requirement()]},
                    )
            finally:
                os.chdir(previous)
            self.assertEqual(result, "done")
            artifacts = ArtifactStore(os.path.join(audit_dir, "artifacts")).list_run(RUN)
            self.assertEqual(len(artifacts), 1)
            self.assertEqual(artifacts[0]["status"], "accepted")
            events = read_events(RUN, audit_dir)
            lifecycle = {item["event_type"] for item in events}
            self.assertTrue({
                "artifact_proposed", "artifact_materialized", "artifact_verified",
                "artifact_accepted",
            }.issubset(lifecycle))
            self.assertEqual([item["outcome"] for item in events
                              if item["event_type"] == "run_state_changed"][-1],
                             "completed")
            envelope = RunEnvelopeStore(os.path.join(
                audit_dir, "envelopes"
            )).load(RUN)
            replay = harness_replay_check(envelope, audit_dir)
            artifact_replays = [item for item in replay["transitions"]
                                if item["transition_type"] == "artifact_contract"]
            self.assertEqual([item["status"] for item in artifact_replays], ["MATCH"])

    def test_empty_artifact_is_rejected_but_not_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = os.path.join(directory, "workspace")
            audit_dir = os.path.join(directory, "audit")
            os.mkdir(workspace)
            writer = AuditWriter(SESSION, RUN, audit_dir)
            decisions = [
                {"type": "tool_call", "tool": "shell", "command": "touch report.md"},
                {"type": "tool_call", "tool": "shell", "command": "cat report.md"},
                {"type": "final_answer", "final_answer": "done"},
            ]
            previous = os.getcwd()
            try:
                os.chdir(workspace)
                with patch("mini_harness_core.agent.request_approval", return_value=True):
                    result = run_agent(
                        "create report", Provider(decisions), max_steps=3,
                        audit_writer=writer,
                        output_contract={"required_artifacts": [requirement()]},
                    )
                self.assertTrue(os.path.exists(PATH))
            finally:
                os.chdir(previous)
            self.assertEqual(result, "incomplete: output contract unsatisfied")
            record = ArtifactStore(os.path.join(audit_dir, "artifacts")).list_run(RUN)[0]
            self.assertEqual(record["status"], "rejected")
            self.assertIn("non_empty", record["references"]["contract_result"]["unsatisfied_requirements"])

    def test_second_write_creates_new_version_and_supersession_event(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = os.path.join(directory, "workspace")
            audit_dir = os.path.join(directory, "audit")
            os.mkdir(workspace)
            writer = AuditWriter(SESSION, RUN, audit_dir)
            decisions = [
                {"type": "tool_call", "tool": "shell", "command": "echo v1 > report.md"},
                {"type": "tool_call", "tool": "shell", "command": "cat report.md"},
                {"type": "tool_call", "tool": "shell", "command": "echo v2 > report.md"},
                {"type": "tool_call", "tool": "shell", "command": "cat report.md"},
                {"type": "final_answer", "final_answer": "done"},
            ]
            previous = os.getcwd()
            try:
                os.chdir(workspace)
                with patch("mini_harness_core.agent.request_approval", return_value=True):
                    self.assertEqual(run_agent(
                        "revise report", Provider(decisions), max_steps=5,
                        audit_writer=writer,
                        output_contract={"required_artifacts": [requirement()]},
                    ), "done")
            finally:
                os.chdir(previous)
            records = ArtifactStore(os.path.join(audit_dir, "artifacts")).list_run(RUN)
            self.assertEqual(len(records), 2)
            self.assertNotEqual(records[0]["content_identity"], records[1]["content_identity"])
            self.assertEqual(records[1]["supersedes_artifact_id"], records[0]["artifact_id"])
            self.assertEqual(current_artifacts(records), [records[1]])
            superseded = [event for event in read_events(RUN, audit_dir)
                          if event["event_type"] == "artifact_superseded"]
            self.assertEqual(superseded[0]["references"]["superseded_artifact_id"],
                             records[0]["artifact_id"])

    def test_cross_run_agent_supersedes_integral_old_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = os.path.join(directory, "workspace")
            audit_dir = os.path.join(directory, "audit")
            os.mkdir(workspace)
            run_ids = ("8" * 32, "9" * 32)
            commands = ("echo v1 > report.md", "echo v2 > report.md")
            previous_cwd = os.getcwd()
            try:
                os.chdir(workspace)
                with patch("mini_harness_core.agent.request_approval", return_value=True):
                    for run_id, command in zip(run_ids, commands):
                        result = run_agent(
                            "write", Provider([
                                {"type": "tool_call", "tool": "shell", "command": command},
                                {"type": "tool_call", "tool": "shell", "command": "cat report.md"},
                                {"type": "final_answer", "final_answer": "done"},
                            ]), max_steps=3,
                            audit_writer=AuditWriter(SESSION, run_id, audit_dir),
                            output_contract={"required_artifacts": [requirement()]},
                        )
                        self.assertEqual(result, "done")
            finally:
                os.chdir(previous_cwd)
            store = ArtifactStore(os.path.join(audit_dir, "artifacts"))
            first, second = (store.list_run(run_id)[0] for run_id in run_ids)
            self.assertEqual(second["supersedes_artifact_id"], first["artifact_id"])
            self.assertTrue(artifact_integrity_check(
                first["artifact_id"], store.directory,
                os.path.join(audit_dir, "evidence"), audit_dir,
            ))
            self.assertTrue(artifact_integrity_check(
                second["artifact_id"], store.directory,
                os.path.join(audit_dir, "evidence"), audit_dir,
            ))

    def test_plan_artifact_gate_requires_main_accepted_step_output(self):
        plan = start_step(create_plan("goal", [
            {"id": "step-1", "description": "write report", "depends_on": []},
        ]))
        with tempfile.TemporaryDirectory() as directory:
            evidence_store = EvidenceStore(os.path.join(directory, "evidence"))
            artifact_store = ArtifactStore(os.path.join(directory, "artifacts"))
            evidence = evidence_store.save(create_verification_evidence(
                RUN,
                {"kind": "plan_step", "target": "step-1",
                 "claim": "current_reality_verified"},
                {"target_type": "file", "path": PATH}, ACTION,
                {"exit_code": 0, "stdout": "hello\n", "stderr": ""},
                EVENT, True,
                artifact=artifact_ref(PATH, IDENTITY["sha256"], IDENTITY["size"]),
            ))
            accepted = artifact_store.save(create_artifact(
                RUN, PATH, "accepted", IDENTITY, producer(step_id="step-1"),
                [evidence["evidence_id"]], requirement(step_id="step-1"),
            ))
            completed = complete_step(
                plan, "step-1", [evidence["evidence_id"]], evidence_store, RUN,
                output_artifact_ids=[accepted["artifact_id"]],
                artifact_store=artifact_store,
            )
            self.assertEqual(completed["steps"][0]["output_artifact_ids"],
                             [accepted["artifact_id"]])
            rejected = artifact_store.save(create_artifact(
                RUN, "bad.md", "rejected", IDENTITY,
                producer(step_id="step-1"),
            ))
            with self.assertRaisesRegex(ValueError, "Artifact Gate"):
                complete_step(
                    plan, "step-1", [evidence["evidence_id"]], evidence_store, RUN,
                    output_artifact_ids=[rejected["artifact_id"]],
                    artifact_store=artifact_store,
                )


class ArtifactCLITests(unittest.TestCase):
    def test_show_trace_check_and_outputs(self):
        from mini_harness_core import cli

        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = os.path.join(directory, "artifacts")
            evidence_dir = os.path.join(directory, "evidence")
            contract_dir = os.path.join(directory, "contracts")
            writer = AuditWriter(SESSION, RUN, directory)
            evidence = verification()
            EvidenceStore(evidence_dir).save(evidence)
            contract = OutputContractStore(contract_dir).save(create_output_contract(
                RUN, {"required_artifacts": [requirement()]}
            ))
            record = ArtifactStore(artifact_dir).save(create_artifact(
                RUN, PATH, "accepted", IDENTITY, producer(),
                [evidence["evidence_id"]], contract["required_artifacts"][0],
            ))
            writer.append("artifact_accepted", "harness", "artifact", "accepted",
                          references={"artifact_id": record["artifact_id"],
                                      "artifact_fingerprint": record["artifact_fingerprint"],
                                      "path": record["path"],
                                      "status": record["status"],
                                      "evidence_ids": record["evidence_ids"]})

            common = {
                "ARTIFACT_DIR": artifact_dir, "EVIDENCE_DIR": evidence_dir,
                "OUTPUT_CONTRACT_DIR": contract_dir, "AUDIT_DIR": directory,
            }
            commands = (
                (["mini_harness.py", "--artifact-show", record["artifact_id"]], "Content identity"),
                (["mini_harness.py", "--artifact-trace", record["artifact_id"]], "Artifact"),
                (["mini_harness.py", "--artifact-check", record["artifact_id"]], "MATCH"),
                (["mini_harness.py", "--outputs", RUN], "OUTPUTS SATISFIED"),
            )
            for argv, expected in commands:
                output = io.StringIO()
                with patch.multiple(cli, **common), patch.object(sys, "argv", argv), \
                        contextlib.redirect_stdout(output):
                    cli.main()
                self.assertIn(expected, output.getvalue())


if __name__ == "__main__":
    unittest.main()
