"""Agent and Subagent runtime orchestration.

Purpose: connect model decisions to Harness-owned planning, authority, execution,
observation, verification, recovery, and result phases.
Owns: phase ordering and the live runtime state shared by those phases.
Does Not Own: policy definitions, execution authority, state-machine rules, or
historical replay semantics; those remain in their focused modules.
Key Invariants: model output is intent rather than authority; every external
execution crosses ``AuthorizedAction``; terminal truth comes from Harness state.
"""

import json
import os
import hashlib
from dataclasses import dataclass, field

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
from .dispatch import (
    authorize_action, dispatch_authorized_action,
    environment_checkpoint_outcome, environment_invocation_from_authorized,
)
from .fault_injection import trigger_fault
from .observation import persisted_safe_observation
from .protected_paths import inspect_mcp_paths, inspect_subagent_paths
from .context import RuntimeContextAssembler
from .durability import (
    build_action_correlation_facts,
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
    create_environment_observation_evidence,
    create_reasoning_evidence, create_reconciliation_evidence,
    create_subagent_return_evidence, create_tool_observation_evidence,
    create_verification_evidence,
)
from .mobile_orchestration import (
    BATTERY_CAPABILITY, BATTERY_STEP_ID, NOTIFICATION_CAPABILITY,
    NOTIFICATION_STEP_ID, MobileWorkflowError, MobileWorkflowOutputStore,
    bind_mobile_condition, build_mobile_workflow_output,
    condition_allows_notification, create_mobile_condition_evidence,
    evaluate_battery_condition, find_condition_evidence, find_step_evidence,
    mobile_output_answer, validate_mobile_workflow,
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
from .environment_registry import (
    ENVIRONMENT_REGISTRY, UnsupportedEnvironmentCapability,
    classify_environment_capability,
)
from .result import (
    ResultStore, answer_identity, bind_final_result,
    build_authoritative_result_state, candidate_metadata_digest,
    evaluate_result_contract, finalize_authoritative_candidate,
    normalize_final_candidate, screen_result_answer,
)


@dataclass
class _RuntimePhaseResult:
    """Control transfer plus existing values produced by one runtime phase."""

    continue_loop: bool = False
    terminal: bool = False
    terminal_result: object = None
    action: object = None
    observation: object = None
    checkpoint: object = None
    verification: object = None
    retry: object = None
    plan: object = None
    degraded: object = None


@dataclass
class _AgentRuntimeState:
    """References and transient values shared by explicit execution phases."""

    references: dict
    requires_verification: bool
    latest_write_command: object
    verification_target: object
    verification_obligation: bool
    rejected_final_answer: object = None
    current_step_id: object = None
    plan_had_action: bool = False
    plan_evidence: list = field(default_factory=list)
    plan_evidence_ids: list = field(default_factory=list)
    pending_verification_action_id: object = None
    pending_artifact: object = None
    recovered_action: object = None
    action_checkpoint: object = None
    current_retry_state: object = None
    last_observation_event_id: object = None

    def __getattr__(self, name):
        try:
            return self.references[name]
        except KeyError as error:
            raise AttributeError(name) from error

    def audit(self, *args, **kwargs):
        return _runtime_audit(self, *args, **kwargs)

    def mark_degraded(self, reason, stage=None):
        return _mark_runtime_degraded(self, reason, stage)

    def persist_evidence(self, record, step_id=None, accepted=None):
        return _persist_runtime_evidence(self, record, step_id, accepted)

    def checkpoint(self):
        return _checkpoint_runtime(self)

    def emit_result(self, *args, **kwargs):
        return _emit_runtime_result(self, *args, **kwargs)

    def persist_action(self, value):
        self.action_checkpoint = value
        if self.save_action_checkpoint:
            self.save_action_checkpoint(value)

    def persist_retry(self, value):
        self.current_retry_state = value
        if self.save_retry_state:
            self.save_retry_state(value)

    def persist_governance(self, value):
        if self.governance_state is None:
            return
        self.governance_state.clear()
        self.governance_state.update(value)
        if self.save_governance_state:
            self.save_governance_state(value)

    def consume_normal_action(self, kind="tool"):
        if self.governance_state is None:
            return None
        decision = normal_action_decision(
            self.governance_state, kind, self.clock,
        )
        if not decision["allowed"]:
            return decision["reason"]
        self.persist_governance(consume_action(
            self.governance_state, kind, self.clock,
        ))
        return None

    def begin_attempt(self):
        if (
            self.current_retry_state is None
            or self.current_retry_state["state"]
            in {"completed", "blocked", "exhausted"}
        ):
            self.current_retry_state = create_retry_state(
                self.current_step_id,
            )
        self.persist_retry(start_attempt(self.current_retry_state))

    def finish_or_decide_retry(self, observation, effect, replay_policy):
        updated, decision = _handle_retry(
            observation, effect, replay_policy, self.current_retry_state,
            self.run_control, self.persist_retry, self.envelope_store,
            self.run_id, self.audit,
        )
        self.current_retry_state = updated
        return decision

    def ask_approval(self, subject, reason, audit_subject=None):
        persisted = subject if audit_subject is None else audit_subject
        self.audit(
            "approval_requested", "harness", persisted, "pending", reason,
        )
        approved = request_approval(
            subject, reason, self.run_control, self.save_run_control,
            self.governance_state, self.save_governance_state, self.clock,
        )
        self.audit(
            "approval_decided", "user", persisted,
            "granted" if approved else "rejected",
        )
        return approved

    def settle_run_control(self):
        updated = settle_control_boundary(self.run_control)
        if updated != self.run_control:
            self.run_control.clear()
            self.run_control.update(updated)
            if self.save_run_control:
                self.save_run_control(updated)
        if (
            self.governance_state is not None
            and self.run_control["state"] == "paused"
            and not self.governance_state["frozen"]
        ):
            self.persist_governance(freeze_governance(
                self.governance_state, "user_pause", self.clock,
            ))
        return self.run_control["state"]

    def scheduling_allowed(self):
        if not can_schedule_action(self.run_control):
            return False
        return (
            self.governance_state is None
            or normal_action_decision(
                self.governance_state, clock=self.clock,
            )["allowed"]
        )

    def deadline_block(self, reason):
        if (
            self.current_plan is not None
            and self.current_step_id is not None
            and self.current_plan["status"] == "active"
        ):
            blocked = block_step(self.current_plan, self.current_step_id)
            step = next(
                item for item in blocked["steps"]
                if item["id"] == self.current_step_id
            )
            step["evidence"].append({
                "kind": "governance", "summary": reason,
            })
            self.current_plan.clear()
            self.current_plan.update(blocked)
            self.checkpoint()
        return f"blocked: {reason}"

    def stop_for_replan_if_needed(self, retry_decision):
        if retry_decision != "replan" or self.current_plan is None:
            return None
        outcome = retry_exhausted_outcome(
            self.current_plan, self.current_step_id,
        )
        if outcome["action"] == "block":
            self.current_plan.clear()
            self.current_plan.update(outcome["plan"])
            self.checkpoint()
            return "blocked: retry exhausted and replan limit reached"
        self.plan_runtime_state["retry_exhausted"] = True
        self.checkpoint()
        return "replan required: action strategy is no longer suitable"

    def dispatch_shell(self, checkpoint, command, policy, approved=True):
        outcome, event_id = _dispatch_shell_action(
            checkpoint, command, policy, approved, self.run_id,
            self.current_retry_state, self.governance_state, self.clock,
            self.persist_action, self.audit, self.effect_state,
            self.fault_injector,
        )
        self.last_observation_event_id = event_id
        if outcome.degraded:
            self.mark_degraded(
                outcome.degraded_reason, outcome.degraded_stage,
            )
        return outcome

    def dispatch_mcp(
        self, checkpoint, reference, arguments, policy, effect, approved=True,
    ):
        outcome, event_id = _dispatch_mcp_action(
            checkpoint, reference, arguments, policy, effect, approved,
            self.run_id, self.current_retry_state, self.governance_state,
            self.clock, self.persist_action, self.audit, self.effect_state,
            self.fault_injector, self.mcp_registry, self.run_control,
            self.late_mcp_completion_journal,
        )
        self.last_observation_event_id = event_id
        if outcome.degraded:
            self.mark_degraded(
                outcome.degraded_reason, outcome.degraded_stage,
            )
        return outcome

    def dispatch_environment(self, checkpoint, reference, arguments, policy,
                             approved=True):
        outcome, event_id = _dispatch_environment_action(
            checkpoint, reference, arguments, policy, approved, self.run_id,
            self.current_retry_state, self.persist_action, self.audit,
            self.effect_state, self.fault_injector,
        )
        self.last_observation_event_id = event_id
        return outcome

    def finalize_artifact_candidate(
        self, verification_record, evidence_id, accepted,
    ):
        return _finalize_runtime_artifact(
            self, verification_record, evidence_id, accepted,
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


def _prepare_turn(
    provider, messages, context_assembler, control_state, context_budget,
    current_plan, plan_runtime_state, envelope_store, run_id, audit,
):
    """Request one model decision and bind its historical identity."""
    # The Provider boundary records sent context and returned intent. Historical
    # binding does not grant execution authority; handlers still apply all gates.
    decision, request_id = _complete(
        provider, messages, context_assembler, control_state, context_budget,
        current_plan, plan_runtime_state,
        request_recorder=(
            lambda prepared: envelope_store.append_request(
                run_id, prepared,
                any("deterministic_compacted_history" in item.get("content", "")
                    for item in prepared),
            )["request_id"]
            if envelope_store is not None else None
        ),
    )
    if ENVIRONMENT_REGISTRY.is_environment_intent(decision.get("tool")):
        try:
            decision = {**decision, "arguments":
                        ENVIRONMENT_REGISTRY.normalize_arguments(
                            decision.get("tool"), decision.get("arguments"))}
        except ValueError:
            # Do not bind rejected secret-bearing or malformed text into the
            # immutable Envelope. The handler receives only a safe denial fact.
            decision = {
                "type": "tool_call", "tool": decision.get("tool"),
                "arguments": {}, "validation_failed": True,
            }
    decision_event = audit(
        "model_decision", "model", decision.get("type"), decision.get("type")
    )
    if envelope_store is not None:
        envelope_store.bind_decision(
            run_id, request_id, decision,
            decision_event.get("event_id") if decision_event else None,
        )
    return decision, request_id, decision_event


def _process_observation(messages, decision, observation, capability,
                         arguments):
    """Persist only the safe observation projection into model continuity."""
    safe_observation = persisted_safe_observation(
        observation, capability, arguments,
    )
    messages.append({
        "role": "assistant",
        "content": json.dumps(decision, ensure_ascii=False),
    })
    messages.append({
        "role": "tool",
        "content": json.dumps(safe_observation, ensure_ascii=False),
    })
    return safe_observation


def _handle_memory_candidate(decision, memory_store, messages, audit,
                             checkpoint):
    """Validate, approve, persist, and record one memory proposal."""
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
            "type": "memory_feedback", "status": "memory not saved",
            "denied_by": "memory_policy", "reason": reason,
        }
        print(f"[Memory Policy] DENY：{reason}")
        audit("memory_decision", "harness", "memory", "rejected", reason)
    elif request_memory_approval(decision):
        try:
            memory_store.add(decision["kind"], decision["content"])
        except (OSError, ValueError) as error:
            feedback = {
                "type": "memory_feedback", "status": "memory not saved",
                "denied_by": "memory_store", "reason": str(error),
            }
            print(f"[Memory] memory not saved：{error}")
        else:
            feedback = {"type": "memory_feedback", "status": "memory saved"}
            print("[Memory] memory saved")
            audit("memory_decision", "harness", "memory", "saved")
    else:
        feedback = {"type": "memory_feedback", "status": "memory not saved"}
        print("[Memory] memory not saved")
        audit("memory_decision", "user", "memory", "rejected")
    recorded = decision if allowed else {
        "type": "memory_candidate", "status": "rejected_by_memory_policy",
    }
    messages.extend((
        {"role": "assistant", "content": json.dumps(
            recorded, ensure_ascii=False,
        )},
        {"role": "user", "content": json.dumps(
            feedback, ensure_ascii=False,
        )},
    ))
    checkpoint()


def _handle_retry(
    observation, effect, replay_policy, current_retry_state, run_control,
    persist_retry, envelope_store, run_id, audit,
):
    """Apply retry semantics after one definite dispatched observation."""
    if observation.get("exit_code") == 0:
        updated = complete_retry(current_retry_state)
        persist_retry(updated)
        return updated, "no_retry"
    failure = classify_failure(observation)
    policy = decide_retry(
        failure["failure_class"], effect, replay_policy,
        current_retry_state["attempt_count"],
        current_retry_state["max_attempts"], run_control["state"],
    )
    updated = record_failure(
        current_retry_state, failure["failure_class"],
        failure["reason_code"], policy,
    )
    persist_retry(updated)
    if envelope_store is not None:
        envelope_store.append_transition(
            run_id, "retry", {
                "failure_class": failure["failure_class"],
                "effect": effect, "replay_policy": replay_policy,
                "attempt_count": updated["attempt_count"],
                "max_attempts": updated["max_attempts"],
                "run_state": run_control["state"],
                "reconciliation_status": (
                    "required" if policy == "reconcile_before_retry"
                    else "not_required"
                ),
                "historical_recorded_observation": True,
            }, {
                "decision": policy,
                "next_delay": updated["backoff_delay"],
            },
        )
    audit(
        "retry_decision", "harness", "action",
        "scheduled" if policy == "retry_with_backoff" else "exhausted",
        reason=f"failure_class={failure['failure_class']}; policy={policy}",
        references={
            "logical_action_id": updated["logical_action_id"],
            "attempt": updated["attempt_count"],
        },
    )
    return updated, policy


def _dispatch_shell_action(
    action_checkpoint, command, policy, approved, run_id,
    current_retry_state, governance_state, clock, persist_action, audit,
    effect_state, fault_injector,
):
    """Seal and dispatch one Shell action through the V26 authority seam."""
    action_refs = {"action_id": action_checkpoint["action_id"]}
    if current_retry_state is not None:
        action_refs.update({
            "logical_action_id": current_retry_state["logical_action_id"],
            "attempt": current_retry_state["attempt_count"],
        })
    authorized = authorize_action(
        checkpoint=action_checkpoint, capability="shell",
        arguments={"command": command}, effect=policy["effect"],
        policy_decision=policy["action"], approval_granted=approved,
        run_id=run_id,
    )

    def execute(arguments):
        value = (
            execute_shell(arguments["command"])
            if governance_state is None else
            execute_shell(
                arguments["command"],
                effective_tool_timeout(governance_state, clock),
            )
        )
        effect_state["tool_has_returned"] = True
        return value

    event_holder = {}

    def after(_action, observation, _terminal):
        event = audit(
            "action_state_changed", "environment", "shell",
            "succeeded" if observation.get("exit_code") == 0 else "failed",
            references=action_refs,
            summary=safe_observation_summary(observation),
        )
        event_holder["event_id"] = event.get("event_id") if event else None

    outcome = dispatch_authorized_action(
        authorized, action_checkpoint, persist_checkpoint=persist_action,
        executor=execute,
        before_dispatch=lambda _action: audit(
            "action_state_changed", "tool", "shell", "started",
            references=action_refs,
        ),
        after_dispatch=after, fault_injector=fault_injector,
    )
    return outcome, event_holder.get("event_id")


def _dispatch_mcp_action(
    action_checkpoint, reference, arguments, policy, effect, approved, run_id,
    current_retry_state, governance_state, clock, persist_action, audit,
    effect_state, fault_injector, mcp_registry, run_control,
    late_mcp_completion_journal,
):
    """Seal and dispatch one MCP action through the V26 authority seam."""
    action_refs = {"action_id": action_checkpoint["action_id"]}
    if current_retry_state is not None:
        action_refs.update({
            "logical_action_id": current_retry_state["logical_action_id"],
            "attempt": current_retry_state["attempt_count"],
        })
    authorized = authorize_action(
        checkpoint=action_checkpoint, capability=reference,
        arguments=arguments, effect=effect,
        policy_decision=policy["action"], approval_granted=approved,
        run_id=run_id,
    )

    def execute(normalized):
        timeout = (
            None if governance_state is None
            else effective_tool_timeout(governance_state, clock)
        )
        kwargs = {}
        if late_mcp_completion_journal is not None:
            kwargs = {
                "late_completion_journal": late_mcp_completion_journal,
                "action_id": action_checkpoint["action_id"],
                "call_id": action_checkpoint["action_id"],
                "run_state": (
                    "deadline_exceeded"
                    if governance_state is not None
                    and deadline_status(governance_state, clock)
                    else run_control["state"]
                ),
            }
        value = execute_mcp_tool(
            mcp_registry, reference, normalized, timeout, **kwargs
        ) if timeout is not None else execute_mcp_tool(
            mcp_registry, reference, normalized, **kwargs
        )
        effect_state["tool_has_returned"] = True
        return value

    event_holder = {}

    def after(_action, observation, _terminal):
        event = audit(
            "mcp_called", "mcp", reference,
            "succeeded" if observation.get("exit_code") == 0 else "failed",
            references=action_refs,
            summary=safe_observation_summary(observation),
        )
        event_holder["event_id"] = event.get("event_id") if event else None

    outcome = dispatch_authorized_action(
        authorized, action_checkpoint, persist_checkpoint=persist_action,
        executor=execute,
        before_dispatch=lambda _action: audit(
            "action_state_changed", "mcp", reference, "started",
            references=action_refs,
        ),
        after_dispatch=after, fault_injector=fault_injector,
    )
    return outcome, event_holder.get("event_id")


def _dispatch_environment_action(action_checkpoint, reference, arguments,
                                 policy, approved, run_id,
                                 current_retry_state, persist_action, audit,
                                 effect_state, fault_injector):
    """Dispatch a fixed registry capability only after sealed authority."""
    effect = policy["effect"]
    refs = {"action_id": action_checkpoint["action_id"],
            "capability": reference, "effect": effect, "zone": "external"}
    if current_retry_state is not None:
        refs.update({"logical_action_id": current_retry_state["logical_action_id"],
                     "attempt": current_retry_state["attempt_count"]})
    authorized = authorize_action(
        checkpoint=action_checkpoint, capability=reference, arguments=arguments,
        effect=effect, policy_decision=policy["action"],
        approval_granted=approved, run_id=run_id,
    )

    def execute(arguments):
        invocation = environment_invocation_from_authorized(authorized)
        if invocation.normalized_args != arguments:
            raise PermissionError("Environment invocation argument drift")
        value = ENVIRONMENT_REGISTRY.invoke(invocation)
        value = value.to_dict()
        if value.get("exit_code") is None:
            value["exit_code"] = (
                127 if value.get("effect_certainty") == "not_started" else -1
            )
        effect_state["tool_has_returned"] = True
        if reference == NOTIFICATION_CAPABILITY:
            trigger_fault(
                fault_injector,
                "after_notification_dispatch_before_checkpoint",
            )
        return value

    holder = {}

    def after(_action, observation, terminal):
        event = audit(
            "action_state_changed", "environment", reference,
            terminal["state"],
            references=refs,
            summary=persisted_safe_observation(observation, reference, {}),
        )
        holder["event_id"] = event.get("event_id") if event else None

    outcome = dispatch_authorized_action(
        authorized, action_checkpoint, persist_checkpoint=persist_action,
        executor=execute,
        before_dispatch=lambda _action: audit(
            "action_state_changed", "harness", reference, "started",
            references=refs,
        ), after_dispatch=after, fault_injector=fault_injector,
        outcome_classifier=environment_checkpoint_outcome,
    )
    return outcome, holder.get("event_id")


def _handle_final_candidate(runtime, decision, request_id, decision_event):
    """Apply verification, planning, output, and result gates."""
    # A final answer is presentation plus claims. Verification, Plan, Output
    # Contract, and authoritative status remain Harness-owned.
    candidate = normalize_final_candidate(decision)["metadata"]
    references = {
        "answer_length": candidate["answer_length"],
        "answer_sha256": candidate["answer_sha256"],
        "claimed_status": candidate["claimed_status"],
        "artifact_ids": candidate["artifact_refs"],
        "evidence_ids": candidate["evidence_refs"],
    }
    references["candidate_digest"] = candidate_metadata_digest(candidate)
    model_event = runtime.audit(
        "model_final_candidate_received", "model", "final_answer", "received",
        references=references,
    )
    # Retain the historical event name as a model-candidate compatibility alias.
    runtime.audit(
        "final_candidate_received", "model", "final_answer", "received",
        references={
            **references,
            "model_candidate_event_id": (
                model_event.get("event_id") if model_event else None
            ),
            "semantic_role": "model_candidate",
        },
    )
    runtime.references["last_model_candidate_identity"] = {
        "event_id": model_event.get("event_id") if model_event else None,
        "candidate_digest": (
            references["candidate_digest"] if model_event else None
        ),
    }
    if runtime.requires_verification:
        if decision == runtime.rejected_final_answer:
            failure = (
                "模型在没有新 tool_call 的情况下重复提交了被 Verification "
                "Gate 拒绝的 final_answer"
            )
            terminal = runtime.emit_result(
                decision, terminal_failure=failure, legacy_value=failure,
            )
            if runtime.return_result:
                return _RuntimePhaseResult(True, True, terminal)
            raise RuntimeError(failure)
        runtime.audit(
            "final_candidate_rejected", "harness", "final_answer",
            "rejected", "verification required before final answer",
            references={
                **references, "authoritative_status": "incomplete",
                "contradiction": True,
            },
        )
        feedback = build_verification_feedback(
            runtime.latest_write_command, runtime.verification_target,
            POLICY_ALLOW,
        )
        runtime.messages.extend((
            {"role": "assistant", "content": json.dumps(
                decision, ensure_ascii=False,
            )},
            {"role": "user", "content": json.dumps(
                feedback, ensure_ascii=False,
            )},
        ))
        runtime.rejected_final_answer = decision
        runtime.checkpoint()
        print("[Verification Gate] verification required before final answer")
        runtime.audit(
            "verification_state_changed", "harness", "final_answer",
            "required", "verification required before final answer",
        )
        return _RuntimePhaseResult(continue_loop=True)

    answer = decision.get("answer", decision.get("final_answer", ""))
    if runtime.output_contract is not None:
        output_gate = current_output_contract_gate(
            runtime.run_id, runtime.output_contract_store,
            runtime.artifact_store, runtime.evidence_store,
        )
        if not output_gate["satisfied"]:
            runtime.checkpoint()
            return _RuntimePhaseResult(
                terminal=True,
                terminal_result=runtime.emit_result(
                    decision,
                    legacy_value="incomplete: output contract unsatisfied",
                ),
            )
    if (
        runtime.mobile_workflow is not None
        and runtime.current_plan is not None
        and runtime.current_plan["status"] == "completed"
    ):
        battery, condition, notification = _mobile_records(runtime)
        branch = "accepted" if notification is not None else "not_required"
        output = _persist_mobile_output(runtime, branch)
        delivered = {
            "type": "final_answer",
            "final_answer": mobile_output_answer(output),
            "claimed_status": "completed",
            "evidence_refs": output["evidence_ids"],
        }
        runtime.audit(
            "mobile_deliverable_bound", "harness", "mobile_workflow",
            "completed", references={
                "output_fingerprint": output["output_fingerprint"],
                "evidence_ids": output["evidence_ids"],
            },
        )
        return _RuntimePhaseResult(
            terminal=True,
            terminal_result=runtime.emit_result(delivered),
        )
    if runtime.current_plan is not None:
        proposal = propose_step_completion(
            runtime.current_plan, runtime.current_step_id, answer,
        )
        if (
            runtime.evidence_store is not None
            and not runtime.plan_had_action
            and not runtime.plan_runtime_state["requires_fresh_grounding"]
        ):
            decision_digest = hashlib.sha256(json.dumps(
                decision, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ).encode()).hexdigest()
            reasoning = create_reasoning_evidence(
                runtime.run_id,
                {"kind": "plan_step", "target": runtime.current_step_id,
                 "claim": "reasoning_completed"},
                decision_event.get("event_id"), decision_digest,
                {"status": "completed"},
                references={"model_request_id": request_id}
                if request_id else {},
            )
            runtime.plan_evidence_ids.append(runtime.persist_evidence(
                reasoning, runtime.current_step_id, True,
            ))
        completed = None
        if runtime.plan_evidence_ids:
            completed = complete_step(
                runtime.current_plan, runtime.current_step_id,
                runtime.plan_evidence_ids,
                evidence_store=runtime.evidence_store,
                current_run_id=runtime.run_id,
                current_reality=runtime.plan_had_action,
                audit_directory=(runtime.audit_writer.directory
                                 if runtime.audit_writer is not None else None),
            )
        if runtime.plan_evidence:
            accepted_evidence = runtime.plan_evidence
        elif (
            runtime.plan_had_action
            or runtime.plan_runtime_state["requires_fresh_grounding"]
        ):
            accepted_evidence = []
        else:
            accepted_evidence = [{
                "kind": "textual_result", "summary": proposal["result"],
            }]
        if completed is None and not accepted_evidence:
            feedback = {
                "type": "plan_feedback",
                "status": "step_completion_rejected",
                "step_id": runtime.current_step_id,
                "reason": "fresh accepted evidence required",
                "instruction": (
                    "Use an allowed observation relevant to the current step "
                    "before returning final_answer again."
                ),
            }
            runtime.messages.extend((
                {"role": "assistant", "content": json.dumps(
                    decision, ensure_ascii=False,
                )},
                {"role": "user", "content": json.dumps(
                    feedback, ensure_ascii=False,
                )},
            ))
            runtime.checkpoint()
            print("[Plan Evidence Gate] step completion requires evidence")
            return _RuntimePhaseResult(continue_loop=True)
        if completed is None:
            completed = complete_step(
                runtime.current_plan, runtime.current_step_id,
                accepted_evidence,
            )
        runtime.current_plan.clear()
        runtime.current_plan.update(completed)
        print(f"[Plan] step completed：{runtime.current_step_id}")
        runtime.audit(
            "plan_step_changed", "harness", "plan_step", "completed",
            references={"plan_id": runtime.current_plan["plan_id"],
                        "step_id": runtime.current_step_id},
        )
    if runtime.result_store is None and screen_result_answer(answer)[0]:
        runtime.messages.append({
            "role": "assistant",
            "content": json.dumps(decision, ensure_ascii=False),
        })
    runtime.checkpoint()
    final_value = runtime.emit_result(decision, legacy_value=answer)
    safe_answer = (
        final_value["answer"] if isinstance(final_value, dict) else final_value
    )
    print(f"[Harness Final Result] {safe_answer}")
    return _RuntimePhaseResult(
        terminal=True, terminal_result=final_value,
    )


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


def _runtime_audit(
    runtime, event_type, actor, subject=None, outcome=None, reason=None,
    references=None, summary=None,
):
    if runtime.audit_writer is None:
        return None
    try:
        return runtime.audit_writer.append(
            event_type, actor, subject, outcome, reason, references, summary,
        )
    except Exception as error:
        if not runtime.effect_state["tool_has_returned"]:
            raise
        runtime.mark_degraded(
            f"audit_append_after_tool: {type(error).__name__}",
        )
        return None


def _mark_runtime_degraded(runtime, reason, stage=None):
    runtime.verification["degraded"] = True
    runtime.verification["degraded_reason"] = str(reason)[:240]
    runtime.verification["degraded_stage"] = (
        stage or str(reason).partition("_")[0].partition(":")[0]
    )[:64]
    if runtime.save_checkpoint:
        try:
            runtime.save_checkpoint()
        except Exception:
            pass


def _persist_runtime_evidence(
    runtime, record, step_id=None, accepted=None,
):
    if runtime.evidence_store is None:
        return None
    if record.get("evidence_type") == "termux_observation":
        trigger_fault(
            runtime.fault_injector,
            "after_environment_success_before_evidence",
        )
    trigger_fault(runtime.fault_injector, "after_session_before_evidence")
    try:
        runtime.evidence_store.save(record)
    except Exception as error:
        runtime.mark_degraded(
            f"evidence_persist: {type(error).__name__}", "evidence",
        )
        return None
    if record.get("evidence_type") == "termux_observation":
        trigger_fault(
            runtime.fault_injector,
            "after_evidence_before_harness_result",
        )
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
        runtime.audit(
            "evidence_created", "harness", "evidence", "created",
            references=references,
        )
        if accepted is not None:
            runtime.audit(
                "evidence_accepted" if accepted else "evidence_rejected",
                "harness", "evidence",
                "accepted" if accepted else "rejected",
                references=references,
            )
    except Exception as error:
        runtime.mark_degraded(
            f"audit_append_after_evidence: {type(error).__name__}", "audit",
        )
    return record["evidence_id"]


def _checkpoint_runtime(runtime):
    runtime.verification["requires_verification"] = (
        runtime.requires_verification
    )
    runtime.verification["latest_write_command"] = (
        runtime.latest_write_command
    )
    runtime.verification["verification_target"] = runtime.verification_target
    if runtime.save_checkpoint:
        try:
            runtime.save_checkpoint()
        except Exception as error:
            if (
                not runtime.effect_state["tool_has_returned"]
                and not runtime.verification.get("degraded")
            ):
                raise
            runtime.verification["degraded"] = True
            runtime.verification["degraded_reason"] = (
                runtime.verification.get("degraded_reason")
                or f"session_persist: {type(error).__name__}"
            )
            runtime.verification["degraded_stage"] = (
                runtime.verification.get("degraded_stage") or "session"
            )


def _bootstrap_agent_runtime(
    task, provider, messages, verification, save_checkpoint, memory_store,
    mcp_registry, context_assembler, context_budget, current_plan,
    plan_revision_history, require_plan_grounding, current_action_checkpoint,
    save_action_checkpoint, run_control, save_run_control,
    current_retry_state, save_retry_state, retry_sleeper, governance_state,
    save_governance_state, clock, step_timeout_seconds, audit_writer,
    session_id, audit_directory, policy_binding, previous_run_id,
    previous_policy_fingerprint, evidence_store, output_contract,
    artifact_store, output_contract_store, result_store, return_result,
    fault_injector, late_mcp_completion_journal, termux_delegated_ceiling,
    mobile_workflow, mobile_workflow_output_store, resume_existing_run,
):
    """Bind run identity, stores, immutable history, and runtime references."""
    run_id = audit_writer.run_id if audit_writer is not None else new_run_id()
    if audit_writer is None and session_id is not None:
        audit_writer = (
            AuditWriter(session_id, run_id, audit_directory)
            if audit_directory else AuditWriter(session_id, run_id)
        )
    if evidence_store is None and audit_writer is not None:
        evidence_store = EvidenceStore(os.path.join(
            audit_writer.directory, "evidence",
        ))
    if mobile_workflow is not None:
        mobile_workflow = validate_mobile_workflow(mobile_workflow)
        base = (
            audit_writer.directory if audit_writer is not None else
            audit_directory or os.path.join(os.getcwd(), ".audit")
        )
        mobile_workflow_output_store = (
            mobile_workflow_output_store
            or MobileWorkflowOutputStore(os.path.join(
                base, "mobile_workflow_outputs",
            ))
        )
    if output_contract is not None:
        evidence_store = evidence_store or EvidenceStore()
        base = (
            audit_writer.directory if audit_writer is not None
            else audit_directory or os.path.join(os.getcwd(), ".audit")
        )
        artifact_store = artifact_store or ArtifactStore(os.path.join(
            base, "artifacts",
        ))
        output_contract_store = (
            output_contract_store
            or OutputContractStore(os.path.join(base, "output_contracts"))
        )
        output_contract = create_output_contract(run_id, output_contract)
        output_contract_store.save(output_contract)

    memory_store = memory_store or MemoryStore()
    context_assembler = context_assembler or RuntimeContextAssembler(
        memory_store=memory_store, mcp_registry=mcp_registry,
        termux_capabilities=True,
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
    run_control = run_control if run_control is not None else create_run_control()
    validate_run_control(run_control)

    references = {
        "task": task, "provider": provider, "messages": messages,
        "verification": verification, "save_checkpoint": save_checkpoint,
        "memory_store": memory_store, "mcp_registry": mcp_registry,
        "context_assembler": context_assembler,
        "context_budget": context_budget, "current_plan": current_plan,
        "plan_revision_history": (
            plan_revision_history if plan_revision_history is not None else []
        ),
        "require_plan_grounding": require_plan_grounding,
        "save_action_checkpoint": save_action_checkpoint,
        "run_control": run_control, "save_run_control": save_run_control,
        "save_retry_state": save_retry_state,
        "retry_sleeper": retry_sleeper,
        "governance_state": governance_state,
        "save_governance_state": save_governance_state,
        "clock": clock, "step_timeout_seconds": step_timeout_seconds,
        "audit_writer": audit_writer, "audit_directory": audit_directory,
        "previous_run_id": previous_run_id,
        "previous_policy_fingerprint": previous_policy_fingerprint,
        "evidence_store": evidence_store,
        "output_contract": output_contract,
        "artifact_store": artifact_store,
        "output_contract_store": output_contract_store,
        "result_store": result_store, "return_result": return_result,
        "fault_injector": fault_injector,
        "late_mcp_completion_journal": late_mcp_completion_journal,
        "termux_delegated_ceiling": termux_delegated_ceiling,
        "mobile_workflow": mobile_workflow,
        "mobile_workflow_output_store": mobile_workflow_output_store,
        "resume_existing_run": bool(resume_existing_run),
        "run_id": run_id, "effect_state": {"tool_has_returned": False},
    }
    runtime = _AgentRuntimeState(
        references=references,
        requires_verification=verification["requires_verification"],
        latest_write_command=verification.get("latest_write_command"),
        verification_target=verification.get("verification_target"),
        verification_obligation=bool(
            verification["requires_verification"]
        ),
        current_retry_state=current_retry_state,
        action_checkpoint=current_action_checkpoint,
    )

    if policy_binding is None:
        policy_directory = os.path.join(
            audit_writer.directory if audit_writer is not None else
            audit_directory or os.path.join(os.getcwd(), ".audit"),
            "policies",
        )
        policy_binding = bind_current_policy(mcp_registry, policy_directory)
    runtime.references["policy_binding"] = policy_binding

    started = {
        "policy_schema_version": policy_binding.schema_version,
        "policy_revision": policy_binding.revision,
        "policy_fingerprint": policy_binding.fingerprint,
    }
    if output_contract is not None:
        started["output_contract_fingerprint"] = output_contract[
            "contract_fingerprint"
        ]
    if audit_writer is not None:
        manifest_store = RunManifestStore(os.path.join(
            audit_writer.directory, "manifests",
        ))
        if resume_existing_run:
            manifest = manifest_store.load(run_id)
        else:
            configuration = build_configuration(
                task, provider, policy_binding, context_assembler, context_budget,
            )
            manifest = build_manifest(
                run_id, audit_writer.session_id, configuration,
            )
            manifest_store.persist(manifest)
        started["manifest_fingerprint"] = manifest[
            "configuration_fingerprint"
        ]
        envelope_store = RunEnvelopeStore(os.path.join(
            audit_writer.directory, "envelopes",
        ))
        if resume_existing_run:
            envelope = envelope_store.load(run_id)
        else:
            envelope = build_envelope(
                run_id, audit_writer.session_id, task, messages or [], manifest,
                current_plan=current_plan,
                control_state={
                    "verification": verification, "run_control": run_control,
                    "retry_state": current_retry_state,
                    "governance_state": governance_state,
                    "output_contract": output_contract,
                },
            )
            envelope_store.persist(envelope)
        started["envelope_fingerprint"] = envelope["envelope_fingerprint"]
        runtime.references.update({
            "manifest_store": manifest_store,
            "envelope_store": envelope_store,
        })
    else:
        runtime.references.update({
            "manifest_store": None, "envelope_store": None,
        })
    if previous_run_id is not None:
        previous_manifest_fingerprint = None
        if audit_writer is not None:
            try:
                previous_manifest_fingerprint = manifest_store.load(
                    previous_run_id,
                )["configuration_fingerprint"]
            except ValueError:
                pass
        started.update({
            "previous_run_id": previous_run_id,
            "previous_policy_fingerprint": previous_policy_fingerprint,
            "policy_drift": (
                previous_policy_fingerprint != policy_binding.fingerprint
            ),
            "previous_manifest_fingerprint": previous_manifest_fingerprint,
            "runtime_drift": (
                previous_manifest_fingerprint is not None
                and previous_manifest_fingerprint
                != started.get("manifest_fingerprint")
            ),
        })
    if not resume_existing_run:
        runtime.audit(
            "run_started", "harness", "run", "running", references=started,
        )
    else:
        runtime.audit(
            "run_resumed", "harness", "run", "running", references={
                "harness_run_id": run_id,
            },
        )
    if audit_writer is not None:
        runtime.references["result_store"] = result_store or ResultStore(
            os.path.join(audit_writer.directory, "results"),
        )
    return runtime


def _persist_authoritative_candidate_finalized(runtime, state, candidate):
    """Durably distinguish the Harness candidate from the model proposal."""
    if runtime.audit_writer is None:
        return None
    digest = candidate_metadata_digest(candidate)
    model_identity = runtime.references.get("last_model_candidate_identity") or {}
    mobile_output = None
    if runtime.mobile_workflow_output_store is not None:
        mobile_output = runtime.mobile_workflow_output_store.load(
            runtime.run_id, missing_ok=True,
        )
    references = {
        "candidate_digest": digest,
        "answer_length": candidate["answer_length"],
        "answer_sha256": candidate["answer_sha256"],
        "claimed_status": candidate["claimed_status"],
        "artifact_ids": candidate["artifact_refs"],
        "evidence_ids": candidate["evidence_refs"],
        "contradiction": candidate["contradiction"],
        "plan_id": (state["plan"] or {}).get("plan_id"),
        "normalization_source": (
            "mobile_output_contract" if mobile_output is not None
            else "result_contract"
        ),
        "model_candidate_event_id": model_identity.get("event_id"),
        "model_candidate_digest": model_identity.get("candidate_digest"),
        "model_identity_equal": (
            model_identity.get("candidate_digest") == digest
            if model_identity.get("candidate_digest") is not None else None
        ),
    }
    if mobile_output is not None:
        references.update({
            "mobile_output_fingerprint": mobile_output["output_fingerprint"],
            "mobile_contract_satisfied": mobile_output["satisfied"],
            "mobile_branch": mobile_output["branch"],
            "mobile_evidence_ids": mobile_output["evidence_ids"],
        })
    existing = [
        event for event in read_events(
            runtime.run_id, runtime.audit_writer.directory, missing_ok=True,
        )
        if event.get("event_type") == "authoritative_candidate_finalized"
    ]
    if existing:
        if (
            len(existing) != 1
            or (existing[0].get("references") or {}).get("candidate_digest")
            != digest
        ):
            raise RuntimeError("authoritative candidate identity conflict")
        return existing[0]
    # This event is a Result publication prerequisite.  Do not use the runtime
    # audit wrapper here because post-action audit degradation must not be
    # swallowed before Result binding becomes durable.
    return runtime.audit_writer.append(
        "authoritative_candidate_finalized", "harness", "final_answer",
        "finalized", references=references,
    )


def _emit_runtime_result(
    runtime, candidate=None, terminal_failure=None, blocking_reason=None,
    legacy_value=None,
):
    """Bind one terminal boundary and preserve the legacy string API."""
    if runtime.effect_state["tool_has_returned"]:
        trigger_fault(runtime.fault_injector, "after_artifact_before_result")
    if (
        runtime.verification.get("degraded")
        and terminal_failure is None and blocking_reason is None
    ):
        blocking_reason = (
            runtime.verification.get("degraded_reason")
            or "persistence degraded"
        )
    if (
        blocking_reason is None
        and runtime.current_retry_state is not None
        and runtime.current_retry_state.get("state") == "exhausted"
    ):
        blocking_reason = "retry exhausted; replan or block required"
    output_status = None
    if runtime.output_contract is not None:
        output_status = current_output_contract_gate(
            runtime.run_id, runtime.output_contract_store,
            runtime.artifact_store, runtime.evidence_store,
        )
    audit_directory = (
        runtime.audit_writer.directory
        if runtime.audit_writer is not None else
        runtime.audit_directory or os.path.join(os.getcwd(), ".audit")
    )
    # Binding observes accumulated Harness state; model ``claimed_status`` never
    # overrides cancellation, failure, exhaustion, verification, or contracts.
    state, normalized = build_authoritative_result_state(
        runtime.run_id, candidate, runtime.run_control, terminal_failure,
        blocking_reason, runtime.current_plan, output_status,
        runtime.verification_obligation, runtime.artifact_store,
        runtime.evidence_store, audit_directory,
    )
    binding = evaluate_result_contract(state)
    finalized_candidate = finalize_authoritative_candidate(state, binding)
    # A successful Environment action followed by an Evidence-store failure is
    # not a terminal Harness result.  The action/Observation truth is already
    # durable and must be repaired without dispatching the capability again.
    # Returning this transient fail-closed value preserves the public call
    # shape, while deliberately publishing no Result or Bridge-authoritative
    # transition that could be mistaken for completion.
    if (
        runtime.effect_state["tool_has_returned"]
        and runtime.verification.get("degraded_stage") == "evidence"
    ):
        result, _ = bind_final_result(state, normalized)
        return (
            result if runtime.return_result
            else "incomplete: environment evidence recovery required"
        )
    _persist_authoritative_candidate_finalized(
        runtime, state, finalized_candidate,
    )
    result, rebound = bind_final_result(state, normalized)
    if rebound != binding or result["candidate"] != finalized_candidate:
        raise RuntimeError("authoritative candidate binding drift")
    if runtime.envelope_store is not None:
        runtime.envelope_store.append_transition(
            runtime.run_id, "result_binding", state, binding,
            idempotent=True,
        )
    identity = answer_identity(result["answer"])
    if result["candidate"]["contradiction"]:
        runtime.audit(
            "final_candidate_rejected", "harness", "final_answer",
            "rejected", result["reason"], references={
                "answer_length": result["candidate"]["answer_length"],
                "answer_sha256": result["candidate"]["answer_sha256"],
                "claimed_status": result["candidate"]["claimed_status"],
                "authoritative_status": result["status"],
                "artifact_ids": result["artifact_ids"],
                "evidence_ids": result["evidence_ids"],
                "contradiction": True,
            },
        )
    if runtime.result_store is not None:
        try:
            runtime.result_store.save(result)
        except Exception as error:
            runtime.mark_degraded(
                f"result_persist: {type(error).__name__}", "result",
            )
            degraded_state, degraded_candidate = (
                build_authoritative_result_state(
                    runtime.run_id, candidate, runtime.run_control,
                    terminal_failure,
                    runtime.verification["degraded_reason"],
                    runtime.current_plan, output_status,
                    runtime.verification_obligation, runtime.artifact_store,
                    runtime.evidence_store, audit_directory,
                )
            )
            degraded_result, _binding = bind_final_result(
                degraded_state, degraded_candidate,
            )
            return (
                degraded_result if runtime.return_result
                else "incomplete: persistence degraded"
            )
    try:
        runtime.audit(
            "final_result_emitted", "harness", "result", result["status"],
            result["reason"], references={
                **identity,
                "claimed_status": result["candidate"]["claimed_status"],
                "authoritative_status": result["status"],
                "artifact_ids": result["artifact_ids"],
                "evidence_ids": result["evidence_ids"],
                "contradiction": result["candidate"]["contradiction"],
                "result_fingerprint": result["result_fingerprint"],
            },
        )
        runtime.audit(
            "run_state_changed", "harness", "run", result["status"],
            result["reason"],
        )
    except Exception as error:
        runtime.mark_degraded(
            f"audit_append_after_result: {type(error).__name__}", "audit",
        )
    if runtime.return_result:
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


def _finalize_runtime_artifact(runtime, verification_record, evidence_id, verification_accepted):
    """Persist an immutable artifact after reused verification."""
    'Persist one immutable historical version after reused V22 verification.'
    if runtime.pending_artifact is None or runtime.artifact_store is None:
        return None
    candidate = runtime.pending_artifact
    required = next((item for item in runtime.output_contract['required_artifacts'] if item['artifact_type'] == 'workspace_file' and item['path'] == runtime.pending_artifact['path']), None)
    draft = create_artifact(runtime.run_id, runtime.pending_artifact['path'], 'materialized', runtime.pending_artifact['content_identity'], runtime.pending_artifact['producer'], [evidence_id] if evidence_id else [], required or {}, references={'verification_accepted': bool(verification_accepted)})
    previous = select_supersession(draft, runtime.run_id, runtime.artifact_store, runtime.evidence_store.directory if runtime.evidence_store is not None else None, runtime.audit_writer.directory if runtime.audit_writer is not None else runtime.audit_directory or os.path.join(os.getcwd(), '.audit'))
    if previous is not None:
        draft = create_artifact(runtime.run_id, runtime.pending_artifact['path'], 'materialized', runtime.pending_artifact['content_identity'], runtime.pending_artifact['producer'], [evidence_id] if evidence_id else [], required or {}, references={'verification_accepted': bool(verification_accepted)}, supersedes_artifact_id=previous['artifact_id'], artifact_id=draft['artifact_id'], created_at=draft['created_at'])
    transition_inputs = None
    if required is None:
        status = 'verified' if verification_accepted else 'rejected'
        result = {'accepted': False, 'status': status, 'reason': 'not required by Output Contract', 'unsatisfied_requirements': []}
    else:
        transition_inputs, result = evaluate_artifact_contract(draft, [verification_record] if verification_record else [], required)
        status = result['status']
    record = create_artifact(runtime.run_id, runtime.pending_artifact['path'], status, runtime.pending_artifact['content_identity'], runtime.pending_artifact['producer'], [evidence_id] if evidence_id else [], required or {}, references={'contract_result': result}, supersedes_artifact_id=previous['artifact_id'] if previous else None, artifact_id=draft['artifact_id'], created_at=draft['created_at'])
    trigger_fault(runtime.fault_injector, 'after_evidence_before_artifact')
    try:
        runtime.artifact_store.save(record)
    except Exception as error:
        runtime.mark_degraded(f'artifact_persist: {type(error).__name__}', 'artifact')
        return None
    refs = {'artifact_id': record['artifact_id'], 'artifact_fingerprint': record['artifact_fingerprint'], 'path': record['path'], 'status': record['status'], 'evidence_ids': list(record['evidence_ids'])}
    try:
        runtime.audit('artifact_proposed', 'harness', 'artifact', 'proposed', references=refs)
        runtime.audit('artifact_materialized', 'harness', 'artifact', 'materialized', references=refs)
        if verification_accepted:
            runtime.audit('artifact_verified', 'harness', 'artifact', 'verified', references=refs)
        if status in {'accepted', 'rejected'}:
            runtime.audit('artifact_accepted' if status == 'accepted' else 'artifact_rejected', 'harness', 'artifact', status, result.get('reason'), references=refs)
        if previous is not None:
            runtime.audit('artifact_superseded', 'harness', 'artifact', 'superseded', references={**refs, 'superseded_artifact_id': previous['artifact_id']})
    except Exception as error:
        runtime.mark_degraded(f'audit_append_after_artifact: {type(error).__name__}')
    if runtime.envelope_store is not None and transition_inputs is not None:
        runtime.envelope_store.append_transition(runtime.run_id, 'artifact_contract', transition_inputs, result)
    runtime.pending_artifact = None if verification_accepted else candidate
    return record


def _initialize_runtime_execution(runtime):
    """Recover durable state, apply entry gates, and select the plan step."""
    runtime.references["plan_runtime_state"] = {
        "requires_fresh_grounding": bool(runtime.require_plan_grounding),
    }
    runtime.references["safety_entry"] = bool(
        runtime.action_checkpoint
        and runtime.action_checkpoint.get("state") in {"executing", "unknown"}
        and runtime.action_checkpoint.get("effect")
        in {"side_effecting", "unknown"}
    )
    # Paused/cancelled/expired runs schedule no normal work. The sole entry
    # exception is bounded reconciliation of an already-unknown side effect.
    if (
        not can_schedule_action(runtime.run_control)
        and not runtime.safety_entry
    ):
        legacy = f"run {runtime.run_control['state']}"
        return _RuntimePhaseResult(
            terminal=True,
            terminal_result=runtime.emit_result(
                blocking_reason=(
                    None if runtime.run_control["state"]
                    in {"cancel_requested", "cancelled"}
                    else runtime.run_control.get("reason") or legacy
                ),
                legacy_value=legacy,
            ),
        )
    if runtime.governance_state is not None:
        validate_governance_state(runtime.governance_state)
        decision = normal_action_decision(
            runtime.governance_state, clock=runtime.clock,
        )
        if not decision["allowed"] and not runtime.safety_entry:
            legacy = f"blocked: {decision['reason']}"
            return _RuntimePhaseResult(
                terminal=True,
                terminal_result=runtime.emit_result(
                    blocking_reason=decision["reason"], legacy_value=legacy,
                ),
            )
    if not runtime.resume_existing_run:
        runtime.messages.append({"role": "user", "content": runtime.task})
    if runtime.save_checkpoint:
        runtime.save_checkpoint()
    if runtime.current_retry_state is not None:
        validate_retry_state(runtime.current_retry_state)

    # Session continuity is not Current Reality. Recovery establishes what is
    # durable, then forces fresh grounding before Plan completion.
    if runtime.action_checkpoint is not None:
        validate_action_checkpoint(runtime.action_checkpoint)
        recovered, recovery_action = recover_action_checkpoint(
            runtime.action_checkpoint,
        )
        runtime.recovered_action = recovered
        if recovered != runtime.action_checkpoint:
            runtime.persist_action(recovered)
        runtime.plan_runtime_state["requires_fresh_grounding"] = True
        if recovery_action in {
            "retry_with_fresh_approval", "retry_as_new_action",
            "reconcile_or_block",
        }:
            runtime.plan_runtime_state["action_recovery"] = (
                recovery_control_state(recovered)
            )
        if recovered["state"] == "succeeded" and runtime.current_plan is not None:
            runtime.plan_evidence.append({
                "kind": "action_checkpoint",
                "summary": "persisted successful action observation",
                "verified": True,
                "action_id": recovered["action_id"],
            })

    if runtime.current_plan is None:
        return _RuntimePhaseResult()
    validate_plan(runtime.current_plan)
    validate_revision_history(runtime.plan_revision_history)
    runtime.audit(
        "plan_created", "harness", "plan", runtime.current_plan["status"],
        references={
            "plan_id": runtime.current_plan["plan_id"],
            "plan_version": runtime.current_plan["version"],
        },
    )
    if runtime.current_plan["status"] != "active":
        if runtime.current_plan["status"] == "failed":
            value = runtime.emit_result(
                terminal_failure="plan failed",
                legacy_value="failed: plan failed",
            )
        elif runtime.current_plan["status"] == "blocked":
            value = runtime.emit_result(
                blocking_reason="plan blocked",
                legacy_value="blocked: plan blocked",
            )
        else:
            value = runtime.emit_result(
                legacy_value="incomplete: final candidate missing",
            )
        return _RuntimePhaseResult(terminal=True, terminal_result=value)
    current = next((
        item for item in runtime.current_plan["steps"]
        if item["status"] == "in_progress"
    ), None)
    if current is None:
        ready = select_ready_step(runtime.current_plan)
        if runtime.envelope_store is not None:
            runtime.envelope_store.append_transition(
                runtime.run_id, "planning",
                planning_transition_input(runtime.current_plan),
                {"selected_step_id": ready["id"] if ready else None},
            )
        if ready is None:
            failure = "active plan 没有 ready step"
            terminal = runtime.emit_result(
                blocking_reason=failure,
                legacy_value=f"blocked: {failure}",
            )
            if runtime.return_result:
                return _RuntimePhaseResult(
                    terminal=True, terminal_result=terminal,
                )
            raise RuntimeError(failure)
        started = start_step(runtime.current_plan, ready["id"])
        runtime.current_plan.clear()
        runtime.current_plan.update(started)
        current = ready
    runtime.current_step_id = current["id"]
    runtime.audit(
        "plan_step_changed", "harness", "plan_step", "started",
        references={
            "plan_id": runtime.current_plan["plan_id"],
            "step_id": runtime.current_step_id,
        },
    )
    if (
        runtime.governance_state is not None
        and runtime.step_timeout_seconds is not None
        and runtime.governance_state["step_deadline_at"] is None
    ):
        runtime.persist_governance(start_step_deadline(
            runtime.governance_state, runtime.step_timeout_seconds,
            runtime.clock,
        ))
    runtime.checkpoint()
    return _RuntimePhaseResult()


# ==================== Agent Loop ====================

def _replace_runtime_plan(runtime, plan):
    runtime.current_plan.clear()
    runtime.current_plan.update(plan)


def _mobile_records(runtime):
    battery = find_step_evidence(
        runtime.evidence_store, runtime.run_id,
        BATTERY_CAPABILITY, BATTERY_STEP_ID,
    )
    if battery is None:
        return None, None, None
    condition = find_condition_evidence(
        runtime.evidence_store, runtime.run_id, battery["evidence_id"],
    )
    notification = find_step_evidence(
        runtime.evidence_store, runtime.run_id,
        NOTIFICATION_CAPABILITY, NOTIFICATION_STEP_ID,
    )
    return battery, condition, notification


def _advance_mobile_workflow(runtime):
    """Advance only from durable accepted Evidence; never dispatch here."""
    battery, condition_record, notification = _mobile_records(runtime)
    if battery is None:
        return
    threshold = runtime.mobile_workflow["threshold"]
    decision = evaluate_battery_condition(battery, threshold, runtime.run_id)
    battery_step = next(item for item in runtime.current_plan["steps"]
                        if item["id"] == BATTERY_STEP_ID)
    if battery_step["status"] == "in_progress":
        _replace_runtime_plan(runtime, complete_step(
            runtime.current_plan, BATTERY_STEP_ID, [battery["evidence_id"]],
            evidence_store=runtime.evidence_store,
            current_run_id=runtime.run_id, current_reality=True,
            audit_directory=(runtime.audit_writer.directory
                             if runtime.audit_writer is not None else None),
        ))
        runtime.audit(
            "plan_step_changed", "harness", "plan_step", "completed",
            references={"plan_id": runtime.current_plan["plan_id"],
                        "step_id": BATTERY_STEP_ID},
        )
    notification_step = next(
        item for item in runtime.current_plan["steps"]
        if item["id"] == NOTIFICATION_STEP_ID
    )
    if notification_step["status"] == "pending":
        _replace_runtime_plan(runtime, start_step(
            runtime.current_plan, NOTIFICATION_STEP_ID,
        ))
        runtime.current_step_id = NOTIFICATION_STEP_ID
        runtime.audit(
            "plan_step_changed", "harness", "plan_step", "started",
            references={"plan_id": runtime.current_plan["plan_id"],
                        "step_id": NOTIFICATION_STEP_ID},
        )
    if condition_record is None:
        condition_record = create_mobile_condition_evidence(
            runtime.run_id, decision,
        )
        runtime.persist_evidence(
            condition_record, NOTIFICATION_STEP_ID, True,
        )
    condition = next(
        item for item in runtime.current_plan["steps"]
        if item["id"] == NOTIFICATION_STEP_ID
    )["condition"]
    if condition["outcome"] is None:
        _replace_runtime_plan(runtime, bind_mobile_condition(
            runtime.current_plan, decision, condition_record["evidence_id"],
        ))
        runtime.audit(
            "condition_evaluated", "harness", NOTIFICATION_STEP_ID,
            "true" if decision["outcome"] else "false",
            references={
                "battery_evidence_id": battery["evidence_id"],
                "condition_evidence_id": condition_record["evidence_id"],
                "operator": "lt", "threshold": threshold,
            },
        )
    notification_step = next(
        item for item in runtime.current_plan["steps"]
        if item["id"] == NOTIFICATION_STEP_ID
    )
    if not decision["outcome"] and notification_step["status"] == "in_progress":
        _replace_runtime_plan(runtime, complete_step(
            runtime.current_plan, NOTIFICATION_STEP_ID,
            [condition_record["evidence_id"]],
            evidence_store=runtime.evidence_store,
            current_run_id=runtime.run_id, current_reality=True,
            audit_directory=(runtime.audit_writer.directory
                             if runtime.audit_writer is not None else None),
        ))
        runtime.audit(
            "plan_step_changed", "harness", "plan_step", "completed",
            reason="notification condition was false",
            references={"plan_id": runtime.current_plan["plan_id"],
                        "step_id": NOTIFICATION_STEP_ID},
        )
    elif (
        decision["outcome"] and notification is not None
        and notification_step["status"] == "in_progress"
    ):
        _replace_runtime_plan(runtime, complete_step(
            runtime.current_plan, NOTIFICATION_STEP_ID,
            [condition_record["evidence_id"], notification["evidence_id"]],
            evidence_store=runtime.evidence_store,
            current_run_id=runtime.run_id, current_reality=True,
            audit_directory=(runtime.audit_writer.directory
                             if runtime.audit_writer is not None else None),
        ))
        runtime.audit(
            "plan_step_changed", "harness", "plan_step", "completed",
            references={"plan_id": runtime.current_plan["plan_id"],
                        "step_id": NOTIFICATION_STEP_ID},
        )
    runtime.plan_runtime_state["requires_fresh_grounding"] = False
    runtime.checkpoint()


def _persist_mobile_output(runtime, branch):
    existing = runtime.mobile_workflow_output_store.load(
        runtime.run_id, missing_ok=True,
    )
    if existing is not None:
        return existing
    battery, condition, notification = _mobile_records(runtime)
    if battery is None or condition is None:
        raise MobileWorkflowError("mobile output requires durable condition chain")
    output = build_mobile_workflow_output(
        runtime.run_id, runtime.current_plan, battery, condition, branch,
        notification if branch == "accepted" else None,
    )
    runtime.mobile_workflow_output_store.save(output)
    runtime.audit(
        "mobile_output_contract_evaluated", "harness", "mobile_workflow",
        "satisfied" if output["satisfied"] else "unsatisfied",
        references={
            "output_fingerprint": output["output_fingerprint"],
            "evidence_ids": output["evidence_ids"],
            "branch": output["branch"],
        },
    )
    return output


def _resume_mobile_workflow(runtime):
    if runtime.current_plan is None:
        return _RuntimePhaseResult(
            terminal=True,
            terminal_result=runtime.emit_result(
                terminal_failure="mobile workflow Plan missing",
                legacy_value="failed: mobile workflow Plan missing",
            ),
        )
    checkpoint = runtime.recovered_action
    if (
        checkpoint is not None
        and checkpoint.get("tool") == NOTIFICATION_CAPABILITY
        and checkpoint.get("state") == "unknown"
    ):
        output = _persist_mobile_output(runtime, "unknown")
        return _RuntimePhaseResult(
            terminal=True,
            terminal_result=runtime.emit_result(
                blocking_reason="unknown notification effect; reconciliation unavailable",
                legacy_value=mobile_output_answer(output),
            ),
        )
    try:
        _advance_mobile_workflow(runtime)
    except MobileWorkflowError as error:
        return _RuntimePhaseResult(
            terminal=True,
            terminal_result=runtime.emit_result(
                legacy_value=f"incomplete: {error}",
            ),
        )
    current = next((item for item in runtime.current_plan["steps"]
                    if item["status"] == "in_progress"), None)
    runtime.current_step_id = current["id"] if current else NOTIFICATION_STEP_ID
    return _RuntimePhaseResult()


def _mobile_action_gate(runtime, reference):
    if runtime.mobile_workflow is None:
        return None
    if runtime.current_plan["status"] == "completed":
        return "mobile workflow Plan already completed"
    if runtime.current_step_id == BATTERY_STEP_ID:
        return None if reference == BATTERY_CAPABILITY else (
            "battery observation step accepts only its registered capability"
        )
    if runtime.current_step_id == NOTIFICATION_STEP_ID:
        if reference != NOTIFICATION_CAPABILITY:
            return "notification step accepts only its registered capability"
        if not condition_allows_notification(
            runtime.current_plan, runtime.evidence_store, runtime.run_id,
            runtime.audit_writer.directory
            if runtime.audit_writer is not None else None,
        ):
            return "notification requires accepted fresh battery Evidence"
        return None
    return "mobile workflow has no executable step"

def _run_agent_runtime(
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
    termux_delegated_ceiling=None,
    mobile_workflow=None, mobile_workflow_output_store=None,
    resume_existing_run=False,
):
    """Orchestrate explicit bootstrap, decision, execution, and completion phases."""
    runtime = _bootstrap_agent_runtime(
        task, provider, messages, verification, save_checkpoint, memory_store,
        mcp_registry, context_assembler, context_budget, current_plan,
        plan_revision_history, require_plan_grounding,
        current_action_checkpoint, save_action_checkpoint, run_control,
        save_run_control, current_retry_state, save_retry_state,
        retry_sleeper, governance_state, save_governance_state, clock,
        step_timeout_seconds, audit_writer, session_id, audit_directory,
        policy_binding, previous_run_id, previous_policy_fingerprint,
        evidence_store, output_contract, artifact_store,
        output_contract_store, result_store, return_result, fault_injector,
        late_mcp_completion_journal, termux_delegated_ceiling,
        mobile_workflow, mobile_workflow_output_store, resume_existing_run,
    )
    phase = _initialize_runtime_execution(runtime)
    if phase.terminal:
        return phase.terminal_result
    if runtime.mobile_workflow is not None:
        phase = _resume_mobile_workflow(runtime)
        if phase.terminal:
            return phase.terminal_result

    for step in range(1, max_steps + 1):
        if not runtime.scheduling_allowed() and not runtime.safety_entry:
            runtime.settle_run_control()
            reason = (
                deadline_status(runtime.governance_state, runtime.clock)
                if runtime.governance_state is not None else None
            )
            legacy = (
                runtime.deadline_block(reason) if reason
                else f"run {runtime.run_control['state']}"
            )
            return runtime.emit_result(
                blocking_reason=(
                    reason or runtime.run_control.get("reason") or legacy
                ),
                legacy_value=legacy,
            )
        print(f"\n[Harness] 第 {step}/{max_steps} 步：请求模型做决定")
        try:
            decision, request_id, decision_event = _prepare_turn(
                runtime.provider, runtime.messages, runtime.context_assembler,
                {
                    "requires_verification": runtime.requires_verification,
                    "latest_write_command": runtime.latest_write_command,
                    "verification_target": runtime.verification_target,
                    "action_recovery": runtime.plan_runtime_state.get(
                        "action_recovery"
                    ),
                    "run_control": runtime.run_control,
                    "retry_state": runtime.current_retry_state,
                    "governance_state": runtime.governance_state,
                    "output_contract": runtime.output_contract,
                    "clock": runtime.clock,
                    "safety_reconciliation": runtime.safety_entry,
                },
                runtime.context_budget, runtime.current_plan,
                runtime.plan_runtime_state, runtime.envelope_store,
                runtime.run_id, runtime.audit,
            )
        except ProviderError:
            failure = "provider terminal failure"
            terminal = runtime.emit_result(
                terminal_failure=failure, legacy_value=failure,
            )
            if runtime.return_result:
                return terminal
            raise

        if decision.get("type") == "memory_candidate":
            _handle_memory_candidate(
                decision, runtime.memory_store, runtime.messages,
                runtime.audit, runtime.checkpoint,
            )
            continue
        if decision.get("type") == "final_answer":
            phase = _handle_final_candidate(
                runtime, decision, request_id, decision_event,
            )
        elif (
            decision.get("type") == "tool_call"
            and str(decision.get("tool", "")).startswith("mcp:")
        ):
            phase = _handle_mcp_decision(runtime, decision, request_id)
        elif (
            decision.get("type") == "tool_call"
            and ENVIRONMENT_REGISTRY.is_environment_intent(decision.get("tool"))
        ):
            phase = _handle_environment_decision(runtime, decision, request_id)
        else:
            phase = _handle_shell_decision(
                runtime, decision, request_id, decision_event,
            )
        if phase.terminal:
            return phase.terminal_result
        if phase.continue_loop:
            continue

    failure = f"达到最大步数 {max_steps}，Agent 已停止，以防止无限循环。"
    terminal = runtime.emit_result(
        terminal_failure=failure, legacy_value=failure,
    )
    if runtime.return_result:
        return terminal
    raise RuntimeError(failure)


def _handle_environment_decision(runtime, decision, request_id):
    """Apply the normal Harness authority chain to fixed registry capabilities."""
    reference, arguments = decision.get("tool"), decision.get("arguments")
    runtime.last_observation_event_id = None
    runtime.audit("tool_requested", "model", reference or "termux", "requested")
    workflow_denial = _mobile_action_gate(runtime, reference)
    if workflow_denial is not None:
        runtime.audit(
            "policy_decision", "harness", reference or "termux", "DENY",
            workflow_denial, references={"gate": "mobile_step_dependency"},
        )
        return _RuntimePhaseResult(
            terminal=True,
            terminal_result=runtime.emit_result(
                legacy_value="incomplete: " + workflow_denial,
            ),
        )
    if runtime.current_plan is not None:
        runtime.plan_had_action = True
    try:
        arguments = ENVIRONMENT_REGISTRY.normalize_arguments(reference, arguments)
        valid = not decision.get("validation_failed")
    except ValueError:
        valid = False
    if not valid:
        try:
            rejected_spec = ENVIRONMENT_REGISTRY.spec(reference)
            rejected_effect = rejected_spec.effect
            rejected_code = "INVALID_ARGUMENT"
        except ValueError:
            rejected_effect = "read_only"
            rejected_code = "UNSUPPORTED_CAPABILITY"
        empty_sha = hashlib.sha256(b"").hexdigest()
        observation = {
            "logical_capability": reference or "unsupported",
            "effect": rejected_effect, "effect_certainty": "not_started",
            "safe_observation": {}, "status": "failed",
            "error_code": rejected_code,
            "exit_code": 126, "denied_by": "capability_validation",
            "stdout_length": 0, "stdout_sha256": empty_sha,
            "stderr_length": 0, "stderr_sha256": empty_sha,
        }
        policy = {"action": POLICY_DENY, "effect": rejected_effect,
                  "reason": "unknown or invalid Termux capability"}
        approved = False
    else:
        policy = classify_environment_capability(
            reference, runtime.policy_binding.snapshot,
            runtime.references.get("termux_delegated_ceiling"),
        )
        effect = policy["effect"]
        argument_refs = {}
        for name, value in sorted(arguments.items()):
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")).encode("utf-8")
            argument_refs[name + "_length"] = len(encoded)
            argument_refs[name + "_sha256"] = hashlib.sha256(encoded).hexdigest()
        runtime.audit(
            "policy_decision", "harness", reference, policy["action"],
            policy["reason"], references={
                "policy_fingerprint": runtime.policy_binding.fingerprint,
                "policy_trace": policy.get("trace", {}),
                "composition_inputs": policy.get("composition_inputs"),
                "effect": effect, "zone": "external", **argument_refs,
            },
        )
        if runtime.envelope_store is not None:
            runtime.envelope_store.append_transition(
                runtime.run_id, "policy", {
                    "policy_fingerprint": runtime.policy_binding.fingerprint,
                    "tool": reference, "action_effect": effect,
                    "composition_inputs": policy["composition_inputs"],
                }, {"decision": policy["action"]},
            )
        approved = policy["action"] == POLICY_ALLOW
        prepared = None
        if policy["action"] == POLICY_ASK:
            prepared = create_action_checkpoint(
                reference, arguments, effect,
                runtime.current_plan["plan_id"] if runtime.current_plan else None,
                runtime.current_plan["version"] if runtime.current_plan else None,
                runtime.current_step_id,
            )
            runtime.persist_action(prepared)
            if (
                runtime.mobile_workflow is not None
                and reference == NOTIFICATION_CAPABILITY
            ):
                trigger_fault(
                    runtime.fault_injector,
                    "after_condition_before_notification_approval",
                )
            approved = runtime.ask_approval(reference, policy["reason"])
        if approved:
            runtime.action_checkpoint = prepared or create_action_checkpoint(
                reference, arguments, effect,
                runtime.current_plan["plan_id"] if runtime.current_plan else None,
                runtime.current_plan["version"] if runtime.current_plan else None,
                runtime.current_step_id,
            )
            reason = runtime.consume_normal_action()
            if reason:
                return _RuntimePhaseResult(
                    terminal=True,
                    terminal_result=runtime.emit_result(
                        blocking_reason=reason,
                        legacy_value=runtime.deadline_block(reason),
                    ),
                )
            runtime.begin_attempt()
            dispatched = runtime.dispatch_environment(
                runtime.action_checkpoint, reference, arguments, policy, approved,
            )
            observation = dispatched.raw_observation
            runtime.action_checkpoint = dispatched.checkpoint
            runtime.recovered_action = dispatched.checkpoint
            retry_decision = runtime.finish_or_decide_retry(
                observation, effect, runtime.action_checkpoint["replay_policy"],
            )
            if (effect == "side_effecting"
                    and runtime.action_checkpoint["state"] == "unknown"):
                safe = _process_observation(
                    runtime.messages, decision, observation, reference, arguments,
                )
                runtime.audit(
                    "observation_recorded", "harness", reference, "unknown",
                    reason="environment reconciliation capability unavailable",
                    references={
                        "action_id": runtime.action_checkpoint["action_id"],
                        "capability": reference, "effect": effect,
                        "zone": "external", **argument_refs,
                    }, summary=safe,
                )
                runtime.checkpoint()
                output = None
                if runtime.mobile_workflow is not None:
                    output = _persist_mobile_output(runtime, "unknown")
                return _RuntimePhaseResult(
                    terminal=True,
                    terminal_result=runtime.emit_result(
                        blocking_reason="unknown environment effect; reconciliation unavailable",
                        legacy_value=(
                            mobile_output_answer(output) if output is not None
                            else "blocked: unknown environment effect"
                        ),
                    ),
                )
            while retry_decision == "retry_with_backoff":
                if runtime.governance_state is not None:
                    backoff = backoff_decision(
                        runtime.governance_state,
                        runtime.current_retry_state["backoff_delay"],
                        runtime.clock,
                    )
                    if not backoff["allowed"]:
                        return _RuntimePhaseResult(
                            terminal=True,
                            terminal_result=runtime.emit_result(
                                blocking_reason=backoff["reason"],
                                legacy_value=runtime.deadline_block(backoff["reason"]),
                            ),
                        )
                if not cooperative_backoff(
                    runtime.current_retry_state["backoff_delay"],
                    runtime.run_control, runtime.retry_sleeper,
                ):
                    break
                reason = runtime.consume_normal_action()
                if reason:
                    return _RuntimePhaseResult(
                        terminal=True,
                        terminal_result=runtime.emit_result(
                            blocking_reason=reason,
                            legacy_value=runtime.deadline_block(reason),
                        ),
                    )
                runtime.begin_attempt()
                runtime.action_checkpoint = create_action_checkpoint(
                    reference, arguments, effect,
                    runtime.current_plan["plan_id"] if runtime.current_plan else None,
                    runtime.current_plan["version"] if runtime.current_plan else None,
                    runtime.current_step_id,
                )
                dispatched = runtime.dispatch_environment(
                    runtime.action_checkpoint, reference, arguments, policy, True,
                )
                observation = dispatched.raw_observation
                runtime.action_checkpoint = dispatched.checkpoint
                runtime.recovered_action = dispatched.checkpoint
                retry_decision = runtime.finish_or_decide_retry(
                    observation, effect,
                    runtime.action_checkpoint["replay_policy"],
                )
        elif policy["action"] == POLICY_DENY:
            observation = {"status": "failed", "exit_code": 126,
                           "denied_by": "policy"}
        else:
            observation = {"status": "failed", "exit_code": 126,
                           "denied_by": "user"}
    safe = _process_observation(runtime.messages, decision, observation,
                                reference or "termux", arguments or {})
    runtime.audit("observation_recorded", "harness", reference or "termux",
                  "recorded", references={
                      "action_id": runtime.action_checkpoint["action_id"]
                      if runtime.action_checkpoint else None,
                      "capability": reference, "effect": policy["effect"],
                      "zone": "external",
                  }, summary=safe)
    runtime.checkpoint()
    if (
        runtime.mobile_workflow is not None
        and reference == NOTIFICATION_CAPABILITY
        and not approved
        and policy["action"] in {POLICY_ASK, POLICY_DENY}
    ):
        output = _persist_mobile_output(runtime, "approval_denied")
        return _RuntimePhaseResult(
            terminal=True,
            terminal_result=runtime.emit_result(
                legacy_value=mobile_output_answer(output),
            ),
        )
    if (approved and observation.get("exit_code") == 0
            and runtime.evidence_store is not None
            and runtime.last_observation_event_id is not None):
        evidence_references = {"step_id": runtime.current_step_id}
        if request_id:
            evidence_references["model_request_id"] = request_id
        evidence = create_environment_observation_evidence(
            runtime.run_id, reference, observation,
            runtime.last_observation_event_id,
            runtime.action_checkpoint["action_id"], argument_refs,
            references=evidence_references,
        )
        if effect == "side_effecting":
            runtime.audit(
                "verification_state_changed", "harness", reference, "accepted",
                reason="environment effect verified by adapter result contract",
                references={"action_id": runtime.action_checkpoint["action_id"]},
            )
        evidence_id = runtime.persist_evidence(
            evidence, runtime.current_step_id, True,
        )
        if evidence_id is not None:
            runtime.plan_evidence_ids.append(evidence_id)
        if runtime.mobile_workflow is not None and evidence_id is not None:
            if reference == BATTERY_CAPABILITY:
                trigger_fault(
                    runtime.fault_injector,
                    "after_battery_evidence_before_condition",
                )
            elif reference == NOTIFICATION_CAPABILITY:
                trigger_fault(
                    runtime.fault_injector,
                    "after_notification_evidence_before_result",
                )
            _advance_mobile_workflow(runtime)
    # read_only observation validity never creates a write verification duty.
    return _RuntimePhaseResult(continue_loop=True)


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
    termux_delegated_ceiling=None,
    mobile_workflow=None, mobile_workflow_output_store=None,
    resume_existing_run=False,
):
    """Orchestrate one Agent run while phase helpers own runtime details."""
    return _run_agent_runtime(
        task, provider, max_steps, messages, verification, save_checkpoint,
        memory_store, mcp_registry, context_assembler, context_budget,
        current_plan, plan_revision_history, require_plan_grounding,
        current_action_checkpoint, save_action_checkpoint, run_control,
        save_run_control, current_retry_state, save_retry_state,
        retry_sleeper, governance_state, save_governance_state, clock,
        step_timeout_seconds, audit_writer, session_id, audit_directory,
        policy_binding, previous_run_id, previous_policy_fingerprint,
        evidence_store, output_contract, artifact_store,
        output_contract_store, result_store, return_result, fault_injector,
        late_mcp_completion_journal, termux_delegated_ceiling,
        mobile_workflow, mobile_workflow_output_store, resume_existing_run,
    )
def _handle_mcp_decision(runtime, decision, request_id):
    """Run the MCP authority, dispatch, observation, and recovery chain."""
    # MCP metadata describes a capability but grants no authority. Schema,
    # Effect, local Policy, paths, Approval, and runtime gates are re-established.
    if not runtime.scheduling_allowed():
        runtime.settle_run_control()
        legacy = f"run {runtime.run_control['state']}"
        return _RuntimePhaseResult(terminal=True, terminal_result=runtime.emit_result(blocking_reason=runtime.run_control.get('reason') or legacy, legacy_value=legacy))
    reference = decision.get('tool')
    mcp_verification_was_required = runtime.requires_verification
    mcp_historical_target = runtime.verification_target
    if runtime.current_plan is not None:
        runtime.plan_had_action = True
    arguments = decision.get('arguments')
    effect = None
    prepared_for_approval = None
    mcp_crash_block = False
    runtime.rejected_final_answer = None
    print(f'[模型请求 MCP capability] {reference}')
    runtime.audit('tool_requested', 'model', reference, 'requested')
    try:
        if runtime.mcp_registry is None:
            raise ValueError('MCP registry 未配置')
        client, name, detail = runtime.mcp_registry.resolve(reference)
        validate_json_schema(arguments, detail.get('inputSchema', {'type': 'object'}))
    except ValueError as error:
        observation = {'result': None, 'error': str(error), 'exit_code': 1, 'denied_by': 'capability_validation'}
        policy = None
        approved = False
        print(f'[MCP Validation] DENY：{error}')
    else:
        policy = runtime.mcp_registry.policy_for(reference, runtime.policy_binding.snapshot)
        effect = runtime.mcp_registry.effect_for(reference, runtime.policy_binding.snapshot)
        path_decision = inspect_mcp_paths(arguments)
        if not path_decision.allowed:
            policy = {**policy, 'action': POLICY_DENY, 'reason': path_decision.reason}
        # Exact correlation prevents historical state from authorizing a merely
        # similar new request.
        correlation = build_action_correlation_facts(runtime.recovered_action, reference, arguments, run_state=runtime.run_control['state'], retry_state=runtime.current_retry_state, verification_state=runtime.verification)
        matches_recovered = correlation['matches_checkpoint']
        unsafe_unknown_replay = correlation['unsafe_unknown_side_effect']
        print(f"[Policy] {policy['action']}：{policy['reason']}")
        runtime.audit('policy_decision', 'harness', reference, policy['action'], policy['reason'], references={'policy_fingerprint': runtime.policy_binding.fingerprint, 'policy_trace': policy.get('trace', {}), 'composition_inputs': policy.get('composition_inputs')})
        if runtime.envelope_store is not None and policy.get('composition_inputs'):
            runtime.envelope_store.append_transition(runtime.run_id, 'policy', {'policy_fingerprint': runtime.policy_binding.fingerprint, 'tool': reference, 'action_effect': effect, 'composition_inputs': policy['composition_inputs']}, {'decision': policy['action']})
        print(f'[MCP Effect] {effect}')
        approved = policy['action'] == POLICY_ALLOW
        blocked_by_verification = False
        if runtime.verification.get('degraded') and effect != MCP_EFFECT_READ_ONLY:
            observation = {'result': None, 'error': 'degraded persistence blocks new side effects', 'exit_code': 126, 'denied_by': 'persistence_gate'}
            approved = False
            blocked_by_verification = True
        elif runtime.requires_verification and effect != MCP_EFFECT_READ_ONLY:
            observation = {'result': None, 'error': 'verification tool must be read-only', 'exit_code': 126, 'denied_by': 'verification_gate'}
            approved = False
            blocked_by_verification = True
        elif policy['action'] == POLICY_ASK and (not unsafe_unknown_replay) and (not (matches_recovered and runtime.recovered_action['state'] in {'succeeded', 'failed'})):
            prepared_for_approval = create_action_checkpoint(reference, arguments, effect, runtime.current_plan['plan_id'] if runtime.current_plan else None, runtime.current_plan['version'] if runtime.current_plan else None, runtime.current_step_id)
            runtime.persist_action(prepared_for_approval)
            approved = runtime.ask_approval(reference, policy['reason'])
            if not runtime.scheduling_allowed():
                runtime.checkpoint()
                legacy = f"run {runtime.run_control['state']}"
                return _RuntimePhaseResult(terminal=True, terminal_result=runtime.emit_result(blocking_reason=runtime.run_control.get('reason') or legacy, legacy_value=legacy))
        handled_recovery = False
        if matches_recovered and runtime.recovered_action['state'] == 'succeeded':
            observation = dict(runtime.recovered_action['observation'])
            approved = False
            handled_recovery = True
        elif matches_recovered and runtime.recovered_action['state'] == 'failed':
            observation = dict(runtime.recovered_action['observation'])
            approved = False
            handled_recovery = True
        # There is no general MCP read-back contract for an unknown side effect,
        # so it is blocked rather than silently retried.
        elif matches_recovered and runtime.recovered_action['state'] == 'unknown' and (runtime.recovered_action['replay_policy'] != 'safe_to_retry'):
            observation = {'result': None, 'error': 'uncertain side effect', 'exit_code': 126, 'denied_by': 'crash_recovery'}
            approved = False
            handled_recovery = True
            mcp_crash_block = True
        if approved:
            runtime.action_checkpoint = prepared_for_approval or create_action_checkpoint(reference, arguments, effect, runtime.current_plan['plan_id'] if runtime.current_plan else None, runtime.current_plan['version'] if runtime.current_plan else None, runtime.current_step_id)
            if not runtime.scheduling_allowed():
                runtime.settle_run_control()
                runtime.checkpoint()
                legacy = f"run {runtime.run_control['state']}"
                return _RuntimePhaseResult(terminal=True, terminal_result=runtime.emit_result(blocking_reason=runtime.run_control.get('reason') or legacy, legacy_value=legacy))
            budget_reason = runtime.consume_normal_action()
            if budget_reason:
                legacy = runtime.deadline_block(budget_reason)
                return _RuntimePhaseResult(terminal=True, terminal_result=runtime.emit_result(blocking_reason=budget_reason, legacy_value=legacy))
            runtime.begin_attempt()
            dispatched = runtime.dispatch_mcp(runtime.action_checkpoint, reference, arguments, policy, effect, approved)
            observation = dispatched.raw_observation
            runtime.action_checkpoint = dispatched.checkpoint
            runtime.recovered_action = runtime.action_checkpoint
            retry_decision = 'no_retry' if dispatched.degraded or runtime.verification.get('degraded') else runtime.finish_or_decide_retry(observation, effect, runtime.action_checkpoint['replay_policy'])
            while retry_decision == 'retry_with_backoff':
                if runtime.governance_state is not None:
                    backoff = backoff_decision(runtime.governance_state, runtime.current_retry_state['backoff_delay'], runtime.clock)
                    if not backoff['allowed']:
                        legacy = runtime.deadline_block(backoff['reason'])
                        return _RuntimePhaseResult(terminal=True, terminal_result=runtime.emit_result(blocking_reason=backoff['reason'], legacy_value=legacy))
                if not cooperative_backoff(runtime.current_retry_state['backoff_delay'], runtime.run_control, runtime.retry_sleeper):
                    runtime.settle_run_control()
                    runtime.checkpoint()
                    legacy = f"run {runtime.run_control['state']}"
                    return _RuntimePhaseResult(terminal=True, terminal_result=runtime.emit_result(blocking_reason=runtime.run_control.get('reason') or legacy, legacy_value=legacy))
                if policy['action'] == POLICY_ASK and (not runtime.ask_approval(reference, policy['reason'])):
                    rejected = {'result': None, 'error': 'tool execution was denied by user', 'exit_code': 126, 'denied_by': 'user'}
                    retry_decision = runtime.finish_or_decide_retry(rejected, effect, runtime.action_checkpoint['replay_policy'])
                    observation = rejected
                    break
                budget_reason = runtime.consume_normal_action()
                if budget_reason:
                    legacy = runtime.deadline_block(budget_reason)
                    return _RuntimePhaseResult(terminal=True, terminal_result=runtime.emit_result(blocking_reason=budget_reason, legacy_value=legacy))
                runtime.begin_attempt()
                runtime.action_checkpoint = create_action_checkpoint(reference, arguments, effect, runtime.current_plan['plan_id'] if runtime.current_plan else None, runtime.current_plan['version'] if runtime.current_plan else None, runtime.current_step_id)
                dispatched = runtime.dispatch_mcp(runtime.action_checkpoint, reference, arguments, policy, effect, True)
                observation = dispatched.raw_observation
                runtime.action_checkpoint = dispatched.checkpoint
                runtime.recovered_action = runtime.action_checkpoint
                retry_decision = 'no_retry' if dispatched.degraded or runtime.verification.get('degraded') else runtime.finish_or_decide_retry(observation, effect, runtime.action_checkpoint['replay_policy'])
            if not runtime.scheduling_allowed():
                runtime.settle_run_control()
            if observation['exit_code'] == 0:
                if runtime.requires_verification and effect == MCP_EFFECT_READ_ONLY:
                    runtime.requires_verification = False
                    runtime.verification_target = None
                elif effect in {MCP_EFFECT_SIDE_EFFECTING, MCP_EFFECT_UNKNOWN}:
                    runtime.requires_verification = True
                    runtime.verification_obligation = True
                    runtime.latest_write_command = reference
                    runtime.verification_target = None
                    runtime.pending_verification_action_id = runtime.action_checkpoint['action_id']
            print('[MCP Tool Execution] 调用完毕')
        elif policy['action'] == POLICY_DENY:
            observation = {'result': None, 'error': 'tool execution was denied by policy', 'exit_code': 126, 'denied_by': 'policy'}
        elif not blocked_by_verification and (not handled_recovery):
            observation = {'result': None, 'error': 'tool execution was denied by user', 'exit_code': 126, 'denied_by': 'user'}
    print(f"[Observation] exit_code={observation['exit_code']}")
    _process_observation(runtime.messages, decision, observation, reference, arguments)
    runtime.checkpoint()
    if runtime.evidence_store is not None and runtime.last_observation_event_id is not None and (runtime.action_checkpoint is not None):
        server = reference.split(':', 2)[1]
        mcp_record = create_mcp_observation_evidence(runtime.run_id, {'kind': 'plan_step', 'target': runtime.current_step_id, 'claim': 'external_observation_recorded'} if runtime.current_step_id else {'kind': 'mcp_call', 'target': runtime.action_checkpoint['action_id'], 'claim': 'external_observation_recorded'}, server, reference, observation, runtime.last_observation_event_id, action_id=runtime.action_checkpoint['action_id'], references={'model_request_id': request_id} if request_id else {})
        runtime.persist_evidence(mcp_record, runtime.current_step_id)
        if mcp_verification_was_required:
            accepted = bool(effect == MCP_EFFECT_READ_ONLY and observation.get('exit_code') == 0)
            reason = None if accepted else 'MCP verification observation failed'
            verification_record = create_verification_evidence(runtime.run_id, {'kind': 'plan_step', 'target': runtime.current_step_id, 'claim': 'external_state_verified'} if runtime.current_step_id else {'kind': 'mcp_call', 'target': reference, 'claim': 'external_state_verified'}, mcp_historical_target, runtime.action_checkpoint['action_id'], observation, runtime.last_observation_event_id, accepted, reason, runtime.pending_verification_action_id, references={'candidate_evidence_id': mcp_record['evidence_id']})
            evidence_id = runtime.persist_evidence(verification_record, runtime.current_step_id, accepted)
            if accepted and runtime.current_plan is not None:
                runtime.plan_evidence_ids.append(evidence_id)
    if runtime.current_plan is not None and observation['exit_code'] == 0 and (effect == MCP_EFFECT_READ_ONLY):
        if runtime.evidence_store is None:
            runtime.plan_evidence.append({'kind': 'tool_observation', 'message_index': len(runtime.messages) - 1, 'summary': f'{reference} read-only observation succeeded', 'verified': True})
        runtime.plan_runtime_state['requires_fresh_grounding'] = False
    runtime.checkpoint()
    replan_result = runtime.stop_for_replan_if_needed(retry_decision if approved and (not mcp_crash_block) else None)
    if replan_result is not None:
        return _RuntimePhaseResult(terminal=True, terminal_result=runtime.emit_result(blocking_reason=replan_result.split(': ', 1)[-1], legacy_value=replan_result))
    if mcp_crash_block:
        if runtime.current_plan is not None and runtime.current_plan['status'] == 'active':
            blocked = block_step(runtime.current_plan, runtime.current_step_id)
            blocked_step = next((item for item in blocked['steps'] if item['id'] == runtime.current_step_id))
            blocked_step['evidence'].append({'kind': 'recovery_block', 'summary': 'uncertain side effect'})
            runtime.current_plan.clear()
            runtime.current_plan.update(blocked)
            runtime.checkpoint()
        return _RuntimePhaseResult(terminal=True, terminal_result=runtime.emit_result(blocking_reason='uncertain side effect', legacy_value='blocked: uncertain side effect'))
    return _RuntimePhaseResult(continue_loop=True)
def _handle_shell_decision(runtime, decision, request_id, decision_event):
    """Run the Shell authority, dispatch, observation, completion, and recovery chain."""
    if decision.get('type') != 'tool_call' or not decision.get('command'):
        failure = '模型返回了无效决定'
        terminal = runtime.emit_result(terminal_failure=failure, legacy_value=failure)
        if runtime.return_result:
            return _RuntimePhaseResult(terminal=True, terminal_result=terminal)
        raise ValueError(f'{failure}：{decision!r}')
    command = decision['command']
    verification_was_required = runtime.requires_verification
    historical_verification_target = runtime.verification_target
    evidence_related = bool(runtime.verification_target is None or is_related_verification(command, runtime.verification_target))
    runtime.last_observation_event_id = None
    if not runtime.scheduling_allowed() and (not runtime.safety_entry):
        runtime.settle_run_control()
        reason = deadline_status(runtime.governance_state, runtime.clock) if runtime.governance_state is not None else None
        legacy = runtime.deadline_block(reason) if reason else f"run {runtime.run_control['state']}"
        return _RuntimePhaseResult(terminal=True, terminal_result=runtime.emit_result(blocking_reason=reason or runtime.run_control.get('reason') or legacy, legacy_value=legacy))
    if runtime.current_plan is not None:
        runtime.plan_had_action = True
    runtime.rejected_final_answer = None
    print(f'[模型请求执行的命令] {command}')
    runtime.audit('tool_requested', 'model', 'shell', 'requested')
    # Classification and Effect describe the request; neither executes it.
    # Runtime gates and fresh Approval remain separate prerequisites.
    policy = classify_shell(command, runtime.policy_binding.snapshot)
    print(f"[Policy] {policy['action']}：{policy['reason']}")
    runtime.audit('policy_decision', 'harness', 'shell', policy['action'], policy['reason'], references={'policy_fingerprint': runtime.policy_binding.fingerprint, 'policy_trace': policy.get('trace', {}), 'composition_inputs': policy.get('composition_inputs')})
    if runtime.envelope_store is not None and policy.get('composition_inputs'):
        runtime.envelope_store.append_transition(runtime.run_id, 'policy', {'policy_fingerprint': runtime.policy_binding.fingerprint, 'tool': 'shell', 'action_effect': policy.get('effect'), 'composition_inputs': policy['composition_inputs']}, {'decision': policy['action']})
    arguments = {'command': command}
    # Correlation is exact by capability and arguments. Old approval or
    # observation data cannot authorize a different or new attempt.
    correlation = build_action_correlation_facts(runtime.recovered_action, 'shell', arguments, run_state=runtime.run_control['state'], retry_state=runtime.current_retry_state, verification_state=runtime.verification)
    matches_recovered = correlation['matches_checkpoint']
    unsafe_unknown_replay = correlation['unsafe_unknown_side_effect']
    recovered_not_applied = correlation['reconciled_not_applied']
    is_reconciliation_attempt = bool(runtime.recovered_action and runtime.recovered_action['state'] == 'unknown' and (runtime.recovered_action['replay_policy'] != 'safe_to_retry') and (policy['effect'] == 'read_only') and (policy['action'] != POLICY_DENY))
    approved = policy['action'] == POLICY_ALLOW
    prepared_for_approval = None
    blocked_by_persistence = bool(runtime.verification.get('degraded') and policy['effect'] != 'read_only')
    if blocked_by_persistence:
        approved = False
        observation = {'status': 'denied', 'denied_by': 'persistence_gate', 'stdout': '', 'stderr': 'degraded persistence blocks new side effects', 'exit_code': 126}
    elif runtime.requires_verification and policy['action'] != POLICY_DENY and (policy['effect'] != 'read_only'):
        approved = False
        observation = {'status': 'denied', 'denied_by': 'verification_gate', 'stdout': '', 'stderr': 'verification tool must be read-only', 'exit_code': 126}
        print('[Verification Gate] 验证工具必须是只读命令')
    elif runtime.requires_verification and policy['action'] != POLICY_DENY and (policy['effect'] == 'read_only') and (runtime.verification_target is not None) and (not is_related_verification(command, runtime.verification_target)):
        approved = False
        observation = {'status': 'denied', 'denied_by': 'verification_quality', 'stdout': '', 'stderr': 'verification evidence is not related to the modified target', 'exit_code': 126, 'verification_target': runtime.verification_target}
        print('[Verification Quality] 验证证据与修改目标无关')
    elif policy['action'] == POLICY_ASK and (not unsafe_unknown_replay) and (not (matches_recovered and runtime.recovered_action['state'] in {'succeeded', 'failed'} and (not recovered_not_applied))):
        prepared_for_approval = create_action_checkpoint('shell', arguments, policy['effect'], runtime.current_plan['plan_id'] if runtime.current_plan else None, runtime.current_plan['version'] if runtime.current_plan else None, runtime.current_step_id)
        runtime.persist_action(prepared_for_approval)
        approved = runtime.ask_approval(command, policy['reason'], safe_shell_command_identity(command))
        if not approved and recovered_not_applied:
            runtime.persist_action(runtime.recovered_action)
            if runtime.current_retry_state is not None:
                runtime.persist_retry(record_failure(runtime.current_retry_state, 'user_rejected', 'approval_rejected', 'no_retry'))
        if not runtime.scheduling_allowed():
            runtime.checkpoint()
            legacy = f"run {runtime.run_control['state']}"
            return _RuntimePhaseResult(terminal=True, terminal_result=runtime.emit_result(blocking_reason=runtime.run_control.get('reason') or legacy, legacy_value=legacy))
    handled_recovery = False
    crash_block_reason = None
    if blocked_by_persistence:
        handled_recovery = True
    if matches_recovered and runtime.recovered_action['state'] in {'succeeded', 'failed'} and (not recovered_not_applied):
        observation = dict(runtime.recovered_action['observation'])
        observation.setdefault('stdout', '')
        observation.setdefault('stderr', '')
        approved = False
        handled_recovery = True
    # Unknown side effects cross the durability boundary: normal retry is
    # forbidden until targeted read-only reconciliation establishes what happened.
    elif matches_recovered and runtime.recovered_action['state'] == 'unknown' and (runtime.recovered_action['replay_policy'] != 'safe_to_retry'):
        observation = {'status': 'blocked', 'denied_by': 'crash_recovery', 'stdout': '', 'stderr': 'uncertain side effect', 'exit_code': 126}
        approved = False
        handled_recovery = True
        crash_block_reason = 'uncertain side effect'
    if approved:
        runtime.action_checkpoint = prepared_for_approval or create_action_checkpoint('shell', arguments, policy['effect'], runtime.current_plan['plan_id'] if runtime.current_plan else None, runtime.current_plan['version'] if runtime.current_plan else None, runtime.current_step_id)
        if not runtime.scheduling_allowed() and (not is_reconciliation_attempt):
            runtime.settle_run_control()
            runtime.checkpoint()
            reason = deadline_status(runtime.governance_state, runtime.clock) if runtime.governance_state is not None else None
            legacy = runtime.deadline_block(reason) if reason else f"run {runtime.run_control['state']}"
            return _RuntimePhaseResult(terminal=True, terminal_result=runtime.emit_result(blocking_reason=reason or runtime.run_control.get('reason') or legacy, legacy_value=legacy))
        # Safety reconciliation has a separate bounded governance allowance; it
        # does not consume or reopen the normal action/retry path.
        if not is_reconciliation_attempt:
            budget_reason = runtime.consume_normal_action()
            if budget_reason:
                legacy = runtime.deadline_block(budget_reason)
                return _RuntimePhaseResult(terminal=True, terminal_result=runtime.emit_result(blocking_reason=budget_reason, legacy_value=legacy))
            runtime.begin_attempt()
        elif runtime.governance_state is not None:
            expected = expected_file_write(runtime.recovered_action)
            related = bool(expected and command in {f"cat {expected['path']}", f"ls {expected['path']}"})
            safety = safety_reconciliation_decision(runtime.governance_state, runtime.recovered_action, 'read_only', related, policy['action'] != POLICY_DENY)
            if not safety['allowed']:
                legacy = f"blocked: {safety['reason']}"
                return _RuntimePhaseResult(terminal=True, terminal_result=runtime.emit_result(blocking_reason=safety['reason'], legacy_value=legacy))
            runtime.persist_governance(
                consume_safety_reconciliation(runtime.governance_state)
            )
        dispatched = runtime.dispatch_shell(runtime.action_checkpoint, command, policy, approved)
        observation = dispatched.raw_observation
        runtime.action_checkpoint = dispatched.checkpoint
        retry_decision = None
        if not is_reconciliation_attempt:
            retry_decision = 'no_retry' if dispatched.degraded or runtime.verification.get('degraded') else runtime.finish_or_decide_retry(observation, runtime.action_checkpoint['effect'], runtime.action_checkpoint['replay_policy'])
        # This loop is reachable only after a definite failed attempt. Durability
        # has already excluded uncertain side effects from automatic retry.
        while retry_decision == 'retry_with_backoff':
            if runtime.governance_state is not None:
                backoff = backoff_decision(runtime.governance_state, runtime.current_retry_state['backoff_delay'], runtime.clock)
                if not backoff['allowed']:
                    legacy = runtime.deadline_block(backoff['reason'])
                    return _RuntimePhaseResult(terminal=True, terminal_result=runtime.emit_result(blocking_reason=backoff['reason'], legacy_value=legacy))
            if not cooperative_backoff(runtime.current_retry_state['backoff_delay'], runtime.run_control, runtime.retry_sleeper):
                runtime.settle_run_control()
                runtime.checkpoint()
                legacy = f"run {runtime.run_control['state']}"
                return _RuntimePhaseResult(terminal=True, terminal_result=runtime.emit_result(blocking_reason=runtime.run_control.get('reason') or legacy, legacy_value=legacy))
            if policy['action'] == POLICY_ASK and (not runtime.ask_approval(command, policy['reason'], safe_shell_command_identity(command))):
                observation = {'status': 'denied', 'denied_by': 'user', 'stdout': '', 'stderr': 'tool execution was denied by user', 'exit_code': 126}
                retry_decision = runtime.finish_or_decide_retry(observation, runtime.action_checkpoint['effect'], runtime.action_checkpoint['replay_policy'])
                break
            budget_reason = runtime.consume_normal_action()
            if budget_reason:
                legacy = runtime.deadline_block(budget_reason)
                return _RuntimePhaseResult(terminal=True, terminal_result=runtime.emit_result(blocking_reason=budget_reason, legacy_value=legacy))
            runtime.begin_attempt()
            runtime.action_checkpoint = create_action_checkpoint('shell', arguments, policy['effect'], runtime.current_plan['plan_id'] if runtime.current_plan else None, runtime.current_plan['version'] if runtime.current_plan else None, runtime.current_step_id)
            dispatched = runtime.dispatch_shell(runtime.action_checkpoint, command, policy, True)
            observation = dispatched.raw_observation
            runtime.action_checkpoint = dispatched.checkpoint
            retry_decision = 'no_retry' if dispatched.degraded or runtime.verification.get('degraded') else runtime.finish_or_decide_retry(observation, runtime.action_checkpoint['effect'], runtime.action_checkpoint['replay_policy'])
        print('[Tool Execution] 命令执行完毕')
        if observation['exit_code'] == 0:
            if runtime.requires_verification and policy['effect'] == 'read_only':
                runtime.requires_verification = False
                runtime.verification_target = None
                runtime.audit('verification_state_changed', 'harness', 'tool', 'succeeded')
                print('[Verification Gate] 只读验证成功，已解除门禁')
            elif policy['effect'] != 'read_only':
                runtime.requires_verification = True
                runtime.verification_obligation = True
                runtime.latest_write_command = command
                runtime.verification_target = extract_verification_target(command)
                runtime.pending_verification_action_id = runtime.action_checkpoint['action_id']
                runtime.audit('verification_state_changed', 'harness', 'tool', 'required', 'side-effecting action requires verification', references={'action_id': runtime.action_checkpoint['action_id']})
                if runtime.verification_target is None:
                    print('[Verification Quality] 无法可靠识别目标，显式降级为 V3 验证行为')
                elif runtime.output_contract is not None and runtime.verification_target.get('target_type') == 'file':
                    try:
                        runtime.pending_artifact = {'path': runtime.verification_target['path'], 'content_identity': observe_workspace_file(runtime.verification_target['path']), 'producer': create_producer(runtime.run_id, action_id=runtime.action_checkpoint['action_id'], capability='shell', step_id=runtime.current_step_id, model_request_id=request_id, model_decision_event_id=decision_event.get('event_id') if decision_event else None, tool='shell')}
                    except (ArtifactError, OSError) as error:
                        runtime.pending_artifact = None
                        runtime.audit('artifact_rejected', 'harness', 'artifact', 'rejected', str(error))
                print('[Verification Gate] 写操作成功，需要只读验证')
    elif not handled_recovery and (not (runtime.requires_verification and policy['action'] != POLICY_DENY and (policy['effect'] != 'read_only' or (policy['effect'] == 'read_only' and runtime.verification_target is not None and (not is_related_verification(command, runtime.verification_target)))))):
        denied_by = 'policy' if policy['action'] == POLICY_DENY else 'user'
        observation = {'status': 'denied', 'denied_by': denied_by, 'stdout': '', 'stderr': f'tool execution was denied by {denied_by}', 'exit_code': 126}
        print(f'[Tool Execution] 未执行：denied_by={denied_by}')
    print(f"[Observation] exit_code={observation['exit_code']}")
    print(f'[Observation] safe={safe_observation_summary(observation)!r}')
    _process_observation(runtime.messages, decision, observation, 'shell', {'command': command})
    runtime.checkpoint()
    verification_record = None
    verification_evidence_id = None
    if verification_was_required:
        verification_inputs = {'requires_verification': True, 'verification_target': historical_verification_target, 'action_effect': policy['effect'], 'evidence_related': evidence_related, 'historical_recorded_observation': True, 'observation': verification_observation_identity(observation, runtime.last_observation_event_id)}
        verification_output = replay_verification_transition(verification_inputs)
        if runtime.evidence_store is not None and runtime.last_observation_event_id is not None:
            subject = {'kind': 'plan_step', 'target': runtime.current_step_id, 'claim': 'current_reality_verified'} if runtime.current_step_id else {'kind': 'workspace_file', 'target': (historical_verification_target or {}).get('path', 'unknown'), 'claim': 'content_verified'}
            verification_record = create_verification_evidence(runtime.run_id, subject, historical_verification_target, runtime.action_checkpoint['action_id'], observation, runtime.last_observation_event_id, verification_output['accepted'], verification_output['reason'], runtime.pending_verification_action_id, artifact=artifact_ref(historical_verification_target['path'], hashlib.sha256(observation.get('stdout', '').encode()).hexdigest(), len(observation.get('stdout', '').encode())) if verification_output['accepted'] and isinstance(historical_verification_target, dict) and (historical_verification_target.get('target_type') == 'file') else None)
            evidence_id = runtime.persist_evidence(verification_record, runtime.current_step_id, verification_output['accepted'])
            verification_evidence_id = evidence_id
            if verification_output['accepted'] and runtime.current_plan is not None:
                runtime.plan_evidence_ids.append(evidence_id)
            verification_inputs.update({'evidence_id': evidence_id, 'evidence_fingerprint': verification_record['evidence_fingerprint']})
        if runtime.envelope_store is not None:
            runtime.envelope_store.append_transition(runtime.run_id, 'verification', verification_inputs, verification_output)
        if runtime.pending_artifact is not None and verification_record is not None:
            runtime.finalize_artifact_candidate(verification_record, verification_evidence_id, verification_output['accepted'])
    if runtime.evidence_store is not None and runtime.last_observation_event_id is not None and (runtime.action_checkpoint is not None):
        source = {'action_id': runtime.action_checkpoint['action_id'], 'logical_action_id': (runtime.current_retry_state or {}).get('logical_action_id', runtime.action_checkpoint['action_id']), 'attempt': (runtime.current_retry_state or {}).get('attempt_count', 1), 'tool': 'shell'}
        subject = {'kind': 'plan_step', 'target': runtime.current_step_id, 'claim': 'observation_recorded'} if runtime.current_step_id else {'kind': 'tool_action', 'target': runtime.action_checkpoint['action_id'], 'claim': 'observation_recorded'}
        tool_record = create_tool_observation_evidence(runtime.run_id, subject, source, observation, runtime.last_observation_event_id, accepted=observation.get('exit_code') == 0 and policy['effect'] == 'read_only', read_only=policy['effect'] == 'read_only', references={'model_request_id': request_id} if request_id else {})
        tool_evidence_id = runtime.persist_evidence(tool_record, runtime.current_step_id)
        if runtime.current_plan is not None and observation.get('exit_code') == 0 and (policy['effect'] == 'read_only'):
            runtime.plan_evidence_ids.append(tool_evidence_id)
    if runtime.recovered_action is not None and runtime.recovered_action['state'] == 'unknown' and (runtime.recovered_action['replay_policy'] != 'safe_to_retry') and (policy['effect'] == 'read_only') and (policy['action'] != POLICY_DENY):
        reconciliation_source_action_id = runtime.recovered_action['action_id']
        reconciliation_target = expected_file_write(runtime.recovered_action) or {'tool': runtime.recovered_action['tool']}
        runtime.audit('reconciliation_state_changed', 'harness', 'action', 'started', references={'action_id': runtime.recovered_action['action_id']})
        reconciliation = reconcile_file_observation(runtime.recovered_action, command, observation)
        if reconciliation['status'] == 'succeeded':
            runtime.recovered_action = reconciliation['checkpoint']
            runtime.persist_action(runtime.recovered_action)
            runtime.plan_evidence.append(reconciliation['evidence'])
            runtime.plan_runtime_state['requires_fresh_grounding'] = False
            runtime.plan_runtime_state.pop('action_recovery', None)
            if runtime.current_retry_state is not None:
                runtime.persist_retry(complete_retry(runtime.current_retry_state))
        elif reconciliation['status'] == 'not_applied':
            runtime.recovered_action = reconciliation['checkpoint']
            runtime.persist_action(runtime.recovered_action)
            runtime.plan_evidence.append(reconciliation['evidence'])
            runtime.plan_runtime_state['requires_fresh_grounding'] = False
            runtime.plan_runtime_state.pop('action_recovery', None)
            if runtime.current_retry_state is not None:
                runtime.persist_retry(reopen_retry_after_reconciliation(runtime.current_retry_state, runtime.recovered_action['replay_policy'], runtime.run_control['state']))
        else:
            crash_block_reason = reconciliation['reason']
        if runtime.evidence_store is not None and runtime.last_observation_event_id is not None:
            result = {'succeeded': 'applied', 'not_applied': 'not_applied'}.get(reconciliation['status'], 'uncertain')
            reconciliation_record = create_reconciliation_evidence(runtime.run_id, {'kind': 'plan_step', 'target': runtime.current_step_id, 'claim': 'reconciliation_completed'} if runtime.current_step_id else {'kind': 'tool_action', 'target': reconciliation_source_action_id, 'claim': 'reconciliation_completed'}, reconciliation_source_action_id, reconciliation_target, result, observation, runtime.last_observation_event_id)
            reconciliation_id = runtime.persist_evidence(reconciliation_record, runtime.current_step_id, result in {'applied', 'not_applied'})
            if result in {'applied', 'not_applied'} and runtime.current_plan is not None:
                runtime.plan_evidence_ids.append(reconciliation_id)
        runtime.audit('reconciliation_state_changed', 'harness', 'action', 'completed' if reconciliation['status'] in {'succeeded', 'not_applied'} else 'blocked', reconciliation.get('reason'), references={'action_id': runtime.recovered_action['action_id']})
        if runtime.governance_state is not None and (deadline_status(runtime.governance_state, runtime.clock) or not can_schedule_action(runtime.run_control)):
            runtime.checkpoint()
            reason = deadline_status(runtime.governance_state, runtime.clock)
            reason = reason or 'run control prevents normal scheduling'
            legacy = f'blocked: {reason}'
            return _RuntimePhaseResult(terminal=True, terminal_result=runtime.emit_result(blocking_reason=reason, legacy_value=legacy))
    if runtime.current_plan is not None and observation['exit_code'] == 0 and (policy['effect'] == 'read_only'):
        if runtime.evidence_store is None:
            runtime.plan_evidence.append({'kind': 'tool_observation', 'message_index': len(runtime.messages) - 1, 'summary': f'{command} read-only observation succeeded', 'verified': True})
        runtime.plan_runtime_state['requires_fresh_grounding'] = False
    runtime.checkpoint()
    replan_result = runtime.stop_for_replan_if_needed(retry_decision if approved and crash_block_reason is None else None)
    if replan_result is not None:
        return _RuntimePhaseResult(terminal=True, terminal_result=runtime.emit_result(blocking_reason=replan_result.split(': ', 1)[-1], legacy_value=replan_result))
    if crash_block_reason is not None:
        if runtime.current_plan is not None and runtime.current_plan['status'] == 'active':
            blocked = block_step(runtime.current_plan, runtime.current_step_id)
            blocked_step = next((item for item in blocked['steps'] if item['id'] == runtime.current_step_id))
            blocked_step['evidence'].append({'kind': 'recovery_block', 'summary': crash_block_reason})
            runtime.current_plan.clear()
            runtime.current_plan.update(blocked)
            runtime.checkpoint()
        legacy = f'blocked: {crash_block_reason}'
        return _RuntimePhaseResult(terminal=True, terminal_result=runtime.emit_result(blocking_reason=crash_block_reason, legacy_value=legacy))
    return _RuntimePhaseResult(continue_loop=True)
