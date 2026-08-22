"""V20 immutable, safe Run configuration identity records.

This module describes runtime configuration.  It never calls a model or a tool.
MCP discovery data passed here has already been obtained by the Harness.
"""

import hashlib
import json
import os
import re
from urllib.parse import urlsplit

from .audit import ID_PATTERN, utc_now
from .context import (
    COMPACTION_RECENT_MESSAGES, COMPACTION_STRATEGY_VERSION,
    TOKEN_ESTIMATOR_VERSION,
)
from .memory import select_memories
from .planning import PLANNING_SCHEMA_VERSION
from .policy_snapshot import (
    POLICY_SCHEMA_VERSION, PolicySnapshotError, load_policy_snapshot,
)
from .project_context import (
    PROJECT_INSTRUCTIONS_FILE, discover_skills, load_project_instructions,
    load_skill_body, select_skill,
)
from .security import SECRET_PATTERNS
from .session import SESSION_VERSION
from .integrity import (
    ImmutableRecordConflict, atomic_json_publish, canonical_json_bytes,
    sha256_identity,
)


MANIFEST_SCHEMA_VERSION = 1
HARNESS_RELEASE = "development"
HARNESS_PROTOCOL_VERSION = 1
MODEL_PROTOCOL_MODE = "json-decision-v1"
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_SECTIONS = {
    "harness", "model", "policy", "project_context", "capabilities",
    "memory", "context_strategy",
}
FORBIDDEN_KEYS = re.compile(
    r"(?:api[_-]?key|authorization|bearer|password|private[_-]?key|"
    r"environment|raw[_-]?endpoint|memory[_-]?content|agents[_-]?content|"
    r"skill[_-]?(?:body|content)|description|result)", re.I,
)


class RunManifestError(ValueError):
    """A manifest is unavailable, unsafe, corrupt, or unsupported."""


def canonical_json(value):
    try:
        return canonical_json_bytes(value)
    except (TypeError, ValueError) as error:
        raise RunManifestError("manifest configuration 不是 canonical JSON") from error


def configuration_fingerprint(configuration):
    return sha256_identity(canonical_json(configuration))


def content_fingerprint(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def endpoint_identity(endpoint):
    """Return only presence plus a digest of scheme/host/effective port."""
    if not endpoint:
        return {"endpoint_present": False, "endpoint_origin_digest": None}
    try:
        parsed = urlsplit(endpoint)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("userinfo is not safe endpoint identity")
        if not parsed.scheme or parsed.hostname is None:
            raise ValueError("origin is incomplete")
        scheme = parsed.scheme.lower()
        host = parsed.hostname.lower()
        port = parsed.port
        if port is None:
            port = 443 if scheme == "https" else 80 if scheme == "http" else None
        origin = f"{scheme}://{host}" + (f":{port}" if port is not None else "")
        digest = content_fingerprint(origin)
    except (TypeError, ValueError):
        # Do not hash the unsafe raw value: even a digest can become a secret oracle.
        digest = None
    return {"endpoint_present": True, "endpoint_origin_digest": digest}


def model_identity(provider):
    client = getattr(provider, "client", None)
    if client is None:
        return {
            "provider_kind": "fake" if provider.__class__.__name__ == "FakeProvider"
            else provider.__class__.__name__,
            "api_mode": "offline" if provider.__class__.__name__ == "FakeProvider"
            else "custom",
            "model_identifier": "fake-model" if provider.__class__.__name__ == "FakeProvider"
            else provider.__class__.__name__,
            "protocol_mode": MODEL_PROTOCOL_MODE,
            "endpoint_present": False,
            "endpoint_origin_digest": None,
        }
    identity = {
        "provider_kind": "openai-compatible",
        "api_mode": getattr(client, "api_mode", "custom"),
        "model_identifier": getattr(client, "model", client.__class__.__name__),
        "protocol_mode": MODEL_PROTOCOL_MODE,
    }
    identity.update(endpoint_identity(getattr(client, "endpoint", None)))
    return identity


def harness_identity():
    return {
        "harness_release": HARNESS_RELEASE,
        "protocol_version": HARNESS_PROTOCOL_VERSION,
        "session_schema_version": SESSION_VERSION,
        "planning_schema_version": PLANNING_SCHEMA_VERSION,
        "policy_schema_version": POLICY_SCHEMA_VERSION,
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
    }


def project_context_identity(project_root, task):
    agents = load_project_instructions(project_root)
    agents_present = os.path.isfile(os.path.join(
        project_root, PROJECT_INSTRUCTIONS_FILE
    ))
    catalog = discover_skills(project_root)
    active_name = select_skill(task, catalog)
    active_body = load_skill_body(project_root, active_name) if active_name else None
    return {
        "agents_present": agents_present,
        "agents_fingerprint": content_fingerprint(agents) if agents_present else None,
        "skill_catalog_fingerprint": configuration_fingerprint(catalog),
        "active_skill_name": active_name,
        "active_skill_fingerprint": (
            content_fingerprint(active_body) if active_body is not None else None
        ),
    }


def memory_identity(memory_store, task):
    selected = select_memories(memory_store.load(), task)
    records = sorted(
        ({"id": item["id"], "version": item["updated_at"]} for item in selected),
        key=lambda item: item["id"],
    )
    return {
        "selected_memory_ids": [item["id"] for item in records],
        "selected_memory_fingerprint": configuration_fingerprint(records),
    }


def _transport_kind(client):
    name = client.__class__.__name__.lower()
    if "fake" in name:
        return "fake"
    if "stdio" in name:
        return "stdio"
    return "custom"


def capability_identity(mcp_registry, policy_binding):
    identities = []
    if mcp_registry is not None:
        catalog = mcp_registry.capability_catalog()
        available = sorted(
            item["tool"] for item in catalog if isinstance(item.get("tool"), str)
        )
        mappings = policy_binding.snapshot["definitions"]["mcp_capability_mappings"]
        for capability_id in available:
            parts = capability_id.split(":", 2)
            server = parts[1] if len(parts) == 3 else "unknown"
            mapping = mappings.get(capability_id)
            identities.append({
                "capability_id": capability_id,
                "mapping_fingerprint": (
                    configuration_fingerprint(mapping) if mapping is not None else None
                ),
                "transport_kind": _transport_kind(mcp_registry.clients.get(server)),
            })
    identities.sort(key=lambda item: item["capability_id"])
    return {
        "capability_catalog_fingerprint": configuration_fingerprint(identities),
        "catalog_identity": identities,
    }


def build_configuration(task, provider, policy_binding, context_assembler,
                        context_budget=None):
    configuration = {
        "harness": harness_identity(),
        "model": model_identity(provider),
        "policy": {
            "policy_schema_version": policy_binding.schema_version,
            "policy_revision": policy_binding.revision,
            "policy_fingerprint": policy_binding.fingerprint,
        },
        "project_context": project_context_identity(context_assembler.project_root, task),
        "capabilities": capability_identity(context_assembler.mcp_registry, policy_binding),
        "memory": memory_identity(context_assembler.memory_store, task),
        "context_strategy": {
            "context_budget": context_budget,
            "compaction_strategy_version": COMPACTION_STRATEGY_VERSION,
            "token_estimator_version": TOKEN_ESTIMATOR_VERSION,
            "recent_message_retention": COMPACTION_RECENT_MESSAGES,
        },
    }
    validate_configuration(configuration)
    return configuration


def rebuild_configuration_for_status(historical, provider, policy_binding,
                                     context_assembler, context_budget=None):
    """Re-observe sources selected by a historical run without needing its task."""
    project = historical["project_context"]
    agents = load_project_instructions(context_assembler.project_root)
    agents_present = os.path.isfile(os.path.join(
        context_assembler.project_root, PROJECT_INSTRUCTIONS_FILE
    ))
    catalog = discover_skills(context_assembler.project_root)
    active_name = project.get("active_skill_name")
    active_body = load_skill_body(context_assembler.project_root, active_name) \
        if active_name else None
    current_memories = {
        item["id"]: item for item in context_assembler.memory_store.load()
    }
    selected_ids = sorted(historical["memory"]["selected_memory_ids"])
    records = [
        {"id": memory_id,
         "version": current_memories.get(memory_id, {}).get("updated_at")}
        for memory_id in selected_ids
    ]
    return {
        "harness": harness_identity(),
        "model": model_identity(provider),
        "policy": {
            "policy_schema_version": policy_binding.schema_version,
            "policy_revision": policy_binding.revision,
            "policy_fingerprint": policy_binding.fingerprint,
        },
        "project_context": {
            "agents_present": agents_present,
            "agents_fingerprint": content_fingerprint(agents) if agents_present else None,
            "skill_catalog_fingerprint": configuration_fingerprint(catalog),
            "active_skill_name": active_name if active_body is not None else None,
            "active_skill_fingerprint": (
                content_fingerprint(active_body) if active_body is not None else None
            ),
        },
        "capabilities": capability_identity(context_assembler.mcp_registry, policy_binding),
        "memory": {
            "selected_memory_ids": selected_ids,
            "selected_memory_fingerprint": configuration_fingerprint(records),
        },
        "context_strategy": {
            "context_budget": context_budget,
            "compaction_strategy_version": COMPACTION_STRATEGY_VERSION,
            "token_estimator_version": TOKEN_ESTIMATOR_VERSION,
            "recent_message_retention": COMPACTION_RECENT_MESSAGES,
        },
    }


def build_manifest(run_id, session_id, configuration, created_at=None):
    manifest = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "session_id": session_id,
        "created_at": created_at or utc_now(),
        "configuration_fingerprint": configuration_fingerprint(configuration),
        "configuration": configuration,
    }
    validate_manifest(manifest)
    return manifest


def _contains_secret(value):
    if isinstance(value, dict):
        return any(FORBIDDEN_KEYS.search(str(key)) or _contains_secret(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    return isinstance(value, str) and any(pattern.search(value) for pattern in SECRET_PATTERNS)


def validate_configuration(configuration):
    if not isinstance(configuration, dict) or set(configuration) != REQUIRED_SECTIONS:
        raise RunManifestError("manifest configuration sections 无效")
    if _contains_secret(configuration):
        raise RunManifestError("manifest secret screening rejected")
    required_fields = {
        "harness": {
            "harness_release", "protocol_version", "session_schema_version",
            "planning_schema_version", "policy_schema_version",
            "manifest_schema_version",
        },
        "model": {
            "provider_kind", "api_mode", "model_identifier", "protocol_mode",
            "endpoint_present", "endpoint_origin_digest",
        },
        "policy": {
            "policy_schema_version", "policy_revision", "policy_fingerprint",
        },
        "project_context": {
            "agents_present", "agents_fingerprint", "skill_catalog_fingerprint",
            "active_skill_name", "active_skill_fingerprint",
        },
        "capabilities": {
            "capability_catalog_fingerprint", "catalog_identity",
        },
        "memory": {"selected_memory_ids", "selected_memory_fingerprint"},
        "context_strategy": {
            "context_budget", "compaction_strategy_version",
            "token_estimator_version", "recent_message_retention",
        },
    }
    for section, fields in required_fields.items():
        if not isinstance(configuration[section], dict) or set(configuration[section]) != fields:
            raise RunManifestError(f"manifest {section} identity fields 无效")
    fingerprints = [
        configuration["policy"]["policy_fingerprint"],
        configuration["project_context"]["skill_catalog_fingerprint"],
        configuration["capabilities"]["capability_catalog_fingerprint"],
        configuration["memory"]["selected_memory_fingerprint"],
    ]
    for optional in ("agents_fingerprint", "active_skill_fingerprint"):
        value = configuration["project_context"][optional]
        if value is not None:
            fingerprints.append(value)
    endpoint_digest = configuration["model"]["endpoint_origin_digest"]
    if endpoint_digest is not None:
        fingerprints.append(endpoint_digest)
    if any(not isinstance(value, str) or not FINGERPRINT_PATTERN.fullmatch(value)
           for value in fingerprints):
        raise RunManifestError("manifest identity fingerprint 无效")
    if not isinstance(configuration["memory"]["selected_memory_ids"], list):
        raise RunManifestError("manifest selected memory IDs 无效")
    if not isinstance(configuration["capabilities"]["catalog_identity"], list):
        raise RunManifestError("manifest capability identity 无效")
    harness = configuration["harness"]
    if not isinstance(harness["harness_release"], str) or not harness["harness_release"]:
        raise RunManifestError("manifest harness release 无效")
    if any(not isinstance(harness[key], int) or isinstance(harness[key], bool)
           or harness[key] < 1 for key in (
               "protocol_version", "session_schema_version",
               "planning_schema_version", "policy_schema_version",
               "manifest_schema_version",
           )):
        raise RunManifestError("manifest harness schema identity 无效")
    model = configuration["model"]
    if any(not isinstance(model[key], str) or not model[key] for key in (
        "provider_kind", "api_mode", "model_identifier", "protocol_mode",
    )) or not isinstance(model["endpoint_present"], bool):
        raise RunManifestError("manifest model identity 无效")
    policy = configuration["policy"]
    if (not isinstance(policy["policy_schema_version"], int)
            or isinstance(policy["policy_schema_version"], bool)
            or not isinstance(policy["policy_revision"], str)
            or not policy["policy_revision"]):
        raise RunManifestError("manifest policy identity 无效")
    project = configuration["project_context"]
    if not isinstance(project["agents_present"], bool) or any(
        project[key] is not None and not isinstance(project[key], str)
        for key in ("active_skill_name",)
    ):
        raise RunManifestError("manifest project identity 无效")
    memory_ids = configuration["memory"]["selected_memory_ids"]
    if (any(not isinstance(item, str) or not item for item in memory_ids)
            or memory_ids != sorted(set(memory_ids))):
        raise RunManifestError("manifest selected memory IDs 无效")
    for item in configuration["capabilities"]["catalog_identity"]:
        if not isinstance(item, dict) or set(item) != {
            "capability_id", "mapping_fingerprint", "transport_kind",
        } or not all(isinstance(item[key], str) and item[key] for key in (
            "capability_id", "transport_kind",
        )) or (item["mapping_fingerprint"] is not None and not
               FINGERPRINT_PATTERN.fullmatch(item["mapping_fingerprint"])):
            raise RunManifestError("manifest capability identity 无效")
    context = configuration["context_strategy"]
    if context["context_budget"] is not None and (
        not isinstance(context["context_budget"], int)
        or isinstance(context["context_budget"], bool)
        or context["context_budget"] < 1
    ):
        raise RunManifestError("manifest context budget 无效")
    if any(not isinstance(context[key], int) or isinstance(context[key], bool)
           or context[key] < 1 for key in (
               "compaction_strategy_version", "token_estimator_version",
               "recent_message_retention",
           )):
        raise RunManifestError("manifest context strategy 无效")
    canonical_json(configuration)
    return configuration


def validate_manifest(manifest, verify_fingerprint=True):
    if not isinstance(manifest, dict) or set(manifest) != {
        "manifest_schema_version", "run_id", "session_id", "created_at",
        "configuration_fingerprint", "configuration",
    }:
        raise RunManifestError("manifest schema 无效")
    if manifest.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        raise RunManifestError("unsupported historical manifest schema")
    if not ID_PATTERN.fullmatch(str(manifest.get("run_id", ""))):
        raise RunManifestError("manifest run_id 无效")
    if not ID_PATTERN.fullmatch(str(manifest.get("session_id", ""))):
        raise RunManifestError("manifest session_id 无效")
    if not isinstance(manifest.get("created_at"), str) or not manifest["created_at"]:
        raise RunManifestError("manifest created_at 无效")
    fingerprint = manifest.get("configuration_fingerprint")
    if not isinstance(fingerprint, str) or not FINGERPRINT_PATTERN.fullmatch(fingerprint):
        raise RunManifestError("manifest fingerprint 无效")
    validate_configuration(manifest.get("configuration"))
    if verify_fingerprint and configuration_fingerprint(manifest["configuration"]) != fingerprint:
        raise RunManifestError("manifest configuration fingerprint mismatch")
    return manifest


class RunManifestStore:
    def __init__(self, directory):
        self.directory = directory

    def _path(self, run_id):
        if not isinstance(run_id, str) or not ID_PATTERN.fullmatch(run_id):
            raise RunManifestError("manifest run_id 无效")
        return os.path.join(self.directory, f"{run_id}.json")

    def persist(self, manifest):
        validate_manifest(manifest)
        path = self._path(manifest["run_id"])
        try:
            atomic_json_publish(
                path, manifest, temporary_prefix=".manifest-",
                temporary_suffix=".tmp",
            )
        except ImmutableRecordConflict as error:
            raise RunManifestError("run manifest immutable conflict") from error
        return path

    def load(self, run_id, verify=True):
        try:
            with open(self._path(run_id), encoding="utf-8") as stream:
                manifest = json.load(stream)
        except FileNotFoundError as error:
            raise RunManifestError(f"run manifest 不存在：{run_id}") from error
        except (OSError, json.JSONDecodeError) as error:
            raise RunManifestError("run manifest corruption") from error
        validate_manifest(manifest, verify_fingerprint=verify)
        return manifest


def manifest_differences(historical, current):
    """Known reproducibility dimensions only; never a generic recursive diff."""
    dimensions = [
        ("Harness", "harness"), ("Model", "model"), ("Policy", "policy"),
        ("Project", "project_context"), ("Capabilities", "capabilities"),
        ("Memory", "memory"), ("Context", "context_strategy"),
    ]
    differences = []
    for label, section in dimensions:
        before, after = historical[section], current[section]
        for key in sorted(set(before) | set(after)):
            if before.get(key) != after.get(key):
                differences.append((label, key, before.get(key), after.get(key)))
    return differences


def integrity_check(manifest, policy_directory):
    try:
        validate_manifest(manifest)
        policy = manifest["configuration"]["policy"]
        snapshot = load_policy_snapshot(policy["policy_fingerprint"], policy_directory)
        if snapshot["policy_schema_version"] != policy["policy_schema_version"]:
            return False
        if snapshot["policy_revision"] != policy["policy_revision"]:
            return False
        return True
    except (RunManifestError, PolicySnapshotError, KeyError, TypeError):
        return False
