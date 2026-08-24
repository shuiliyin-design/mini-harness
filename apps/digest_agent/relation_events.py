"""Typed manual publisher for durable UserSubscription relation events."""

from dataclasses import dataclass
import copy

from .domain import relation_event_attempt_identity, utc_now


class RelationEventPublisherError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RelationPublishOutcome:
    outcome: str
    error_code: str | None = None

    def __post_init__(self):
        if self.outcome not in {
                "accepted", "explicit_failure", "timeout_unknown"}:
            raise ValueError("invalid relation publisher outcome")
        if self.outcome == "accepted" and self.error_code is not None:
            raise ValueError("accepted publisher outcome cannot have an error")
        if self.outcome != "accepted" and not self.error_code:
            raise ValueError("publisher failure needs a safe error code")


class FakeRelationEventPublisher:
    """Deterministic adapter; stores only the minimal public event projection."""

    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.calls = []

    def publish(self, event_id, event_type, payload):
        self.calls.append({
            "event_id": event_id,
            "event_type": event_type,
            "user_subscription_id": payload["user_subscription_id"],
            "subscription_id": payload["subscription_id"],
            "relation_version": payload["relation_version"],
        })
        if self.outcomes:
            return self.outcomes.pop(0)
        return RelationPublishOutcome("accepted")


@dataclass(frozen=True, slots=True)
class RelationEventWork:
    worker_status: str
    event_id: str | None
    publication_status: str | None
    user_subscription_id: str | None
    subscription_id: str | None
    relation_status: str | None
    attempt_number: int | None
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class RelationEventInspection:
    event_id: str
    event_type: str
    publication_status: str
    outbox_status: str
    attempt_number: int
    attempt_status: str | None
    effect_certainty: str | None
    user_subscription_id: str
    subscription_id: str
    relation_status: str
    safe_recovery_actions: tuple[str, ...]
    blocking_reason: str | None
    updated_at: str


class RelationEventPublisherService:
    """Single-process, manual-tick publisher with an unknown-effect fence."""

    def __init__(self, repository, publisher, clock=utc_now,
                 fault_injector=None):
        self.repository = repository
        self.publisher = publisher
        self.clock = clock
        self.fault_injector = fault_injector

    def _fault(self, stage, value):
        if self.fault_injector is not None:
            self.fault_injector(stage, value)

    def _relation(self, event):
        relation = self.repository.get_user_subscription_for_subscription(
            event.subscription_id,
        )
        if (relation is None
                or relation.user_subscription_id
                != event.user_subscription_id
                or relation.user_id != event.user_id):
            raise RelationEventPublisherError("relation_event_refs_invalid")
        return relation

    @staticmethod
    def _publication_status(event, attempt=None):
        if event.status == "pending":
            return "PENDING"
        if event.status == "retry_wait":
            return "RETRYABLE"
        if event.status == "completed":
            return "SUCCEEDED"
        if event.status == "failed":
            return "FAILED"
        if event.status == "blocked":
            return ("UNKNOWN" if event.last_error_code
                    == "PUBLICATION_UNKNOWN" else "BLOCKED")
        if (event.status == "claimed" and attempt is not None
                and attempt.effect_certainty == "unknown"):
            return "UNKNOWN"
        return "CLAIMED"

    def _work(self, worker_status, event=None, failure_reason=None):
        if event is None:
            return RelationEventWork(
                worker_status, None, None, None, None, None, None, None,
            )
        attempt = self.repository.get_current_relation_event_attempt(
            event.event_id,
        )
        relation = self._relation(event)
        return RelationEventWork(
            worker_status, event.event_id,
            self._publication_status(event, attempt),
            event.user_subscription_id, event.subscription_id,
            relation.status, event.attempt_number, failure_reason,
        )

    def run_once(self):
        timestamp = self.clock()
        event = self.repository.claim_relation_event(timestamp)
        if event is None:
            return self._work("NO_WORK")
        self._fault("after_claim", event)
        self._relation(event)
        attempt_id = relation_event_attempt_identity(
            event.event_id, event.attempt_number,
        )
        self.repository.mark_relation_event_dispatch_started(
            event.event_id, event.version, attempt_id, self.clock(),
        )
        self._fault("after_dispatch_fence", event)
        try:
            outcome = self.publisher.publish(
                event.event_id, event.event_type, copy.deepcopy(event.payload),
            )
        except Exception:
            outcome = RelationPublishOutcome(
                "timeout_unknown", "PUBLICATION_UNKNOWN",
            )
        self._fault("after_publish", outcome)
        error_code = outcome.error_code
        if outcome.outcome == "explicit_failure":
            error_code = error_code or "PUBLISH_NOT_APPLIED"
        elif outcome.outcome == "timeout_unknown":
            error_code = "PUBLICATION_UNKNOWN"
        finalized = self.repository.finalize_relation_event(
            event.event_id, event.version, attempt_id, outcome.outcome,
            error_code, self.clock(), self.clock(),
        )
        worker_status = {
            "accepted": "SUCCEEDED",
            "explicit_failure": "RETRYABLE",
            "timeout_unknown": "BLOCKED",
        }[outcome.outcome]
        return self._work(worker_status, finalized, error_code)

    def drain(self, maximum):
        if type(maximum) is not int or not 1 <= maximum <= 1000:
            raise RelationEventPublisherError("invalid_maximum")
        values = []
        for _index in range(maximum):
            value = self.run_once()
            if value.worker_status == "NO_WORK":
                break
            values.append(value)
        return tuple(values)

    def inspect(self, event_id):
        event = self.repository.get_relation_event(event_id)
        if event is None:
            raise RelationEventPublisherError("not_found")
        attempt = self.repository.get_current_relation_event_attempt(event_id)
        relation = self._relation(event)
        actions = ()
        blocking = None
        if event.status == "claimed":
            if attempt is not None and attempt.status == "prepared":
                actions = ("release_not_started",)
                blocking = "CLAIM_OWNER_UNKNOWN_PUBLISH_NOT_STARTED"
            elif (attempt is not None
                  and attempt.effect_certainty == "unknown"):
                actions = ("block_unknown",)
                blocking = "PUBLICATION_EFFECT_UNKNOWN"
            else:
                blocking = "CLAIM_STATE_INCONSISTENT"
        elif event.status == "blocked":
            blocking = event.last_error_code or "RECONCILIATION_REQUIRED"
        return RelationEventInspection(
            event.event_id, event.event_type,
            self._publication_status(event, attempt), event.status,
            event.attempt_number, attempt.status if attempt else None,
            attempt.effect_certainty if attempt else None,
            event.user_subscription_id, event.subscription_id,
            relation.status, actions, blocking, event.updated_at,
        )

    def inspect_all(self):
        return tuple(self.inspect(event.event_id)
                     for event in self.repository.list_relation_events())

    def recover(self, event_id, action):
        inspection = self.inspect(event_id)
        if action not in inspection.safe_recovery_actions:
            raise RelationEventPublisherError("unsafe_recovery_action")
        event = self.repository.get_relation_event(event_id)
        recovered = self.repository.recover_relation_event(
            event_id, event.version, action, self.clock(),
        )
        return self._work("RECOVERED", recovered, recovered.last_error_code)
