"""Teaching-grade Harness-owned deadline and execution-budget decisions."""

import copy
import time
from datetime import datetime, timedelta, timezone


DEFAULT_TOOL_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_ACTIONS = 20
DEFAULT_MAX_SUBAGENT_CALLS = 3
DEFAULT_MAX_SAFETY_RECONCILIATION_ACTIONS = 1
GOVERNANCE_FIELDS = frozenset({
    "run_started_at", "run_deadline_at", "step_started_at",
    "step_deadline_at", "tool_timeout_seconds", "actions_used",
    "max_actions", "subagent_calls_used", "max_subagent_calls", "frozen",
    "freeze_reason", "frozen_run_remaining_seconds",
    "frozen_step_remaining_seconds", "safety_reconciliation_actions_used",
    "max_safety_reconciliation_actions",
})


class Clock:
    """Production clock: UTC persists; monotonic measures this process only."""

    def utc_now(self):
        return datetime.now(timezone.utc)

    def monotonic(self):
        return time.monotonic()


class FakeClock(Clock):
    def __init__(self, utc="2026-01-01T00:00:00Z", monotonic=0.0):
        self._utc = parse_utc(utc)
        self._monotonic = float(monotonic)

    def utc_now(self):
        return self._utc

    def monotonic(self):
        return self._monotonic

    def advance(self, seconds):
        if not isinstance(seconds, (int, float)) or seconds < 0:
            raise ValueError("FakeClock advance 必须是非负数")
        self._utc += timedelta(seconds=seconds)
        self._monotonic += seconds


def parse_utc(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("governance timestamp 无效") from error
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("governance timestamp 必须是带 timezone 的 UTC 时间")
    return value.astimezone(timezone.utc)


def utc_text(value):
    return parse_utc(value).isoformat().replace("+00:00", "Z")


def _duration(value, name, allow_none=False):
    if allow_none and value is None:
        return
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ValueError(f"governance {name} 无效")


def _limit(value, name):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"governance {name} 无效")


def validate_governance_state(state):
    if not isinstance(state, dict) or set(state) != GOVERNANCE_FIELDS:
        raise ValueError("governance state schema 无效")
    for name in ("run_started_at", "run_deadline_at"):
        parse_utc(state[name])
    for name in ("step_started_at", "step_deadline_at"):
        if state[name] is not None:
            parse_utc(state[name])
    if (state["step_started_at"] is None) != (state["step_deadline_at"] is None):
        raise ValueError("governance step deadline 不完整")
    _duration(state["tool_timeout_seconds"], "tool_timeout_seconds")
    if state["tool_timeout_seconds"] == 0:
        raise ValueError("governance tool_timeout_seconds 必须大于零")
    for name in ("actions_used", "max_actions", "subagent_calls_used",
                 "max_subagent_calls", "safety_reconciliation_actions_used",
                 "max_safety_reconciliation_actions"):
        _limit(state[name], name)
    if state["actions_used"] > state["max_actions"]:
        raise ValueError("governance action budget 无效")
    if state["subagent_calls_used"] > state["max_subagent_calls"]:
        raise ValueError("governance subagent budget 无效")
    if state["safety_reconciliation_actions_used"] > state["max_safety_reconciliation_actions"]:
        raise ValueError("governance safety reconciliation budget 无效")
    if not isinstance(state["frozen"], bool):
        raise ValueError("governance frozen 无效")
    if state["freeze_reason"] not in {None, "user_pause", "approval_wait"}:
        raise ValueError("governance freeze_reason 无效")
    for name in ("frozen_run_remaining_seconds", "frozen_step_remaining_seconds"):
        _duration(state[name], name, allow_none=True)
    if state["frozen"]:
        if state["freeze_reason"] is None or state["frozen_run_remaining_seconds"] is None:
            raise ValueError("governance frozen state 不完整")
    elif any(state[name] is not None for name in (
        "freeze_reason", "frozen_run_remaining_seconds",
        "frozen_step_remaining_seconds",
    )):
        raise ValueError("governance active state 不能保留 freeze 数据")
    return state


def create_governance_state(run_timeout_seconds=300,
                            tool_timeout_seconds=DEFAULT_TOOL_TIMEOUT_SECONDS,
                            max_actions=DEFAULT_MAX_ACTIONS,
                            max_subagent_calls=DEFAULT_MAX_SUBAGENT_CALLS,
                            clock=None):
    _duration(run_timeout_seconds, "run_timeout_seconds")
    if run_timeout_seconds == 0:
        raise ValueError("run_timeout_seconds 必须大于零")
    clock = clock or Clock()
    now = clock.utc_now()
    state = {
        "run_started_at": utc_text(now),
        "run_deadline_at": utc_text(now + timedelta(seconds=run_timeout_seconds)),
        "step_started_at": None, "step_deadline_at": None,
        "tool_timeout_seconds": float(tool_timeout_seconds),
        "actions_used": 0, "max_actions": max_actions,
        "subagent_calls_used": 0, "max_subagent_calls": max_subagent_calls,
        "frozen": False, "freeze_reason": None,
        "frozen_run_remaining_seconds": None,
        "frozen_step_remaining_seconds": None,
        "safety_reconciliation_actions_used": 0,
        "max_safety_reconciliation_actions": DEFAULT_MAX_SAFETY_RECONCILIATION_ACTIONS,
    }
    return validate_governance_state(state)


def _remaining(deadline, clock):
    return max(0.0, (parse_utc(deadline) - clock.utc_now()).total_seconds())


def run_remaining(state, clock=None):
    validate_governance_state(state)
    if state["frozen"]:
        return state["frozen_run_remaining_seconds"]
    return _remaining(state["run_deadline_at"], clock or Clock())


def step_remaining(state, clock=None):
    validate_governance_state(state)
    if state["step_deadline_at"] is None:
        return None
    if state["frozen"]:
        return state["frozen_step_remaining_seconds"]
    return _remaining(state["step_deadline_at"], clock or Clock())


def start_step_deadline(state, timeout_seconds, clock=None):
    validate_governance_state(state)
    _duration(timeout_seconds, "step_timeout_seconds")
    if timeout_seconds == 0 or state["frozen"]:
        raise ValueError("不能以零时限或 frozen 状态开始 step")
    clock = clock or Clock()
    now = clock.utc_now()
    updated = copy.deepcopy(state)
    updated["step_started_at"] = utc_text(now)
    updated["step_deadline_at"] = utc_text(now + timedelta(seconds=timeout_seconds))
    return validate_governance_state(updated)


def clear_step_deadline(state):
    validate_governance_state(state)
    updated = copy.deepcopy(state)
    updated["step_started_at"] = None
    updated["step_deadline_at"] = None
    if updated["frozen"]:
        updated["frozen_step_remaining_seconds"] = None
    return validate_governance_state(updated)


def freeze_governance(state, reason, clock=None):
    validate_governance_state(state)
    if reason not in {"user_pause", "approval_wait"}:
        raise ValueError("governance freeze reason 无效")
    if state["frozen"]:
        return copy.deepcopy(state)
    updated = copy.deepcopy(state)
    updated.update({
        "frozen": True, "freeze_reason": reason,
        "frozen_run_remaining_seconds": run_remaining(state, clock),
        "frozen_step_remaining_seconds": step_remaining(state, clock),
    })
    return validate_governance_state(updated)


def resume_governance(state, clock=None):
    validate_governance_state(state)
    if not state["frozen"]:
        return copy.deepcopy(state)
    clock = clock or Clock()
    now = clock.utc_now()
    updated = copy.deepcopy(state)
    updated["run_deadline_at"] = utc_text(
        now + timedelta(seconds=state["frozen_run_remaining_seconds"])
    )
    if state["step_deadline_at"] is not None:
        updated["step_deadline_at"] = utc_text(
            now + timedelta(seconds=state["frozen_step_remaining_seconds"])
        )
    updated.update({"frozen": False, "freeze_reason": None,
                    "frozen_run_remaining_seconds": None,
                    "frozen_step_remaining_seconds": None})
    return validate_governance_state(updated)


def deadline_status(state, clock=None):
    if run_remaining(state, clock) <= 0:
        return "run deadline exceeded"
    remaining = step_remaining(state, clock)
    if remaining is not None and remaining <= 0:
        return "step deadline exceeded"
    return None


def effective_tool_timeout(state, clock=None, local_timeout=None):
    validate_governance_state(state)
    limits = [state["tool_timeout_seconds"], run_remaining(state, clock)]
    remaining = step_remaining(state, clock)
    if remaining is not None:
        limits.append(remaining)
    if local_timeout is not None:
        _duration(local_timeout, "local_timeout")
        limits.append(local_timeout)
    return min(limits)


def normal_action_decision(state, kind="tool", clock=None):
    validate_governance_state(state)
    if state["frozen"]:
        return {"allowed": False, "reason": "execution deadline frozen"}
    reason = deadline_status(state, clock)
    if reason:
        return {"allowed": False, "reason": reason}
    if state["actions_used"] >= state["max_actions"]:
        return {"allowed": False, "reason": "run action budget exhausted"}
    if kind == "subagent" and state["subagent_calls_used"] >= state["max_subagent_calls"]:
        return {"allowed": False, "reason": "run subagent budget exhausted"}
    return {"allowed": True, "reason": None}


def consume_action(state, kind="tool", clock=None):
    decision = normal_action_decision(state, kind, clock)
    if not decision["allowed"]:
        raise ValueError(decision["reason"])
    updated = copy.deepcopy(state)
    updated["actions_used"] += 1
    if kind == "subagent":
        updated["subagent_calls_used"] += 1
    return validate_governance_state(updated)


def backoff_decision(state, seconds, clock=None):
    _duration(seconds, "backoff_seconds")
    reason = deadline_status(state, clock)
    available = run_remaining(state, clock)
    remaining = step_remaining(state, clock)
    if remaining is not None:
        available = min(available, remaining)
    if reason or seconds > available:
        return {"allowed": False, "reason": reason or "backoff cannot fit remaining deadline"}
    return {"allowed": True, "reason": None}


def effective_subagent_timeout(state, requested_seconds, clock=None):
    _duration(requested_seconds, "requested_subagent_timeout")
    limits = [requested_seconds, run_remaining(state, clock)]
    remaining = step_remaining(state, clock)
    if remaining is not None:
        limits.append(remaining)
    return min(limits)


def safety_reconciliation_decision(state, checkpoint, capability_effect,
                                   related_to_target, security_allowed=True):
    """A narrow exception to deadline/budget/cancel, never to Security/DENY."""
    validate_governance_state(state)
    if not security_allowed:
        return {"allowed": False, "reason": "security policy denied"}
    if not isinstance(checkpoint, dict) or checkpoint.get("state") != "unknown":
        return {"allowed": False, "reason": "no unknown action checkpoint"}
    if checkpoint.get("effect") not in {"side_effecting", "unknown"}:
        return {"allowed": False, "reason": "checkpoint does not contain an uncertain side effect"}
    if capability_effect != "read_only":
        return {"allowed": False, "reason": "safety reconciliation must be read-only"}
    if not related_to_target:
        return {"allowed": False, "reason": "reconciliation is unrelated to the unknown action"}
    if state["safety_reconciliation_actions_used"] >= state["max_safety_reconciliation_actions"]:
        return {"allowed": False, "reason": "safety reconciliation budget exhausted"}
    return {"allowed": True, "reason": None}


def consume_safety_reconciliation(state):
    validate_governance_state(state)
    if state["safety_reconciliation_actions_used"] >= state["max_safety_reconciliation_actions"]:
        raise ValueError("safety reconciliation budget exhausted")
    updated = copy.deepcopy(state)
    updated["safety_reconciliation_actions_used"] += 1
    return validate_governance_state(updated)


def governance_context(state, clock=None, safety_mode=False):
    validate_governance_state(state)
    step = step_remaining(state, clock)
    return {
        "run_remaining": round(run_remaining(state, clock), 3),
        "step_remaining": None if step is None else round(step, 3),
        "actions": f"{state['actions_used']}/{state['max_actions']}",
        "subagents": f"{state['subagent_calls_used']}/{state['max_subagent_calls']}",
        "deadline_status": "exceeded" if deadline_status(state, clock) else "active",
        "mode": "safety_reconciliation" if safety_mode else "normal",
    }
