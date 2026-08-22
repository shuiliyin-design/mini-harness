"""Pure helpers for fresh verification decisions and target correlation.

Purpose: determine whether a recorded read-only observation can verify a prior
side effect and produce deterministic feedback/replay inputs.
Owns: target extraction, relatedness checks, observation identity, and the pure
verification transition.
Does Not Own: executing verification commands, creating execution authority,
persisting Evidence, or declaring the whole task complete.
Key Invariants: Tool success is not verification; verification is read-only and,
when a target is known, must observe that same target.
"""

import hashlib
import json
import os
import shlex
import re

from .observation import observation_digest


SHELL_OPERATORS = {"&&", "||", ";", "|", "&", ">", ">>", "<", "<<"}
LS_OPTION_CHARS = frozenset("aAlh1")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def verification_observation_identity(observation, event_id=None):
    """Reduce one historical observation to safe metadata; never copy output."""
    digest = observation_digest(observation)
    identity = {"observation_event_id": event_id,
                "exit_code": observation.get("exit_code")}
    for name in ("stdout", "stderr"):
        identity[f"{name}_length"] = digest.get(f"{name}_length", 0)
        identity[f"{name}_sha256"] = digest.get(
            f"{name}_sha256", hashlib.sha256(b"").hexdigest()
        )
    for name in ("status", "denied_by"):
        if name in observation:
            identity[name] = observation[name]
    return identity


def replay_verification_transition(inputs):
    """Replay the gate from recorded evidence identity, never current reality."""
    required = {
        "requires_verification", "verification_target", "action_effect",
        "evidence_related", "historical_recorded_observation", "observation",
    }
    if not isinstance(inputs, dict) or set(inputs) != required:
        raise ValueError("verification replay input evidence 不完整")
    if inputs["historical_recorded_observation"] is not True:
        raise ValueError("verification replay 只接受 historical observation")
    if not isinstance(inputs["requires_verification"], bool) or not isinstance(
        inputs["evidence_related"], bool
    ):
        raise ValueError("verification replay gate input 无效")
    observation = inputs["observation"]
    base_fields = {
        "observation_event_id", "exit_code", "stdout_length", "stdout_sha256",
        "stderr_length", "stderr_sha256",
    }
    if not isinstance(observation, dict) or not base_fields.issubset(observation):
        raise ValueError("verification historical observation evidence 不完整")
    if set(observation) - base_fields - {"status", "denied_by"}:
        raise ValueError("verification observation metadata 无效")
    if observation["observation_event_id"] is not None and not isinstance(
        observation["observation_event_id"], str
    ):
        raise ValueError("verification observation event reference 无效")
    if not isinstance(observation["exit_code"], int) or isinstance(
        observation["exit_code"], bool
    ):
        raise ValueError("verification observation exit_code 无效")
    for name in ("stdout", "stderr"):
        if not isinstance(observation[f"{name}_length"], int) or isinstance(
            observation[f"{name}_length"], bool
        ) or observation[f"{name}_length"] < 0 or not SHA256_PATTERN.fullmatch(
            str(observation[f"{name}_sha256"])
        ):
            raise ValueError("verification observation identity 无效")
    evidence_sha256 = hashlib.sha256(json.dumps(
        observation, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()
    accepted = False
    reason = "verification still required"
    if not inputs["requires_verification"]:
        reason = "verification was not required"
    elif inputs["action_effect"] != "read_only":
        reason = "verification tool must be read-only"
    elif inputs["verification_target"] is not None and not inputs["evidence_related"]:
        reason = "verification evidence is not related to the modified target"
    elif observation["exit_code"] != 0:
        reason = "verification observation failed"
    else:
        accepted, reason = True, None
    return {
        "accepted": accepted,
        "next_verification_state": "not_required" if accepted else "required",
        "reason": reason,
        "evidence_identity_sha256": evidence_sha256,
    }


def _is_within_workspace(path):
    workspace = os.path.realpath(os.getcwd())
    candidate = os.path.realpath(os.path.abspath(path))
    try:
        return os.path.commonpath((workspace, candidate)) == workspace
    except ValueError:
        return False


def _parse_shell_tokens(command):
    """用与教学级 Policy 相同的规则拆分一条 shell 命令。"""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        return list(lexer)
    except (TypeError, ValueError):
        return None


def _normalized_workspace_path(path):
    """返回安全的 workspace 相对路径；不解析或猜测 shell 展开。"""
    if not isinstance(path, str) or not path or path.startswith("-"):
        return None
    path_parts = path.replace("\\", "/").split("/")
    if os.path.isabs(path) or ".." in path_parts:
        return None
    if any(marker in path for marker in ("`", "$", "~", "*", "?", "[")):
        return None
    if not _is_within_workspace(path):
        return None
    normalized = os.path.normpath(path)
    if normalized in ("", "."):
        return None
    return normalized.replace(os.sep, "/")


def extract_verification_target(command):
    """从少量严格白名单写法提取单一目标；不确定时返回 None。"""
    if not isinstance(command, str) or any(
        marker in command for marker in ("`", "$", "~", "*", "?", "[")
    ):
        return None
    tokens = _parse_shell_tokens(command)
    if not tokens:
        return None

    target_type = None
    raw_path = None
    if tokens[0] == "echo":
        if tokens.count(">") != 1 or tokens[-2:-1] != [">"]:
            return None
        if len(tokens) < 4 or any(
            token in SHELL_OPERATORS for token in tokens[1:-2]
        ):
            return None
        target_type = "file"
        raw_path = tokens[-1]
    elif tokens[0] == "touch" and len(tokens) == 2:
        target_type = "file"
        raw_path = tokens[1]
    elif tokens[0] == "mkdir" and len(tokens) == 2:
        target_type = "directory"
        raw_path = tokens[1]
    else:
        return None

    path = _normalized_workspace_path(raw_path)
    if path is None:
        return None
    return {"target_type": target_type, "path": path}


def is_related_verification(command, target):
    """判断最小只读证据是否明确读取了同一个 file/directory。"""
    if not isinstance(target, dict):
        return False
    tokens = _parse_shell_tokens(command)
    if not tokens:
        return False

    raw_path = None
    if target.get("target_type") == "file":
        if len(tokens) != 2 or tokens[0] != "cat":
            return False
        raw_path = tokens[1]
    elif target.get("target_type") == "directory":
        if tokens[0] != "ls":
            return False
        paths = []
        for token in tokens[1:]:
            if token.startswith("-"):
                if token == "--" or not set(token[1:]).issubset(LS_OPTION_CHARS):
                    return False
            else:
                paths.append(token)
        if len(paths) != 1:
            return False
        raw_path = paths[0]
    else:
        return False

    path = _normalized_workspace_path(raw_path)
    return path is not None and path == target.get("path")


def build_verification_feedback(latest_write_command, verification_target, policy_allow):
    return {
        "type": "verification_feedback",
        "status": "final_answer_rejected",
        "final_answer_allowed": False,
        "reason": "verification required before final answer",
        "required_next_action": {
            "type": "tool_call",
            "tool": "shell",
            "policy_must_be": policy_allow,
            "command_must_be": "read-only",
            "purpose": "verify the most recent successful write operation",
        },
        "write_operation_to_verify": latest_write_command,
        "verification_target": verification_target,
        "instruction": (
            "Do not submit final_answer now. Request one read-only shell "
            "tool_call classified as Policy=ALLOW to verify the write "
            "operation above. Submit final_answer only after that tool "
            "call succeeds."
        ),
    }
