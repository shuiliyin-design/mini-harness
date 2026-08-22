"""Execute the tiny, side-effect-free Bridge v1 teaching task allowlist."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import uuid

from .bridge_attempt_fence import (
    ATTEMPT_FENCE_LOCKED, acquire_bridge_attempt_fence,
)
from .bridge_inspector import (
    CLAIMED_BY_SELF_UNKNOWN,
    COMPLETED,
    INVALID_HISTORY,
    inspect_bridge_task,
)
from .bridge_paths import BridgePathReader, valid_task_id
from .bridge_publisher import _json_bytes, _publish_file, _screen_payload


EXECUTED = "EXECUTED"
ALREADY_COMPLETED = "ALREADY_COMPLETED"
RESULT_PUBLISH_INCOMPLETE = "RESULT_PUBLISH_INCOMPLETE"
EXECUTION_NOT_ALLOWED = "EXECUTION_NOT_ALLOWED"
UNSUPPORTED_TASK_TYPE = "UNSUPPORTED_TASK_TYPE"
INVALID_TASK = "INVALID_TASK"
ATTEMPT_LOCKED = "ATTEMPT_LOCKED"


@dataclass(frozen=True, slots=True)
class BridgeExecutionResult:
    task_id: str
    claim_nonce: str
    task_type: str | None
    status: str
    result_path: str
    protocol_state: str


def _outcome(task_id, claim_nonce, task_type, status, path, state):
    return BridgeExecutionResult(
        task_id, claim_nonce, task_type, status, path, state,
    )


def _execute_bridge_test(payload):
    if not isinstance(payload, dict) or set(payload) != {"message"}:
        raise ValueError("bridge_test payload must contain only message")
    message = payload["message"]
    if not isinstance(message, str):
        raise ValueError("bridge_test message must be a string")
    _screen_payload(payload)
    return {"echo": message}


def _read_task(reader, task_path, task_id):
    try:
        task = reader.read_json(task_path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("task JSON is unreadable") from error
    required = {
        "task_schema_version", "task_id", "task_type", "payload",
        "publisher_id", "published_at",
    }
    if not isinstance(task, dict) or not required.issubset(task):
        raise ValueError("Task v1 record is missing required fields")
    if task["task_schema_version"] != 1 or task["task_id"] != task_id:
        raise ValueError("Task v1 identity is invalid")
    return task


def _execute_bridge_task_under_fence(
    bridge_root, task_id, consumer_id, claim_nonce,
):
    """Execute one owned attempt without granting broader execution authority."""
    if not valid_task_id(task_id):
        raise ValueError("task_id is unsafe or invalid")
    if not isinstance(consumer_id, str) or not consumer_id:
        raise ValueError("consumer_id must be a non-empty string")
    if not valid_task_id(claim_nonce):
        raise ValueError("claim_nonce is unsafe or invalid")

    reader = BridgePathReader(bridge_root)
    result_path = reader.path("outbox", "result-" + task_id + ".json")
    ready_path = reader.path("outbox", "result-" + task_id + ".ready")
    state = inspect_bridge_task(
        reader.root, task_id, consumer_id=consumer_id, claim_nonce=claim_nonce,
    )

    if state.state == COMPLETED:
        return _outcome(
            task_id, claim_nonce, None, ALREADY_COMPLETED, result_path, state.state,
        )
    if state.state == INVALID_HISTORY:
        return _outcome(
            task_id, claim_nonce, None, EXECUTION_NOT_ALLOWED, result_path, state.state,
        )
    result_exists = reader.exists(result_path)
    ready_exists = reader.exists(ready_path)
    if ready_exists:
        return _outcome(
            task_id, claim_nonce, None, ALREADY_COMPLETED, result_path, state.state,
        )
    if result_exists:
        return _outcome(
            task_id, claim_nonce, None, RESULT_PUBLISH_INCOMPLETE,
            result_path, state.state,
        )
    if (
        state.state != CLAIMED_BY_SELF_UNKNOWN
        or state.latest_claim_nonce != claim_nonce
        or state.consumer_id != consumer_id
    ):
        return _outcome(
            task_id, claim_nonce, None, EXECUTION_NOT_ALLOWED, result_path, state.state,
        )

    task_path = reader.path("inbox", task_id + ".json")
    try:
        task = _read_task(reader, task_path, task_id)
    except ValueError:
        return _outcome(
            task_id, claim_nonce, None, INVALID_TASK, result_path, state.state,
        )
    task_type = task["task_type"]
    if task_type != "bridge_test":
        return _outcome(
            task_id, claim_nonce, str(task_type), UNSUPPORTED_TASK_TYPE,
            result_path, state.state,
        )
    try:
        result_value = _execute_bridge_test(task["payload"])
        _screen_payload(result_value)
    except ValueError:
        return _outcome(
            task_id, claim_nonce, task_type, INVALID_TASK, result_path, state.state,
        )

    record = {
        "result_schema_version": 1,
        "task_id": task_id,
        "claim_nonce": claim_nonce,
        "consumer_id": consumer_id,
        "status": "completed",
        "result": result_value,
        "artifact_refs": [],
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    outbox = reader.require_directory(reader.path("outbox"))
    nonce = uuid.uuid4().hex
    try:
        _publish_file(outbox, result_path, _json_bytes(record), nonce)
        _publish_file(outbox, ready_path, b"", nonce)
    except FileExistsError:
        if reader.exists(ready_path):
            status = ALREADY_COMPLETED
        else:
            status = RESULT_PUBLISH_INCOMPLETE
        current = inspect_bridge_task(
            reader.root, task_id, consumer_id=consumer_id, claim_nonce=claim_nonce,
        )
        return _outcome(
            task_id, claim_nonce, task_type, status, result_path, current.state,
        )

    current = inspect_bridge_task(
        reader.root, task_id, consumer_id=consumer_id, claim_nonce=claim_nonce,
    )
    return _outcome(
        task_id, claim_nonce, task_type, EXECUTED, result_path, current.state,
    )


def execute_bridge_task(bridge_root, task_id, consumer_id, claim_nonce):
    """Execute only while owning the shared attempt execution fence."""
    if not valid_task_id(task_id):
        raise ValueError("task_id is unsafe or invalid")
    if not isinstance(consumer_id, str) or not consumer_id:
        raise ValueError("consumer_id must be a non-empty string")
    if not valid_task_id(claim_nonce):
        raise ValueError("claim_nonce is unsafe or invalid")
    fence, fence_status = acquire_bridge_attempt_fence(
        bridge_root, task_id, claim_nonce,
    )
    if fence_status == ATTEMPT_FENCE_LOCKED:
        reader = BridgePathReader(bridge_root)
        return _outcome(
            task_id, claim_nonce, None, ATTEMPT_LOCKED,
            reader.path("outbox", "result-" + task_id + ".json"),
            inspect_bridge_task(reader.root, task_id).state,
        )
    try:
        return _execute_bridge_task_under_fence(
            bridge_root, task_id, consumer_id, claim_nonce,
        )
    finally:
        fence.release()
