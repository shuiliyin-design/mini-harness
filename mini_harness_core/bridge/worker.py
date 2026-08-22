"""A deterministic, single-step composition of Bridge v1 primitives."""

from dataclasses import asdict, dataclass
import os
import uuid

from .claimer import CLAIMED, claim_bridge_task
from .executor import execute_bridge_task
from .inspector import (
    BLOCKED_UNCERTAIN_EFFECT,
    CLAIMED_BY_OTHER,
    CLAIMED_BY_SELF_UNKNOWN,
    CLAIMED_UNKNOWN,
    COMPLETED,
    EFFECT_APPLIED_NEEDS_RESULT_REPAIR,
    INVALID_HISTORY,
    READY_TO_CLAIM,
    SAFE_TO_RECLAIM_WITH_NEW_NONCE,
    inspect_bridge_task,
)
from .paths import BridgePathReader, valid_task_id


IDLE = "IDLE"
CLAIM_AND_EXECUTE = "CLAIM_AND_EXECUTE"
WOULD_CLAIM_AND_EXECUTE = "WOULD_CLAIM_AND_EXECUTE"
CLAIM_FAILED = "CLAIM_FAILED"
NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
NEEDS_RECLAIM = "NEEDS_RECLAIM"
NEEDS_RESULT_REPAIR = "NEEDS_RESULT_REPAIR"
BLOCKED = "BLOCKED"
SKIPPED_INVALID = "SKIPPED_INVALID"


@dataclass(frozen=True, slots=True)
class BridgeWorkerResult:
    consumer_id: str
    task_id: str | None
    initial_state: str
    action: str
    final_state: str
    claim_nonce: str | None = None
    reason: str | None = None

    def to_dict(self):
        return asdict(self)


def _discover(reader):
    inbox = reader.require_directory(reader.path("inbox"))
    names = set(os.listdir(inbox))
    candidates = []
    for name in names:
        if name.startswith(".") or not name.endswith(".json"):
            continue
        task_id = name[:-5]
        if valid_task_id(task_id) and task_id + ".ready" in names:
            candidates.append(task_id)
    return sorted(candidates)


def _report_non_actionable(consumer_id, task_id, state):
    if state == SAFE_TO_RECLAIM_WITH_NEW_NONCE:
        action = NEEDS_RECLAIM
        reason = "V1 worker does not auto-reclaim"
    elif state == BLOCKED_UNCERTAIN_EFFECT:
        action = BLOCKED
        reason = "claim effect is uncertain"
    elif state == EFFECT_APPLIED_NEEDS_RESULT_REPAIR:
        action = NEEDS_RESULT_REPAIR
        reason = "result repair tool is required"
    elif state in {CLAIMED_UNKNOWN, CLAIMED_BY_OTHER, CLAIMED_BY_SELF_UNKNOWN}:
        action = NEEDS_RECONCILIATION
        reason = "existing claim is never continued automatically"
    else:
        action = SKIPPED_INVALID
        reason = "invalid Bridge history"
    return BridgeWorkerResult(
        consumer_id, task_id, state, action, state, reason=reason,
    )


def run_bridge_worker_once(bridge_root, consumer_id, dry_run=False):
    """Inspect all committed tasks and process at most one READY task."""
    if not isinstance(consumer_id, str) or not consumer_id:
        raise ValueError("consumer_id must be a non-empty string")
    reader = BridgePathReader(bridge_root)
    non_actionable = []
    inspected = [
        (
            task_id,
            inspect_bridge_task(reader.root, task_id, consumer_id=consumer_id),
        )
        for task_id in _discover(reader)
    ]
    invalid_tasks = [
        task_id for task_id, state in inspected if state.state == INVALID_HISTORY
    ]

    for task_id, state in inspected:
        if state.state == COMPLETED:
            continue
        if state.state == INVALID_HISTORY:
            non_actionable.append(_report_non_actionable(
                consumer_id, task_id, state.state,
            ))
            continue
        if state.state != READY_TO_CLAIM:
            non_actionable.append(_report_non_actionable(
                consumer_id, task_id, state.state,
            ))
            continue

        warning = None
        if invalid_tasks:
            warning = "skipped invalid history: " + ", ".join(invalid_tasks)
        if dry_run:
            return BridgeWorkerResult(
                consumer_id, task_id, state.state, WOULD_CLAIM_AND_EXECUTE,
                state.state, reason=warning,
            )

        claim_nonce = "claim-" + uuid.uuid4().hex
        claim = claim_bridge_task(
            reader.root, task_id, consumer_id, claim_nonce,
        )
        if claim.status != CLAIMED:
            current = inspect_bridge_task(
                reader.root, task_id, consumer_id=consumer_id,
            )
            reason = "claimer returned " + claim.status
            if warning:
                reason += "; " + warning
            return BridgeWorkerResult(
                consumer_id, task_id, state.state, CLAIM_FAILED,
                current.state, claim_nonce, reason,
            )

        # This process-local value is the only authority to continue. A later
        # worker invocation cannot reconstruct it from Inspector state.
        execution = execute_bridge_task(
            reader.root, task_id, consumer_id, claim_nonce,
        )
        reason = None if execution.status == "EXECUTED" else execution.status
        if warning:
            reason = warning if reason is None else reason + "; " + warning
        return BridgeWorkerResult(
            consumer_id, task_id, state.state, CLAIM_AND_EXECUTE,
            execution.protocol_state, claim_nonce, reason,
        )

    if non_actionable:
        priority = {
            NEEDS_RECONCILIATION: 0,
            BLOCKED: 1,
            NEEDS_RESULT_REPAIR: 2,
            NEEDS_RECLAIM: 3,
            SKIPPED_INVALID: 4,
        }
        fallback = min(
            non_actionable,
            key=lambda item: (priority[item.action], item.task_id),
        )
        if invalid_tasks and fallback.reason != "invalid Bridge history":
            reason = fallback.reason + "; skipped invalid history: " + ", ".join(invalid_tasks)
            return BridgeWorkerResult(
                fallback.consumer_id, fallback.task_id, fallback.initial_state,
                fallback.action, fallback.final_state, fallback.claim_nonce, reason,
            )
        return fallback
    return BridgeWorkerResult(
        consumer_id, None, IDLE, IDLE, IDLE,
        reason=("skipped invalid history: " + ", ".join(invalid_tasks))
        if invalid_tasks else None,
    )
