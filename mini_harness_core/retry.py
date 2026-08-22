"""Harness-owned failure classification and bounded retry policy.

Purpose: decide whether a definite failed attempt may lead to another attempt.
Owns: failure classes, attempt accounting, retry decisions, and backoff state.
Does Not Own: crash recovery, side-effect reconciliation, execution, or replanning.
Key Invariants: Retry is not Replay; durability limits take precedence; unknown
or possibly side-effecting outcomes never become an automatic retry.
"""

import copy
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

from .security import SECRET_PATTERNS

FAILURE_CLASSES = frozenset({"transient", "permanent", "policy", "user_rejected", "unknown"})
RETRY_POLICIES = frozenset({"no_retry", "retry_with_backoff", "reconcile_before_retry", "replan", "block"})
RETRY_STATES = frozenset({"ready", "waiting_backoff", "awaiting_approval", "executing", "reconciling", "exhausted", "completed", "blocked"})
RETRY_FIELDS = frozenset({"logical_action_id", "step_id", "attempt_count", "max_attempts", "last_failure_class", "last_reason_code", "retry_policy", "next_retry_at", "backoff_delay", "state", "created_at", "updated_at"})
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 1.0


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _contains_secret(value):
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in SECRET_PATTERNS)
    if isinstance(value, dict):
        return any(_contains_secret(k) or _contains_secret(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(item) for item in value)
    return False


def validate_retry_state(state):
    if not isinstance(state, dict) or set(state) != RETRY_FIELDS:
        raise ValueError("retry state schema 无效")
    if not isinstance(state["logical_action_id"], str) or not state["logical_action_id"]:
        raise ValueError("logical_action_id 无效")
    if state["step_id"] is not None and not isinstance(state["step_id"], str):
        raise ValueError("retry step_id 无效")
    for name in ("attempt_count", "max_attempts"):
        if not isinstance(state[name], int) or isinstance(state[name], bool):
            raise ValueError(f"retry {name} 无效")
    if not 0 <= state["attempt_count"] <= state["max_attempts"] or state["max_attempts"] < 1:
        raise ValueError("retry budget 无效")
    if state["last_failure_class"] is not None and state["last_failure_class"] not in FAILURE_CLASSES:
        raise ValueError("last_failure_class 无效")
    if state["last_reason_code"] is not None and not isinstance(state["last_reason_code"], str):
        raise ValueError("last_reason_code 无效")
    if state["retry_policy"] not in RETRY_POLICIES or state["state"] not in RETRY_STATES:
        raise ValueError("retry policy/state 无效")
    if state["next_retry_at"] is not None and not isinstance(state["next_retry_at"], str):
        raise ValueError("next_retry_at 无效")
    if not isinstance(state["backoff_delay"], (int, float)) or isinstance(state["backoff_delay"], bool) or state["backoff_delay"] < 0:
        raise ValueError("backoff_delay 无效")
    if not all(isinstance(state[name], str) and state[name] for name in ("created_at", "updated_at")):
        raise ValueError("retry timestamp 无效")
    if _contains_secret(state):
        raise ValueError("retry state 疑似包含 secret")
    try:
        json.dumps(state, ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise ValueError("retry state 不是 JSON-safe 数据") from error
    return state


def create_retry_state(step_id=None, max_attempts=DEFAULT_MAX_ATTEMPTS, logical_action_id=None, now=None):
    timestamp = now or utc_now()
    return validate_retry_state({
        "logical_action_id": logical_action_id or uuid.uuid4().hex, "step_id": step_id,
        "attempt_count": 0, "max_attempts": max_attempts, "last_failure_class": None,
        "last_reason_code": None, "retry_policy": "no_retry", "next_retry_at": None,
        "backoff_delay": 0, "state": "ready", "created_at": timestamp, "updated_at": timestamp,
    })


def start_attempt(state, now=None):
    validate_retry_state(state)
    if state["attempt_count"] >= state["max_attempts"]:
        raise ValueError("retry budget 已耗尽")
    updated = copy.deepcopy(state)
    updated.update({"attempt_count": state["attempt_count"] + 1, "state": "executing", "next_retry_at": None, "backoff_delay": 0, "updated_at": now or utc_now()})
    return validate_retry_state(updated)


def classify_failure(observation):
    if not isinstance(observation, dict):
        return {"failure_class": "unknown", "reason_code": "invalid_observation"}
    denied = observation.get("denied_by")
    if denied == "policy" or denied in {"verification_gate", "memory_policy", "capability_validation"}:
        return {"failure_class": "policy", "reason_code": denied}
    if denied == "user":
        return {"failure_class": "user_rejected", "reason_code": "approval_rejected"}
    text = " ".join(str(observation.get(k, "")) for k in ("error", "stderr", "status")).lower()
    if observation.get("exit_code") == -1 or any(token in text for token in ("timeout", "timed out", "temporarily unavailable", "server unavailable", "connection reset", "connection refused", "transport lost", "503", "429")):
        reason = "rate_limited" if "429" in text or "rate limit" in text else ("timeout" if "timeout" in text or "timed out" in text else "transport_unavailable")
        return {"failure_class": "transient", "reason_code": reason}
    if any(token in text for token in ("no such file", "not found", "permission denied", "invalid argument", "schema")):
        return {"failure_class": "permanent", "reason_code": "validation_or_environment"}
    if observation.get("exit_code") not in (None, 0):
        return {"failure_class": "permanent", "reason_code": "nonzero_exit"}
    return {"failure_class": "unknown", "reason_code": "unclassified"}


def decide_retry(failure_class, effect, replay_policy, attempt_count, max_attempts, run_state="running"):
    # Precedence is safety-significant: user control and durable replay policy
    # are checked before transient-failure convenience. Retry may schedule a new
    # attempt, but cannot reinterpret an uncertain old one.
    if run_state in {"pause_requested", "paused", "cancel_requested", "cancelled"}:
        return "block"
    if failure_class in {"policy", "user_rejected"}:
        return "no_retry"
    if replay_policy == "never_auto_retry":
        return "block"
    # A non-zero result does not prove that a dispatched side effect did nothing.
    if effect in {"side_effecting", "unknown"}:
        return "reconcile_before_retry"
    if failure_class == "unknown":
        return "block"
    if failure_class == "permanent" or attempt_count >= max_attempts:
        return "replan"
    return "retry_with_backoff"


def record_failure(state, failure_class, reason_code, retry_policy, base_delay=DEFAULT_BASE_DELAY, now=None):
    validate_retry_state(state)
    if failure_class not in FAILURE_CLASSES or retry_policy not in RETRY_POLICIES:
        raise ValueError("failure/retry policy 无效")
    updated = copy.deepcopy(state)
    timestamp = now or datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if isinstance(timestamp, str) else timestamp
    timestamp_text = timestamp if isinstance(timestamp, str) else timestamp.isoformat().replace("+00:00", "Z")
    delay = base_delay * (2 ** max(0, state["attempt_count"] - 1)) if retry_policy == "retry_with_backoff" else 0
    terminal_state = "waiting_backoff" if retry_policy == "retry_with_backoff" else ("reconciling" if retry_policy == "reconcile_before_retry" else ("exhausted" if retry_policy == "replan" and state["attempt_count"] >= state["max_attempts"] else "blocked"))
    updated.update({"last_failure_class": failure_class, "last_reason_code": reason_code, "retry_policy": retry_policy, "backoff_delay": delay, "next_retry_at": (parsed + timedelta(seconds=delay)).isoformat().replace("+00:00", "Z") if delay else None, "state": terminal_state, "updated_at": timestamp_text})
    return validate_retry_state(updated)


def complete_retry(state, now=None):
    validate_retry_state(state)
    updated = copy.deepcopy(state)
    updated.update({"state": "completed", "retry_policy": "no_retry", "next_retry_at": None, "backoff_delay": 0, "updated_at": now or utc_now()})
    return validate_retry_state(updated)


def reopen_retry_after_reconciliation(state, replay_policy, run_state="running",
                                      policy_allows=True, now=None):
    """Open a gate, never an execution, after fresh evidence proves no effect."""
    validate_retry_state(state)
    updated = copy.deepcopy(state)
    allowed = (
        state["last_failure_class"] == "transient"
        and state["attempt_count"] < state["max_attempts"]
        and replay_policy != "never_auto_retry"
        and run_state == "running"
        and policy_allows
    )
    updated.update({
        "retry_policy": "retry_with_backoff" if allowed else "no_retry",
        "state": "ready" if allowed else "blocked",
        "next_retry_at": None,
        "backoff_delay": 0,
        "updated_at": now or utc_now(),
    })
    return validate_retry_state(updated)


def cooperative_backoff(delay, run_control, sleeper=None, quantum=0.1):
    sleeper = sleeper or time.sleep
    remaining = float(delay)
    while remaining > 1e-9:
        if run_control.get("state") != "running":
            return False
        chunk = min(quantum, remaining)
        sleeper(chunk)
        remaining -= chunk
    return run_control.get("state") == "running"


def retry_context(state):
    validate_retry_state(state)
    return {"attempt": f"{state['attempt_count']}/{state['max_attempts']}", "last_failure": state["last_failure_class"], "retry_allowed": state["retry_policy"] == "retry_with_backoff" and state["attempt_count"] < state["max_attempts"], "next": state["retry_policy"]}
