import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from mini_harness_core.artifacts import (
    ArtifactStore, OutputContractStore, artifact_trace, create_artifact,
    create_output_contract, create_producer,
)
from mini_harness_core.audit import AuditWriter
from mini_harness_core.cli import main
from mini_harness_core.context import RuntimeContextAssembler
from mini_harness_core.evidence import (
    EvidenceStore, create_subagent_return_evidence,
    create_verification_evidence, evidence_trace,
)
from mini_harness_core.memory import MemoryStore
from mini_harness_core.policy_snapshot import (
    PolicyBinding, build_policy_snapshot, persist_snapshot,
)
from mini_harness_core.providers import FakeProvider
from mini_harness_core.result import (
    ResultStore, bind_final_result, normalize_final_candidate,
    result_integrity_check,
)
from mini_harness_core.run_bundle import (
    BundleHistoricalResolver, LocalHistoricalResolver, RunBundleError,
    bundle_fingerprint, check_bundle, export_run_bundle, replay_bundle,
    show_bundle,
)
from mini_harness_core.run_envelope import (
    RunEnvelopeStore, build_envelope,
)
from mini_harness_core.run_manifest import (
    RunManifestStore, build_configuration, build_manifest,
)


RUN = "1" * 32
OTHER_RUN = "2" * 32
SESSION = "3" * 32


class RunBundleV25Tests(unittest.TestCase):
    def base_history(self, audit_directory, run_id=RUN):
        snapshot = build_policy_snapshot(mcp_mappings={})
        fingerprint = persist_snapshot(
            snapshot, os.path.join(audit_directory, "policies")
        )
        binding = PolicyBinding(snapshot, fingerprint)
        project = os.path.join(audit_directory, "project")
        os.makedirs(project, exist_ok=True)
        memory = MemoryStore(os.path.join(project, ".memory", "memories.json"))
        assembler = RuntimeContextAssembler(project, memory)
        configuration = build_configuration(
            "bundle task", FakeProvider(), binding, assembler, 1000
        )
        manifest = build_manifest(
            run_id, SESSION, configuration, created_at="2026-01-01T00:00:00Z"
        )
        RunManifestStore(os.path.join(
            audit_directory, "manifests"
        )).persist(manifest)
        envelope = build_envelope(
            run_id, SESSION, "bundle task", [], manifest,
            control_state={"state": "running"},
            created_at="2026-01-01T00:00:00Z",
        )
        RunEnvelopeStore(os.path.join(
            audit_directory, "envelopes"
        )).persist(envelope)
        writer = AuditWriter(SESSION, run_id, audit_directory)
        writer.append(
            "run_started", "harness", "run", "running",
            references={
                "policy_schema_version": 1,
                "policy_revision": snapshot["policy_revision"],
                "policy_fingerprint": fingerprint,
                "manifest_fingerprint": manifest["configuration_fingerprint"],
                "envelope_fingerprint": envelope["envelope_fingerprint"],
            },
        )
        return writer, manifest, envelope

    def result_history(self, audit_directory, status="completed",
                       with_artifact=False, with_output_contract=False):
        writer, manifest, _envelope = self.base_history(audit_directory)
        artifacts = []
        evidences = []
        old_artifact = None
        if with_artifact:
            old_writer = AuditWriter(SESSION, OTHER_RUN, audit_directory)
            old_artifact = create_artifact(
                OTHER_RUN, "report.txt", "accepted",
                {"sha256": "a" * 64, "size": 1},
                create_producer(
                    OTHER_RUN, action_id="old-action", capability="shell"
                ),
            )
            ArtifactStore(os.path.join(
                audit_directory, "artifacts"
            )).save(old_artifact)
            old_writer.append(
                "artifact_accepted", "harness", "artifact", "accepted",
                references={
                    "artifact_id": old_artifact["artifact_id"],
                    "artifact_fingerprint": old_artifact["artifact_fingerprint"],
                    "path": old_artifact["path"], "status": "accepted",
                    "evidence_ids": [],
                },
            )
            action = writer.append(
                "action_state_changed", "harness", "action", "completed",
                references={"action_id": "action-1",
                            "verification_action_id": "verify-1"},
            )
            observation = writer.append(
                "tool_observed", "tool", "shell", "observed",
                references={"action_id": "action-1"},
            )
            evidence = create_verification_evidence(
                RUN,
                {"kind": "artifact", "target": "report.txt",
                 "claim": "content verified"},
                {"target_type": "file", "path": "report.txt"},
                "verify-1",
                {"exit_code": 0, "stdout": "ok", "stderr": ""},
                observation["event_id"], True,
                source_action_id="action-1",
            )
            EvidenceStore(os.path.join(
                audit_directory, "evidence"
            )).save(evidence)
            writer.append(
                "evidence_created", "harness", "evidence", "created",
                references={
                    "evidence_id": evidence["evidence_id"],
                    "evidence_fingerprint": evidence["evidence_fingerprint"],
                    "action_event_id": action["event_id"],
                },
            )
            artifact = create_artifact(
                RUN, "report.txt", "accepted",
                {"sha256": "b" * 64, "size": 2},
                create_producer(
                    RUN, action_id="action-1", capability="shell"
                ), evidence_ids=[evidence["evidence_id"]],
                supersedes_artifact_id=old_artifact["artifact_id"],
            )
            ArtifactStore(os.path.join(
                audit_directory, "artifacts"
            )).save(artifact)
            writer.append(
                "artifact_accepted", "harness", "artifact", "accepted",
                references={
                    "artifact_id": artifact["artifact_id"],
                    "artifact_fingerprint": artifact["artifact_fingerprint"],
                    "path": artifact["path"], "status": "accepted",
                    "evidence_ids": artifact["evidence_ids"],
                },
            )
            artifacts = [{
                "artifact_id": artifact["artifact_id"],
                "artifact_fingerprint": artifact["artifact_fingerprint"],
            }]
            evidences = [{
                "evidence_id": evidence["evidence_id"],
                "evidence_fingerprint": evidence["evidence_fingerprint"],
            }]
        artifact_ids = [item["artifact_id"] for item in artifacts]
        evidence_ids = [item["evidence_id"] for item in evidences]
        if status == "incomplete":
            writer.append(
                "plan_created", "harness", "plan", "active",
                references={"plan_id": "plan-1", "plan_version": 1},
            )
        normalized = normalize_final_candidate({
            "type": "final_answer", "answer": "done",
            "claimed_status": status,
            "artifact_refs": artifact_ids, "evidence_refs": evidence_ids,
        })
        candidate = normalized["metadata"]
        writer.append(
            "final_candidate_received", "model", "final_answer", "received",
            references={
                "answer_length": candidate["answer_length"],
                "answer_sha256": candidate["answer_sha256"],
                "claimed_status": candidate["claimed_status"],
                "artifact_ids": artifact_ids, "evidence_ids": evidence_ids,
            },
        )
        output_identity = None
        if with_output_contract:
            if not with_artifact:
                raise ValueError("output contract fixture requires artifact")
            contract = create_output_contract(RUN, {"required_artifacts": [{
                "name": "report", "artifact_type": "workspace_file",
                "path": "report.txt", "requirements": ["exists"],
            }]}, created_at="2026-01-01T00:00:00Z")
            OutputContractStore(os.path.join(
                audit_directory, "output_contracts",
            )).save(contract)
            output_identity = {
                "satisfied": True,
                "contract_fingerprint": contract["contract_fingerprint"],
                "accepted_artifact_ids": artifact_ids,
            }
        binding_input = {
            "run_id": RUN,
            "run_control": {"state": "running", "reason": None},
            "terminal_failure": None,
            "blocking_reason": (
                "historical block" if status == "blocked" else None
            ),
            "plan": ({
                "plan_id": "plan-1", "status": "active",
                "completed_step_ids": [],
            } if status == "incomplete" else None),
            "output_contract": output_identity,
            "verification_required": False,
            "accepted_artifacts": artifacts,
            "accepted_evidence": evidences,
            "candidate": candidate,
        }
        result, output = bind_final_result(binding_input, normalized)
        RunEnvelopeStore(os.path.join(
            audit_directory, "envelopes"
        )).append_transition(RUN, "result_binding", binding_input, output)
        ResultStore(os.path.join(
            audit_directory, "results"
        )).save(result)
        identity = {
            "answer_length": len(result["answer"].encode()),
            "answer_sha256": hashlib.sha256(result["answer"].encode()).hexdigest(),
        }
        if result["candidate"]["contradiction"]:
            writer.append(
                "final_candidate_rejected", "harness", "final_answer",
                "rejected", result["reason"], references={
                    "answer_length": candidate["answer_length"],
                    "answer_sha256": candidate["answer_sha256"],
                    "claimed_status": candidate["claimed_status"],
                    "authoritative_status": result["status"],
                    "artifact_ids": artifact_ids, "evidence_ids": evidence_ids,
                    "contradiction": True,
                },
            )
        writer.append(
            "final_result_emitted", "harness", "result", result["status"],
            result["reason"], references={
                **identity, "claimed_status": candidate["claimed_status"],
                "authoritative_status": result["status"],
                "artifact_ids": result["artifact_ids"],
                "evidence_ids": result["evidence_ids"],
                "contradiction": result["candidate"]["contradiction"],
                "result_fingerprint": result["result_fingerprint"],
            },
        )
        writer.append(
            "run_state_changed", "harness", "run", result["status"],
            result["reason"],
        )
        return result, old_artifact

    def export(self, audit_directory, created_at="first"):
        bundles = os.path.join(os.path.dirname(audit_directory), "bundles")
        return export_run_bundle(
            RUN, audit_directory, bundles, created_at=created_at
        )

    def test_result_export_is_deterministic_and_fully_offline(self):
        with tempfile.TemporaryDirectory() as root:
            audit = os.path.join(root, "audit")
            self.result_history(audit)
            path, first, reused = self.export(audit, "first")
            self.assertFalse(reused)
            same_path, second, reused = self.export(audit, "different")
            self.assertTrue(reused)
            self.assertEqual(path, same_path)
            self.assertEqual(first["bundle_fingerprint"],
                             second["bundle_fingerprint"])
            changed = copy.deepcopy(first)
            changed["created_at"] = "anything"
            self.assertEqual(bundle_fingerprint(first),
                             bundle_fingerprint(changed))
            self.assertTrue(check_bundle(path)["match"])
            summary = show_bundle(path)
            self.assertEqual(summary["status"], "result")
            self.assertEqual(summary["result_status"], "completed")
            os.rename(audit, audit + "-offline")
            with open(os.path.join(root, "report.txt"), "w", encoding="utf-8") as stream:
                stream.write("different current workspace reality")
            previous_cwd = os.getcwd()
            try:
                os.chdir(root)
                with patch("mini_harness_core.agent.execute_shell") as tool, \
                     patch("mini_harness_core.agent.request_approval") as approval, \
                     patch("mini_harness_core.providers.RealProvider.complete") as llm:
                    replay = replay_bundle(path)
            finally:
                os.chdir(previous_cwd)
            self.assertEqual(replay["status"], "MATCH")
            tool.assert_not_called()
            approval.assert_not_called()
            llm.assert_not_called()

    def test_blocked_result_and_forensic_no_result_export(self):
        with tempfile.TemporaryDirectory() as root:
            audit = os.path.join(root, "audit")
            result, _old = self.result_history(audit, status="blocked")
            path, manifest, _reused = self.export(audit)
            self.assertEqual(manifest["bundle_status"], "result")
            self.assertEqual(show_bundle(path)["result_status"], "blocked")
            self.assertEqual(result["status"], "blocked")
        with tempfile.TemporaryDirectory() as root:
            audit = os.path.join(root, "audit")
            result, _old = self.result_history(audit, status="incomplete")
            path, manifest, _reused = self.export(audit)
            self.assertEqual(manifest["bundle_status"], "result")
            self.assertEqual(show_bundle(path)["result_status"], "incomplete")
            self.assertEqual(result["status"], "incomplete")
        with tempfile.TemporaryDirectory() as root:
            audit = os.path.join(root, "audit")
            self.base_history(audit)
            path, manifest, _reused = self.export(audit)
            self.assertEqual(manifest["bundle_status"], "forensic")
            self.assertEqual(manifest["root"], {"type": "run", "id": RUN})
            self.assertEqual(show_bundle(path)["result_status"], "absent")
            self.assertTrue(check_bundle(path)["match"])

    def test_reference_closure_and_cross_run_artifact_are_minimal(self):
        with tempfile.TemporaryDirectory() as root:
            audit = os.path.join(root, "audit")
            result, old = self.result_history(audit, with_artifact=True)
            unrelated = create_artifact(
                RUN, "unrelated.txt", "materialized",
                {"sha256": "c" * 64, "size": 3},
                create_producer(
                    RUN, action_id="unrelated", capability="shell"
                ),
            )
            ArtifactStore(os.path.join(audit, "artifacts")).save(unrelated)
            path, manifest, _reused = self.export(audit)
            artifacts = [item for item in manifest["objects"]
                         if item["object_type"] == "artifact"]
            ids = {item["logical_id"] for item in artifacts}
            self.assertEqual(ids, {result["artifact_ids"][0], old["artifact_id"]})
            self.assertNotIn(unrelated["artifact_id"], ids)
            old_index = next(item for item in artifacts
                             if item["logical_id"] == old["artifact_id"])
            self.assertTrue(old_index["vendored_cross_run"])
            self.assertEqual(old_index["source_run_id"], OTHER_RUN)
            self.assertNotIn(("audit", OTHER_RUN), {
                (item["object_type"], item["logical_id"])
                for item in manifest["objects"]
            })
            self.assertTrue(check_bundle(path)["match"])
            resolver = BundleHistoricalResolver(path)
            artifact = resolver.load("artifact", result["artifact_ids"][0])
            evidence = resolver.load("evidence", result["evidence_ids"][0])
            self.assertIn("Observation Event:", "\n".join(evidence_trace(
                evidence, resolver=resolver
            )))
            self.assertIn("Evidence", "\n".join(artifact_trace(
                artifact, resolver=resolver
            )))
            self.assertTrue(result_integrity_check(
                RUN, os.path.join(audit, "results"),
                os.path.join(audit, "artifacts"),
                os.path.join(audit, "evidence"), audit,
            ))

    def test_result_bundle_requires_output_contract_but_not_child_run(self):
        with tempfile.TemporaryDirectory() as root:
            audit = os.path.join(root, "audit")
            self.result_history(
                audit, with_artifact=True, with_output_contract=True,
            )
            path, manifest, _reused = self.export(audit)
            keys = {(item["object_type"], item["logical_id"])
                    for item in manifest["objects"]}
            self.assertIn(("output_contract", RUN), keys)
            self.assertNotIn(("audit", OTHER_RUN), keys)
            self.assertTrue(check_bundle(path)["match"])
            item = next(item for item in manifest["objects"]
                        if item["object_type"] == "output_contract")
            os.unlink(os.path.join(path, *item["path"].split("/")))
            checked = check_bundle(path)
            self.assertFalse(checked["match"])

    def test_forensic_bundle_allows_optional_trace_unavailable(self):
        with tempfile.TemporaryDirectory() as root:
            audit = os.path.join(root, "audit")
            self.base_history(audit)
            os.unlink(os.path.join(audit, RUN + ".jsonl"))
            path, manifest, _reused = self.export(audit)
            self.assertEqual(manifest["bundle_status"], "forensic")
            checked = check_bundle(path)
            self.assertTrue(checked["match"])
            self.assertEqual(checked["closure_status"], "PARTIAL")
            self.assertEqual(show_bundle(path)["trace_status"], "unavailable")

    def test_subagent_return_vendors_minimal_reference_not_child_run(self):
        with tempfile.TemporaryDirectory() as root:
            audit = os.path.join(root, "audit")
            writer, _manifest, _envelope = self.base_history(audit)
            child_run_id = "4" * 32
            handoff_id = "handoff-minimal"
            writer.append(
                "subagent_return", "subagent", "subagent", "completed",
                references={"handoff_id": handoff_id,
                            "subagent_run_id": child_run_id},
            )
            evidence = create_subagent_return_evidence(
                RUN,
                {"kind": "subagent", "target": handoff_id,
                 "claim": "candidate_returned"},
                handoff_id, child_run_id, "completed",
                return_reference={"status": "completed", "length": 0},
            )
            EvidenceStore(os.path.join(audit, "evidence")).save(evidence)
            writer.append(
                "evidence_created", "harness", "evidence", "created",
                references={"evidence_id": evidence["evidence_id"],
                            "evidence_fingerprint": evidence["evidence_fingerprint"]},
            )
            path, manifest, _reused = self.export(audit)
            keys = {(item["object_type"], item["logical_id"])
                    for item in manifest["objects"]}
            self.assertIn(("evidence", evidence["evidence_id"]), keys)
            self.assertNotIn(("audit", child_run_id), keys)
            self.assertNotIn(("envelope", child_run_id), keys)
            self.assertTrue(check_bundle(path)["match"])

    def test_tampering_missing_extra_index_and_hash_are_detected(self):
        with tempfile.TemporaryDirectory() as root:
            audit = os.path.join(root, "audit")
            self.result_history(audit, with_artifact=True)
            original, manifest, _reused = self.export(audit)

            def copied(name):
                target = os.path.join(root, name)
                shutil.copytree(original, target)
                return target

            by_type = {item["object_type"]: item for item in manifest["objects"]}
            for object_type in ("evidence", "artifact", "result", "audit"):
                target = copied("mutated-" + object_type)
                item = by_type[object_type]
                path = os.path.join(target, *item["path"].split("/"))
                with open(path, "ab") as stream:
                    stream.write(b" ")
                self.assertFalse(check_bundle(target)["match"])
            target = copied("missing")
            os.unlink(os.path.join(
                target, *by_type["evidence"]["path"].split("/")
            ))
            self.assertFalse(check_bundle(target)["match"])
            target = copied("extra")
            with open(os.path.join(target, "objects", "extra.json"),
                      "w", encoding="utf-8") as stream:
                stream.write("{}")
            self.assertFalse(check_bundle(target)["match"])
            target = copied("index")
            bundle_json = os.path.join(target, "bundle.json")
            with open(bundle_json, encoding="utf-8") as stream:
                document = json.load(stream)
            document["objects"].reverse()
            with open(bundle_json, "w", encoding="utf-8") as stream:
                json.dump(document, stream)
            self.assertFalse(check_bundle(target)["match"])
            target = copied("bad-hash")
            bundle_json = os.path.join(target, "bundle.json")
            with open(bundle_json, encoding="utf-8") as stream:
                document = json.load(stream)
            document["objects"][0]["sha256"] = "0" * 64
            document["bundle_fingerprint"] = bundle_fingerprint(document)
            with open(bundle_json, "w", encoding="utf-8") as stream:
                json.dump(document, stream)
            self.assertFalse(check_bundle(target)["match"])

    def test_export_rejects_forbidden_history_without_sanitizing(self):
        with tempfile.TemporaryDirectory() as root:
            audit = os.path.join(root, "audit")
            self.base_history(audit)
            audit_path = os.path.join(audit, RUN + ".jsonl")
            with open(audit_path, encoding="utf-8") as stream:
                event = json.loads(stream.readline())
            event["summary"] = "raw command payload"
            original = json.dumps(event, separators=(",", ":")) + "\n"
            with open(audit_path, "w", encoding="utf-8") as stream:
                stream.write(original)
            with self.assertRaisesRegex(RunBundleError, "offending object"):
                self.export(audit)
            with open(audit_path, encoding="utf-8") as stream:
                self.assertEqual(stream.read(), original)

    def test_external_path_and_symlink_escape_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            audit = os.path.join(root, "audit")
            self.result_history(audit)
            original, manifest, _reused = self.export(audit)
            traversal = os.path.join(root, "traversal")
            shutil.copytree(original, traversal)
            bundle_json = os.path.join(traversal, "bundle.json")
            with open(bundle_json, encoding="utf-8") as stream:
                document = json.load(stream)
            document["objects"][0]["path"] = "../outside"
            document["bundle_fingerprint"] = bundle_fingerprint(document)
            with open(bundle_json, "w", encoding="utf-8") as stream:
                json.dump(document, stream)
            self.assertFalse(check_bundle(traversal)["match"])
            escaped = os.path.join(root, "escaped")
            shutil.copytree(original, escaped)
            item = manifest["objects"][0]
            object_path = os.path.join(escaped, *item["path"].split("/"))
            outside = os.path.join(root, "outside")
            with open(outside, "wb") as stream:
                stream.write(b"outside")
            os.unlink(object_path)
            os.symlink(outside, object_path)
            self.assertFalse(check_bundle(escaped)["match"])

    def test_bundle_resolver_never_falls_back_and_has_no_authority_api(self):
        with tempfile.TemporaryDirectory() as root:
            audit = os.path.join(root, "audit")
            self.result_history(audit)
            bundle, manifest, _reused = self.export(audit)
            local = LocalHistoricalResolver(audit)
            self.assertEqual(local.load("result", RUN)["status"], "completed")
            resolver = BundleHistoricalResolver(bundle)
            self.assertTrue(resolver.historical_read_only)
            for name in ("resume", "authorize", "activate_policy",
                         "execute", "approve", "mark_fresh"):
                self.assertFalse(hasattr(resolver, name))
            result_item = next(item for item in manifest["objects"]
                               if item["object_type"] == "result")
            os.unlink(os.path.join(
                bundle, *result_item["path"].split("/")
            ))
            with self.assertRaisesRegex(RunBundleError, "missing reference"):
                resolver.load("result", RUN)
            self.assertTrue(os.path.isfile(os.path.join(
                audit, "results", RUN + ".json"
            )))

    def test_cli_export_show_check_replay_and_unknown_inputs(self):
        with tempfile.TemporaryDirectory() as root:
            audit = os.path.join(root, "audit")
            self.result_history(audit)
            output = io.StringIO()
            with patch("mini_harness_core.cli.AUDIT_DIR", audit), \
                 patch("sys.argv", ["mini_harness.py", "--bundle-export", RUN]), \
                 contextlib.redirect_stdout(output):
                main()
            self.assertIn("Bundle fingerprint:", output.getvalue())
            bundle = os.path.join(audit, "bundles", RUN)
            for option, expected in (
                ("--bundle-show", "Run ID:"),
                ("--bundle-check", "BUNDLE CHECK MATCH"),
                ("--bundle-replay", "BUNDLE REPLAY MATCH"),
            ):
                output = io.StringIO()
                with patch("sys.argv", ["mini_harness.py", option, bundle]), \
                     contextlib.redirect_stdout(output):
                    main()
                self.assertIn(expected, output.getvalue())
            with patch("mini_harness_core.cli.AUDIT_DIR", audit), \
                 patch("sys.argv", ["mini_harness.py", "--bundle-export",
                                    "9" * 32]), \
                 contextlib.redirect_stdout(io.StringIO()), \
                 self.assertRaises(SystemExit):
                main()
            with patch("sys.argv", ["mini_harness.py", "--bundle-check",
                                    os.path.join(root, "unknown")]), \
                 contextlib.redirect_stdout(io.StringIO()), \
                 self.assertRaises(SystemExit):
                main()


if __name__ == "__main__":
    unittest.main()
