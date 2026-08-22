"""Fast, offline release sanity checks for the teaching Harness."""

import ast
import contextlib
import io
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from mini_harness_core.agent import run_agent
from mini_harness_core.artifacts import ArtifactStore
from mini_harness_core.audit import AuditWriter
from mini_harness_core.dispatch import authorize_action, dispatch_authorized_action
from mini_harness_core.durability import create_action_checkpoint, recover_action_checkpoint
from mini_harness_core.evidence import EvidenceStore
from mini_harness_core.fault_injection import DeterministicFaultInjector, InjectedFault
from mini_harness_core.observation import model_context_observation, persisted_safe_observation
from mini_harness_core.planning import create_plan
from mini_harness_core.protected_paths import inspect_shell_paths
from mini_harness_core.result import ResultStore
from mini_harness_core.run_bundle import check_bundle, export_run_bundle, replay_bundle


class _SequenceProvider:
    def __init__(self, decisions):
        self._decisions = iter(decisions)

    def complete(self, _messages):
        return next(self._decisions)


def _dependency_dag():
    root = Path(__file__).parents[1] / "mini_harness_core"
    paths = {
        ".".join(path.relative_to(root).with_suffix("").parts): path
        for path in root.rglob("*.py") if path.name != "__init__.py"
    }
    graph = {name: set() for name in paths}
    for name, path in paths.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.level:
                continue
            package = name.split(".")[:-1]
            keep = max(0, len(package) - node.level + 1)
            target = ".".join(package[:keep] + (node.module or "").split("."))
            if target in paths:
                graph[name].add(target)
    visiting, visited = set(), set()

    def visit(name):
        if name in visiting:
            raise AssertionError("dependency cycle")
        if name in visited:
            return
        visiting.add(name)
        for target in graph[name]:
            visit(target)
        visiting.remove(name)
        visited.add(name)

    for name in graph:
        visit(name)


def _authority_and_protected_paths():
    checkpoint = create_action_checkpoint("shell", {"command": "pwd"}, "read_only")
    action = authorize_action(
        checkpoint=checkpoint, capability="shell", arguments=checkpoint["arguments"],
        effect="read_only", policy_decision="ALLOW", approval_granted=False,
        run_id="a" * 32,
    )
    calls = []
    dispatch_authorized_action(
        action, checkpoint, persist_checkpoint=lambda _value: None,
        executor=lambda arguments: calls.append(arguments) or {"exit_code": 0},
    )
    if len(calls) != 1:
        raise AssertionError("sealed action did not dispatch exactly once")
    if inspect_shell_paths("cat .env.local").allowed:
        raise AssertionError("protected path allowed")
    try:
        dispatch_authorized_action(
            {}, checkpoint, persist_checkpoint=lambda _value: None,
            executor=lambda _arguments: None,
        )
    except PermissionError:
        pass
    else:
        raise AssertionError("unsealed action dispatched")


def _golden_and_bundle(root):
    os.mkdir(root)
    workspace = os.path.join(root, "workspace")
    audit = os.path.join(root, "audit")
    bundles = os.path.join(root, "bundles")
    os.mkdir(workspace)
    writer = AuditWriter("b" * 32, "c" * 32, audit)
    plan = create_plan("create report", [{
        "id": "step-1", "description": "write verified report", "depends_on": [],
    }], plan_id="self-check-plan")
    provider = _SequenceProvider([
        {"type": "tool_call", "command": "echo hello > report.md"},
        {"type": "tool_call", "command": "cat report.md"},
        {"type": "final_answer", "final_answer": "done", "claimed_status": "completed"},
    ])
    previous = os.getcwd()
    try:
        os.chdir(workspace)
        with patch("mini_harness_core.agent.request_approval", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()):
            result = run_agent(
                "create report", provider, max_steps=3, current_plan=plan,
                audit_writer=writer, return_result=True,
                output_contract={"required_artifacts": [{
                    "name": "report", "artifact_type": "workspace_file",
                    "path": "report.md", "step_id": "step-1",
                    "requirements": ["exists", "non_empty", "content_identity", "verified"],
                }]},
            )
    finally:
        os.chdir(previous)
    if result["status"] != "completed":
        raise AssertionError("golden result incomplete")
    stored_result = ResultStore(os.path.join(audit, "results")).load(writer.run_id)
    if not ArtifactStore(os.path.join(audit, "artifacts")).list_run(writer.run_id):
        raise AssertionError("artifact missing")
    if not stored_result["evidence_ids"] or not all(
        EvidenceStore(os.path.join(audit, "evidence")).load(evidence_id)
        for evidence_id in stored_result["evidence_ids"]
    ):
        raise AssertionError("evidence missing")
    bundle, _manifest, _reused = export_run_bundle(writer.run_id, audit, bundles)
    if not check_bundle(bundle)["match"] or replay_bundle(bundle)["status"] != "MATCH":
        raise AssertionError("offline bundle replay mismatch")
    return bundle


def _exactly_once(root):
    os.mkdir(root)
    target = os.path.join(root, "once.txt")
    checkpoint = create_action_checkpoint(
        "shell", {"command": "touch once.txt"}, "side_effecting"
    )
    action = authorize_action(
        checkpoint=checkpoint, capability="shell", arguments=checkpoint["arguments"],
        effect="side_effecting", policy_decision="ASK", approval_granted=True,
        run_id="d" * 32, workspace_root=root,
    )
    states, calls = [], []

    def execute(_arguments):
        calls.append(True)
        Path(target).write_text("once\n", encoding="utf-8")
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    try:
        dispatch_authorized_action(
            action, checkpoint, persist_checkpoint=lambda value: states.append(value),
            executor=execute,
            fault_injector=DeterministicFaultInjector([
                "after_tool_success_before_terminal_checkpoint"
            ]),
        )
    except InjectedFault:
        pass
    else:
        raise AssertionError("crash hook did not fire")
    recovered, decision = recover_action_checkpoint(states[-1])
    if len(calls) != 1 or recovered["state"] != "unknown" or decision != "reconcile_or_block":
        raise AssertionError("unknown effect was replayed or misclassified")
    if Path(target).read_text(encoding="utf-8") != "once\n":
        raise AssertionError("side effect identity changed")


def _secret_boundary():
    marker = "secret-marker"
    raw = {
        "exit_code": 0,
        "stdout": "OPENAI_API_KEY=" + marker,
        "result": {"Authorization": "Bearer " + marker},
    }
    persisted = persisted_safe_observation(raw, "mcp:fake:read", {})
    context = model_context_observation(persisted)
    if marker in json.dumps({"persisted": persisted, "context": context}):
        raise AssertionError("secret crossed projection boundary")


def run_self_check():
    """Return ordered check results; all work stays inside a temp directory."""
    checks = {
        "dependency_dag": lambda _root: _dependency_dag(),
        "authority": lambda _root: _authority_and_protected_paths(),
        "protected_paths": lambda _root: (
            None if not inspect_shell_paths("cat .env.local").allowed
            else (_ for _ in ()).throw(AssertionError("protected path allowed"))
        ),
        "golden_run": _golden_and_bundle,
        "exactly_once": _exactly_once,
        "secret_boundary": lambda _root: _secret_boundary(),
        "bundle_replay": _golden_and_bundle,
    }
    results = []
    with tempfile.TemporaryDirectory(prefix="mini-harness-self-check-") as root:
        for name, check in checks.items():
            try:
                check(os.path.join(root, name)) if name in {
                    "golden_run", "exactly_once", "bundle_replay"
                } else check(root)
            except Exception as error:
                results.append((name, False, f"{type(error).__name__}: {error}"))
            else:
                results.append((name, True, None))
    return results


def print_self_check():
    results = run_self_check()
    print("SELF CHECK")
    for name, passed, _error in results:
        print(f"{name}: {'PASS' if passed else 'FAIL'}")
    passed = all(item[1] for item in results)
    print("\nFINAL: " + ("PASS" if passed else "FAIL"))
    return 0 if passed else 1
