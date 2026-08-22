import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from mini_harness import (
    AuditWriter, create_handoff, read_events, run_agent, run_subagent,
)
from mini_harness_core import policy_composition as composition
from mini_harness_core.policy_composition import (
    ASK, DENY, SIDE_EFFECTING, WORKSPACE, StaticPolicyLayer,
)
from mini_harness_core.policy_snapshot import (
    PolicyBinding, PolicySnapshotError, authority_diff, binding_from_events,
    build_policy_snapshot, canonical_json, compose_from_snapshot,
    load_policy_snapshot, persist_snapshot, policy_drift,
    policy_fingerprint, replay_policy_events, neutral_delegated_summary,
)


class FinalProvider:
    SYSTEM_PROMPT = None

    def complete(self, messages):
        return {"type": "final_answer", "final_answer": "done"}


class ShellProvider:
    SYSTEM_PROMPT = None

    def __init__(self):
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        if self.calls == 1:
            return {"type": "tool_call", "command": "pwd"}
        return {"type": "final_answer", "final_answer": "done"}


class PolicySnapshotV19Tests(unittest.TestCase):
    def snapshot(self):
        return build_policy_snapshot(mcp_mappings={
            "mcp:demo:echo": {
                "zone": "external", "profile": "mcp-capability",
                "local_effect": "read_only", "policy": "ASK",
            }
        })

    def test_canonical_dict_order_and_same_policy_are_stable(self):
        first = {"b": {"y": 2, "x": 1}, "a": "中文"}
        second = {"a": "中文", "b": {"x": 1, "y": 2}}
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(policy_fingerprint(self.snapshot()),
                         policy_fingerprint(self.snapshot()))

    def test_authority_change_changes_fingerprint_but_agents_file_does_not(self):
        original = self.snapshot()
        changed = copy.deepcopy(original)
        changed["definitions"]["capability_profiles"]["workspace-editor"][
            "can_write_workspace"
        ] = False
        self.assertNotEqual(policy_fingerprint(original), policy_fingerprint(changed))
        with tempfile.TemporaryDirectory() as directory:
            agents = os.path.join(directory, "AGENTS.md")
            with open(agents, "w", encoding="utf-8") as stream:
                stream.write("first")
            before = policy_fingerprint(self.snapshot())
            with open(agents, "w", encoding="utf-8") as stream:
                stream.write("completely different project instructions")
            self.assertEqual(before, policy_fingerprint(self.snapshot()))

    def test_content_addressed_reuse_corruption_and_secret_rejection(self):
        snapshot = self.snapshot()
        with tempfile.TemporaryDirectory() as directory:
            fingerprint = persist_snapshot(snapshot, directory)
            path = os.path.join(directory, fingerprint + ".json")
            first_mtime = os.stat(path).st_mtime_ns
            self.assertEqual(persist_snapshot(snapshot, directory), fingerprint)
            self.assertEqual(os.stat(path).st_mtime_ns, first_mtime)
            self.assertEqual(load_policy_snapshot(fingerprint, directory), snapshot)
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({"corrupt": True}, stream)
            with self.assertRaisesRegex(PolicySnapshotError, "corruption"):
                load_policy_snapshot(fingerprint, directory)
        unsafe = self.snapshot()
        unsafe["policy_revision"] = "Authorization: Bearer hidden"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(PolicySnapshotError, "secret screening"):
                persist_snapshot(unsafe, directory)

    def test_unknown_schema_is_not_replayed_as_v1(self):
        snapshot = self.snapshot()
        snapshot["policy_schema_version"] = 2
        with self.assertRaisesRegex(PolicySnapshotError,
                                    "unsupported historical policy schema"):
            compose_from_snapshot(snapshot, {})

    def test_authority_diff_is_narrow_and_reports_requested_fields(self):
        old = self.snapshot()
        new = copy.deepcopy(old)
        new["definitions"]["capability_profiles"]["workspace-editor"][
            "can_write_workspace"
        ] = False
        new["definitions"]["trust_zones"][WORKSPACE]["policy"] = DENY
        new["definitions"]["mcp_capability_mappings"]["mcp:demo:echo"][
            "local_effect"
        ] = SIDE_EFFECTING
        paths = {item[0] for item in authority_diff(old, new)}
        self.assertIn("profile.workspace-editor.can_write_workspace", paths)
        self.assertIn("zone.workspace.policy", paths)
        self.assertIn("mcp.mcp:demo:echo.local_effect", paths)
        self.assertEqual(authority_diff(old, copy.deepcopy(old)), [])

    def test_policy_drift_same_and_changed(self):
        old = self.snapshot()
        fingerprint = policy_fingerprint(old)
        self.assertFalse(policy_drift(fingerprint, copy.deepcopy(old)))
        new = copy.deepcopy(old)
        new["definitions"]["global_policy"]["policy"] = DENY
        self.assertTrue(policy_drift(fingerprint, new))

    def test_run_started_and_policy_decision_bind_fingerprint(self):
        snapshot = self.snapshot()
        with tempfile.TemporaryDirectory() as directory:
            fingerprint = persist_snapshot(snapshot, os.path.join(directory, "policies"))
            binding = PolicyBinding(snapshot, fingerprint)
            writer = AuditWriter("1" * 32, directory=directory)
            self.assertEqual(run_agent("task", ShellProvider(), audit_writer=writer,
                                       policy_binding=binding), "done")
            events = read_events(writer.run_id, directory)
        started = events[0]["references"]
        decision = next(event for event in events
                        if event["event_type"] == "policy_decision")
        self.assertEqual(started["policy_fingerprint"], fingerprint)
        self.assertEqual(started["policy_schema_version"], 1)
        self.assertEqual(decision["references"]["policy_fingerprint"], fingerprint)
        self.assertEqual(decision["references"]["composition_inputs"]["zone"], WORKSPACE)

    def test_bound_run_is_unchanged_and_new_snapshot_observes_definition_change(self):
        old = self.snapshot()
        old_fingerprint = policy_fingerprint(old)
        binding = PolicyBinding(old, old_fingerprint)
        detached = binding.snapshot
        detached["definitions"]["global_policy"]["policy"] = DENY
        self.assertNotEqual(detached, binding.snapshot)
        self.assertEqual(binding.fingerprint, old_fingerprint)
        asking = StaticPolicyLayer(
            "workspace-editor", ASK, frozenset({"shell"}),
            SIDE_EFFECTING, False, False,
        )
        with patch.dict(composition.CAPABILITY_PROFILES,
                        {"workspace-editor": asking}, clear=False):
            self.assertEqual(policy_fingerprint(old), old_fingerprint)
            new = build_policy_snapshot()
        self.assertNotEqual(policy_fingerprint(new), old_fingerprint)

    def test_resume_metadata_uses_current_binding_and_records_drift(self):
        current = self.snapshot()
        with tempfile.TemporaryDirectory() as directory:
            fingerprint = persist_snapshot(current, os.path.join(directory, "policies"))
            writer = AuditWriter("2" * 32, directory=directory)
            run_agent("resume", FinalProvider(), audit_writer=writer,
                      policy_binding=PolicyBinding(current, fingerprint),
                      previous_run_id="3" * 32,
                      previous_policy_fingerprint="a" * 64)
            started = read_events(writer.run_id, directory)[0]["references"]
        self.assertEqual(started["previous_policy_fingerprint"], "a" * 64)
        self.assertEqual(started["policy_fingerprint"], fingerprint)
        self.assertTrue(started["policy_drift"])

    def test_old_audit_binding_is_explicitly_unavailable(self):
        events = [{"event_type": "run_started", "references": {}}]
        with self.assertRaisesRegex(PolicySnapshotError,
                                    "historical policy binding unavailable"):
            binding_from_events(events)

    def test_subagent_missing_v19_base_snapshot_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = AuditWriter("4" * 32, directory=directory)
            writer.append(
                "run_started", "harness", "run", "running",
                references={"policy_fingerprint": "b" * 64},
            )
            with self.assertRaisesRegex(PolicySnapshotError,
                                        "historical policy snapshot unavailable"):
                run_subagent(create_handoff("task"), FinalProvider(),
                             audit_writer=writer)

    def test_replay_match_and_mismatch_without_execution(self):
        snapshot = self.snapshot()
        inputs = {
            "zone": WORKSPACE, "profile": "readonly-local",
            "classification": "ALLOW", "tool_kind": "shell",
            "effect": "read_only",
            "delegated_ceiling": neutral_delegated_summary(),
        }
        event = {
            "sequence": 2, "event_type": "policy_decision", "outcome": "ALLOW",
            "references": {
                "composition_inputs": inputs,
                "policy_fingerprint": policy_fingerprint(snapshot),
            },
        }
        matched = replay_policy_events([event], snapshot)
        self.assertTrue(matched[0]["match"])
        corrupt = copy.deepcopy(event)
        corrupt["outcome"] = "DENY"
        mismatched = replay_policy_events([corrupt], snapshot)
        self.assertFalse(mismatched[0]["match"])


if __name__ == "__main__":
    unittest.main()
