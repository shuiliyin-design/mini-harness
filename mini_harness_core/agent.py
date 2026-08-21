"""Agent and Subagent runtime control flow."""

import json
import os

from .audit import AuditWriter, new_run_id, read_events, safe_observation_summary
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
from .verification import (
    build_verification_feedback,
    extract_verification_target,
    is_related_verification,
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


def _complete(
    provider, messages, context_assembler, control_state, context_budget,
    current_plan=None, plan_runtime_state=None,
):
    system_instructions = getattr(provider, "SYSTEM_PROMPT", None)
    if isinstance(system_instructions, str):
        messages = context_assembler.prepare_request(
            system_instructions, messages, control_state, context_budget,
            current_plan, plan_runtime_state,
        )
    return provider.complete(messages)


def _run_subagent_once(
    handoff, provider, main_authority=None, memory_store=None,
    mcp_registry=None, context_assembler=None, context_budget=None,
    run_control=None, governance_state=None, clock=None,
    subagent_timeout_seconds=None, policy_binding=None,
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
            decision = _complete(
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
                observation = execute_mcp_tool(
                    mcp_registry, reference, decision.get("arguments", {}), timeout
                )
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
                observation = execute_shell(command, timeout)
                action = {"tool": "shell", "command": command,
                          "outcome": observation["exit_code"]}
                if (
                    observation["exit_code"] == 0
                    and verification["requires_verification"]
                ):
                    verification["requires_verification"] = False

            observations.append(observation)
            actions.append(action)
            messages.append({
                "role": "assistant",
                "content": json.dumps(decision, ensure_ascii=False),
            })
            messages.append({
                "role": "tool",
                "content": json.dumps(observation, ensure_ascii=False),
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
    policy_binding=None,
):
    """Run one Subagent durably; V13 never recursively recovers a lost run."""
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
    if save_action_checkpoint:
        save_action_checkpoint(checkpoint)
    retry_state = create_retry_state(max_attempts=1)
    retry_state = start_attempt(retry_state)
    if save_retry_state:
        save_retry_state(retry_state)
    checkpoint = transition_action_checkpoint(checkpoint, "executing")
    if save_action_checkpoint:
        save_action_checkpoint(checkpoint)
    result = _run_subagent_once(
        handoff, provider, main_authority, memory_store, mcp_registry,
        context_assembler, context_budget,
        run_control, governance_state, clock, effective_deadline, policy_binding,
    )
    checkpoint = transition_action_checkpoint(
        checkpoint,
        "succeeded" if result.get("status") == "completed" else "failed",
        {"status": result.get("status"), "exit_code": 0 if result.get("status") == "completed" else 1,
         "result": result.get("summary", "")},
    )
    if save_action_checkpoint:
        save_action_checkpoint(checkpoint)
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
):
    """Harness 行为：驱动模型、工具和 observation 之间的循环。"""
    run_id = audit_writer.run_id if audit_writer is not None else new_run_id()
    if audit_writer is None and session_id is not None:
        audit_writer = AuditWriter(session_id, run_id, audit_directory) if audit_directory else AuditWriter(session_id, run_id)

    def audit(event_type, actor, subject=None, outcome=None, reason=None,
              references=None, summary=None):
        if audit_writer is not None:
            return audit_writer.append(
                event_type, actor, subject, outcome, reason, references, summary
            )
        return None

    if policy_binding is None:
        policy_directory = os.path.join(
            audit_writer.directory if audit_writer is not None else
            (audit_directory or os.path.join(os.getcwd(), ".audit")),
            "policies",
        )
        policy_binding = bind_current_policy(mcp_registry, policy_directory)
    memory_store = memory_store or MemoryStore()
    context_assembler = context_assembler or RuntimeContextAssembler(
        memory_store=memory_store, mcp_registry=mcp_registry
    )
    started_references = {
        "policy_schema_version": policy_binding.schema_version,
        "policy_revision": policy_binding.revision,
        "policy_fingerprint": policy_binding.fingerprint,
    }
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
    run_control = run_control if run_control is not None else create_run_control()
    validate_run_control(run_control)
    safety_entry = bool(
        current_action_checkpoint
        and current_action_checkpoint.get("state") in {"executing", "unknown"}
        and current_action_checkpoint.get("effect") in {"side_effecting", "unknown"}
    )
    if not can_schedule_action(run_control) and not safety_entry:
        return f"run {run_control['state']}"
    if governance_state is not None:
        validate_governance_state(governance_state)
        decision = normal_action_decision(governance_state, clock=clock)
        if not decision["allowed"] and not safety_entry:
            return f"blocked: {decision['reason']}"
    messages = messages if messages is not None else []
    verification = verification if verification is not None else {
        "requires_verification": False,
        "latest_write_command": None,
        "verification_target": None,
    }
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
    plan_runtime_state = {
        "requires_fresh_grounding": bool(require_plan_grounding),
    }
    recovered_action = None

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

    def invoke_shell(command):
        audit("action_state_changed", "tool", "shell", "started")
        if governance_state is None:
            observation = execute_shell(command)
        else:
            observation = execute_shell(command, effective_tool_timeout(governance_state, clock))
        audit(
            "action_state_changed", "environment", "shell",
            "succeeded" if observation.get("exit_code") == 0 else "failed",
            summary=safe_observation_summary(observation),
        )
        return observation

    def invoke_mcp(reference, arguments):
        audit("action_state_changed", "mcp", reference, "started")
        if governance_state is None:
            observation = execute_mcp_tool(mcp_registry, reference, arguments)
        else:
            observation = execute_mcp_tool(
                mcp_registry, reference, arguments,
                effective_tool_timeout(governance_state, clock),
            )
        audit(
            "mcp_called", "mcp", reference,
            "succeeded" if observation.get("exit_code") == 0 else "failed",
            summary=safe_observation_summary(observation),
        )
        return observation

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
        actor = "tool" if value["state"] == "executing" else (
            "environment" if value["state"] in {"succeeded", "failed", "unknown"}
            else "harness"
        )
        audit(
            "action_state_changed", actor, value["tool"], value["state"],
            references={key: value.get(key) for key in
                        ("action_id", "plan_id", "step_id") if value.get(key)},
            summary=(safe_observation_summary(value["observation"])
                     if value.get("observation") else None),
        )

    def ask_approval(subject, reason):
        audit("approval_requested", "harness", subject, "pending", reason)
        approved = request_approval(
            subject, reason, run_control, save_run_control,
            governance_state, save_governance_state, clock,
        )
        audit("approval_decided", "user", subject,
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
            save_checkpoint()

    if current_plan is not None:
        validate_plan(current_plan)
        validate_revision_history(plan_revision_history)
        audit(
            "plan_created", "harness", "plan", "active",
            references={"plan_id": current_plan["plan_id"],
                        "plan_version": current_plan["version"]},
        )
        if current_plan["status"] != "active":
            raise RuntimeError("只有 active plan 可以进入 Agent execution")
        current = next((
            item for item in current_plan["steps"]
            if item["status"] == "in_progress"
        ), None)
        if current is None:
            ready = select_ready_step(current_plan)
            if ready is None:
                raise RuntimeError("active plan 没有 ready step")
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
            return deadline_block(reason) if reason else f"run {run_control['state']}"
        print(f"\n[Harness] 第 {step}/{max_steps} 步：请求模型做决定")
        decision = _complete(provider, messages, context_assembler, {
            "requires_verification": requires_verification,
            "latest_write_command": latest_write_command,
            "verification_target": verification_target,
            "action_recovery": plan_runtime_state.get("action_recovery"),
            "run_control": run_control,
            "retry_state": current_retry_state,
            "governance_state": governance_state,
            "clock": clock,
            "safety_reconciliation": safety_entry,
        }, context_budget, current_plan, plan_runtime_state)
        audit("model_decision", "model", decision.get("type"), decision.get("type"))

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
            if requires_verification:
                if decision == rejected_final_answer:
                    raise RuntimeError(
                        "模型在没有新 tool_call 的情况下重复提交了被 Verification "
                        "Gate 拒绝的 final_answer"
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
            answer = decision.get("final_answer", "")
            if current_plan is not None:
                proposal = propose_step_completion(
                    current_plan, current_step_id, answer
                )
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
                if not accepted_evidence:
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
            messages.append({
                "role": "assistant",
                "content": json.dumps(decision, ensure_ascii=False),
            })
            checkpoint()
            print(f"[模型最终答案] {answer}")
            audit("run_state_changed", "harness", "run", "completed")
            return answer

        if decision.get("type") == "tool_call" and str(
            decision.get("tool", "")
        ).startswith("mcp:"):
            if not scheduling_allowed():
                settle_run_control()
                return f"run {run_control['state']}"
            reference = decision.get("tool")
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
                print(f"[MCP Effect] {effect}")
                approved = policy["action"] == POLICY_ALLOW
                blocked_by_verification = False
                if requires_verification and effect != MCP_EFFECT_READ_ONLY:
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
                        return f"run {run_control['state']}"
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
                    persist_action(action_checkpoint)
                    if not scheduling_allowed():
                        settle_run_control()
                        checkpoint()
                        return f"run {run_control['state']}"
                    budget_reason = consume_normal_action()
                    if budget_reason:
                        return deadline_block(budget_reason)
                    begin_attempt()
                    action_checkpoint = transition_action_checkpoint(
                        action_checkpoint, "executing"
                    )
                    persist_action(action_checkpoint)
                    observation = invoke_mcp(reference, arguments)
                    uncertain = (
                        observation["exit_code"] == -1
                        and effect in {MCP_EFFECT_SIDE_EFFECTING, MCP_EFFECT_UNKNOWN}
                    )
                    action_checkpoint = transition_action_checkpoint(
                        action_checkpoint,
                        "unknown" if uncertain else (
                            "succeeded" if observation["exit_code"] == 0 else "failed"
                        ),
                        None if uncertain else observation,
                    )
                    persist_action(action_checkpoint)
                    recovered_action = action_checkpoint
                    retry_decision = finish_or_decide_retry(
                        observation, effect, action_checkpoint["replay_policy"]
                    )
                    while retry_decision == "retry_with_backoff":
                        if governance_state is not None:
                            backoff = backoff_decision(
                                governance_state, current_retry_state["backoff_delay"], clock
                            )
                            if not backoff["allowed"]:
                                return deadline_block(backoff["reason"])
                        if not cooperative_backoff(
                            current_retry_state["backoff_delay"], run_control,
                            retry_sleeper,
                        ):
                            settle_run_control()
                            checkpoint()
                            return f"run {run_control['state']}"
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
                            return deadline_block(budget_reason)
                        begin_attempt()
                        action_checkpoint = create_action_checkpoint(
                            reference, arguments, effect,
                            current_plan["plan_id"] if current_plan else None,
                            current_plan["version"] if current_plan else None,
                            current_step_id,
                        )
                        persist_action(action_checkpoint)
                        action_checkpoint = transition_action_checkpoint(action_checkpoint, "executing")
                        persist_action(action_checkpoint)
                        observation = invoke_mcp(reference, arguments)
                        uncertain = observation["exit_code"] == -1 and effect in {MCP_EFFECT_SIDE_EFFECTING, MCP_EFFECT_UNKNOWN}
                        action_checkpoint = transition_action_checkpoint(action_checkpoint, "unknown" if uncertain else ("succeeded" if observation["exit_code"] == 0 else "failed"), None if uncertain else observation)
                        persist_action(action_checkpoint)
                        recovered_action = action_checkpoint
                        retry_decision = finish_or_decide_retry(observation, effect, action_checkpoint["replay_policy"])
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
                            latest_write_command = reference
                            verification_target = None
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
            messages.append({
                "role": "assistant",
                "content": json.dumps(decision, ensure_ascii=False),
            })
            messages.append({
                "role": "tool",
                "content": json.dumps(observation, ensure_ascii=False),
            })
            if (
                current_plan is not None
                and observation["exit_code"] == 0
                and effect == MCP_EFFECT_READ_ONLY
            ):
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
                return replan_result
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
                return "blocked: uncertain side effect"
            continue

        if decision.get("type") != "tool_call" or not decision.get("command"):
            raise ValueError(f"模型返回了无效决定：{decision!r}")

        command = decision["command"]
        if not scheduling_allowed() and not safety_entry:
            settle_run_control()
            reason = deadline_status(governance_state, clock) if governance_state is not None else None
            return deadline_block(reason) if reason else f"run {run_control['state']}"
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
        if (
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
            approved = ask_approval(command, policy["reason"])
            if not approved and recovered_not_applied:
                persist_action(recovered_action)
                if current_retry_state is not None:
                    persist_retry(record_failure(
                        current_retry_state, "user_rejected", "approval_rejected",
                        "no_retry",
                    ))
            if not scheduling_allowed():
                checkpoint()
                return f"run {run_control['state']}"

        handled_recovery = False
        crash_block_reason = None
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
            persist_action(action_checkpoint)
            if not scheduling_allowed() and not is_reconciliation_attempt:
                settle_run_control()
                checkpoint()
                reason = deadline_status(governance_state, clock) if governance_state is not None else None
                return deadline_block(reason) if reason else f"run {run_control['state']}"
            if not is_reconciliation_attempt:
                budget_reason = consume_normal_action()
                if budget_reason:
                    return deadline_block(budget_reason)
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
                    return f"blocked: {safety['reason']}"
                persist_governance(consume_safety_reconciliation(governance_state))
            action_checkpoint = transition_action_checkpoint(
                action_checkpoint, "executing"
            )
            persist_action(action_checkpoint)
            observation = invoke_shell(command)
            uncertain = (
                observation["exit_code"] == -1
                and action_checkpoint["effect"] != "read_only"
            )
            action_checkpoint = transition_action_checkpoint(
                action_checkpoint,
                "unknown" if uncertain else (
                    "succeeded" if observation["exit_code"] == 0 else "failed"
                ),
                None if uncertain else observation,
            )
            persist_action(action_checkpoint)
            retry_decision = None
            if not is_reconciliation_attempt:
                retry_decision = finish_or_decide_retry(
                    observation, action_checkpoint["effect"], action_checkpoint["replay_policy"]
                )
            while retry_decision == "retry_with_backoff":
                if governance_state is not None:
                    backoff = backoff_decision(
                        governance_state, current_retry_state["backoff_delay"], clock
                    )
                    if not backoff["allowed"]:
                        return deadline_block(backoff["reason"])
                if not cooperative_backoff(
                    current_retry_state["backoff_delay"], run_control, retry_sleeper
                ):
                    settle_run_control()
                    checkpoint()
                    return f"run {run_control['state']}"
                if policy["action"] == POLICY_ASK and not ask_approval(
                    command, policy["reason"]
                ):
                    observation = {"status": "denied", "denied_by": "user", "stdout": "", "stderr": "tool execution was denied by user", "exit_code": 126}
                    retry_decision = finish_or_decide_retry(
                        observation, action_checkpoint["effect"], action_checkpoint["replay_policy"]
                    )
                    break
                budget_reason = consume_normal_action()
                if budget_reason:
                    return deadline_block(budget_reason)
                begin_attempt()
                action_checkpoint = create_action_checkpoint(
                    "shell", arguments, policy["effect"],
                    current_plan["plan_id"] if current_plan else None,
                    current_plan["version"] if current_plan else None,
                    current_step_id,
                )
                persist_action(action_checkpoint)
                action_checkpoint = transition_action_checkpoint(action_checkpoint, "executing")
                persist_action(action_checkpoint)
                observation = invoke_shell(command)
                uncertain = observation["exit_code"] == -1 and action_checkpoint["effect"] != "read_only"
                action_checkpoint = transition_action_checkpoint(action_checkpoint, "unknown" if uncertain else ("succeeded" if observation["exit_code"] == 0 else "failed"), None if uncertain else observation)
                persist_action(action_checkpoint)
                retry_decision = finish_or_decide_retry(observation, action_checkpoint["effect"], action_checkpoint["replay_policy"])
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
                    latest_write_command = command
                    verification_target = extract_verification_target(command)
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
        print(f"[Observation] stdout={observation['stdout'].rstrip()!r}")
        print(f"[Observation] stderr={observation['stderr'].rstrip()!r}")

        # Harness 行为：保存模型决定，并把工具结果作为 observation 发回模型。
        messages.append({"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)})
        messages.append({"role": "tool", "content": json.dumps(observation, ensure_ascii=False)})
        if (
            recovered_action is not None
            and recovered_action["state"] == "unknown"
            and recovered_action["replay_policy"] != "safe_to_retry"
            and policy["effect"] == "read_only"
            and policy["action"] != POLICY_DENY
        ):
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
                return f"blocked: {reason or 'run control prevents normal scheduling'}"
        if (
            current_plan is not None
            and observation["exit_code"] == 0
            and policy["effect"] == "read_only"
        ):
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
            return replan_result
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
            return f"blocked: {crash_block_reason}"

    raise RuntimeError(f"达到最大步数 {max_steps}，Agent 已停止，以防止无限循环。")
