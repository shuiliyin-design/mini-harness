"""Application-owned deterministic CONDITION observation and evaluation."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os

from mini_harness_core.evidence import (
    EvidenceError, EvidenceStore, create_evidence, observation_identity,
)

from .domain import (
    AcceptedFlightPriceObservation, ConditionEvaluation, DomainError,
    FlightObservationQuery, TrackingUpdate, UpdateDistribution,
    condition_evaluation_identity, condition_update_identity,
    flight_price_signal_identity, update_distribution_identity, utc_now,
)


class ConditionProcessingError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ConditionWorkResult:
    worker_status: str
    request_id: str | None
    subscription_id: str | None
    monitoring_status: str | None
    evaluation_id: str | None
    update_id: str | None
    distribution_id: str | None
    reused: bool
    failure_code: str | None = None


def _utc(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise DomainError("flight observed_at 无效") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DomainError("flight observed_at 必须包含 timezone")
    return parsed.astimezone(timezone.utc)


class FlightConditionService:
    """Observe once, accept Evidence, and evaluate ``price < threshold``."""

    def __init__(self, repository, provider, evidence_store, *, clock=None,
                 fault_injector=None):
        self.repository = repository
        self.provider = provider
        self.evidence_store = evidence_store
        self.clock = clock or utc_now
        self.fault_injector = fault_injector

    def _fault(self, stage, value):
        if self.fault_injector is not None:
            self.fault_injector(stage, value)

    @staticmethod
    def _quote_evidence(subscription_id, quote):
        signal_identity = flight_price_signal_identity(subscription_id, quote)
        observation_id = signal_identity[:32]
        evidence_id = hashlib.sha256(
            f"flight-price-evidence\n{observation_id}".encode("utf-8"),
        ).hexdigest()[:32]
        event_id = hashlib.sha256(
            f"flight-price-observation\n{observation_id}".encode("utf-8"),
        ).hexdigest()[:32]
        action_id = hashlib.sha256(
            f"fake-flight-price-read\n{observation_id}".encode("utf-8"),
        ).hexdigest()[:32]
        payload = json.dumps(
            quote.as_dict(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        empty = hashlib.sha256(b"").hexdigest()
        observation = {
            "exit_code": 0,
            "stdout_length": len(payload),
            "stdout_sha256": hashlib.sha256(payload).hexdigest(),
            "stderr_length": 0,
            "stderr_sha256": empty,
        }
        record = create_evidence(
            observation_id,
            "tool_observation",
            {
                "kind": "external_observation",
                "target": "flight_round_trip_price",
                "claim": "typed_price_quote_observed",
            },
            source={
                "action_id": action_id,
                "logical_action_id": action_id,
                "attempt": 1,
                "observation_event_id": event_id,
                "tool": "fake_flight_price",
                "run_id": observation_id,
            },
            verification={"accepted": True, "read_only": True},
            freshness={
                "scope": "run", "observed_at": quote.observed_at,
                "run_id": observation_id,
            },
            content_identity={
                "claim": quote.as_dict(),
                "observation": observation_identity(observation, event_id),
            },
            references={"subscription_id": subscription_id},
            evidence_id=evidence_id,
            created_at=quote.observed_at,
        )
        return observation_id, evidence_id, signal_identity, record

    def _fail(self, request, code, timestamp):
        failed = self.repository.fail_condition_request(
            request.request_id, code, timestamp,
        )
        return ConditionWorkResult(
            "FAILED", failed.request_id, failed.subscription_id,
            "FAILED", None, None, None, False, code,
        )

    def run_once(self):
        request = self.repository.get_pending_condition_request()
        if request is None:
            return ConditionWorkResult(
                "NO_WORK", None, None, None, None, None, None, False,
            )
        tracking = self.repository.get_tracking_definition(
            request.definition_id, request.definition_version,
        )
        policies = self.repository.get_tracking_policy(
            request.subscription_id, request.definition_id,
            request.definition_version,
        )
        product = self.repository.get_product_subscription(
            request.subscription_id,
        )
        relation = self.repository.get_user_subscription_for_subscription(
            request.subscription_id,
        )
        if (tracking is None or policies is None or product is None
                or relation is None or product.workflow_kind != "CONDITION"
                or product.status != "ACTIVE" or relation.status != "ACTIVE"):
            raise ConditionProcessingError("condition_binding_invalid")
        route = tracking.snapshot["route"]
        query = FlightObservationQuery(
            route["origin"], route["destination"], route["trip_type"],
            tracking.snapshot["travel_month"],
        )
        timestamp = self.clock()
        try:
            quote = self.provider.observe(query)
        except DomainError:
            return self._fail(request, "INVALID_OBSERVATION", timestamp)
        now = _utc(timestamp)
        observed_at = _utc(quote.observed_at)
        freshness = policies.execution["freshness_seconds"]
        age = (now - observed_at).total_seconds()
        if age > freshness or age < -300:
            return self._fail(request, "STALE_OBSERVATION", timestamp)
        observation_id, evidence_id, signal_identity, evidence = (
            self._quote_evidence(request.subscription_id, quote)
        )
        try:
            self.evidence_store.save(evidence)
        except (EvidenceError, OSError) as error:
            raise ConditionProcessingError("evidence_persist_failed") from error
        accepted = AcceptedFlightPriceObservation(
            observation_id, request.subscription_id, quote, evidence_id,
            signal_identity, timestamp,
        )
        criterion = tracking.snapshot["signal"]["criterion"]
        evaluation_id = condition_evaluation_identity(
            request.subscription_id, request.definition_id,
            request.definition_version, observation_id,
        )
        existing = self.repository.get_condition_evaluation(evaluation_id)
        if existing is not None:
            linked = self.repository.link_condition_request(
                request.request_id, existing.evaluation_id, timestamp,
            )
            update = self.repository.get_update_for_evaluation(
                existing.evaluation_id,
            )
            distribution = (
                self.repository.get_distribution_for_update(update.update_id)
                if update is not None else None
            )
            return ConditionWorkResult(
                "REUSED", linked.request_id, linked.subscription_id,
                existing.result, existing.evaluation_id,
                update.update_id if update is not None else None,
                (distribution.distribution_id
                 if distribution is not None else None), True,
            )
        evaluation = ConditionEvaluation(
            evaluation_id, request.subscription_id, request.definition_id,
            request.definition_version, observation_id, evidence_id,
            quote.price, criterion["value"], quote.currency,
            criterion["operator"],
            "MATCHED" if quote.price < criterion["value"] else "NO_UPDATE",
            policies.execution["evaluator_version"], timestamp,
        )
        update = distribution = None
        if evaluation.result == "MATCHED":
            update_id = condition_update_identity(evaluation.evaluation_id)
            payload = {
                "title": "深圳—武汉 9 月往返机票达到提醒条件",
                "summary": (
                    f"最近往返价格 ¥{quote.price}，低于你设置的 "
                    f"¥{criterion['value']}。"
                ),
                "origin": quote.origin,
                "destination": quote.destination,
                "travel_month": quote.travel_month,
                "observed_price": quote.price,
                "threshold": criterion["value"],
                "currency": quote.currency,
                "observed_at": quote.observed_at,
            }
            update = TrackingUpdate(
                update_id, request.subscription_id, request.definition_id,
                request.definition_version, evaluation.evaluation_id,
                evidence_id, "CONDITION", payload, quote.observed_at,
                timestamp,
            )
            distribution = UpdateDistribution(
                update_distribution_identity(
                    update_id, relation.user_subscription_id,
                ),
                update_id, relation.user_subscription_id, "AVAILABLE",
                timestamp,
            )
        try:
            completed, stored_evaluation, stored_update, stored_distribution, reused = (
                self.repository.complete_condition_request(
                    request, accepted, evaluation, update, distribution,
                    self.fault_injector,
                )
            )
        except (DomainError, ValueError) as error:
            raise ConditionProcessingError("condition_persist_failed") from error
        return ConditionWorkResult(
            ("UPDATE_CREATED" if stored_update is not None else "NO_UPDATE"),
            completed.request_id, completed.subscription_id,
            stored_evaluation.result, stored_evaluation.evaluation_id,
            stored_update.update_id if stored_update is not None else None,
            (stored_distribution.distribution_id
             if stored_distribution is not None else None),
            reused,
        )

    def drain(self, maximum=100):
        if type(maximum) is not int or not 1 <= maximum <= 1000:
            raise ConditionProcessingError("invalid_maximum")
        values = []
        for _ in range(maximum):
            value = self.run_once()
            if value.worker_status == "NO_WORK":
                break
            values.append(value)
        return tuple(values)


def build_flight_condition_service(repository, provider, audit_path, **kwargs):
    """Keep Evidence-store composition out of transports/bootstrap internals."""
    return FlightConditionService(
        repository, provider,
        EvidenceStore(os.path.join(audit_path, "evidence")), **kwargs,
    )
