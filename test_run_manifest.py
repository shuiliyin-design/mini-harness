import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from mini_harness_core.agent import run_agent
from mini_harness_core.audit import AuditWriter, read_events
from mini_harness_core.cli import main
from mini_harness_core.context import RuntimeContextAssembler
from mini_harness_core.mcp import FakeMCPClient, MCPRegistry
from mini_harness_core.memory import MemoryStore
from mini_harness_core.policy_snapshot import (
    PolicyBinding, build_policy_snapshot, persist_snapshot, policy_fingerprint,
)
from mini_harness_core.providers import FakeProvider, OpenAICompatibleHTTPClient, RealProvider
from mini_harness_core.run_manifest import (
    HARNESS_RELEASE, MANIFEST_SCHEMA_VERSION, RunManifestError,
    RunManifestStore, build_configuration, build_manifest,
    configuration_fingerprint, endpoint_identity, integrity_check,
    manifest_differences,
)
from mini_harness_core.policy_snapshot import mappings_from_registry
from mini_harness_core.context import PROJECT_ROOT


class FinalProvider:
    def complete(self, messages):
        return {"type": "final_answer", "final_answer": "done"}


class RunManifestV20Tests(unittest.TestCase):
    def binding(self, directory, registry=None):
        snapshot = build_policy_snapshot(
            mcp_mappings={} if registry is None else {
                "mcp:demo:echo": {
                    "zone": "external", "profile": "mcp-capability",
                    "local_effect": "read_only", "policy": "ASK",
                }
            }
        )
        fingerprint = persist_snapshot(snapshot, os.path.join(directory, "policies"))
        return PolicyBinding(snapshot, fingerprint)

    def configuration(self, root, directory, provider=None, registry=None,
                      task="task", budget=5000, store=None):
        store = store or MemoryStore(os.path.join(root, ".memory", "memories.json"))
        assembler = RuntimeContextAssembler(root, store, registry)
        return build_configuration(
            task, provider or FakeProvider(), self.binding(directory, registry),
            assembler, budget,
        )

    def test_canonical_fingerprint_and_instance_fields(self):
        left = {"b": 2, "a": {"y": 1, "x": 0}}
        right = {"a": {"x": 0, "y": 1}, "b": 2}
        self.assertEqual(configuration_fingerprint(left), configuration_fingerprint(right))
        with tempfile.TemporaryDirectory() as root:
            config = self.configuration(root, root)
            first = build_manifest("1" * 32, "2" * 32, config, "one")
            second = build_manifest("3" * 32, "4" * 32, config, "two")
            self.assertEqual(first["configuration_fingerprint"],
                             second["configuration_fingerprint"])

    def test_harness_and_schema_identity_are_explicit(self):
        with tempfile.TemporaryDirectory() as root:
            harness = self.configuration(root, root)["harness"]
        self.assertEqual(HARNESS_RELEASE, "development")
        self.assertEqual(harness["manifest_schema_version"], MANIFEST_SCHEMA_VERSION)
        self.assertIn("planning_schema_version", harness)
        self.assertIn("session_schema_version", harness)

    def test_model_endpoint_is_safe_and_changes_identity(self):
        safe = endpoint_identity("https://user:pass@example.test/secret?api_key=x#frag")
        self.assertTrue(safe["endpoint_present"])
        self.assertIsNone(safe["endpoint_origin_digest"])
        a = endpoint_identity("https://example.test/v1?api_key=SECRET")
        b = endpoint_identity("https://other.test/v1")
        self.assertNotEqual(a["endpoint_origin_digest"], b["endpoint_origin_digest"])
        self.assertNotIn("example.test", json.dumps(a))
        with tempfile.TemporaryDirectory() as root:
            client = OpenAICompatibleHTTPClient(
                "https://example.test/v1?api_key=SECRET", "model-a", "key"
            )
            config = self.configuration(root, root, RealProvider(client))
            serialized = json.dumps(config)
            self.assertNotIn("SECRET", serialized)
            self.assertNotIn("example.test", serialized)
            self.assertNotIn("api_key", serialized.lower())

    def test_model_api_mode_and_identifier_cause_drift(self):
        with tempfile.TemporaryDirectory() as root:
            a = self.configuration(root, root, RealProvider(
                OpenAICompatibleHTTPClient("https://a.test/v1", "one")))
            b = self.configuration(root, root, RealProvider(
                OpenAICompatibleHTTPClient("https://a.test/v1", "two",
                                           api_mode="completions")))
        differences = manifest_differences(a, b)
        self.assertTrue(any(item[0] == "Model" for item in differences))

    def test_project_skill_and_memory_content_do_not_persist(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "AGENTS.md"), "w", encoding="utf-8") as stream:
                stream.write("UNIQUE AGENTS BODY")
            skill_dir = os.path.join(root, "skills", "python-testing")
            os.makedirs(skill_dir)
            with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as stream:
                stream.write("---\nname: python-testing\ndescription: python testing\n---\nUNIQUE SKILL BODY")
            store = MemoryStore(os.path.join(root, ".memory", "memories.json"))
            store.save([{
                "id": "memory-1", "created_at": "one", "updated_at": "one",
                "kind": "workflow", "content": "UNIQUE MEMORY BODY",
                "source": "user_approved", "status": "active",
            }])
            first = self.configuration(root, root, task="python-testing", store=store)
            serialized = json.dumps(first)
            self.assertNotIn("UNIQUE AGENTS BODY", serialized)
            self.assertNotIn("UNIQUE SKILL BODY", serialized)
            self.assertNotIn("UNIQUE MEMORY BODY", serialized)
            with open(os.path.join(root, "AGENTS.md"), "w", encoding="utf-8") as stream:
                stream.write("changed")
            second = self.configuration(root, root, task="other", store=store)
            self.assertNotEqual(configuration_fingerprint(first),
                                configuration_fingerprint(second))
            self.assertNotEqual(first["project_context"]["agents_fingerprint"],
                                second["project_context"]["agents_fingerprint"])
            self.assertNotEqual(first["project_context"]["active_skill_name"],
                                second["project_context"]["active_skill_name"])

    def test_memory_version_and_capability_transport_cause_drift(self):
        with tempfile.TemporaryDirectory() as root:
            store = MemoryStore(os.path.join(root, ".memory", "memories.json"))
            record = {
                "id": "m", "created_at": "one", "updated_at": "one",
                "kind": "workflow", "content": "stable workflow",
                "source": "user_approved", "status": "active",
            }
            store.save([record])
            fake = MCPRegistry({"demo": FakeMCPClient()})
            first = self.configuration(root, root, registry=fake, store=store)
            record["updated_at"] = "two"
            record["content"] = "updated workflow"
            store.save([record])
            second = self.configuration(root, root, registry=fake, store=store)
            self.assertNotEqual(first["memory"], second["memory"])
            changed = copy.deepcopy(first)
            changed["capabilities"]["catalog_identity"][0]["transport_kind"] = "stdio"
            changed["capabilities"]["capability_catalog_fingerprint"] = \
                configuration_fingerprint(changed["capabilities"]["catalog_identity"])
            self.assertNotEqual(configuration_fingerprint(first),
                                configuration_fingerprint(changed))
            self.assertNotIn("description", json.dumps(first["capabilities"]))
            fake.close()

    def test_persistence_is_atomic_and_immutable(self):
        with tempfile.TemporaryDirectory() as root:
            config = self.configuration(root, root)
            store = RunManifestStore(os.path.join(root, "manifests"))
            manifest = build_manifest("1" * 32, "2" * 32, config)
            store.persist(manifest)
            store.persist(manifest)
            changed = copy.deepcopy(manifest)
            changed["created_at"] = "different"
            with self.assertRaisesRegex(RunManifestError, "immutable"):
                store.persist(changed)

    def test_run_publishes_manifest_before_audit_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            binding = self.binding(directory)
            writer = AuditWriter("2" * 32, directory=directory)
            self.assertEqual(run_agent(
                "task", FinalProvider(), audit_writer=writer,
                policy_binding=binding,
                context_assembler=RuntimeContextAssembler(
                    directory, MemoryStore(os.path.join(directory, "memory.json"))
                ),
            ), "done")
            manifest = RunManifestStore(os.path.join(directory, "manifests")).load(
                writer.run_id
            )
            started = read_events(writer.run_id, directory)[0]
            self.assertEqual(started["references"]["manifest_fingerprint"],
                             manifest["configuration_fingerprint"])
            self.assertEqual(started["references"]["policy_fingerprint"],
                             binding.fingerprint)

    def test_resume_creates_new_manifest_and_records_runtime_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            agents_path = os.path.join(directory, "AGENTS.md")
            with open(agents_path, "w", encoding="utf-8") as stream:
                stream.write("first")
            binding = self.binding(directory)
            assembler = RuntimeContextAssembler(
                directory, MemoryStore(os.path.join(directory, "memory.json"))
            )
            first_writer = AuditWriter("2" * 32, directory=directory)
            run_agent("task", FinalProvider(), audit_writer=first_writer,
                      policy_binding=binding, context_assembler=assembler)
            manifest_store = RunManifestStore(os.path.join(directory, "manifests"))
            old = manifest_store.load(first_writer.run_id)
            with open(agents_path, "w", encoding="utf-8") as stream:
                stream.write("second")
            second_writer = AuditWriter("2" * 32, directory=directory)
            run_agent(
                "task", FinalProvider(), audit_writer=second_writer,
                policy_binding=binding, context_assembler=assembler,
                previous_run_id=first_writer.run_id,
                previous_policy_fingerprint=binding.fingerprint,
            )
            started = read_events(second_writer.run_id, directory)[0]["references"]
            self.assertTrue(started["runtime_drift"])
            self.assertEqual(started["previous_manifest_fingerprint"],
                             old["configuration_fingerprint"])
            self.assertEqual(manifest_store.load(first_writer.run_id), old)

    def test_integrity_and_cli_show_check_reconstruct(self):
        with tempfile.TemporaryDirectory() as directory:
            policy_dir = os.path.join(directory, "policies")
            config = self.configuration(directory, directory)
            manifest = build_manifest("a" * 32, "b" * 32, config)
            store = RunManifestStore(os.path.join(directory, "manifests"))
            store.persist(manifest)
            self.assertTrue(integrity_check(manifest, policy_dir))
            for option, expected in (
                ("--manifest-show", "Configuration Fingerprint="),
                ("--manifest-check", "MANIFEST CHECK MATCH"),
                ("--manifest-reconstruct", "NOT EXECUTION REPLAY"),
            ):
                output = io.StringIO()
                with patch("mini_harness_core.cli.POLICY_DIRECTORY", policy_dir), \
                     patch("sys.argv", ["mini_harness.py", option, "a" * 32]), \
                     redirect_stdout(output):
                    main()
                self.assertIn(expected, output.getvalue())

    def test_cli_status_same_drift_and_known_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            policy_dir = os.path.join(directory, "policies")

            def make_registry():
                return MCPRegistry({"demo": FakeMCPClient()})

            registry = make_registry()
            snapshot = build_policy_snapshot(mcp_mappings=mappings_from_registry(registry))
            binding = PolicyBinding(snapshot, policy_fingerprint(snapshot))
            config = build_configuration(
                "", FakeProvider(), binding,
                RuntimeContextAssembler(mcp_registry=registry), None,
            )
            manifest = build_manifest("c" * 32, "d" * 32, config)
            RunManifestStore(os.path.join(directory, "manifests")).persist(manifest)
            registry.close()

            def invoke(option, budget=None):
                output = io.StringIO()
                environment = {"MINI_HARNESS_PROVIDER": "fake"}
                if budget is not None:
                    environment["MINI_HARNESS_CONTEXT_BUDGET"] = str(budget)
                with patch("mini_harness_core.cli.POLICY_DIRECTORY", policy_dir), \
                     patch("mini_harness_core.cli.MCPRegistry",
                           side_effect=lambda *args, **kwargs: make_registry()), \
                     patch.dict(os.environ, environment, clear=True), \
                     patch("sys.argv", ["mini_harness.py", option, "c" * 32]), \
                     redirect_stdout(output):
                    main()
                return output.getvalue()

            self.assertIn("SAME", invoke("--manifest-status"))
            self.assertIn("RUNTIME_DRIFT", invoke("--manifest-status", 7))
            self.assertIn("Context.context_budget", invoke("--manifest-diff", 7))

    def test_corruption_and_unknown_run_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            store = RunManifestStore(root)
            with self.assertRaisesRegex(RunManifestError, "不存在"):
                store.load("f" * 32)
            config = self.configuration(root, root)
            manifest = build_manifest("1" * 32, "2" * 32, config)
            manifest["configuration_fingerprint"] = "0" * 64
            self.assertFalse(integrity_check(manifest, os.path.join(root, "policies")))


if __name__ == "__main__":
    unittest.main()
