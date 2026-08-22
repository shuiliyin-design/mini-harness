"""V25 portable, read-only historical Run evidence bundles.

Purpose: export and resolve a typed, tamper-evident historical reference closure.
Owns: Bundle schema/index, safe export screening, local/bundle resolvers,
relationship checks, offline show/check, and deterministic Bundle replay.
Does Not Own: Session/resume, Current Reality, Approval, Provider, Tool, MCP,
Subagent, workspace mutation, or execution Authority.
Key Invariants: portable history is not portable Authority; only indexed regular
files are readable; replay performs zero external execution.
"""

import json
import os
import re
import shutil
import stat
import tempfile

from .artifacts import (
    ARTIFACT_ID_PATTERN, ArtifactError, artifact_integrity_check,
    validate_artifact, validate_output_contract,
)
from .audit import ACTORS, AUDIT_DIR, ID_PATTERN, utc_now
from .evidence import (
    EVIDENCE_ID_PATTERN, EvidenceError, evidence_integrity_check,
    validate_evidence,
)
from .policy_snapshot import (
    FINGERPRINT_PATTERN, policy_fingerprint, validate_snapshot,
)
from .result import (
    ResultError, result_integrity_check, validate_result,
)
from .run_envelope import (
    RunEnvelopeError, harness_replay_check, validate_envelope,
)
from .run_manifest import RunManifestError, validate_manifest
from .security import SECRET_PATTERNS
from .historical_types import canonical_json_bytes, sha256_identity


BUNDLE_SCHEMA_VERSION = 1
BUNDLE_STATUSES = frozenset({"result", "forensic"})
OBJECT_TYPES = frozenset({
    "audit", "policy_snapshot", "manifest", "envelope", "evidence",
    "artifact", "output_contract", "result",
})
OBJECT_DIRECTORIES = {
    "audit": "audit", "policy_snapshot": "policies",
    "manifest": "manifests", "envelope": "envelopes",
    "evidence": "evidence", "artifact": "artifacts",
    "output_contract": "output_contracts", "result": "results",
}
OBJECT_EXTENSIONS = {"audit": ".jsonl", **{
    name: ".json" for name in OBJECT_TYPES if name != "audit"
}}
INDEX_FIELDS = frozenset({
    "object_type", "logical_id", "path", "sha256", "size",
})
CROSS_RUN_FIELDS = frozenset({"vendored_cross_run", "source_run_id"})
BUNDLE_FIELDS = frozenset({
    "bundle_schema_version", "run_id", "created_at", "bundle_status",
    "bundle_fingerprint", "root", "objects",
})
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
FORBIDDEN_EXPORT_KEYS = re.compile(
    r"(?:api[_-]?key|authorization|bearer|password|private[_-]?key|"
    r"raw[_-]?(?:environment|command|stdout|stderr|result|body|payload)|"
    r"(?:stdout|stderr|command)[_-]?payload|hidden[_-]?reasoning|"
    r"chain[_-]?of[_-]?thought|agents?[_-]?(?:body|content|text)|"
    r"skills?[_-]?(?:body|content|text)|memory[_-]?(?:body|content|text)|"
    r"mcp[_-]?(?:body|result|schema[_-]?body|description|connection)|"
    r"connection[_-]?config|secret[_-]?endpoint|\.env\.local)", re.I,
)
FORBIDDEN_EXPORT_TEXT = re.compile(
    r"(?:raw command payload|raw stdout|raw stderr|raw environment|"
    r"hidden reasoning|chain[- ]of[- ]thought|-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.I,
)


class RunBundleError(ValueError):
    """A Bundle is unsafe, corrupt, incomplete, or unavailable."""


def canonical_json(value):
    try:
        return canonical_json_bytes(value)
    except (TypeError, ValueError) as error:
        raise RunBundleError("Bundle 不是 canonical JSON") from error


def _sha256(payload):
    return sha256_identity(payload)


def _valid_logical_id(object_type, logical_id):
    pattern = (
        FINGERPRINT_PATTERN if object_type == "policy_snapshot"
        else EVIDENCE_ID_PATTERN if object_type == "evidence"
        else ARTIFACT_ID_PATTERN if object_type == "artifact"
        else ID_PATTERN
    )
    return isinstance(logical_id, str) and pattern.fullmatch(logical_id)


def _object_relative_path(object_type, logical_id):
    if object_type not in OBJECT_TYPES or not _valid_logical_id(
        object_type, logical_id
    ):
        raise RunBundleError("historical object identity 无效")
    return "/".join((
        "objects", OBJECT_DIRECTORIES[object_type],
        logical_id + OBJECT_EXTENSIONS[object_type],
    ))


def _parse_audit(payload, run_id):
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RunBundleError("Audit JSONL encoding 无效") from error
    events = []
    for raw_line in text.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            break  # Preserve V17 behavior for a torn final crash line.
        required = {
            "version", "event_id", "timestamp", "sequence", "run_id",
            "session_id", "event_type", "actor", "subject", "outcome",
            "reason", "references", "summary",
        }
        if (not isinstance(event, dict) or set(event) != required
                or event.get("version") != 1
                or not ID_PATTERN.fullmatch(str(event.get("event_id")))
                or event.get("run_id") != run_id
                or event.get("sequence") != len(events) + 1
                or not ID_PATTERN.fullmatch(str(event.get("session_id")))
                or event.get("actor") not in ACTORS
                or not isinstance(event.get("event_type"), str)
                or not isinstance(event.get("references"), dict)):
            raise RunBundleError("Audit event schema/order 无效")
        events.append(event)
    if not events:
        raise RunBundleError("Audit object 为空或损坏")
    return events


def _decode_json(payload, label):
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunBundleError(f"{label} JSON corruption") from error


def _validate_object_payload(object_type, logical_id, payload):
    if object_type == "audit":
        return _parse_audit(payload, logical_id)
    value = _decode_json(payload, object_type)
    try:
        if object_type == "policy_snapshot":
            validate_snapshot(value)
            if policy_fingerprint(value) != logical_id:
                raise RunBundleError("Policy Snapshot identity mismatch")
        elif object_type == "manifest":
            validate_manifest(value)
            if value["run_id"] != logical_id:
                raise RunBundleError("Manifest identity mismatch")
        elif object_type == "envelope":
            validate_envelope(value)
            if value["run_id"] != logical_id:
                raise RunBundleError("Envelope identity mismatch")
        elif object_type == "evidence":
            validate_evidence(value)
            if value["evidence_id"] != logical_id:
                raise RunBundleError("Evidence identity mismatch")
        elif object_type == "artifact":
            validate_artifact(value)
            if value["artifact_id"] != logical_id:
                raise RunBundleError("Artifact identity mismatch")
        elif object_type == "output_contract":
            validate_output_contract(value)
            if value["run_id"] != logical_id:
                raise RunBundleError("Output Contract identity mismatch")
        elif object_type == "result":
            validate_result(value)
            if value["run_id"] != logical_id:
                raise RunBundleError("Result identity mismatch")
        else:
            raise RunBundleError("unknown historical object type")
    except (ArtifactError, EvidenceError, ResultError, RunEnvelopeError,
            RunManifestError, ValueError, KeyError, TypeError) as error:
        if isinstance(error, RunBundleError):
            raise
        raise RunBundleError(f"{object_type} object integrity mismatch") from error
    return value


class HistoricalObjectResolver:
    """Minimal read-only object source used by historical checks and replay."""

    historical_read_only = True

    def read_bytes(self, object_type, logical_id):
        raise NotImplementedError

    def exists(self, object_type, logical_id):
        try:
            self.read_bytes(object_type, logical_id)
            return True
        except RunBundleError:
            return False

    def load(self, object_type, logical_id):
        return _validate_object_payload(
            object_type, logical_id,
            self.read_bytes(object_type, logical_id),
        )

    def list(self, object_type, source_run_id=None):
        raise NotImplementedError

    def audit_events(self, run_id):
        return self.load("audit", run_id)


class LocalHistoricalResolver(HistoricalObjectResolver):
    """Read existing Harness-owned history under one explicit .audit root."""

    def __init__(self, audit_directory=AUDIT_DIR):
        self.audit_directory = os.path.abspath(audit_directory)

    def _path(self, object_type, logical_id):
        relative = _object_relative_path(object_type, logical_id)
        if object_type == "audit":
            return os.path.join(
                self.audit_directory,
                logical_id + OBJECT_EXTENSIONS[object_type],
            )
        suffix = relative[len("objects/"):]
        return os.path.join(self.audit_directory, *suffix.split("/"))

    def read_bytes(self, object_type, logical_id):
        path = self._path(object_type, logical_id)
        try:
            metadata = os.lstat(path)
            if not stat.S_ISREG(metadata.st_mode) or os.path.islink(path):
                raise RunBundleError("historical object 必须是普通文件")
            with open(path, "rb") as stream:
                return stream.read()
        except FileNotFoundError as error:
            raise RunBundleError(
                f"missing reference: {object_type}:{logical_id}"
            ) from error

    def list(self, object_type, source_run_id=None):
        if object_type not in OBJECT_TYPES:
            raise RunBundleError("unknown historical object type")
        directory = os.path.dirname(self._path(
            object_type,
            ("0" * 64 if object_type == "policy_snapshot" else "0" * 32),
        ))
        try:
            names = sorted(os.listdir(directory))
        except FileNotFoundError:
            return []
        values = []
        extension = OBJECT_EXTENSIONS[object_type]
        for name in names:
            logical_id = name[:-len(extension)] if name.endswith(extension) else ""
            if not _valid_logical_id(object_type, logical_id):
                continue
            value = self.load(object_type, logical_id)
            owner = _object_owner_run(object_type, logical_id, value)
            if source_run_id is None or owner == source_run_id:
                values.append(value)
        return values


def _safe_index_path(bundle_root, relative_path):
    if (not isinstance(relative_path, str) or not relative_path
            or os.path.isabs(relative_path) or "\\" in relative_path
            or os.path.normpath(relative_path).replace(os.sep, "/")
            != relative_path
            or ".." in relative_path.split("/")):
        raise RunBundleError("unsafe indexed object path")
    root = os.path.realpath(bundle_root)
    candidate = os.path.join(root, *relative_path.split("/"))
    current = root
    for part in relative_path.split("/"):
        current = os.path.join(current, part)
        if os.path.lexists(current) and os.path.islink(current):
            raise RunBundleError("Bundle indexed path contains symlink")
    real = os.path.realpath(candidate)
    try:
        within = os.path.commonpath((root, real)) == root
    except ValueError:
        within = False
    if not within:
        raise RunBundleError("Bundle indexed path escapes root")
    return candidate


class BundleHistoricalResolver(HistoricalObjectResolver):
    """Resolve only indexed regular files inside one external Bundle root."""

    def __init__(self, bundle_directory, manifest=None):
        if os.path.islink(bundle_directory):
            raise RunBundleError("Bundle root 不能是 symlink")
        self.bundle_directory = os.path.abspath(bundle_directory)
        if not os.path.isdir(self.bundle_directory):
            raise RunBundleError("Bundle path 不存在")
        self.manifest = manifest or _read_bundle_manifest(self.bundle_directory)
        validate_bundle_manifest(self.manifest)
        self._index = {
            (item["object_type"], item["logical_id"]): item
            for item in self.manifest["objects"]
        }

    def read_bytes(self, object_type, logical_id):
        item = self._index.get((object_type, logical_id))
        if item is None:
            raise RunBundleError(
                f"missing reference: {object_type}:{logical_id}"
            )
        path = _safe_index_path(self.bundle_directory, item["path"])
        try:
            metadata = os.lstat(path)
            if not stat.S_ISREG(metadata.st_mode) or os.path.islink(path):
                raise RunBundleError("Bundle object 必须是普通文件")
            with open(path, "rb") as stream:
                payload = stream.read()
        except FileNotFoundError as error:
            raise RunBundleError(
                f"missing reference: {object_type}:{logical_id}"
            ) from error
        if len(payload) != item["size"] or _sha256(payload) != item["sha256"]:
            raise RunBundleError(
                f"object hash/size mismatch: {object_type}:{logical_id}"
            )
        return payload

    def list(self, object_type, source_run_id=None):
        values = []
        for item in self.manifest["objects"]:
            if item["object_type"] != object_type:
                continue
            value = self.load(object_type, item["logical_id"])
            owner = _object_owner_run(object_type, item["logical_id"], value)
            if source_run_id is None or owner == source_run_id:
                values.append(value)
        return values


def _object_owner_run(object_type, logical_id, value):
    if object_type in {
        "audit", "manifest", "envelope", "output_contract", "result",
    }:
        return logical_id
    if object_type in {"evidence", "artifact"}:
        return value["run_id"]
    return None


def _screen_value(value):
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            safe_identity = key_text.endswith(("_sha256", "_length", "_fingerprint"))
            if not safe_identity and FORBIDDEN_EXPORT_KEYS.search(key_text):
                raise RunBundleError(f"forbidden export field: {key_text}")
            _screen_value(item)
    elif isinstance(value, list):
        for item in value:
            _screen_value(item)
    elif isinstance(value, str):
        if FORBIDDEN_EXPORT_TEXT.search(value) or any(
            pattern.search(value) for pattern in SECRET_PATTERNS
        ):
            raise RunBundleError("forbidden export content")


def screen_export_object(object_type, logical_id, payload):
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RunBundleError("historical object is not UTF-8 metadata") from error
    if FORBIDDEN_EXPORT_TEXT.search(text):
        raise RunBundleError(
            f"offending object={object_type}:{logical_id}"
        )
    try:
        value = _validate_object_payload(object_type, logical_id, payload)
        _screen_value(value)
    except RunBundleError as error:
        raise RunBundleError(
            f"offending object={object_type}:{logical_id}: {error}"
        ) from error
    return value


def _references_from_envelope(envelope):
    refs = []
    for transition in envelope["transitions"]:
        inputs = transition["input"]
        if transition["transition_type"] == "verification" and inputs.get("evidence_id"):
            refs.append(("evidence", inputs["evidence_id"]))
        if transition["transition_type"] == "artifact_contract":
            identity = inputs.get("artifact_identity") or {}
            if identity.get("artifact_id"):
                refs.append(("artifact", identity["artifact_id"]))
            refs.extend(
                ("evidence", item["evidence_id"])
                for item in inputs.get("evidence_identities", [])
                if item.get("evidence_id")
            )
        if transition["transition_type"] == "result_binding":
            refs.extend(
                ("artifact", item["artifact_id"])
                for item in inputs.get("accepted_artifacts", [])
            )
            refs.extend(
                ("evidence", item["evidence_id"])
                for item in inputs.get("accepted_evidence", [])
            )
            if inputs.get("output_contract") is not None:
                refs.append(("output_contract", inputs["run_id"]))
    return refs


def _references_from_audit(events):
    refs = []
    for event in events:
        event_refs = event.get("references") or {}
        for key, object_type in (("evidence_id", "evidence"),
                                 ("artifact_id", "artifact")):
            value = event_refs.get(key)
            if isinstance(value, str):
                refs.append((object_type, value))
        for key, object_type in (("evidence_ids", "evidence"),
                                 ("artifact_ids", "artifact")):
            values = event_refs.get(key)
            if isinstance(values, list):
                refs.extend(
                    (object_type, value) for value in values
                    if isinstance(value, str)
                )
    return refs


def collect_reference_closure(run_id, resolver):
    """Return validated source payloads for one Run's typed strong closure."""
    if not ID_PATTERN.fullmatch(str(run_id)):
        raise RunBundleError("run_id 无效")
    status = "result" if resolver.exists("result", run_id) else "forensic"
    root = {"type": "result" if status == "result" else "run", "id": run_id}
    collected = {}
    pending = []

    def add(object_type, logical_id, required=True):
        key = (object_type, logical_id)
        if key in collected or key in pending:
            return
        if not resolver.exists(object_type, logical_id):
            if required:
                raise RunBundleError(
                    f"missing reference: {object_type}:{logical_id}"
                )
            return
        pending.append(key)

    # A Result's current integrity contract binds Audit events.  A forensic
    # bundle may carry no trace and must report it as unavailable, not MATCH.
    add("audit", run_id, required=status == "result")
    add("result", run_id, required=status == "result")
    add("envelope", run_id, required=status == "result")
    add("manifest", run_id, required=status == "result")

    while pending:
        object_type, logical_id = pending.pop(0)
        payload = resolver.read_bytes(object_type, logical_id)
        value = screen_export_object(object_type, logical_id, payload)
        collected[(object_type, logical_id)] = (payload, value)
        if object_type == "result":
            for artifact_id in value["artifact_ids"]:
                add("artifact", artifact_id)
            for evidence_id in value["evidence_ids"]:
                add("evidence", evidence_id)
        elif object_type == "manifest":
            add("policy_snapshot", value["configuration"]["policy"]["policy_fingerprint"])
        elif object_type == "envelope":
            add("policy_snapshot", value["inputs"]["policy_fingerprint"])
            for reference in _references_from_envelope(value):
                add(*reference)
        elif object_type == "audit":
            started = next((
                event for event in value
                if event["event_type"] == "run_started"
            ), None)
            policy_id = ((started or {}).get("references") or {}).get(
                "policy_fingerprint"
            )
            if policy_id:
                add("policy_snapshot", policy_id)
            for reference in _references_from_audit(value):
                add(*reference, required=False)
        elif object_type == "artifact":
            for evidence_id in value["evidence_ids"]:
                add("evidence", evidence_id)
            if value["supersedes_artifact_id"] is not None:
                add("artifact", value["supersedes_artifact_id"])
        elif object_type == "evidence":
            references = dict(value["source"])
            references.update(value["references"])
            for key, reference in references.items():
                if (key.endswith("evidence_id")
                        and reference != value["evidence_id"]):
                    add("evidence", reference)
    if not any(
        (object_type, run_id) in collected
        for object_type in ("audit", "envelope", "manifest", "result")
    ):
        raise RunBundleError(f"run history unavailable: {run_id}")
    return status, root, collected


def bundle_fingerprint(manifest):
    material = {
        "bundle_schema_version": manifest["bundle_schema_version"],
        "run_id": manifest["run_id"],
        "bundle_status": manifest["bundle_status"],
        "root": manifest["root"],
        "objects": manifest["objects"],
    }
    return _sha256(canonical_json(material))


def _validate_index_item(item):
    if not isinstance(item, dict):
        raise RunBundleError("Bundle object index schema 无效")
    fields = set(item)
    if fields not in (INDEX_FIELDS, INDEX_FIELDS | CROSS_RUN_FIELDS):
        raise RunBundleError("Bundle object index fields 无效")
    object_type, logical_id = item.get("object_type"), item.get("logical_id")
    if object_type not in OBJECT_TYPES or not _valid_logical_id(
        object_type, logical_id
    ):
        raise RunBundleError("Bundle object index identity 无效")
    if item.get("path") != _object_relative_path(object_type, logical_id):
        raise RunBundleError("Bundle object index path mismatch")
    if not SHA256_PATTERN.fullmatch(str(item.get("sha256"))):
        raise RunBundleError("Bundle object sha256 无效")
    if (not isinstance(item.get("size"), int)
            or isinstance(item.get("size"), bool) or item["size"] < 0):
        raise RunBundleError("Bundle object size 无效")
    if fields == INDEX_FIELDS | CROSS_RUN_FIELDS and (
        item["vendored_cross_run"] is not True
        or not ID_PATTERN.fullmatch(str(item["source_run_id"]))
    ):
        raise RunBundleError("cross-run object metadata 无效")
    return item


def validate_bundle_manifest(manifest, verify_fingerprint=True):
    if not isinstance(manifest, dict) or set(manifest) != BUNDLE_FIELDS:
        raise RunBundleError("Bundle schema 无效")
    if manifest["bundle_schema_version"] != BUNDLE_SCHEMA_VERSION:
        raise RunBundleError("unsupported Bundle schema")
    if not ID_PATTERN.fullmatch(str(manifest["run_id"])):
        raise RunBundleError("Bundle run_id 无效")
    if not isinstance(manifest["created_at"], str) or not manifest["created_at"]:
        raise RunBundleError("Bundle created_at 无效")
    if manifest["bundle_status"] not in BUNDLE_STATUSES:
        raise RunBundleError("Bundle status 无效")
    expected_root = {
        "type": "result" if manifest["bundle_status"] == "result" else "run",
        "id": manifest["run_id"],
    }
    if manifest["root"] != expected_root:
        raise RunBundleError("Bundle root binding mismatch")
    if not isinstance(manifest["objects"], list):
        raise RunBundleError("Bundle object index 无效")
    for item in manifest["objects"]:
        _validate_index_item(item)
    ordered = sorted(
        manifest["objects"],
        key=lambda item: (
            item["object_type"], item["logical_id"], item["path"]
        ),
    )
    if manifest["objects"] != ordered:
        raise RunBundleError("Bundle object index ordering mismatch")
    identities = [(item["object_type"], item["logical_id"])
                  for item in manifest["objects"]]
    paths = [item["path"] for item in manifest["objects"]]
    if len(identities) != len(set(identities)) or len(paths) != len(set(paths)):
        raise RunBundleError("duplicate Bundle object identity/path")
    if not SHA256_PATTERN.fullmatch(str(manifest["bundle_fingerprint"])):
        raise RunBundleError("Bundle fingerprint 无效")
    if verify_fingerprint and bundle_fingerprint(manifest) != manifest["bundle_fingerprint"]:
        raise RunBundleError("Bundle fingerprint mismatch")
    return manifest


def _read_bundle_manifest(bundle_directory):
    path = os.path.join(os.path.abspath(bundle_directory), "bundle.json")
    if os.path.islink(path):
        raise RunBundleError("bundle.json 不能是 symlink")
    try:
        metadata = os.lstat(path)
        if not stat.S_ISREG(metadata.st_mode):
            raise RunBundleError("bundle.json 必须是普通文件")
        with open(path, "rb") as stream:
            return _decode_json(stream.read(), "bundle")
    except FileNotFoundError as error:
        raise RunBundleError("Bundle path 不存在或缺少 bundle.json") from error


def _write_file(path, payload):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def export_run_bundle(run_id, audit_directory=AUDIT_DIR,
                      bundles_directory=None, created_at=None):
    resolver = LocalHistoricalResolver(audit_directory)
    status, root, collected = collect_reference_closure(run_id, resolver)
    index = []
    for (object_type, logical_id), (payload, value) in collected.items():
        item = {
            "object_type": object_type, "logical_id": logical_id,
            "path": _object_relative_path(object_type, logical_id),
            "sha256": _sha256(payload), "size": len(payload),
        }
        owner = _object_owner_run(object_type, logical_id, value)
        if owner is not None and owner != run_id:
            item.update({
                "vendored_cross_run": True, "source_run_id": owner,
            })
        index.append(item)
    index.sort(key=lambda item: (
        item["object_type"], item["logical_id"], item["path"]
    ))
    manifest = {
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "run_id": run_id, "created_at": created_at or utc_now(),
        "bundle_status": status, "bundle_fingerprint": "",
        "root": root, "objects": index,
    }
    manifest["bundle_fingerprint"] = bundle_fingerprint(manifest)
    validate_bundle_manifest(manifest)
    bundle_parent = os.path.abspath(
        bundles_directory or os.path.join(audit_directory, "bundles")
    )
    target = os.path.join(bundle_parent, run_id)
    if os.path.lexists(target):
        if os.path.islink(target) or not os.path.isdir(target):
            raise RunBundleError("existing Bundle target is unsafe")
        existing = _read_bundle_manifest(target)
        if (existing.get("bundle_fingerprint") == manifest["bundle_fingerprint"]
                and check_bundle(target)["match"]):
            return target, existing, True
        raise RunBundleError("existing Bundle content differs; refusing overwrite")
    os.makedirs(bundle_parent, mode=0o700, exist_ok=True)
    temporary = tempfile.mkdtemp(prefix=".bundle-", dir=bundle_parent)
    try:
        by_key = {key: payload for key, (payload, _value) in collected.items()}
        for item in index:
            path = os.path.join(temporary, *item["path"].split("/"))
            _write_file(path, by_key[(item["object_type"], item["logical_id"])])
        _write_file(
            os.path.join(temporary, "bundle.json"),
            canonical_json(manifest) + b"\n",
        )
        result = check_bundle(temporary)
        if not result["match"]:
            raise RunBundleError(
                "new Bundle failed self-check: " + result["error"]
            )
        try:
            os.rename(temporary, target)
        except FileExistsError as error:
            raise RunBundleError("Bundle target appeared during export") from error
        temporary = None
    finally:
        if temporary is not None:
            shutil.rmtree(temporary)
    return target, manifest, False


def _indexed_files(bundle_directory):
    files = set()
    for root, directories, names in os.walk(bundle_directory, followlinks=False):
        for name in list(directories):
            if os.path.islink(os.path.join(root, name)):
                raise RunBundleError("Bundle contains symlink directory")
        for name in names:
            path = os.path.join(root, name)
            if os.path.islink(path):
                raise RunBundleError("Bundle contains symlink file")
            relative = os.path.relpath(path, bundle_directory).replace(os.sep, "/")
            files.add(relative)
    return files


def _check_relationships(manifest, resolver):
    run_id = manifest["run_id"]
    indexed_keys = {
        (item["object_type"], item["logical_id"])
        for item in manifest["objects"]
    }
    events = (
        resolver.load("audit", run_id)
        if ("audit", run_id) in indexed_keys else []
    )
    if manifest["bundle_status"] == "result":
        resolver.load("result", run_id)
    envelope = resolver.load("envelope", run_id) if (
        "envelope", run_id
    ) in indexed_keys else None
    manifest_object = resolver.load("manifest", run_id) if (
        "manifest", run_id
    ) in indexed_keys else None
    if manifest["bundle_status"] == "result" and (
        envelope is None or manifest_object is None
        or ("audit", run_id) not in indexed_keys
    ):
        raise RunBundleError("Result Bundle missing required integrity closure")
    if envelope is not None:
        policy_id = envelope["inputs"]["policy_fingerprint"]
        snapshot = resolver.load("policy_snapshot", policy_id)
        if manifest_object is None:
            raise RunBundleError("Envelope missing Manifest")
        if (manifest_object["configuration_fingerprint"]
                != envelope["inputs"]["manifest_fingerprint"]
                or manifest_object["configuration"]["policy"]["policy_fingerprint"]
                != policy_id or policy_fingerprint(snapshot) != policy_id):
            raise RunBundleError("Envelope/Manifest/Policy binding mismatch")
        for object_type, logical_id in _references_from_envelope(envelope):
            resolver.load(object_type, logical_id)
    if manifest_object is not None:
        policy = manifest_object["configuration"]["policy"]
        snapshot = resolver.load("policy_snapshot", policy["policy_fingerprint"])
        if (snapshot["policy_schema_version"] != policy["policy_schema_version"]
                or snapshot["policy_revision"] != policy["policy_revision"]):
            raise RunBundleError("Manifest Policy integrity mismatch")
    started = next((
        event for event in events if event["event_type"] == "run_started"
    ), None)
    started_refs = (started or {}).get("references") or {}
    if manifest_object is not None and started_refs.get(
        "manifest_fingerprint"
    ) not in {None, manifest_object["configuration_fingerprint"]}:
        raise RunBundleError("Audit Manifest reference mismatch")
    if envelope is not None and started_refs.get(
        "envelope_fingerprint"
    ) not in {None, envelope["envelope_fingerprint"]}:
        raise RunBundleError("Audit Envelope reference mismatch")
    if manifest_object is not None and started_refs.get(
        "policy_fingerprint"
    ) not in {
        None,
        manifest_object["configuration"]["policy"]["policy_fingerprint"],
    }:
        raise RunBundleError("Audit Policy reference mismatch")
    for item in manifest["objects"]:
        value = resolver.load(item["object_type"], item["logical_id"])
        owner = _object_owner_run(item["object_type"], item["logical_id"], value)
        cross = owner is not None and owner != run_id
        if cross != (item.get("vendored_cross_run") is True):
            raise RunBundleError("cross-run object index binding mismatch")
        if cross and item.get("source_run_id") != owner:
            raise RunBundleError("cross-run source_run_id mismatch")
        if item["object_type"] == "evidence":
            if not evidence_integrity_check(
                item["logical_id"], resolver=resolver
            ):
                raise RunBundleError("Evidence integrity mismatch")
        elif item["object_type"] == "artifact":
            if not artifact_integrity_check(
                item["logical_id"], resolver=resolver
            ):
                raise RunBundleError("Artifact integrity mismatch")
    if manifest["bundle_status"] == "result":
        if not result_integrity_check(run_id, resolver=resolver):
            raise RunBundleError("Result integrity mismatch")


def check_bundle(bundle_directory):
    """Perform a fully offline check; return a stable diagnostic record."""
    try:
        if os.path.islink(bundle_directory):
            raise RunBundleError("Bundle root 不能是 symlink")
        manifest = _read_bundle_manifest(bundle_directory)
        validate_bundle_manifest(manifest)
        expected_files = {"bundle.json"} | {
            item["path"] for item in manifest["objects"]
        }
        actual_files = _indexed_files(os.path.abspath(bundle_directory))
        if actual_files != expected_files:
            missing = sorted(expected_files - actual_files)
            extra = sorted(actual_files - expected_files)
            raise RunBundleError(
                f"Bundle indexed files mismatch missing={missing} extra={extra}"
            )
        resolver = BundleHistoricalResolver(bundle_directory, manifest)
        for item in manifest["objects"]:
            resolver.load(item["object_type"], item["logical_id"])
        _check_relationships(manifest, resolver)
        trace_available = resolver.exists("audit", manifest["run_id"])
        return {
            "match": True, "error": None, "manifest": manifest,
            "closure_status": "MATCH" if trace_available else "PARTIAL",
            "trace_status": "available" if trace_available else "unavailable",
        }
    except (OSError, RunBundleError, ValueError, KeyError, TypeError) as error:
        return {
            "match": False, "error": str(error), "manifest": None,
            "closure_status": "MISMATCH", "trace_status": "unavailable",
        }


def show_bundle(bundle_directory):
    checked = check_bundle(bundle_directory)
    if not checked["match"]:
        raise RunBundleError("Bundle check mismatch: " + checked["error"])
    manifest = checked["manifest"]
    resolver = BundleHistoricalResolver(bundle_directory, manifest)
    counts = {name: 0 for name in sorted(OBJECT_TYPES)}
    for item in manifest["objects"]:
        counts[item["object_type"]] += 1
    run_id = manifest["run_id"]
    manifest_object = (
        resolver.load("manifest", run_id)
        if resolver.exists("manifest", run_id) else None
    )
    envelope = (
        resolver.load("envelope", run_id)
        if resolver.exists("envelope", run_id) else None
    )
    result = (
        resolver.load("result", run_id)
        if resolver.exists("result", run_id) else None
    )
    policy_items = [item for item in manifest["objects"]
                    if item["object_type"] == "policy_snapshot"]
    return {
        "run_id": run_id, "status": manifest["bundle_status"],
        "bundle_fingerprint": manifest["bundle_fingerprint"],
        "counts": counts,
        "policy_fingerprint": (
            policy_items[0]["logical_id"] if len(policy_items) == 1
            else "unavailable" if not policy_items else "multiple"
        ),
        "manifest_fingerprint": (
            manifest_object["configuration_fingerprint"]
            if manifest_object else "unavailable"
        ),
        "envelope_fingerprint": (
            envelope["envelope_fingerprint"] if envelope else "unavailable"
        ),
        "result_status": result["status"] if result else "absent",
        "trace_status": (
            "available" if resolver.exists("audit", run_id) else "unavailable"
        ),
        "cross_run_vendored": sum(
            item.get("vendored_cross_run") is True
            for item in manifest["objects"]
        ),
    }


def replay_bundle(bundle_directory):
    checked = check_bundle(bundle_directory)
    if not checked["match"]:
        return {
            "status": "MISMATCH", "identity": "MISMATCH",
            "transitions": [], "error": checked["error"],
        }
    manifest = checked["manifest"]
    resolver = BundleHistoricalResolver(bundle_directory, manifest)
    if not resolver.exists("envelope", manifest["run_id"]):
        return {
            "status": "UNAVAILABLE", "identity": "UNAVAILABLE",
            "transitions": [], "error": "Envelope unavailable",
        }
    result = harness_replay_check(
        resolver.load("envelope", manifest["run_id"]), resolver=resolver
    )
    statuses = [item["status"] for item in result["transitions"]]
    status = (
        "MISMATCH" if result["identity"] == "MISMATCH" or "MISMATCH" in statuses
        else "UNAVAILABLE" if "UNAVAILABLE" in statuses
        else "MATCH"
    )
    return {
        "status": status, "identity": result["identity"],
        "transitions": result["transitions"], "error": None,
    }
