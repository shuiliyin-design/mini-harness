"""Minimal sealed authorization and dispatch boundary for executable actions."""

from dataclasses import dataclass
import copy
import json

from .durability import transition_action_checkpoint, validate_action_checkpoint
from .fault_injection import trigger_fault
from .protected_paths import (
    ProtectedPathDecision, inspect_mcp_paths, inspect_shell_paths,
    inspect_subagent_paths,
)
from .environment.contracts import EnvironmentInvocation, _INVOCATION_SEAL
from .environment.registry import ENVIRONMENT_REGISTRY


_AUTHORIZATION_SEAL = object()


@dataclass(frozen=True, slots=True)
class AuthorizedAction:
    action_id: str
    capability: str
    normalized_arguments_json: str
    effect: str
    policy_decision: str
    approval_status: str
    run_id: str
    checkpoint_id: str
    _seal: object

    @property
    def normalized_arguments(self):
        return json.loads(self.normalized_arguments_json)


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    raw_observation: dict
    checkpoint: dict
    degraded: bool = False
    degraded_reason: str | None = None
    degraded_stage: str | None = None


def environment_checkpoint_outcome(effect, adapter_result):
    """Map Environment certainty to one Harness checkpoint outcome.

    This is a Harness durability interpretation, not Adapter policy.  Exit
    codes remain observation facts but cannot downgrade an ambiguous external
    effect to an ordinary failure.
    """
    certainty = adapter_result.get("effect_certainty")
    status = adapter_result.get("status")
    if effect == "read_only":
        return "succeeded" if (
            certainty == "no_side_effect" and status == "succeeded"
            and adapter_result.get("exit_code") == 0
        ) else "failed"
    if certainty == "known_applied":
        return "succeeded"
    if certainty == "unknown":
        return "unknown"
    if certainty == "not_started":
        return "failed"
    # A side-effecting adapter returning read-only certainty violates the
    # contract boundary.  Preserve uncertainty rather than guessing failure.
    return "unknown"


def _path_decision(capability, arguments, workspace_root):
    if ENVIRONMENT_REGISTRY.is_environment_intent(capability):
        try:
            ENVIRONMENT_REGISTRY.normalize_arguments(capability, arguments)
        except ValueError as error:
            return ProtectedPathDecision(False, str(error))
        return ProtectedPathDecision(
            True, "fixed Environment capability arguments passed registry schema",
        )
    if capability == "shell":
        return inspect_shell_paths(arguments.get("command"), workspace_root)
    if capability.startswith("mcp:"):
        return inspect_mcp_paths(arguments, workspace_root)
    if capability == "subagent":
        return inspect_subagent_paths(arguments.get("handoff"), workspace_root)
    return ProtectedPathDecision(False, "unknown executable capability")


def environment_invocation_from_authorized(action):
    """Create the contract invocation only from this module's sealed action."""
    if not isinstance(action, AuthorizedAction) or action._seal is not _AUTHORIZATION_SEAL:
        raise PermissionError("EnvironmentInvocation requires AuthorizedAction")
    spec = ENVIRONMENT_REGISTRY.spec(action.capability)
    if spec.effect != action.effect:
        raise PermissionError("Environment capability effect drift")
    normalized = ENVIRONMENT_REGISTRY.normalize_arguments(
        action.capability, action.normalized_arguments,
    )
    return EnvironmentInvocation(
        action.capability, normalized, action.action_id, action.run_id,
        _seal=_INVOCATION_SEAL,
    )


def authorize_action(*, checkpoint, capability, arguments, effect,
                     policy_decision, approval_granted, run_id,
                     runtime_allowed=True, workspace_root=None):
    """Seal the final authorization decision; no execution happens here."""
    # This is the final Authority boundary, not a convenience wrapper around
    # Policy. A seal is minted only when durable intent, current runtime gates,
    # static Policy, fresh Approval, and protected-path checks all agree.
    validate_action_checkpoint(checkpoint)
    if checkpoint["state"] != "prepared":
        raise ValueError("AuthorizedAction requires a prepared checkpoint")
    if checkpoint["tool"] != capability or checkpoint["arguments"] != arguments:
        raise ValueError("AuthorizedAction does not match checkpoint")
    if checkpoint["effect"] != effect:
        raise ValueError("AuthorizedAction effect does not match checkpoint")
    if not runtime_allowed:
        raise PermissionError("runtime gates rejected action")
    if policy_decision not in {"ALLOW", "ASK"}:
        raise PermissionError("static policy did not authorize action")
    if policy_decision == "ASK" and approval_granted is not True:
        raise PermissionError("fresh approval is required")
    path_decision = _path_decision(capability, arguments, workspace_root)
    if not path_decision.allowed:
        raise PermissionError(path_decision.reason)
    normalized = json.dumps(arguments, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False)
    return AuthorizedAction(
        checkpoint["action_id"], capability, normalized, effect,
        policy_decision, "granted" if policy_decision == "ASK" else "not_required",
        run_id, checkpoint["action_id"], _AUTHORIZATION_SEAL,
    )


def dispatch_authorized_action(action, checkpoint, *, persist_checkpoint,
                               executor, before_dispatch=None,
                               after_dispatch=None, fault_injector=None,
                               persist_session=None, outcome_classifier=None):
    """Persist executing before calling exactly one already-selected adapter."""
    if not isinstance(action, AuthorizedAction) or action._seal is not _AUTHORIZATION_SEAL:
        raise PermissionError("dispatch requires a sealed AuthorizedAction")
    validate_action_checkpoint(checkpoint)
    if (checkpoint["state"] != "prepared"
            or checkpoint["action_id"] != action.action_id
            or checkpoint["tool"] != action.capability
            or checkpoint["arguments"] != action.normalized_arguments):
        raise PermissionError("AuthorizedAction/checkpoint binding mismatch")
    if not callable(persist_checkpoint):
        raise ValueError("dispatch requires checkpoint persistence")

    # Persisting ``prepared`` and then ``executing`` is a precondition for the
    # side effect. If either write fails, the executor must not start; otherwise
    # a crash could leave no durable fact that an external effect was possible.
    persist_checkpoint(copy.deepcopy(checkpoint))
    executing = transition_action_checkpoint(checkpoint, "executing")
    persist_checkpoint(copy.deepcopy(executing))
    if before_dispatch is not None:
        before_dispatch(action)

    # Exactly one adapter call occurs beyond this line. Later Audit/Session
    # failures may degrade the run, but must never justify replaying this call.
    raw = executor(action.normalized_arguments)
    if not isinstance(raw, dict):
        raw = {"status": "failed", "error": "executor returned invalid observation",
               "exit_code": -1}
    terminal_state = (
        outcome_classifier(action.effect, raw)
        if outcome_classifier is not None else
        (
            "unknown"
            if raw.get("exit_code") == -1 and action.effect != "read_only"
            else "succeeded" if raw.get("exit_code") == 0 else "failed"
        )
    )
    if terminal_state not in {"succeeded", "failed", "unknown"}:
        raise ValueError("dispatch outcome classifier returned invalid state")
    uncertain = terminal_state == "unknown"
    if terminal_state == "succeeded":
        trigger_fault(
            fault_injector,
            "after_tool_success_before_terminal_checkpoint",
        )
    terminal = transition_action_checkpoint(
        executing, terminal_state, None if uncertain else raw
    )
    # Once the Tool has returned, forward truth wins over store availability.
    # Losing the terminal checkpoint becomes ``unknown``; it is not rewritten as
    # an ordinary Tool failure because the external outcome already happened.
    try:
        persist_checkpoint(copy.deepcopy(terminal))
    except Exception as error:
        unknown = transition_action_checkpoint(executing, "unknown")
        try:
            persist_checkpoint(copy.deepcopy(unknown))
        except Exception:
            pass
        return DispatchOutcome(
            copy.deepcopy(raw), unknown, True,
            f"session_persist_after_tool: {type(error).__name__}",
            "session",
        )
    trigger_fault(fault_injector, "after_terminal_checkpoint_before_audit")
    try:
        if after_dispatch is not None:
            after_dispatch(action, raw, terminal)
    except Exception as error:
        return DispatchOutcome(
            copy.deepcopy(raw), terminal, True,
            f"audit_append_after_tool: {type(error).__name__}",
            "audit",
        )
    trigger_fault(fault_injector, "after_audit_before_session")
    if persist_session is not None:
        try:
            persist_session(copy.deepcopy(terminal))
        except Exception as error:
            return DispatchOutcome(
                copy.deepcopy(raw), terminal, True,
                f"session_persist_after_audit: {type(error).__name__}",
                "session",
            )
    return DispatchOutcome(copy.deepcopy(raw), terminal)
