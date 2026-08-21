"""V24 authoritative Task Result Contract and Final Answer binding.

The model proposes presentation and structured claims.  This module binds
those claims to immutable Harness state; it never executes a tool or calls a
model.
"""

import copy
import hashlib
import json
import os
import re
import tempfile

from .artifacts import (
    ARTIFACT_ID_PATTERN, ArtifactError, ArtifactStore, current_artifacts,
    artifact_integrity_check,
)
from .audit import AUDIT_DIR, ID_PATTERN, read_events
from .evidence import (
    EVIDENCE_ID_PATTERN, EvidenceError, EvidenceStore,
    evidence_integrity_check,
)
from .security import SECRET_PATTERNS


RESULT_SCHEMA_VERSION = 1
RESULT_DIR = os.path.join(AUDIT_DIR, "results")
RESULT_STATUSES = frozenset({
    "completed", "blocked", "failed", "cancelled", "incomplete",
})
RESULT_FIELDS = frozenset({
    "result_schema_version", "run_id", "status", "answer",
    "artifact_ids", "evidence_ids", "plan_id", "reason", "candidate",
    "result_fingerprint",
})
CANDIDATE_FIELDS = frozenset({
    "answer_length", "answer_sha256", "claimed_status", "artifact_refs",
    "evidence_refs", "answer_allowed", "answer_rejection_reason",
    "contradiction",
})
TRANSITION_INPUT_FIELDS = frozenset({
    "run_id", "run_control", "terminal_failure", "blocking_reason",
    "plan", "output_contract", "verification_required",
    "accepted_artifacts", "accepted_evidence", "candidate",
})
TRANSITION_OUTPUT_FIELDS = frozenset({
    "authoritative_status", "accepted_artifact_ids",
    "accepted_evidence_ids", "reason", "contradiction",
})
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
FORBIDDEN_ANSWER_PATTERNS = SECRET_PATTERNS + (
    re.compile(r"\bhidden reasoning\b|\bchain[- ]of[- ]thought\b", re.I),
    re.compile(r"\braw (?:tool output|command payload)\b", re.I),
)
MAX_ANSWER_BYTES = 64 * 1024


class ResultError(ValueError):
    """A Result or Final Candidate is unsafe, corrupt, or unavailable."""


def canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def answer_identity(answer):
    if not isinstance(answer, str) or not answer.strip():
        raise ResultError("final answer 必须是非空字符串")
    encoded = answer.encode("utf-8")
    return {
        "answer_length": len(encoded),
        "answer_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def screen_result_answer(answer):
    identity = answer_identity(answer)
    if identity["answer_length"] > MAX_ANSWER_BYTES:
        return False, "answer exceeds result size limit"
    if any(pattern.search(answer) for pattern in FORBIDDEN_ANSWER_PATTERNS):
        return False, "answer failed secret screening"
    return True, None


def _validate_refs(refs, pattern, name):
    if (not isinstance(refs, list) or len(refs) != len(set(refs))
            or not all(isinstance(item, str) and pattern.fullmatch(item)
                       for item in refs)):
        raise ResultError(f"{name} 必须是唯一 ID 数组")
    return list(refs)


def normalize_final_candidate(decision):
    """Normalize old/new Provider forms without granting claim authority."""
    if not isinstance(decision, dict) or decision.get("type") != "final_answer":
        raise ResultError("final candidate schema 无效")
    allowed = {
        "type", "answer", "final_answer", "claimed_status",
        "artifact_refs", "evidence_refs",
    }
    if set(decision) - allowed:
        raise ResultError("final candidate 包含未知字段")
    if "answer" in decision and "final_answer" in decision:
        raise ResultError("final candidate answer 字段重复")
    answer = decision.get("answer", decision.get("final_answer"))
    identity = answer_identity(answer)
    answer_allowed, answer_rejection_reason = screen_result_answer(answer)
    claimed = decision.get("claimed_status")
    if claimed is not None and claimed not in RESULT_STATUSES:
        raise ResultError("claimed_status 无效")
    artifact_refs = _validate_refs(
        decision.get("artifact_refs", []), ARTIFACT_ID_PATTERN,
        "artifact_refs",
    )
    evidence_refs = _validate_refs(
        decision.get("evidence_refs", []), EVIDENCE_ID_PATTERN,
        "evidence_refs",
    )
    return {
        "answer": answer,
        "metadata": {
            **identity,
            "claimed_status": claimed,
            "artifact_refs": artifact_refs,
            "evidence_refs": evidence_refs,
            "answer_allowed": answer_allowed,
            "answer_rejection_reason": answer_rejection_reason,
            "contradiction": False,
        },
    }


def _valid_identity(item, id_key, fingerprint_key, id_pattern):
    return (
        isinstance(item, dict)
        and isinstance(item.get(id_key), str)
        and id_pattern.fullmatch(item[id_key]) is not None
        and isinstance(item.get(fingerprint_key), str)
        and SHA256_PATTERN.fullmatch(item[fingerprint_key]) is not None
    )


def validate_result_binding_input(value):
    if not isinstance(value, dict) or set(value) != TRANSITION_INPUT_FIELDS:
        raise ResultError("result binding input schema 无效")
    if not ID_PATTERN.fullmatch(str(value["run_id"])):
        raise ResultError("result binding run_id 无效")
    control = value["run_control"]
    if (not isinstance(control, dict)
            or set(control) != {"state", "reason"}
            or not isinstance(control["state"], str)
            or control["reason"] is not None
            and not isinstance(control["reason"], str)):
        raise ResultError("result binding run_control 无效")
    for key in ("terminal_failure", "blocking_reason"):
        if value[key] is not None and (
            not isinstance(value[key], str) or not value[key].strip()
        ):
            raise ResultError(f"result binding {key} 无效")
    plan = value["plan"]
    if plan is not None and (
        not isinstance(plan, dict)
        or set(plan) != {"plan_id", "status", "completed_step_ids"}
        or not isinstance(plan["plan_id"], str)
        or not isinstance(plan["status"], str)
        or not isinstance(plan["completed_step_ids"], list)
        or not all(isinstance(item, str)
                   for item in plan["completed_step_ids"])
    ):
        raise ResultError("result binding plan 无效")
    output = value["output_contract"]
    if output is not None and (
        not isinstance(output, dict)
        or set(output) != {
            "satisfied", "contract_fingerprint", "accepted_artifact_ids",
        }
        or not isinstance(output["satisfied"], bool)
        or not SHA256_PATTERN.fullmatch(str(output["contract_fingerprint"]))
    ):
        raise ResultError("result binding output contract 无效")
    if output is not None:
        _validate_refs(
            output["accepted_artifact_ids"], ARTIFACT_ID_PATTERN,
            "output accepted_artifact_ids",
        )
    if not isinstance(value["verification_required"], bool):
        raise ResultError("verification_required 无效")
    artifacts = value["accepted_artifacts"]
    evidence = value["accepted_evidence"]
    if (not isinstance(artifacts, list)
            or not all(_valid_identity(
                item, "artifact_id", "artifact_fingerprint",
                ARTIFACT_ID_PATTERN,
            ) for item in artifacts)):
        raise ResultError("accepted artifact identities 无效")
    if (not isinstance(evidence, list)
            or not all(_valid_identity(
                item, "evidence_id", "evidence_fingerprint",
                EVIDENCE_ID_PATTERN,
            ) for item in evidence)):
        raise ResultError("accepted evidence identities 无效")
    candidate = value["candidate"]
    if candidate is not None:
        validate_candidate_metadata(candidate)
    return value


def validate_candidate_metadata(candidate):
    if not isinstance(candidate, dict) or set(candidate) != CANDIDATE_FIELDS:
        raise ResultError("candidate metadata schema 无效")
    if (not isinstance(candidate["answer_length"], int)
            or isinstance(candidate["answer_length"], bool)
            or candidate["answer_length"] < 0
            or not SHA256_PATTERN.fullmatch(str(candidate["answer_sha256"]))):
        raise ResultError("candidate answer identity 无效")
    if (candidate["claimed_status"] is not None
            and candidate["claimed_status"] not in RESULT_STATUSES):
        raise ResultError("candidate claimed_status 无效")
    _validate_refs(candidate["artifact_refs"], ARTIFACT_ID_PATTERN,
                   "candidate artifact_refs")
    _validate_refs(candidate["evidence_refs"], EVIDENCE_ID_PATTERN,
                   "candidate evidence_refs")
    if not isinstance(candidate["contradiction"], bool):
        raise ResultError("candidate contradiction 无效")
    if not isinstance(candidate["answer_allowed"], bool):
        raise ResultError("candidate answer screening 无效")
    if (candidate["answer_rejection_reason"] is not None
            and (not isinstance(candidate["answer_rejection_reason"], str)
                 or not candidate["answer_rejection_reason"].strip())):
        raise ResultError("candidate answer rejection reason 无效")
    if candidate["answer_allowed"] != (
        candidate["answer_rejection_reason"] is None
    ):
        raise ResultError("candidate answer screening binding 无效")
    return candidate


def evaluate_result_contract(inputs):
    """Pure deterministic transition used by both live binding and replay."""
    validate_result_binding_input(inputs)
    candidate = inputs["candidate"]
    eligible_artifacts = {
        item["artifact_id"] for item in inputs["accepted_artifacts"]
    }
    eligible_evidence = {
        item["evidence_id"] for item in inputs["accepted_evidence"]
    }
    output = inputs["output_contract"]
    artifact_ids = set(output["accepted_artifact_ids"] if output else [])
    evidence_ids = set()
    contradiction_reasons = []
    if candidate is not None:
        invalid_artifacts = sorted(
            set(candidate["artifact_refs"]) - eligible_artifacts
        )
        invalid_evidence = sorted(
            set(candidate["evidence_refs"]) - eligible_evidence
        )
        if invalid_artifacts:
            contradiction_reasons.append("invalid artifact reference")
        if invalid_evidence:
            contradiction_reasons.append("invalid evidence reference")
        artifact_ids.update(set(candidate["artifact_refs"]) & eligible_artifacts)
        evidence_ids.update(set(candidate["evidence_refs"]) & eligible_evidence)
    # Partial accepted outputs/evidence remain useful on non-completed runs.
    artifact_ids.update(eligible_artifacts)
    evidence_ids.update(eligible_evidence)

    control = inputs["run_control"]
    plan = inputs["plan"]
    reason = None
    if control["state"] in {"cancel_requested", "cancelled"}:
        status = "cancelled"
        reason = control["reason"] or "run cancelled"
    elif inputs["terminal_failure"] is not None or (
        plan is not None and plan["status"] == "failed"
    ):
        status = "failed"
        reason = inputs["terminal_failure"] or "plan failed"
    elif inputs["blocking_reason"] is not None or (
        plan is not None and plan["status"] == "blocked"
    ):
        status = "blocked"
        reason = inputs["blocking_reason"] or "plan blocked"
    elif plan is not None and plan["status"] != "completed":
        status = "incomplete"
        reason = "plan not completed"
    elif output is not None and not output["satisfied"]:
        status = "incomplete"
        reason = "output contract unsatisfied"
    elif inputs["verification_required"] and not inputs["accepted_evidence"]:
        status = "incomplete"
        reason = "required evidence unsatisfied"
    elif candidate is None:
        status = "incomplete"
        reason = "final candidate missing"
    else:
        status = "completed"

    if (candidate is not None and candidate["claimed_status"] is not None
            and candidate["claimed_status"] != status):
        contradiction_reasons.insert(0, "claimed status mismatch")
    elif candidate is not None and status != "completed":
        contradiction_reasons.insert(0, "completion gates unsatisfied")
    if candidate is not None and not candidate["answer_allowed"]:
        contradiction_reasons.append(candidate["answer_rejection_reason"])
    contradiction = bool(contradiction_reasons)
    if contradiction and reason is None:
        reason = "; ".join(contradiction_reasons)
    if reason is not None and any(
        pattern.search(reason) for pattern in FORBIDDEN_ANSWER_PATTERNS
    ):
        reason = "result reason removed by secret screening"
        contradiction = True
    return {
        "authoritative_status": status,
        "accepted_artifact_ids": sorted(artifact_ids),
        "accepted_evidence_ids": sorted(evidence_ids),
        "reason": reason,
        "contradiction": contradiction,
    }


def _evidence_is_accepted(record, run_id, plan, verification_required):
    if record["run_id"] != run_id:
        return False
    if (record["freshness"].get("scope") != "run"
            or record["freshness"].get("run_id") != run_id):
        return False
    kind = record["evidence_type"]
    verification = record["verification"]
    if record["subject"].get("kind") == "plan_step" and (
        plan is None
        or record["subject"].get("target")
        not in set(plan["completed_step_ids"])
    ):
        return False
    if kind in {"subagent_return", "mcp_observation"}:
        return False
    if kind in {"verification", "tool_observation"}:
        return verification.get("accepted") is True
    if kind == "reconciliation":
        return verification.get("result") in {"applied", "not_applied"}
    if kind == "reasoning_result":
        if verification_required or plan is None:
            return False
        return (
            plan["status"] == "completed"
            and record["subject"].get("kind") == "plan_step"
        )
    return False


def _list_run_evidence(store, run_id):
    try:
        names = sorted(os.listdir(store.directory))
    except FileNotFoundError:
        return []
    records = []
    for name in names:
        evidence_id = name[:-5] if name.endswith(".json") else ""
        if not EVIDENCE_ID_PATTERN.fullmatch(evidence_id):
            continue
        record = store.load(evidence_id)
        if record["run_id"] == run_id:
            records.append(record)
    return sorted(records, key=lambda item: item["created_at"])


def build_authoritative_result_state(
    run_id, candidate=None, run_control=None, terminal_failure=None,
    blocking_reason=None, plan=None, output_contract_result=None,
    verification_required=False, artifact_store=None, evidence_store=None,
    audit_directory=AUDIT_DIR,
):
    """Observe immutable Harness records and produce replay-safe input."""
    if not ID_PATTERN.fullmatch(str(run_id)):
        raise ResultError("result run_id 无效")
    normalized = normalize_final_candidate(candidate) if candidate else None
    artifact_store = artifact_store or ArtifactStore(os.path.join(
        audit_directory, "artifacts",
    ))
    evidence_store = evidence_store or EvidenceStore(os.path.join(
        audit_directory, "evidence",
    ))
    accepted_artifacts = []
    try:
        artifacts = current_artifacts(artifact_store.list_run(run_id))
    except (ArtifactError, OSError):
        artifacts = []
    for artifact in artifacts:
        if artifact["status"] != "accepted":
            continue
        if not artifact_integrity_check(
            artifact["artifact_id"], artifact_store.directory,
            evidence_store.directory, audit_directory,
        ):
            continue
        accepted_artifacts.append({
            "artifact_id": artifact["artifact_id"],
            "artifact_fingerprint": artifact["artifact_fingerprint"],
        })
    accepted_evidence = []
    try:
        evidences = _list_run_evidence(evidence_store, run_id)
    except (EvidenceError, OSError):
        evidences = []
    plan_identity = (
        {
            "plan_id": plan["plan_id"], "status": plan["status"],
            "completed_step_ids": [
                item["id"] for item in plan.get("steps", [])
                if item.get("status") == "completed"
            ],
        }
        if plan is not None else None
    )
    for evidence in evidences:
        if not _evidence_is_accepted(
            evidence, run_id, plan_identity, verification_required,
        ):
            continue
        if not evidence_integrity_check(
            evidence["evidence_id"], evidence_store.directory,
            audit_directory,
        ):
            continue
        accepted_evidence.append({
            "evidence_id": evidence["evidence_id"],
            "evidence_fingerprint": evidence["evidence_fingerprint"],
        })
    output = None
    if output_contract_result is not None:
        eligible_ids = {
            item["artifact_id"] for item in accepted_artifacts
        }
        output_ids = list(output_contract_result.get(
            "accepted_artifact_ids", []
        ))
        output = {
            "satisfied": (
                bool(output_contract_result["satisfied"])
                and set(output_ids).issubset(eligible_ids)
            ),
            "contract_fingerprint": output_contract_result[
                "contract_fingerprint"
            ],
            "accepted_artifact_ids": [
                item for item in output_ids if item in eligible_ids
            ],
        }
    state = {
        "run_id": run_id,
        "run_control": {
            "state": (run_control or {}).get("state", "running"),
            "reason": (run_control or {}).get("reason"),
        },
        "terminal_failure": terminal_failure,
        "blocking_reason": blocking_reason,
        "plan": plan_identity,
        "output_contract": output,
        "verification_required": bool(verification_required),
        "accepted_artifacts": accepted_artifacts,
        "accepted_evidence": accepted_evidence,
        "candidate": copy.deepcopy(normalized["metadata"])
        if normalized else None,
    }
    validate_result_binding_input(state)
    return state, normalized


def safe_result_summary(status, reason=None):
    reason = reason or {
        "completed": "result contract satisfied",
        "blocked": "run blocked",
        "failed": "terminal failure",
        "cancelled": "run cancelled",
        "incomplete": "result contract unsatisfied",
    }[status]
    prefixes = {
        "completed": "任务已完成，但模型候选答案未通过 Result Contract",
        "blocked": "任务被阻止",
        "failed": "任务失败",
        "cancelled": "任务已取消",
        "incomplete": "任务未完成",
    }
    return f"{prefixes[status]}：{reason}。"


def result_fingerprint(result):
    stable = {
        key: result.get(key) for key in RESULT_FIELDS
        if key != "result_fingerprint"
    }
    return _digest(stable)


def create_result(run_id, answer, binding_input, binding_output):
    validate_result_binding_input(binding_input)
    if set(binding_output) != TRANSITION_OUTPUT_FIELDS:
        raise ResultError("result binding output schema 无效")
    status = binding_output["authoritative_status"]
    candidate = copy.deepcopy(binding_input["candidate"])
    if candidate is None:
        # Non-candidate terminal outcomes still have stable empty identity.
        candidate = {
            "answer_length": 0,
            "answer_sha256": hashlib.sha256(b"").hexdigest(),
            "claimed_status": None, "artifact_refs": [],
            "evidence_refs": [], "answer_allowed": True,
            "answer_rejection_reason": None, "contradiction": False,
        }
    candidate["contradiction"] = binding_output["contradiction"]
    result = {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "run_id": run_id,
        "status": status,
        "answer": answer,
        "artifact_ids": list(binding_output["accepted_artifact_ids"]),
        "evidence_ids": list(binding_output["accepted_evidence_ids"]),
        "plan_id": (binding_input["plan"] or {}).get("plan_id"),
        "reason": binding_output["reason"],
        "candidate": candidate,
        "result_fingerprint": "",
    }
    result["result_fingerprint"] = result_fingerprint(result)
    return validate_result(result)


def bind_final_result(binding_input, normalized_candidate=None):
    output = evaluate_result_contract(binding_input)
    original = normalized_candidate["answer"] if normalized_candidate else None
    answer_allowed = bool(
        normalized_candidate is not None
        and binding_input["candidate"]["answer_allowed"]
        and output["authoritative_status"] == "completed"
        and not output["contradiction"]
    )
    if not answer_allowed:
        answer = safe_result_summary(
            output["authoritative_status"], output["reason"]
        )
    else:
        answer = original
    return create_result(binding_input["run_id"], answer, binding_input, output), output


def validate_result(result, verify_fingerprint=True):
    if not isinstance(result, dict) or set(result) != RESULT_FIELDS:
        raise ResultError("Result schema 无效")
    if result["result_schema_version"] != RESULT_SCHEMA_VERSION:
        raise ResultError("unsupported historical result schema")
    if not ID_PATTERN.fullmatch(str(result["run_id"])):
        raise ResultError("Result run_id 无效")
    if result["status"] not in RESULT_STATUSES:
        raise ResultError("Result status 无效")
    allowed, reason = screen_result_answer(result["answer"])
    if not allowed:
        raise ResultError(reason)
    _validate_refs(result["artifact_ids"], ARTIFACT_ID_PATTERN,
                   "Result artifact_ids")
    _validate_refs(result["evidence_ids"], EVIDENCE_ID_PATTERN,
                   "Result evidence_ids")
    if result["plan_id"] is not None and not isinstance(result["plan_id"], str):
        raise ResultError("Result plan_id 无效")
    if result["reason"] is not None and (
        not isinstance(result["reason"], str) or not result["reason"].strip()
    ):
        raise ResultError("Result reason 无效")
    validate_candidate_metadata(result["candidate"])
    if (result["status"] == "completed"
            and not result["candidate"]["contradiction"]
            and result["candidate"]["answer_length"]
            and answer_identity(result["answer"]) != {
                "answer_length": result["candidate"]["answer_length"],
                "answer_sha256": result["candidate"]["answer_sha256"],
            }):
        raise ResultError("accepted candidate answer identity mismatch")
    if (not SHA256_PATTERN.fullmatch(str(result["result_fingerprint"]))
            or verify_fingerprint
            and result["result_fingerprint"] != result_fingerprint(result)):
        raise ResultError("result fingerprint mismatch")
    return result


class ResultStore:
    def __init__(self, directory=RESULT_DIR):
        self.directory = directory

    def _path(self, run_id):
        if not ID_PATTERN.fullmatch(str(run_id)):
            raise ResultError("Result run_id 无效")
        return os.path.join(self.directory, run_id + ".json")

    def save(self, result):
        validate_result(result)
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        path = self._path(result["run_id"])
        payload = canonical_json(result) + b"\n"
        if os.path.exists(path):
            with open(path, "rb") as stream:
                if stream.read() != payload:
                    raise ResultError("immutable Result duplicate conflict")
            return result
        descriptor, temporary = tempfile.mkstemp(
            prefix=".result-", suffix=".tmp", dir=self.directory,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                with open(path, "rb") as stream:
                    if stream.read() != payload:
                        raise ResultError("immutable Result duplicate conflict")
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return result

    def load(self, run_id, verify=True):
        try:
            with open(self._path(run_id), encoding="utf-8") as stream:
                return validate_result(json.load(stream), verify)
        except FileNotFoundError as error:
            raise ResultError(f"Result 不存在：{run_id}") from error
        except (OSError, json.JSONDecodeError) as error:
            raise ResultError("Result corruption") from error


def result_integrity_check(
    run_id, result_directory=RESULT_DIR, artifact_directory=None,
    evidence_directory=None, audit_directory=AUDIT_DIR, resolver=None,
):
    """Check historical identities only; deliberately ignore current files."""
    try:
        result = (
            resolver.load("result", run_id)
            if resolver is not None else ResultStore(result_directory).load(run_id)
        )
        artifact_directory = artifact_directory or os.path.join(
            audit_directory, "artifacts",
        )
        evidence_directory = evidence_directory or os.path.join(
            audit_directory, "evidence",
        )
        run_artifacts = (
            resolver.list("artifact", run_id)
            if resolver is not None else
            ArtifactStore(artifact_directory).list_run(run_id)
        )
        current = {
            item["artifact_id"]: item
            for item in current_artifacts(run_artifacts)
        }
        for artifact_id in result["artifact_ids"]:
            artifact = current.get(artifact_id)
            if (artifact is None or artifact["status"] != "accepted"
                    or not artifact_integrity_check(
                        artifact_id, artifact_directory, evidence_directory,
                        audit_directory, resolver=resolver,
                    )):
                return False
        for evidence_id in result["evidence_ids"]:
            evidence = (
                resolver.load("evidence", evidence_id)
                if resolver is not None else
                EvidenceStore(evidence_directory).load(evidence_id)
            )
            if (evidence["run_id"] != run_id
                    or not evidence_integrity_check(
                        evidence_id, evidence_directory, audit_directory,
                        resolver=resolver,
                    )):
                return False
        from .run_envelope import RunEnvelopeStore, _replay_transition
        envelope = (
            resolver.load("envelope", run_id)
            if resolver is not None else
            RunEnvelopeStore(os.path.join(
                audit_directory, "envelopes",
            )).load(run_id)
        )
        transitions = [
            item for item in envelope["transitions"]
            if item["transition_type"] == "result_binding"
        ]
        if len(transitions) != 1:
            return False
        transition_input = transitions[0]["input"]
        transition_plan = transition_input.get("plan")
        if result["plan_id"] != (
            transition_plan or {}
        ).get("plan_id"):
            return False
        status, replayed = _replay_transition(
            transitions[0], None, audit_directory, resolver=resolver,
        )
        if status != "MATCH" or replayed is None:
            return False
        expected = {
            "authoritative_status": result["status"],
            "accepted_artifact_ids": result["artifact_ids"],
            "accepted_evidence_ids": result["evidence_ids"],
            "reason": result["reason"],
            "contradiction": result["candidate"]["contradiction"],
        }
        if replayed != expected:
            return False
        events = (
            resolver.audit_events(run_id)
            if resolver is not None else read_events(run_id, audit_directory)
        )
        if result["plan_id"] is not None and not any(
            event["event_type"] == "plan_created"
            and (event.get("references") or {}).get("plan_id")
            == result["plan_id"]
            for event in events
        ):
            return False
        candidate_events = [
            event for event in events
            if event["event_type"] == "final_candidate_received"
        ]
        last_candidate_sequence = 0
        if result["candidate"]["answer_length"]:
            if not candidate_events:
                return False
            last_candidate = candidate_events[-1]
            last_candidate_sequence = last_candidate["sequence"]
            candidate_refs = last_candidate.get("references") or {}
            if (candidate_refs.get("answer_length")
                    != result["candidate"]["answer_length"]
                    or candidate_refs.get("answer_sha256")
                    != result["candidate"]["answer_sha256"]
                    or candidate_refs.get("claimed_status")
                    != result["candidate"]["claimed_status"]):
                return False
        if result["candidate"]["answer_length"]:
            rejected = [
                event for event in events
                if event["event_type"] == "final_candidate_rejected"
                and event["sequence"] > last_candidate_sequence
            ]
            if result["candidate"]["contradiction"] != bool(rejected):
                return False
        emitted = [
            event for event in events
            if event["event_type"] == "final_result_emitted"
        ]
        if len(emitted) != 1:
            return False
        refs = emitted[0].get("references") or {}
        identity = answer_identity(result["answer"])
        return (
            refs.get("result_fingerprint") == result["result_fingerprint"]
            and refs.get("answer_length") == identity["answer_length"]
            and refs.get("answer_sha256") == identity["answer_sha256"]
            and refs.get("authoritative_status") == result["status"]
            and refs.get("artifact_ids") == result["artifact_ids"]
            and refs.get("evidence_ids") == result["evidence_ids"]
            and refs.get("contradiction")
            == result["candidate"]["contradiction"]
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False
