"""Persist immutable judgments about Bridge v1 claim effects."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import re

from .attempt_fence import (
    ATTEMPT_FENCE_LOCKED, acquire_bridge_attempt_fence,
    harness_terminal_truth_exists,
)
from .inspector import COMPLETED, INVALID_HISTORY, inspect_bridge_task
from .paths import (
    BridgePathReader,
    atomic_rename_no_replace,
    valid_task_id,
)
from .publisher import _screen_payload


RECONCILED = "RECONCILED"
RECONCILIATION_EXISTS = "RECONCILIATION_EXISTS"
RECONCILIATION_NOT_ALLOWED = "RECONCILIATION_NOT_ALLOWED"
CLAIM_NOT_FOUND = "CLAIM_NOT_FOUND"
ATTEMPT_LOCKED = "ATTEMPT_LOCKED"

RECONCILIATION_RESULTS = frozenset({"applied", "not_applied", "uncertain"})
SHORT_TEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class BridgeReconciliationResult:
    task_id: str
    claim_nonce: str
    result: str
    status: str
    task_state: str | None = None


def _short_text(value, field):
    if not isinstance(value, str) or SHORT_TEXT.fullmatch(value) is None:
        raise ValueError(f"{field} must be a safe short string")
    _screen_payload(value, field)


def _ensure_reconciliation_directory(reader, task_id):
    root = reader.require_directory(reader.path("reconciliations"))
    directory = reader.path("reconciliations", task_id)
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    return reader.require_directory(directory)


def _publish_reconciliation(directory, final_path, record):
    temporary = os.path.join(
        directory, "." + os.path.basename(final_path) + ".tmp",
    )
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            json.dump(record, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        atomic_rename_no_replace(temporary, final_path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _reconcile_bridge_claim_under_fence(
    bridge_root, task_id, claim_nonce, result, checked_by, method,
):
    """Validate history and persist a judgment without taking follow-up action."""
    if not valid_task_id(task_id):
        raise ValueError("task_id is unsafe or invalid")
    if not valid_task_id(claim_nonce):
        raise ValueError("claim_nonce is unsafe or invalid")
    if result not in RECONCILIATION_RESULTS:
        raise ValueError("result must be applied, not_applied, or uncertain")
    _short_text(checked_by, "checked_by")
    _short_text(method, "method")

    reader = BridgePathReader(bridge_root)
    state = inspect_bridge_task(reader.root, task_id)
    final_path = reader.path("reconciliations", task_id, claim_nonce + ".json")

    if state.state in {INVALID_HISTORY, COMPLETED}:
        return BridgeReconciliationResult(
            task_id, claim_nonce, result, RECONCILIATION_NOT_ALLOWED, state.state,
        )
    if state.latest_claim_nonce is None:
        return BridgeReconciliationResult(
            task_id, claim_nonce, result, CLAIM_NOT_FOUND, state.state,
        )
    if claim_nonce != state.latest_claim_nonce:
        return BridgeReconciliationResult(
            task_id, claim_nonce, result, RECONCILIATION_NOT_ALLOWED, state.state,
        )
    if reader.exists(final_path):
        return BridgeReconciliationResult(
            task_id, claim_nonce, result, RECONCILIATION_EXISTS, state.state,
        )
    if state.reconciliation is not None:
        return BridgeReconciliationResult(
            task_id, claim_nonce, result, RECONCILIATION_EXISTS, state.state,
        )

    directory = _ensure_reconciliation_directory(reader, task_id)
    if reader.exists(final_path):
        return BridgeReconciliationResult(
            task_id, claim_nonce, result, RECONCILIATION_EXISTS, state.state,
        )
    record = {
        "reconciliation_schema_version": 1,
        "task_id": task_id,
        "claim_nonce": claim_nonce,
        "result": result,
        "checked_by": checked_by,
        "method": method,
        "reconciled_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _publish_reconciliation(directory, final_path, record)
    except FileExistsError:
        return BridgeReconciliationResult(
            task_id, claim_nonce, result, RECONCILIATION_EXISTS, state.state,
        )
    return BridgeReconciliationResult(
        task_id, claim_nonce, result, RECONCILED, state.state,
    )


def reconcile_bridge_claim(
    bridge_root, task_id, claim_nonce, result, checked_by, method,
):
    """Reconcile only while no live executor owns the same attempt."""
    if not valid_task_id(task_id):
        raise ValueError("task_id is unsafe or invalid")
    if not valid_task_id(claim_nonce):
        raise ValueError("claim_nonce is unsafe or invalid")
    if result not in RECONCILIATION_RESULTS:
        raise ValueError("result must be applied, not_applied, or uncertain")
    _short_text(checked_by, "checked_by")
    _short_text(method, "method")
    # Once the Harness has a durable terminal Result, Bridge-side effect
    # judgment is no longer an admissible recovery path.  This immutable marker
    # remains meaningful even if no live projection owner currently holds the
    # transient attempt fence.
    if harness_terminal_truth_exists(bridge_root, task_id, claim_nonce):
        return BridgeReconciliationResult(
            task_id, claim_nonce, result, RECONCILIATION_NOT_ALLOWED,
            inspect_bridge_task(bridge_root, task_id).state,
        )
    fence, fence_status = acquire_bridge_attempt_fence(
        bridge_root, task_id, claim_nonce,
    )
    if fence_status == ATTEMPT_FENCE_LOCKED:
        return BridgeReconciliationResult(
            task_id, claim_nonce, result, ATTEMPT_LOCKED,
            inspect_bridge_task(bridge_root, task_id).state,
        )
    try:
        # Re-read under the fence so publication racing with this entry can only
        # lower authority and never create a reconciliation after terminal
        # Harness truth.
        if harness_terminal_truth_exists(bridge_root, task_id, claim_nonce):
            return BridgeReconciliationResult(
                task_id, claim_nonce, result, RECONCILIATION_NOT_ALLOWED,
                inspect_bridge_task(bridge_root, task_id).state,
            )
        return _reconcile_bridge_claim_under_fence(
            bridge_root, task_id, claim_nonce, result, checked_by, method,
        )
    finally:
        fence.release()
