"""Teaching-grade action checkpoints and conservative crash recovery."""

import copy
import json
import re
import uuid
from datetime import datetime, timezone

from .security import SECRET_PATTERNS
from .observation import persisted_safe_observation
from .verification import extract_verification_target


ACTION_STATES = frozenset({"prepared", "executing", "succeeded", "failed", "unknown"})
EFFECTS = frozenset({"read_only", "side_effecting", "unknown"})
REPLAY_POLICIES = frozenset({
    "safe_to_retry", "requires_reconciliation", "never_auto_retry",
})
CHECKPOINT_FIELDS = frozenset({
    "action_id", "plan_id", "plan_version", "step_id", "tool", "arguments",
    "effect", "replay_policy", "state", "observation", "created_at", "updated_at",
})
_TRANSITIONS = {
    "prepared": {"executing"},
    "executing": {"succeeded", "failed", "unknown"},
    "unknown": {"succeeded", "failed"},
    "succeeded": set(),
    "failed": set(),
}
_ECHO_WRITE = re.compile(r"^echo\s+(?P<text>'[^']*'|\"[^\"]*\"|[^;&|<>]+?)\s*>\s*(?P<path>[^\s;&|<>]+)$")


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _contains_secret(value):
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in SECRET_PATTERNS)
    if isinstance(value, dict):
        return any(_contains_secret(key) or _contains_secret(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(item) for item in value)
    return False


def default_replay_policy(effect):
    if effect not in EFFECTS:
        raise ValueError("action effect 无效")
    return "safe_to_retry" if effect == "read_only" else "requires_reconciliation"


def validate_action_checkpoint(checkpoint):
    if not isinstance(checkpoint, dict) or set(checkpoint) != CHECKPOINT_FIELDS:
        raise ValueError("action checkpoint schema 无效")
    for name in ("action_id", "tool", "created_at", "updated_at"):
        if not isinstance(checkpoint[name], str) or not checkpoint[name].strip():
            raise ValueError(f"action checkpoint {name} 无效")
    for name in ("plan_id", "step_id"):
        if checkpoint[name] is not None and (
            not isinstance(checkpoint[name], str) or not checkpoint[name].strip()
        ):
            raise ValueError(f"action checkpoint {name} 无效")
    version = checkpoint["plan_version"]
    if version is not None and (
        not isinstance(version, int) or isinstance(version, bool) or version < 1
    ):
        raise ValueError("action checkpoint plan_version 无效")
    if not isinstance(checkpoint["arguments"], dict):
        raise ValueError("action checkpoint arguments 必须是对象")
    if checkpoint["effect"] not in EFFECTS:
        raise ValueError("action checkpoint effect 无效")
    if checkpoint["replay_policy"] not in REPLAY_POLICIES:
        raise ValueError("action checkpoint replay_policy 无效")
    if checkpoint["state"] not in ACTION_STATES:
        raise ValueError("action checkpoint state 无效")
    observation = checkpoint["observation"]
    if observation is not None and not isinstance(observation, dict):
        raise ValueError("action checkpoint observation 无效")
    if checkpoint["state"] in {"succeeded", "failed"} and observation is None:
        raise ValueError("terminal action checkpoint 缺少 observation")
    if _contains_secret(checkpoint):
        raise ValueError("action checkpoint 疑似包含 secret 或 Authorization")
    try:
        json.dumps(checkpoint, ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise ValueError("action checkpoint 不是 JSON-safe 数据") from error
    return checkpoint


def create_action_checkpoint(
    tool, arguments, effect, plan_id=None, plan_version=None, step_id=None,
    replay_policy=None, action_id=None,
):
    policy = replay_policy or default_replay_policy(effect)
    # Only local Harness callers may pass a stricter override. No external
    # metadata is consulted here, and non-read-only effects cannot be promoted.
    if effect != "read_only" and policy == "safe_to_retry":
        raise ValueError("非只读 action 不能提升为 safe_to_retry")
    now = utc_now()
    checkpoint = {
        "action_id": action_id or uuid.uuid4().hex,
        "plan_id": plan_id,
        "plan_version": plan_version,
        "step_id": step_id,
        "tool": tool,
        "arguments": copy.deepcopy(arguments),
        "effect": effect,
        "replay_policy": policy,
        "state": "prepared",
        "observation": None,
        "created_at": now,
        "updated_at": now,
    }
    validate_action_checkpoint(checkpoint)
    return checkpoint


def summarize_observation(observation):
    return persisted_safe_observation(observation)


def transition_action_checkpoint(checkpoint, state, observation=None):
    validate_action_checkpoint(checkpoint)
    if state not in _TRANSITIONS[checkpoint["state"]]:
        raise ValueError(f"非法 action state transition：{checkpoint['state']} -> {state}")
    updated = copy.deepcopy(checkpoint)
    updated["state"] = state
    updated["updated_at"] = utc_now()
    if state in {"succeeded", "failed"}:
        updated["observation"] = summarize_observation(observation)
    validate_action_checkpoint(updated)
    return updated


def recover_action_checkpoint(checkpoint):
    """Return a recovered snapshot and a deterministic Harness decision."""
    validate_action_checkpoint(checkpoint)
    recovered = copy.deepcopy(checkpoint)
    if recovered["state"] == "executing":
        recovered = transition_action_checkpoint(recovered, "unknown")
    state = recovered["state"]
    if state == "prepared":
        action = "retry_with_fresh_approval"
    elif state == "succeeded":
        action = "continue_with_observation"
    elif state == "failed":
        action = "return_to_plan"
    elif recovered["replay_policy"] == "safe_to_retry":
        action = "retry_as_new_action"
    else:
        action = "reconcile_or_block"
    return recovered, action


def recovery_control_state(checkpoint):
    validate_action_checkpoint(checkpoint)
    if checkpoint["state"] not in {"prepared", "unknown"}:
        return None
    return {
        "required": True,
        "tool": checkpoint["tool"],
        "state": checkpoint["state"],
        "effect": checkpoint["effect"],
        "replay_policy": checkpoint["replay_policy"],
        "instruction": (
            "retry as a new action with fresh approval"
            if checkpoint["replay_policy"] == "safe_to_retry"
            else "reconciliation required; do not repeat the original action"
        ),
    }


def expected_file_write(checkpoint):
    """Recognize only the tiny shell form supported by V13 reconciliation."""
    validate_action_checkpoint(checkpoint)
    if checkpoint["tool"] != "shell":
        return None
    command = checkpoint["arguments"].get("command")
    target = extract_verification_target(command) if isinstance(command, str) else None
    match = _ECHO_WRITE.fullmatch(command or "")
    if target is None or target.get("target_type") != "file" or match is None:
        return None
    text = match.group("text").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1]
    return {"path": target["path"], "expected_stdout": text + "\n"}


def reconcile_file_observation(checkpoint, command, observation):
    """Evaluate fresh, read-only evidence without executing either action."""
    expected = expected_file_write(checkpoint)
    if expected is None or command not in {
        f"cat {expected['path']}", f"ls {expected['path']}",
    }:
        return {"status": "blocked", "reason": "uncertain side effect"}
    if not isinstance(observation, dict):
        return {"status": "blocked", "reason": "uncertain side effect"}
    if observation.get("exit_code") != 0:
        stderr = str(observation.get("stderr", "")).lower()
        proves_absence = (
            command.startswith(("cat ", "ls "))
            and observation.get("stdout", "") == ""
            and any(marker in stderr for marker in ("no such file", "not found"))
        )
        if not proves_absence:
            return {"status": "blocked", "reason": "uncertain side effect"}
        evidence = {
            "kind": "reconciliation_absence_observation",
            "summary": f"fresh read-only evidence confirmed {expected['path']} is absent",
            "verified": True,
        }
        failed = transition_action_checkpoint(checkpoint, "failed", {
            "status": "reconciled_not_applied", "exit_code": observation["exit_code"],
            "verification_target": {"target_type": "file", "path": expected["path"]},
        })
        return {
            "status": "not_applied", "checkpoint": failed, "evidence": evidence,
        }
    if command.startswith("ls "):
        return {"status": "blocked", "reason": "uncertain side effect"}
    if observation.get("stdout") != expected["expected_stdout"]:
        return {"status": "blocked", "reason": "uncertain side effect"}
    evidence = {
        "kind": "reconciliation_observation",
        "summary": f"fresh read-only evidence confirmed {expected['path']}",
        "verified": True,
    }
    succeeded = transition_action_checkpoint(checkpoint, "succeeded", {
        "status": "reconciled", "exit_code": 0,
        "verification_target": {"target_type": "file", "path": expected["path"]},
    })
    return {"status": "succeeded", "checkpoint": succeeded, "evidence": evidence}
