"""Projection boundaries for untrusted runtime observations."""

import copy
import hashlib
import json

from .security import SECRET_PATTERNS


SAFE_STRUCTURED_FIELDS = frozenset({
    "cwd", "path", "status", "denied_by", "verification_target", "source",
    "trust",
})
SAFE_MCP_RESULT_FIELDS = frozenset({"cwd", "path", "status"})


def _stream_text(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def _safe_scalar(value):
    return value is None or isinstance(value, (bool, int, float, str))


def _contains_secret(value):
    text = _stream_text(value)
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def observation_digest(observation):
    """Existing Audit/Evidence-compatible length and digest projection."""
    if not isinstance(observation, dict):
        raise ValueError("observation 必须是对象")
    projected = {}
    for key in ("status", "exit_code", "denied_by"):
        if key in observation and _safe_scalar(observation[key]):
            projected[key] = copy.deepcopy(observation[key])
    redacted = False
    for key in ("stdout", "stderr", "result", "error"):
        if key not in observation or observation[key] is None:
            continue
        raw = _stream_text(observation[key])
        encoded = raw.encode("utf-8", errors="replace")
        projected[f"{key}_length"] = len(raw)
        projected[f"{key}_sha256"] = hashlib.sha256(encoded).hexdigest()
        redacted = redacted or _contains_secret(raw)
    if redacted:
        projected["redacted"] = True
    return projected


def persisted_safe_observation(observation, capability=None, normalized_args=None):
    """Produce the only observation shape allowed in Session/checkpoints."""
    projected = observation_digest(observation)
    # Idempotence matters when a persisted Session is assembled again.
    for stream in ("stdout", "stderr", "result", "error"):
        length_key, digest_key = f"{stream}_length", f"{stream}_sha256"
        if (length_key in observation and digest_key in observation
                and isinstance(observation[length_key], int)
                and not isinstance(observation[length_key], bool)
                and observation[length_key] >= 0
                and isinstance(observation[digest_key], str)
                and len(observation[digest_key]) == 64):
            projected[length_key] = observation[length_key]
            projected[digest_key] = observation[digest_key]
    if observation.get("redacted") is True:
        projected["redacted"] = True
    existing_structured = observation.get("structured")
    if (isinstance(existing_structured, dict)
            and set(existing_structured).issubset(SAFE_MCP_RESULT_FIELDS)
            and all(_safe_scalar(value) for value in existing_structured.values())
            and not _contains_secret(existing_structured)):
        projected["structured"] = copy.deepcopy(existing_structured)
    for key in SAFE_STRUCTURED_FIELDS:
        value = observation.get(key)
        if key in observation and _safe_scalar(value) and not _contains_secret(value):
            projected[key] = copy.deepcopy(value)
    target = observation.get("verification_target")
    if (isinstance(target, dict)
            and set(target).issubset({"target_type", "path"})
            and all(_safe_scalar(value) for value in target.values())
            and not _contains_secret(target)):
        projected["verification_target"] = copy.deepcopy(target)

    args = normalized_args or {}
    if capability == "shell" and args.get("command") in {"pwd", "pwd -L", "pwd -P"}:
        stdout = observation.get("stdout")
        if (observation.get("exit_code") == 0 and isinstance(stdout, str)
                and stdout.strip() and "\n" not in stdout.strip()
                and not _contains_secret(stdout)):
            projected["cwd"] = stdout.strip()
    if capability and capability.startswith("mcp:"):
        result = observation.get("result")
        if isinstance(result, dict):
            structured = {
                key: copy.deepcopy(value) for key, value in result.items()
                if key in SAFE_MCP_RESULT_FIELDS and _safe_scalar(value)
                and not _contains_secret(value)
            }
            if structured:
                projected["structured"] = structured
    return projected


def model_context_observation(persisted):
    """Project Session-safe metadata once more before Provider transport."""
    if not isinstance(persisted, dict):
        raise ValueError("persisted observation 必须是对象")
    allowed = {
        "status", "exit_code", "denied_by", "redacted", "cwd", "path",
        "verification_target", "source", "trust", "structured",
        "stdout_length", "stdout_sha256", "stderr_length", "stderr_sha256",
        "result_length", "result_sha256", "error_length", "error_sha256",
    }
    return {key: copy.deepcopy(value) for key, value in persisted.items()
            if key in allowed}
