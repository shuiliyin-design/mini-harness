"""Deterministic, Harness-local crash hooks for offline failure tests.

The hook is deliberately absent from CLI, Session, Envelope, provider input,
and action arguments.  Only an embedding test/developer may pass an instance
directly to ``run_agent`` or the dispatch seam.
"""

from collections import Counter


FAULT_POINTS = frozenset({
    "after_tool_success_before_terminal_checkpoint",
    "after_terminal_checkpoint_before_audit",
    "after_audit_before_session",
    "after_session_before_evidence",
    "after_evidence_before_artifact",
    "after_artifact_before_result",
})

TRUTH_ORDER = (
    "external_effect",
    "checkpoint_durability",
    "observation_evidence_durability",
    "artifact_acceptance",
    "result_persistence",
    "audit_completeness",
)


class InjectedFault(BaseException):
    """A simulated process crash; ordinary persistence handlers must not lie."""

    def __init__(self, point):
        super().__init__(f"deterministic fault injected at {point}")
        self.point = point


class DeterministicFaultInjector:
    """Fire configured points a deterministic number of times, then disarm."""

    __slots__ = ("_remaining", "hits")

    def __init__(self, points=()):
        unknown = set(points) - FAULT_POINTS
        if unknown:
            raise ValueError(f"unknown fault point: {sorted(unknown)[0]}")
        self._remaining = Counter(points)
        self.hits = []

    def trigger(self, point):
        if point not in FAULT_POINTS:
            raise ValueError(f"unknown fault point: {point}")
        if self._remaining[point] > 0:
            self._remaining[point] -= 1
            self.hits.append(point)
            raise InjectedFault(point)


def trigger_fault(injector, point):
    """Near-zero-cost production path: one ``None`` branch per boundary."""
    if injector is not None:
        if not isinstance(injector, DeterministicFaultInjector):
            raise TypeError("fault injector must be Harness-created")
        injector.trigger(point)
