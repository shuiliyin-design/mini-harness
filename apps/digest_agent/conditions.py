"""Application-owned deterministic CONDITION observation and evaluation."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import uuid

from mini_harness_core.evidence import (
    EvidenceError, EvidenceStore, create_evidence, observation_identity,
)

from .domain import (
    AcceptedFlightPriceObservation, ConditionEvaluation,
    ConditionObservationCycle, ConditionObservationRequest, DomainError,
    FlightObservationQuery, TrackingUpdate, UpdateDistribution,
    condition_cycle_identity, condition_evaluation_identity,
    condition_update_identity, flight_price_signal_identity,
    update_distribution_identity, utc_now, utc_timestamp,
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
    cycle_id: str | None = None
    emission_decision: str | None = None


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

    @staticmethod
    def _transition(previous_truth, truth):
        if truth == "TRUE":
            if previous_truth == "UNKNOWN":
                return "EMIT_FIRST_MATCH"
            if previous_truth == "FALSE":
                return "EMIT_THRESHOLD_CROSSING"
            return "SUPPRESS_STILL_MATCHED"
        if previous_truth == "TRUE":
            return "SUPPRESS_REARMED"
        return "SUPPRESS_FALSE"

    @staticmethod
    def _request_cycle(state, kind, scheduled_due_at, coalesced_from_at,
                       coalesced_to_at, coalesced_count, timestamp,
                       idempotency_key=None):
        cycle_id = condition_cycle_identity(
            state.subscription_id, state.execution_policy_version,
            scheduled_due_at, kind,
        )
        request_id = hashlib.sha256(
            (("manual-condition-request\n" + state.subscription_id + "\n"
              + idempotency_key) if idempotency_key is not None else
             "condition-cycle-request\n" + cycle_id).encode("utf-8"),
        ).hexdigest()[:32]
        key = idempotency_key or f"condition-cycle:{cycle_id}"
        request = ConditionObservationRequest(
            request_id, state.subscription_id, state.definition_id,
            state.definition_version, key, "PENDING", None, None,
            timestamp, timestamp,
        )
        cycle = ConditionObservationCycle(
            cycle_id, request_id, state.subscription_id, state.definition_id,
            state.definition_version, state.execution_policy_version, kind,
            scheduled_due_at, coalesced_from_at, coalesced_to_at,
            coalesced_count, "PENDING", None, None, None, None, None, None,
            None, None, None, timestamp, timestamp,
        )
        return request, cycle

    def reserve_manual_cycle(self, subscription_id, idempotency_key):
        state = self.repository.get_condition_temporal_state(subscription_id)
        if state is None or state.lifecycle_status != "ACTIVE":
            raise ConditionProcessingError("condition_binding_invalid")
        timestamp = self.clock()
        request, cycle = self._request_cycle(
            state, "MANUAL", timestamp, timestamp, timestamp, 1, timestamp,
            idempotency_key=idempotency_key,
        )
        stored = self.repository.reserve_manual_condition_cycle(
            request, cycle,
        )
        if stored is None:
            raise ConditionProcessingError("condition_persist_failed")
        return stored.request_id

    def plan_due_cycles(self, maximum=100):
        if type(maximum) is not int or not 1 <= maximum <= 1000:
            raise ConditionProcessingError("invalid_maximum")
        timestamp = self.clock()
        self.repository.expire_condition_temporal_states(timestamp)
        planned = []
        for state in self.repository.list_due_condition_temporal_states(
                timestamp, maximum):
            now = _utc(timestamp)
            first_due = _utc(state.next_due_at)
            cadence = timedelta(seconds=state.cadence_seconds)
            elapsed = max(0.0, (now - first_due).total_seconds())
            skipped = int(elapsed // state.cadence_seconds)
            latest_due = first_due + skipped * cadence
            kind = "SCHEDULED" if now == first_due else "CATCH_UP"
            scheduled = utc_timestamp(latest_due)
            request, cycle = self._request_cycle(
                state, kind, scheduled, state.next_due_at, scheduled,
                skipped + 1, timestamp,
            )
            stored, created = self.repository.reserve_condition_cycle(
                state.version, request, cycle,
                utc_timestamp(latest_due + cadence),
            )
            if created:
                planned.append(stored)
        return tuple(planned)

    def _fail_claimed(self, cycle, claim_token, code, timestamp):
        failed = self.repository.fail_condition_cycle(
            cycle.cycle_id, claim_token, code, timestamp,
        )
        return ConditionWorkResult(
            "FAILED", cycle.request_id, cycle.subscription_id, "FAILED",
            None, None, None, False, code, failed.cycle_id, None,
        )

    def _run_cycle_once(self):
        timestamp = self.clock()
        claim_token = uuid.uuid4().hex
        recovery_before = utc_timestamp(
            _utc(timestamp) - timedelta(minutes=5),
        )
        claimed = self.repository.claim_condition_cycle(
            claim_token, timestamp, recovery_before,
        )
        if claimed is None:
            return None
        request, cycle, state = claimed
        try:
            tracking = self.repository.get_tracking_definition(
                cycle.definition_id, cycle.definition_version,
            )
            policies = self.repository.get_tracking_policy(
                cycle.subscription_id, cycle.definition_id,
                cycle.definition_version,
            )
            product = self.repository.get_product_subscription(
                cycle.subscription_id,
            )
            relation = self.repository.get_user_subscription_for_subscription(
                cycle.subscription_id,
            )
            if (tracking is None or policies is None or product is None
                    or relation is None or product.workflow_kind != "CONDITION"
                    or product.status != "ACTIVE"
                    or relation.status != "ACTIVE"
                    or state.lifecycle_status != "ACTIVE"):
                raise ConditionProcessingError("condition_binding_invalid")
            route = tracking.snapshot["route"]
            query = FlightObservationQuery(
                route["origin"], route["destination"], route["trip_type"],
                tracking.snapshot["travel_month"],
            )
            try:
                quote = self.provider.observe(query)
            except TimeoutError:
                return self._fail_claimed(
                    cycle, claim_token, "PROVIDER_TIMEOUT", timestamp,
                )
            except DomainError:
                return self._fail_claimed(
                    cycle, claim_token, "INVALID_OBSERVATION", timestamp,
                )
            except Exception:
                return self._fail_claimed(
                    cycle, claim_token, "PROVIDER_ERROR", timestamp,
                )
            observed_at = _utc(quote.observed_at)
            age = (_utc(timestamp) - observed_at).total_seconds()
            freshness = policies.execution["freshness_seconds"]
            if age > freshness or age < -300:
                return self._fail_claimed(
                    cycle, claim_token, "STALE_OBSERVATION", timestamp,
                )
            observation_id, evidence_id, _signal_identity, evidence = (
                self._quote_evidence(cycle.subscription_id, quote)
            )
            existing_evaluation_id = condition_evaluation_identity(
                cycle.subscription_id, cycle.definition_id,
                cycle.definition_version, observation_id,
            )
            existing = self.repository.get_condition_evaluation(
                existing_evaluation_id,
            )
            if state.last_observed_at is not None and existing is None:
                last = _utc(state.last_observed_at)
                if observed_at < last:
                    return self._fail_claimed(
                        cycle, claim_token, "OUT_OF_ORDER_OBSERVATION",
                        timestamp,
                    )
                if observed_at == last:
                    return self._fail_claimed(
                        cycle, claim_token, "OBSERVATION_CONFLICT", timestamp,
                    )
            try:
                self.evidence_store.save(evidence)
            except (EvidenceError, OSError):
                return self._fail_claimed(
                    cycle, claim_token, "EVIDENCE_PERSIST_FAILED", timestamp,
                )
            accepted = AcceptedFlightPriceObservation(
                observation_id, cycle.subscription_id, quote, evidence_id,
                flight_price_signal_identity(cycle.subscription_id, quote),
                timestamp,
            )
            criterion = tracking.snapshot["signal"]["criterion"]
            evaluation = existing or ConditionEvaluation(
                existing_evaluation_id, cycle.subscription_id,
                cycle.definition_id, cycle.definition_version,
                observation_id, evidence_id, quote.price, criterion["value"],
                quote.currency, criterion["operator"],
                "MATCHED" if quote.price < criterion["value"] else "NO_UPDATE",
                policies.execution["evaluator_version"], timestamp,
            )
            truth = "TRUE" if evaluation.result == "MATCHED" else "FALSE"
            decision = (
                "DUPLICATE_OBSERVATION" if existing is not None else
                self._transition(state.previous_truth, truth)
            )
            update = distribution = None
            if decision in {
                    "EMIT_FIRST_MATCH", "EMIT_THRESHOLD_CROSSING"}:
                update_id = condition_update_identity(
                    evaluation.evaluation_id,
                )
                payload = {
                    "title": "深圳—武汉 9 月往返机票达到提醒条件",
                    "summary": (
                        f"最近往返价格 ¥{quote.price}，低于你设置的 "
                        f"¥{criterion['value']}。"
                    ),
                    "origin": quote.origin, "destination": quote.destination,
                    "travel_month": quote.travel_month,
                    "observed_price": quote.price,
                    "threshold": criterion["value"],
                    "currency": quote.currency,
                    "observed_at": quote.observed_at,
                }
                update = TrackingUpdate(
                    update_id, cycle.subscription_id, cycle.definition_id,
                    cycle.definition_version, evaluation.evaluation_id,
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
            result = self.repository.complete_condition_cycle(
                request, cycle, claim_token, state.version, accepted,
                evaluation, decision, update, distribution,
                self.fault_injector,
            )
            (completed, stored_evaluation, stored_update,
             stored_distribution, stored_cycle, _stored_state, reused,
             completion_status) = result
            if completion_status == "SUPERSEDED":
                return ConditionWorkResult(
                    "SUPERSEDED", completed.request_id,
                    completed.subscription_id, "PAUSED", None, None, None,
                    False, None, stored_cycle.cycle_id, None,
                )
            worker = (
                "REUSED" if reused else
                "UPDATE_CREATED" if stored_update is not None else
                "NO_UPDATE"
            )
            return ConditionWorkResult(
                worker, completed.request_id, completed.subscription_id,
                stored_evaluation.result, stored_evaluation.evaluation_id,
                stored_update.update_id if stored_update else None,
                (stored_distribution.distribution_id
                 if stored_distribution else None),
                reused, None, stored_cycle.cycle_id,
                stored_cycle.emission_decision,
            )
        except Exception:
            self.repository.release_condition_cycle_claim(
                cycle.cycle_id, claim_token, timestamp,
            )
            raise

    def run_once(self):
        result = self._run_cycle_once()
        if result is not None:
            return result
        return self._run_legacy_once()

    def tick(self, maximum=100):
        self.plan_due_cycles(maximum)
        return self.drain(maximum)

    def _run_legacy_once(self):
        request = self.repository.get_pending_legacy_condition_request()
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
