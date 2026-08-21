"""Teaching-grade append-only audit events and deterministic explanations."""

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone

from .security import SECRET_PATTERNS


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIT_DIR = os.path.join(PROJECT_ROOT, ".audit")
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
ACTORS = frozenset({
    "user", "model", "harness", "tool", "mcp", "subagent", "environment",
})


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_run_id():
    return uuid.uuid4().hex


def _sensitive(text):
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def _safe_text(value, limit=240):
    text = " ".join(str(value).split())
    if _sensitive(text):
        return "[REDACTED]"
    return text[:limit] + ("…" if len(text) > limit else "")


def _sanitize(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            safe_key = _safe_text(key, 80)
            result[safe_key] = "[REDACTED]" if _sensitive(str(key)) else _sanitize(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    return _safe_text(value)


def safe_observation_summary(observation):
    """Describe an observation without persisting its raw output or result."""
    if not isinstance(observation, dict):
        raise ValueError("audit observation 必须是对象")
    summary = {}
    for key in ("status", "exit_code", "denied_by"):
        if key in observation:
            summary[key] = _sanitize(observation[key])
    redacted = False
    for key in ("stdout", "stderr", "result", "error"):
        if key not in observation or observation[key] is None:
            continue
        raw = observation[key]
        if not isinstance(raw, str):
            raw = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)
        encoded = raw.encode("utf-8", errors="replace")
        summary[f"{key}_length"] = len(raw)
        summary[f"{key}_sha256"] = hashlib.sha256(encoded).hexdigest()
        redacted = redacted or _sensitive(raw)
    if redacted:
        summary["redacted"] = True
    return summary


class AuditWriter:
    """One append-only, fsync'd JSONL stream for one Run."""

    def __init__(self, session_id, run_id=None, directory=AUDIT_DIR):
        if not isinstance(session_id, str) or not ID_PATTERN.fullmatch(session_id):
            raise ValueError("audit session_id 无效")
        self.session_id = session_id
        self.run_id = run_id or new_run_id()
        if not ID_PATTERN.fullmatch(self.run_id):
            raise ValueError("audit run_id 无效")
        self.directory = directory
        self.path = os.path.join(directory, f"{self.run_id}.jsonl")
        self.sequence = self._last_sequence()

    def _last_sequence(self):
        events = read_events(self.run_id, self.directory, missing_ok=True)
        return events[-1]["sequence"] if events else 0

    def append(self, event_type, actor, subject=None, outcome=None, reason=None,
               references=None, summary=None):
        if actor not in ACTORS:
            raise ValueError("audit actor 无效")
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("audit event_type 无效")
        self.sequence += 1
        event = {
            "version": 1,
            "event_id": uuid.uuid4().hex,
            "timestamp": utc_now(),
            "sequence": self.sequence,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "event_type": event_type,
            "actor": actor,
            "subject": _sanitize(subject),
            "outcome": _sanitize(outcome),
            "reason": _sanitize(reason),
            "references": _sanitize(references or {}),
            "summary": _sanitize(summary),
        }
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        os.makedirs(self.directory, exist_ok=True)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            self.sequence -= 1
            raise
        return event


def read_events(run_id, directory=AUDIT_DIR, missing_ok=False):
    if not isinstance(run_id, str) or not ID_PATTERN.fullmatch(run_id):
        raise ValueError("audit run_id 无效")
    path = os.path.join(directory, f"{run_id}.jsonl")
    events = []
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    break  # A crash may leave only the final line torn.
                if event.get("run_id") != run_id or event.get("sequence") != len(events) + 1:
                    raise ValueError("audit event ordering 无效")
                events.append(event)
    except FileNotFoundError:
        if missing_ok:
            return []
        raise ValueError(f"audit run 不存在：{run_id}")
    return events


def list_runs(directory=AUDIT_DIR):
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return []
    runs = []
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        run_id = name[:-6]
        if not ID_PATTERN.fullmatch(run_id):
            continue
        events = read_events(run_id, directory)
        if not events:
            continue
        final = next((event for event in reversed(events)
                      if event["event_type"] == "run_state_changed"), None)
        runs.append({
            "run_id": run_id,
            "session_id": events[0]["session_id"],
            "started_at": events[0]["timestamp"],
            "status": final.get("outcome") if final else "incomplete",
        })
    return sorted(runs, key=lambda item: item["started_at"], reverse=True)


def format_timeline(events):
    lines = []
    for event in events:
        detail = event.get("outcome") or event.get("subject")
        suffix = f": {detail}" if detail is not None else ""
        lines.append(
            f"{event['sequence']:02d} {event['actor']:<11} "
            f"{event['event_type']}{suffix}"
        )
    return "\n".join(lines)


def explain_events(events):
    """Explain only explicit recorded outcomes and reasons; never infer causes."""
    def runtime_block_after(index):
        for later in events[index + 1:]:
            if later.get("event_type") == "policy_decision":
                break
            references = later.get("references") or {}
            if later.get("event_type") == "governance_limit_reached":
                return later
            if later.get("event_type") == "runtime_gate" and (
                references.get("allowed") is False
                or later.get("outcome") in {"blocked", "BLOCKED", "DENY"}
            ):
                return later
        return None

    lines = []
    for index, event in enumerate(events):
        if event["event_type"] not in {
            "policy_decision", "approval_decided", "action_state_changed",
            "verification_state_changed", "plan_replanned", "retry_decision",
            "governance_limit_reached", "reconciliation_state_changed",
            "run_state_changed", "subagent_return", "memory_decision",
            "runtime_gate",
        }:
            continue
        label = event["event_type"]
        outcome = event.get("outcome") or "未记录结果"
        reason = event.get("reason")
        references = event.get("references") or {}
        identity = next((f"{key}={references[key]}" for key in (
            "action_id", "step_id", "logical_action_id", "handoff_id"
        ) if references.get(key)), "")
        text = f"{event['sequence']:02d} {label} {identity}：{outcome}".replace("  ：", "：")
        trace = references.get("policy_trace")
        if label == "policy_decision" and trace:
            disposition = (
                trace.get("disposition")
                if isinstance(trace, dict) else None
            )
            if not isinstance(disposition, dict):
                disposition = trace
            required = {
                "global", "zone", "profile", "delegated", "effective",
                "limiting_factor",
            }
            layers = ("global", "zone", "profile", "delegated")
            factors = ("global", "zone", "profile", "delegated", "delegation")
            valid = (
                isinstance(disposition, dict)
                and required.issubset(disposition)
                and all(disposition.get(name) in {"ALLOW", "ASK", "DENY"}
                        for name in (*layers, "effective"))
                and disposition.get("limiting_factor") in factors
            )
            if valid and disposition["global"] == "DENY":
                valid = (
                    disposition["effective"] == "DENY"
                    and disposition["limiting_factor"] == "global"
                )
            if reason:
                text += f"；classification：{reason}"
            if not valid:
                text += "；composition：证据不足（policy trace 不完整或无效）"
            else:
                factor = disposition["limiting_factor"]
                text += (
                    "；composition："
                    f"global={disposition['global']}, "
                    f"zone={disposition['zone']}, "
                    f"profile={disposition['profile']}, "
                    f"delegated={disposition['delegated']}；"
                    f"Effective Policy={disposition['effective']}；"
                    f"limiting_factor={factor}"
                )
                capability_explained = False
                write = trace.get("write")
                if (
                    outcome in {"DENY", "blocked"}
                    and isinstance(write, dict)
                    and write.get("effective") is False
                    and write.get("limiting_factor") == "delegation"
                    and write.get("profile") is True
                    and write.get("delegated") is False
                ):
                    text += (
                        "；capability：workspace-editor Profile 本身允许 "
                        "workspace write，但 Delegated Authority 将 "
                        "can_write_workspace 收紧为 false；composition "
                        "只能取交集，最终 write=false"
                    )
                    capability_explained = True
                mcp = trace.get("mcp")
                if (
                    not capability_explained
                    and outcome in {"DENY", "blocked"}
                    and isinstance(mcp, dict)
                    and mcp.get("effective") is False
                    and mcp.get("limiting_factor") == "delegation"
                    and mcp.get("profile") is True
                    and mcp.get("delegated") is False
                ):
                    text += (
                        "；capability：Profile 允许 MCP，但 Delegated "
                        "Authority 将 can_use_mcp 收紧为 false；最终 "
                        "mcp=false"
                    )
                    capability_explained = True
                runtime_block = runtime_block_after(index)
                if runtime_block is not None:
                    block_reason = runtime_block.get("reason") or (
                        runtime_block.get("references") or {}
                    ).get("reason") or runtime_block.get("outcome")
                    text += (
                        f"；runtime gate：{block_reason}；"
                        "FINAL AUTHORIZATION: BLOCKED；action 不会执行，"
                        "也不会进入 Human Approval"
                    )
                elif capability_explained or outcome in {"DENY", "blocked"}:
                    text += (
                        "；FINAL AUTHORIZATION: DENY；action 在 Approval "
                        "之前被 Harness DENY，不会进入 Human Approval"
                    )
                elif disposition["effective"] == "DENY":
                    text += (
                        "；FINAL AUTHORIZATION: DENY；action 被 policy 拒绝"
                    )
                elif disposition["effective"] == "ASK":
                    text += (
                        "；FINAL AUTHORIZATION: ASK；因此需要 Human Approval"
                    )
                else:
                    text += "；FINAL AUTHORIZATION: ALLOW"
        elif reason:
            text += f"；原因：{reason}"
        lines.append(text)
    return "\n".join(lines) if lines else "Audit 中没有记录可解释的显式原因。"
