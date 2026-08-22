"""Bind untrusted Bridge requests to fresh Harness runs without adding authority."""

from collections import Counter
import copy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import uuid

from .audit import AuditWriter, read_events
from .bridge_attempt_fence import (
    ATTEMPT_FENCE_LOCKED, acquire_bridge_attempt_fence,
    harness_terminal_marker_path,
)
from .bridge_inspector import (
    CLAIMED_BY_SELF_UNKNOWN,
    COMPLETED,
    INVALID_HISTORY,
    inspect_bridge_task,
)
from .bridge_paths import BridgePathReader, valid_task_id
from .bridge_publisher import _json_bytes, _publish_file, _screen_payload
from .integrity import (
    ImmutableRecordConflict,
    atomic_json_publish,
    canonical_json_bytes,
    sha256_identity,
)
from .evidence import (
    EvidenceError, EvidenceStore, create_environment_observation_evidence,
)
from .result import (
    ResultError, ResultStore, answer_identity,
    bind_final_result, build_authoritative_result_state,
    screen_result_answer, validate_result,
)
from .run_envelope import RunEnvelopeStore
from .session import SessionStore


SOURCE = "bridge"
SOURCE_LABEL = "untrusted_external_input"
BRIDGE_HARNESS_TASK = "bridge_harness_task"
MAX_REQUEST_BYTES = 16 * 1024
MAX_SUMMARY_BYTES = 4 * 1024

BINDING_CREATED = "BINDING_CREATED"
BINDING_REUSED = "BINDING_REUSED"
BINDING_CONFLICT = "BINDING_CONFLICT"
RUN_COMPLETED = "RUN_COMPLETED"
INTEGRATION_UNKNOWN = "INTEGRATION_UNKNOWN"
BOUND_NOT_STARTED = "BOUND_NOT_STARTED"
HARNESS_RECOVERY_REQUIRED = "HARNESS_RECOVERY_REQUIRED"
RESULT_PROJECTION_REQUIRED = "RESULT_PROJECTION_REQUIRED"
DONE = "DONE"
BINDING_LOCKED = "BINDING_LOCKED"
PROJECTION_REPAIRED = "PROJECTION_REPAIRED"
EVIDENCE_REPAIR_REQUIRED = "EVIDENCE_REPAIR_REQUIRED"
EVIDENCE_REPAIRED = "EVIDENCE_REPAIRED"
HARNESS_RESULT_REPAIR_REQUIRED = "HARNESS_RESULT_REPAIR_REQUIRED"
OBSERVATION_RECOVERY_REQUIRED = "OBSERVATION_RECOVERY_REQUIRED"

ADAPTER_FAULT_POINTS = frozenset({
    "after_claim_before_binding",
    "after_bridge_claim_before_binding",
    "after_binding_before_run_fence",
    "after_run_fence_before_run_start",
    "after_binding_before_run_create",
    "after_harness_terminal_before_bridge_result",
    "after_harness_result_before_bridge_projection",
    "after_bridge_result_json_before_ready",
})
_HEX_ID = re.compile(r"[0-9a-f]{32}\Z")
_ENVIRONMENT_CAPABILITY = re.compile(r"termux:[a-z][a-z0-9_]{0,63}\Z")
_BINDING_FIELDS = frozenset({
    "binding_schema_version", "task_id", "claim_nonce",
    "harness_session_id", "harness_run_id", "source",
    "source_fingerprint", "created_at", "binding_fingerprint",
})
_TERMINAL_MARKER_FIELDS = frozenset({
    "marker_schema_version", "task_id", "claim_nonce", "harness_run_id",
    "binding_fingerprint", "result_fingerprint",
})


class BridgeAdapterError(ValueError):
    pass


class AdapterInjectedFault(BaseException):
    def __init__(self, point):
        super().__init__("deterministic adapter fault at " + point)
        self.point = point


class DeterministicAdapterFaults:
    def __init__(self, points=()):
        unknown = set(points) - ADAPTER_FAULT_POINTS
        if unknown:
            raise ValueError("unknown adapter fault point: " + sorted(unknown)[0])
        self._remaining = Counter(points)
        self.hits = []

    def trigger(self, point):
        if point not in ADAPTER_FAULT_POINTS:
            raise ValueError("unknown adapter fault point: " + point)
        if self._remaining[point]:
            self._remaining[point] -= 1
            self.hits.append(point)
            raise AdapterInjectedFault(point)


def trigger_adapter_fault(injector, point):
    if injector is not None:
        if not isinstance(injector, DeterministicAdapterFaults):
            raise TypeError("fault injector must be adapter-created")
        injector.trigger(point)


@dataclass(frozen=True, slots=True)
class AdaptedBridgeTask:
    task_id: str
    request: str
    publisher_id: str
    source: str
    source_label: str
    source_fingerprint: str


@dataclass(frozen=True, slots=True)
class BridgeAdapterResult:
    task_id: str
    claim_nonce: str
    status: str
    harness_session_id: str | None = None
    harness_run_id: str | None = None
    harness_result_status: str | None = None
    bridge_state: str | None = None
    reason: str | None = None

    def to_dict(self):
        return asdict(self)


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _binding_fingerprint(binding):
    stable = {key: value for key, value in binding.items()
              if key != "binding_fingerprint"}
    return sha256_identity(stable)


def validate_bridge_binding(binding):
    if not isinstance(binding, dict) or set(binding) != _BINDING_FIELDS:
        raise BridgeAdapterError("binding schema is invalid")
    if binding["binding_schema_version"] != 1:
        raise BridgeAdapterError("binding_schema_version must be 1")
    if not valid_task_id(binding["task_id"]):
        raise BridgeAdapterError("binding task_id is invalid")
    if not valid_task_id(binding["claim_nonce"]):
        raise BridgeAdapterError("binding claim_nonce is invalid")
    for field in ("harness_session_id", "harness_run_id"):
        if not _HEX_ID.fullmatch(str(binding[field])):
            raise BridgeAdapterError(f"binding {field} is invalid")
    if binding["source"] != SOURCE:
        raise BridgeAdapterError("binding source must be bridge")
    for field in ("source_fingerprint", "binding_fingerprint"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(binding[field])):
            raise BridgeAdapterError(f"binding {field} is invalid")
    if not isinstance(binding["created_at"], str) or not binding["created_at"]:
        raise BridgeAdapterError("binding created_at is invalid")
    if binding["binding_fingerprint"] != _binding_fingerprint(binding):
        raise BridgeAdapterError("binding fingerprint mismatch")
    try:
        _screen_payload(binding, "binding")
    except ValueError as error:
        raise BridgeAdapterError("binding failed secret screening") from error
    return binding


class BridgeBindingStore:
    """Harness-owned immutable integration identities."""

    def __init__(self, audit_directory):
        self.audit_directory = os.path.realpath(os.path.abspath(audit_directory))
        os.makedirs(self.audit_directory, mode=0o700, exist_ok=True)
        self.directory = os.path.join(self.audit_directory, "bridge_bindings")

    def _path(self, task_id, claim_nonce):
        if not valid_task_id(task_id) or not valid_task_id(claim_nonce):
            raise BridgeAdapterError("unsafe binding identity")
        path = os.path.abspath(os.path.join(
            self.directory, task_id, claim_nonce + ".json",
        ))
        if os.path.commonpath((self.audit_directory, path)) != self.audit_directory:
            raise BridgeAdapterError("binding path escapes audit directory")
        parent = os.path.dirname(path)
        existing = parent
        while not os.path.lexists(existing):
            parent_existing = os.path.dirname(existing)
            if parent_existing == existing:
                break
            existing = parent_existing
        if os.path.lexists(existing):
            resolved = os.path.realpath(existing)
            if os.path.commonpath((self.audit_directory, resolved)) != self.audit_directory:
                raise BridgeAdapterError("binding symlink escapes audit directory")
        return path

    def load(self, task_id, claim_nonce, missing_ok=False):
        path = self._path(task_id, claim_nonce)
        try:
            with open(path, encoding="utf-8") as stream:
                return validate_bridge_binding(json.load(stream))
        except FileNotFoundError:
            if missing_ok:
                return None
            raise BridgeAdapterError("binding does not exist")
        except (OSError, json.JSONDecodeError) as error:
            raise BridgeAdapterError("binding is unreadable") from error

    def publish(self, binding):
        validate_bridge_binding(binding)
        try:
            atomic_json_publish(
                self._path(binding["task_id"], binding["claim_nonce"]), binding,
                temporary_prefix=".binding-", temporary_suffix=".tmp",
            )
        except ImmutableRecordConflict as error:
            raise BridgeAdapterError(BINDING_CONFLICT) from error
        return binding


def read_bridge_harness_task(bridge_root, task_id):
    """Validate a committed external request without granting authority."""
    if not valid_task_id(task_id):
        raise BridgeAdapterError("task_id is unsafe or invalid")
    reader = BridgePathReader(bridge_root)
    task_path = reader.path("inbox", task_id + ".json")
    ready_path = reader.path("inbox", task_id + ".ready")
    if not reader.exists(task_path) or not reader.exists(ready_path):
        raise BridgeAdapterError("Bridge task is not committed")
    try:
        task = reader.read_json(task_path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise BridgeAdapterError("Bridge task is unreadable") from error
    fields = {
        "task_schema_version", "task_id", "task_type", "payload",
        "publisher_id", "published_at",
    }
    if not isinstance(task, dict) or set(task) != fields:
        raise BridgeAdapterError("Bridge Task v1 schema is invalid")
    if task["task_schema_version"] != 1 or task["task_id"] != task_id:
        raise BridgeAdapterError("Bridge Task v1 identity mismatch")
    if task["task_type"] != BRIDGE_HARNESS_TASK:
        raise BridgeAdapterError("unsupported Bridge task_type")
    if not isinstance(task["publisher_id"], str) or not task["publisher_id"]:
        raise BridgeAdapterError("publisher_id must be a non-empty string")
    if not isinstance(task["published_at"], str) or not task["published_at"]:
        raise BridgeAdapterError("published_at must be a non-empty string")
    payload = task["payload"]
    if not isinstance(payload, dict) or set(payload) != {"request"}:
        raise BridgeAdapterError("bridge_harness_task payload must contain only request")
    request = payload["request"]
    if not isinstance(request, str) or not request.strip():
        raise BridgeAdapterError("request must be a non-empty string")
    try:
        encoded = request.encode("utf-8")
    except UnicodeError as error:
        raise BridgeAdapterError("request must be valid UTF-8") from error
    if len(encoded) > MAX_REQUEST_BYTES:
        raise BridgeAdapterError("request exceeds size limit")
    try:
        _screen_payload(payload)
    except ValueError as error:
        raise BridgeAdapterError("request failed secret screening") from error
    return AdaptedBridgeTask(
        task_id, request, task["publisher_id"], SOURCE, SOURCE_LABEL,
        sha256_identity(canonical_json_bytes(task)),
    )


def bind_bridge_attempt(
    bridge_root, audit_directory, task_id, claim_nonce, consumer_id,
    expected_source_fingerprint=None, session_id=None, run_id=None,
):
    """Durably bind one valid current claim before its Harness Run exists."""
    state = inspect_bridge_task(
        bridge_root, task_id, consumer_id=consumer_id, claim_nonce=claim_nonce,
    )
    task = read_bridge_harness_task(bridge_root, task_id)
    if (
        expected_source_fingerprint is not None
        and task.source_fingerprint != expected_source_fingerprint
    ):
        return None, BINDING_CONFLICT
    store = BridgeBindingStore(audit_directory)
    existing = store.load(task_id, claim_nonce, missing_ok=True)
    if existing is not None:
        if (
            existing["source_fingerprint"] != task.source_fingerprint
            or state.state == INVALID_HISTORY
            or state.latest_claim_nonce != claim_nonce
            or state.consumer_id != consumer_id
        ):
            return None, BINDING_CONFLICT
        return existing, BINDING_REUSED
    if (
        state.state != CLAIMED_BY_SELF_UNKNOWN
        or state.latest_claim_nonce != claim_nonce
        or state.consumer_id != consumer_id
    ):
        return None, BINDING_CONFLICT
    binding = {
        "binding_schema_version": 1,
        "task_id": task_id,
        "claim_nonce": claim_nonce,
        "harness_session_id": session_id or uuid.uuid4().hex,
        "harness_run_id": run_id or uuid.uuid4().hex,
        "source": SOURCE,
        "source_fingerprint": task.source_fingerprint,
        "created_at": _utc_now(),
        "binding_fingerprint": "",
    }
    binding["binding_fingerprint"] = _binding_fingerprint(binding)
    try:
        store.publish(binding)
    except BridgeAdapterError:
        existing = store.load(task_id, claim_nonce, missing_ok=True)
        if existing is None or existing["source_fingerprint"] != task.source_fingerprint:
            return None, BINDING_CONFLICT
        return existing, BINDING_REUSED
    writer = AuditWriter(
        binding["harness_session_id"], binding["harness_run_id"], audit_directory,
    )
    references = {
        "task_id": task_id,
        "claim_nonce": claim_nonce,
        "source_fingerprint": task.source_fingerprint,
        "harness_run_id": binding["harness_run_id"],
    }
    writer.append(
        "bridge_task_received", "environment", "bridge_task", "received",
        references=references,
    )
    writer.append(
        "bridge_attempt_bound", "harness", "bridge_attempt", "bound",
        references=references,
    )
    return binding, BINDING_CREATED


def _result_path(audit_directory, run_id):
    return os.path.join(audit_directory, "results", run_id + ".json")


def _load_harness_result(audit_directory, run_id):
    try:
        return ResultStore(os.path.join(audit_directory, "results")).load(run_id)
    except ResultError as error:
        if not os.path.exists(_result_path(audit_directory, run_id)):
            return None
        raise BridgeAdapterError("Harness Result is corrupt") from error


def _has_started_run(audit_directory, run_id):
    return any(
        event["event_type"] == "run_started"
        for event in read_events(run_id, audit_directory, missing_ok=True)
    )


def _environment_observation_event(audit_directory, run_id, checkpoint):
    """Locate the durable safe Observation bound to one succeeded action."""
    matches = [
        event for event in read_events(run_id, audit_directory, missing_ok=True)
        if event.get("event_type") == "action_state_changed"
        and event.get("actor") == "environment"
        and event.get("subject") == checkpoint["tool"]
        and event.get("outcome") == "succeeded"
        and (event.get("references") or {}).get("action_id")
        == checkpoint["action_id"]
        and isinstance(event.get("summary"), dict)
    ]
    return matches[-1] if len(matches) == 1 else None


def _matching_environment_evidence(
    audit_directory, run_id, capability, action_id, observation_event_id=None,
):
    store = EvidenceStore(os.path.join(audit_directory, "evidence"))
    try:
        names = sorted(os.listdir(store.directory))
    except FileNotFoundError:
        return []
    matches = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            record = store.load(name[:-5])
        except EvidenceError as error:
            raise BridgeAdapterError("Harness Evidence store is corrupt") from error
        source = record.get("source") or {}
        if (
            record.get("run_id") == run_id
            and record.get("evidence_type") == "termux_observation"
            and source.get("capability") == capability
            and source.get("action_id") == action_id
            and (
                observation_event_id is None
                or source.get("observation_event_id") == observation_event_id
            )
        ):
            matches.append(record)
    return matches


def _session_evidence_repair_required(
    session_directory, audit_directory, binding,
):
    try:
        session = SessionStore(session_directory).load(
            binding["harness_session_id"],
        )
        checkpoint = session["current_action_checkpoint"]
        if (
            not isinstance(checkpoint, dict)
            or checkpoint.get("state") != "succeeded"
            or _ENVIRONMENT_CAPABILITY.fullmatch(
                str(checkpoint.get("tool", "")),
            ) is None
        ):
            return False
        event = _environment_observation_event(
            audit_directory, binding["harness_run_id"], checkpoint,
        )
        if event is None:
            return False
        return not _matching_environment_evidence(
            audit_directory, binding["harness_run_id"], checkpoint["tool"],
            checkpoint["action_id"], event["event_id"],
        )
    except (BridgeAdapterError, ValueError, OSError):
        return False


def inspect_bridge_binding(bridge_root, audit_directory, task_id, claim_nonce):
    """Derive integration recovery state without changing Bridge or Harness."""
    binding = BridgeBindingStore(audit_directory).load(
        task_id, claim_nonce, missing_ok=True,
    )
    state = inspect_bridge_task(bridge_root, task_id)
    if binding is None:
        return INTEGRATION_UNKNOWN
    try:
        task = read_bridge_harness_task(bridge_root, task_id)
    except BridgeAdapterError:
        return BINDING_CONFLICT
    if task.source_fingerprint != binding["source_fingerprint"]:
        return BINDING_CONFLICT
    if state.state == COMPLETED:
        return DONE
    result = _load_harness_result(audit_directory, binding["harness_run_id"])
    if result is not None:
        return RESULT_PROJECTION_REQUIRED
    if _has_started_run(audit_directory, binding["harness_run_id"]):
        return HARNESS_RECOVERY_REQUIRED
    return BOUND_NOT_STARTED


def _safe_projection_summary(result):
    summary = result["answer"]
    allowed, _reason = screen_result_answer(summary)
    if not allowed:
        summary = "Harness terminal result: " + result["status"]
    encoded = summary.encode("utf-8")
    if len(encoded) > MAX_SUMMARY_BYTES:
        summary = encoded[:MAX_SUMMARY_BYTES].decode("utf-8", errors="ignore")
    try:
        _screen_payload(summary, "summary")
    except ValueError:
        summary = "Harness terminal result: " + result["status"]
    return summary


def _projection_matches(existing, expected):
    if not isinstance(existing, dict):
        return False
    fields = set(expected) | {"completed_at"}
    if set(existing) != fields:
        return False
    return all(existing.get(key) == value for key, value in expected.items())


def _terminal_marker_record(binding, harness_result):
    return {
        "marker_schema_version": 1,
        "task_id": binding["task_id"],
        "claim_nonce": binding["claim_nonce"],
        "harness_run_id": binding["harness_run_id"],
        "binding_fingerprint": binding["binding_fingerprint"],
        "result_fingerprint": harness_result["result_fingerprint"],
    }


def _publish_harness_terminal_marker(bridge_root, binding, harness_result):
    """Durably exclude Bridge reconciliation before projection can race it."""
    validate_bridge_binding(binding)
    validate_result(harness_result)
    if harness_result["run_id"] != binding["harness_run_id"]:
        raise BridgeAdapterError("Harness terminal marker run mismatch")
    reader = BridgePathReader(bridge_root)
    directory = reader.require_directory(reader.path("locks"))
    path = harness_terminal_marker_path(
        reader, binding["task_id"], binding["claim_nonce"],
    )
    record = _terminal_marker_record(binding, harness_result)
    if set(record) != _TERMINAL_MARKER_FIELDS:
        raise BridgeAdapterError("Harness terminal marker schema invalid")
    if reader.exists(path):
        try:
            existing = reader.read_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise BridgeAdapterError("Harness terminal marker unreadable") from error
        if existing != record:
            raise BridgeAdapterError("Harness terminal marker conflict")
        return path
    try:
        _publish_file(
            directory, path, _json_bytes(record),
            "harness-terminal-" + uuid.uuid4().hex,
        )
    except FileExistsError:
        if reader.read_json(path) != record:
            raise BridgeAdapterError("Harness terminal marker conflict")
    return path


def project_harness_result_to_bridge(
    bridge_root, binding, consumer_id, harness_result, fault_injector=None,
):
    """Project authoritative status; never turn Bridge data into Evidence."""
    validate_bridge_binding(binding)
    validate_result(harness_result)
    if harness_result["run_id"] != binding["harness_run_id"]:
        raise BridgeAdapterError("Harness Result run_id does not match binding")
    reader = BridgePathReader(bridge_root)
    task_id, claim_nonce = binding["task_id"], binding["claim_nonce"]
    result_path = reader.path("outbox", "result-" + task_id + ".json")
    ready_path = reader.path("outbox", "result-" + task_id + ".ready")
    state = inspect_bridge_task(reader.root, task_id)
    if state.state == COMPLETED:
        return DONE
    if state.latest_claim_nonce != claim_nonce or state.consumer_id != consumer_id:
        raise BridgeAdapterError("Bridge claim no longer matches binding")
    artifact_refs = list(harness_result["artifact_ids"])
    projection = {
        "bridge_result_schema_version": 1,
        "harness_run_id": binding["harness_run_id"],
        "harness_result_status": harness_result["status"],
        "summary": _safe_projection_summary(harness_result),
        "artifact_refs": artifact_refs,
    }
    _screen_payload(projection, "projection")
    expected = {
        "result_schema_version": 1,
        "task_id": task_id,
        "claim_nonce": claim_nonce,
        "consumer_id": consumer_id,
        "status": "completed",
        "result": projection,
        "artifact_refs": artifact_refs,
        "completion_source": "harness_result_projection",
    }
    outbox = reader.require_directory(reader.path("outbox"))
    if reader.exists(result_path):
        try:
            existing = reader.read_json(result_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise BridgeAdapterError("Bridge Result projection is unreadable") from error
        if not _projection_matches(existing, expected):
            raise BridgeAdapterError("Bridge Result projection conflict")
    else:
        record = {**expected, "completed_at": _utc_now()}
        try:
            _publish_file(outbox, result_path, _json_bytes(record), uuid.uuid4().hex)
        except FileExistsError:
            existing = reader.read_json(result_path)
            if not _projection_matches(existing, expected):
                raise BridgeAdapterError("Bridge Result projection conflict")
    trigger_adapter_fault(
        fault_injector, "after_bridge_result_json_before_ready",
    )
    try:
        _publish_file(outbox, ready_path, b"", uuid.uuid4().hex)
    except FileExistsError:
        pass
    return DONE


def run_bound_bridge_request(
    bridge_root, audit_directory, session_directory, binding, consumer_id,
    provider, harness_runner, max_steps=5, fault_injector=None,
    harness_fault_injector=None,
):
    """Create/recover exactly one bound Run, then project its terminal Result."""
    validate_bridge_binding(binding)
    persisted = BridgeBindingStore(audit_directory).load(
        binding["task_id"], binding["claim_nonce"], missing_ok=True,
    )
    if persisted != binding:
        return BridgeAdapterResult(
            binding["task_id"], binding["claim_nonce"], BINDING_CONFLICT,
            binding["harness_session_id"], binding["harness_run_id"],
            reason="Harness Run requires a durable identical binding",
        )
    task = read_bridge_harness_task(bridge_root, binding["task_id"])
    if task.source_fingerprint != binding["source_fingerprint"]:
        return BridgeAdapterResult(
            binding["task_id"], binding["claim_nonce"], BINDING_CONFLICT,
            binding["harness_session_id"], binding["harness_run_id"],
        )
    run_id = binding["harness_run_id"]
    bridge_attempt = inspect_bridge_task(
        bridge_root, binding["task_id"], consumer_id=consumer_id,
        claim_nonce=binding["claim_nonce"],
    )
    if (
        bridge_attempt.state == INVALID_HISTORY
        or bridge_attempt.latest_claim_nonce != binding["claim_nonce"]
        or bridge_attempt.consumer_id != consumer_id
    ):
        return BridgeAdapterResult(
            binding["task_id"], binding["claim_nonce"], BINDING_CONFLICT,
            binding["harness_session_id"], run_id,
            bridge_state=bridge_attempt.state,
            reason="current Bridge history no longer matches binding",
        )
    terminal = _load_harness_result(audit_directory, run_id)
    if terminal is None and bridge_attempt.state != CLAIMED_BY_SELF_UNKNOWN:
        return BridgeAdapterResult(
            binding["task_id"], binding["claim_nonce"],
            HARNESS_RECOVERY_REQUIRED,
            binding["harness_session_id"], run_id,
            bridge_state=bridge_attempt.state,
            reason="bound attempt is no longer safe to start",
        )
    if terminal is None:
        trigger_adapter_fault(fault_injector, "after_binding_before_run_fence")
    fence, fence_status = acquire_bridge_attempt_fence(
        bridge_root, binding["task_id"], binding["claim_nonce"],
    )
    if fence_status == ATTEMPT_FENCE_LOCKED:
        return BridgeAdapterResult(
            binding["task_id"], binding["claim_nonce"], BINDING_LOCKED,
            binding["harness_session_id"], run_id,
            bridge_state=bridge_attempt.state,
            reason="attempt execution/projection fence is active",
        )
    if terminal is None:
        # A deterministic crash here intentionally leaves the same residue a
        # process crash would leave. V1 never steals or ages this fence.
        trigger_adapter_fault(fault_injector, "after_run_fence_before_run_start")
    leave_fence_for_manual_recovery = False
    try:
        # Start, terminal binding, and transport projection share one owner.
        # Every decision is re-read under the attempt fence.
        persisted = BridgeBindingStore(audit_directory).load(
            binding["task_id"], binding["claim_nonce"], missing_ok=True,
        )
        current_task = read_bridge_harness_task(
            bridge_root, binding["task_id"],
        )
        current_attempt = inspect_bridge_task(
            bridge_root, binding["task_id"], consumer_id=consumer_id,
            claim_nonce=binding["claim_nonce"],
        )
        terminal = _load_harness_result(audit_directory, run_id)
        if terminal is None and (
            persisted != binding
            or current_task.source_fingerprint != binding["source_fingerprint"]
            or current_attempt.state != CLAIMED_BY_SELF_UNKNOWN
            or current_attempt.latest_claim_nonce != binding["claim_nonce"]
            or current_attempt.consumer_id != consumer_id
        ):
            return BridgeAdapterResult(
                binding["task_id"], binding["claim_nonce"],
                HARNESS_RECOVERY_REQUIRED,
                binding["harness_session_id"], run_id,
                bridge_state=current_attempt.state,
                reason="bound attempt changed before Harness start",
            )
        if terminal is None and _has_started_run(audit_directory, run_id):
            status = (
                EVIDENCE_REPAIR_REQUIRED
                if _session_evidence_repair_required(
                    session_directory, audit_directory, binding,
                ) else HARNESS_RECOVERY_REQUIRED
            )
            return BridgeAdapterResult(
                binding["task_id"], binding["claim_nonce"], status,
                binding["harness_session_id"], run_id,
                bridge_state=current_attempt.state,
                reason=(
                    "durable Environment Observation requires Evidence repair"
                    if status == EVIDENCE_REPAIR_REQUIRED else
                    "Harness durability owns the started Run"
                ),
            )
        if terminal is None:
            trigger_adapter_fault(
                fault_injector, "after_binding_before_run_create",
            )
            _start_bound_harness_run(
                bridge_root, audit_directory, session_directory, binding,
                task, provider, harness_runner, max_steps,
                harness_fault_injector,
            )
            terminal = _load_harness_result(audit_directory, run_id)
        if terminal is None:
            status = (
                EVIDENCE_REPAIR_REQUIRED
                if _session_evidence_repair_required(
                    session_directory, audit_directory, binding,
                ) else HARNESS_RECOVERY_REQUIRED
            )
            return BridgeAdapterResult(
                binding["task_id"], binding["claim_nonce"], status,
                binding["harness_session_id"], run_id,
                bridge_state=inspect_bridge_task(
                    bridge_root, binding["task_id"],
                ).state,
                reason=(
                    "durable Environment Observation requires Evidence repair"
                    if status == EVIDENCE_REPAIR_REQUIRED else
                    "Bridge projection requires a durable Harness Result"
                ),
            )
        try:
            _publish_harness_terminal_marker(bridge_root, binding, terminal)
        except Exception:
            # Without this immutable exclusion marker, releasing the fence
            # would reopen a Reconciler race against durable Harness truth.
            leave_fence_for_manual_recovery = True
            raise
        trigger_adapter_fault(
            fault_injector, "after_harness_terminal_before_bridge_result",
        )
        trigger_adapter_fault(
            fault_injector, "after_harness_result_before_bridge_projection",
        )
        project_harness_result_to_bridge(
            bridge_root, binding, consumer_id, terminal, fault_injector,
        )
        state = inspect_bridge_task(bridge_root, binding["task_id"])
        return BridgeAdapterResult(
            binding["task_id"], binding["claim_nonce"], RUN_COMPLETED,
            binding["harness_session_id"], run_id, terminal["status"],
            state.state,
        )
    finally:
        if not leave_fence_for_manual_recovery:
            fence.release()


def _start_bound_harness_run(
    bridge_root, audit_directory, session_directory, binding, task, provider,
    harness_runner, max_steps, harness_fault_injector,
):
    """Start one fenced Run with explicit Harness-owned durable stores."""
    sessions = SessionStore(session_directory)
    session_path = sessions._path(binding["harness_session_id"])
    if os.path.exists(session_path):
        session = sessions.load(binding["harness_session_id"])
    else:
        session = sessions.create(binding["harness_session_id"])
    writer = AuditWriter(
        binding["harness_session_id"], binding["harness_run_id"],
        audit_directory,
    )

    def save_field(name):
        def save(value):
            session[name] = value
            sessions.save(session)
        return save

    external_task = (
        "Source: untrusted_external_input (Bridge textual request)\n"
        + task.request
    )
    terminal = harness_runner(
        external_task, provider, max_steps=max_steps,
        messages=session["messages"], verification=session["verification"],
        save_checkpoint=lambda: sessions.save(session),
        current_plan=session["current_plan"],
        plan_revision_history=session["plan_revision_history"],
        current_action_checkpoint=session["current_action_checkpoint"],
        save_action_checkpoint=save_field("current_action_checkpoint"),
        run_control=session["run_control"],
        save_run_control=save_field("run_control"),
        current_retry_state=session["current_retry_state"],
        save_retry_state=save_field("current_retry_state"),
        governance_state=session["current_governance_state"],
        save_governance_state=save_field("current_governance_state"),
        audit_writer=writer,
        evidence_store=EvidenceStore(os.path.join(audit_directory, "evidence")),
        result_store=ResultStore(os.path.join(audit_directory, "results")),
        return_result=True,
        fault_injector=harness_fault_injector,
    )
    sessions.save(session)
    return terminal


def _environment_recovery_source(audit_directory, session_directory, binding):
    """Rebuild only the identity-safe input already made durable by Harness."""
    sessions = SessionStore(session_directory)
    session = sessions.load(binding["harness_session_id"])
    checkpoint = session.get("current_action_checkpoint")
    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("state") != "succeeded"
        or not isinstance(checkpoint.get("observation"), dict)
        or _ENVIRONMENT_CAPABILITY.fullmatch(
            str(checkpoint.get("tool", "")),
        ) is None
    ):
        raise BridgeAdapterError(OBSERVATION_RECOVERY_REQUIRED)
    capability = checkpoint["tool"]
    effect = checkpoint.get("effect")
    if effect not in {"read_only", "side_effecting"}:
        raise BridgeAdapterError(OBSERVATION_RECOVERY_REQUIRED)
    event = _environment_observation_event(
        audit_directory, binding["harness_run_id"], checkpoint,
    )
    if event is None:
        raise BridgeAdapterError(OBSERVATION_RECOVERY_REQUIRED)
    summary = event["summary"]
    expected_certainty = (
        "no_side_effect" if effect == "read_only" else "known_applied"
    )
    structured = summary.get("structured")
    if (
        summary.get("status") != "succeeded"
        or summary.get("exit_code") != 0
        or summary.get("effect") != effect
        or summary.get("effect_certainty") != expected_certainty
        or not isinstance(structured, dict)
        or not structured
    ):
        raise BridgeAdapterError(OBSERVATION_RECOVERY_REQUIRED)
    checkpoint_observation = checkpoint["observation"]
    for key in (
        "status", "exit_code", "stdout_length", "stdout_sha256",
        "stderr_length", "stderr_sha256",
    ):
        if checkpoint_observation.get(key) != summary.get(key):
            raise BridgeAdapterError(OBSERVATION_RECOVERY_REQUIRED)
    observation = {
        "logical_capability": capability,
        "effect": effect,
        "effect_certainty": expected_certainty,
        "status": "succeeded",
        "safe_observation": copy.deepcopy(structured),
        "exit_code": 0,
        "stdout_length": summary["stdout_length"],
        "stdout_sha256": summary["stdout_sha256"],
        "stderr_length": summary["stderr_length"],
        "stderr_sha256": summary["stderr_sha256"],
    }
    arguments_identity = {}
    for name, value in sorted(checkpoint["arguments"].items()):
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        arguments_identity[name + "_length"] = len(encoded)
        arguments_identity[name + "_sha256"] = hashlib.sha256(encoded).hexdigest()
    return session, checkpoint, event, observation, arguments_identity


def _ensure_recovery_evidence_audit(
    audit_directory, binding, evidence,
):
    events = read_events(
        binding["harness_run_id"], audit_directory, missing_ok=True,
    )
    references = {
        "evidence_id": evidence["evidence_id"],
        "evidence_fingerprint": evidence["evidence_fingerprint"],
    }
    writer = AuditWriter(
        binding["harness_session_id"], binding["harness_run_id"],
        audit_directory,
    )
    if not any(
        item.get("event_type") == "evidence_created"
        and (item.get("references") or {}).get("evidence_id")
        == evidence["evidence_id"] for item in events
    ):
        writer.append(
            "evidence_created", "harness", "evidence", "created",
            references=references,
        )
    events = read_events(
        binding["harness_run_id"], audit_directory, missing_ok=True,
    )
    if not any(
        item.get("event_type") == "evidence_accepted"
        and (item.get("references") or {}).get("evidence_id")
        == evidence["evidence_id"] for item in events
    ):
        writer.append(
            "evidence_accepted", "harness", "evidence", "accepted",
            references=references,
        )


def _persist_recovery_result(audit_directory, binding, session):
    """Bind repaired Evidence without inventing a lost Model final answer."""
    run_id = binding["harness_run_id"]
    evidence_store = EvidenceStore(os.path.join(audit_directory, "evidence"))
    state, normalized = build_authoritative_result_state(
        run_id, candidate=None,
        run_control=session.get("run_control"),
        plan=session.get("current_plan"),
        verification_required=bool(
            session.get("verification", {}).get("requires_verification")
        ),
        evidence_store=evidence_store,
        audit_directory=audit_directory,
    )
    result, output = bind_final_result(state, normalized)
    RunEnvelopeStore(os.path.join(
        audit_directory, "envelopes",
    )).append_transition(
        run_id, "result_binding", state, output, idempotent=True,
    )
    ResultStore(os.path.join(audit_directory, "results")).save(result)
    events = read_events(run_id, audit_directory, missing_ok=True)
    writer = AuditWriter(
        binding["harness_session_id"], run_id, audit_directory,
    )
    if not any(
        item.get("event_type") == "final_result_emitted"
        and (item.get("references") or {}).get("result_fingerprint")
        == result["result_fingerprint"] for item in events
    ):
        identity = answer_identity(result["answer"])
        writer.append(
            "final_result_emitted", "harness", "result", result["status"],
            result["reason"], references={
                **identity,
                "claimed_status": result["candidate"]["claimed_status"],
                "authoritative_status": result["status"],
                "artifact_ids": result["artifact_ids"],
                "evidence_ids": result["evidence_ids"],
                "contradiction": result["candidate"]["contradiction"],
                "result_fingerprint": result["result_fingerprint"],
            },
        )
    events = read_events(run_id, audit_directory, missing_ok=True)
    if not any(
        item.get("event_type") == "run_state_changed"
        and item.get("outcome") == result["status"] for item in events
    ):
        writer.append(
            "run_state_changed", "harness", "run", result["status"],
            result["reason"],
        )
    return result


def recover_environment_evidence(
    bridge_root, audit_directory, session_directory, task_id, claim_nonce,
    consumer_id, fault_injector=None,
):
    """Repair Evidence/Result/projection from durable identities only.

    This helper has no Provider or Environment adapter parameter by design.  It
    cannot dispatch an action, mint new authority, obtain a claim, or infer an
    Observation that was not already durable.
    """
    task = read_bridge_harness_task(bridge_root, task_id)
    binding = BridgeBindingStore(audit_directory).load(task_id, claim_nonce)
    if binding["source_fingerprint"] != task.source_fingerprint:
        raise BridgeAdapterError(BINDING_CONFLICT)
    fence, fence_status = acquire_bridge_attempt_fence(
        bridge_root, task_id, claim_nonce,
    )
    if fence_status == ATTEMPT_FENCE_LOCKED:
        return BridgeAdapterResult(
            task_id, claim_nonce, BINDING_LOCKED,
            binding["harness_session_id"], binding["harness_run_id"],
            reason="attempt execution/projection fence is active",
        )
    leave_fence_for_manual_recovery = False
    repaired_evidence = False
    try:
        # Re-read every identity under the same lifecycle fence.
        if BridgeBindingStore(audit_directory).load(
            task_id, claim_nonce,
        ) != binding:
            raise BridgeAdapterError(BINDING_CONFLICT)
        current_task = read_bridge_harness_task(bridge_root, task_id)
        current = inspect_bridge_task(
            bridge_root, task_id, consumer_id=consumer_id,
            claim_nonce=claim_nonce,
        )
        if (
            current_task.source_fingerprint != binding["source_fingerprint"]
            or current.latest_claim_nonce != claim_nonce
            or current.consumer_id != consumer_id
        ):
            raise BridgeAdapterError(BINDING_CONFLICT)
        terminal = _load_harness_result(
            audit_directory, binding["harness_run_id"],
        )
        if terminal is None:
            try:
                session, checkpoint, event, observation, argument_identity = (
                    _environment_recovery_source(
                        audit_directory, session_directory, binding,
                    )
                )
            except BridgeAdapterError as error:
                if str(error) != OBSERVATION_RECOVERY_REQUIRED:
                    raise
                return BridgeAdapterResult(
                    task_id, claim_nonce, OBSERVATION_RECOVERY_REQUIRED,
                    binding["harness_session_id"], binding["harness_run_id"],
                    bridge_state=current.state,
                    reason="durable safe Observation identity is unavailable",
                )
            matches = _matching_environment_evidence(
                audit_directory, binding["harness_run_id"],
                checkpoint["tool"], checkpoint["action_id"], event["event_id"],
            )
            if len(matches) > 1:
                raise BridgeAdapterError("conflicting Environment Evidence history")
            if matches:
                evidence = matches[0]
            else:
                identity = {
                    "run_id": binding["harness_run_id"],
                    "action_id": checkpoint["action_id"],
                    "observation_id": event["event_id"],
                    "capability": checkpoint["tool"],
                    "effect": checkpoint["effect"],
                    "effect_certainty": observation["effect_certainty"],
                    "safe_observation": observation["safe_observation"],
                    "stream_identity": {
                        key: observation[key] for key in (
                            "stdout_length", "stdout_sha256",
                            "stderr_length", "stderr_sha256",
                        )
                    },
                }
                evidence = create_environment_observation_evidence(
                    binding["harness_run_id"], checkpoint["tool"], observation,
                    event["event_id"], checkpoint["action_id"],
                    argument_identity,
                    evidence_id=sha256_identity(identity)[:32],
                    created_at=event["timestamp"],
                    references={"recovery_source": "durable_observation"},
                )
                EvidenceStore(os.path.join(
                    audit_directory, "evidence",
                )).save(evidence)
                repaired_evidence = True
            _ensure_recovery_evidence_audit(
                audit_directory, binding, evidence,
            )
            session["verification"]["degraded"] = False
            session["verification"]["degraded_reason"] = None
            session["verification"]["degraded_stage"] = None
            SessionStore(session_directory).save(session)
            try:
                terminal = _persist_recovery_result(
                    audit_directory, binding, session,
                )
            except Exception as error:
                return BridgeAdapterResult(
                    task_id, claim_nonce, HARNESS_RESULT_REPAIR_REQUIRED,
                    binding["harness_session_id"], binding["harness_run_id"],
                    bridge_state=current.state,
                    reason="Evidence is durable; Harness Result repair remains: "
                    + type(error).__name__,
                )
        try:
            _publish_harness_terminal_marker(bridge_root, binding, terminal)
        except Exception:
            leave_fence_for_manual_recovery = True
            raise
        project_harness_result_to_bridge(
            bridge_root, binding, consumer_id, terminal, fault_injector,
        )
        state = inspect_bridge_task(bridge_root, task_id)
        return BridgeAdapterResult(
            task_id, claim_nonce,
            EVIDENCE_REPAIRED if repaired_evidence else PROJECTION_REPAIRED,
            binding["harness_session_id"], binding["harness_run_id"],
            terminal["status"], state.state,
        )
    finally:
        if not leave_fence_for_manual_recovery:
            fence.release()


def repair_bridge_harness_projection(
    bridge_root, audit_directory, task_id, claim_nonce, consumer_id,
    fault_injector=None,
):
    """Repair transport projection from one durable Harness Result only."""
    task = read_bridge_harness_task(bridge_root, task_id)
    binding = BridgeBindingStore(audit_directory).load(task_id, claim_nonce)
    if binding["source_fingerprint"] != task.source_fingerprint:
        raise BridgeAdapterError(BINDING_CONFLICT)
    fence, fence_status = acquire_bridge_attempt_fence(
        bridge_root, task_id, claim_nonce,
    )
    if fence_status == ATTEMPT_FENCE_LOCKED:
        return BridgeAdapterResult(
            task_id, claim_nonce, BINDING_LOCKED,
            binding["harness_session_id"], binding["harness_run_id"],
            reason="attempt execution/projection fence is active",
        )
    leave_fence_for_manual_recovery = False
    try:
        persisted = BridgeBindingStore(audit_directory).load(
            task_id, claim_nonce,
        )
        current_task = read_bridge_harness_task(bridge_root, task_id)
        if (
            persisted != binding
            or current_task.source_fingerprint != binding["source_fingerprint"]
        ):
            raise BridgeAdapterError(BINDING_CONFLICT)
        terminal = _load_harness_result(
            audit_directory, binding["harness_run_id"],
        )
        if terminal is None:
            raise BridgeAdapterError(HARNESS_RECOVERY_REQUIRED)
        try:
            _publish_harness_terminal_marker(bridge_root, binding, terminal)
        except Exception:
            leave_fence_for_manual_recovery = True
            raise
        project_harness_result_to_bridge(
            bridge_root, binding, consumer_id, terminal, fault_injector,
        )
        return BridgeAdapterResult(
            task_id, claim_nonce, PROJECTION_REPAIRED,
            binding["harness_session_id"], binding["harness_run_id"],
            terminal["status"], inspect_bridge_task(bridge_root, task_id).state,
        )
    finally:
        if not leave_fence_for_manual_recovery:
            fence.release()


def historical_bridge_binding_identity(binding):
    """Offline replay view: validate supplied history and perform zero I/O."""
    validate_bridge_binding(binding)
    return {
        "task_id": binding["task_id"],
        "claim_nonce": binding["claim_nonce"],
        "harness_run_id": binding["harness_run_id"],
        "source_fingerprint": binding["source_fingerprint"],
    }


def recover_bridge_binding(
    bridge_root, audit_directory, session_directory, task_id, claim_nonce,
    consumer_id, provider, harness_runner, max_steps=5, fault_injector=None,
):
    """Explicitly recover A/B/C; ordinary Workers never call this helper."""
    task = read_bridge_harness_task(bridge_root, task_id)
    binding, status = bind_bridge_attempt(
        bridge_root, audit_directory, task_id, claim_nonce, consumer_id,
        expected_source_fingerprint=task.source_fingerprint,
    )
    if binding is None:
        return BridgeAdapterResult(
            task_id, claim_nonce, status, reason="binding recovery failed closed",
        )
    return run_bound_bridge_request(
        bridge_root, audit_directory, session_directory, binding, consumer_id,
        provider, harness_runner, max_steps=max_steps,
        fault_injector=fault_injector,
    )
