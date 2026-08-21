"""V19 immutable, content-addressed Authority policy snapshots.

This module deals only with Harness-owned static policy.  It never executes a
tool, calls a model, or replays runtime gates.
"""

import hashlib
import json
import os
import re
import tempfile

from .policy_composition import (
    ALLOW, ASK, CAPABILITY_PROFILES, DECISIONS, DECISION_RANK, DENY, EFFECTS, EXTERNAL,
    GLOBAL_SECURITY_POLICY, NEUTRAL_DELEGATED_CEILING, ZONE_POLICIES,
    CapabilityProfile, StaticPolicyLayer, compose_static_policy,
)
from .security import SECRET_PATTERNS


POLICY_SCHEMA_VERSION = 1
POLICY_REVISION = "v19-default"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY_DIRECTORY = os.path.join(PROJECT_ROOT, ".audit", "policies")
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class PolicySnapshotError(ValueError):
    """A snapshot is unavailable, unsafe, corrupt, or unsupported."""


class PolicyBinding:
    """An immutable binding backed by canonical bytes, not a mutable dict."""

    __slots__ = ("_canonical", "fingerprint", "_sealed")

    def __init__(self, snapshot, fingerprint):
        validate_snapshot(snapshot)
        if policy_fingerprint(snapshot) != fingerprint:
            raise PolicySnapshotError("policy binding fingerprint mismatch")
        object.__setattr__(self, "_canonical", canonical_json(snapshot))
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise AttributeError("PolicyBinding is immutable")
        object.__setattr__(self, name, value)

    @property
    def snapshot(self):
        return json.loads(self._canonical.decode("utf-8"))

    @property
    def schema_version(self):
        return json.loads(self._canonical)["policy_schema_version"]

    @property
    def revision(self):
        return json.loads(self._canonical)["policy_revision"]


def canonical_json(value):
    """Return the one canonical byte representation used by V19."""
    try:
        text = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise PolicySnapshotError("policy snapshot 不是 canonical JSON") from error
    return text.encode("utf-8")


def policy_fingerprint(snapshot):
    return hashlib.sha256(canonical_json(snapshot)).hexdigest()


def _layer_document(layer):
    return {
        "policy": layer.policy,
        "allowed_tools": sorted(layer.allowed_tools),
        "max_effect": layer.max_effect,
        "can_write_workspace": layer.can_write_workspace,
        "can_use_mcp": layer.can_use_mcp,
    }


def neutral_delegated_summary():
    """Minimal deterministic input for a non-delegated Main action."""
    return _layer_document(NEUTRAL_DELEGATED_CEILING)


def _mcp_documents(mcp_mappings):
    result = {}
    for reference, mapping in sorted((mcp_mappings or {}).items()):
        if not isinstance(mapping, dict):
            raise PolicySnapshotError("MCP policy mapping 无效")
        result[reference] = {
            "zone": mapping.get("zone", EXTERNAL),
            "profile": mapping.get("profile", "external-reader"),
            "local_effect": mapping.get("local_effect", mapping.get("effect")),
            "policy": mapping.get("policy", DENY),
        }
    return result


def build_policy_snapshot(revision=POLICY_REVISION, global_policy=None,
                          zone_policies=None, capability_profiles=None,
                          mcp_mappings=None):
    """Copy current Harness definitions into a detached JSON document."""
    global_policy = global_policy or GLOBAL_SECURITY_POLICY
    zone_policies = zone_policies or ZONE_POLICIES
    capability_profiles = capability_profiles or CAPABILITY_PROFILES
    profile_documents = {
        name: _layer_document(layer)
        for name, layer in sorted(capability_profiles.items())
    }
    # This is the Harness-owned per-tool MCP ceiling already used by V18.
    profile_documents["mcp-capability"] = _layer_document(CapabilityProfile(
        "mcp-capability", ALLOW, frozenset({"mcp"}),
        "side_effecting", False, True,
    ))
    snapshot = {
        "policy_schema_version": POLICY_SCHEMA_VERSION,
        "policy_revision": revision,
        "definitions": {
            "global_policy": _layer_document(global_policy),
            "trust_zones": {
                name: _layer_document(layer)
                for name, layer in sorted(zone_policies.items())
            },
            "capability_profiles": profile_documents,
            "mcp_capability_mappings": _mcp_documents(mcp_mappings),
            "composition_constants": {
                "decision_order": ["ALLOW", "ASK", "DENY"],
                "effect_order": ["read_only", "side_effecting"],
            },
            "static_security_defaults": {
                "deny_precedence": True,
                "unknown_zone": "DENY",
                "unknown_profile": "DENY",
                "unmapped_mcp_tool": "DENY",
            },
        },
    }
    validate_snapshot(snapshot)
    # Detach every caller-owned container and normalize it to JSON types.
    return json.loads(canonical_json(snapshot).decode("utf-8"))


def mappings_from_registry(registry):
    """Extract only Harness-owned MCP authorization config, never discovery."""
    if registry is None:
        return {}
    references = set(registry.tool_policies) | set(registry.tool_effects)
    # These two demo capabilities are Harness-owned built-ins, not values
    # learned from tools/list. Other discovered tools remain unmapped/denied.
    if "demo" in registry.clients:
        references.add("mcp:demo:echo")
    if "demo-stdio" in registry.clients:
        references.add("mcp:demo-stdio:echo")
    return {
        reference: {
            "zone": EXTERNAL,
            "profile": "mcp-capability",
            "local_effect": registry.tool_effects.get(reference, "unknown"),
            # A Harness-owned effect entry is an explicit mapping. Preserve the
            # teaching registry's historical ASK default for that mapped tool.
            "policy": registry.tool_policies.get(reference, ASK),
        }
        for reference in references
    }


def bind_current_policy(mcp_registry=None, directory=POLICY_DIRECTORY,
                        revision=POLICY_REVISION):
    snapshot = build_policy_snapshot(
        revision=revision, mcp_mappings=mappings_from_registry(mcp_registry)
    )
    fingerprint = persist_snapshot(snapshot, directory)
    return PolicyBinding(snapshot, fingerprint)


def _contains_secret(value):
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in SECRET_PATTERNS)
    if isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_secret(key) or _contains_secret(item)
                   for key, item in value.items())
    return False


def validate_snapshot(snapshot):
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "policy_schema_version", "policy_revision", "definitions"
    }:
        raise PolicySnapshotError("policy snapshot schema 无效")
    version = snapshot.get("policy_schema_version")
    if version != POLICY_SCHEMA_VERSION:
        raise PolicySnapshotError("unsupported historical policy schema")
    if not isinstance(snapshot.get("policy_revision"), str) or not snapshot["policy_revision"]:
        raise PolicySnapshotError("policy revision 无效")
    definitions = snapshot.get("definitions")
    required = {
        "global_policy", "trust_zones", "capability_profiles",
        "mcp_capability_mappings", "composition_constants",
        "static_security_defaults",
    }
    if not isinstance(definitions, dict) or set(definitions) != required:
        raise PolicySnapshotError("policy definitions schema 无效")
    layer_fields = {
        "policy", "allowed_tools", "max_effect",
        "can_write_workspace", "can_use_mcp",
    }
    layers = [definitions["global_policy"]]
    for section in ("trust_zones", "capability_profiles"):
        if not isinstance(definitions[section], dict):
            raise PolicySnapshotError("policy layer collection 无效")
        layers.extend(definitions[section].values())
    for layer in layers:
        if not isinstance(layer, dict) or set(layer) != layer_fields:
            raise PolicySnapshotError("policy layer schema 无效")
        if (layer["policy"] not in DECISIONS
                or layer["max_effect"] not in EFFECTS
                or not isinstance(layer["allowed_tools"], list)
                or not all(isinstance(item, str) and item
                           for item in layer["allowed_tools"])
                or not isinstance(layer["can_write_workspace"], bool)
                or not isinstance(layer["can_use_mcp"], bool)):
            raise PolicySnapshotError("policy layer value 无效")
    mappings = definitions["mcp_capability_mappings"]
    mapping_fields = {"zone", "profile", "local_effect", "policy"}
    if not isinstance(mappings, dict):
        raise PolicySnapshotError("MCP policy mappings 无效")
    for reference, mapping in mappings.items():
        if (not isinstance(reference, str) or not reference.startswith("mcp:")
                or not isinstance(mapping, dict) or set(mapping) != mapping_fields
                or mapping["policy"] not in DECISIONS
                or not all(isinstance(mapping[name], str)
                           for name in ("zone", "profile", "local_effect"))):
            raise PolicySnapshotError("MCP policy mapping schema 无效")
    if definitions["composition_constants"] != {
        "decision_order": ["ALLOW", "ASK", "DENY"],
        "effect_order": ["read_only", "side_effecting"],
    }:
        raise PolicySnapshotError("composition constants 无效")
    if definitions["static_security_defaults"] != {
        "deny_precedence": True, "unknown_zone": "DENY",
        "unknown_profile": "DENY", "unmapped_mcp_tool": "DENY",
    }:
        raise PolicySnapshotError("static security defaults 无效")
    if _contains_secret(snapshot):
        raise PolicySnapshotError("policy snapshot secret screening rejected")
    canonical_json(snapshot)
    return snapshot


def persist_snapshot(snapshot, directory=POLICY_DIRECTORY):
    validate_snapshot(snapshot)
    payload = canonical_json(snapshot) + b"\n"
    fingerprint = policy_fingerprint(snapshot)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, f"{fingerprint}.json")
    if os.path.exists(path):
        loaded = load_policy_snapshot(fingerprint, directory)
        if canonical_json(loaded) != canonical_json(snapshot):
            raise PolicySnapshotError("policy snapshot corruption")
        return fingerprint
    descriptor, temporary = tempfile.mkstemp(prefix=".policy-", suffix=".tmp", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)  # exclusive publication: never replace history
        except FileExistsError:
            load_policy_snapshot(fingerprint, directory)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return fingerprint


def load_policy_snapshot(fingerprint, directory=POLICY_DIRECTORY):
    if not isinstance(fingerprint, str) or not FINGERPRINT_PATTERN.fullmatch(fingerprint):
        raise PolicySnapshotError("policy fingerprint 无效")
    path = os.path.join(directory, f"{fingerprint}.json")
    try:
        with open(path, encoding="utf-8") as stream:
            snapshot = json.load(stream)
    except FileNotFoundError as error:
        raise PolicySnapshotError("historical policy snapshot unavailable") from error
    except (OSError, json.JSONDecodeError) as error:
        raise PolicySnapshotError("policy snapshot corruption") from error
    try:
        validate_snapshot(snapshot)
    except PolicySnapshotError as error:
        if str(error) == "unsupported historical policy schema":
            raise
        raise PolicySnapshotError("policy snapshot corruption") from error
    if policy_fingerprint(snapshot) != fingerprint:
        raise PolicySnapshotError("policy snapshot corruption")
    return snapshot


def binding_from_events(events, directory=POLICY_DIRECTORY):
    started = next((event for event in events if event.get("event_type") == "run_started"), None)
    references = (started or {}).get("references") or {}
    fingerprint = references.get("policy_fingerprint")
    if not fingerprint:
        raise PolicySnapshotError("historical policy binding unavailable")
    snapshot = load_policy_snapshot(fingerprint, directory)
    return PolicyBinding(snapshot, fingerprint)


def _layer_from_document(name, value, profile=False):
    cls = CapabilityProfile if profile else StaticPolicyLayer
    if not isinstance(value, dict):
        return None
    try:
        layer = cls(name, value["policy"], frozenset(value["allowed_tools"]),
                    value["max_effect"], value["can_write_workspace"],
                    value["can_use_mcp"])
    except (KeyError, TypeError):
        return None
    if layer.policy not in DECISIONS or layer.max_effect not in EFFECTS:
        return None
    return layer


def compose_effective_from_snapshot(snapshot, inputs):
    """Compose one EffectivePolicy from immutable snapshot definitions."""
    validate_snapshot(snapshot)
    definitions = snapshot["definitions"]
    zone_name = inputs.get("zone")
    profile_name = inputs.get("profile")
    global_doc = dict(definitions["global_policy"])
    classification = inputs.get("classification", DENY)
    global_doc["policy"] = max(
        (global_doc["policy"], classification), key=DECISION_RANK.get
    ) if classification in DECISIONS else DENY
    global_layer = _layer_from_document("global", global_doc)
    zone = _layer_from_document("zone", definitions["trust_zones"].get(zone_name))
    profile = _layer_from_document(
        profile_name or "profile",
        definitions["capability_profiles"].get(profile_name), True,
    )
    delegated_doc = inputs.get("delegated_ceiling")
    delegated = (
        _layer_from_document("delegated", delegated_doc)
        if delegated_doc is not None else NEUTRAL_DELEGATED_CEILING
    )
    if None in (global_layer, zone, profile, delegated):
        # Reuse the composition function's deterministic invalid-layer denial.
        return compose_static_policy(global_layer, zone, profile, delegated)
    return compose_static_policy(global_layer, zone, profile, delegated)


def compose_from_snapshot(snapshot, inputs):
    """Replay one static authorization from minimal recorded inputs."""
    effective = compose_effective_from_snapshot(snapshot, inputs)
    effect = inputs.get("effect")
    if effect == "unknown":
        effect = "side_effecting"
    return effective.authorize(inputs.get("tool_kind"), effect)


def authority_diff(historical, current):
    """Return a stable, deliberately narrow Authority policy diff."""
    validate_snapshot(historical)
    validate_snapshot(current)
    old, new = historical["definitions"], current["definitions"]
    differences = []

    def compare(prefix, left, right, fields):
        for field in fields:
            before = left.get(field) if isinstance(left, dict) else None
            after = right.get(field) if isinstance(right, dict) else None
            if before != after:
                differences.append((f"{prefix}.{field}", before, after))

    layer_fields = ("policy", "allowed_tools", "max_effect",
                    "can_write_workspace", "can_use_mcp")
    compare("global", old["global_policy"], new["global_policy"], layer_fields)
    for section, label in (("trust_zones", "zone"),
                           ("capability_profiles", "profile")):
        for name in sorted(set(old[section]) | set(new[section])):
            compare(f"{label}.{name}", old[section].get(name),
                    new[section].get(name), layer_fields)
    mapping_fields = ("zone", "profile", "local_effect", "policy")
    section = "mcp_capability_mappings"
    for name in sorted(set(old[section]) | set(new[section])):
        compare(f"mcp.{name}", old[section].get(name), new[section].get(name), mapping_fields)
    return differences


def policy_drift(historical_fingerprint, current_snapshot):
    """Compare only snapshot identities; drift does not invalidate history."""
    return historical_fingerprint != policy_fingerprint(current_snapshot)


def effective_policy_reference(base_fingerprint, delegated_authority,
                               requested_profile="workspace-editor"):
    """Identify subagent attenuation without copying the base snapshot."""
    document = {
        "base_policy_fingerprint": base_fingerprint,
        "delegated_authority": {
            name: delegated_authority.get(name)
            for name in ("allowed_tools", "can_write_workspace", "can_use_mcp")
        },
        "requested_profile": requested_profile,
    }
    return hashlib.sha256(canonical_json(document)).hexdigest()


def replay_policy_events(events, snapshot):
    results = []
    snapshot_fingerprint = policy_fingerprint(snapshot)
    required_inputs = {
        "zone", "profile", "delegated_ceiling", "effect", "tool_kind",
        "classification",
    }
    for event in events:
        if event.get("event_type") != "policy_decision":
            continue
        references = event.get("references") or {}
        inputs = references.get("composition_inputs")
        if not isinstance(inputs, dict) or not required_inputs.issubset(inputs):
            continue
        replayed = compose_from_snapshot(snapshot, inputs)
        recorded = event.get("outcome")
        event_fingerprint = references.get("policy_fingerprint")
        results.append({"sequence": event.get("sequence"), "recorded": recorded,
                        "replayed": replayed,
                        "match": (recorded == replayed
                                  and event_fingerprint == snapshot_fingerprint)})
    return results
