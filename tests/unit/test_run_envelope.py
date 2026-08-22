import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from mini_harness_core.context import RuntimeContextAssembler
from mini_harness_core.memory import MemoryStore
from mini_harness_core.policy_snapshot import (
    PolicyBinding, build_policy_snapshot, persist_snapshot,
)
from mini_harness_core.providers import FakeProvider
from mini_harness_core.run_envelope import (
    RunEnvelopeError, RunEnvelopeStore, build_envelope,
    envelope_fingerprint, envelope_integrity_check, harness_replay_check,
)
from mini_harness_core.run_manifest import (
    RunManifestStore, build_configuration, build_manifest,
)
from mini_harness_core.verification import (
    replay_verification_transition, verification_observation_identity,
)


class RunEnvelopeV21Tests(unittest.TestCase):
    def fixture(self, directory):
        snapshot = build_policy_snapshot(mcp_mappings={})
        fingerprint = persist_snapshot(snapshot, os.path.join(directory, "policies"))
        binding = PolicyBinding(snapshot, fingerprint)
        project = os.path.join(directory, "project")
        os.makedirs(project)
        assembler = RuntimeContextAssembler(
            project, MemoryStore(os.path.join(project, ".memory", "memories.json"))
        )
        configuration = build_configuration(
            "中文任务", FakeProvider(), binding, assembler, 1000
        )
        manifest = build_manifest("1" * 32, "2" * 32, configuration, "now")
        RunManifestStore(os.path.join(directory, "manifests")).persist(manifest)
        envelope = build_envelope(
            manifest["run_id"], manifest["session_id"], "中文任务",
            [{"role": "user", "content": "earlier"}], manifest,
            current_plan=None, control_state={"state": "running"}, created_at="now",
        )
        return envelope

    def test_fingerprint_covers_only_initial_inputs_and_task_is_digest_only(self):
        with tempfile.TemporaryDirectory() as directory:
            envelope = self.fixture(directory)
            before = envelope["envelope_fingerprint"]
            self.assertNotIn("中文任务", json.dumps(envelope, ensure_ascii=False))
            self.assertEqual(envelope["inputs"]["task"]["task_length"], 12)
            changed_instance = copy.deepcopy(envelope)
            changed_instance.update({"run_id": "3" * 32, "session_id": "4" * 32,
                                     "created_at": "later"})
            changed_instance["inputs"]["task"]["task_message_ref"]["session_id"] = "4" * 32
            self.assertEqual(before, envelope_fingerprint(envelope["inputs"]))
            envelope["requests"].append({"not": "hashed"})
            self.assertEqual(before, envelope_fingerprint(envelope["inputs"]))

    def test_atomic_records_identity_and_replay_without_external_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            envelope = self.fixture(directory)
            store = RunEnvelopeStore(os.path.join(directory, "envelopes"))
            store.persist(envelope)
            request = store.append_request(
                envelope["run_id"], [{"role": "user", "content": "secret text"}]
            )
            decision = {"type": "final_answer", "final_answer": "nondeterministic"}
            store.bind_decision(envelope["run_id"], request["request_id"], decision, "e" * 32)
            store.append_transition(envelope["run_id"], "retry", {
                "failure_class": "transient", "effect": "read_only",
                "replay_policy": "safe_to_retry", "attempt_count": 1,
                "max_attempts": 3, "run_state": "running",
                "reconciliation_status": "not_required",
                "historical_recorded_observation": True,
            }, {"decision": "retry_with_backoff", "next_delay": 1.0})
            loaded = store.load(envelope["run_id"])
            serialized = json.dumps(loaded)
            self.assertNotIn("secret text", serialized)
            self.assertNotIn("nondeterministic", serialized)
            self.assertTrue(envelope_integrity_check(loaded, directory))
            replay = harness_replay_check(loaded, directory)
            self.assertTrue(replay["match"])
            self.assertEqual(replay["transitions"][0]["status"], "MATCH")

    def test_initial_inputs_cannot_change_and_unknown_evidence_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            envelope = self.fixture(directory)
            store = RunEnvelopeStore(os.path.join(directory, "envelopes"))
            store.persist(envelope)
            corrupt = store.load(envelope["run_id"])
            corrupt["inputs"]["task"]["task_length"] += 1
            with self.assertRaisesRegex(RunEnvelopeError, "fingerprint mismatch"):
                store.persist(corrupt)
            store.append_transition(
                envelope["run_id"], "verification",
                {"historical_recorded_observation": True}, {"accepted": True},
            )
            replay = harness_replay_check(store.load(envelope["run_id"]), directory)
            self.assertEqual(replay["transitions"][0]["status"], "UNAVAILABLE")
            self.assertFalse(replay["match"])

    def test_forbidden_raw_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            envelope = self.fixture(directory)
            envelope["inputs"]["task"]["task_text"] = "do not store"
            envelope["envelope_fingerprint"] = envelope_fingerprint(envelope["inputs"])
            with self.assertRaisesRegex(RunEnvelopeError, "task identity|forbidden"):
                RunEnvelopeStore(os.path.join(directory, "envelopes")).persist(envelope)

    def verification_input(self, observation, related=True):
        return {
            "requires_verification": True,
            "verification_target": {"target_type": "file", "path": "proof.txt"},
            "action_effect": "read_only", "evidence_related": related,
            "historical_recorded_observation": True,
            "observation": verification_observation_identity(
                observation, "a" * 32
            ),
        }

    def test_successful_and_rejected_historical_verification_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            envelope = self.fixture(directory)
            store = RunEnvelopeStore(os.path.join(directory, "envelopes"))
            store.persist(envelope)
            accepted = self.verification_input({
                "exit_code": 0, "stdout": "historical contents\n", "stderr": "",
            })
            rejected = self.verification_input({
                "exit_code": 126, "stdout": "",
                "stderr": "verification evidence is unrelated",
                "denied_by": "verification_quality",
            }, related=False)
            store.append_transition(
                envelope["run_id"], "verification", accepted,
                replay_verification_transition(accepted),
            )
            store.append_transition(
                envelope["run_id"], "verification", rejected,
                replay_verification_transition(rejected),
            )
            results = harness_replay_check(
                store.load(envelope["run_id"]), directory
            )["transitions"]
            self.assertEqual([item["status"] for item in results], ["MATCH", "MATCH"])
            self.assertTrue(replay_verification_transition(accepted)["accepted"])
            self.assertFalse(replay_verification_transition(rejected)["accepted"])

    def test_corruption_mismatches_and_replay_ignores_current_reality(self):
        with tempfile.TemporaryDirectory() as directory:
            envelope = self.fixture(directory)
            store = RunEnvelopeStore(os.path.join(directory, "envelopes"))
            store.persist(envelope)
            historical = self.verification_input({
                "exit_code": 0, "stdout": "before\n", "stderr": "",
            })
            store.append_transition(
                envelope["run_id"], "verification", historical,
                replay_verification_transition(historical),
            )
            path = os.path.join(directory, "proof.txt")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("different current reality")
            original_cwd = os.getcwd()
            try:
                os.chdir(directory)
                with patch("mini_harness_core.agent.execute_shell") as shell, \
                     patch("mini_harness_core.agent.request_approval") as approval, \
                     patch("mini_harness_core.providers.RealProvider.complete") as model:
                    replay = harness_replay_check(
                        store.load(envelope["run_id"]), directory
                    )
                shell.assert_not_called()
                approval.assert_not_called()
                model.assert_not_called()
            finally:
                os.chdir(original_cwd)
            self.assertTrue(replay["match"])
            corrupt = store.load(envelope["run_id"])
            corrupt["transitions"][0]["input"]["observation"]["stdout_length"] += 1
            mismatch = harness_replay_check(corrupt, directory)
            self.assertEqual(mismatch["transitions"][0]["status"], "MISMATCH")

    def test_verification_transition_contains_no_raw_observation(self):
        inputs = self.verification_input({
            "exit_code": 0, "stdout": "DO-NOT-PERSIST-RAW", "stderr": "RAW-ERROR",
        })
        serialized = json.dumps(inputs)
        self.assertNotIn("DO-NOT-PERSIST-RAW", serialized)
        self.assertNotIn("RAW-ERROR", serialized)
        incomplete = {
            "transition_id": "old", "sequence": 1,
            "transition_type": "verification",
            "input": {"historical_recorded_observation": True},
            "recorded_output": {"accepted": True},
        }
        with tempfile.TemporaryDirectory() as directory:
            envelope = self.fixture(directory)
            envelope["transitions"] = [incomplete]
            self.assertEqual(
                harness_replay_check(envelope, directory)["transitions"][0]["status"],
                "UNAVAILABLE",
            )


if __name__ == "__main__":
    unittest.main()
