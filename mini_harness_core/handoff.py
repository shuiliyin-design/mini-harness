"""Structured handoff envelope validation and return-contract helpers."""

import json
import os
import uuid

from .memory import _SECRET_PATTERNS


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


HANDOFF_FIELDS = {
    "handoff_id", "task", "context", "constraints", "evidence",
    "workspace", "authority", "return_contract",
}
HANDOFF_WORKSPACE_FIELDS = {"cwd", "project_root", "relevant_paths"}
HANDOFF_AUTHORITY_FIELDS = {
    "allowed_tools", "can_write_workspace", "can_use_mcp", "max_steps",
}
HANDOFF_RETURN_FIELDS = {
    "require_summary", "require_evidence", "require_actions",
}


def _contains_secret(value):
    """Screen handoff-shaped JSON before it can become conversation state."""
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _SECRET_PATTERNS)
    if isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    if isinstance(value, dict):
        return any(
            _contains_secret(key) or _contains_secret(item)
            for key, item in value.items()
        )
    return False


def _validate_string_list(value, name):
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"handoff {name} 必须是非空字符串数组（可为空数组）")


def validate_handoff(handoff):
    """Validate the deliberately small V10 envelope and reject secrets."""
    if not isinstance(handoff, dict) or set(handoff) != HANDOFF_FIELDS:
        raise ValueError("handoff 顶层 schema 无效")
    if not isinstance(handoff["handoff_id"], str) or not handoff["handoff_id"].strip():
        raise ValueError("handoff_id 必须是非空字符串")
    if not isinstance(handoff["task"], str) or not handoff["task"].strip():
        raise ValueError("handoff task 必须是非空字符串")
    for name in ("context", "constraints", "evidence"):
        if not isinstance(handoff[name], list):
            raise ValueError(f"handoff {name} 必须是数组")

    workspace = handoff["workspace"]
    if not isinstance(workspace, dict) or set(workspace) != HANDOFF_WORKSPACE_FIELDS:
        raise ValueError("handoff workspace schema 无效")
    for name in ("cwd", "project_root"):
        if not isinstance(workspace[name], str) or not workspace[name].strip():
            raise ValueError(f"handoff workspace.{name} 必须是非空字符串")
    _validate_string_list(workspace["relevant_paths"], "workspace.relevant_paths")

    authority = handoff["authority"]
    if not isinstance(authority, dict) or set(authority) != HANDOFF_AUTHORITY_FIELDS:
        raise ValueError("handoff authority schema 无效")
    _validate_string_list(authority["allowed_tools"], "authority.allowed_tools")
    if not all(isinstance(authority[name], bool) for name in (
        "can_write_workspace", "can_use_mcp",
    )):
        raise ValueError("handoff authority boolean 字段无效")
    if not isinstance(authority["max_steps"], int) or isinstance(
        authority["max_steps"], bool
    ) or authority["max_steps"] <= 0:
        raise ValueError("handoff authority.max_steps 必须是正整数")

    contract = handoff["return_contract"]
    if not isinstance(contract, dict) or set(contract) != HANDOFF_RETURN_FIELDS:
        raise ValueError("handoff return_contract schema 无效")
    if not all(isinstance(value, bool) for value in contract.values()):
        raise ValueError("handoff return_contract 字段必须是 boolean")
    if not all(contract.values()):
        raise ValueError("V10 return_contract 的三个字段必须为 true")
    if _contains_secret(handoff):
        raise ValueError("handoff 疑似包含 secret、credential 或 .env.local")
    return handoff


def create_handoff(
    task, context=None, constraints=None, evidence=None, workspace=None,
    authority=None,
):
    """Build and screen a minimal envelope; no Session object is accepted."""
    handoff = {
        "handoff_id": uuid.uuid4().hex,
        "task": task,
        "context": list(context or []),
        "constraints": list(constraints or []),
        "evidence": list(evidence or []),
        "workspace": dict(workspace or {
            "cwd": os.getcwd(),
            "project_root": PROJECT_ROOT,
            "relevant_paths": [],
        }),
        "authority": dict(authority or {
            "allowed_tools": [],
            "can_write_workspace": False,
            "can_use_mcp": False,
            "max_steps": 3,
        }),
        "return_contract": {
            "require_summary": True,
            "require_evidence": True,
            "require_actions": True,
        },
    }
    validate_handoff(handoff)
    # Detach caller-owned nested data so later mutation cannot alter the package.
    return json.loads(json.dumps(handoff, ensure_ascii=False))



def _safe_result(status, summary, evidence, actions):
    result = {
        "status": status,
        "summary": summary,
        "evidence": evidence,
        "actions_taken": actions,
    }
    if _contains_secret(result):
        return {
            "status": "failed",
            "summary": "Subagent 输出因 secret screening 被移除",
            "evidence": [],
            "actions_taken": [],
        }
    return result



