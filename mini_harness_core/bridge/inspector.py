"""Read-only Bridge Protocol v1 history inspection and state derivation.

This module observes protocol records.  It never claims, reconciles, executes,
repairs, or writes a task.  In particular, timestamps are data to validate, not
an ordering or lease mechanism; claim order comes only from attempt_number.
"""

from dataclasses import asdict, dataclass
import json
import os

from .paths import BridgePathReader, valid_task_id


NOT_READY = "NOT_READY"
READY_TO_CLAIM = "READY_TO_CLAIM"
CLAIMED_UNKNOWN = "CLAIMED_UNKNOWN"
CLAIMED_BY_SELF_UNKNOWN = "CLAIMED_BY_SELF_UNKNOWN"
CLAIMED_BY_OTHER = "CLAIMED_BY_OTHER"
EFFECT_APPLIED_NEEDS_RESULT_REPAIR = "EFFECT_APPLIED_NEEDS_RESULT_REPAIR"
SAFE_TO_RECLAIM_WITH_NEW_NONCE = "SAFE_TO_RECLAIM_WITH_NEW_NONCE"
BLOCKED_UNCERTAIN_EFFECT = "BLOCKED_UNCERTAIN_EFFECT"
COMPLETED = "COMPLETED"
INVALID_HISTORY = "INVALID_HISTORY"

RECONCILIATION_RESULTS = frozenset({"applied", "not_applied", "uncertain"})
RESULT_STATUSES = frozenset({"completed"})


@dataclass(frozen=True, slots=True)
class BridgeTaskState:
    task_id: str
    state: str
    latest_claim_nonce: str | None = None
    latest_attempt: int | None = None
    consumer_id: str | None = None
    reconciliation: str | None = None
    result_status: str | None = None
    validation_errors: tuple[str, ...] = ()

    def to_dict(self):
        """Return a JSON-serializable representation."""
        value = asdict(self)
        value["validation_errors"] = list(self.validation_errors)
        return value


@dataclass(frozen=True, slots=True)
class _Claim:
    claim_nonce: str
    attempt_number: int
    previous_claim_nonce: str | None
    consumer_id: str
    path: str


@dataclass(frozen=True, slots=True)
class _Reconciliation:
    claim_nonce: str
    result: str
    path: str


def _label(path, root):
    return os.path.relpath(path, root).replace(os.sep, "/")


def _object(reader, path, errors):
    try:
        value = reader.read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        errors.append(f"{_label(path, reader.root)}: unreadable JSON: {error}")
        return None
    if not isinstance(value, dict):
        errors.append(f"{_label(path, reader.root)}: JSON record must be an object")
        return None
    return value


def _required(record, fields, label, errors):
    missing = [field for field in fields if field not in record]
    if missing:
        errors.append(f"{label}: missing required fields: {', '.join(missing)}")
        return False
    return True


def _invalid(task_id, errors, latest=None, reconciliation=None, result_status=None):
    return BridgeTaskState(
        task_id=task_id,
        state=INVALID_HISTORY,
        latest_claim_nonce=latest.claim_nonce if latest else None,
        latest_attempt=latest.attempt_number if latest else None,
        consumer_id=latest.consumer_id if latest else None,
        reconciliation=reconciliation,
        result_status=result_status,
        validation_errors=tuple(errors),
    )


def inspect_bridge_task(bridge_root, task_id, consumer_id=None, claim_nonce=None):
    """Inspect Bridge v1 history and derive the task's current protocol state.

    ``consumer_id`` and ``claim_nonce`` are only observer identity hints.  They
    grant no authority and are used solely to distinguish self from other when
    the latest claim has an unknown effect.
    """
    if not valid_task_id(task_id):
        return _invalid(str(task_id), ["task_id is unsafe or invalid"])
    if consumer_id is not None and (not isinstance(consumer_id, str) or not consumer_id):
        return _invalid(task_id, ["consumer_id must be a non-empty string"])
    if claim_nonce is not None and (not isinstance(claim_nonce, str) or not claim_nonce):
        return _invalid(task_id, ["claim_nonce must be a non-empty string"])

    try:
        reader = BridgePathReader(bridge_root)
    except ValueError as error:
        return _invalid(task_id, [str(error)])

    errors = []
    inbox = reader.path("inbox", task_id + ".json")
    inbox_ready = reader.path("inbox", task_id + ".ready")
    claims_dir = reader.path("claims", task_id)
    reconciliations_dir = reader.path("reconciliations", task_id)
    result_path = reader.path("outbox", "result-" + task_id + ".json")
    result_ready = reader.path("outbox", "result-" + task_id + ".ready")

    try:
        inbox_exists = reader.exists(inbox)
        ready_exists = reader.exists(inbox_ready)
        result_exists = reader.exists(result_path)
        result_ready_exists = reader.exists(result_ready)
        claim_paths = reader.list_json(claims_dir)
        reconciliation_paths = reader.list_json(reconciliations_dir)
    except (OSError, ValueError) as error:
        return _invalid(task_id, [str(error)])

    if inbox_exists:
        record = _object(reader, inbox, errors)
        label = _label(inbox, reader.root)
        if record is not None and _required(record, ("task_schema_version", "task_id"), label, errors):
            if record["task_schema_version"] != 1:
                errors.append(f"{label}: task_schema_version must be 1")
            if record["task_id"] != task_id:
                errors.append(f"{label}: task_id mismatch")
    elif ready_exists:
        errors.append("inbox ready marker exists without task JSON")

    claims = []
    for path in claim_paths:
        record = _object(reader, path, errors)
        label = _label(path, reader.root)
        required = (
            "claim_schema_version", "task_id", "consumer_id", "claim_nonce",
            "attempt_number", "previous_claim_nonce", "claimed_at",
        )
        if record is None or not _required(record, required, label, errors):
            continue
        valid = True
        if record["claim_schema_version"] != 1:
            errors.append(f"{label}: claim_schema_version must be 1")
            valid = False
        if record["task_id"] != task_id:
            errors.append(f"{label}: task_id mismatch")
            valid = False
        for field in ("consumer_id", "claim_nonce", "claimed_at"):
            if not isinstance(record[field], str) or not record[field]:
                errors.append(f"{label}: {field} must be a non-empty string")
                valid = False
        attempt = record["attempt_number"]
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            errors.append(f"{label}: attempt_number must be a positive integer")
            valid = False
        previous = record["previous_claim_nonce"]
        if previous is not None and (not isinstance(previous, str) or not previous):
            errors.append(f"{label}: previous_claim_nonce must be null or a non-empty string")
            valid = False
        if valid:
            claims.append(_Claim(record["claim_nonce"], attempt, previous,
                                 record["consumer_id"], path))

    nonce_counts = {}
    attempt_counts = {}
    for claim in claims:
        nonce_counts[claim.claim_nonce] = nonce_counts.get(claim.claim_nonce, 0) + 1
        attempt_counts[claim.attempt_number] = attempt_counts.get(claim.attempt_number, 0) + 1
    for nonce, count in sorted(nonce_counts.items()):
        if count > 1:
            errors.append(f"duplicate claim_nonce: {nonce}")
    for attempt, count in sorted(attempt_counts.items()):
        if count > 1:
            errors.append(f"fork or duplicate attempt_number: {attempt}")

    ordered = sorted(claims, key=lambda item: (item.attempt_number, item.claim_nonce))
    if ordered:
        expected_attempts = list(range(1, len(ordered) + 1))
        actual_attempts = [claim.attempt_number for claim in ordered]
        if actual_attempts != expected_attempts:
            errors.append(
                "claim attempt gap or duplicate: expected "
                f"{expected_attempts}, got {actual_attempts}"
            )
        for index, claim in enumerate(ordered):
            expected_previous = None if index == 0 else ordered[index - 1].claim_nonce
            if claim.previous_claim_nonce != expected_previous:
                errors.append(
                    f"claim attempt {claim.attempt_number}: previous_claim_nonce "
                    f"must be {expected_previous!r}"
                )
    latest = ordered[-1] if ordered else None

    valid_nonces = set(nonce_counts)
    reconciliations = []
    for path in reconciliation_paths:
        record = _object(reader, path, errors)
        label = _label(path, reader.root)
        required = ("reconciliation_schema_version", "task_id", "claim_nonce", "result")
        if record is None or not _required(record, required, label, errors):
            continue
        valid = True
        if record["reconciliation_schema_version"] != 1:
            errors.append(f"{label}: reconciliation_schema_version must be 1")
            valid = False
        if record["task_id"] != task_id:
            errors.append(f"{label}: task_id mismatch")
            valid = False
        if not isinstance(record["claim_nonce"], str) or not record["claim_nonce"]:
            errors.append(f"{label}: claim_nonce must be a non-empty string")
            valid = False
        elif record["claim_nonce"] not in valid_nonces:
            errors.append(f"{label}: reconciliation binds unknown claim")
            valid = False
        if record["result"] not in RECONCILIATION_RESULTS:
            errors.append(f"{label}: reconciliation result is invalid")
            valid = False
        if valid:
            reconciliations.append(_Reconciliation(
                record["claim_nonce"], record["result"], path,
            ))

    reconciliation_by_nonce = {}
    for reconciliation in reconciliations:
        if reconciliation.claim_nonce in reconciliation_by_nonce:
            errors.append(
                "multiple reconciliations for claim: " + reconciliation.claim_nonce
            )
        else:
            reconciliation_by_nonce[reconciliation.claim_nonce] = reconciliation
    for claim in ordered[:-1]:
        reconciliation = reconciliation_by_nonce.get(claim.claim_nonce)
        if reconciliation is None or reconciliation.result != "not_applied":
            errors.append(
                f"claim {claim.claim_nonce}: successor requires prior not_applied reconciliation"
            )

    latest_reconciliation = (
        reconciliation_by_nonce.get(latest.claim_nonce) if latest else None
    )
    result_status = None
    result_claim_nonce = None
    result_valid = False
    if result_exists:
        record = _object(reader, result_path, errors)
        label = _label(result_path, reader.root)
        required = ("result_schema_version", "task_id", "claim_nonce", "status")
        if record is not None and _required(record, required, label, errors):
            valid = True
            result_status = record["status"] if isinstance(record["status"], str) else None
            result_claim_nonce = record["claim_nonce"]
            if record["result_schema_version"] != 1:
                errors.append(f"{label}: result_schema_version must be 1")
                valid = False
            if record["task_id"] != task_id:
                errors.append(f"{label}: task_id mismatch")
                valid = False
            if result_claim_nonce not in valid_nonces:
                errors.append(f"{label}: result binds unknown claim")
                valid = False
            if latest is not None and result_claim_nonce != latest.claim_nonce:
                errors.append(f"{label}: result must bind the latest claim")
                valid = False
            if record["status"] not in RESULT_STATUSES:
                errors.append(f"{label}: result status is invalid")
                valid = False
            result_valid = valid
    elif result_ready_exists:
        errors.append("result ready marker exists without result JSON")

    if not ready_exists and (claim_paths or reconciliation_paths or result_exists
                             or result_ready_exists):
        errors.append("protocol history exists before inbox ready marker")

    if result_ready_exists:
        if errors or not result_valid:
            return _invalid(
                task_id, errors or ["ready result is invalid"], latest,
                latest_reconciliation.result if latest_reconciliation else None,
                result_status,
            )
        return BridgeTaskState(
            task_id, COMPLETED,
            latest.claim_nonce if latest else result_claim_nonce,
            latest.attempt_number if latest else None,
            latest.consumer_id if latest else None,
            latest_reconciliation.result if latest_reconciliation else None,
            result_status,
        )

    if ready_exists and not inbox_exists:
        # Already recorded above, retained here only as explicit state invariant.
        pass
    if errors:
        return _invalid(
            task_id, errors, latest,
            latest_reconciliation.result if latest_reconciliation else None,
            result_status,
        )
    if not ready_exists:
        return BridgeTaskState(task_id, NOT_READY, result_status=result_status)
    if latest is None:
        return BridgeTaskState(task_id, READY_TO_CLAIM, result_status=result_status)

    reconciliation_result = (
        latest_reconciliation.result if latest_reconciliation else None
    )
    common = {
        "task_id": task_id,
        "latest_claim_nonce": latest.claim_nonce,
        "latest_attempt": latest.attempt_number,
        "consumer_id": latest.consumer_id,
        "reconciliation": reconciliation_result,
        "result_status": result_status,
    }
    if reconciliation_result == "applied":
        return BridgeTaskState(state=EFFECT_APPLIED_NEEDS_RESULT_REPAIR, **common)
    if reconciliation_result == "not_applied":
        return BridgeTaskState(state=SAFE_TO_RECLAIM_WITH_NEW_NONCE, **common)
    if reconciliation_result == "uncertain":
        return BridgeTaskState(state=BLOCKED_UNCERTAIN_EFFECT, **common)

    if consumer_id is None and claim_nonce is None:
        state = CLAIMED_UNKNOWN
    else:
        consumer_matches = consumer_id is None or consumer_id == latest.consumer_id
        nonce_matches = claim_nonce is None or claim_nonce == latest.claim_nonce
        state = CLAIMED_BY_SELF_UNKNOWN if consumer_matches and nonce_matches else CLAIMED_BY_OTHER
    return BridgeTaskState(state=state, **common)
