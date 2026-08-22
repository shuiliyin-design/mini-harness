"""Minimal execution-mechanics contract for Harness environment adapters.

This module owns schemas and safe result validation only.  It intentionally
does not import or decide Policy, Approval, Retry, Evidence, Result, Bridge, or
Provider semantics.
"""

from collections.abc import Mapping
from dataclasses import dataclass
import copy
import json
import re

from .integrity import canonical_json_bytes, sha256_identity
from .security import SECRET_PATTERNS


CAPABILITY_NOT_INSTALLED = "CAPABILITY_NOT_INSTALLED"
COMPANION_UNAVAILABLE = "COMPANION_UNAVAILABLE"
TIMEOUT = "TIMEOUT"
INVALID_RESPONSE = "INVALID_RESPONSE"
EXECUTION_FAILED = "EXECUTION_FAILED"
INVALID_ARGUMENT = "INVALID_ARGUMENT"
UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"

EFFECTS = frozenset({"read_only", "side_effecting"})
ZONES = frozenset({"external"})
STATUSES = frozenset({"succeeded", "failed"})
EFFECT_CERTAINTIES = frozenset({
    "no_side_effect", "known_applied", "not_started", "unknown",
})
ERROR_CODES = frozenset({
    CAPABILITY_NOT_INSTALLED, COMPANION_UNAVAILABLE, TIMEOUT,
    INVALID_RESPONSE, EXECUTION_FAILED, INVALID_ARGUMENT,
    UNSUPPORTED_CAPABILITY,
})
FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_INVOCATION_SEAL = object()


def _contains_secret(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def _json_object(value, name, maximum=16_384):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    try:
        encoded = canonical_json_bytes(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be canonical JSON") from error
    if len(encoded) > maximum or _contains_secret(value):
        raise ValueError(f"{name} failed safety validation")
    return copy.deepcopy(value)


@dataclass(frozen=True, slots=True)
class EnvironmentCapabilitySpec:
    logical_name: str
    effect: str
    zone: str
    adapter_id: str
    adapter_version: int
    input_schema_identity: str

    def __post_init__(self):
        if (not isinstance(self.logical_name, str) or ":" not in self.logical_name
                or self.effect not in EFFECTS or self.zone not in ZONES
                or not isinstance(self.adapter_id, str) or not self.adapter_id
                or not isinstance(self.adapter_version, int)
                or isinstance(self.adapter_version, bool)
                or self.adapter_version < 1
                or not FINGERPRINT.fullmatch(str(self.input_schema_identity))):
            raise ValueError("invalid EnvironmentCapabilitySpec")

    def to_dict(self):
        return {
            "logical_name": self.logical_name, "effect": self.effect,
            "zone": self.zone, "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "input_schema_identity": self.input_schema_identity,
        }


@dataclass(frozen=True, slots=True, init=False)
class EnvironmentInvocation:
    logical_capability: str
    normalized_args: dict
    action_id: str
    run_id: str

    def __init__(self, logical_capability, normalized_args, action_id, run_id,
                 _seal=None):
        if _seal is not _INVOCATION_SEAL:
            raise PermissionError("EnvironmentInvocation requires AuthorizedAction")
        if not all(isinstance(value, str) and value for value in
                   (logical_capability, action_id, run_id)):
            raise ValueError("invalid EnvironmentInvocation identity")
        object.__setattr__(self, "logical_capability", logical_capability)
        object.__setattr__(self, "normalized_args",
                           _json_object(normalized_args, "normalized_args"))
        object.__setattr__(self, "action_id", action_id)
        object.__setattr__(self, "run_id", run_id)


@dataclass(frozen=True, slots=True)
class EnvironmentAdapterResult(Mapping):
    logical_capability: str
    status: str
    effect: str
    effect_certainty: str
    safe_observation: dict
    exit_code: int | None
    stdout_length: int
    stdout_sha256: str
    stderr_length: int
    stderr_sha256: str
    error_code: str | None = None

    def __post_init__(self):
        if (not isinstance(self.logical_capability, str)
                or self.status not in STATUSES or self.effect not in EFFECTS
                or self.effect_certainty not in EFFECT_CERTAINTIES
                or self.error_code is not None and self.error_code not in ERROR_CODES
                or self.exit_code is not None and (
                    not isinstance(self.exit_code, int)
                    or isinstance(self.exit_code, bool))):
            raise ValueError("invalid EnvironmentAdapterResult")
        for length, digest in ((self.stdout_length, self.stdout_sha256),
                               (self.stderr_length, self.stderr_sha256)):
            if (not isinstance(length, int) or isinstance(length, bool) or length < 0
                    or not FINGERPRINT.fullmatch(str(digest))):
                raise ValueError("invalid EnvironmentAdapterResult stream identity")
        safe = _json_object(self.safe_observation, "safe_observation")
        object.__setattr__(self, "safe_observation", safe)
        if self.status == "succeeded" and self.error_code is not None:
            raise ValueError("successful adapter result cannot have error_code")

    def to_dict(self):
        return {
            "logical_capability": self.logical_capability,
            "status": self.status, "effect": self.effect,
            "effect_certainty": self.effect_certainty,
            "safe_observation": copy.deepcopy(self.safe_observation),
            "exit_code": self.exit_code,
            "stdout_length": self.stdout_length,
            "stdout_sha256": self.stdout_sha256,
            "stderr_length": self.stderr_length,
            "stderr_sha256": self.stderr_sha256,
            "error_code": self.error_code,
        }

    def __getitem__(self, key):
        # Temporary source compatibility for the Phase 2 adapter teaching APIs.
        if key == "capability":
            return self.logical_capability
        if key == "observation":
            return copy.deepcopy(self.safe_observation)
        return self.to_dict()[key]

    def __iter__(self):
        return iter(self.to_dict())

    def __len__(self):
        return len(self.to_dict())

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


def input_schema_identity(schema):
    return sha256_identity(canonical_json_bytes(_json_object(
        schema, "input_schema", maximum=8_192,
    )))
