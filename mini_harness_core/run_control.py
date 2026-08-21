"""Harness-owned cooperative pause, cancel, and resume state."""

import copy
from datetime import datetime, timezone


RUN_CONTROL_STATES = frozenset({
    "running", "pause_requested", "paused", "cancel_requested", "cancelled",
})
RUN_CONTROL_FIELDS = frozenset({"state", "reason", "requested_at", "updated_at"})
_TRANSITIONS = {
    "running": {"pause_requested", "cancel_requested"},
    "pause_requested": {"paused", "cancel_requested"},
    "paused": {"running", "cancel_requested"},
    "cancel_requested": {"cancelled"},
    "cancelled": set(),
}


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def create_run_control(now=None):
    timestamp = now or utc_now()
    return {"state": "running", "reason": None,
            "requested_at": None, "updated_at": timestamp}


def validate_run_control(control):
    if not isinstance(control, dict) or set(control) != RUN_CONTROL_FIELDS:
        raise ValueError("run_control schema 无效")
    if control["state"] not in RUN_CONTROL_STATES:
        raise ValueError("run_control state 无效")
    if control["reason"] is not None and (
        not isinstance(control["reason"], str) or not control["reason"].strip()
    ):
        raise ValueError("run_control reason 无效")
    if control["requested_at"] is not None and not isinstance(
        control["requested_at"], str
    ):
        raise ValueError("run_control requested_at 无效")
    if not isinstance(control["updated_at"], str) or not control["updated_at"]:
        raise ValueError("run_control updated_at 无效")
    if control["state"] == "running" and control["requested_at"] is not None:
        raise ValueError("running 状态不能保留 requested_at")
    return control


def _transition(control, state, reason=None, now=None, new_request=False):
    validate_run_control(control)
    if state not in _TRANSITIONS[control["state"]]:
        raise ValueError(f"非法 run control transition：{control['state']} -> {state}")
    timestamp = now or utc_now()
    updated = copy.deepcopy(control)
    updated["state"] = state
    updated["updated_at"] = timestamp
    if new_request:
        updated["reason"] = reason
        updated["requested_at"] = timestamp
    elif state == "running":
        updated["reason"] = None
        updated["requested_at"] = None
    validate_run_control(updated)
    return updated


def request_pause(control, reason="user requested pause", now=None):
    return _transition(control, "pause_requested", reason, now, True)


def mark_paused(control, now=None):
    return _transition(control, "paused", now=now)


def request_cancel(control, reason="user requested cancel", now=None):
    return _transition(control, "cancel_requested", reason, now, True)


def mark_cancelled(control, now=None):
    return _transition(control, "cancelled", now=now)


def resume_run(control, now=None):
    return _transition(control, "running", now=now)


def can_schedule_action(control):
    validate_run_control(control)
    return control["state"] == "running"


def settle_control_boundary(control, now=None):
    """Finish a cooperative request at a reliable action boundary."""
    validate_run_control(control)
    if control["state"] == "pause_requested":
        return mark_paused(control, now)
    if control["state"] == "cancel_requested":
        return mark_cancelled(control, now)
    return copy.deepcopy(control)
