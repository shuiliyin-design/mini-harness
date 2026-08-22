"""Single-step Bridge-to-Harness orchestration with no new authority."""

from dataclasses import asdict, dataclass
import json
import os
import uuid

from .bridge_adapter import (
    BINDING_CONFLICT,
    BridgeAdapterError,
    bind_bridge_attempt,
    inspect_bridge_binding,
    read_bridge_harness_task,
    run_bound_bridge_request,
    trigger_adapter_fault,
)
from .bridge_claimer import CLAIMED, claim_bridge_task
from .bridge_inspector import (
    CLAIMED_BY_OTHER,
    CLAIMED_BY_SELF_UNKNOWN,
    CLAIMED_UNKNOWN,
    COMPLETED,
    READY_TO_CLAIM,
    inspect_bridge_task,
)
from .bridge_paths import BridgePathReader, valid_task_id


IDLE = "IDLE"
CLAIM_AND_RUN = "CLAIM_AND_RUN"
CLAIM_FAILED = "CLAIM_FAILED"
TASK_REJECTED = "TASK_REJECTED"


@dataclass(frozen=True, slots=True)
class BridgeHarnessWorkerResult:
    consumer_id: str
    task_id: str | None
    initial_state: str
    action: str
    final_state: str
    claim_nonce: str | None = None
    harness_run_id: str | None = None
    harness_result_status: str | None = None
    reason: str | None = None

    def to_dict(self):
        return asdict(self)


def _committed_candidates(reader):
    inbox = reader.require_directory(reader.path("inbox"))
    names = set(os.listdir(inbox))
    return sorted(
        name[:-5] for name in names
        if name.endswith(".json") and not name.startswith(".")
        and valid_task_id(name[:-5]) and name[:-5] + ".ready" in names
    )


def _declared_task_type(reader, task_id):
    try:
        value = reader.read_json(reader.path("inbox", task_id + ".json"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    return value.get("task_type") if isinstance(value, dict) else None


def run_bridge_harness_worker_once(
    bridge_root, consumer_id, provider, *, audit_directory,
    harness_runner, session_directory=None, max_steps=5,
    fault_injector=None, harness_fault_injector=None,
):
    """Claim and run at most one fresh bridge_harness_task."""
    if not isinstance(consumer_id, str) or not consumer_id:
        raise ValueError("consumer_id must be a non-empty string")
    audit_directory = os.path.realpath(os.path.abspath(audit_directory))
    session_directory = session_directory or os.path.join(
        audit_directory, "sessions",
    )
    reader = BridgePathReader(bridge_root)
    fallback = None

    for task_id in _committed_candidates(reader):
        if _declared_task_type(reader, task_id) != "bridge_harness_task":
            continue
        state = inspect_bridge_task(reader.root, task_id, consumer_id=consumer_id)
        if state.state == COMPLETED:
            continue
        if state.state in {
            CLAIMED_UNKNOWN, CLAIMED_BY_OTHER, CLAIMED_BY_SELF_UNKNOWN,
        }:
            integration = inspect_bridge_binding(
                reader.root, audit_directory, task_id,
                state.latest_claim_nonce,
            )
            if fallback is None:
                fallback = BridgeHarnessWorkerResult(
                    consumer_id, task_id, state.state, integration,
                    state.state, state.latest_claim_nonce,
                    reason="old Bridge claim is never resumed automatically",
                )
            continue
        if state.state != READY_TO_CLAIM:
            if fallback is None:
                fallback = BridgeHarnessWorkerResult(
                    consumer_id, task_id, state.state, TASK_REJECTED,
                    state.state, reason="Bridge state is not actionable",
                )
            continue
        try:
            task = read_bridge_harness_task(reader.root, task_id)
        except BridgeAdapterError as error:
            return BridgeHarnessWorkerResult(
                consumer_id, task_id, state.state, TASK_REJECTED,
                state.state, reason=str(error),
            )
        claim_nonce = "claim-" + uuid.uuid4().hex
        claim = claim_bridge_task(
            reader.root, task_id, consumer_id, claim_nonce,
        )
        if claim.status != CLAIMED:
            current = inspect_bridge_task(
                reader.root, task_id, consumer_id=consumer_id,
            )
            return BridgeHarnessWorkerResult(
                consumer_id, task_id, state.state, CLAIM_FAILED,
                current.state, claim_nonce, reason=claim.status,
            )
        trigger_adapter_fault(fault_injector, "after_claim_before_binding")
        trigger_adapter_fault(
            fault_injector, "after_bridge_claim_before_binding",
        )
        binding, binding_status = bind_bridge_attempt(
            reader.root, audit_directory, task_id, claim_nonce, consumer_id,
            expected_source_fingerprint=task.source_fingerprint,
        )
        if binding is None:
            current = inspect_bridge_task(
                reader.root, task_id, consumer_id=consumer_id,
            )
            return BridgeHarnessWorkerResult(
                consumer_id, task_id, state.state, BINDING_CONFLICT,
                current.state, claim_nonce, reason=binding_status,
            )
        adapted = run_bound_bridge_request(
            reader.root, audit_directory, session_directory, binding,
            consumer_id, provider, harness_runner, max_steps, fault_injector,
            harness_fault_injector,
        )
        return BridgeHarnessWorkerResult(
            consumer_id, task_id, state.state, CLAIM_AND_RUN,
            adapted.bridge_state or inspect_bridge_task(reader.root, task_id).state,
            claim_nonce, adapted.harness_run_id,
            adapted.harness_result_status,
            None if adapted.status == "RUN_COMPLETED" else adapted.status,
        )
    if fallback is not None:
        return fallback
    return BridgeHarnessWorkerResult(
        consumer_id, None, IDLE, IDLE, IDLE,
    )
