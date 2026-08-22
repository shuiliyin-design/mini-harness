import ast
import copy
import os
import tempfile
import unittest
from pathlib import Path

from mini_harness_core.durability import (
    build_action_correlation_facts, create_action_checkpoint,
    transition_action_checkpoint,
)
from mini_harness_core.integrity import (
    ImmutableRecordConflict, atomic_json_publish, canonical_json_bytes,
    sha256_identity, verify_immutable_record,
)


ROOT = Path(__file__).parent
CORE = ROOT / "mini_harness_core"


def function_node(path, name):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def dependency_graph():
    paths = {path.stem: path for path in CORE.glob("*.py")}
    graph = {name: set() for name in paths}
    for name, path in paths.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level and node.module:
                dependency = node.module.split(".")[0]
                if dependency in paths:
                    graph[name].add(dependency)
    return graph


class AgentArchitectureV27Tests(unittest.TestCase):
    def test_public_orchestrator_is_small_and_has_no_executor_reference(self):
        path = CORE / "agent.py"
        node = function_node(path, "run_agent")
        self.assertLessEqual(node.end_lineno - node.lineno + 1, 100)
        names = {
            child.id for child in ast.walk(node)
            if isinstance(child, ast.Name)
        }
        self.assertTrue({"execute_shell", "execute_mcp_tool"}.isdisjoint(names))
        runtime = function_node(path, "_run_agent_runtime")
        runtime_names = {
            child.id for child in ast.walk(runtime)
            if isinstance(child, ast.Name)
        }
        self.assertTrue(
            {"execute_shell", "execute_mcp_tool"}.isdisjoint(runtime_names)
        )

    def test_runtime_and_phase_helpers_have_no_moved_monolith(self):
        path = CORE / "agent.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        ]
        runtime = function_node(path, "_run_agent_runtime")
        self.assertLessEqual(runtime.end_lineno - runtime.lineno + 1, 300)
        self.assertLessEqual(
            max(node.end_lineno - node.lineno + 1 for node in functions),
            350,
        )
        self.assertFalse(any(
            node.end_lineno - node.lineno + 1 > 500 for node in functions
        ))

    def test_authority_order_and_dispatch_boundary_remain_explicit(self):
        path = CORE / "agent.py"
        shell = function_node(path, "_handle_shell_decision")
        mcp = function_node(path, "_handle_mcp_decision")
        shell_source = ast.unparse(shell)
        mcp_source = ast.unparse(mcp)
        self.assertLess(
            shell_source.index("classify_shell"),
            shell_source.index("runtime.dispatch_shell"),
        )
        self.assertLess(
            mcp_source.index("policy_for"),
            mcp_source.index("runtime.dispatch_mcp"),
        )
        for name in ("_dispatch_shell_action", "_dispatch_mcp_action"):
            source = ast.unparse(function_node(path, name))
            self.assertLess(
                source.index("authorize_action"),
                source.index("dispatch_authorized_action"),
            )
        for node in (shell, mcp, function_node(path, "_run_agent_runtime")):
            calls = {
                child.func.id
                for child in ast.walk(node)
                if isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
            }
            self.assertTrue(
                {"execute_shell", "execute_mcp_tool",
                 "dispatch_authorized_action"}.isdisjoint(calls)
            )

    def test_facade_binding_count_remains_compatible(self):
        tree = ast.parse((ROOT / "mini_harness.py").read_text(encoding="utf-8"))
        bindings = [
            alias.asname or alias.name
            for node in tree.body if isinstance(node, ast.ImportFrom)
            for alias in node.names
        ]
        self.assertEqual(len(bindings), 315)
        self.assertEqual(len(set(bindings)), 315)


class IntegrityV27Tests(unittest.TestCase):
    def test_golden_canonical_bytes_and_fingerprint(self):
        value = {"z": 1, "a": "中文", "nested": {"b": False, "a": None}}
        expected = (
            b'{"a":"\xe4\xb8\xad\xe6\x96\x87",'
            b'"nested":{"a":null,"b":false},"z":1}'
        )
        self.assertEqual(canonical_json_bytes(value), expected)
        self.assertEqual(
            sha256_identity(value),
            "86ac6ddf789720c1474ec14da811ac9ee5cdd54e1638353b5fcc76ac27b1069b",
        )

    def test_immutable_publish_is_idempotent_and_conflicts_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "records", "one.json")
            value = {"value": 1}
            atomic_json_publish(path, value)
            atomic_json_publish(path, copy.deepcopy(value))
            payload = canonical_json_bytes(value) + b"\n"
            self.assertTrue(verify_immutable_record(path, payload))
            with self.assertRaises(ImmutableRecordConflict):
                atomic_json_publish(path, {"value": 2})

    def test_integrity_has_only_standard_library_imports(self):
        tree = ast.parse((CORE / "integrity.py").read_text(encoding="utf-8"))
        imports = {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imports.update(
            alias.name.split(".")[0]
            for node in ast.walk(tree) if isinstance(node, ast.Import)
            for alias in node.names
        )
        self.assertEqual(imports, {"hashlib", "json", "os", "tempfile"})


class CorrelationV27Tests(unittest.TestCase):
    def test_facts_do_not_merge_verification_retry_and_reconciliation(self):
        checkpoint = create_action_checkpoint(
            "shell", {"command": "touch result.txt"}, "side_effecting",
        )
        unknown = transition_action_checkpoint(checkpoint, "executing")
        unknown = transition_action_checkpoint(unknown, "unknown")
        facts = build_action_correlation_facts(
            unknown, "shell", {"command": "touch result.txt"},
            verification_state={"requires_verification": True},
            evidence_state={"fresh": False},
            reconciliation_state={"required": True},
        )
        self.assertTrue(facts["matches_checkpoint"])
        self.assertTrue(facts["unsafe_unknown_side_effect"])
        self.assertEqual(facts["checkpoint_state"], "unknown")
        self.assertNotIn("decision", facts)


class DependencyV27Tests(unittest.TestCase):
    def test_complete_core_dependency_graph_is_a_dag(self):
        graph = dependency_graph()
        visiting, visited = set(), set()

        def visit(name):
            if name in visiting:
                self.fail(f"dependency cycle at {name}")
            if name in visited:
                return
            visiting.add(name)
            for dependency in graph[name]:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)

        for name in graph:
            visit(name)

    def test_layering_and_cycle_sensitive_modules(self):
        graph = dependency_graph()
        for name, dependencies in graph.items():
            if name != "cli":
                self.assertNotIn("cli", dependencies)
                self.assertNotIn("agent", dependencies)
        self.assertNotIn("run_envelope", graph["result"])
        self.assertNotIn("result", graph["run_envelope"])
        self.assertEqual(graph["providers"], set())
        self.assertTrue({"agent", "cli"}.isdisjoint(graph["authority"]))

    def test_no_lazy_import_is_used_to_hide_a_cycle(self):
        graph = dependency_graph()
        # dependency_graph walks the complete AST, including function bodies.
        self.assertIn("run_control", graph["context"])
        self.assertNotIn("context", graph["run_control"])


if __name__ == "__main__":
    unittest.main()
