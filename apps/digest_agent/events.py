"""P4.6 deterministic Verified EVENT vertical slice."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import uuid

from mini_harness_core.agent import run_agent
from mini_harness_core.audit import AuditWriter
from mini_harness_core.evidence import EvidenceError, EvidenceStore, create_evidence
from mini_harness_core.result import ResultError, ResultStore

from .adapters.provider import FinalCandidateProvider
from .domain import (
    DomainError, EventCandidate, EventCandidateSupport,
    EventObservationCycle, EventObservationQuery, EventVerification,
    TrackingUpdate, UpdateDistribution, VerifiedEvent,
    event_candidate_identity, event_cycle_identity,
    event_harness_run_identity, event_update_identity,
    event_verification_identity, normalize_event_model_name,
    update_distribution_identity, utc_now, utc_timestamp,
    verified_event_identity,
)


class EventProcessingError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class EventWorkResult:
    worker_status: str
    subscription_id: str | None
    outcome: str | None
    reason_code: str | None
    event_id: str | None
    update_id: str | None
    distribution_id: str | None
    cycle_id: str | None
    reused: bool = False
    failure_code: str | None = None


def _utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise DomainError("EVENT timestamp 必须包含 timezone")
    return parsed.astimezone(timezone.utc)


class VerifiedEventService:
    """Agent proposes candidates; Application deterministically verifies truth."""

    RELEASE_MARKERS = (
        "we are releasing", "openai released", "now available",
        "is now available", "现已推出", "正式发布",
    )
    CONFLICT_MARKERS = (
        "coming soon", "not released", "has not been released",
        "unconfirmed", "尚未发布", "未经证实",
    )

    def __init__(self, repository, source, candidate_agent, audit_directory,
                 *, clock=None, fault_injector=None):
        self.repository = repository
        self.source = source
        self.candidate_agent = candidate_agent
        self.audit_directory = os.path.realpath(audit_directory)
        self.clock = clock or utc_now
        self.fault_injector = fault_injector
        os.makedirs(self.audit_directory, exist_ok=True)
        self.evidence_store = EvidenceStore(os.path.join(
            self.audit_directory, "evidence",
        ))
        self.result_store = ResultStore(os.path.join(
            self.audit_directory, "results",
        ))

    def _fault(self, stage, value):
        if self.fault_injector is not None:
            self.fault_injector(stage, value)

    @staticmethod
    def _observation_evidence(observation, run_id):
        evidence_id = hashlib.sha256(
            f"event-observation-evidence\n{observation.observation_id}".encode()
        ).hexdigest()[:32]
        return create_evidence(
            run_id, "tool_observation",
            {"kind": "external_observation", "target": "openai_release_sources",
             "claim": "typed_event_sources_observed"},
            source={
                "action_id": observation.observation_id,
                "logical_action_id": observation.observation_id,
                "attempt": 1,
                "observation_event_id": observation.observation_id,
                "tool": "fake_event_search", "run_id": run_id,
            },
            verification={"accepted": True, "read_only": True},
            freshness={"scope": "run", "observed_at": observation.retrieved_at,
                       "run_id": run_id},
            content_identity={
                "observation_id": observation.observation_id,
                "coverage_complete": observation.coverage_complete,
                "truncated": observation.truncated,
                "source_fingerprints": [
                    item.content_fingerprint for item in observation.results
                ],
            },
            references={"entity_key": observation.entity_key},
            evidence_id=evidence_id, created_at=observation.retrieved_at,
        )

    @staticmethod
    def _verification_evidence(verification, run_id):
        evidence_id = verification.verification_evidence_id
        accepted = verification.outcome == "VERIFIED"
        reason = None if accepted else verification.reason_code
        return create_evidence(
            run_id, "verification",
            {"kind": "event_verification", "target": "openai_model_release",
             "claim": "deterministic_event_gate_completed"},
            source={
                "verification_action_id": verification.verification_id,
                "observation_event_id": verification.observation_id,
                "run_id": run_id,
            },
            verification={
                "accepted": accepted,
                "reason": reason,
                "verification_target": "OpenAI/MODEL_RELEASED/MODEL",
                "deterministic": True,
            },
            freshness={"scope": "run", "observed_at": verification.verified_at,
                       "run_id": run_id},
            content_identity={
                "outcome": verification.outcome,
                "reason_code": verification.reason_code,
                "logical_event_identity": verification.logical_event_identity,
            },
            references={
                "observation_evidence_id": verification.observation_evidence_id,
                "candidate_id": verification.candidate_id,
            }, evidence_id=evidence_id, created_at=verification.verified_at,
        )

    def _run_candidate_agent(self, cycle, observation):
        try:
            result = self.result_store.load(cycle.harness_run_id)
        except ResultError:
            envelope = self.candidate_agent.propose(observation)
            provider = FinalCandidateProvider(json.dumps(
                envelope, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ))
            result = run_agent(
                "identify bounded OpenAI model release candidate",
                provider, max_steps=1,
                audit_writer=AuditWriter(
                    cycle.subscription_id, cycle.harness_run_id,
                    self.audit_directory,
                ),
                result_store=self.result_store, return_result=True,
            )
        if result["status"] != "completed":
            raise EventProcessingError("HARNESS_INCOMPLETE")
        try:
            envelope = json.loads(result["answer"])
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise EventProcessingError("AGENT_CONTRACT_FAILED") from error
        if (not isinstance(envelope, dict)
                or set(envelope) != {"schema_version", "candidates"}
                or envelope["schema_version"] != 1
                or not isinstance(envelope["candidates"], list)
                or len(envelope["candidates"]) > 1):
            raise EventProcessingError("AGENT_CONTRACT_FAILED")
        if not envelope["candidates"]:
            return None
        raw = envelope["candidates"][0]
        if not isinstance(raw, dict) or set(raw) != {
            "entity_key", "event_type", "object_type", "display_name",
            "canonical_name_candidate", "occurred_at_candidate", "support",
        } or not isinstance(raw["support"], list):
            raise EventProcessingError("AGENT_CONTRACT_FAILED")
        by_ref = {item.source_ref: item for item in observation.results}
        supports = []
        for item in raw["support"]:
            if (not isinstance(item, dict)
                    or set(item) != {"source_ref", "exact_span"}
                    or item["source_ref"] not in by_ref):
                raise EventProcessingError("AGENT_CONTRACT_FAILED")
            source_text = (
                by_ref[item["source_ref"]].title + " "
                + by_ref[item["source_ref"]].snippet
            )
            if item["exact_span"] not in source_text:
                raise EventProcessingError("AGENT_CONTRACT_FAILED")
            supports.append(EventCandidateSupport(
                item["source_ref"], item["exact_span"],
            ))
        supports = tuple(supports)
        candidate_id = event_candidate_identity(
            observation.observation_id, cycle.harness_run_id,
            raw["entity_key"], raw["event_type"], raw["object_type"],
            raw["display_name"], raw["canonical_name_candidate"],
            raw["occurred_at_candidate"], supports,
        )
        return EventCandidate(
            candidate_id, observation.observation_id, cycle.harness_run_id,
            raw["entity_key"], raw["event_type"], raw["object_type"],
            raw["display_name"], raw["canonical_name_candidate"],
            raw["occurred_at_candidate"], supports,
        )

    def _verification(self, cycle, state, observation, evidence_id, candidate,
                      timestamp):
        outcome, reason = "NO_UPDATE", "NO_EVENT_FOUND"
        logical = model_key = None
        if not observation.coverage_complete or observation.truncated:
            outcome, reason = "VERIFICATION_INCOMPLETE", "COVERAGE_INCOMPLETE"
        elif candidate is not None:
            if (candidate.entity_key != "openai"
                    or candidate.event_type != "MODEL_RELEASED"
                    or candidate.object_type != "MODEL"):
                outcome, reason = "NO_UPDATE", "OUTSIDE_SCOPE"
            else:
                try:
                    model_key = normalize_event_model_name(
                        candidate.canonical_name_candidate,
                    )
                    display_key = normalize_event_model_name(
                        candidate.display_name,
                    )
                except DomainError:
                    outcome, reason = (
                        "VERIFICATION_INCOMPLETE", "MODEL_NAME_UNCONFIRMED"
                    )
                    model_key = None
                if model_key is not None and display_key != model_key:
                    outcome, reason = (
                        "VERIFICATION_INCOMPLETE", "MODEL_NAME_UNCONFIRMED"
                    )
                if model_key is not None and display_key == model_key:
                    by_ref = {item.source_ref: item for item in observation.results}
                    supported = [
                        by_ref[item.source_ref] for item in candidate.support
                        if item.source_ref in by_ref
                    ]
                    official = [item for item in supported if
                                item.source_kind == "official_primary"
                                and item.publisher == "OpenAI"
                                and item.canonical_url.split("/", 3)[2]
                                in {"openai.com", "www.openai.com"}]
                    normalized_text = " ".join(
                        f"{item.title} {item.snippet}".casefold()
                        for item in official
                    )
                    all_text = " ".join(
                        f"{item.title} {item.snippet}".casefold()
                        for item in observation.results
                    )
                    model_variants = {
                        model_key, model_key.replace(" ", "-"),
                        model_key.replace(" ", "_"),
                    }
                    if not official:
                        outcome, reason = (
                            "VERIFICATION_INCOMPLETE",
                            "INSUFFICIENT_OFFICIAL_SUPPORT",
                        )
                    elif (not any(value in normalized_text
                                  for value in model_variants)
                          or not any(marker in normalized_text
                                     for marker in self.RELEASE_MARKERS)):
                        outcome, reason = (
                            "VERIFICATION_INCOMPLETE",
                            "RELEASE_SEMANTICS_UNCONFIRMED",
                        )
                    elif (model_key in all_text
                          and any(marker in all_text
                                  for marker in self.CONFLICT_MARKERS)):
                        outcome, reason = (
                            "VERIFICATION_INCOMPLETE", "CONFLICTING_EVIDENCE",
                        )
                    elif (candidate.occurred_at_candidate is None
                          or not any(item.published_at
                                     == candidate.occurred_at_candidate
                                     for item in official)):
                        outcome, reason = (
                            "VERIFICATION_INCOMPLETE", "SOURCE_TIME_UNCONFIRMED",
                        )
                    else:
                        occurred = _utc(candidate.occurred_at_candidate)
                        eligible_start = max(
                            _utc(state.activation_at),
                            _utc(observation.window_start_at),
                        )
                        eligible_end = min(
                            _utc(observation.window_end_at),
                            _utc(observation.retrieved_at)
                            + timedelta(minutes=5),
                        )
                        if occurred < eligible_start or occurred > eligible_end:
                            outcome, reason = "NO_UPDATE", "OUTSIDE_SCOPE"
                        else:
                            logical = verified_event_identity(
                                "openai", "MODEL_RELEASED", "MODEL", model_key,
                            )
                            if self.repository.get_verified_event_by_identity(logical):
                                outcome, reason = (
                                    "NO_UPDATE", "DUPLICATE_VERIFIED_EVENT",
                                )
                                logical = model_key = None
                            else:
                                outcome, reason = "VERIFIED", "VERIFIED_NEW_EVENT"
        verification_id = event_verification_identity(
            cycle.subscription_id, cycle.definition_id,
            cycle.definition_version, observation.observation_id,
            candidate.candidate_id if candidate else None,
            "openai_model_release_v1",
        )
        verification_evidence_id = hashlib.sha256(
            f"event-verification-evidence\n{verification_id}".encode()
        ).hexdigest()[:32]
        return EventVerification(
            verification_id, cycle.subscription_id, cycle.definition_id,
            cycle.definition_version, observation.observation_id, evidence_id,
            candidate.candidate_id if candidate else None, outcome, reason,
            "openai_model_release_v1", logical, model_key,
            verification_evidence_id, timestamp,
        )

    def plan_due_cycles(self, maximum=100):
        timestamp = self.clock()
        planned = []
        for state in self.repository.list_due_event_temporal_states(
                timestamp, maximum):
            now = _utc(timestamp)
            first_due = _utc(state.next_due_at)
            skipped = max(0, int(
                (now - first_due).total_seconds() // state.cadence_seconds
            ))
            latest_due = first_due + timedelta(
                seconds=skipped * state.cadence_seconds,
            )
            kind = "SCHEDULED" if skipped == 0 else "CATCH_UP"
            scheduled = utc_timestamp(latest_due)
            window_start = max(
                _utc(state.activation_at),
                _utc(state.verified_through or state.activation_at)
                - timedelta(days=1),
            )
            cycle_id = event_cycle_identity(
                state.subscription_id, state.execution_policy_version,
                scheduled, kind,
            )
            cycle = EventObservationCycle(
                cycle_id, state.subscription_id, state.definition_id,
                state.definition_version, state.execution_policy_version,
                kind, scheduled, state.next_due_at, scheduled, skipped + 1,
                utc_timestamp(window_start), timestamp, "PENDING",
                event_harness_run_identity(cycle_id), None, None, None, None,
                None, None, None, None, None, None, None, timestamp, timestamp,
            )
            stored, created = self.repository.reserve_event_cycle(
                state.version, cycle,
                utc_timestamp(latest_due + timedelta(
                    seconds=state.cadence_seconds,
                )),
            )
            if created:
                planned.append(stored)
        return tuple(planned)

    def _fail(self, cycle, claim_token, code, timestamp):
        failed = self.repository.fail_event_cycle(
            cycle.cycle_id, claim_token, code, timestamp,
        )
        return EventWorkResult(
            "FAILED", cycle.subscription_id, None, None, None, None, None,
            failed.cycle_id, False, code,
        )

    def run_once(self):
        timestamp = self.clock()
        claim_token = uuid.uuid4().hex
        claimed = self.repository.claim_event_cycle(
            claim_token, timestamp,
            utc_timestamp(_utc(timestamp) - timedelta(minutes=5)),
        )
        if claimed is None:
            return EventWorkResult(
                "NO_WORK", None, None, None, None, None, None, None,
            )
        cycle, state = claimed
        try:
            tracking = self.repository.get_tracking_definition(
                cycle.definition_id, cycle.definition_version,
            )
            policy = self.repository.get_tracking_policy(
                cycle.subscription_id, cycle.definition_id,
                cycle.definition_version,
            )
            relation = self.repository.get_user_subscription_for_subscription(
                cycle.subscription_id,
            )
            if (tracking is None or policy is None or relation is None
                    or tracking.workflow_kind != "EVENT"
                    or state.lifecycle_status != "ACTIVE"):
                raise EventProcessingError("PROVIDER_ERROR")
            try:
                observation = self.source.observe(EventObservationQuery(
                    "openai", cycle.window_start_at, cycle.window_end_at,
                ))
            except TimeoutError:
                return self._fail(cycle, claim_token, "PROVIDER_TIMEOUT", timestamp)
            except DomainError:
                return self._fail(cycle, claim_token, "INVALID_OBSERVATION", timestamp)
            except Exception:
                return self._fail(cycle, claim_token, "PROVIDER_ERROR", timestamp)
            observation_evidence = self._observation_evidence(
                observation, cycle.harness_run_id,
            )
            try:
                self.evidence_store.save(observation_evidence)
            except (EvidenceError, OSError):
                return self._fail(
                    cycle, claim_token, "EVIDENCE_PERSIST_FAILED", timestamp,
                )
            self._fault("after_observation_evidence", cycle)
            try:
                candidate = self._run_candidate_agent(cycle, observation)
            except EventProcessingError as error:
                return self._fail(cycle, claim_token, error.code, timestamp)
            verification = self._verification(
                cycle, state, observation,
                observation_evidence["evidence_id"], candidate, timestamp,
            )
            verification_evidence = self._verification_evidence(
                verification, cycle.harness_run_id,
            )
            try:
                self.evidence_store.save(verification_evidence)
            except (EvidenceError, OSError):
                return self._fail(
                    cycle, claim_token,
                    "VERIFICATION_EVIDENCE_PERSIST_FAILED", timestamp,
                )
            self._fault("after_verification_evidence", cycle)
            event = update = distribution = None
            if verification.outcome == "VERIFIED":
                official = next(
                    item for item in observation.results
                    if item.source_kind == "official_primary"
                    and item.publisher == "OpenAI"
                    and item.published_at == candidate.occurred_at_candidate
                )
                event = VerifiedEvent(
                    verification.logical_event_identity[:32],
                    verification.logical_event_identity, "openai",
                    "MODEL_RELEASED", "MODEL",
                    verification.canonical_model_key, candidate.display_name,
                    candidate.occurred_at_candidate,
                    verification.verification_id,
                    verification.verification_evidence_id, timestamp,
                )
                update_id = event_update_identity(
                    cycle.subscription_id, event.event_id,
                )
                update = TrackingUpdate(
                    update_id, cycle.subscription_id, cycle.definition_id,
                    cycle.definition_version, verification.verification_id,
                    verification.verification_evidence_id, "EVENT", {
                        "title": "发现并验证了 OpenAI 新模型发布",
                        "summary": f"OpenAI 已发布 {candidate.display_name}。",
                        "entity": "OpenAI",
                        "model_name": candidate.display_name,
                        "event_type": "MODEL_RELEASED",
                        "occurred_at": candidate.occurred_at_candidate,
                        "source_title": official.title,
                        "source_url": official.canonical_url,
                    }, candidate.occurred_at_candidate, timestamp,
                    event.event_id,
                )
                distribution = UpdateDistribution(
                    update_distribution_identity(
                        update_id, relation.user_subscription_id,
                    ), update_id, relation.user_subscription_id,
                    "AVAILABLE", timestamp,
                )
            completed, reused = self.repository.complete_event_cycle(
                cycle.cycle_id, claim_token, observation, candidate,
                verification, event, update, distribution, timestamp,
            )
            worker = (
                "UPDATE_CREATED" if update is not None else
                "VERIFICATION_INCOMPLETE"
                if verification.outcome == "VERIFICATION_INCOMPLETE" else
                "NO_UPDATE"
            )
            return EventWorkResult(
                worker, cycle.subscription_id, verification.outcome,
                verification.reason_code,
                completed.event_id, completed.update_id,
                completed.distribution_id, completed.cycle_id, reused,
            )
        except Exception:
            self.repository.release_event_cycle_claim(
                cycle.cycle_id, claim_token, timestamp,
            )
            raise

    def tick(self, maximum=100):
        self.plan_due_cycles(maximum)
        results = []
        for _ in range(maximum):
            result = self.run_once()
            if result.worker_status == "NO_WORK":
                break
            results.append(result)
        return tuple(results)


def build_verified_event_service(repository, source, candidate_agent,
                                 audit_directory, *, clock=None,
                                 fault_injector=None):
    return VerifiedEventService(
        repository, source, candidate_agent, audit_directory, clock=clock,
        fault_injector=fault_injector,
    )
