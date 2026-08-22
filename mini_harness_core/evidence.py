"""V22 immutable, safe Evidence provenance records.

Purpose: bind claims to safe Observation identities and Harness decisions.
Owns: Evidence schemas, creation, immutable storage, provenance/integrity checks,
and the current-run evidence gate.
Does Not Own: raw Tool output, filesystem refresh, Artifact acceptance, Plan
ownership, execution Authority, or Result status.
Key Invariants: integrity is not freshness; current filesystem claims require
accepted current-run Evidence; historical records never grant Authority.
"""
import json
import os
import re
import uuid

from .audit import AUDIT_DIR, ID_PATTERN, read_events, utc_now
from .integrity import (
    ImmutableRecordConflict, atomic_json_publish, canonical_json_bytes,
    sha256_identity,
)
from .security import SECRET_PATTERNS
from .verification import SHA256_PATTERN, verification_observation_identity

EVIDENCE_SCHEMA_VERSION = 1
EVIDENCE_TYPES = frozenset(("tool_observation", "verification", "reconciliation",
                            "subagent_return", "mcp_observation", "reasoning_result",
                            "termux_observation"))
EVIDENCE_DIR = os.path.join(AUDIT_DIR, "evidence")
EVIDENCE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
FIELDS = frozenset(("evidence_schema_version", "evidence_id", "run_id", "created_at",
                    "evidence_type", "subject", "source", "verification", "freshness",
                    "content_identity", "references", "evidence_fingerprint"))
FORBIDDEN = re.compile(r"(?:api[_-]?key|authorization|bearer|password|private[_-]?key|"
                       r"raw[_-]?(?:stdout|stderr|environment)|(?:full[_-]?)?environment$|"
                       r"(?:full[_-]?)?mcp[_-]?(?:result|body)|"
                       r"(?:tool[_-]?)?output$|(?:response[_-]?)?body$|mcp[_-]?response|"
                       r"hidden[_-]?reasoning|agents?[_-]?(?:body|content)|"
                       r"skills?[_-]?(?:body|content)|memory[_-]?(?:body|content)|"
                       r"^(?:agents?|skills?|memory|project[_-]?instructions)$|"
                       r"chain[_-]?of[_-]?thought|reasoning[_-]?(?:text|content)|"
                       r"\.env\.local|stdout$|stderr$)", re.I)
OBS_FIELDS = frozenset(("observation_event_id", "exit_code", "stdout_length",
                        "stdout_sha256", "stderr_length", "stderr_sha256", "cwd",
                        "verification_target", "denied_by", "status"))


class EvidenceError(ValueError):
    pass


def canonical_json(value):
    return canonical_json_bytes(value)


def evidence_fingerprint(evidence):
    stable = {key: evidence.get(key) for key in (
        "run_id", "evidence_type", "subject", "source", "verification",
        "freshness", "content_identity", "references")}
    return sha256_identity(canonical_json(stable))


def observation_identity(observation, event_id=None):
    """The sole V17/V21 observation digest adapter."""
    identity = verification_observation_identity(observation, event_id)
    for key in ("cwd", "verification_target"):
        if key in observation:
            identity[key] = observation[key]
    return identity


def artifact_ref(path, sha256, size):
    value = {"artifact_type": "workspace_file", "path": path,
             "sha256": sha256, "size": size}
    validate_artifact_ref(value)
    return value


def validate_artifact_ref(value):
    if not isinstance(value, dict) or set(value) != {"artifact_type", "path", "sha256", "size"}:
        raise EvidenceError("ArtifactRef schema 无效")
    path = value["path"]
    if (value["artifact_type"] != "workspace_file" or not isinstance(path, str)
            or not path or os.path.isabs(path) or "\\" in path
            or os.path.normpath(path).replace(os.sep, "/") != path
            or path in {".", ".."} or ".." in path.split("/")
            or any(token in path for token in ("~", "$", "`", "*", "?", "["))):
        raise EvidenceError("ArtifactRef path 必须是 workspace-safe relative path")
    if not SHA256_PATTERN.fullmatch(str(value["sha256"])):
        raise EvidenceError("ArtifactRef sha256 无效")
    if not isinstance(value["size"], int) or isinstance(value["size"], bool) or value["size"] < 0:
        raise EvidenceError("ArtifactRef size 无效")
    return value


def _walk(value):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _screen(value, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) == "result" and path != ("verification",):
                raise EvidenceError("Evidence forbidden full result field")
            if FORBIDDEN.search(str(key)) and not str(key).endswith(("_sha256", "_length")):
                raise EvidenceError("Evidence forbidden raw/secret field")
            _screen(item, path + (str(key),))
    elif isinstance(value, list):
        for item in value:
            _screen(item, path)
    elif isinstance(value, str) and any(pattern.search(value) for pattern in SECRET_PATTERNS):
        raise EvidenceError("Evidence 疑似包含 secret 或 credential")


def _validate_observation(value):
    for item in _walk(value):
        if not isinstance(item, dict) or not {"stdout_length", "stdout_sha256",
                                               "stderr_length", "stderr_sha256"}.issubset(item):
            continue
        if set(item) - OBS_FIELDS:
            raise EvidenceError("Observation identity 包含 raw 或未知字段")
        if (not isinstance(item.get("exit_code"), int)
                or isinstance(item.get("exit_code"), bool)
                or (item.get("observation_event_id") is not None
                    and not isinstance(item.get("observation_event_id"), str))):
            raise EvidenceError("Observation identity exit/event 无效")
        for stream in ("stdout", "stderr"):
            if (not isinstance(item[stream + "_length"], int)
                    or isinstance(item[stream + "_length"], bool)
                    or item[stream + "_length"] < 0
                    or not SHA256_PATTERN.fullmatch(str(item[stream + "_sha256"]))):
                raise EvidenceError("Observation identity 无效")


def validate_evidence(evidence, verify_fingerprint=True):
    if not isinstance(evidence, dict):
        raise EvidenceError("evidence schema 无效")
    if evidence.get("evidence_schema_version") != 1:
        raise EvidenceError("unsupported historical evidence schema")
    if set(evidence) != FIELDS:
        raise EvidenceError("evidence schema 无效")
    if not EVIDENCE_ID_PATTERN.fullmatch(str(evidence["evidence_id"])):
        raise EvidenceError("evidence_id 无效")
    if not ID_PATTERN.fullmatch(str(evidence["run_id"])):
        raise EvidenceError("run_id 无效")
    if evidence["evidence_type"] not in EVIDENCE_TYPES:
        raise EvidenceError("evidence type 无效")
    for key in ("subject", "source", "verification", "freshness", "content_identity", "references"):
        if not isinstance(evidence[key], dict):
            raise EvidenceError(f"{key} 必须是对象")
    subject = evidence["subject"]
    if any(not isinstance(subject.get(key), str) or not subject[key] for key in ("kind", "target", "claim")):
        raise EvidenceError("subject 必须明确 kind/target/claim")
    freshness = evidence["freshness"]
    if set(freshness) != {"scope", "observed_at", "run_id"} or freshness["scope"] not in {"run", "historical"}:
        raise EvidenceError("freshness 无效")
    if (not ID_PATTERN.fullmatch(str(freshness["run_id"]))
            or freshness["run_id"] != evidence["run_id"]
            or not isinstance(freshness["observed_at"], str)
            or not freshness["observed_at"]
            or not isinstance(evidence["created_at"], str)
            or not evidence["created_at"]):
        raise EvidenceError("freshness identity 无效")
    verification = evidence["verification"]
    kind = evidence["evidence_type"]
    source = evidence["source"]
    if kind == "tool_observation" and not {
        "action_id", "logical_action_id", "attempt", "observation_event_id",
        "tool", "run_id",
    }.issubset(source):
        raise EvidenceError("tool observation source provenance 不完整")
    if kind == "tool_observation" and not isinstance(verification.get("accepted"), bool):
        raise EvidenceError("tool observation acceptance 无效")
    if (kind == "tool_observation" and verification["accepted"]
            and verification.get("read_only") is not True):
        raise EvidenceError("accepted tool observation 必须来自 read-only action")
    if kind == "tool_observation" and (
        not isinstance(source.get("attempt"), int)
        or isinstance(source.get("attempt"), bool)
        or source["attempt"] < 1
        or source.get("run_id") != evidence["run_id"]
    ):
        raise EvidenceError("tool observation source identity 无效")
    if kind == "mcp_observation":
        if not {"server", "tool", "observation_event_id"}.issubset(source) or not (
            "action_id" in source or "call_id" in source
        ):
            raise EvidenceError("MCP source provenance 不完整")
        if "verified" in verification or "verified" in source:
            raise EvidenceError("MCP server verified metadata 不可信")
        if any(not isinstance(source.get(key), str) or not source[key]
               for key in ("server", "tool", "observation_event_id")):
            raise EvidenceError("MCP source identity 无效")
        call_identity = source.get("action_id", source.get("call_id"))
        if not isinstance(call_identity, str) or not call_identity:
            raise EvidenceError("MCP call/action identity 无效")
    if kind == "subagent_return" and not {
        "handoff_id", "subagent_run_id", "return_status",
    }.issubset(source):
        raise EvidenceError("subagent source provenance 不完整")
    if kind == "subagent_return" and (
        not isinstance(source.get("handoff_id"), str)
        or not source["handoff_id"]
        or not ID_PATTERN.fullmatch(str(source.get("subagent_run_id")))
        or source.get("return_status") not in {"completed", "blocked", "failed"}
    ):
        raise EvidenceError("subagent source identity 无效")
    if kind == "reasoning_result" and not {
        "model_decision_event_id", "decision_digest",
    }.issubset(source):
        raise EvidenceError("reasoning source provenance 不完整")
    if kind == "reasoning_result" and (
        not isinstance(source.get("model_decision_event_id"), str)
        or not source["model_decision_event_id"]
        or not SHA256_PATTERN.fullmatch(str(source.get("decision_digest")))
    ):
        raise EvidenceError("reasoning source identity 无效")
    if kind == "verification" and not isinstance(verification.get("accepted"), bool):
        raise EvidenceError("verification 缺少 accepted")
    if kind == "verification" and (
        "verification_target" not in verification
        or not isinstance(verification.get("reason"), (str, type(None)))
        or not {"verification_action_id", "observation_event_id", "run_id"}.issubset(source)
    ):
        raise EvidenceError("verification provenance 不完整")
    if kind == "verification" and (
        source.get("run_id") != evidence["run_id"]
        or any(not isinstance(source.get(key), str) or not source[key]
               for key in ("verification_action_id", "observation_event_id"))
    ):
        raise EvidenceError("verification source identity 无效")
    if (kind == "verification" and verification["accepted"] is False
            and not verification.get("reason")):
        raise EvidenceError("rejected verification 必须记录 reason")
    if kind == "reconciliation":
        if verification.get("result") not in {"applied", "not_applied", "uncertain"}:
            raise EvidenceError("reconciliation result 无效")
        if ("target" not in verification
                or not {"source_action_id", "observation_event_id", "run_id"}.issubset(source)
                or source.get("run_id") != evidence["run_id"]):
            raise EvidenceError("reconciliation provenance 不完整")
    if kind == "subagent_return" and (verification.get("candidate") is not True or verification.get("accepted_by_main") is not False):
        raise EvidenceError("subagent evidence 必须是 candidate")
    if kind == "mcp_observation" and verification.get("untrusted_external") is not True:
        raise EvidenceError("MCP evidence 必须是不可信外部结果")
    if kind == "reasoning_result" and verification.get("environment_grounded") is not False:
        raise EvidenceError("reasoning evidence 不能证明环境现实")
    if kind == "reasoning_result" and len(canonical_json(evidence["content_identity"])) > 1_024:
        raise EvidenceError("reasoning result metadata 过大")
    for item in _walk(evidence["content_identity"]):
        if isinstance(item, dict) and "artifact_type" in item:
            validate_artifact_ref(item)
    if "artifact" in evidence["content_identity"]:
        validate_artifact_ref(evidence["content_identity"]["artifact"])
    _screen(evidence)
    if len(canonical_json(evidence)) > 16_384:
        raise EvidenceError("Evidence metadata 过大")
    _validate_observation(evidence)
    if verify_fingerprint and evidence["evidence_fingerprint"] != evidence_fingerprint(evidence):
        raise EvidenceError("evidence fingerprint mismatch")
    return evidence


def create_evidence(run_id, evidence_type, subject, source=None, verification=None,
                    freshness=None, content_identity=None, references=None,
                    evidence_id=None, created_at=None):
    created_at = created_at or utc_now()
    record = {"evidence_schema_version": 1, "evidence_id": evidence_id or uuid.uuid4().hex,
              "run_id": run_id, "created_at": created_at, "evidence_type": evidence_type,
              "subject": subject, "source": source or {}, "verification": verification or {},
              "freshness": freshness or {"scope": "run", "observed_at": created_at, "run_id": run_id},
              "content_identity": content_identity or {}, "references": references or {},
              "evidence_fingerprint": ""}
    record["evidence_fingerprint"] = evidence_fingerprint(record)
    return validate_evidence(record)


def create_tool_observation_evidence(run_id, subject, source, observation,
                                     observation_event_id, accepted=False,
                                     read_only=False, **kwargs):
    source = dict(source)
    source["run_id"] = run_id
    source["observation_event_id"] = observation_event_id
    return create_evidence(
        run_id, "tool_observation", subject, source=source,
        verification={"accepted": bool(accepted), "read_only": bool(read_only)},
        content_identity={"observation": observation_identity(
            observation, observation_event_id,
        )}, **kwargs,
    )


def create_verification_evidence(run_id, subject, verification_target,
                                 verification_action_id, observation,
                                 observation_event_id, accepted, reason=None,
                                 source_action_id=None, references=None,
                                 artifact=None, **kwargs):
    refs = dict(references or {})
    if source_action_id:
        refs["source_action_id"] = source_action_id
    return create_evidence(
        run_id, "verification", subject,
        source={"verification_action_id": verification_action_id,
                "observation_event_id": observation_event_id, "run_id": run_id},
        verification={"accepted": accepted,
                      "reason": reason if accepted or reason else "verification rejected",
                      "verification_target": verification_target},
        content_identity={
            "observation": observation_identity(observation, observation_event_id),
            **({"artifact": artifact} if artifact is not None else {}),
        }, references=refs, **kwargs,
    )


def create_reconciliation_evidence(run_id, subject, source_action_id, target,
                                   result, observation, observation_event_id,
                                   references=None, **kwargs):
    return create_evidence(
        run_id, "reconciliation", subject,
        source={"source_action_id": source_action_id,
                "observation_event_id": observation_event_id, "run_id": run_id},
        verification={"result": result, "target": target},
        content_identity={"observation": observation_identity(
            observation, observation_event_id,
        )}, references=references, **kwargs,
    )


def create_subagent_return_evidence(run_id, subject, handoff_id,
                                    subagent_run_id, return_status,
                                    return_reference=None, **kwargs):
    source = {"handoff_id": handoff_id, "subagent_run_id": subagent_run_id,
              "return_status": return_status}
    if return_reference:
        source["return_reference"] = return_reference
    references = dict(kwargs.pop("references", {}) or {})
    references.setdefault("main_run_id", run_id)
    return create_evidence(
        run_id, "subagent_return", subject, source=source,
        verification={"candidate": True, "accepted_by_main": False},
        references=references, **kwargs,
    )


def create_mcp_observation_evidence(run_id, subject, server, tool,
                                    observation, observation_event_id,
                                    action_id=None, call_id=None, **kwargs):
    source = {"server": server, "tool": tool,
              "observation_event_id": observation_event_id}
    source["action_id" if action_id else "call_id"] = action_id or call_id
    return create_evidence(
        run_id, "mcp_observation", subject, source=source,
        verification={"untrusted_external": True},
        content_identity={"observation": observation_identity(
            observation, observation_event_id,
        )}, **kwargs,
    )


def create_termux_battery_evidence(run_id, capability, observation,
                                   observation_event_id, action_id, **kwargs):
    structured = observation.get("observation") or {}
    percentage = structured.get("percentage")
    return create_evidence(
        run_id, "termux_observation",
        {"kind": "capability", "target": capability,
         "claim": "battery_percentage_observed"},
        source={"capability": capability, "observation_event_id": observation_event_id,
                "action_id": action_id, "run_id": run_id},
        verification={"accepted": observation.get("exit_code") == 0,
                      "read_only": True},
        freshness={"scope": "run", "observed_at": utc_now(), "run_id": run_id},
        content_identity={
            "claim": {"percentage": percentage},
            "observation": observation_identity(observation, observation_event_id),
        }, **kwargs,
    )


def create_termux_notification_evidence(run_id, capability, observation,
                                        observation_event_id, action_id,
                                        arguments_identity, **kwargs):
    structured = observation.get("observation") or {}
    accepted = bool(
        observation.get("exit_code") == 0
        and structured.get("request_accepted") is True
    )


def create_environment_observation_evidence(
    run_id, capability, observation, observation_event_id, action_id,
    arguments_identity, **kwargs,
):
    """Generic historical evidence; claim semantics remain Harness-owned."""
    safe = observation.get("safe_observation") or observation.get("observation") or {}
    accepted = bool(observation.get("exit_code") == 0)
    # Recovery supplies the durable Observation event timestamp so rebuilding
    # the same Evidence cannot manufacture a new freshness identity.
    observed_at = kwargs.get("created_at") or utc_now()
    return create_evidence(
        run_id, "termux_observation",
        {"kind": "capability", "target": capability,
         "claim": "environment_observation_recorded"},
        source={"capability": capability,
                "observation_event_id": observation_event_id,
                "action_id": action_id, "run_id": run_id},
        verification={"accepted": accepted,
                      "effect": observation.get("effect"),
                      "effect_certainty": observation.get("effect_certainty")},
        freshness={"scope": "run", "observed_at": observed_at, "run_id": run_id},
        content_identity={
            "safe_observation": safe,
            "request_arguments": arguments_identity,
            "observation": observation_identity(observation, observation_event_id),
        }, **kwargs,
    )
    return create_evidence(
        run_id, "termux_observation",
        {"kind": "capability", "target": capability,
         "claim": "notification_request_accepted"},
        source={"capability": capability,
                "observation_event_id": observation_event_id,
                "action_id": action_id, "run_id": run_id},
        verification={"accepted": accepted, "read_only": False,
                      "claim_scope": "request_submission"},
        freshness={"scope": "run", "observed_at": utc_now(), "run_id": run_id},
        content_identity={
            "claim": {"request_accepted": accepted},
            "request_arguments": arguments_identity,
            "observation": observation_identity(observation, observation_event_id),
        }, **kwargs,
    )


def create_reasoning_evidence(run_id, subject, model_decision_event_id,
                              decision_digest, metadata=None, **kwargs):
    return create_evidence(
        run_id, "reasoning_result", subject,
        source={"model_decision_event_id": model_decision_event_id,
                "decision_digest": decision_digest},
        verification={"environment_grounded": False},
        content_identity={"result_metadata": metadata or {}}, **kwargs,
    )


class EvidenceStore:
    def __init__(self, directory=EVIDENCE_DIR):
        self.directory = directory

    def _path(self, evidence_id):
        if not EVIDENCE_ID_PATTERN.fullmatch(str(evidence_id)):
            raise EvidenceError("evidence_id 无效")
        return os.path.join(self.directory, evidence_id + ".json")

    def save(self, evidence):
        validate_evidence(evidence)
        try:
            atomic_json_publish(
                self._path(evidence["evidence_id"]), evidence,
                temporary_prefix=".tmp-", temporary_suffix="",
            )
        except ImmutableRecordConflict as error:
            raise EvidenceError("immutable evidence duplicate conflict") from error
        return evidence

    def create(self, *args, **kwargs):
        return self.save(create_evidence(*args, **kwargs))

    def load(self, evidence_id, verify=True):
        try:
            with open(self._path(evidence_id), encoding="utf-8") as stream:
                return validate_evidence(json.load(stream), verify)
        except FileNotFoundError as error:
            raise EvidenceError("unknown evidence") from error


def evidence_gate(evidence, step_id, current_run_id, current_reality=True):
    try:
        validate_evidence(evidence)
    except EvidenceError as error:
        return False, str(error)
    if not ((evidence["subject"]["kind"] == "plan_step"
             and evidence["subject"]["target"] == step_id)
            or evidence["references"].get("step_id") == step_id):
        return False, "irrelevant evidence"
    kind, verification = evidence["evidence_type"], evidence["verification"]
    if kind == "subagent_return":
        return False, "candidate subagent evidence not accepted"
    if kind == "mcp_observation":
        return False, "observation evidence is not Harness acceptance"
    if current_reality and kind == "reasoning_result":
        return False, "reasoning cannot prove filesystem"
    # A valid historical fingerprint proves record integrity, not that the
    # observed environment still exists. Current Reality requires same-Run,
    # run-scoped Evidence; callers must obtain a new Observation after drift.
    if current_reality and (evidence["run_id"] != current_run_id or evidence["freshness"]["scope"] != "run"):
        return False, "fresh current-run evidence required"
    accepted = (verification.get("accepted") is True if kind in {"verification", "tool_observation"} else
                verification.get("result") in {"applied", "not_applied"} if kind == "reconciliation" else
                kind == "reasoning_result" and not current_reality)
    return (True, None) if accepted else (False, "evidence not accepted")


def evidence_integrity_check(evidence_id, evidence_directory=EVIDENCE_DIR,
                             audit_directory=AUDIT_DIR, resolver=None):
    try:
        record = (
            resolver.load("evidence", evidence_id)
            if resolver is not None else
            EvidenceStore(evidence_directory).load(evidence_id)
        )
        has_audit = (
            resolver.exists("audit", record["run_id"])
            if resolver is not None else True
        )
        events = (
            resolver.audit_events(record["run_id"])
            if resolver is not None and has_audit else
            read_events(record["run_id"], audit_directory)
            if resolver is None else []
        )
        if resolver is None and not events:
            return False
        ids = {event.get("event_id") for event in events}
        refs = dict(record["source"]); refs.update(record["references"])
        referenced_values = {
            value for event in events for value in (event.get("references") or {}).values()
            if isinstance(value, str)
        }
        for key, value in refs.items():
            if key.endswith("event_id") and has_audit and value not in ids:
                return False
            if key in {"action_id", "logical_action_id", "source_action_id",
                       "verification_action_id", "handoff_id"} and value not in referenced_values:
                if has_audit:
                    return False
            if key.endswith("evidence_id"):
                (
                    resolver.load("evidence", value)
                    if resolver is not None else
                    EvidenceStore(evidence_directory).load(value)
                )
        subagent_run_id = refs.get("subagent_run_id")
        if subagent_run_id is not None:
            if resolver is None and not read_events(
                subagent_run_id, audit_directory
            ):
                return False
            if (resolver is not None
                    and resolver.exists("audit", subagent_run_id)
                    and not resolver.audit_events(subagent_run_id)):
                return False
        model_request_id = refs.get("model_request_id")
        if model_request_id is not None:
            envelope = None
            if resolver is not None and resolver.exists(
                "envelope", record["run_id"]
            ):
                envelope = resolver.load("envelope", record["run_id"])
            elif resolver is None:
                envelope_path = os.path.join(
                    audit_directory, "envelopes", record["run_id"] + ".json",
                )
                with open(envelope_path, encoding="utf-8") as stream:
                    envelope = json.load(stream)
            if envelope is not None and model_request_id not in {
                request.get("request_id") for request in envelope.get("requests", [])
            }:
                return False
        if record["evidence_type"] in {"tool_observation", "verification", "reconciliation", "mcp_observation"}:
            if not isinstance(refs.get("observation_event_id"), str):
                return False
        return True
    except (EvidenceError, ValueError, OSError, TypeError):
        return False


def evidence_trace(record, audit_directory=AUDIT_DIR, resolver=None):
    validate_evidence(record)
    try:
        events = (
            resolver.audit_events(record["run_id"])
            if resolver is not None else
            read_events(record["run_id"], audit_directory)
        )
    except ValueError:
        events = []
    by_id = {event.get("event_id"): event for event in events}
    refs = dict(record["source"]); refs.update(record["references"])
    lines = [f"Evidence {record['evidence_id']}"]
    for label, keys in (("Verification", ("verification_evidence_id",)),
                        ("Observation Event", ("observation_event_id",)),
                        ("Action", ("verification_action_id", "source_action_id", "action_id")),
                        ("Model Request", ("model_request_id",)),
                        ("Policy/Approval", ("approval_event_id",))):
        value = next((refs[key] for key in keys if refs.get(key)), None)
        if label == "Verification" and record["evidence_type"] == "verification":
            value = record["evidence_id"]
        detail = f"{value} ({by_id[value]['event_type']})" if value in by_id else (value or "unavailable")
        lines.append(f"<- {label}: {detail}")
    if record["evidence_type"] == "subagent_return":
        for label, key in (("Subagent Run", "subagent_run_id"), ("Handoff", "handoff_id"), ("Main Run", "main_run_id")):
            lines.append(f"<- {label}: {refs.get(key, 'unavailable')}")
    return lines
