"""Static policy composition over Harness-owned capability ceilings.

Purpose: combine global, local, trust-zone, capability, and delegated limits.
Owns: static ``ALLOW``/``ASK``/``DENY`` composition and capability intersection.
Does Not Own: current run state, approval, deadlines, durability, retry,
verification, or execution.
Key Invariants: ``DENY > ASK > ALLOW``; every composition is monotonic; Policy
and action Effect are independent facts; server/project metadata cannot elevate
Harness authority.
"""

from dataclasses import dataclass, replace
from typing import FrozenSet


ALLOW = "ALLOW"
ASK = "ASK"
DENY = "DENY"
DECISIONS = frozenset({ALLOW, ASK, DENY})
DECISION_RANK = {ALLOW: 0, ASK: 1, DENY: 2}

READ_ONLY = "read_only"
SIDE_EFFECTING = "side_effecting"
EFFECTS = frozenset({READ_ONLY, SIDE_EFFECTING})
EFFECT_RANK = {READ_ONLY: 0, SIDE_EFFECTING: 1}

HARNESS_LOCAL = "harness_local"
WORKSPACE = "workspace"
EXTERNAL = "external"
TRUST_ZONES = frozenset({HARNESS_LOCAL, WORKSPACE, EXTERNAL})


@dataclass(frozen=True)
class StaticPolicyLayer:
    """One Harness-owned static ceiling; every field participates in merge."""

    name: str
    policy: str
    allowed_tools: FrozenSet[str]
    max_effect: str
    can_write_workspace: bool
    can_use_mcp: bool


@dataclass(frozen=True)
class CapabilityProfile(StaticPolicyLayer):
    """Named reusable static capability ceiling."""


@dataclass(frozen=True)
class EffectivePolicy:
    policy: str
    allowed_tools: FrozenSet[str]
    max_effect: str
    can_write_workspace: bool
    can_use_mcp: bool
    trace: dict

    def authorize(self, tool, effect):
        """Apply action classification to the already-composed ceiling."""
        if self.policy == DENY:
            return DENY
        if not isinstance(tool, str) or tool not in self.allowed_tools:
            return DENY
        if effect not in EFFECTS:
            return DENY
        if EFFECT_RANK[effect] > EFFECT_RANK[self.max_effect]:
            return DENY
        if tool == "mcp" and not self.can_use_mcp:
            return DENY
        if (
            tool == "shell" and effect == SIDE_EFFECTING
            and not self.can_write_workspace
        ):
            return DENY
        return self.policy


@dataclass(frozen=True)
class RuntimeGateResult:
    """Compact result shape for existing, independently evaluated gates."""

    allowed: bool
    gate: str
    reason: str

    def as_dict(self):
        return {"allowed": self.allowed, "gate": self.gate, "reason": self.reason}


@dataclass(frozen=True)
class SafetyReconciliationPermit:
    """One exact, read-only runtime exception; never a static policy override."""

    target: str
    max_effect: str = READ_ONLY
    remaining_actions: int = 1
    mode: str = "safety_reconciliation"

    def decide(self, target, effect, harness_approved, global_allowed,
               secret_allowed=True):
        if not global_allowed or not secret_allowed:
            return RuntimeGateResult(False, self.mode, "security policy denied")
        if self.remaining_actions != 1:
            return RuntimeGateResult(False, self.mode, "permit exhausted")
        if not harness_approved:
            return RuntimeGateResult(
                False, self.mode, "capability is not Harness-approved"
            )
        if target != self.target:
            return RuntimeGateResult(False, self.mode, "target is unrelated")
        if effect != READ_ONLY or self.max_effect != READ_ONLY:
            return RuntimeGateResult(False, self.mode, "read-only action required")
        return RuntimeGateResult(True, self.mode, "targeted read-only reconciliation")

    def consume(self):
        if self.remaining_actions != 1:
            raise ValueError("safety reconciliation permit exhausted")
        return replace(self, remaining_actions=0)


GLOBAL_SECURITY_POLICY = StaticPolicyLayer(
    "global", ALLOW, frozenset({"builtin", "shell", "mcp", "termux"}),
    SIDE_EFFECTING, True, True,
)

ZONE_POLICIES = {
    HARNESS_LOCAL: StaticPolicyLayer(
        "zone", ALLOW, frozenset({"builtin"}), READ_ONLY, False, False,
    ),
    WORKSPACE: StaticPolicyLayer(
        "zone", ALLOW, frozenset({"shell"}), SIDE_EFFECTING, True, False,
    ),
    EXTERNAL: StaticPolicyLayer(
        "zone", ALLOW, frozenset({"mcp", "termux"}), SIDE_EFFECTING, False, True,
    ),
}

CAPABILITY_PROFILES = {
    "readonly-local": CapabilityProfile(
        "readonly-local", ALLOW, frozenset({"shell"}), READ_ONLY, False, False,
    ),
    "workspace-editor": CapabilityProfile(
        "workspace-editor", ASK, frozenset({"shell"}), SIDE_EFFECTING, True, False,
    ),
    # Per-tool MCP policy still decides ALLOW/ASK/DENY.  This profile supplies
    # the external/read-only ceiling and cannot turn a local ASK into ALLOW.
    "external-reader": CapabilityProfile(
        "external-reader", ALLOW, frozenset({"mcp", "termux"}), READ_ONLY, False, True,
    ),
    "external-actor": CapabilityProfile(
        "external-actor", ASK, frozenset({"termux"}), SIDE_EFFECTING, False, False,
    ),
}

NEUTRAL_DELEGATED_CEILING = StaticPolicyLayer(
    "delegated", ALLOW, frozenset({"builtin", "shell", "mcp", "termux"}),
    SIDE_EFFECTING, True, True,
)


def _valid_layer(layer):
    return bool(
        isinstance(layer, StaticPolicyLayer)
        and isinstance(layer.name, str) and layer.name
        and layer.policy in DECISIONS
        and isinstance(layer.allowed_tools, frozenset)
        and all(isinstance(tool, str) and tool for tool in layer.allowed_tools)
        and layer.max_effect in EFFECTS
        and isinstance(layer.can_write_workspace, bool)
        and isinstance(layer.can_use_mcp, bool)
    )


def _deny_effective(layer_names, reason):
    trace = {name: DENY for name in layer_names}
    disposition = {
        **trace, "effective": DENY, "limiting_factor": reason,
        "limiting_layers": [reason],
    }
    trace.update({
        "effective": DENY, "limiting_factor": reason,
        "disposition": disposition,
    })
    return EffectivePolicy(
        DENY, frozenset(), READ_ONLY, False, False, trace,
    )


def compose_static_policy(global_policy, zone_policy, profile,
                          delegated=None):
    """Intersect four ceilings with deterministic, fail-closed semantics."""
    layers = (
        global_policy, zone_policy, profile,
        delegated if delegated is not None else NEUTRAL_DELEGATED_CEILING,
    )
    names = ("global", "zone", "profile", "delegated")
    if not all(_valid_layer(layer) for layer in layers):
        return _deny_effective(names, "invalid_layer")

    decision = max((layer.policy for layer in layers), key=DECISION_RANK.get)
    max_effect = min(
        (layer.max_effect for layer in layers), key=EFFECT_RANK.get
    )
    tools = frozenset.intersection(*(layer.allowed_tools for layer in layers))
    write_allowed = all(layer.can_write_workspace for layer in layers)
    mcp_allowed = all(layer.can_use_mcp for layer in layers)
    limiting = next(
        name for name, layer in zip(names, layers) if layer.policy == decision
    )
    factor_name = {
        "global": "global", "zone": "zone", "profile": "profile",
        "delegated": "delegation",
    }
    disposition_limiters = [
        factor_name[name] for name, layer in zip(names, layers)
        if layer.policy == decision
    ]

    def boolean_dimension(attribute, effective):
        values = {
            name: getattr(layer, attribute)
            for name, layer in zip(names, layers)
        }
        limiters = [
            factor_name[name] for name in names if not values[name]
        ]
        return {
            **values,
            "effective": effective,
            "limiting_factor": limiters[0] if limiters else None,
            "limiting_layers": limiters,
        }

    effect_values = {
        name: layer.max_effect for name, layer in zip(names, layers)
    }
    effect_limiters = [
        factor_name[name] for name in names
        if effect_values[name] == max_effect
    ]
    tool_values = {
        name: sorted(layer.allowed_tools)
        for name, layer in zip(names, layers)
    }
    tool_universe = set().union(*(layer.allowed_tools for layer in layers))
    removed_by = {
        tool: [
            factor_name[name] for name, layer in zip(names, layers)
            if tool not in layer.allowed_tools
        ]
        for tool in sorted(tool_universe)
        if tool not in tools
    }
    tool_limiters = []
    for tool in sorted(removed_by):
        for factor in removed_by[tool]:
            if factor not in tool_limiters:
                tool_limiters.append(factor)

    disposition_trace = {
        **{name: layer.policy for name, layer in zip(names, layers)},
        "effective": decision,
        "limiting_factor": factor_name[limiting],
        "limiting_layers": disposition_limiters,
    }
    trace = {
        **{name: layer.policy for name, layer in zip(names, layers)},
        "effective": decision,
        "limiting_factor": factor_name[limiting],
        "effective_effect_ceiling": max_effect,
        "tool_allowed": bool(tools),
        "write_allowed": write_allowed,
        "mcp_allowed": mcp_allowed,
        "disposition": disposition_trace,
        "write": boolean_dimension("can_write_workspace", write_allowed),
        "mcp": boolean_dimension("can_use_mcp", mcp_allowed),
        "effect_ceiling": {
            **effect_values,
            "effective": max_effect,
            "limiting_factor": effect_limiters[0],
            "limiting_layers": effect_limiters,
        },
        "allowed_tools": {
            **tool_values,
            "effective": sorted(tools),
            "removed_by": removed_by,
            "limiting_factor": tool_limiters[0] if tool_limiters else None,
            "limiting_layers": tool_limiters,
        },
    }
    return EffectivePolicy(
        decision, tools, max_effect, write_allowed, mcp_allowed, trace,
    )


def policy_for(zone, profile_name, global_policy=GLOBAL_SECURITY_POLICY,
               delegated=None):
    """Resolve only Harness-owned zone/profile names; unknown values deny."""
    zone_policy = ZONE_POLICIES.get(zone)
    profile = CAPABILITY_PROFILES.get(profile_name)
    if zone_policy is None or profile is None:
        return _deny_effective(
            ("global", "zone", "profile", "delegated"),
            "unknown_zone" if zone_policy is None else "unknown_profile",
        )
    return compose_static_policy(global_policy, zone_policy, profile, delegated)


def delegated_ceiling(authority, name="delegated"):
    """Convert a validated handoff authority into a static attenuation layer."""
    if not isinstance(authority, dict):
        return None
    tools = authority.get("allowed_tools")
    write = authority.get("can_write_workspace")
    mcp = authority.get("can_use_mcp")
    if (
        not isinstance(tools, (list, tuple, set, frozenset))
        or not all(isinstance(tool, str) and tool for tool in tools)
        or not isinstance(write, bool) or not isinstance(mcp, bool)
    ):
        return None
    normalized = frozenset(
        "mcp" if tool == "mcp" or tool.startswith("mcp:") else tool
        for tool in tools
    )
    return StaticPolicyLayer(
        name, ALLOW, normalized,
        SIDE_EFFECTING if write else READ_ONLY, write, mcp,
    )


def compose_subagent_policy(
    main_effective, requested_profile_name, handoff_authority, zone=WORKSPACE,
):
    """Attenuate Main by both requested profile and the handoff ceiling."""
    if not isinstance(main_effective, EffectivePolicy):
        return _deny_effective(
            ("global", "zone", "profile", "delegated"), "invalid_main_policy"
        )
    main_ceiling = StaticPolicyLayer(
        "global", main_effective.policy, main_effective.allowed_tools,
        main_effective.max_effect, main_effective.can_write_workspace,
        main_effective.can_use_mcp,
    )
    delegated = delegated_ceiling(handoff_authority)
    if delegated is None:
        return _deny_effective(
            ("global", "zone", "profile", "delegated"), "invalid_delegation"
        )
    return policy_for(zone, requested_profile_name, main_ceiling, delegated)


def local_mcp_mapping(reference, effect):
    """Build trusted classification from Harness config, never server metadata."""
    if not isinstance(reference, str) or not reference.startswith("mcp:"):
        return None
    if effect not in EFFECTS:
        return None
    return {
        "tool": reference, "tool_kind": "mcp", "zone": EXTERNAL,
        "profile": "external-reader", "effect": effect,
    }
