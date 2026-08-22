"""Repair Result publication after an applied reconciliation, without execution."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import uuid

from .inspector import (
    COMPLETED,
    EFFECT_APPLIED_NEEDS_RESULT_REPAIR,
    inspect_bridge_task,
)
from .paths import BridgePathReader, valid_task_id
from .publisher import _json_bytes, _publish_file, _screen_payload


RESULT_REPAIRED = "RESULT_REPAIRED"
RESULT_READY_REPAIRED = "RESULT_READY_REPAIRED"
ALREADY_COMPLETED = "ALREADY_COMPLETED"
RESULT_CONFLICT = "RESULT_CONFLICT"
REPAIR_NOT_ALLOWED = "REPAIR_NOT_ALLOWED"
INTEGRATION_REPAIR_REQUIRED = "INTEGRATION_REPAIR_REQUIRED"

_SEMANTIC_FIELDS = (
    "result_schema_version", "task_id", "claim_nonce", "consumer_id",
    "status", "result", "artifact_refs",
)
_ALLOWED_FIELDS = frozenset(_SEMANTIC_FIELDS) | {
    "completion_source", "completed_at",
}


@dataclass(frozen=True, slots=True)
class BridgeResultRepair:
    task_id: str
    claim_nonce: str
    status: str
    result_path: str
    ready_path: str
    protocol_state: str


def _outcome(task_id, claim_nonce, status, result_path, ready_path, state):
    return BridgeResultRepair(
        task_id, claim_nonce, status, result_path, ready_path, state,
    )


def _read_partial(reader, path):
    try:
        value = reader.read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _task_type(reader, task_id):
    try:
        task = reader.read_json(reader.path("inbox", task_id + ".json"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    if (
        not isinstance(task, dict)
        or task.get("task_schema_version") != 1
        or task.get("task_id") != task_id
    ):
        return None
    return task.get("task_type")


def _partial_matches(partial, expected):
    if partial is None or not set(partial).issubset(_ALLOWED_FIELDS):
        return False
    if any(partial.get(field) != expected[field] for field in _SEMANTIC_FIELDS):
        return False
    source = partial.get("completion_source")
    if source not in (None, "reconciliation_repair"):
        return False
    return isinstance(partial.get("completed_at"), str) and bool(partial["completed_at"])


def repair_bridge_result(
    bridge_root, task_id, claim_nonce, consumer_id, result_payload,
    artifact_refs=None,
):
    """Commit an already-proven effect as Result; never execute the task."""
    if not valid_task_id(task_id):
        raise ValueError("task_id is unsafe or invalid")
    if not valid_task_id(claim_nonce):
        raise ValueError("claim_nonce is unsafe or invalid")
    if not isinstance(consumer_id, str) or not consumer_id:
        raise ValueError("consumer_id must be a non-empty string")
    if artifact_refs is None:
        artifact_refs = []
    if not isinstance(result_payload, dict):
        raise ValueError("result_payload must be a JSON object")
    if not isinstance(artifact_refs, list):
        raise ValueError("artifact_refs must be a list")
    _screen_payload(result_payload, "result_payload")
    _screen_payload(artifact_refs, "artifact_refs")
    # Round-trip once to reject non-JSON values and normalize tuples to arrays.
    result_payload = json.loads(_json_bytes(result_payload))
    artifact_refs = json.loads(_json_bytes(artifact_refs))

    reader = BridgePathReader(bridge_root)
    result_path = reader.path("outbox", "result-" + task_id + ".json")
    ready_path = reader.path("outbox", "result-" + task_id + ".ready")
    state = inspect_bridge_task(reader.root, task_id)

    # Caller-provided repair payload is valid only for the bridge-native
    # teaching executor.  Integration results belong to Harness and must be
    # projected from its durable terminal Result.
    task_type = _task_type(reader, task_id)
    if task_type == "bridge_harness_task":
        return _outcome(
            task_id, claim_nonce, INTEGRATION_REPAIR_REQUIRED,
            result_path, ready_path, state.state,
        )
    if task_type != "bridge_test":
        return _outcome(
            task_id, claim_nonce, REPAIR_NOT_ALLOWED,
            result_path, ready_path, state.state,
        )

    if (
        state.latest_claim_nonce != claim_nonce
        or state.consumer_id != consumer_id
    ):
        return _outcome(
            task_id, claim_nonce, REPAIR_NOT_ALLOWED,
            result_path, ready_path, state.state,
        )
    if state.state == COMPLETED:
        return _outcome(
            task_id, claim_nonce, ALREADY_COMPLETED,
            result_path, ready_path, state.state,
        )
    if (
        state.state != EFFECT_APPLIED_NEEDS_RESULT_REPAIR
        or state.reconciliation != "applied"
    ):
        return _outcome(
            task_id, claim_nonce, REPAIR_NOT_ALLOWED,
            result_path, ready_path, state.state,
        )
    if reader.exists(ready_path):
        return _outcome(
            task_id, claim_nonce, ALREADY_COMPLETED,
            result_path, ready_path, state.state,
        )

    expected = {
        "result_schema_version": 1,
        "task_id": task_id,
        "claim_nonce": claim_nonce,
        "consumer_id": consumer_id,
        "status": "completed",
        "result": result_payload,
        "artifact_refs": artifact_refs,
        "completion_source": "reconciliation_repair",
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    result_exists = reader.exists(result_path)
    if result_exists:
        if not _partial_matches(_read_partial(reader, result_path), expected):
            return _outcome(
                task_id, claim_nonce, RESULT_CONFLICT,
                result_path, ready_path, state.state,
            )
        status = RESULT_READY_REPAIRED
    else:
        outbox = reader.require_directory(reader.path("outbox"))
        try:
            _publish_file(
                outbox, result_path, _json_bytes(expected), uuid.uuid4().hex,
            )
        except FileExistsError:
            if not _partial_matches(_read_partial(reader, result_path), expected):
                return _outcome(
                    task_id, claim_nonce, RESULT_CONFLICT,
                    result_path, ready_path, state.state,
                )
        status = RESULT_REPAIRED

    outbox = reader.require_directory(reader.path("outbox"))
    try:
        _publish_file(outbox, ready_path, b"", uuid.uuid4().hex)
    except FileExistsError:
        status = ALREADY_COMPLETED
    current = inspect_bridge_task(reader.root, task_id)
    return _outcome(
        task_id, claim_nonce, status, result_path, ready_path, current.state,
    )
