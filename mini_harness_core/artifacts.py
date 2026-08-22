"""V23 Harness-owned workspace Artifact lifecycle and Output Contract.

Artifact records describe historical file versions.  Contract replay consumes
only immutable Artifact/Evidence identities and never reads the current file.
"""

import copy
import hashlib
import json
import os
import re
import uuid

from .audit import AUDIT_DIR, ID_PATTERN, read_events, utc_now
from .evidence import EvidenceError, EvidenceStore, evidence_trace
from .integrity import (
    ImmutableRecordConflict, atomic_json_publish, canonical_json_bytes,
    sha256_identity,
)
from .security import SECRET_PATTERNS
from .verification import SHA256_PATTERN


ARTIFACT_SCHEMA_VERSION = 1
OUTPUT_CONTRACT_SCHEMA_VERSION = 1
ARTIFACT_DIR = os.path.join(AUDIT_DIR, "artifacts")
OUTPUT_CONTRACT_DIR = os.path.join(AUDIT_DIR, "output_contracts")
ARTIFACT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
ARTIFACT_STATUSES = frozenset({
    "proposed", "materialized", "verified", "accepted", "rejected",
})
REQUIREMENTS = frozenset({
    "exists", "non_empty", "content_identity", "verified",
})
ARTIFACT_FIELDS = frozenset({
    "artifact_schema_version", "artifact_id", "run_id", "created_at",
    "artifact_type", "path", "status", "content_identity", "producer",
    "evidence_ids", "contract", "references", "supersedes_artifact_id",
    "artifact_fingerprint",
})
PRODUCER_FIELDS = frozenset({
    "kind", "run_id", "action_id", "capability", "step_id",
    "model_request_id", "model_decision_event_id", "subagent_run_id",
    "handoff_id", "server", "tool",
})
FORBIDDEN_KEYS = re.compile(
    r"(?:api[_-]?key|authorization|bearer|password|private[_-]?key|"
    r"raw[_-]?(?:stdout|stderr|content|output)|file[_-]?content|body|"
    r"hidden[_-]?reasoning|chain[_-]?of[_-]?thought)", re.I,
)
SECRET_FILENAMES = frozenset({
    ".env", ".env.local", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
})


class ArtifactError(ValueError):
    pass


def canonical_json(value):
    return canonical_json_bytes(value)


def _digest(value):
    return sha256_identity(canonical_json(value))


def validate_artifact_path(path, workspace=None, require_current=False):
    """Return one normalized workspace-relative, non-secret-bearing path."""
    if (not isinstance(path, str) or not path or os.path.isabs(path)
            or "\\" in path or path in {".", ".."}
            or os.path.normpath(path).replace(os.sep, "/") != path
            or ".." in path.split("/")
            or any(token in path for token in ("~", "$", "`", "*", "?", "["))):
        raise ArtifactError("Artifact path 必须是 workspace-safe relative path")
    parts = path.lower().split("/")
    basename = parts[-1]
    if (basename in SECRET_FILENAMES or basename.endswith((".pem", ".key", ".p12", ".pfx"))
            or "private_key" in basename or "private-key" in basename
            or parts[0] == ".audit"):
        raise ArtifactError("Artifact path 指向 secret-bearing 或 Harness-owned 文件")
    if workspace is not None:
        root = os.path.realpath(workspace)
        candidate = os.path.realpath(os.path.join(root, path))
        try:
            within = os.path.commonpath((root, candidate)) == root
        except ValueError:
            within = False
        if not within:
            raise ArtifactError("Artifact path 逃逸 workspace")
        if require_current and (not os.path.isfile(candidate) or os.path.islink(os.path.join(root, path))):
            raise ArtifactError("Artifact 必须是当前 workspace 中的普通文件")
    return path


def _screen(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if FORBIDDEN_KEYS.search(str(key)):
                raise ArtifactError("Artifact record 禁止保存正文、原始输出或 secret 字段")
            _screen(item)
    elif isinstance(value, list):
        for item in value:
            _screen(item)
    elif isinstance(value, str) and any(pattern.search(value) for pattern in SECRET_PATTERNS):
        raise ArtifactError("Artifact record 疑似包含 secret 或 credential")


def artifact_fingerprint(record):
    stable = {
        key: record.get(key) for key in ARTIFACT_FIELDS
        if key not in {"artifact_id", "created_at", "artifact_fingerprint"}
    }
    return _digest(stable)


def _validate_producer(producer, run_id):
    if not isinstance(producer, dict) or set(producer) != PRODUCER_FIELDS:
        raise ArtifactError("Artifact producer schema 无效")
    if producer["kind"] not in {"tool_action", "main_grounding", "subagent_candidate"}:
        raise ArtifactError("Artifact producer kind 无效")
    if producer["run_id"] != run_id or not ID_PATTERN.fullmatch(str(run_id)):
        raise ArtifactError("Artifact producer run identity 无效")
    for key in PRODUCER_FIELDS - {"kind", "run_id"}:
        if producer[key] is not None and (not isinstance(producer[key], str) or not producer[key]):
            raise ArtifactError(f"Artifact producer {key} 无效")
    if producer["kind"] in {"tool_action", "main_grounding"} and not all(
        producer[key] for key in ("action_id", "capability")
    ):
        raise ArtifactError("Main-produced Artifact 缺少 action/capability provenance")
    if producer["kind"] == "subagent_candidate" and not all(
        producer[key] for key in ("subagent_run_id", "handoff_id")
    ):
        raise ArtifactError("Subagent candidate provenance 不完整")
    return producer


def create_producer(run_id, kind="tool_action", action_id=None,
                    capability=None, step_id=None, model_request_id=None,
                    model_decision_event_id=None, subagent_run_id=None,
                    handoff_id=None, server=None, tool=None):
    value = {
        "kind": kind, "run_id": run_id, "action_id": action_id,
        "capability": capability, "step_id": step_id,
        "model_request_id": model_request_id,
        "model_decision_event_id": model_decision_event_id,
        "subagent_run_id": subagent_run_id, "handoff_id": handoff_id,
        "server": server, "tool": tool,
    }
    return _validate_producer(value, run_id)


def validate_required_artifact(value):
    allowed = {"name", "artifact_type", "path", "requirements", "step_id"}
    if not isinstance(value, dict) or not set(value).issubset(allowed) or not {
        "name", "artifact_type", "path", "requirements",
    }.issubset(value):
        raise ArtifactError("required artifact schema 无效")
    if (not isinstance(value["name"], str) or not value["name"]
            or value["artifact_type"] != "workspace_file"):
        raise ArtifactError("required artifact identity 无效")
    validate_artifact_path(value["path"])
    requirements = value["requirements"]
    if (not isinstance(requirements, list) or not requirements
            or len(requirements) != len(set(requirements))
            or not set(requirements).issubset(REQUIREMENTS)):
        raise ArtifactError("Output Contract requirements 无效")
    if value.get("step_id") is not None and (
        not isinstance(value["step_id"], str) or not value["step_id"]
    ):
        raise ArtifactError("Output Contract step_id 无效")
    return value


def output_contract_fingerprint(contract):
    return _digest({"required_artifacts": contract["required_artifacts"]})


def create_output_contract(run_id, specification, created_at=None):
    if not ID_PATTERN.fullmatch(str(run_id)) or not isinstance(specification, dict):
        raise ArtifactError("Output Contract identity 无效")
    if set(specification) == {"required_artifacts"}:
        required = specification["required_artifacts"]
    elif set(specification) == {
        "output_contract_schema_version", "run_id", "created_at",
        "required_artifacts", "contract_fingerprint",
    }:
        record = validate_output_contract(copy.deepcopy(specification))
        if record["run_id"] != run_id:
            raise ArtifactError("Output Contract 不能跨 Run 复用")
        return record
    else:
        raise ArtifactError("Output Contract schema 无效")
    if not isinstance(required, list) or not required:
        raise ArtifactError("Output Contract 必须声明 required_artifacts")
    normalized = []
    names = set()
    for item in required:
        item = copy.deepcopy(item)
        validate_required_artifact(item)
        if item["name"] in names:
            raise ArtifactError("required artifact name 必须唯一")
        names.add(item["name"])
        normalized.append(item)
    record = {
        "output_contract_schema_version": 1, "run_id": run_id,
        "created_at": created_at or utc_now(), "required_artifacts": normalized,
        "contract_fingerprint": "",
    }
    record["contract_fingerprint"] = output_contract_fingerprint(record)
    return validate_output_contract(record)


def validate_output_contract(contract, verify_fingerprint=True):
    fields = {"output_contract_schema_version", "run_id", "created_at",
              "required_artifacts", "contract_fingerprint"}
    if not isinstance(contract, dict) or set(contract) != fields:
        raise ArtifactError("Output Contract schema 无效")
    if contract["output_contract_schema_version"] != 1:
        raise ArtifactError("unsupported historical output contract schema")
    if not ID_PATTERN.fullmatch(str(contract["run_id"])):
        raise ArtifactError("Output Contract run_id 无效")
    if not isinstance(contract["created_at"], str) or not contract["created_at"]:
        raise ArtifactError("Output Contract created_at 无效")
    names = []
    for item in contract["required_artifacts"]:
        validate_required_artifact(item)
        names.append(item["name"])
    if not names or len(names) != len(set(names)):
        raise ArtifactError("Output Contract required artifacts 无效")
    if (not SHA256_PATTERN.fullmatch(str(contract["contract_fingerprint"]))
            or verify_fingerprint and contract["contract_fingerprint"] != output_contract_fingerprint(contract)):
        raise ArtifactError("Output Contract fingerprint mismatch")
    _screen(contract)
    return contract


def validate_artifact(record, verify_fingerprint=True):
    if not isinstance(record, dict) or set(record) != ARTIFACT_FIELDS:
        raise ArtifactError("Artifact schema 无效")
    if record["artifact_schema_version"] != ARTIFACT_SCHEMA_VERSION:
        raise ArtifactError("unsupported historical artifact schema")
    if not ARTIFACT_ID_PATTERN.fullmatch(str(record["artifact_id"])):
        raise ArtifactError("artifact_id 无效")
    if not ID_PATTERN.fullmatch(str(record["run_id"])):
        raise ArtifactError("Artifact run_id 无效")
    if record["artifact_type"] != "workspace_file":
        raise ArtifactError("V23 只管理 workspace_file")
    validate_artifact_path(record["path"])
    if record["status"] not in ARTIFACT_STATUSES:
        raise ArtifactError("Artifact status 无效")
    identity = record["content_identity"]
    if not isinstance(identity, dict) or set(identity) != {"sha256", "size"}:
        raise ArtifactError("Artifact content identity schema 无效")
    if record["status"] == "proposed":
        if identity != {"sha256": None, "size": None}:
            raise ArtifactError("proposed Artifact 不得伪造 content identity")
    elif (not SHA256_PATTERN.fullmatch(str(identity["sha256"]))
          or not isinstance(identity["size"], int)
          or isinstance(identity["size"], bool) or identity["size"] < 0):
        raise ArtifactError("Artifact content identity fields 无效")
    _validate_producer(record["producer"], record["run_id"])
    evidence_ids = record["evidence_ids"]
    if (not isinstance(evidence_ids, list) or len(evidence_ids) != len(set(evidence_ids))
            or not all(re.fullmatch(r"[0-9a-f]{32}", str(item)) for item in evidence_ids)):
        raise ArtifactError("Artifact evidence_ids 无效")
    if not isinstance(record["contract"], dict) or not isinstance(record["references"], dict):
        raise ArtifactError("Artifact contract/references 无效")
    if record["contract"]:
        validate_required_artifact(record["contract"])
    previous = record["supersedes_artifact_id"]
    if previous is not None and (not ARTIFACT_ID_PATTERN.fullmatch(str(previous))
                                 or previous == record["artifact_id"]):
        raise ArtifactError("Artifact supersedes relation 无效")
    if not isinstance(record["created_at"], str) or not record["created_at"]:
        raise ArtifactError("Artifact created_at 无效")
    _screen(record)
    if len(canonical_json(record)) > 16_384:
        raise ArtifactError("Artifact metadata 过大")
    if (not SHA256_PATTERN.fullmatch(str(record["artifact_fingerprint"]))
            or verify_fingerprint and record["artifact_fingerprint"] != artifact_fingerprint(record)):
        raise ArtifactError("artifact fingerprint mismatch")
    if record["producer"]["kind"] == "subagent_candidate" and record["status"] == "accepted":
        raise ArtifactError("Subagent candidate 不能直接 accepted")
    return record


def create_artifact(run_id, path, status, content_identity, producer,
                    evidence_ids=None, contract=None, references=None,
                    supersedes_artifact_id=None, artifact_id=None, created_at=None):
    record = {
        "artifact_schema_version": 1,
        "artifact_id": artifact_id or uuid.uuid4().hex,
        "run_id": run_id, "created_at": created_at or utc_now(),
        "artifact_type": "workspace_file", "path": path, "status": status,
        "content_identity": copy.deepcopy(content_identity),
        "producer": copy.deepcopy(producer),
        "evidence_ids": list(evidence_ids or []),
        "contract": copy.deepcopy(contract or {}),
        "references": copy.deepcopy(references or {}),
        "supersedes_artifact_id": supersedes_artifact_id,
        "artifact_fingerprint": "",
    }
    record["artifact_fingerprint"] = artifact_fingerprint(record)
    return validate_artifact(record)


def observe_workspace_file(path, workspace=None):
    """Create fresh identity metadata; bytes are never returned or persisted."""
    root = os.path.realpath(workspace or os.getcwd())
    validate_artifact_path(path, root, require_current=True)
    digest = hashlib.sha256()
    size = 0
    with open(os.path.join(root, path), "rb") as stream:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return {"sha256": digest.hexdigest(), "size": size}


def _evidence_identity(evidence):
    artifact = evidence.get("content_identity", {}).get("artifact")
    return {
        "evidence_id": evidence.get("evidence_id"),
        "evidence_fingerprint": evidence.get("evidence_fingerprint"),
        "run_id": evidence.get("run_id"),
        "evidence_type": evidence.get("evidence_type"),
        "subject": copy.deepcopy(evidence.get("subject")),
        "accepted": evidence.get("verification", {}).get("accepted") is True,
        "artifact_identity": copy.deepcopy(artifact),
    }


def artifact_contract_transition_input(artifact, evidences, requirement):
    validate_artifact(artifact)
    validate_required_artifact(requirement)
    return {
        "artifact_identity": {
            "artifact_id": artifact["artifact_id"], "run_id": artifact["run_id"],
            "artifact_type": artifact["artifact_type"], "path": artifact["path"],
            "sha256": artifact["content_identity"]["sha256"],
            "size": artifact["content_identity"]["size"],
            "producer_kind": artifact["producer"]["kind"],
        },
        "evidence_identities": [_evidence_identity(item) for item in evidences],
        "contract_requirement": copy.deepcopy(requirement),
        "historical_records": True,
    }


def replay_artifact_contract_transition(inputs):
    """Deterministically replay acceptance from historical identities only."""
    if not isinstance(inputs, dict) or set(inputs) != {
        "artifact_identity", "evidence_identities", "contract_requirement",
        "historical_records",
    } or inputs["historical_records"] is not True:
        raise ArtifactError("artifact contract replay input 无效")
    artifact = inputs["artifact_identity"]
    required = inputs["contract_requirement"]
    validate_required_artifact(required)
    if not isinstance(artifact, dict) or set(artifact) != {
        "artifact_id", "run_id", "artifact_type", "path", "sha256", "size",
        "producer_kind",
    }:
        raise ArtifactError("historical Artifact identity 无效")
    unsatisfied = []
    exact = (artifact["artifact_type"] == required["artifact_type"]
             and artifact["path"] == required["path"])
    if not exact:
        unsatisfied.append("exact_path")
    has_identity = (SHA256_PATTERN.fullmatch(str(artifact["sha256"])) is not None
                    and isinstance(artifact["size"], int)
                    and not isinstance(artifact["size"], bool)
                    and artifact["size"] >= 0)
    if "exists" in required["requirements"] and not has_identity:
        unsatisfied.append("exists")
    if "non_empty" in required["requirements"] and (
        not has_identity or artifact["size"] == 0
    ):
        unsatisfied.append("non_empty")
    if "content_identity" in required["requirements"] and not has_identity:
        unsatisfied.append("content_identity")
    relevant_verification = False
    related_verification_seen = False
    for evidence in inputs["evidence_identities"]:
        if not isinstance(evidence, dict):
            continue
        ref = evidence.get("artifact_identity")
        subject = evidence.get("subject") or {}
        related = bool(
            evidence.get("run_id") == artifact["run_id"]
            and evidence.get("evidence_type") == "verification"
            and ((isinstance(ref, dict) and ref.get("path") == artifact["path"])
                 or subject.get("target") == artifact["path"])
        )
        related_verification_seen = related_verification_seen or related
        relevant = bool(
            related
            and evidence.get("accepted") is True
            and isinstance(ref, dict)
            and ref.get("artifact_type") == artifact["artifact_type"]
            and ref.get("path") == artifact["path"]
            and ref.get("sha256") == artifact["sha256"]
            and ref.get("size") == artifact["size"]
        )
        relevant_verification = relevant_verification or relevant
    if related_verification_seen and not relevant_verification:
        unsatisfied.append("verification_failed")
    if "verified" in required["requirements"] and not relevant_verification:
        unsatisfied.append("verified")
    if artifact.get("producer_kind") == "subagent_candidate":
        unsatisfied.append("main_acceptance")
    unsatisfied = list(dict.fromkeys(unsatisfied))
    return {
        "accepted": not unsatisfied,
        "status": "accepted" if not unsatisfied else "rejected",
        "reason": None if not unsatisfied else "unsatisfied: " + ",".join(unsatisfied),
        "unsatisfied_requirements": unsatisfied,
    }


def evaluate_artifact_contract(artifact, evidences, requirement):
    inputs = artifact_contract_transition_input(artifact, evidences, requirement)
    return inputs, replay_artifact_contract_transition(inputs)


class _ImmutableJSONStore:
    prefix = ".tmp-"

    def _atomic_save(self, path, value, validator):
        validator(value)
        try:
            atomic_json_publish(
                path, value, temporary_prefix=self.prefix,
                temporary_suffix="",
            )
        except ImmutableRecordConflict as error:
            raise ArtifactError("immutable record duplicate conflict") from error
        return value


class ArtifactStore(_ImmutableJSONStore):
    def __init__(self, directory=ARTIFACT_DIR):
        self.directory = directory

    def _path(self, artifact_id):
        if not ARTIFACT_ID_PATTERN.fullmatch(str(artifact_id)):
            raise ArtifactError("artifact_id 无效")
        return os.path.join(self.directory, artifact_id + ".json")

    def save(self, record):
        return self._atomic_save(self._path(record.get("artifact_id")), record,
                                 validate_artifact)

    def load(self, artifact_id, verify=True):
        try:
            with open(self._path(artifact_id), encoding="utf-8") as stream:
                return validate_artifact(json.load(stream), verify)
        except FileNotFoundError as error:
            raise ArtifactError("unknown artifact") from error

    def list_run(self, run_id):
        if not ID_PATTERN.fullmatch(str(run_id)):
            raise ArtifactError("run_id 无效")
        try:
            names = sorted(os.listdir(self.directory))
        except FileNotFoundError:
            return []
        records = []
        for name in names:
            if ARTIFACT_ID_PATTERN.fullmatch(name[:-5] if name.endswith(".json") else ""):
                record = self.load(name[:-5])
                if record["run_id"] == run_id:
                    records.append(record)
        return sorted(records, key=lambda item: item["created_at"])

    def list_all(self):
        try:
            names = sorted(os.listdir(self.directory))
        except FileNotFoundError:
            return []
        records = []
        for name in names:
            artifact_id = name[:-5] if name.endswith(".json") else ""
            if ARTIFACT_ID_PATTERN.fullmatch(artifact_id):
                records.append(self.load(artifact_id))
        return sorted(records, key=lambda item: item["created_at"])

    def latest_for_path(self, run_id, path):
        matches = [item for item in self.list_run(run_id) if item["path"] == path]
        return matches[-1] if matches else None


class OutputContractStore(_ImmutableJSONStore):
    def __init__(self, directory=OUTPUT_CONTRACT_DIR):
        self.directory = directory

    def _path(self, run_id):
        if not ID_PATTERN.fullmatch(str(run_id)):
            raise ArtifactError("run_id 无效")
        return os.path.join(self.directory, run_id + ".json")

    def save(self, contract):
        return self._atomic_save(self._path(contract.get("run_id")), contract,
                                 validate_output_contract)

    def load(self, run_id, verify=True):
        try:
            with open(self._path(run_id), encoding="utf-8") as stream:
                return validate_output_contract(json.load(stream), verify)
        except FileNotFoundError as error:
            raise ArtifactError("run 没有 Output Contract") from error


def current_artifacts(records):
    superseded = {item["supersedes_artifact_id"] for item in records
                  if item["supersedes_artifact_id"] is not None}
    return [item for item in records if item["artifact_id"] not in superseded]


def outputs_status(run_id, contract_store=None, artifact_store=None,
                   evidence_store=None):
    contract_store = contract_store or OutputContractStore()
    artifact_store = artifact_store or ArtifactStore()
    evidence_store = evidence_store or EvidenceStore()
    contract = contract_store.load(run_id)
    records = current_artifacts(artifact_store.list_run(run_id))
    required_output = []
    accepted_ids = []
    for required in contract["required_artifacts"]:
        candidates = [item for item in records if item["path"] == required["path"]]
        accepted = []
        unsatisfied = list(required["requirements"])
        for candidate in reversed(candidates):
            evidences = []
            try:
                evidences = [evidence_store.load(item) for item in candidate["evidence_ids"]]
            except EvidenceError:
                continue
            _inputs, result = evaluate_artifact_contract(candidate, evidences, required)
            if result["accepted"] and candidate["status"] == "accepted":
                accepted = [candidate["artifact_id"]]
                unsatisfied = []
                break
            unsatisfied = (result["unsatisfied_requirements"]
                           if not result["accepted"] else ["accepted_status"])
        accepted_ids.extend(accepted)
        required_output.append({
            "name": required["name"], "path": required["path"],
            "requirements": list(required["requirements"]),
            "accepted_artifact_ids": accepted,
            "unsatisfied_requirements": unsatisfied,
        })
    return {
        "run_id": run_id, "contract_fingerprint": contract["contract_fingerprint"],
        "required_artifacts": required_output,
        "accepted_artifact_ids": accepted_ids,
        "satisfied": all(not item["unsatisfied_requirements"] for item in required_output),
    }


def current_output_contract_gate(run_id, contract_store=None,
                                 artifact_store=None, evidence_store=None,
                                 workspace=None):
    """Add Current Reality freshness to the historical output summary.

    This never mutates an old Artifact.  A mismatch means a new observation,
    Evidence, and Artifact version are required before completion.
    """
    artifact_store = artifact_store or ArtifactStore()
    status = outputs_status(
        run_id, contract_store, artifact_store, evidence_store
    )
    by_id = {item["artifact_id"]: item for item in artifact_store.list_run(run_id)}
    for required in status["required_artifacts"]:
        for artifact_id in list(required["accepted_artifact_ids"]):
            artifact = by_id[artifact_id]
            try:
                current = observe_workspace_file(artifact["path"], workspace)
            except (ArtifactError, OSError):
                current = None
            if current != artifact["content_identity"]:
                required["unsatisfied_requirements"].append(
                    "current_content_identity"
                )
    status["satisfied"] = all(
        not item["unsatisfied_requirements"]
        for item in status["required_artifacts"]
    )
    status["current_reality_checked"] = True
    return status


def _artifact_integrity_check(artifact_id, artifact_directory,
                              evidence_directory, audit_directory, seen,
                              resolver=None):
    try:
        record = (
            resolver.load("artifact", artifact_id)
            if resolver is not None else
            ArtifactStore(artifact_directory).load(artifact_id)
        )
        if artifact_id in seen:
            return False
        seen.add(artifact_id)
        for evidence_id in record["evidence_ids"]:
            evidence = (
                resolver.load("evidence", evidence_id)
                if resolver is not None else
                EvidenceStore(evidence_directory or os.path.join(
                    audit_directory, "evidence"
                )).load(evidence_id)
            )
            if evidence["run_id"] != record["run_id"]:
                return False
        if record["supersedes_artifact_id"] is not None:
            previous = (
                resolver.load("artifact", record["supersedes_artifact_id"])
                if resolver is not None else
                ArtifactStore(artifact_directory).load(
                    record["supersedes_artifact_id"]
                )
            )
            if (previous["artifact_type"] != "workspace_file"
                    or record["artifact_type"] != "workspace_file"
                    or previous["path"] != record["path"]
                    or previous["content_identity"] == record["content_identity"]
                    or not _artifact_integrity_check(
                        previous["artifact_id"], artifact_directory,
                        evidence_directory, audit_directory, seen, resolver,
                    )):
                return False
        if (resolver is not None
                and not resolver.exists("audit", record["run_id"])):
            return True  # Cross-run immutable record without its full trace.
        events = (
            resolver.audit_events(record["run_id"])
            if resolver is not None else
            read_events(record["run_id"], audit_directory)
        )
        linked = [event for event in events if (event.get("references") or {}).get(
            "artifact_id"
        ) == artifact_id]
        return bool(linked) and all(
            (event.get("references") or {}).get("artifact_fingerprint")
            == record["artifact_fingerprint"]
            and (event.get("references") or {}).get("path") == record["path"]
            and (event.get("references") or {}).get("status") == record["status"]
            and (event.get("references") or {}).get("evidence_ids")
            == record["evidence_ids"]
            for event in linked
        )
    except (ArtifactError, EvidenceError, OSError, ValueError, TypeError):
        return False


def artifact_integrity_check(artifact_id, artifact_directory=ARTIFACT_DIR,
                             evidence_directory=None, audit_directory=AUDIT_DIR,
                             resolver=None):
    """Check historical record links only; deliberately ignore current files."""
    return _artifact_integrity_check(
        artifact_id, artifact_directory, evidence_directory, audit_directory,
        set(), resolver,
    )


def validate_supersession(record, current_run_id, artifact_store=None,
                          evidence_directory=None, audit_directory=AUDIT_DIR):
    """Validate one immutable new -> old logical workspace version link."""
    validate_artifact(record)
    artifact_store = artifact_store or ArtifactStore()
    previous_id = record["supersedes_artifact_id"]
    if previous_id is None:
        return None
    if record["run_id"] != current_run_id:
        raise ArtifactError("new Artifact 不属于 current Run")
    if previous_id == record["artifact_id"]:
        raise ArtifactError("Artifact supersession 不允许 self reference")
    try:
        previous = artifact_store.load(previous_id)
    except ArtifactError as error:
        raise ArtifactError("superseded Artifact 不存在或损坏") from error
    if (record["artifact_type"] != "workspace_file"
            or previous["artifact_type"] != "workspace_file"):
        raise ArtifactError("supersession 只支持 workspace_file")
    if previous["path"] != record["path"]:
        raise ArtifactError("supersession path 必须完全相同")
    if previous["content_identity"] == record["content_identity"]:
        raise ArtifactError("相同 content identity 不需要 supersede")
    if not artifact_integrity_check(
        previous_id, artifact_store.directory, evidence_directory,
        audit_directory,
    ):
        raise ArtifactError("old Artifact integrity check 失败")
    seen = {record["artifact_id"]}
    cursor = previous
    while cursor is not None:
        cursor_id = cursor["artifact_id"]
        if cursor_id in seen:
            raise ArtifactError("Artifact supersession cycle")
        seen.add(cursor_id)
        parent_id = cursor["supersedes_artifact_id"]
        cursor = artifact_store.load(parent_id) if parent_id else None
    return previous


def select_supersession(record, current_run_id, artifact_store=None,
                        evidence_directory=None, audit_directory=AUDIT_DIR):
    """Select the latest current same-path historical version across Runs."""
    validate_artifact(record)
    artifact_store = artifact_store or ArtifactStore()
    candidates = current_artifacts([
        item for item in artifact_store.list_all()
        if item["path"] == record["path"]
        and item["artifact_id"] != record["artifact_id"]
    ])
    if not candidates:
        return None
    previous = candidates[-1]
    if previous["content_identity"] == record["content_identity"]:
        return None
    candidate = copy.deepcopy(record)
    candidate["supersedes_artifact_id"] = previous["artifact_id"]
    candidate["artifact_fingerprint"] = artifact_fingerprint(candidate)
    validate_supersession(
        candidate, current_run_id, artifact_store, evidence_directory,
        audit_directory,
    )
    return previous


def artifact_trace(record, evidence_store=None, audit_directory=AUDIT_DIR,
                   resolver=None):
    validate_artifact(record)
    if resolver is None:
        evidence_store = evidence_store or EvidenceStore(os.path.join(
            audit_directory, "evidence"
        ))
    lines = [f"Artifact {record['artifact_id']} ({record['status']})"]
    for evidence_id in record["evidence_ids"]:
        try:
            evidence = (
                resolver.load("evidence", evidence_id)
                if resolver is not None else evidence_store.load(evidence_id)
            )
            lines.extend("  " + line for line in evidence_trace(
                evidence, audit_directory, resolver=resolver
            ))
        except (EvidenceError, ValueError):
            lines.append(f"<- Evidence {evidence_id}: unavailable")
    producer = record["producer"]
    lines.append(f"<- Producer Action: {producer.get('action_id') or 'unavailable'}")
    lines.append(f"<- Model Request: {producer.get('model_request_id') or 'unavailable'}")
    if producer["kind"] == "subagent_candidate":
        lines.append(f"<- Subagent Run: {producer['subagent_run_id']}")
        lines.append(f"<- Handoff: {producer['handoff_id']}")
    if record["supersedes_artifact_id"]:
        lines.append(f"<- Supersedes Artifact: {record['supersedes_artifact_id']}")
    return lines
