"""Command-line parsing, runtime wiring, and user interaction."""

import argparse
import json
import os
import re
import sys

from .agent import run_agent
from .audit import (
    AUDIT_DIR, AuditWriter, explain_events, format_timeline, list_runs, read_events,
)
from .authority import POLICY_ASK
from .context import RuntimeContextAssembler, parse_context_budget
from .mcp import (
    MCP_EFFECT_READ_ONLY,
    FakeMCPClient,
    MCPRegistry,
    StdioMCPClient,
)
from .memory import MemoryStore, screen_memory_content
from .providers import FakeProvider, OpenAICompatibleHTTPClient, RealProvider
from .session import SessionStore
from .run_control import mark_cancelled, mark_paused, resume_run
from .governance import resume_governance
from .policy_snapshot import (
    POLICY_DIRECTORY, PolicyBinding, PolicySnapshotError, authority_diff,
    binding_from_events, build_policy_snapshot, mappings_from_registry,
    policy_fingerprint, replay_policy_events,
)
from .run_manifest import (
    RunManifestError, RunManifestStore, build_configuration,
    configuration_fingerprint, integrity_check, manifest_differences,
    rebuild_configuration_for_status,
)
from .run_envelope import (
    RunEnvelopeError, RunEnvelopeStore, envelope_integrity_check,
    harness_replay_check,
)
from .evidence import (
    EVIDENCE_DIR, EvidenceStore, evidence_integrity_check, evidence_trace,
)
from .artifacts import (
    ARTIFACT_DIR, OUTPUT_CONTRACT_DIR, ArtifactStore, OutputContractStore,
    artifact_integrity_check, artifact_trace, outputs_status,
)
from .result import (
    RESULT_DIR, ResultStore, answer_identity, result_integrity_check,
)
from .run_bundle import (
    LocalHistoricalResolver, RunBundleError, check_bundle, export_run_bundle,
    replay_bundle, show_bundle,
)


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _render_match(label, matched):
    print(label + ("MATCH" if matched else "MISMATCH"))


def load_dotenv_local(path=None):
    """Load simple KEY=value entries without overriding the process environment."""
    env_path = path or os.path.join(PROJECT_ROOT, ".env.local")
    try:
        with open(env_path, encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                if "=" not in line:
                    continue
                name, value = line.split("=", 1)
                name = name.strip()
                if not ENV_NAME_PATTERN.fullmatch(name):
                    continue
                value = value.strip()
                if (
                    len(value) >= 2
                    and value[0] == value[-1]
                    and value[0] in ("'", '"')
                ):
                    value = value[1:-1]
                os.environ.setdefault(name, value)
    except FileNotFoundError:
        pass


# ==================== MCP External Capabilities ====================

# MCP public symbols are imported from mini_harness_core.mcp.
# Provider public symbols are imported from mini_harness_core.providers.
# ==================== Tool Executor ====================


def list_memories(store):
    memories = store.load()
    if not memories:
        print("[Memory] 暂无长期记忆")
        return []
    for memory in memories:
        print(f"id: {memory['id']}")
        print(f"kind: {memory['kind']}")
        print(f"content: {memory['content']}")
        print(f"updated_at: {memory['updated_at']}")
        print(f"status: {memory['status']}")
        print()
    return memories


def forget_memory_interactively(store, memory_id):
    memories = store.load()
    memory = store._find(memories, memory_id)
    print(f"[Memory Forget] {memory['id']} {memory['kind']}: {memory['content']}")
    if input("设为 inactive？输入 y 批准，其他输入拒绝：").strip() != "y":
        print("memory not forgotten")
        return False
    store.forget(memory_id)
    print("memory forgotten")
    return True


def update_memory_interactively(store, memory_id):
    memories = store.load()
    memory = store._find(memories, memory_id)
    content = input("请输入新的 memory content：").strip()
    allowed, reason = screen_memory_content(content)
    if not allowed:
        raise ValueError(f"Memory Policy DENY：{reason}")
    print(f"[Memory Update] id: {memory['id']}")
    print(f"old: {memory['content']}")
    print(f"new: {content}")
    if input("确认更新？输入 y 批准，其他输入拒绝：").strip() != "y":
        print("memory not updated")
        return False
    store.update(memory_id, content)
    print("memory updated")
    return True


def main():
    parser = argparse.ArgumentParser(description="最小 AI Agent Harness")
    parser.add_argument("--resume", metavar="SESSION_ID", help="恢复指定 session")
    management = parser.add_mutually_exclusive_group()
    management.add_argument(
        "--memory-list", action="store_true", help="列出长期记忆"
    )
    management.add_argument(
        "--memory-forget", metavar="ID", help="将指定长期记忆设为 inactive"
    )
    management.add_argument(
        "--memory-update", metavar="ID", help="交互式更新指定长期记忆"
    )
    management.add_argument(
        "--audit-list", action="store_true", help="列出最近 Audit Runs"
    )
    management.add_argument(
        "--audit-show", metavar="RUN_ID", help="显示紧凑 Audit timeline"
    )
    management.add_argument(
        "--audit-why", metavar="RUN_ID", help="确定性解释 Audit Run"
    )
    management.add_argument(
        "--audit-json", metavar="RUN_ID", help="输出 Audit JSONL 内容"
    )
    management.add_argument(
        "--policy-status", metavar="RUN_ID", help="比较历史与当前 Authority Policy"
    )
    management.add_argument(
        "--policy-diff", metavar="RUN_ID", help="显示 Authority Policy 差异"
    )
    management.add_argument(
        "--policy-replay", metavar="RUN_ID", help="重算历史 Static Policy 决策"
    )
    management.add_argument("--manifest-show", metavar="RUN_ID")
    management.add_argument("--manifest-status", metavar="RUN_ID")
    management.add_argument("--manifest-diff", metavar="RUN_ID")
    management.add_argument("--manifest-check", metavar="RUN_ID")
    management.add_argument("--manifest-reconstruct", metavar="RUN_ID")
    management.add_argument("--envelope-show", metavar="RUN_ID")
    management.add_argument("--envelope-check", metavar="RUN_ID")
    management.add_argument("--replay-check", metavar="RUN_ID")
    management.add_argument("--evidence-show", metavar="EVIDENCE_ID")
    management.add_argument("--evidence-trace", metavar="EVIDENCE_ID")
    management.add_argument("--evidence-check", metavar="EVIDENCE_ID")
    management.add_argument("--artifact-show", metavar="ARTIFACT_ID")
    management.add_argument("--artifact-trace", metavar="ARTIFACT_ID")
    management.add_argument("--artifact-check", metavar="ARTIFACT_ID")
    management.add_argument("--outputs", metavar="RUN_ID")
    management.add_argument("--result-show", metavar="RUN_ID")
    management.add_argument("--result-check", metavar="RUN_ID")
    management.add_argument("--bundle-export", metavar="RUN_ID")
    management.add_argument("--bundle-show", metavar="BUNDLE_PATH")
    management.add_argument("--bundle-check", metavar="BUNDLE_PATH")
    management.add_argument("--bundle-replay", metavar="BUNDLE_PATH")
    args = parser.parse_args()

    if args.resume and any((
        args.memory_list, args.memory_forget, args.memory_update,
        args.audit_list, args.audit_show, args.audit_why, args.audit_json,
        args.policy_status, args.policy_diff, args.policy_replay,
        args.manifest_show, args.manifest_status, args.manifest_diff,
        args.manifest_check, args.manifest_reconstruct,
        args.envelope_show, args.envelope_check, args.replay_check,
        args.evidence_show, args.evidence_trace, args.evidence_check,
        args.artifact_show, args.artifact_trace, args.artifact_check,
        args.outputs, args.result_show, args.result_check,
        args.bundle_export, args.bundle_show, args.bundle_check,
        args.bundle_replay,
    )):
        parser.error("--resume 不能与 management 参数同时使用")

    try:
        historical_resolver = LocalHistoricalResolver(AUDIT_DIR)
        bundle_argument = (
            args.bundle_export or args.bundle_show or args.bundle_check
            or args.bundle_replay
        )
        if bundle_argument:
            if args.bundle_export:
                try:
                    path, manifest, reused = export_run_bundle(
                        args.bundle_export, AUDIT_DIR
                    )
                except RunBundleError as error:
                    print("BUNDLE EXPORT REJECTED")
                    print(str(error))
                    raise SystemExit(1)
                print(f"Bundle path: {path}")
                print("Bundle fingerprint: " + manifest["bundle_fingerprint"])
                print(f"Object count: {len(manifest['objects'])}")
                print("Status: " + manifest["bundle_status"])
                if reused:
                    print("Existing Bundle: MATCH")
                return
            if args.bundle_check:
                checked = check_bundle(args.bundle_check)
                print("BUNDLE CHECK " + checked["closure_status"])
                if checked["match"] and checked["trace_status"] == "unavailable":
                    print("Trace: unavailable")
                if not checked["match"]:
                    print(checked["error"])
                    raise SystemExit(1)
                return
            if args.bundle_replay:
                replay = replay_bundle(args.bundle_replay)
                for item in replay["transitions"]:
                    print(
                        f"{item['transition_type']} #{item['sequence']} "
                        f"{item['status']}"
                    )
                print("BUNDLE REPLAY " + replay["status"])
                if replay["error"]:
                    print(replay["error"])
                if replay["status"] != "MATCH":
                    raise SystemExit(1)
                return
            summary = show_bundle(args.bundle_show)
            print("Run ID: " + summary["run_id"])
            print("Status: " + summary["status"])
            if summary["status"] == "forensic":
                print("FORENSIC BUNDLE")
                print("NOT A COMPLETED RESULT BUNDLE")
            print("Bundle fingerprint: " + summary["bundle_fingerprint"])
            print("Object counts: " + ",".join(
                f"{key}={value}" for key, value in summary["counts"].items()
                if value
            ))
            print("Policy fingerprint: " + summary["policy_fingerprint"])
            print("Manifest fingerprint: " + summary["manifest_fingerprint"])
            print("Envelope fingerprint: " + summary["envelope_fingerprint"])
            print(f"Evidence count: {summary['counts']['evidence']}")
            print(f"Artifact count: {summary['counts']['artifact']}")
            print("Result status: " + summary["result_status"])
            print(f"Cross-run vendored refs: {summary['cross_run_vendored']}")
            return
        result_run = args.result_show or args.result_check
        if result_run:
            if args.result_check:
                _render_match(
                    "RESULT CHECK ",
                    result_integrity_check(
                        result_run, resolver=historical_resolver,
                    ),
                )
                return
            result = historical_resolver.load("result", result_run)
            identity = answer_identity(result["answer"])
            print(f"Run: {result['run_id']}")
            print(f"Status: {result['status']}")
            print(f"Reason: {result['reason'] or 'none'}")
            print(f"Answer length: {identity['answer_length']}")
            print(f"Answer digest: {identity['answer_sha256']}")
            print("Artifact IDs: " + (
                ",".join(result["artifact_ids"])
                if result["artifact_ids"] else "none"
            ))
            print("Evidence IDs: " + (
                ",".join(result["evidence_ids"])
                if result["evidence_ids"] else "none"
            ))
            print(f"Plan: {result['plan_id'] or 'none'}")
            print("Candidate claimed status: " + (
                result["candidate"]["claimed_status"] or "none"
            ))
            print("Contradiction: " + (
                "true" if result["candidate"]["contradiction"] else "false"
            ))
            return
        artifact_id = (
            args.artifact_show or args.artifact_trace or args.artifact_check
        )
        if artifact_id:
            if args.artifact_check:
                _render_match(
                    "ARTIFACT CHECK ",
                    artifact_integrity_check(
                        artifact_id, resolver=historical_resolver,
                    ),
                )
                return
            artifact = historical_resolver.load("artifact", artifact_id)
            if args.artifact_trace:
                print("\n".join(artifact_trace(
                    artifact, resolver=historical_resolver,
                )))
                return
            for label, key in (
                ("ID", "artifact_id"), ("Run", "run_id"), ("Path", "path"),
                ("Status", "status"), ("Content identity", "content_identity"),
                ("Producer", "producer"), ("Evidence IDs", "evidence_ids"),
                ("Contract", "contract"),
                ("Supersedes", "supersedes_artifact_id"),
                ("Fingerprint", "artifact_fingerprint"),
            ):
                value = artifact[key]
                print(f"{label}: " + (json.dumps(
                    value, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ) if isinstance(value, (dict, list)) else str(value)))
            return
        if args.outputs:
            status = outputs_status(
                args.outputs, OutputContractStore(OUTPUT_CONTRACT_DIR),
                ArtifactStore(ARTIFACT_DIR), EvidenceStore(EVIDENCE_DIR),
            )
            print(f"Run: {status['run_id']}")
            print("Contract: " + status["contract_fingerprint"])
            for required in status["required_artifacts"]:
                print(f"Required {required['name']}: {required['path']}")
                accepted = required["accepted_artifact_ids"]
                unsatisfied = required["unsatisfied_requirements"]
                print("  Accepted: " + (",".join(accepted) if accepted else "none"))
                print("  Unsatisfied: " + (
                    ",".join(unsatisfied) if unsatisfied else "none"
                ))
            print("OUTPUTS " + ("SATISFIED" if status["satisfied"] else "UNSATISFIED"))
            return
        evidence_id = args.evidence_show or args.evidence_trace or args.evidence_check
        if evidence_id:
            if args.evidence_check:
                _render_match(
                    "EVIDENCE CHECK ", evidence_integrity_check(
                        evidence_id, resolver=historical_resolver,
                    ),
                )
                return
            evidence = historical_resolver.load("evidence", evidence_id)
            if args.evidence_trace:
                print("\n".join(evidence_trace(
                    evidence, resolver=historical_resolver,
                )))
                return
            for label, key in (
                ("ID", "evidence_id"), ("Fingerprint", "evidence_fingerprint"),
                ("Type", "evidence_type"), ("Run", "run_id"),
                ("Subject", "subject"), ("Source", "source"),
                ("Verification", "verification"), ("Freshness", "freshness"),
                ("Artifact identity", "content_identity"),
                ("References", "references"),
            ):
                value = evidence[key]
                print(f"{label}: " + (json.dumps(
                    value, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ) if isinstance(value, dict) else str(value)))
            return
        envelope_run = args.envelope_show or args.envelope_check or args.replay_check
        if envelope_run:
            if args.envelope_check:
                try:
                    envelope = historical_resolver.load("envelope", envelope_run)
                    matched = envelope_integrity_check(
                        envelope, resolver=historical_resolver,
                    )
                except (RunEnvelopeError, RunBundleError):
                    matched = False
                _render_match("ENVELOPE CHECK ", matched)
                return
            if args.replay_check:
                try:
                    envelope = historical_resolver.load("envelope", envelope_run)
                except (RunEnvelopeError, RunBundleError):
                    print("IDENTITY MISMATCH")
                    print("HARNESS REPLAY MISMATCH")
                    print("LEVEL 3 EXTERNAL RE-EXECUTION: NOT SUPPORTED")
                    return
                result = harness_replay_check(
                    envelope, resolver=historical_resolver,
                )
                print("IDENTITY " + result["identity"])
                for item in result["transitions"]:
                    print(
                        f"{item['transition_type']} #{item['sequence']} "
                        f"{item['status']}"
                    )
                print("HARNESS REPLAY " + ("MATCH" if result["match"] else "MISMATCH"))
                print("LEVEL 3 EXTERNAL RE-EXECUTION: NOT SUPPORTED")
                return
            envelope = historical_resolver.load("envelope", envelope_run)
            inputs = envelope["inputs"]
            types = sorted({item["transition_type"] for item in envelope["transitions"]})
            print(f"Run={envelope['run_id']} Session={envelope['session_id']}")
            print("Envelope Fingerprint=" + envelope["envelope_fingerprint"])
            print("Task Digest=" + inputs["task"]["task_sha256"])
            print("Session Source Digest=" + inputs["session"]["source_history_sha256"])
            print("Manifest=" + inputs["manifest_fingerprint"])
            print("Policy=" + inputs["policy_fingerprint"])
            print(f"Requests={len(envelope['requests'])}")
            print(f"Transitions={len(envelope['transitions'])}")
            print("Transition Types=" + (",".join(types) if types else "none"))
            return
        if args.audit_list:
            runs = list_runs()
            if not runs:
                print("[Audit] 暂无 Runs")
            for run in runs:
                print(
                    f"{run['run_id']}  {run['session_id']}  "
                    f"{run['started_at']}  {run['status']}"
                )
            return
        if args.audit_show:
            print(format_timeline(read_events(args.audit_show)))
            return
        if args.audit_why:
            events = read_events(args.audit_why)
            try:
                binding_from_events(events)
            except PolicySnapshotError as error:
                print(str(error))
                return
            print(explain_events(events))
            return
        if args.audit_json:
            for event in read_events(args.audit_json):
                print(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
            return
        historical_manifest_run = (
            args.manifest_show or args.manifest_check or args.manifest_reconstruct
        )
        if historical_manifest_run:
            manifest_store = RunManifestStore(os.path.join(
                os.path.dirname(POLICY_DIRECTORY), "manifests"
            ))
            if args.manifest_check:
                try:
                    manifest = manifest_store.load(historical_manifest_run, verify=False)
                    matched = integrity_check(manifest, POLICY_DIRECTORY)
                except RunManifestError as error:
                    if str(error) == "unsupported historical manifest schema":
                        print(str(error))
                        return
                    matched = False
                _render_match("MANIFEST CHECK ", matched)
                return
            manifest = manifest_store.load(historical_manifest_run)
            config = manifest["configuration"]
            if args.manifest_reconstruct:
                print("DESCRIPTIVE RECONSTRUCTION")
                print("NOT EXECUTION REPLAY")
                print("Historical Run would use:")
                print(f"Provider={config['model']['provider_kind']}")
                print(f"Model={config['model']['model_identifier']}")
                print(f"Policy={config['policy']['policy_fingerprint']}")
                print(f"AGENTS fingerprint={config['project_context']['agents_fingerprint']}")
                print(f"Active Skill={config['project_context']['active_skill_name']}")
                print(f"Capabilities fingerprint={config['capabilities']['capability_catalog_fingerprint']}")
                print(f"Memory identity={config['memory']['selected_memory_fingerprint']}")
                print("Context strategy=" + json.dumps(
                    config["context_strategy"], ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ))
                return
            print(f"Run={manifest['run_id']} Session={manifest['session_id']}")
            for label, key in (
                ("Harness", "harness"), ("Model", "model"),
                ("Policy", "policy"), ("Project Context", "project_context"),
                ("Capabilities", "capabilities"), ("Memory", "memory"),
                ("Context Strategy", "context_strategy"),
            ):
                print(label + "=" + json.dumps(
                    config[key], ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ))
            print("Configuration Fingerprint=" + manifest["configuration_fingerprint"])
            return
        policy_run_id = args.policy_status or args.policy_diff or args.policy_replay
        if policy_run_id:
            events = read_events(policy_run_id)
            historical = binding_from_events(events)
            current_snapshot = build_policy_snapshot(mcp_mappings={
                "mcp:demo:echo": {
                    "zone": "external", "profile": "mcp-capability",
                    "local_effect": MCP_EFFECT_READ_ONLY, "policy": POLICY_ASK,
                },
                "mcp:demo-stdio:echo": {
                    "zone": "external", "profile": "mcp-capability",
                    "local_effect": MCP_EFFECT_READ_ONLY, "policy": POLICY_ASK,
                },
            })
            current_fingerprint = policy_fingerprint(current_snapshot)
            if args.policy_status:
                print(f"run_id={policy_run_id}")
                print(f"historical_revision={historical.revision}")
                print(f"historical_fingerprint={historical.fingerprint}")
                print(f"current_revision={current_snapshot['policy_revision']}")
                print(f"current_fingerprint={current_fingerprint}")
                print("status=" + (
                    "SAME" if historical.fingerprint == current_fingerprint
                    else "POLICY_DRIFT"
                ))
            elif args.policy_diff:
                differences = authority_diff(historical.snapshot, current_snapshot)
                if not differences:
                    print("NO AUTHORITY POLICY DIFFERENCE")
                for path, before, after in differences:
                    print(f"{path}:")
                    print("historical=" + json.dumps(before, ensure_ascii=False,
                                                     separators=(",", ":")))
                    print("current=" + json.dumps(after, ensure_ascii=False,
                                                  separators=(",", ":")))
            else:
                results = replay_policy_events(events, historical.snapshot)
                for result in results:
                    print(f"event sequence={result['sequence']}")
                    print(f"recorded={result['recorded']}")
                    print(f"replayed={result['replayed']}")
                    print("MATCH" if result["match"] else "MISMATCH")
                matched = bool(results) and all(item["match"] for item in results)
                print("POLICY REPLAY " + ("MATCH" if matched else "MISMATCH"))
                print("POLICY REPLAY ≠ FINAL AUTHORIZATION REPLAY")
            return
    except (OSError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1)

    try:
        memory_store = MemoryStore()
        if args.memory_list:
            list_memories(memory_store)
            return
        if args.memory_forget:
            forget_memory_interactively(memory_store, args.memory_forget)
            return
        if args.memory_update:
            update_memory_interactively(memory_store, args.memory_update)
            return
        # 新建与 resume 都先验证当前 Store；Session 中不保存 Memory snapshot。
        memory_store.load()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。", file=sys.stderr)
        raise SystemExit(130)
    except (OSError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1)

    load_dotenv_local()
    try:
        context_budget = parse_context_budget(
            os.environ.get("MINI_HARNESS_CONTEXT_BUDGET")
        )
    except ValueError as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(2)
    mcp_registry = MCPRegistry(
        {"demo": FakeMCPClient(), "demo-stdio": StdioMCPClient()},
        {
            "mcp:demo:echo": POLICY_ASK,
            "mcp:demo-stdio:echo": POLICY_ASK,
        },
        {
            "mcp:demo:echo": MCP_EFFECT_READ_ONLY,
            "mcp:demo-stdio:echo": MCP_EFFECT_READ_ONLY,
        },
    )
    provider_name = os.environ.get("MINI_HARNESS_PROVIDER", "fake").lower()
    if provider_name == "fake":
        provider = FakeProvider()
    elif provider_name == "real":
        endpoint = os.environ.get("LLM_ENDPOINT", "")
        model = os.environ.get("LLM_MODEL", "")
        if not endpoint or not model:
            print(
                "错误：RealProvider 需要设置 LLM_ENDPOINT 和 LLM_MODEL。",
                file=sys.stderr,
            )
            raise SystemExit(2)
        client = OpenAICompatibleHTTPClient(
            endpoint=endpoint,
            model=model,
            api_key=os.environ.get("LLM_API_KEY", ""),
            api_mode=os.environ.get("LLM_API_MODE", "chat-completions").lower(),
        )
        provider = RealProvider(client)
    else:
        print(
            "错误：MINI_HARNESS_PROVIDER 只能是 fake 或 real。", file=sys.stderr
        )
        raise SystemExit(2)

    manifest_runtime_run = args.manifest_status or args.manifest_diff
    if manifest_runtime_run:
        try:
            manifest_store = RunManifestStore(os.path.join(
                os.path.dirname(POLICY_DIRECTORY), "manifests"
            ))
            historical_manifest = manifest_store.load(manifest_runtime_run)
            snapshot = build_policy_snapshot(
                mcp_mappings=mappings_from_registry(mcp_registry)
            )
            binding = PolicyBinding(snapshot, policy_fingerprint(snapshot))
            assembler = RuntimeContextAssembler(
                memory_store=memory_store, mcp_registry=mcp_registry
            )
            current = rebuild_configuration_for_status(
                historical_manifest["configuration"], provider, binding,
                assembler, context_budget,
            )
            current_fingerprint = configuration_fingerprint(current)
            historical_fingerprint = historical_manifest["configuration_fingerprint"]
            if args.manifest_status:
                print(f"historical_fingerprint={historical_fingerprint}")
                print(f"current_fingerprint={current_fingerprint}")
                print("SAME" if historical_fingerprint == current_fingerprint
                      else "RUNTIME_DRIFT")
            else:
                differences = manifest_differences(
                    historical_manifest["configuration"], current
                )
                if not differences:
                    print("NO REPRODUCIBILITY DIFFERENCE")
                for section, key, before, after in differences:
                    print(f"{section}.{key}: " +
                          json.dumps(before, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")) + " -> " +
                          json.dumps(after, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")))
            return
        except (OSError, ValueError) as error:
            print(f"错误：{error}", file=sys.stderr)
            raise SystemExit(1)
        finally:
            mcp_registry.close()

    try:
        store = SessionStore()
        session = store.load(args.resume) if args.resume else store.create()
        resumed_control = False
        if args.resume:
            state = session["run_control"]["state"]
            if state == "pause_requested":
                session["run_control"] = mark_paused(session["run_control"])
                state = "paused"
            if state == "paused":
                session["run_control"] = resume_run(session["run_control"])
                if session["current_governance_state"]["frozen"]:
                    session["current_governance_state"] = resume_governance(
                        session["current_governance_state"]
                    )
                resumed_control = True
                store.save(session)
            elif state == "cancel_requested":
                session["run_control"] = mark_cancelled(session["run_control"])
                store.save(session)
                raise ValueError("cancelled run 不能 resume；请创建新 run")
            elif state == "cancelled":
                raise ValueError("cancelled run 不能 resume；请创建新 run")
        action = "已恢复" if args.resume else "已创建"
        print(f"最小 AI Agent Harness（{provider.__class__.__name__}）")
        print(f"[Session] {action}：{session['session_id']}")
        task = input("请输入中文任务（直接回车运行 demo）：").strip()
        if not task:
            task = "演示工具失败后，Provider 如何根据 Observation 改变决策。"
            print(f"[Demo 任务] {task}")

        def save_action_checkpoint(value):
            session["current_action_checkpoint"] = value
            store.save(session)

        def save_run_control(value):
            session["run_control"] = value
            store.save(session)

        def save_retry_state(value):
            session["current_retry_state"] = value
            store.save(session)

        def save_governance_state(value):
            session["current_governance_state"] = value
            store.save(session)

        previous_run_id = None
        previous_policy_fingerprint = None
        if args.resume:
            previous = next((item for item in list_runs()
                             if item["session_id"] == session["session_id"]), None)
            if previous is not None:
                previous_run_id = previous["run_id"]
                previous_events = read_events(previous_run_id)
                started = next((event for event in previous_events
                                if event["event_type"] == "run_started"), None)
                previous_policy_fingerprint = (
                    ((started or {}).get("references") or {}).get("policy_fingerprint")
                )
        audit_writer = AuditWriter(session["session_id"])
        print(f"[Run] {audit_writer.run_id}")
        run_agent(
            task,
            provider,
            messages=session["messages"],
            verification=session["verification"],
            save_checkpoint=lambda: store.save(session),
            memory_store=memory_store,
            mcp_registry=mcp_registry,
            context_assembler=RuntimeContextAssembler(
                memory_store=memory_store, mcp_registry=mcp_registry
            ),
            context_budget=context_budget,
            current_plan=session["current_plan"],
            plan_revision_history=session["plan_revision_history"],
            require_plan_grounding=bool(args.resume) or resumed_control,
            current_action_checkpoint=session["current_action_checkpoint"],
            save_action_checkpoint=save_action_checkpoint,
            run_control=session["run_control"],
            save_run_control=save_run_control,
            current_retry_state=session["current_retry_state"],
            save_retry_state=save_retry_state,
            governance_state=session["current_governance_state"],
            save_governance_state=save_governance_state,
            audit_writer=audit_writer,
            previous_run_id=previous_run_id,
            previous_policy_fingerprint=previous_policy_fingerprint,
        )
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。", file=sys.stderr)
        raise SystemExit(130)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1)
    finally:
        mcp_registry.close()
