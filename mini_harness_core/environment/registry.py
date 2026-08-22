"""Harness-owned static registry for the two Phase 2 mobile capabilities."""

from dataclasses import dataclass
from types import MappingProxyType
import copy

from .contracts import (
    EnvironmentAdapterResult, EnvironmentCapabilitySpec,
    EnvironmentInvocation, input_schema_identity,
)
from ..integrity import canonical_json_bytes, sha256_identity
from .termux import (
    invoke_termux_capability, invoke_termux_notification,
    validate_termux_notification_arguments,
)


REGISTRY_SCHEMA_VERSION = 1
BATTERY = "termux:battery_status"
NOTIFICATION = "termux:notification"

BATTERY_SCHEMA = {"type": "object", "additionalProperties": False}
NOTIFICATION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "content"],
    "properties": {
        "title": {"type": "string"}, "content": {"type": "string"},
    },
}


@dataclass(frozen=True, slots=True)
class _RegistryEntry:
    spec: EnvironmentCapabilitySpec
    description: str
    input_schema: dict
    policy_profile: str
    adapter: object
    normalizer: object


class UnsupportedEnvironmentCapability(ValueError):
    error_code = "UNSUPPORTED_CAPABILITY"


def _battery_args(arguments):
    if arguments != {}:
        raise ValueError("battery_status arguments must be empty")
    return {}


def _battery_adapter(_invocation):
    return invoke_termux_capability("battery_status")


def _notification_adapter(invocation):
    args = invocation.normalized_args
    return invoke_termux_notification(args["title"], args["content"])


def _entry(name, effect, adapter_id, schema, description, profile,
           adapter, normalizer):
    return _RegistryEntry(
        EnvironmentCapabilitySpec(
            name, effect, "external", adapter_id, 1,
            input_schema_identity(schema),
        ), description, copy.deepcopy(schema), profile, adapter, normalizer,
    )


class EnvironmentCapabilityRegistry:
    """Closed immutable lookup; discovery and external registration do not exist."""

    __slots__ = ("_entries", "_sealed")

    def __init__(self):
        entries = {
            BATTERY: _entry(
                BATTERY, "read_only", "termux_api_battery_status",
                BATTERY_SCHEMA, "Read the current Android battery status.",
                "external-reader", _battery_adapter, _battery_args,
            ),
            NOTIFICATION: _entry(
                NOTIFICATION, "side_effecting", "termux_api_notification",
                NOTIFICATION_SCHEMA, "Submit an Android notification request.",
                "external-actor", _notification_adapter,
                validate_termux_notification_arguments,
            ),
        }
        object.__setattr__(self, "_entries", MappingProxyType(entries))
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise AttributeError("Environment registry is immutable")
        object.__setattr__(self, name, value)

    def contains(self, logical_name):
        return logical_name in self._entries

    def is_environment_intent(self, logical_name):
        return self.contains(logical_name) or (
            isinstance(logical_name, str) and logical_name.startswith("termux:")
        )

    def spec(self, logical_name):
        try:
            return self._entries[logical_name].spec
        except KeyError as error:
            raise UnsupportedEnvironmentCapability(
                "unsupported environment capability"
            ) from error

    def normalize_arguments(self, logical_name, arguments):
        try:
            entry = self._entries[logical_name]
        except KeyError as error:
            raise UnsupportedEnvironmentCapability(
                "unsupported environment capability"
            ) from error
        return entry.normalizer(arguments)

    def policy_profile(self, logical_name):
        try:
            return self._entries[logical_name].policy_profile
        except KeyError as error:
            raise UnsupportedEnvironmentCapability(
                "unsupported environment capability"
            ) from error

    def invoke(self, invocation):
        if not isinstance(invocation, EnvironmentInvocation):
            raise PermissionError("registry requires EnvironmentInvocation")
        try:
            entry = self._entries[invocation.logical_capability]
        except KeyError as error:
            raise UnsupportedEnvironmentCapability(
                "unsupported environment capability"
            ) from error
        normalized = entry.normalizer(invocation.normalized_args)
        if normalized != invocation.normalized_args:
            raise PermissionError("invocation arguments are not normalized")
        result = entry.adapter(invocation)
        if not isinstance(result, EnvironmentAdapterResult):
            raise TypeError("environment adapter violated result contract")
        if (result.logical_capability != entry.spec.logical_name
                or result.effect != entry.spec.effect):
            raise ValueError("environment adapter identity/effect drift")
        return result

    def model_catalog(self):
        return [{
            "capability": name, "description": entry.description,
            "effect": entry.spec.effect,
            "arguments_schema": copy.deepcopy(entry.input_schema),
        } for name, entry in sorted(self._entries.items())]

    def identity(self):
        capabilities = [
            entry.spec.to_dict()
            for _name, entry in sorted(self._entries.items())
        ]
        stable = {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "capabilities": capabilities,
        }
        return {**stable,
                "fingerprint": sha256_identity(canonical_json_bytes(stable))}


ENVIRONMENT_REGISTRY = EnvironmentCapabilityRegistry()


def classify_environment_capability(logical_name, policy_snapshot,
                                    delegated_ceiling=None):
    """Feed registry facts into existing composition; registry grants no policy."""
    from ..policy_snapshot import compose_effective_from_snapshot, neutral_delegated_summary
    try:
        spec = ENVIRONMENT_REGISTRY.spec(logical_name)
        profile = ENVIRONMENT_REGISTRY.policy_profile(logical_name)
    except UnsupportedEnvironmentCapability:
        return {"action": "DENY", "reason": "unsupported environment capability",
                "effect": "read_only", "zone": "external"}
    inputs = {
        "zone": spec.zone, "profile": profile, "classification": "ALLOW",
        "tool_kind": "termux", "effect": spec.effect,
        "delegated_ceiling": delegated_ceiling or neutral_delegated_summary(),
    }
    effective = compose_effective_from_snapshot(policy_snapshot, inputs)
    return {
        "action": effective.authorize("termux", spec.effect),
        "reason": "fixed Harness-owned environment capability",
        "effect": spec.effect, "zone": spec.zone, "trace": effective.trace,
        "composition_inputs": inputs,
    }

