"""Small, deterministic Plan state for teaching explicit task execution."""

import copy
import re
import uuid

from .security import SECRET_PATTERNS as _SECRET_PATTERNS


MAX_PLAN_STEPS = 8
MAX_REPLANS = 3
MAX_PLAN_TEXT = 300
MAX_EVIDENCE_PER_STEP = 8

PLAN_STATUSES = frozenset({"active", "completed", "blocked", "failed"})
STEP_STATUSES = frozenset({
    "pending", "in_progress", "completed", "blocked", "failed",
})
STEP_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

PLAN_FIELDS = {
    "plan_id", "version", "goal", "status", "replan_count", "steps",
}
STEP_FIELDS = {
    "id", "description", "status", "depends_on", "evidence",
}


def _validate_text(value, name):
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > MAX_PLAN_TEXT
    ):
        raise ValueError(f"{name} 必须是整洁的非空短文本")
    if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        raise ValueError(f"{name} 疑似包含 secret 或 credential")


def _contains_secret(value):
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


def _validate_evidence(evidence):
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE_PER_STEP:
        raise ValueError("step evidence 必须是有限数组")
    for item in evidence:
        if not isinstance(item, dict) or not item:
            raise ValueError("step evidence item 必须是非空对象")
        if _contains_secret(item):
            raise ValueError("step evidence 疑似包含 secret 或 credential")
        try:
            encoded_length = len(repr(item))
        except Exception as error:
            raise ValueError("step evidence 无法安全表示") from error
        if encoded_length > MAX_PLAN_TEXT * 2:
            raise ValueError("step evidence item 过大")


def _validate_dependencies(steps):
    ids = {step["id"] for step in steps}
    graph = {step["id"]: step["depends_on"] for step in steps}
    for step_id, dependencies in graph.items():
        if len(dependencies) != len(set(dependencies)):
            raise ValueError(f"step {step_id} 的 depends_on 包含重复项")
        unknown = [dependency for dependency in dependencies if dependency not in ids]
        if unknown:
            raise ValueError(f"step {step_id} 引用了不存在的 dependency")

    visiting = set()
    visited = set()

    def visit(step_id):
        if step_id in visiting:
            raise ValueError("plan dependency graph 存在循环")
        if step_id in visited:
            return
        visiting.add(step_id)
        for dependency in graph[step_id]:
            visit(dependency)
        visiting.remove(step_id)
        visited.add(step_id)

    for step_id in graph:
        visit(step_id)


def validate_plan(plan):
    """Validate one persisted Plan snapshot without granting it authority."""
    if not isinstance(plan, dict) or set(plan) != PLAN_FIELDS:
        raise ValueError("plan schema 无效")
    _validate_text(plan["plan_id"], "plan_id")
    _validate_text(plan["goal"], "goal")
    if plan["status"] not in PLAN_STATUSES:
        raise ValueError("plan status 无效")
    if not isinstance(plan["version"], int) or isinstance(plan["version"], bool) or plan["version"] < 1:
        raise ValueError("plan version 无效")
    if (
        not isinstance(plan["replan_count"], int)
        or isinstance(plan["replan_count"], bool)
        or not 0 <= plan["replan_count"] <= MAX_REPLANS
    ):
        raise ValueError("plan replan_count 无效")
    steps = plan["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_PLAN_STEPS:
        raise ValueError(f"plan steps 必须包含 1 到 {MAX_PLAN_STEPS} 项")

    seen = set()
    in_progress = 0
    for step in steps:
        if not isinstance(step, dict) or set(step) != STEP_FIELDS:
            raise ValueError("plan step schema 无效")
        step_id = step["id"]
        if not isinstance(step_id, str) or not STEP_ID_PATTERN.fullmatch(step_id):
            raise ValueError("step id 无效")
        if step_id in seen:
            raise ValueError("step id 必须唯一")
        seen.add(step_id)
        _validate_text(step["description"], "step description")
        if step["status"] not in STEP_STATUSES:
            raise ValueError("step status 无效")
        in_progress += step["status"] == "in_progress"
        if not isinstance(step["depends_on"], list) or not all(
            isinstance(dependency, str) for dependency in step["depends_on"]
        ):
            raise ValueError("step depends_on 必须是 step id 数组")
        _validate_evidence(step["evidence"])
    if in_progress > 1:
        raise ValueError("每次只允许一个 in_progress step")
    _validate_dependencies(steps)
    return plan


def validate_revision_history(history):
    if not isinstance(history, list) or len(history) > MAX_REPLANS:
        raise ValueError("plan revision history 无效")
    previous_version = 0
    for revision in history:
        if not isinstance(revision, dict) or set(revision) != {
            "version", "reason", "plan",
        }:
            raise ValueError("plan revision schema 无效")
        _validate_text(revision["reason"], "plan revision reason")
        validate_plan(revision["plan"])
        if (
            revision["version"] != revision["plan"]["version"]
            or revision["version"] <= previous_version
        ):
            raise ValueError("plan revision version 无效")
        previous_version = revision["version"]
    return history


def _candidate_steps(steps):
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_PLAN_STEPS:
        raise ValueError(f"plan candidate 最多允许 {MAX_PLAN_STEPS} 个 steps")
    normalized = []
    for step in steps:
        if not isinstance(step, dict) or set(step) != {
            "id", "description", "depends_on",
        }:
            raise ValueError("plan candidate step schema 无效")
        normalized.append({
            "id": step["id"],
            "description": step["description"],
            "status": "pending",
            "depends_on": list(step["depends_on"])
            if isinstance(step["depends_on"], list) else step["depends_on"],
            "evidence": [],
        })
    return normalized


def create_plan(goal, steps, plan_id=None):
    """Turn a model-proposed goal and step list into Harness-owned state."""
    plan = {
        "plan_id": plan_id or uuid.uuid4().hex,
        "version": 1,
        "goal": goal,
        "status": "active",
        "replan_count": 0,
        "steps": _candidate_steps(steps),
    }
    validate_plan(plan)
    return plan


def select_ready_step(plan):
    validate_plan(plan)
    completed = {
        step["id"] for step in plan["steps"] if step["status"] == "completed"
    }
    return next((
        step for step in plan["steps"]
        if step["status"] == "pending"
        and set(step["depends_on"]).issubset(completed)
    ), None)


def _step(plan, step_id):
    for step in plan["steps"]:
        if step["id"] == step_id:
            return step
    raise ValueError(f"plan step 不存在：{step_id}")


def start_step(plan, step_id=None):
    """Start exactly the selected ready step and return a new Plan snapshot."""
    validate_plan(plan)
    if plan["status"] != "active":
        raise ValueError("只有 active plan 可以开始 step")
    if any(step["status"] == "in_progress" for step in plan["steps"]):
        raise ValueError("已有 in_progress step")
    ready = select_ready_step(plan)
    if ready is None or (step_id is not None and ready["id"] != step_id):
        raise ValueError("step 尚未 ready")
    updated = copy.deepcopy(plan)
    _step(updated, ready["id"])["status"] = "in_progress"
    return updated


def propose_step_completion(plan, step_id, result):
    """Validate a model proposal without changing Plan state."""
    validate_plan(plan)
    if _step(plan, step_id)["status"] != "in_progress":
        raise ValueError("只有 in_progress step 可以提议完成")
    _validate_text(result, "step completion result")
    return {"step_id": step_id, "result": result}


def complete_step(plan, step_id, accepted_evidence):
    """Complete a step only after the caller supplies accepted evidence."""
    validate_plan(plan)
    if _step(plan, step_id)["status"] != "in_progress":
        raise ValueError("只有 in_progress step 可以完成")
    _validate_evidence(accepted_evidence)
    if not accepted_evidence:
        raise ValueError("step completion 缺少 accepted evidence")
    updated = copy.deepcopy(plan)
    step = _step(updated, step_id)
    step["evidence"].extend(copy.deepcopy(accepted_evidence))
    if len(step["evidence"]) > MAX_EVIDENCE_PER_STEP:
        raise ValueError("step evidence 超过限制")
    step["status"] = "completed"
    if all(item["status"] == "completed" for item in updated["steps"]):
        updated["status"] = "completed"
    validate_plan(updated)
    return updated


def _finish_step(plan, step_id, status):
    validate_plan(plan)
    if _step(plan, step_id)["status"] != "in_progress":
        raise ValueError("只有 in_progress step 可以结束")
    updated = copy.deepcopy(plan)
    _step(updated, step_id)["status"] = status
    updated["status"] = "blocked" if status == "blocked" else "failed"
    return updated


def block_step(plan, step_id):
    return _finish_step(plan, step_id, "blocked")


def fail_step(plan, step_id):
    return _finish_step(plan, step_id, "failed")


def retry_exhausted_outcome(plan, step_id):
    """Conservative V15 boundary: request replan, or block at its local limit."""
    validate_plan(plan)
    if _step(plan, step_id)["status"] != "in_progress":
        raise ValueError("只有 in_progress step 可以处理 retry exhausted")
    if plan["replan_count"] < MAX_REPLANS:
        return {"action": "replan", "plan": copy.deepcopy(plan)}
    return {"action": "block", "plan": block_step(plan, step_id)}


def revise_plan(plan, steps, reason, revision_history=None):
    """Create version N+1 and append the immutable old snapshot to history."""
    validate_plan(plan)
    if plan["replan_count"] >= MAX_REPLANS:
        raise ValueError("plan 已达到 replan limit")
    _validate_text(reason, "plan revision reason")
    candidate = _candidate_steps(steps)
    candidate_by_id = {step["id"]: step for step in candidate}
    for old_step in plan["steps"]:
        if old_step["status"] != "completed":
            continue
        replacement = candidate_by_id.get(old_step["id"])
        if replacement is None or any(
            replacement[key] != old_step[key]
            for key in ("description", "depends_on")
        ):
            raise ValueError("plan revision 不能删除或改写 completed step")
        replacement["status"] = "completed"
        replacement["evidence"] = copy.deepcopy(old_step["evidence"])

    revised = {
        "plan_id": plan["plan_id"],
        "version": plan["version"] + 1,
        "goal": plan["goal"],
        "status": "active",
        "replan_count": plan["replan_count"] + 1,
        "steps": candidate,
    }
    validate_plan(revised)
    history = copy.deepcopy(revision_history or [])
    validate_revision_history(history)
    history.append({
        "version": plan["version"],
        "reason": reason,
        "plan": copy.deepcopy(plan),
    })
    return revised, history


def subagent_result_evidence(result):
    """Convert a structured return into an evidence candidate, never Plan state."""
    if not isinstance(result, dict):
        raise ValueError("Subagent result 必须是对象")
    status = result.get("status")
    summary = result.get("summary")
    if status not in {"completed", "blocked", "failed"}:
        raise ValueError("Subagent result status 无效")
    _validate_text(summary, "Subagent result summary")
    evidence = {
        "kind": "subagent_result",
        "status": status,
        "summary": summary,
    }
    _validate_evidence([evidence])
    return evidence
