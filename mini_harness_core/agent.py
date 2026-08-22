"""Agent and Subagent runtime control flow."""

import json
import os
import hashlib

from .audit import (
    AuditWriter, new_run_id, read_events, safe_observation_summary,
    safe_shell_command_identity,
)
from .authority import (
    POLICY_ALLOW,
    POLICY_ASK,
    POLICY_DENY,
    _effective_subagent_authority,
    _tool_allowed,
    classify_shell,
    execute_shell,
    request_approval,
)
from .dispatch import authorize_action, dispatch_authorized_action
from .fault_injection import trigger_fault
from .observation import persisted_safe_observation
from .protected_paths import inspect_mcp_paths, inspect_subagent_paths
from .context import RuntimeContextAssembler
from .durability import (
    create_action_checkpoint,
    expected_file_write,
    reconcile_file_observation,
    recover_action_checkpoint,
    recovery_control_state,
    transition_action_checkpoint,
    validate_action_checkpoint,
)
from .handoff import _safe_result, validate_handoff
from .evidence import (
    EvidenceStore, artifact_ref, create_mcp_observation_evidence,
    create_reasoning_evidence, create_reconciliation_evidence,
    create_subagent_return_evidence, create_tool_observation_evidence,
    create_verification_evidence,
)
from .artifacts import (
    ArtifactError, ArtifactStore, OutputContractStore,
    create_artifact,
    create_output_contract, create_producer, evaluate_artifact_contract,
    current_output_contract_gate, observe_workspace_file, select_supersession,
)
from .mcp import (
    MCP_EFFECT_READ_ONLY,
    MCP_EFFECT_SIDE_EFFECTING,
    MCP_EFFECT_UNKNOWN,
    execute_mcp_tool,
    validate_json_schema,
)
from .memory import (
    MEMORY_KINDS,
    MemoryStore,
    request_memory_approval,
    screen_memory_content,
)
from .planning import (
    block_step,
    complete_step,
    propose_step_completion,
    select_ready_step,
    start_step,
    retry_exhausted_outcome,
    validate_plan,
    validate_revision_history,
)
from .run_control import (
    can_schedule_action, create_run_control, settle_control_boundary,
    validate_run_control,
)
from .retry import (
    classify_failure, complete_retry, cooperative_backoff, create_retry_state,
    decide_retry, record_failure, reopen_retry_after_reconciliation,
    start_attempt, validate_retry_state,
)
from .run_manifest import (
    RunManifestStore, build_configuration, build_manifest,
)
from .run_envelope import (
    RunEnvelopeStore, build_envelope, planning_transition_input,
)
from .verification import (
    build_verification_feedback,
    extract_verification_target,
    is_related_verification,
    replay_verification_transition,
    verification_observation_identity,
)
from .governance import (
    backoff_decision, consume_action, consume_safety_reconciliation,
    deadline_status, effective_subagent_timeout, effective_tool_timeout,
    freeze_governance, normal_action_decision, safety_reconciliation_decision,
    start_step_deadline, validate_governance_state,
)
from .policy_snapshot import (
    bind_current_policy, binding_from_events, effective_policy_reference,
)
from .providers import ProviderError
from .result import (
    ResultStore, answer_identity, bind_final_result,
    build_authoritative_result_state, normalize_final_candidate,
    screen_result_answer,
)


def _complete(
    provider, messages, context_assembler, control_state, context_budget,
    current_plan=None, plan_runtime_state=None, request_recorder=None,
):
    system_instructions = getattr(provider, "SYSTEM_PROMPT", None)
    if isinstance(system_instructions, str):
        messages = context_assembler.prepare_request(
            system_instructions, messages, control_state, context_budget,
            current_plan, plan_runtime_state,
        )
    request_id = request_recorder(messages) if request_recorder else None
    decision = provider.complete(messages)
    return decision, request_id


def _run_subagent_once(
    handoff, provider, main_authority=None, memory_store=None,
    mcp_registry=None, context_assembler=None, context_budget=None,
    run_control=None, governance_state=None, clock=None,
    subagent_timeout_seconds=None, policy_binding=None, subagent_run_id=None,
):
    """Run exactly one isolated, in-process Subagent and return four fields."""
    validate_handoff(handoff)
    authority = _effective_subagent_authority(
        handoff["authority"], main_authority
    )
    if authority["max_steps"] <= 0:
        return _safe_result("blocked", "Subagent 没有可用 step authority", [], [])

    # This is the complete new conversation. Main Session/messages are neither
    # accepted nor copied. Workspace/evidence are explicitly labelled as hints.
    package = {
        "type": "structured_handoff",
        "handoff_id": handoff["handoff_id"],
        "task": handoff["task"],
        "context": handoff["context"],
        "constraints": handoff["constraints"],
        "evidence": handoff["evidence"],
        "workspace": handoff["workspace"],
        "authority": authority,
        "grounding_rule": (
            "workspace/evidence are hints; use an allowed tool observation for "
            "current reality, and prefer that observation on conflict"
        ),
    }
    messages = [{
        "role": "user",
        "content": json.dumps(package, ensure_ascii=False, separators=(",", ":")),
    }]
    verification = {
        "requires_verification": False,
        "latest_write_command": None,
        "verification_target": None,
    }
    observations = []
    actions = []
    inner_checkpoints = []
    child_run_id = subagent_run_id or new_run_id()

    def dispatch_child(capability, arguments, effect, policy, executor):
        checkpoint = create_action_checkpoint(capability, arguments, effect)
        authorized = authorize_action(
            checkpoint=checkpoint, capability=capability, arguments=arguments,
            effect=effect, policy_decision=policy["action"],
            approval_granted=False, run_id=child_run_id,
        )
        return dispatch_authorized_action(
            authorized, checkpoint,
            persist_checkpoint=lambda value: inner_checkpoints.append(value),
            executor=executor,
        )
    context_assembler = context_assembler or RuntimeContextAssembler(
        memory_store=memory_store,
        mcp_registry=mcp_registry if authority["can_use_mcp"] else None,
    )
    subagent_started = clock.monotonic() if clock is not None else None

    def subagent_remaining():
        if subagent_timeout_seconds is None or clock is None:
            return subagent_timeout_seconds
        return max(0.0, subagent_timeout_seconds - (
            clock.monotonic() - subagent_started
        ))

    try:
        for _step in range(1, authority["max_steps"] + 1):
            if subagent_remaining() is not None and subagent_remaining() <= 0:
                return _safe_result("blocked", "deadline exceeded", observations, actions)
            if run_control is not None and not can_schedule_action(run_control):
                return _safe_result(
                    "blocked", f"run control prevents new action: {run_control['state']}",
                    observations, actions,
                )
            decision, _request_id = _complete(
                provider, messages, context_assembler, verification,
                context_budget,
            )

            if decision.get("type") == "final_answer":
                if verification["requires_verification"]:
                    messages.append({
                        "role": "assistant",
                        "content": json.dumps(decision, ensure_ascii=False),
                    })
                    messages.append({
                        "role": "user",
                        "content": json.dumps({
                            "type": "verification_feedback",
                            "status": "final_answer_rejected",
                            "reason": "verification required before final answer",
                            "instruction": "use an allowed read-only observation",
                        }, ensure_ascii=False),
                    })
                    continue
                return _safe_result(
                    "completed", decision.get("final_answer", ""),
                    observations, actions,
                )
            if decision.get("type") != "tool_call":
                return _safe_result(
                    "failed", "Subagent 返回了不支持的决定", observations, actions,
                )

            if run_control is not None and not can_schedule_action(run_control):
                return _safe_result(
                    "blocked", f"run control prevents new action: {run_control['state']}",
                    observations, actions,
                )

            reference = decision.get("tool", "shell")
            if not _tool_allowed(reference, authority):
                return _safe_result(
                    "blocked", f"tool authority 不允许：{reference}",
                    observations, actions,
                )

            if reference.startswith("mcp:"):
                if mcp_registry is None:
                    return _safe_result(
                        "blocked", "MCP authority 已授予，但 registry 未配置",
                        observations, actions,
                    )
                policy = mcp_registry.policy_for(
                    reference, policy_binding.snapshot if policy_binding else None
                )
                if policy["action"] == POLICY_DENY:
                    return _safe_result(
                        "failed", "MCP action 被 Harness DENY policy 阻止",
                        observations, actions,
                    )
                if policy["action"] == POLICY_ASK:
                    return _safe_result(
                        "blocked", "human approval required", observations, actions,
                    )
                effect = mcp_registry.effect_for(
                    reference, policy_binding.snapshot if policy_binding else None
                )
                if verification["requires_verification"] and effect != MCP_EFFECT_READ_ONLY:
                    return _safe_result(
                        "blocked", "verification requires a read-only observation",
                        observations, actions,
                    )
                if not authority["can_write_workspace"] and effect != MCP_EFFECT_READ_ONLY:
                    return _safe_result(
                        "blocked", "workspace write authority 未授予",
                        observations, actions,
                    )
                timeout = subagent_remaining()
                if governance_state is not None:
                    timeout = effective_tool_timeout(governance_state, clock, timeout)
                arguments = decision.get("arguments", {})
                path_decision = inspect_mcp_paths(arguments)
                if not path_decision.allowed:
                    return _safe_result(
                        "blocked", path_decision.reason, observations, actions,
                    )
                dispatched = dispatch_child(
                    reference, arguments, effect, policy,
                    lambda normalized: execute_mcp_tool(
                        mcp_registry, reference, normalized, timeout
                    ),
                )
                observation = dispatched.raw_observation
                action = {"tool": reference, "outcome": observation["exit_code"]}
                if observation["exit_code"] == 0:
                    if verification["requires_verification"]:
                        verification["requires_verification"] = False
                    elif effect != MCP_EFFECT_READ_ONLY:
                        verification["requires_verification"] = True
                        verification["latest_write_command"] = reference
            else:
                command = decision.get("command", "")
                policy = classify_shell(
                    command, policy_binding.snapshot if policy_binding else None
                )
                if policy["action"] == POLICY_DENY:
                    return _safe_result(
                        "failed", "shell action 被 Harness DENY policy 阻止",
                        observations, actions,
                    )
                # V10 has no inherited or interactive approval. All currently
                # supported writes classify ASK, so no-write authority is enforced
                # before execution as well as by the global policy.
                if policy["action"] == POLICY_ASK:
                    reason = (
                        "workspace write authority 未授予"
                        if not authority["can_write_workspace"]
                        else "human approval required"
                    )
                    return _safe_result("blocked", reason, observations, actions)
                timeout = subagent_remaining()
                if governance_state is not None:
                    timeout = effective_tool_timeout(governance_state, clock, timeout)
                dispatched = dispatch_child(
                    "shell", {"command": command}, policy["effect"], policy,
                    lambda normalized: execute_shell(
                        normalized["command"], timeout
                    ),
                )
                observation = dispatched.raw_observation
                action = {"tool": "shell", "command": command,
                          "outcome": observation["exit_code"]}
                if (
                    observation["exit_code"] == 0
                    and verification["requires_verification"]
                ):
                    verification["requires_verification"] = False

            safe_observation = persisted_safe_observation(
                observation, reference,
                decision.get("arguments", {}) if reference.startswith("mcp:")
                else {"command": decision.get("command", "")},
            )
            observations.append(safe_observation)
            actions.append(action)
            messages.append({
                "role": "assistant",
                "content": json.dumps(decision, ensure_ascii=False),
            })
            messages.append({
                "role": "tool",
                "content": json.dumps(safe_observation, ensure_ascii=False),
            })
    except Exception as error:
        return _safe_result(
            "failed", f"Subagent runtime failure: {error}", observations, actions,
        )
    return _safe_result(
        "failed", f"达到 Subagent 最大步数 {authority['max_steps']}",
        observations, actions,
    )


def run_subagent(
    handoff, provider, main_authority=None, memory_store=None,
    mcp_registry=None, context_assembler=None, context_budget=None,
    current_action_checkpoint=None, save_action_checkpoint=None,
    return_contract=None, run_control=None, save_run_control=None,
    current_retry_state=None, save_retry_state=None,
    governance_state=None, save_governance_state=None, clock=None,
    requested_deadline_seconds=None,
    audit_writer=None, audit_directory=None, session_id=None,
    policy_binding=None, evidence_store=None,
):
    """Run one Subagent durably; V13 never recursively recovers a lost run."""
    validate_handoff(handoff)
    path_decision = inspect_subagent_paths(handoff)
    if not path_decision.allowed:
        return _safe_result("blocked", path_decision.reason, [], [])
    subagent_run_id = new_run_id()
    child_audit = None
    if audit_writer is not None:
        if policy_binding is None:
            parent_events = read_events(
                audit_writer.run_id, audit_writer.directory, missing_ok=True
            )
            parent_started = next((event for event in parent_events
                                   if event.get("event_type") == "run_started"), None)
            parent_references = (parent_started or {}).get("references") or {}
            policy_directory = os.path.join(audit_writer.directory, "policies")
            if parent_references.get("policy_fingerprint"):
                # A V19 parent must resolve its exact base snapshot. Missing or
                # corrupt history is a fail-closed error, never Current Policy.
                policy_binding = binding_from_events(parent_events, policy_directory)
            else:
                # Compatibility for direct V10-V18 run_subagent callers whose
                # parent Audit stream predates policy bindings.
                policy_binding = bind_current_policy(mcp_registry, policy_directory)
        child_audit = AuditWriter(
            session_id or audit_writer.session_id, subagent_run_id,
            audit_directory or audit_writer.directory,
        )
        audit_writer.append(
            "subagent_handoff", "harness", "subagent", "started",
            references={"handoff_id": handoff.get("handoff_id"),
                        "subagent_run_id": subagent_run_id},
        )
        child_audit.append(
            "run_started", "harness", "subagent", "running",
            references={"parent_run_id": audit_writer.run_id,
                        "handoff_id": handoff.get("handoff_id"),
                        "base_policy_fingerprint": policy_binding.fingerprint,
                        "policy_schema_version": policy_binding.schema_version,
                        "policy_revision": policy_binding.revision,
                        "policy_fingerprint": policy_binding.fingerprint,
                        "effective_policy_reference": effective_policy_reference(
                            policy_binding.fingerprint, handoff.get("authority", {})
                        )},
        )
    run_control = run_control if run_control is not None else create_run_control()
    validate_run_control(run_control)
    if not can_schedule_action(run_control):
        return _safe_result(
            "blocked", f"run control prevents Subagent start: {run_control['state']}",
            [], [],
        )
    effective_deadline = requested_deadline_seconds
    if governance_state is not None:
        validate_governance_state(governance_state)
        decision = normal_action_decision(governance_state, "subagent", clock)
        if not decision["allowed"]:
            return _safe_result("blocked", decision["reason"], [], [])
        if requested_deadline_seconds is None:
            requested_deadline_seconds = governance_state["tool_timeout_seconds"]
        effective_deadline = effective_subagent_timeout(
            governance_state, requested_deadline_seconds, clock
        )
        if effective_deadline <= 0:
            return _safe_result("blocked", "deadline exceeded", [], [])
        updated_governance = consume_action(governance_state, "subagent", clock)
        governance_state.clear()
        governance_state.update(updated_governance)
        if save_governance_state:
            save_governance_state(updated_governance)
    if current_retry_state is not None:
        validate_retry_state(current_retry_state)
    if current_action_checkpoint is not None:
        recovered, action = recover_action_checkpoint(current_action_checkpoint)
        if recovered != current_action_checkpoint and save_action_checkpoint:
            save_action_checkpoint(recovered)
        if recovered["state"] == "succeeded" and return_contract is not None:
            return return_contract
        if recovered["state"] in {"executing", "unknown", "succeeded"}:
            return _safe_result(
                "blocked", "Subagent crash recovery requires a persisted Return Contract",
                [], [],
            )
        if action == "return_to_plan":
            return _safe_result("blocked", "previous Subagent attempt failed", [], [])

    checkpoint = create_action_checkpoint(
        "subagent", {"handoff": handoff}, "unknown",
        replay_policy="never_auto_retry",
    )
    retry_state = create_retry_state(max_attempts=1)
    retry_state = start_attempt(retry_state)
    if save_retry_state:
        save_retry_state(retry_state)
    authorized = authorize_action(
        checkpoint=checkpoint, capability="subagent",
        arguments={"handoff": handoff}, effect="unknown",
        policy_decision=POLICY_ALLOW, approval_granted=False,
        run_id=subagent_run_id,
    )
    saved_checkpoints = []
    persist = save_action_checkpoint or saved_checkpoints.append
    result_holder = {}
    def execute_subagent(_arguments):
        result_holder["result"] = _run_subagent_once(
            handoff, provider, main_authority, memory_store, mcp_registry,
            context_assembler, context_budget,
            run_control, governance_state, clock, effective_deadline,
            policy_binding, subagent_run_id,
        )
        result = result_holder["result"]
        return {
            "status": result.get("status"),
            "exit_code": 0 if result.get("status") == "completed" else 1,
            "result": result.get("summary", ""),
        }
    dispatched = dispatch_authorized_action(
        authorized, checkpoint, persist_checkpoint=persist,
        executor=execute_subagent,
    )
    checkpoint = dispatched.checkpoint
    result = result_holder.get("result") or _safe_result(
        "blocked", dispatched.degraded_reason or "Subagent persistence degraded",
        [], [],
    )
    if result.get("status") == "completed":
        retry_state = complete_retry(retry_state)
    else:
        failure = classify_failure({
            "status": result.get("status"), "exit_code": 1,
            "error": result.get("summary", ""),
        })
        retry_state = record_failure(
            retry_state, failure["failure_class"], failure["reason_code"], "block"
        )
    if save_retry_state:
        save_retry_state(retry_state)
    settled = settle_control_boundary(run_control)
    if settled != run_control:
        run_control.clear()
        run_control.update(settled)
        if save_run_control:
            save_run_control(settled)
    if child_audit is not None:
        child_audit.append(
            "run_state_changed", "harness", "subagent", result.get("status"),
            reason=result.get("summary"),
            references={"handoff_id": handoff.get("handoff_id")},
        )
        audit_writer.append(
            "subagent_return", "subagent", "subagent", result.get("status"),
            references={"handoff_id": handoff.get("handoff_id"),
                        "subagent_run_id": subagent_run_id},
        )
        evidence_store = evidence_store or EvidenceStore(os.path.join(
            audit_directory or audit_writer.directory, "evidence"
        ))
        record = create_subagent_return_evidence(
            audit_writer.run_id,
            {"kind": "subagent_return", "target": handoff["handoff_id"],
             "claim": "candidate_returned"},
            handoff["handoff_id"], subagent_run_id, result.get("status"),
        )
        evidence_store.save(record)
        audit_writer.append(
            "evidence_created", "harness", "evidence", "created",
            references={"evidence_id": record["evidence_id"],
                        "evidence_fingerprint": record["evidence_fingerprint"]},
        )
    return result


# ==================== Agent Loop ====================

def run_agent(
    task, provider, max_steps=5, messages=None, verification=None,
    save_checkpoint=None, memory_store=None, mcp_registry=None,
    context_assembler=None, context_budget=None,
    current_plan=None, plan_revision_history=None,
    require_plan_grounding=False,
    current_action_checkpoint=None, save_action_checkpoint=None,
    run_control=None, save_run_control=None,
    current_retry_state=None, save_retry_state=None, retry_sleeper=None,
    governance_state=None, save_governance_state=None, clock=None,
    step_timeout_seconds=None,
    audit_writer=None, session_id=None, audit_directory=None,
    policy_binding=None, previous_run_id=None,
    previous_policy_fingerprint=None,
    evidence_store=None,
    output_contract=None, artifact_store=None, output_contract_store=None,
    result_store=None, return_result=False, fault_injector=None,
    late_mcp_completion_journal=None,
):
    """Harness 行为：驱动模型、工具和 observation 之间的循环。"""
    run_id = audit_writer.run_id if audit_writer is not None else new_run_id()
    if audit_writer is None and session_id is not None:
        audit_writer = AuditWriter(session_id, run_id, audit_directory) if audit_directory else AuditWriter(session_id, run_id)
    if evidence_store is None and audit_writer is not None:
        evidence_store = EvidenceStore(os.path.join(
            audit_writer.directory, "evidence"
        ))
    if output_contract is not None:
        if evidence_store is None:
            evidence_store = EvidenceStore()
        artifact_store = artifact_store or ArtifactStore(os.path.join(
            audit_writer.directory if audit_writer is not None else
            (audit_directory or os.path.join(os.getcwd(), ".audit")),
            "artifacts",
        ))
        output_contract_store = output_contract_store or OutputContractStore(
            os.path.join(
                audit_writer.directory if audit_writer is not None else
                (audit_directory or os.path.join(os.getcwd(), ".audit")),
                "output_contracts",
            )
        )
        output_contract = create_output_contract(run_id, output_contract)
        output_contract_store.save(output_contract)

    tool_has_returned = False

    def audit(event_type, actor, subject=None, outcome=None, reason=None,
              references=None, summary=None):
        if audit_writer is not None:
            try:
                return audit_writer.append(
                    event_type, actor, subject, outcome, reason, references, summary
                )
            except Exception as error:
                if not tool_has_returned:
                    raise
                mark_degraded(f"audit_append_after_tool: {type(error).__name__}")
        return None

    if policy_binding is None:
        policy_directory = os.path.join(
            audit_writer.directory if audit_writer is not None else
            (audit_directory or os.path.join(os.getcwd(), ".audit")),
            "policies",
        )
        policy_binding = bind_current_policy(mcp_registry, policy_directory)

    def persist_evidence(record, step_id=None, accepted=None):
        if evidence_store is None:
            return None
        trigger_fault(fault_injector, "after_session_before_evidence")
        try:
            evidence_store.save(record)
        except Exception as error:
            mark_degraded(
                f"evidence_persist: {type(error).__name__}", "evidence",
            )
            return None
        references = {
            "evidence_id": record["evidence_id"],
            "evidence_fingerprint": record["evidence_fingerprint"],
        }
        if step_id:
            references["step_id"] = step_id
        target = record["verification"].get("verification_target")
        if target is not None:
            references["verification_target"] = target
        try:
            audit("evidence_created", "harness", "evidence", "created",
                  references=references)
            if accepted is not None:
                audit("evidence_accepted" if accepted else "evidence_rejected",
                      "harness", "evidence", "accepted" if accepted else "rejected",
                      references=references)
        except Exception as error:
            mark_degraded(
                f"audit_append_after_evidence: {type(error).__name__}",
                "audit",
            )
        return record["evidence_id"]
    memory_store = memory_store or MemoryStore()
    context_assembler = context_assembler or RuntimeContextAssembler(
        memory_store=memory_store, mcp_registry=mcp_registry
    )
    messages = messages if messages is not None else []
    verification = verification if verification is not None else {
        "requires_verification": False,
        "latest_write_command": None,
        "verification_target": None,
        "degraded": False,
        "degraded_reason": None,
        "degraded_stage": None,
    }
    verification.setdefault("degraded", False)
    verification.setdefault("degraded_reason", None)
    verification.setdefault("degraded_stage", None)

    def mark_degraded(reason, stage=None):
        verification["degraded"] = True
        verification["degraded_reason"] = str(reason)[:240]
        verification["degraded_stage"] = (
            stage or str(reason).partition("_")[0].partition(":")[0]
        )[:64]
        if save_checkpoint:
            try:
                save_checkpoint()
            except Exception:
                pass
    run_control = run_control if run_control is not None else create_run_control()
    validate_run_control(run_control)
    started_references = {
        "policy_schema_version": policy_binding.schema_version,
        "policy_revision": policy_binding.revision,
        "policy_fingerprint": policy_binding.fingerprint,
    }
    if output_contract is not None:
        started_references["output_contract_fingerprint"] = output_contract[
            "contract_fingerprint"
        ]
    if audit_writer is not None:
        manifest_store = RunManifestStore(os.path.join(
            audit_writer.directory, "manifests"
        ))
        configuration = build_configuration(
            task, provider, policy_binding, context_assembler, context_budget
        )
        manifest = build_manifest(
            run_id, audit_writer.session_id, configuration
        )
        # Persistence precedes run_started: audit can never bind an unpublished
        # manifest during normal execution.
        manifest_store.persist(manifest)
        started_references["manifest_fingerprint"] = manifest[
            "configuration_fingerprint"
        ]
        envelope_store = RunEnvelopeStore(os.path.join(
            audit_writer.directory, "envelopes"
        ))
        envelope = build_envelope(
            run_id, audit_writer.session_id, task, messages or [], manifest,
            current_plan=current_plan,
            control_state={
                "verification": verification,
                "run_control": run_control,
                "retry_state": current_retry_state,
                "governance_state": governance_state,
                "output_contract": output_contract,
            },
        )
        envelope_store.persist(envelope)
        started_references["envelope_fingerprint"] = envelope[
            "envelope_fingerprint"
        ]
    else:
        envelope_store = None
    if previous_run_id is not None:
        previous_manifest_fingerprint = None
        if audit_writer is not None:
            try:
                previous_manifest = manifest_store.load(previous_run_id)
                previous_manifest_fingerprint = previous_manifest[
                    "configuration_fingerprint"
                ]
            except ValueError:
                pass  # V19 and older runs have no manifest.
        started_references.update({
            "previous_run_id": previous_run_id,
            "previous_policy_fingerprint": previous_policy_fingerprint,
            "policy_drift": previous_policy_fingerprint != policy_binding.fingerprint,
            "previous_manifest_fingerprint": previous_manifest_fingerprint,
            "runtime_drift": (
                previous_manifest_fingerprint is not None
                and previous_manifest_fingerprint
                != started_references.get("manifest_fingerprint")
            ),
        })
    audit("run_started", "harness", "run", "running",
          references=started_references)
    verification_obligation = bool(verification["requires_verification"])
    if audit_writer is not None:
        result_store = result_store or ResultStore(os.path.join(
            audit_writer.directory, "results"
        ))

    def emit_result(candidate=None, terminal_failure=None,
                    blocking_reason=None, legacy_value=None):
        """Bind one terminal boundary and preserve the legacy string API."""
        if tool_has_returned:
            trigger_fault(fault_injector, "after_artifact_before_result")
        if (verification.get("degraded") and terminal_failure is None
                and blocking_reason is None):
            blocking_reason = verification.get("degraded_reason") or "persistence degraded"
        output_status = None
        if output_contract is not None:
            output_status = current_output_contract_gate(
                run_id, output_contract_store, artifact_store, evidence_store
            )
        state, normalized = build_authoritative_result_state(
            run_id, candidate, run_control, terminal_failure,
            blocking_reason, current_plan, output_status,
            verification_obligation,
            artifact_store, evidence_store,
            audit_writer.directory if audit_writer is not None else
            (audit_directory or os.path.join(os.getcwd(), ".audit")),
        )
        result, binding = bind_final_result(state, normalized)
        if envelope_store is not None:
            envelope_store.append_transition(
                run_id, "result_binding", state, binding, idempotent=True,
            )
        result_identity = answer_identity(result["answer"])
        if result["candidate"]["contradiction"]:
            audit(
                "final_candidate_rejected", "harness", "final_answer",
                "rejected", result["reason"],
                references={
                    "answer_length": result["candidate"]["answer_length"],
                    "answer_sha256": result["candidate"]["answer_sha256"],
                    "claimed_status": result["candidate"]["claimed_status"],
                    "authoritative_status": result["status"],
                    "artifact_ids": result["artifact_ids"],
                    "evidence_ids": result["evidence_ids"],
                    "contradiction": True,
                },
            )
        if result_store is not None:
            try:
                result_store.save(result)
            except Exception as error:
                mark_degraded(
                    f"result_persist: {type(error).__name__}", "result",
                )
                degraded_state, degraded_candidate = build_authoritative_result_state(
                    run_id, candidate, run_control, terminal_failure,
                    verification["degraded_reason"], current_plan, output_status,
                    verification_obligation, artifact_store, evidence_store,
                    audit_writer.directory if audit_writer is not None else
                    (audit_directory or os.path.join(os.getcwd(), ".audit")),
                )
                degraded_result, _binding = bind_final_result(
                    degraded_state, degraded_candidate
                )
                return degraded_result if return_result else "incomplete: persistence degraded"
        try:
            audit(
                "final_result_emitted", "harness", "result", result["status"],
                result["reason"],
                references={
                    **result_identity,
                    "claimed_status": result["candidate"]["claimed_status"],
                    "authoritative_status": result["status"],
                    "artifact_ids": result["artifact_ids"],
                    "evidence_ids": result["evidence_ids"],
                    "contradiction": result["candidate"]["contradiction"],
                    "result_fingerprint": result["result_fingerprint"],
                },
            )
            audit(
                "run_state_changed", "harness", "run", result["status"],
                result["reason"],
            )
        except Exception as error:
            mark_degraded(
                f"audit_append_after_result: {type(error).__name__}", "audit",
            )
        if return_result:
            return result
        if result["candidate"]["contradiction"]:
            explicit_claim = bool(
                result["candidate"]["claimed_status"] is not None
                or result["candidate"]["artifact_refs"]
                or result["candidate"]["evidence_refs"]
            )
            if explicit_claim or not result["candidate"]["answer_allowed"]:
                return result["answer"]
        return result["answer"] if legacy_value is None else legacy_value

    safety_entry = bool(
        current_action_checkpoint
        and current_action_checkpoint.get("state") in {"executing", "unknown"}
        and current_action_checkpoint.get("effect") in {"side_effecting", "unknown"}
    )
    if not can_schedule_action(run_control) and not safety_entry:
        legacy = f"run {run_control['state']}"
        return emit_result(blocking_reason=(
            None if run_control["state"] in {"cancel_requested", "cancelled"}
            else run_control.get("reason") or legacy
        ), legacy_value=legacy)
    if governance_state is not None:
        validate_governance_state(governance_state)
        decision = normal_action_decision(governance_state, clock=clock)
        if not decision["allowed"] and not safety_entry:
            legacy = f"blocked: {decision['reason']}"
            return emit_result(
                blocking_reason=decision["reason"], legacy_value=legacy
            )
    messages.append({"role": "user", "content": task})
    if save_checkpoint:
        save_checkpoint()
    requires_verification = verification["requires_verification"]
    latest_write_command = verification.get("latest_write_command")
    verification_target = verification.get("verification_target")
    rejected_final_answer = None
    plan_revision_history = (
        plan_revision_history if plan_revision_history is not None else []
    )
    current_step_id = None
    plan_had_action = False
    plan_evidence = []
    plan_evidence_ids = []
    pending_verification_action_id = None
    pending_artifact = None
    plan_runtime_state = {
        "requires_fresh_grounding": bool(require_plan_grounding),
    }
    recovered_action = None
    action_checkpoint = current_action_checkpoint

    if current_retry_state is not None:
        validate_retry_state(current_retry_state)

    def persist_retry(value):
        nonlocal current_retry_state
        current_retry_state = value
        if save_retry_state:
            save_retry_state(value)

    def persist_governance(value):
        if governance_state is None:
            return
        governance_state.clear()
        governance_state.update(value)
        if save_governance_state:
            save_governance_state(value)

    def consume_normal_action(kind="tool"):
        if governance_state is None:
            return None
        decision = normal_action_decision(governance_state, kind, clock)
        if not decision["allowed"]:
            return decision["reason"]
        persist_governance(consume_action(governance_state, kind, clock))
        return None

    def dispatch_shell(action_checkpoint, command, policy, approved=True):
        nonlocal last_observation_event_id
        action_refs = {"action_id": action_checkpoint["action_id"]}
        if action_refs is not None and current_retry_state is not None:
            action_refs.update({"logical_action_id": current_retry_state["logical_action_id"],
                                "attempt": current_retry_state["attempt_count"]})
        authorized = authorize_action(
            checkpoint=action_checkpoint, capability="shell",
            arguments={"command": command}, effect=policy["effect"],
            policy_decision=policy["action"], approval_granted=approved,
            run_id=run_id,
        )
        def execute(arguments):
            nonlocal tool_has_returned
            if governance_state is None:
                value = execute_shell(arguments["command"])
            else:
                value = execute_shell(
                    arguments["command"],
                    effective_tool_timeout(governance_state, clock),
                )
            tool_has_returned = True
            return value
        def after(_action, observation, _terminal):
            nonlocal last_observation_event_id
            event = audit(
                "action_state_changed", "environment", "shell",
                "succeeded" if observation.get("exit_code") == 0 else "failed",
                references=action_refs,
                summary=safe_observation_summary(observation),
            )
            last_observation_event_id = event.get("event_id") if event else None
        outcome = dispatch_authorized_action(
            authorized, action_checkpoint, persist_checkpoint=persist_action,
            executor=execute,
            before_dispatch=lambda _action: audit(
                "action_state_changed", "tool", "shell", "started",
                references=action_refs,
            ),
            after_dispatch=after,
            fault_injector=fault_injector,
        )
        if outcome.degraded:
            mark_degraded(outcome.degraded_reason, outcome.degraded_stage)
        return outcome

    last_observation_event_id = None

    def dispatch_mcp(action_checkpoint, reference, arguments, policy, effect,
                     approved=True):
        nonlocal last_observation_event_id
        action_refs = {"action_id": action_checkpoint["action_id"]}
        if action_refs is not None and current_retry_state is not None:
            action_refs.update({"logical_action_id": current_retry_state["logical_action_id"],
                                "attempt": current_retry_state["attempt_count"]})
        authorized = authorize_action(
            checkpoint=action_checkpoint, capability=reference,
            arguments=arguments, effect=effect,
            policy_decision=policy["action"], approval_granted=approved,
            run_id=run_id,
        )
        def execute(normalized):
            nonlocal tool_has_returned
            if governance_state is None:
                if late_mcp_completion_journal is None:
                    value = execute_mcp_tool(mcp_registry, reference, normalized)
                else:
                    value = execute_mcp_tool(
                        mcp_registry, reference, normalized,
                        late_completion_journal=late_mcp_completion_journal,
                        action_id=action_checkpoint["action_id"],
                        call_id=action_checkpoint["action_id"],
                        run_state=run_control["state"],
                    )
            else:
                timeout = effective_tool_timeout(governance_state, clock)
                if late_mcp_completion_journal is None:
                    value = execute_mcp_tool(
                        mcp_registry, reference, normalized, timeout,
                    )
                else:
                    value = execute_mcp_tool(
                        mcp_registry, reference, normalized, timeout,
                        late_completion_journal=late_mcp_completion_journal,
                        action_id=action_checkpoint["action_id"],
                        call_id=action_checkpoint["action_id"],
                        run_state=(
                            "deadline_exceeded"
                            if deadline_status(governance_state, clock) else
                            run_control["state"]
                        ),
                    )
            tool_has_returned = True
            return value
        def after(_action, observation, _terminal):
            nonlocal last_observation_event_id
            event = audit(
                "mcp_called", "mcp", reference,
                "succeeded" if observation.get("exit_code") == 0 else "failed",
                references=action_refs,
                summary=safe_observation_summary(observation),
            )
            last_observation_event_id = event.get("event_id") if event else None
        outcome = dispatch_authorized_action(
            authorized, action_checkpoint, persist_checkpoint=persist_action,
            executor=execute,
            before_dispatch=lambda _action: audit(
                "action_state_changed", "mcp", reference, "started",
                references=action_refs,
            ),
            after_dispatch=after,
            fault_injector=fault_injector,
        )
        if outcome.degraded:
            mark_degraded(outcome.degraded_reason, outcome.degraded_stage)
        return outcome

    def begin_attempt():
        nonlocal current_retry_state
        if current_retry_state is None or current_retry_state["state"] in {"completed", "blocked", "exhausted"}:
            current_retry_state = create_retry_state(current_step_id)
        current_retry_state = start_attempt(current_retry_state)
        persist_retry(current_retry_state)

    def finish_or_decide_retry(observation, effect, replay_policy):
        nonlocal current_retry_state
        if observation.get("exit_code") == 0:
            current_retry_state = complete_retry(current_retry_state)
            persist_retry(current_retry_state)
            return "no_retry"
        failure = classify_failure(observation)
        policy = decide_retry(
            failure["failure_class"], effect, replay_policy,
            current_retry_state["attempt_count"], current_retry_state["max_attempts"],
            run_control["state"],
        )
        current_retry_state = record_failure(
            current_retry_state, failure["failure_class"],
            failure["reason_code"], policy,
        )
        persist_retry(current_retry_state)
        if envelope_store is not None:
            envelope_store.append_transition(
                run_id, "retry", {
                    "failure_class": failure["failure_class"],
                    "effect": effect, "replay_policy": replay_policy,
                    "attempt_count": current_retry_state["attempt_count"],
                    "max_attempts": current_retry_state["max_attempts"],
                    "run_state": run_control["state"],
                    "reconciliation_status": (
                        "required" if policy == "reconcile_before_retry" else "not_required"
                    ),
                    "historical_recorded_observation": True,
                }, {
                    "decision": policy,
                    "next_delay": current_retry_state["backoff_delay"],
                },
            )
        audit(
            "retry_decision", "harness", "action",
            "scheduled" if policy == "retry_with_backoff" else "exhausted",
            reason=f"failure_class={failure['failure_class']}; policy={policy}",
            references={"logical_action_id": current_retry_state["logical_action_id"],
                        "attempt": current_retry_state["attempt_count"]},
        )
        return policy

    def persist_action(value):
        nonlocal current_action_checkpoint
        current_action_checkpoint = value
        if save_action_checkpoint:
            save_action_checkpoint(value)

    def ask_approval(subject, reason, audit_subject=None):
        persisted_subject = subject if audit_subject is None else audit_subject
        audit("approval_requested", "harness", persisted_subject, "pending", reason)
        approved = request_approval(
            subject, reason, run_control, save_run_control,
            governance_state, save_governance_state, clock,
        )
        audit("approval_decided", "user", persisted_subject,
              "granted" if approved else "rejected")
        return approved

    def settle_run_control():
        updated = settle_control_boundary(run_control)
        if updated != run_control:
            run_control.clear()
            run_control.update(updated)
            if save_run_control:
                save_run_control(updated)
        if (
            governance_state is not None
            and run_control["state"] == "paused"
            and not governance_state["frozen"]
        ):
            persist_governance(freeze_governance(
                governance_state, "user_pause", clock
            ))
        return run_control["state"]

    def scheduling_allowed():
        if not can_schedule_action(run_control):
            return False
        return governance_state is None or normal_action_decision(
            governance_state, clock=clock
        )["allowed"]

    def deadline_block(reason):
        if current_plan is not None and current_step_id is not None and current_plan["status"] == "active":
            blocked = block_step(current_plan, current_step_id)
            blocked_step = next(item for item in blocked["steps"] if item["id"] == current_step_id)
            blocked_step["evidence"].append({"kind": "governance", "summary": reason})
            current_plan.clear()
            current_plan.update(blocked)
            checkpoint()
        return f"blocked: {reason}"

    def stop_for_replan_if_needed(retry_decision):
        if retry_decision != "replan" or current_plan is None:
            return None
        outcome = retry_exhausted_outcome(current_plan, current_step_id)
        if outcome["action"] == "block":
            current_plan.clear()
            current_plan.update(outcome["plan"])
            checkpoint()
            return "blocked: retry exhausted and replan limit reached"
        plan_runtime_state["retry_exhausted"] = True
        checkpoint()
        return "replan required: action strategy is no longer suitable"

    if current_action_checkpoint is not None:
        validate_action_checkpoint(current_action_checkpoint)
        recovered_action, recovery_action = recover_action_checkpoint(
            current_action_checkpoint
        )
        if recovered_action != current_action_checkpoint:
            persist_action(recovered_action)
        plan_runtime_state["requires_fresh_grounding"] = True
        if recovery_action in {
            "retry_with_fresh_approval", "retry_as_new_action", "reconcile_or_block",
        }:
            plan_runtime_state["action_recovery"] = recovery_control_state(
                recovered_action
            )
        if recovered_action["state"] == "succeeded" and current_plan is not None:
            plan_evidence.append({
                "kind": "action_checkpoint",
                "summary": "persisted successful action observation",
                "verified": True,
                "action_id": recovered_action["action_id"],
            })

    def checkpoint():
        verification["requires_verification"] = requires_verification
        verification["latest_write_command"] = latest_write_command
        verification["verification_target"] = verification_target
        if save_checkpoint:
            try:
                save_checkpoint()
            except Exception as error:
                if not tool_has_returned and not verification.get("degraded"):
                    raise
                verification["degraded"] = True
                verification["degraded_reason"] = (
                    verification.get("degraded_reason")
                    or f"session_persist: {type(error).__name__}"
                )
                verification["degraded_stage"] = (
                    verification.get("degraded_stage") or "session"
                )

    def finalize_artifact_candidate(verification_record, evidence_id,
                                    verification_accepted):
        """Persist one immutable historical version after reused V22 verification."""
        nonlocal pending_artifact
        if pending_artifact is None or artifact_store is None:
            return None
        candidate = pending_artifact
        required = next((item for item in output_contract["required_artifacts"]
                         if item["artifact_type"] == "workspace_file"
                         and item["path"] == pending_artifact["path"]), None)
        draft = create_artifact(
            run_id, pending_artifact["path"], "materialized",
            pending_artifact["content_identity"], pending_artifact["producer"],
            [evidence_id] if evidence_id else [], required or {},
            references={"verification_accepted": bool(verification_accepted)},
        )
        previous = select_supersession(
            draft, run_id, artifact_store,
            evidence_store.directory if evidence_store is not None else None,
            audit_writer.directory if audit_writer is not None else
            (audit_directory or os.path.join(os.getcwd(), ".audit")),
        )
        if previous is not None:
            draft = create_artifact(
                run_id, pending_artifact["path"], "materialized",
                pending_artifact["content_identity"], pending_artifact["producer"],
                [evidence_id] if evidence_id else [], required or {},
                references={"verification_accepted": bool(verification_accepted)},
                supersedes_artifact_id=previous["artifact_id"],
                artifact_id=draft["artifact_id"], created_at=draft["created_at"],
            )
        transition_inputs = None
        if required is None:
            status = "verified" if verification_accepted else "rejected"
            result = {"accepted": False, "status": status,
                      "reason": "not required by Output Contract",
                      "unsatisfied_requirements": []}
        else:
            transition_inputs, result = evaluate_artifact_contract(
                draft, [verification_record] if verification_record else [], required
            )
            status = result["status"]
        record = create_artifact(
            run_id, pending_artifact["path"], status,
            pending_artifact["content_identity"], pending_artifact["producer"],
            [evidence_id] if evidence_id else [], required or {},
            references={"contract_result": result},
            supersedes_artifact_id=(previous["artifact_id"] if previous else None),
            artifact_id=draft["artifact_id"], created_at=draft["created_at"],
        )
        trigger_fault(fault_injector, "after_evidence_before_artifact")
        try:
            artifact_store.save(record)
        except Exception as error:
            mark_degraded(
                f"artifact_persist: {type(error).__name__}", "artifact",
            )
            return None
        refs = {
            "artifact_id": record["artifact_id"],
            "artifact_fingerprint": record["artifact_fingerprint"],
            "path": record["path"], "status": record["status"],
            "evidence_ids": list(record["evidence_ids"]),
        }
        try:
            audit("artifact_proposed", "harness", "artifact", "proposed",
                  references=refs)
            audit("artifact_materialized", "harness", "artifact", "materialized",
                  references=refs)
            if verification_accepted:
                audit("artifact_verified", "harness", "artifact", "verified",
                      references=refs)
            if status in {"accepted", "rejected"}:
                audit("artifact_accepted" if status == "accepted" else "artifact_rejected",
                      "harness", "artifact", status, result.get("reason"), references=refs)
            if previous is not None:
                audit("artifact_superseded", "harness", "artifact", "superseded",
                      references={**refs, "superseded_artifact_id": previous["artifact_id"]})
        except Exception as error:
            mark_degraded(f"audit_append_after_artifact: {type(error).__name__}")
        if envelope_store is not None and transition_inputs is not None:
            envelope_store.append_transition(
                run_id, "artifact_contract", transition_inputs, result
            )
        # A failed read-only check records rejection but keeps the materialized
        # candidate available for a later fresh Verification attempt.  The next
        # immutable record will supersede this rejected attempt.
        pending_artifact = None if verification_accepted else candidate
        return record

    if current_plan is not None:
        validate_plan(current_plan)
        validate_revision_history(plan_revision_history)
        audit(
            "plan_created", "harness", "plan", current_plan["status"],
            references={"plan_id": current_plan["plan_id"],
                        "plan_version": current_plan["version"]},
        )
        if current_plan["status"] != "active":
            if current_plan["status"] == "failed":
                return emit_result(
                    terminal_failure="plan failed",
                    legacy_value="failed: plan failed",
                )
            if current_plan["status"] == "blocked":
                return emit_result(
                    blocking_reason="plan blocked",
                    legacy_value="blocked: plan blocked",
                )
            return emit_result(
                legacy_value="incomplete: final candidate missing"
            )
        current = next((
            item for item in current_plan["steps"]
            if item["status"] == "in_progress"
        ), None)
        if current is None:
            ready = select_ready_step(current_plan)
            if envelope_store is not None:
                envelope_store.append_transition(
                    run_id, "planning", planning_transition_input(current_plan),
                    {"selected_step_id": ready["id"] if ready else None},
                )
            if ready is None:
                failure = "active plan 没有 ready step"
                terminal = emit_result(
                    blocking_reason=failure,
                    legacy_value=f"blocked: {failure}",
                )
                if return_result:
                    return terminal
                raise RuntimeError(failure)
            started = start_step(current_plan, ready["id"])
            current_plan.clear()
            current_plan.update(started)
            current = ready
        current_step_id = current["id"]
        audit(
            "plan_step_changed", "harness", "plan_step", "started",
            references={"plan_id": current_plan["plan_id"],
                        "step_id": current_step_id},
        )
        if governance_state is not None and step_timeout_seconds is not None and governance_state["step_deadline_at"] is None:
            persist_governance(start_step_deadline(
                governance_state, step_timeout_seconds, clock
            ))
        checkpoint()

    for step in range(1, max_steps + 1):
        if not scheduling_allowed() and not safety_entry:
            settle_run_control()
            reason = deadline_status(governance_state, clock) if governance_state is not None else None
            legacy = deadline_block(reason) if reason else f"run {run_control['state']}"
            return emit_result(
                blocking_reason=reason or run_control.get("reason") or legacy,
                legacy_value=legacy,
            )
        print(f"\n[Harness] 第 {step}/{max_steps} 步：请求模型做决定")
        try:
            decision, request_id = _complete(provider, messages, context_assembler, {
                "requires_verification": requires_verification,
                "latest_write_command": latest_write_command,
                "verification_target": verification_target,
                "action_recovery": plan_runtime_state.get("action_recovery"),
                "run_control": run_control,
                "retry_state": current_retry_state,
                "governance_state": governance_state,
                "output_contract": output_contract,
                "clock": clock,
                "safety_reconciliation": safety_entry,
            }, context_budget, current_plan, plan_runtime_state,
            request_recorder=(
                lambda prepared: envelope_store.append_request(
                    run_id, prepared,
                    any("deterministic_compacted_history" in item.get("content", "")
                        for item in prepared),
                )["request_id"]
                if envelope_store is not None else None
            ))
        except ProviderError:
            failure = "provider terminal failure"
            terminal = emit_result(
                terminal_failure=failure, legacy_value=failure
            )
            if return_result:
                return terminal
            raise
        decision_event = audit(
            "model_decision", "model", decision.get("type"), decision.get("type")
        )
        if envelope_store is not None:
            envelope_store.bind_decision(
                run_id, request_id, decision,
                decision_event.get("event_id") if decision_event else None,
            )

        if decision.get("type") == "memory_candidate":
            audit("memory_decision", "model", "memory", "candidate")
            if (
                set(decision) != {"type", "kind", "content"}
                or decision.get("kind") not in MEMORY_KINDS
            ):
                allowed, reason = False, "memory candidate schema 或 kind 无效"
            else:
                allowed, reason = screen_memory_content(decision.get("content"))
            if not allowed:
                feedback = {
                    "type": "memory_feedback",
                    "status": "memory not saved",
                    "denied_by": "memory_policy",
                    "reason": reason,
                }
                print(f"[Memory Policy] DENY：{reason}")
                audit("memory_decision", "harness", "memory", "rejected", reason)
            elif request_memory_approval(decision):
                try:
                    memory_store.add(decision["kind"], decision["content"])
                except (OSError, ValueError) as error:
                    feedback = {
                        "type": "memory_feedback",
                        "status": "memory not saved",
                        "denied_by": "memory_store",
                        "reason": str(error),
                    }
                    print(f"[Memory] memory not saved：{error}")
                else:
                    feedback = {
                        "type": "memory_feedback", "status": "memory saved",
                    }
                    print("[Memory] memory saved")
                    audit("memory_decision", "harness", "memory", "saved")
            else:
                feedback = {
                    "type": "memory_feedback", "status": "memory not saved",
                }
                print("[Memory] memory not saved")
                audit("memory_decision", "user", "memory", "rejected")
            recorded_decision = decision if allowed else {
                "type": "memory_candidate", "status": "rejected_by_memory_policy",
            }
            candidate_record = {
                "role": "assistant",
                "content": json.dumps(recorded_decision, ensure_ascii=False),
            }
            messages.append(candidate_record)
            messages.append({
                "role": "user",
                "content": json.dumps(feedback, ensure_ascii=False),
            })
            checkpoint()
            continue

        if decision.get("type") == "final_answer":
            candidate_metadata = normalize_final_candidate(decision)["metadata"]
            candidate_references = {
                "answer_length": candidate_metadata["answer_length"],
                "answer_sha256": candidate_metadata["answer_sha256"],
                "claimed_status": candidate_metadata["claimed_status"],
                "artifact_ids": candidate_metadata["artifact_refs"],
                "evidence_ids": candidate_metadata["evidence_refs"],
            }
            audit(
                "final_candidate_received", "model", "final_answer",
                "received", references=candidate_references,
            )
            if requires_verification:
                if decision == rejected_final_answer:
                    failure = (
                        "模型在没有新 tool_call 的情况下重复提交了被 Verification "
                        "Gate 拒绝的 final_answer"
                    )
                    terminal = emit_result(
                        decision, terminal_failure=failure,
                        legacy_value=failure,
                    )
                    if return_result:
                        return terminal
                    raise RuntimeError(failure)
                audit(
                    "final_candidate_rejected", "harness", "final_answer",
                    "rejected", "verification required before final answer",
                    references={
                        **candidate_references,
                        "authoritative_status": "incomplete",
                        "contradiction": True,
                    },
                )
                feedback = build_verification_feedback(
                    latest_write_command, verification_target, POLICY_ALLOW
                )
                messages.append({
                    "role": "assistant",
                    "content": json.dumps(decision, ensure_ascii=False),
                })
                messages.append({
                    "role": "user",
                    "content": json.dumps(feedback, ensure_ascii=False),
                })
                rejected_final_answer = decision
                checkpoint()
                print("[Verification Gate] verification required before final answer")
                audit(
                    "verification_state_changed", "harness", "final_answer",
                    "required", "verification required before final answer",
                )
                continue
            answer = decision.get("answer", decision.get("final_answer", ""))
            if output_contract is not None:
                output_gate = current_output_contract_gate(
                    run_id, output_contract_store, artifact_store, evidence_store
                )
                if not output_gate["satisfied"]:
                    checkpoint()
                    return emit_result(
                        decision,
                        legacy_value="incomplete: output contract unsatisfied",
                    )
            if current_plan is not None:
                proposal = propose_step_completion(
                    current_plan, current_step_id, answer
                )
                if (evidence_store is not None and not plan_had_action
                        and not plan_runtime_state["requires_fresh_grounding"]):
                    decision_digest = hashlib.sha256(json.dumps(
                        decision, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"),
                    ).encode()).hexdigest()
                    reasoning = create_reasoning_evidence(
                        run_id,
                        {"kind": "plan_step", "target": current_step_id,
                         "claim": "reasoning_completed"},
                        decision_event.get("event_id"), decision_digest,
                        {"status": "completed"},
                        references={"model_request_id": request_id}
                        if request_id else {},
                    )
                    plan_evidence_ids.append(persist_evidence(
                        reasoning, current_step_id, True,
                    ))
                if plan_evidence_ids:
                    completed = complete_step(
                        current_plan, current_step_id, plan_evidence_ids,
                        evidence_store=evidence_store,
                        current_run_id=run_id,
                        current_reality=plan_had_action,
                        audit_directory=(audit_writer.directory
                                         if audit_writer is not None else None),
                    )
                    accepted_evidence = None
                else:
                    completed = None
                if plan_evidence:
                    accepted_evidence = plan_evidence
                elif plan_had_action:
                    accepted_evidence = []
                elif plan_runtime_state["requires_fresh_grounding"]:
                    accepted_evidence = []
                else:
                    accepted_evidence = [{
                        "kind": "textual_result",
                        "summary": proposal["result"],
                    }]
                if completed is None and not accepted_evidence:
                    feedback = {
                        "type": "plan_feedback",
                        "status": "step_completion_rejected",
                        "step_id": current_step_id,
                        "reason": "fresh accepted evidence required",
                        "instruction": (
                            "Use an allowed observation relevant to the current "
                            "step before returning final_answer again."
                        ),
                    }
                    messages.append({
                        "role": "assistant",
                        "content": json.dumps(decision, ensure_ascii=False),
                    })
                    messages.append({
                        "role": "user",
                        "content": json.dumps(feedback, ensure_ascii=False),
                    })
                    checkpoint()
                    print("[Plan Evidence Gate] step completion requires evidence")
                    continue
                if completed is None:
                    completed = complete_step(
                        current_plan, current_step_id, accepted_evidence
                    )
                current_plan.clear()
                current_plan.update(completed)
                print(f"[Plan] step completed：{current_step_id}")
                audit(
                    "plan_step_changed", "harness", "plan_step", "completed",
                    references={"plan_id": current_plan["plan_id"],
                                "step_id": current_step_id},
                )
            if result_store is None and screen_result_answer(answer)[0]:
                # Legacy in-memory runs have no Result Store; keep their
                # historical continuity. Persisted V24 runs store answer text
                # only in the immutable Result record.
                messages.append({
                    "role": "assistant",
                    "content": json.dumps(decision, ensure_ascii=False),
                })
            checkpoint()
            final_value = emit_result(decision, legacy_value=answer)
            safe_answer = (
                final_value["answer"]
                if isinstance(final_value, dict) else final_value
            )
            print(f"[Harness Final Result] {safe_answer}")
            return final_value

        if decision.get("type") == "tool_call" and str(
            decision.get("tool", "")
        ).startswith("mcp:"):
            if not scheduling_allowed():
                settle_run_control()
                legacy = f"run {run_control['state']}"
                return emit_result(
                    blocking_reason=run_control.get("reason") or legacy,
                    legacy_value=legacy,
                )
            reference = decision.get("tool")
            mcp_verification_was_required = requires_verification
            mcp_historical_target = verification_target
            if current_plan is not None:
                plan_had_action = True
            arguments = decision.get("arguments")
            effect = None
            prepared_for_approval = None
            mcp_crash_block = False
            rejected_final_answer = None
            print(f"[模型请求 MCP capability] {reference}")
            audit("tool_requested", "model", reference, "requested")
            try:
                if mcp_registry is None:
                    raise ValueError("MCP registry 未配置")
                client, name, detail = mcp_registry.resolve(reference)
                validate_json_schema(
                    arguments, detail.get("inputSchema", {"type": "object"})
                )
            except ValueError as error:
                observation = {
                    "result": None, "error": str(error), "exit_code": 1,
                    "denied_by": "capability_validation",
                }
                policy = None
                approved = False
                print(f"[MCP Validation] DENY：{error}")
            else:
                policy = mcp_registry.policy_for(reference, policy_binding.snapshot)
                effect = mcp_registry.effect_for(reference, policy_binding.snapshot)
                path_decision = inspect_mcp_paths(arguments)
                if not path_decision.allowed:
                    policy = {
                        **policy, "action": POLICY_DENY,
                        "reason": path_decision.reason,
                    }
                matches_recovered = bool(
                    recovered_action
                    and recovered_action["tool"] == reference
                    and recovered_action["arguments"] == arguments
                )
                unsafe_unknown_replay = bool(
                    matches_recovered
                    and recovered_action["state"] == "unknown"
                    and recovered_action["replay_policy"] != "safe_to_retry"
                )
                print(f"[Policy] {policy['action']}：{policy['reason']}")
                audit("policy_decision", "harness", reference,
                      policy["action"], policy["reason"],
                      references={
                          "policy_fingerprint": policy_binding.fingerprint,
                          "policy_trace": policy.get("trace", {}),
                          "composition_inputs": policy.get("composition_inputs"),
                      })
                if envelope_store is not None and policy.get("composition_inputs"):
                    envelope_store.append_transition(
                        run_id, "policy", {
                            "policy_fingerprint": policy_binding.fingerprint,
                            "tool": reference,
                            "action_effect": effect,
                            "composition_inputs": policy["composition_inputs"],
                        }, {"decision": policy["action"]},
                    )
                print(f"[MCP Effect] {effect}")
                approved = policy["action"] == POLICY_ALLOW
                blocked_by_verification = False
                if (verification.get("degraded")
                        and effect != MCP_EFFECT_READ_ONLY):
                    observation = {
                        "result": None,
                        "error": "degraded persistence blocks new side effects",
                        "exit_code": 126,
                        "denied_by": "persistence_gate",
                    }
                    approved = False
                    blocked_by_verification = True
                elif requires_verification and effect != MCP_EFFECT_READ_ONLY:
                    observation = {
                        "result": None,
                        "error": "verification tool must be read-only",
                        "exit_code": 126,
                        "denied_by": "verification_gate",
                    }
                    approved = False
                    blocked_by_verification = True
                elif (
                    policy["action"] == POLICY_ASK
                    and not unsafe_unknown_replay
                    and not (matches_recovered and recovered_action["state"] in {"succeeded", "failed"})
                ):
                    prepared_for_approval = create_action_checkpoint(
                        reference, arguments, effect,
                        current_plan["plan_id"] if current_plan else None,
                        current_plan["version"] if current_plan else None,
                        current_step_id,
                    )
                    persist_action(prepared_for_approval)
                    approved = ask_approval(reference, policy["reason"])
                    if not scheduling_allowed():
                        checkpoint()
                        legacy = f"run {run_control['state']}"
                        return emit_result(
                            blocking_reason=run_control.get("reason") or legacy,
                            legacy_value=legacy,
                        )
                handled_recovery = False
                if matches_recovered and recovered_action["state"] == "succeeded":
                    observation = dict(recovered_action["observation"])
                    approved = False
                    handled_recovery = True
                elif matches_recovered and recovered_action["state"] == "failed":
                    observation = dict(recovered_action["observation"])
                    approved = False
                    handled_recovery = True
                elif (
                    matches_recovered
                    and recovered_action["state"] == "unknown"
                    and recovered_action["replay_policy"] != "safe_to_retry"
                ):
                    observation = {
                        "result": None, "error": "uncertain side effect",
                        "exit_code": 126, "denied_by": "crash_recovery",
                    }
                    approved = False
                    handled_recovery = True
                    mcp_crash_block = True
                if approved:
                    action_checkpoint = prepared_for_approval or create_action_checkpoint(
                        reference, arguments, effect,
                        current_plan["plan_id"] if current_plan else None,
                        current_plan["version"] if current_plan else None,
                        current_step_id,
                    )
                    if not scheduling_allowed():
                        settle_run_control()
                        checkpoint()
                        legacy = f"run {run_control['state']}"
                        return emit_result(
                            blocking_reason=run_control.get("reason") or legacy,
                            legacy_value=legacy,
                        )
                    budget_reason = consume_normal_action()
                    if budget_reason:
                        legacy = deadline_block(budget_reason)
                        return emit_result(
                            blocking_reason=budget_reason,
                            legacy_value=legacy,
                        )
                    begin_attempt()
                    dispatched = dispatch_mcp(
                        action_checkpoint, reference, arguments, policy, effect,
                        approved,
                    )
                    observation = dispatched.raw_observation
                    action_checkpoint = dispatched.checkpoint
                    recovered_action = action_checkpoint
                    retry_decision = (
                        "no_retry" if (dispatched.degraded or verification.get("degraded")) else
                        finish_or_decide_retry(
                            observation, effect, action_checkpoint["replay_policy"]
                        )
                    )
                    while retry_decision == "retry_with_backoff":
                        if governance_state is not None:
                            backoff = backoff_decision(
                                governance_state, current_retry_state["backoff_delay"], clock
                            )
                            if not backoff["allowed"]:
                                legacy = deadline_block(backoff["reason"])
                                return emit_result(
                                    blocking_reason=backoff["reason"],
                                    legacy_value=legacy,
                                )
                        if not cooperative_backoff(
                            current_retry_state["backoff_delay"], run_control,
                            retry_sleeper,
                        ):
                            settle_run_control()
                            checkpoint()
                            legacy = f"run {run_control['state']}"
                            return emit_result(
                                blocking_reason=(run_control.get("reason")
                                                 or legacy),
                                legacy_value=legacy,
                            )
                        if policy["action"] == POLICY_ASK and not ask_approval(
                            reference, policy["reason"]
                        ):
                            rejected = {"result": None, "error": "tool execution was denied by user", "exit_code": 126, "denied_by": "user"}
                            retry_decision = finish_or_decide_retry(
                                rejected, effect, action_checkpoint["replay_policy"]
                            )
                            observation = rejected
                            break
                        budget_reason = consume_normal_action()
                        if budget_reason:
                            legacy = deadline_block(budget_reason)
                            return emit_result(
                                blocking_reason=budget_reason,
                                legacy_value=legacy,
                            )
                        begin_attempt()
                        action_checkpoint = create_action_checkpoint(
                            reference, arguments, effect,
                            current_plan["plan_id"] if current_plan else None,
                            current_plan["version"] if current_plan else None,
                            current_step_id,
                        )
                        dispatched = dispatch_mcp(
                            action_checkpoint, reference, arguments, policy,
                            effect, True,
                        )
                        observation = dispatched.raw_observation
                        action_checkpoint = dispatched.checkpoint
                        recovered_action = action_checkpoint
                        retry_decision = (
                            "no_retry" if (dispatched.degraded or verification.get("degraded")) else
                            finish_or_decide_retry(
                                observation, effect,
                                action_checkpoint["replay_policy"],
                            )
                        )
                    if not scheduling_allowed():
                        settle_run_control()
                    if observation["exit_code"] == 0:
                        if requires_verification and effect == MCP_EFFECT_READ_ONLY:
                            requires_verification = False
                            verification_target = None
                        elif effect in {
                            MCP_EFFECT_SIDE_EFFECTING, MCP_EFFECT_UNKNOWN,
                        }:
                            requires_verification = True
                            verification_obligation = True
                            latest_write_command = reference
                            verification_target = None
                            pending_verification_action_id = action_checkpoint["action_id"]
                    print("[MCP Tool Execution] 调用完毕")
                elif policy["action"] == POLICY_DENY:
                    observation = {
                        "result": None, "error": "tool execution was denied by policy",
                        "exit_code": 126, "denied_by": "policy",
                    }
                elif not blocked_by_verification and not handled_recovery:
                    observation = {
                        "result": None, "error": "tool execution was denied by user",
                        "exit_code": 126, "denied_by": "user",
                    }
            print(f"[Observation] exit_code={observation['exit_code']}")
            safe_observation = persisted_safe_observation(
                observation, reference, arguments,
            )
            messages.append({
                "role": "assistant",
                "content": json.dumps(decision, ensure_ascii=False),
            })
            messages.append({
                "role": "tool",
                "content": json.dumps(safe_observation, ensure_ascii=False),
            })
            # Session-safe projection is durable before any Evidence derived
            # from the raw runtime observation is accepted.
            checkpoint()
            if (evidence_store is not None and last_observation_event_id is not None
                    and action_checkpoint is not None):
                server = reference.split(":", 2)[1]
                mcp_record = create_mcp_observation_evidence(
                    run_id,
                    {"kind": "plan_step", "target": current_step_id,
                     "claim": "external_observation_recorded"}
                    if current_step_id else
                    {"kind": "mcp_call", "target": action_checkpoint["action_id"],
                     "claim": "external_observation_recorded"},
                    server, reference, observation, last_observation_event_id,
                    action_id=action_checkpoint["action_id"],
                    references={"model_request_id": request_id} if request_id else {},
                )
                persist_evidence(mcp_record, current_step_id)
                if mcp_verification_was_required:
                    accepted = bool(effect == MCP_EFFECT_READ_ONLY
                                    and observation.get("exit_code") == 0)
                    reason = None if accepted else "MCP verification observation failed"
                    verification_record = create_verification_evidence(
                        run_id,
                        {"kind": "plan_step", "target": current_step_id,
                         "claim": "external_state_verified"}
                        if current_step_id else
                        {"kind": "mcp_call", "target": reference,
                         "claim": "external_state_verified"},
                        mcp_historical_target, action_checkpoint["action_id"],
                        observation, last_observation_event_id, accepted, reason,
                        pending_verification_action_id,
                        references={"candidate_evidence_id": mcp_record["evidence_id"]},
                    )
                    evidence_id = persist_evidence(
                        verification_record, current_step_id, accepted,
                    )
                    if accepted and current_plan is not None:
                        plan_evidence_ids.append(evidence_id)
            if (
                current_plan is not None
                and observation["exit_code"] == 0
                and effect == MCP_EFFECT_READ_ONLY
            ):
                if evidence_store is None:
                    plan_evidence.append({
                        "kind": "tool_observation",
                        "message_index": len(messages) - 1,
                        "summary": f"{reference} read-only observation succeeded",
                        "verified": True,
                    })
                plan_runtime_state["requires_fresh_grounding"] = False
            checkpoint()
            replan_result = stop_for_replan_if_needed(
                retry_decision if approved and not mcp_crash_block else None
            )
            if replan_result is not None:
                return emit_result(
                    blocking_reason=replan_result.split(": ", 1)[-1],
                    legacy_value=replan_result,
                )
            if mcp_crash_block:
                if current_plan is not None and current_plan["status"] == "active":
                    blocked = block_step(current_plan, current_step_id)
                    blocked_step = next(
                        item for item in blocked["steps"] if item["id"] == current_step_id
                    )
                    blocked_step["evidence"].append({
                        "kind": "recovery_block",
                        "summary": "uncertain side effect",
                    })
                    current_plan.clear()
                    current_plan.update(blocked)
                    checkpoint()
                return emit_result(
                    blocking_reason="uncertain side effect",
                    legacy_value="blocked: uncertain side effect",
                )
            continue

        if decision.get("type") != "tool_call" or not decision.get("command"):
            failure = "模型返回了无效决定"
            terminal = emit_result(
                terminal_failure=failure, legacy_value=failure
            )
            if return_result:
                return terminal
            raise ValueError(f"{failure}：{decision!r}")

        command = decision["command"]
        verification_was_required = requires_verification
        historical_verification_target = verification_target
        evidence_related = bool(
            verification_target is None
            or is_related_verification(command, verification_target)
        )
        last_observation_event_id = None
        if not scheduling_allowed() and not safety_entry:
            settle_run_control()
            reason = deadline_status(governance_state, clock) if governance_state is not None else None
            legacy = deadline_block(reason) if reason else f"run {run_control['state']}"
            return emit_result(
                blocking_reason=reason or run_control.get("reason") or legacy,
                legacy_value=legacy,
            )
        if current_plan is not None:
            plan_had_action = True
        rejected_final_answer = None
        print(f"[模型请求执行的命令] {command}")
        audit("tool_requested", "model", "shell", "requested")
        policy = classify_shell(command, policy_binding.snapshot)
        print(f"[Policy] {policy['action']}：{policy['reason']}")
        audit("policy_decision", "harness", "shell",
              policy["action"], policy["reason"],
              references={
                  "policy_fingerprint": policy_binding.fingerprint,
                  "policy_trace": policy.get("trace", {}),
                  "composition_inputs": policy.get("composition_inputs"),
              })
        if envelope_store is not None and policy.get("composition_inputs"):
            envelope_store.append_transition(
                run_id, "policy", {
                    "policy_fingerprint": policy_binding.fingerprint,
                    "tool": "shell", "action_effect": policy.get("effect"),
                    "composition_inputs": policy["composition_inputs"],
                }, {"decision": policy["action"]},
            )

        arguments = {"command": command}
        matches_recovered = bool(
            recovered_action
            and recovered_action["tool"] == "shell"
            and recovered_action["arguments"] == arguments
        )
        unsafe_unknown_replay = bool(
            matches_recovered
            and recovered_action["state"] == "unknown"
            and recovered_action["replay_policy"] != "safe_to_retry"
        )
        recovered_not_applied = bool(
            matches_recovered
            and recovered_action["state"] == "failed"
            and recovered_action.get("observation", {}).get("status")
            == "reconciled_not_applied"
        )
        is_reconciliation_attempt = bool(
            recovered_action
            and recovered_action["state"] == "unknown"
            and recovered_action["replay_policy"] != "safe_to_retry"
            and policy["effect"] == "read_only"
            and policy["action"] != POLICY_DENY
        )
        approved = policy["action"] == POLICY_ALLOW
        prepared_for_approval = None
        blocked_by_persistence = bool(
            verification.get("degraded") and policy["effect"] != "read_only"
        )
        if blocked_by_persistence:
            approved = False
            observation = {
                "status": "denied", "denied_by": "persistence_gate",
                "stdout": "",
                "stderr": "degraded persistence blocks new side effects",
                "exit_code": 126,
            }
        elif (
            requires_verification
            and policy["action"] != POLICY_DENY
            and policy["effect"] != "read_only"
        ):
            approved = False
            observation = {
                "status": "denied",
                "denied_by": "verification_gate",
                "stdout": "",
                "stderr": "verification tool must be read-only",
                "exit_code": 126,
            }
            print("[Verification Gate] 验证工具必须是只读命令")
        elif (
            requires_verification
            and policy["action"] != POLICY_DENY
            and policy["effect"] == "read_only"
            and verification_target is not None
            and not is_related_verification(command, verification_target)
        ):
            approved = False
            observation = {
                "status": "denied",
                "denied_by": "verification_quality",
                "stdout": "",
                "stderr": (
                    "verification evidence is not related to the modified target"
                ),
                "exit_code": 126,
                "verification_target": verification_target,
            }
            print("[Verification Quality] 验证证据与修改目标无关")
        elif (
            policy["action"] == POLICY_ASK
            and not unsafe_unknown_replay
            and not (
                matches_recovered
                and recovered_action["state"] in {"succeeded", "failed"}
                and not recovered_not_applied
            )
        ):
            prepared_for_approval = create_action_checkpoint(
                "shell", arguments, policy["effect"],
                current_plan["plan_id"] if current_plan else None,
                current_plan["version"] if current_plan else None,
                current_step_id,
            )
            persist_action(prepared_for_approval)
            approved = ask_approval(
                command, policy["reason"], safe_shell_command_identity(command)
            )
            if not approved and recovered_not_applied:
                persist_action(recovered_action)
                if current_retry_state is not None:
                    persist_retry(record_failure(
                        current_retry_state, "user_rejected", "approval_rejected",
                        "no_retry",
                    ))
            if not scheduling_allowed():
                checkpoint()
                legacy = f"run {run_control['state']}"
                return emit_result(
                    blocking_reason=run_control.get("reason") or legacy,
                    legacy_value=legacy,
                )

        handled_recovery = False
        crash_block_reason = None
        if blocked_by_persistence:
            handled_recovery = True
        if (
            matches_recovered
            and recovered_action["state"] in {"succeeded", "failed"}
            and not recovered_not_applied
        ):
            observation = dict(recovered_action["observation"])
            observation.setdefault("stdout", "")
            observation.setdefault("stderr", "")
            approved = False
            handled_recovery = True
        elif (
            matches_recovered
            and recovered_action["state"] == "unknown"
            and recovered_action["replay_policy"] != "safe_to_retry"
        ):
            observation = {
                "status": "blocked", "denied_by": "crash_recovery",
                "stdout": "", "stderr": "uncertain side effect", "exit_code": 126,
            }
            approved = False
            handled_recovery = True
            crash_block_reason = "uncertain side effect"

        if approved:
            action_checkpoint = prepared_for_approval or create_action_checkpoint(
                "shell", arguments, policy["effect"],
                current_plan["plan_id"] if current_plan else None,
                current_plan["version"] if current_plan else None,
                current_step_id,
            )
            if not scheduling_allowed() and not is_reconciliation_attempt:
                settle_run_control()
                checkpoint()
                reason = deadline_status(governance_state, clock) if governance_state is not None else None
                legacy = deadline_block(reason) if reason else f"run {run_control['state']}"
                return emit_result(
                    blocking_reason=reason or run_control.get("reason") or legacy,
                    legacy_value=legacy,
                )
            if not is_reconciliation_attempt:
                budget_reason = consume_normal_action()
                if budget_reason:
                    legacy = deadline_block(budget_reason)
                    return emit_result(
                        blocking_reason=budget_reason, legacy_value=legacy
                    )
                begin_attempt()
            elif governance_state is not None:
                expected = expected_file_write(recovered_action)
                related = bool(expected and command in {
                    f"cat {expected['path']}", f"ls {expected['path']}",
                })
                safety = safety_reconciliation_decision(
                    governance_state, recovered_action, "read_only", related,
                    policy["action"] != POLICY_DENY,
                )
                if not safety["allowed"]:
                    legacy = f"blocked: {safety['reason']}"
                    return emit_result(
                        blocking_reason=safety["reason"], legacy_value=legacy
                    )
                persist_governance(consume_safety_reconciliation(governance_state))
            dispatched = dispatch_shell(
                action_checkpoint, command, policy, approved,
            )
            observation = dispatched.raw_observation
            action_checkpoint = dispatched.checkpoint
            retry_decision = None
            if not is_reconciliation_attempt:
                retry_decision = (
                    "no_retry" if (dispatched.degraded or verification.get("degraded")) else
                    finish_or_decide_retry(
                        observation, action_checkpoint["effect"],
                        action_checkpoint["replay_policy"],
                    )
                )
            while retry_decision == "retry_with_backoff":
                if governance_state is not None:
                    backoff = backoff_decision(
                        governance_state, current_retry_state["backoff_delay"], clock
                    )
                    if not backoff["allowed"]:
                        legacy = deadline_block(backoff["reason"])
                        return emit_result(
                            blocking_reason=backoff["reason"],
                            legacy_value=legacy,
                        )
                if not cooperative_backoff(
                    current_retry_state["backoff_delay"], run_control, retry_sleeper
                ):
                    settle_run_control()
                    checkpoint()
                    legacy = f"run {run_control['state']}"
                    return emit_result(
                        blocking_reason=run_control.get("reason") or legacy,
                        legacy_value=legacy,
                    )
                if policy["action"] == POLICY_ASK and not ask_approval(
                    command, policy["reason"], safe_shell_command_identity(command)
                ):
                    observation = {"status": "denied", "denied_by": "user", "stdout": "", "stderr": "tool execution was denied by user", "exit_code": 126}
                    retry_decision = finish_or_decide_retry(
                        observation, action_checkpoint["effect"], action_checkpoint["replay_policy"]
                    )
                    break
                budget_reason = consume_normal_action()
                if budget_reason:
                    legacy = deadline_block(budget_reason)
                    return emit_result(
                        blocking_reason=budget_reason, legacy_value=legacy
                    )
                begin_attempt()
                action_checkpoint = create_action_checkpoint(
                    "shell", arguments, policy["effect"],
                    current_plan["plan_id"] if current_plan else None,
                    current_plan["version"] if current_plan else None,
                    current_step_id,
                )
                dispatched = dispatch_shell(
                    action_checkpoint, command, policy, True,
                )
                observation = dispatched.raw_observation
                action_checkpoint = dispatched.checkpoint
                retry_decision = (
                    "no_retry" if (dispatched.degraded or verification.get("degraded")) else
                    finish_or_decide_retry(
                        observation, action_checkpoint["effect"],
                        action_checkpoint["replay_policy"],
                    )
                )
            print("[Tool Execution] 命令执行完毕")
            if observation["exit_code"] == 0:
                if requires_verification and policy["effect"] == "read_only":
                    requires_verification = False
                    verification_target = None
                    audit("verification_state_changed", "harness", "tool",
                          "succeeded")
                    print("[Verification Gate] 只读验证成功，已解除门禁")
                elif policy["effect"] != "read_only":
                    requires_verification = True
                    verification_obligation = True
                    latest_write_command = command
                    verification_target = extract_verification_target(command)
                    pending_verification_action_id = action_checkpoint["action_id"]
                    audit(
                        "verification_state_changed", "harness", "tool",
                        "required", "side-effecting action requires verification",
                        references={"action_id": action_checkpoint["action_id"]},
                    )
                    if verification_target is None:
                        print(
                            "[Verification Quality] 无法可靠识别目标，"
                            "显式降级为 V3 验证行为"
                        )
                    elif (output_contract is not None
                          and verification_target.get("target_type") == "file"):
                        try:
                            pending_artifact = {
                                "path": verification_target["path"],
                                "content_identity": observe_workspace_file(
                                    verification_target["path"]
                                ),
                                "producer": create_producer(
                                    run_id, action_id=action_checkpoint["action_id"],
                                    capability="shell", step_id=current_step_id,
                                    model_request_id=request_id,
                                    model_decision_event_id=(
                                        decision_event.get("event_id")
                                        if decision_event else None
                                    ),
                                    tool="shell",
                                ),
                            }
                        except (ArtifactError, OSError) as error:
                            pending_artifact = None
                            audit("artifact_rejected", "harness", "artifact",
                                  "rejected", str(error))
                    print("[Verification Gate] 写操作成功，需要只读验证")
        elif not handled_recovery and not (
            requires_verification
            and policy["action"] != POLICY_DENY
            and (
                policy["effect"] != "read_only"
                or (
                    policy["effect"] == "read_only"
                    and verification_target is not None
                    and not is_related_verification(command, verification_target)
                )
            )
        ):
            denied_by = "policy" if policy["action"] == POLICY_DENY else "user"
            observation = {
                "status": "denied",
                "denied_by": denied_by,
                "stdout": "",
                "stderr": f"tool execution was denied by {denied_by}",
                "exit_code": 126,
            }
            print(f"[Tool Execution] 未执行：denied_by={denied_by}")
        print(f"[Observation] exit_code={observation['exit_code']}")
        print(f"[Observation] safe={safe_observation_summary(observation)!r}")

        safe_observation = persisted_safe_observation(
            observation, "shell", {"command": command},
        )
        messages.append({
            "role": "assistant",
            "content": json.dumps(decision, ensure_ascii=False),
        })
        messages.append({
            "role": "tool",
            "content": json.dumps(safe_observation, ensure_ascii=False),
        })
        # This establishes the named after_session_before_evidence boundary.
        checkpoint()

        verification_record = None
        verification_evidence_id = None
        if verification_was_required:
            verification_inputs = {
                "requires_verification": True,
                "verification_target": historical_verification_target,
                "action_effect": policy["effect"],
                "evidence_related": evidence_related,
                "historical_recorded_observation": True,
                "observation": verification_observation_identity(
                    observation, last_observation_event_id
                ),
            }
            verification_output = replay_verification_transition(verification_inputs)
            if evidence_store is not None and last_observation_event_id is not None:
                subject = ({"kind": "plan_step", "target": current_step_id,
                            "claim": "current_reality_verified"}
                           if current_step_id else
                           {"kind": "workspace_file",
                            "target": (historical_verification_target or {}).get("path", "unknown"),
                            "claim": "content_verified"})
                verification_record = create_verification_evidence(
                    run_id, subject, historical_verification_target,
                    action_checkpoint["action_id"], observation,
                    last_observation_event_id, verification_output["accepted"],
                    verification_output["reason"], pending_verification_action_id,
                    artifact=(artifact_ref(
                        historical_verification_target["path"],
                        hashlib.sha256(observation.get("stdout", "").encode()).hexdigest(),
                        len(observation.get("stdout", "").encode()),
                    ) if verification_output["accepted"]
                         and isinstance(historical_verification_target, dict)
                         and historical_verification_target.get("target_type") == "file"
                         else None),
                )
                evidence_id = persist_evidence(
                    verification_record, current_step_id,
                    verification_output["accepted"],
                )
                verification_evidence_id = evidence_id
                if verification_output["accepted"] and current_plan is not None:
                    plan_evidence_ids.append(evidence_id)
                verification_inputs.update({
                    "evidence_id": evidence_id,
                    "evidence_fingerprint": verification_record["evidence_fingerprint"],
                })
            if envelope_store is not None:
                envelope_store.append_transition(
                    run_id, "verification", verification_inputs,
                    verification_output,
                )
            if pending_artifact is not None and verification_record is not None:
                finalize_artifact_candidate(
                    verification_record, verification_evidence_id,
                    verification_output["accepted"],
                )

        # Harness 行为：safe observation 已在 Evidence 前写入 Session。
        if (evidence_store is not None and last_observation_event_id is not None
                and action_checkpoint is not None):
            source = {
                "action_id": action_checkpoint["action_id"],
                "logical_action_id": (
                    current_retry_state or {}
                ).get("logical_action_id", action_checkpoint["action_id"]),
                "attempt": (current_retry_state or {}).get("attempt_count", 1),
                "tool": "shell",
            }
            subject = ({"kind": "plan_step", "target": current_step_id,
                        "claim": "observation_recorded"}
                       if current_step_id else
                       {"kind": "tool_action", "target": action_checkpoint["action_id"],
                        "claim": "observation_recorded"})
            tool_record = create_tool_observation_evidence(
                run_id, subject, source, observation, last_observation_event_id,
                accepted=(observation.get("exit_code") == 0
                          and policy["effect"] == "read_only"),
                read_only=policy["effect"] == "read_only",
                references={"model_request_id": request_id} if request_id else {},
            )
            tool_evidence_id = persist_evidence(tool_record, current_step_id)
            if (current_plan is not None and observation.get("exit_code") == 0
                    and policy["effect"] == "read_only"):
                plan_evidence_ids.append(tool_evidence_id)
        if (
            recovered_action is not None
            and recovered_action["state"] == "unknown"
            and recovered_action["replay_policy"] != "safe_to_retry"
            and policy["effect"] == "read_only"
            and policy["action"] != POLICY_DENY
        ):
            reconciliation_source_action_id = recovered_action["action_id"]
            reconciliation_target = expected_file_write(recovered_action) or {
                "tool": recovered_action["tool"]
            }
            audit(
                "reconciliation_state_changed", "harness", "action", "started",
                references={"action_id": recovered_action["action_id"]},
            )
            reconciliation = reconcile_file_observation(
                recovered_action, command, observation
            )
            if reconciliation["status"] == "succeeded":
                recovered_action = reconciliation["checkpoint"]
                persist_action(recovered_action)
                plan_evidence.append(reconciliation["evidence"])
                plan_runtime_state["requires_fresh_grounding"] = False
                plan_runtime_state.pop("action_recovery", None)
                if current_retry_state is not None:
                    persist_retry(complete_retry(current_retry_state))
            elif reconciliation["status"] == "not_applied":
                recovered_action = reconciliation["checkpoint"]
                persist_action(recovered_action)
                plan_evidence.append(reconciliation["evidence"])
                plan_runtime_state["requires_fresh_grounding"] = False
                plan_runtime_state.pop("action_recovery", None)
                if current_retry_state is not None:
                    persist_retry(reopen_retry_after_reconciliation(
                        current_retry_state,
                        recovered_action["replay_policy"],
                        run_control["state"],
                    ))
            else:
                crash_block_reason = reconciliation["reason"]
            if evidence_store is not None and last_observation_event_id is not None:
                result = ({"succeeded": "applied", "not_applied": "not_applied"}
                          .get(reconciliation["status"], "uncertain"))
                reconciliation_record = create_reconciliation_evidence(
                    run_id,
                    {"kind": "plan_step", "target": current_step_id,
                     "claim": "reconciliation_completed"}
                    if current_step_id else
                    {"kind": "tool_action", "target": reconciliation_source_action_id,
                     "claim": "reconciliation_completed"},
                    reconciliation_source_action_id, reconciliation_target,
                    result, observation, last_observation_event_id,
                )
                reconciliation_id = persist_evidence(
                    reconciliation_record, current_step_id,
                    result in {"applied", "not_applied"},
                )
                if result in {"applied", "not_applied"} and current_plan is not None:
                    plan_evidence_ids.append(reconciliation_id)
            audit(
                "reconciliation_state_changed", "harness", "action",
                "completed" if reconciliation["status"] in {"succeeded", "not_applied"}
                else "blocked",
                reconciliation.get("reason"),
                references={"action_id": recovered_action["action_id"]},
            )
            if governance_state is not None and (
                deadline_status(governance_state, clock)
                or not can_schedule_action(run_control)
            ):
                checkpoint()
                reason = deadline_status(governance_state, clock)
                reason = reason or "run control prevents normal scheduling"
                legacy = f"blocked: {reason}"
                return emit_result(
                    blocking_reason=reason, legacy_value=legacy
                )
        if (
            current_plan is not None
            and observation["exit_code"] == 0
            and policy["effect"] == "read_only"
        ):
            if evidence_store is None:
                plan_evidence.append({
                    "kind": "tool_observation",
                    "message_index": len(messages) - 1,
                    "summary": f"{command} read-only observation succeeded",
                    "verified": True,
                })
            plan_runtime_state["requires_fresh_grounding"] = False
        checkpoint()
        replan_result = stop_for_replan_if_needed(
            retry_decision if approved and crash_block_reason is None else None
        )
        if replan_result is not None:
            return emit_result(
                blocking_reason=replan_result.split(": ", 1)[-1],
                legacy_value=replan_result,
            )
        if crash_block_reason is not None:
            if current_plan is not None and current_plan["status"] == "active":
                blocked = block_step(current_plan, current_step_id)
                blocked_step = next(
                    item for item in blocked["steps"] if item["id"] == current_step_id
                )
                blocked_step["evidence"].append({
                    "kind": "recovery_block",
                    "summary": crash_block_reason,
                })
                current_plan.clear()
                current_plan.update(blocked)
                checkpoint()
            legacy = f"blocked: {crash_block_reason}"
            return emit_result(
                blocking_reason=crash_block_reason, legacy_value=legacy
            )

    failure = f"达到最大步数 {max_steps}，Agent 已停止，以防止无限循环。"
    terminal = emit_result(
        terminal_failure=failure, legacy_value=failure
    )
    if return_result:
        return terminal
    raise RuntimeError(failure)
