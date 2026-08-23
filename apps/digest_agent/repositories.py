"""Application repository ports and durable run reservation types."""

from dataclasses import dataclass
from typing import Protocol

from .domain import (
    ContentCandidate, DeliveryRecord, Digest, Feedback, FeedbackResult,
    InterestProfile, Subscription,
)


@dataclass(frozen=True, slots=True)
class DigestRunRecord:
    digest_run_id: str
    subscription_id: str
    period_key: str
    harness_run_id: str
    status: str
    reason: str | None
    digest_id: str | None
    artifact_id: str | None
    harness_result: dict | None
    profile_version: int = 0
    profile_projection_id: str | None = None
    profile_projection: dict | None = None
    idempotency_key: str | None = None
    subscription_version: int = 1
    subscription_snapshot: dict | None = None
    harness_bound_at: str | None = None
    started_at: str | None = None
    updated_at: str | None = None
    failure_stage: str | None = None
    failure_code: str | None = None
    failure_subtype: str | None = None
    failure_diagnostics: dict | None = None


@dataclass(frozen=True, slots=True)
class RecoveryOperationRecord:
    operation_id: str
    application_run_id: str
    action: str
    status: str
    before_state: str
    after_state: str | None
    requested_at: str
    completed_at: str | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class GenerationAttemptRecord:
    attempt_id: str
    application_run_id: str
    attempt_number: int
    status: str
    request_metadata: dict
    response_metadata: dict | None
    failure_subtype: str | None
    started_at: str
    completed_at: str | None


class DigestRepository(Protocol):
    def save_subscription(self, subscription: Subscription) -> None: ...
    def get_subscription(self, subscription_id: str) -> Subscription | None: ...
    def list_subscriptions(self) -> tuple[Subscription, ...]: ...
    def list_subscriptions_for_user(self, user_id: str) -> tuple[Subscription, ...]: ...
    def update_subscription(self, subscription: Subscription,
                            expected_version: int) -> bool: ...
    def reserve_digest_run(self, record: DigestRunRecord) -> tuple[DigestRunRecord, bool]: ...
    def save_candidates(self, digest_run_id: str,
                        candidates: tuple[ContentCandidate, ...]) -> None: ...
    def finish_digest_run(self, record: DigestRunRecord,
                          digest: Digest | None = None) -> None: ...
    def get_digest_run(self, digest_run_id: str) -> DigestRunRecord | None: ...
    def get_digest(self, digest_id: str) -> Digest | None: ...
    def list_digests(self, user_id: str,
                     subscription_id: str | None = None) -> tuple[Digest, ...]: ...
    def bind_digest_run(self, digest_run_id: str, harness_run_id: str,
                        timestamp: str) -> DigestRunRecord: ...
    def claim_bound_digest_run_recovery(self, digest_run_id: str,
                                        timestamp: str) -> DigestRunRecord: ...
    def mark_digest_run_recovery_required(self, digest_run_id: str,
                                          reason: str,
                                          timestamp: str) -> DigestRunRecord: ...
    def reserve_recovery_operation(self, record: RecoveryOperationRecord
                                   ) -> tuple[RecoveryOperationRecord, bool]: ...
    def finish_recovery_operation(self, record: RecoveryOperationRecord
                                  ) -> RecoveryOperationRecord: ...
    def get_recovery_operation(self, operation_id: str
                               ) -> RecoveryOperationRecord | None: ...
    def reserve_generation_attempt(self, record: GenerationAttemptRecord
                                   ) -> GenerationAttemptRecord: ...
    def finish_generation_attempt(self, record: GenerationAttemptRecord
                                  ) -> GenerationAttemptRecord: ...
    def list_generation_attempts(self, application_run_id: str
                                 ) -> tuple[GenerationAttemptRecord, ...]: ...
    def get_profile(self, user_id: str) -> InterestProfile | None: ...
    def get_seen_content(self, user_id: str) -> frozenset[str]: ...
    def apply_feedback(self, feedback: Feedback, topic_keys: tuple[str, ...],
                       timestamp: str) -> FeedbackResult: ...
    def reserve_delivery(self, record: DeliveryRecord) -> tuple[DeliveryRecord, bool]: ...
    def reserve_delivery_retry(self, previous: DeliveryRecord,
                               record: DeliveryRecord) -> DeliveryRecord: ...
    def mark_delivery_dispatch_started(self, delivery_id: str,
                                       attempt_id: str) -> DeliveryRecord: ...
    def finish_delivery(self, record: DeliveryRecord) -> DeliveryRecord: ...
    def get_delivery(self, delivery_id: str) -> DeliveryRecord | None: ...
    def get_delivery_for_digest(self, digest_id: str,
                                channel: str) -> DeliveryRecord | None: ...
