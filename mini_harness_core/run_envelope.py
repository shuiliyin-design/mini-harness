"""V21 historical input identity and deterministic Harness replay.

Policy Snapshot = Historical Authority Definition
Run Manifest = Historical Runtime Configuration Identity
Run Envelope = Historical Execution Input Identity
Audit = Historical Event Trace

This module never calls a provider, tool, MCP server, subagent, or approval UI.
"""

import copy
import hashlib
import json
import os
import re
import tempfile

from .audit import ID_PATTERN, utc_now
from .context import measure_context
from .planning import select_ready_step
from .policy_snapshot import (
    compose_from_snapshot, load_policy_snapshot, policy_fingerprint,
)
from .retry import decide_retry
from .run_manifest import (
    FINGERPRINT_PATTERN, RunManifestStore, canonical_json, integrity_check,
)
from .security import SECRET_PATTERNS
from .verification import replay_verification_transition


ENVELOPE_SCHEMA_VERSION = 1
TRANSITION_TYPES = frozenset({
    "policy", "planning", "retry", "verification", "governance",
})
FORBIDDEN_KEYS = re.compile(
    r"(?:api[_-]?key|authorization|bearer|password|private[_-]?key|"
    r"task_text|messages|message_text|raw[_-]?(?:environment|headers)|"
    r"hidden[_-]?reasoning|agents[_-]?(?:body|content)|"
    r"skill[_-]?(?:body|content)|memory[_-]?content|mcp[_-]?description)", re.I,
)


class RunEnvelopeError(ValueError):
    """An envelope is unsafe, corrupt, unavailable, or unsupported."""


def digest_json(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def digest_text(value):
    if not isinstance(value, str):
        raise RunEnvelopeError("text identity 必须是字符串")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def envelope_fingerprint(inputs):
    """Hash stable initial input identity only."""
    return digest_json(inputs)


def _identity(value):
    return {"sha256": digest_json(value)}


def build_envelope(run_id, session_id, task, source_messages, manifest,
                   current_plan=None, control_state=None, created_at=None):
    if not isinstance(source_messages, list):
        raise RunEnvelopeError("session source messages 无效")
    configuration = manifest["configuration"]
    task_index = len(source_messages)
    inputs = {
        "task": {
            "task_message_ref": {
                "session_id": session_id, "message_index": task_index,
            },
            "task_length": len(task.encode("utf-8")),
            "task_sha256": digest_text(task),
        },
        "session": {
            "source_message_count": len(source_messages),
            "source_history_sha256": digest_json(source_messages),
        },
        "manifest_fingerprint": manifest["configuration_fingerprint"],
        "policy_fingerprint": configuration["policy"]["policy_fingerprint"],
        "project_context": _identity(configuration["project_context"]),
        "memory_selection": _identity(configuration["memory"]),
        "capability_catalog": _identity(configuration["capabilities"]),
        "plan_input": _identity(current_plan),
        "control_state": _identity(control_state),
    }
    envelope = {
        "envelope_schema_version": ENVELOPE_SCHEMA_VERSION,
        "run_id": run_id,
        "session_id": session_id,
        "created_at": created_at or utc_now(),
        "envelope_fingerprint": envelope_fingerprint(inputs),
        "inputs": inputs,
        "requests": [],
        "transitions": [],
    }
    validate_envelope(envelope)
    return envelope


def _contains_forbidden_key(value):
    if isinstance(value, dict):
        return any(
            (not str(key).endswith(("_sha256", "_fingerprint"))
             and FORBIDDEN_KEYS.search(str(key)))
            or _contains_forbidden_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def _contains_secret(value):
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in SECRET_PATTERNS)
    if isinstance(value, dict):
        return any(_contains_secret(key) or _contains_secret(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    return False


def planning_transition_input(plan):
    """Keep only the plan identity and fields used by ready-step selection."""
    return {
        "plan_sha256": digest_json(plan),
        "plan_id": plan["plan_id"], "version": plan["version"],
        "status": plan["status"], "replan_count": plan["replan_count"],
        "steps": [{
            "id": step["id"], "status": step["status"],
            "depends_on": list(step["depends_on"]),
        } for step in plan["steps"]],
    }


def _valid_digest(value):
    return isinstance(value, str) and FINGERPRINT_PATTERN.fullmatch(value)


def validate_request(record):
    fields = {
        "request_id", "sequence", "provider_request_sequence",
        "prepared_messages_sha256", "message_count", "characters",
        "approx_tokens", "compaction_applied", "model_decision_event_id",
        "decision_sha256", "decision_type",
    }
    if not isinstance(record, dict) or set(record) != fields:
        raise RunEnvelopeError("request identity schema 无效")
    if not isinstance(record["request_id"], str) or not record["request_id"]:
        raise RunEnvelopeError("request_id 无效")
    if record["sequence"] != record["provider_request_sequence"]:
        raise RunEnvelopeError("provider request sequence 无效")
    if not isinstance(record["sequence"], int) or isinstance(record["sequence"], bool) or record["sequence"] < 1:
        raise RunEnvelopeError("request sequence 无效")
    if not _valid_digest(record["prepared_messages_sha256"]):
        raise RunEnvelopeError("prepared messages digest 无效")
    for key in ("message_count", "characters", "approx_tokens"):
        if not isinstance(record[key], int) or isinstance(record[key], bool) or record[key] < 0:
            raise RunEnvelopeError(f"request {key} 无效")
    if not isinstance(record["compaction_applied"], bool):
        raise RunEnvelopeError("request compaction identity 无效")
    optional = ("model_decision_event_id", "decision_type")
    if any(record[key] is not None and not isinstance(record[key], str) for key in optional):
        raise RunEnvelopeError("model decision reference 无效")
    if record["decision_sha256"] is not None and not _valid_digest(record["decision_sha256"]):
        raise RunEnvelopeError("model decision digest 无效")
    bound = record["decision_sha256"] is not None
    if bound != (record["decision_type"] is not None):
        raise RunEnvelopeError("model decision binding 不完整")
    return record


def validate_transition(record):
    if not isinstance(record, dict) or set(record) != {
        "transition_id", "sequence", "transition_type", "input",
        "recorded_output",
    }:
        raise RunEnvelopeError("transition schema 无效")
    if not isinstance(record["transition_id"], str) or not record["transition_id"]:
        raise RunEnvelopeError("transition_id 无效")
    if record["transition_type"] not in TRANSITION_TYPES:
        raise RunEnvelopeError("transition type 无效")
    if not isinstance(record["sequence"], int) or isinstance(record["sequence"], bool) or record["sequence"] < 1:
        raise RunEnvelopeError("transition sequence 无效")
    if not isinstance(record["input"], dict) or not isinstance(record["recorded_output"], dict):
        raise RunEnvelopeError("transition input/output 无效")
    return record


def validate_envelope(envelope, verify_fingerprint=True):
    if not isinstance(envelope, dict) or set(envelope) != {
        "envelope_schema_version", "run_id", "session_id", "created_at",
        "envelope_fingerprint", "inputs", "requests", "transitions",
    }:
        raise RunEnvelopeError("envelope schema 无效")
    if envelope["envelope_schema_version"] != ENVELOPE_SCHEMA_VERSION:
        raise RunEnvelopeError("unsupported historical envelope schema")
    for key in ("run_id", "session_id"):
        if not ID_PATTERN.fullmatch(str(envelope[key])):
            raise RunEnvelopeError(f"envelope {key} 无效")
    if not isinstance(envelope["created_at"], str) or not envelope["created_at"]:
        raise RunEnvelopeError("envelope created_at 无效")
    inputs = envelope["inputs"]
    required = {
        "task", "session", "manifest_fingerprint", "policy_fingerprint",
        "project_context", "memory_selection", "capability_catalog",
        "plan_input", "control_state",
    }
    if not isinstance(inputs, dict) or set(inputs) != required:
        raise RunEnvelopeError("envelope inputs schema 无效")
    if _contains_forbidden_key(envelope):
        raise RunEnvelopeError("envelope forbidden secret-bearing field")
    if _contains_secret(envelope):
        raise RunEnvelopeError("envelope secret screening rejected")
    for key in ("manifest_fingerprint", "policy_fingerprint"):
        if not _valid_digest(inputs[key]):
            raise RunEnvelopeError(f"envelope {key} 无效")
    task = inputs["task"]
    session = inputs["session"]
    if set(task) != {"task_message_ref", "task_length", "task_sha256"} or not _valid_digest(task["task_sha256"]):
        raise RunEnvelopeError("task identity 无效")
    if set(task["task_message_ref"]) != {"session_id", "message_index"}:
        raise RunEnvelopeError("task message reference 无效")
    if task["task_message_ref"]["session_id"] != envelope["session_id"]:
        raise RunEnvelopeError("task session reference mismatch")
    if set(session) != {"source_message_count", "source_history_sha256"} or not _valid_digest(session["source_history_sha256"]):
        raise RunEnvelopeError("session identity 无效")
    for value in (task["task_length"], task["task_message_ref"]["message_index"], session["source_message_count"]):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RunEnvelopeError("input count 无效")
    if task["task_message_ref"]["message_index"] != session["source_message_count"]:
        raise RunEnvelopeError("task message index mismatch")
    for key in ("project_context", "memory_selection", "capability_catalog", "plan_input", "control_state"):
        if not isinstance(inputs[key], dict) or set(inputs[key]) != {"sha256"} or not _valid_digest(inputs[key]["sha256"]):
            raise RunEnvelopeError(f"{key} identity 无效")
    if not _valid_digest(envelope["envelope_fingerprint"]):
        raise RunEnvelopeError("envelope fingerprint 无效")
    if verify_fingerprint and envelope_fingerprint(inputs) != envelope["envelope_fingerprint"]:
        raise RunEnvelopeError("envelope fingerprint mismatch")
    if not isinstance(envelope["requests"], list) or not isinstance(envelope["transitions"], list):
        raise RunEnvelopeError("envelope records 无效")
    for index, request in enumerate(envelope["requests"], 1):
        validate_request(request)
        if request["sequence"] != index:
            raise RunEnvelopeError("request sequence 不连续")
    for index, transition in enumerate(envelope["transitions"], 1):
        validate_transition(transition)
        if transition["sequence"] != index:
            raise RunEnvelopeError("transition sequence 不连续")
    return envelope


class RunEnvelopeStore:
    def __init__(self, directory):
        self.directory = directory

    def _path(self, run_id):
        if not isinstance(run_id, str) or not ID_PATTERN.fullmatch(run_id):
            raise RunEnvelopeError("envelope run_id 无效")
        return os.path.join(self.directory, f"{run_id}.json")

    def _write(self, envelope):
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".envelope-", suffix=".tmp", dir=self.directory)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(canonical_json(envelope) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path(envelope["run_id"]))
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def persist(self, envelope):
        validate_envelope(envelope)
        path = self._path(envelope["run_id"])
        if os.path.exists(path):
            existing = self.load(envelope["run_id"])
            if canonical_json(existing) != canonical_json(envelope):
                raise RunEnvelopeError("run envelope immutable conflict")
            return path
        self._write(envelope)
        return path

    def load(self, run_id, verify=True):
        try:
            with open(self._path(run_id), encoding="utf-8") as stream:
                envelope = json.load(stream)
        except FileNotFoundError as error:
            raise RunEnvelopeError(f"run envelope 不存在：{run_id}") from error
        except (OSError, json.JSONDecodeError) as error:
            raise RunEnvelopeError("run envelope corruption") from error
        return validate_envelope(envelope, verify)

    def _update(self, run_id, update):
        envelope = self.load(run_id)
        initial = envelope["envelope_fingerprint"]
        updated = copy.deepcopy(envelope)
        update(updated)
        validate_envelope(updated)
        if updated["envelope_fingerprint"] != initial:
            raise RunEnvelopeError("envelope initial inputs are immutable")
        self._write(updated)
        return updated

    def append_request(self, run_id, prepared_messages, compaction_applied=False):
        stats = measure_context(prepared_messages)
        result = {}
        def update(envelope):
            sequence = len(envelope["requests"]) + 1
            record = {
                "request_id": f"request-{sequence}", "sequence": sequence,
                "provider_request_sequence": sequence,
                "prepared_messages_sha256": digest_json(prepared_messages),
                "message_count": stats["message_count"],
                "characters": stats["total_characters"],
                "approx_tokens": stats["approximate_tokens"],
                "compaction_applied": bool(compaction_applied),
                "model_decision_event_id": None, "decision_sha256": None,
                "decision_type": None,
            }
            envelope["requests"].append(record)
            result.update(record)
        self._update(run_id, update)
        return result

    def bind_decision(self, run_id, request_id, decision, event_id=None):
        def update(envelope):
            matches = [item for item in envelope["requests"] if item["request_id"] == request_id]
            if len(matches) != 1:
                raise RunEnvelopeError("request decision binding target 不存在")
            record = matches[0]
            decision_sha256 = digest_json(decision)
            decision_type = decision.get("type") if isinstance(decision, dict) else type(decision).__name__
            if record["decision_sha256"] is not None:
                if (record["decision_sha256"] != decision_sha256
                        or record["decision_type"] != decision_type
                        or (record["model_decision_event_id"] is not None
                            and record["model_decision_event_id"] != event_id)):
                    raise RunEnvelopeError("model decision binding immutable conflict")
                if event_id is not None:
                    record["model_decision_event_id"] = event_id
                return
            record.update({
                "model_decision_event_id": event_id,
                "decision_sha256": decision_sha256,
                "decision_type": decision_type,
            })
        return self._update(run_id, update)

    def append_transition(self, run_id, transition_type, inputs, recorded_output):
        result = {}
        def update(envelope):
            sequence = len(envelope["transitions"]) + 1
            record = {
                "transition_id": f"transition-{sequence}",
                "sequence": sequence, "transition_type": transition_type,
                "input": copy.deepcopy(inputs),
                "recorded_output": copy.deepcopy(recorded_output),
            }
            validate_transition(record)
            envelope["transitions"].append(record)
            result.update(record)
        self._update(run_id, update)
        return result


def envelope_integrity_check(envelope, audit_directory):
    try:
        validate_envelope(envelope)
        manifest = RunManifestStore(os.path.join(audit_directory, "manifests")).load(envelope["run_id"])
        if manifest["configuration_fingerprint"] != envelope["inputs"]["manifest_fingerprint"]:
            return False
        if manifest["configuration"]["policy"]["policy_fingerprint"] != envelope["inputs"]["policy_fingerprint"]:
            return False
        return integrity_check(manifest, os.path.join(audit_directory, "policies"))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _replay_transition(transition, snapshot):
    kind, inputs = transition["transition_type"], transition["input"]
    try:
        if kind == "policy":
            if inputs.get("policy_fingerprint") != policy_fingerprint(snapshot):
                return "UNAVAILABLE", None
            output = {"decision": compose_from_snapshot(snapshot, inputs["composition_inputs"])}
        elif kind == "planning":
            plan = {
                "plan_id": inputs["plan_id"], "version": inputs["version"],
                "goal": "historical-plan", "status": inputs["status"],
                "replan_count": inputs["replan_count"],
                "steps": [{
                    "id": step["id"], "description": "historical-step",
                    "status": step["status"],
                    "depends_on": list(step["depends_on"]), "evidence": [],
                } for step in inputs["steps"]],
            }
            ready = select_ready_step(plan)
            output = {"selected_step_id": ready["id"] if ready else None}
        elif kind == "retry":
            output = {"decision": decide_retry(
                inputs["failure_class"], inputs["effect"], inputs["replay_policy"],
                inputs["attempt_count"], inputs["max_attempts"], inputs["run_state"],
            )}
            if "next_delay" in transition["recorded_output"]:
                attempt = inputs["attempt_count"]
                output["next_delay"] = (
                    1.0 * (2 ** max(0, attempt - 1))
                    if output["decision"] == "retry_with_backoff" else 0
                )
        elif kind == "verification":
            output = replay_verification_transition(inputs)
        else:
            # Governance records require a named pure helper contract.
            return "UNAVAILABLE", None
        return ("MATCH" if output == transition["recorded_output"] else "MISMATCH"), output
    except (KeyError, TypeError, ValueError):
        return "UNAVAILABLE", None


def harness_replay_check(envelope, audit_directory):
    if not envelope_integrity_check(envelope, audit_directory):
        return {"identity": "MISMATCH", "transitions": [], "match": False}
    try:
        snapshot = load_policy_snapshot(
            envelope["inputs"]["policy_fingerprint"],
            os.path.join(audit_directory, "policies"),
        )
    except ValueError:
        return {"identity": "MATCH", "transitions": [], "match": False}
    results = []
    for transition in envelope["transitions"]:
        status, replayed = _replay_transition(transition, snapshot)
        results.append({
            "transition_id": transition["transition_id"],
            "sequence": transition["sequence"],
            "transition_type": transition["transition_type"],
            "status": status, "replayed_output": replayed,
        })
    return {
        "identity": "MATCH", "transitions": results,
        "match": all(item["status"] == "MATCH" for item in results),
    }
