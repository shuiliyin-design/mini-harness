"""Stable application façade and public DTOs for the Digest product."""

from dataclasses import dataclass
import copy
import hashlib
import re

from .domain import DomainError, InterestProfile
from .contracts import CONTRACT_FAILURE_SUBTYPES
from .adapters.provider import (
    CANDIDATE_SCHEMA_FIELDS, ENVELOPE_EXTRACTION_ERRORS,
    GENERATION_DIAGNOSTIC_FIELDS, GENERATION_FAILURE_SUBTYPES,
    JSON_LEXICAL_SUBTYPES, SAFE_JSON_TYPES,
)
from .services import DeliveryPersistenceError
from .repositories import RecoveryOperationRecord
from .conversation import ConversationError, SAFE_CONVERSATION_FAILURES
from .activation import ActivationError
from .outbox import DurableOutboxWorker, OutboxWorkerError


SAFE_RUN_REASONS = frozenset({
    "configuration_error", "search_unavailable", "generation_incomplete",
    "subscription_disabled", "recovery_required", "run_already_active",
})


class ApplicationError(ValueError):
    """Stable public failure; internal exceptions are deliberately excluded."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SubscriptionView:
    subscription_id: str
    topic: str
    natural_language_request: str
    cadence: str
    language: str
    max_chars: int
    max_items: int
    focus_topics: tuple[str, ...]
    delivery_channel: str
    enabled: bool
    version: int
    created_at: str
    updated_at: str
    product_kind: str
    product_status: str | None
    definition_id: str | None
    definition_version: int | None
    user_subscription_id: str | None
    first_briefing_application_run_id: str | None
    first_briefing_status: str | None


@dataclass(frozen=True, slots=True)
class RunView:
    application_run_id: str
    subscription_id: str
    idempotency_key: str
    status: str
    failure_reason: str | None
    digest_id: str | None
    subscription_version: int
    reused: bool
    failure_stage: str | None = None
    failure_code: str | None = None
    failure_subtype: str | None = None
    failure_diagnostics: dict | None = None
    definition_id: str | None = None
    definition_version: int | None = None


@dataclass(frozen=True, slots=True)
class DigestView:
    digest_id: str
    application_run_id: str
    subscription_id: str
    subscription_version: int
    content: dict
    created_at: str
    definition_id: str | None = None
    definition_version: int | None = None


@dataclass(frozen=True, slots=True)
class DeliveryView:
    delivery_id: str
    digest_id: str
    channel: str
    status: str
    effect_certainty: str
    attempt_number: int
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class ProfileView:
    version: int
    rule_version: int
    topic_weights: tuple[tuple[str, int], ...]
    updated_at: str


@dataclass(frozen=True, slots=True)
class FeedbackView:
    feedback_id: str
    applied: bool
    profile: ProfileView


@dataclass(frozen=True, slots=True)
class ConversationView:
    conversation_id: str
    status: str
    turn_count: int
    version: int
    latest_outcome: str | None
    question: str | None
    rejection_reason: str | None
    definition: dict | None
    processing: bool
    failure_reason: str | None
    reused: bool
    updated_at: str


@dataclass(frozen=True, slots=True)
class SubscriptionCommitView:
    conversation_id: str
    definition_outcome_id: str
    definition_id: str
    definition_version: int
    subscription_id: str
    status: str
    user_subscription_id: str
    relation_status: str
    first_briefing_application_run_id: str
    first_briefing_status: str
    message: str
    reused: bool
    committed_at: str


@dataclass(frozen=True, slots=True)
class FirstBriefingView:
    subscription_id: str
    subscription_status: str
    relation_status: str
    application_run_id: str
    status: str
    digest_id: str | None
    failure_reason: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class OutboxWorkView:
    worker_status: str
    outbox_id: str | None
    outbox_status: str | None
    subscription_id: str | None
    application_run_id: str | None
    first_briefing_status: str | None
    digest_id: str | None
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class OutboxInspectionView:
    outbox_id: str
    event_type: str
    outbox_status: str
    attempt_number: int
    subscription_id: str
    application_run_id: str
    first_briefing_status: str
    application_run_status: str
    binding_status: str
    terminal_result_available: bool
    safe_recovery_actions: tuple[str, ...]
    blocking_reason: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class RecoveryInspection:
    application_run_id: str
    application_run_status: str
    recovery_reason: str
    binding_status: str
    harness_run_status: str
    terminal_result_available: bool
    safe_recovery_actions: tuple[str, ...]
    blocking_reason: str | None


@dataclass(frozen=True, slots=True)
class RecoveryOperationView:
    recovery_operation_id: str
    application_run_id: str
    selected_action: str
    status: str
    before_state: str
    after_state: str | None
    failure_reason: str | None


def _subscription_view(value, product=None, relation=None, briefing=None,
                       briefing_status=None):
    original_fields = (
        "subscription_id", "topic", "natural_language_request", "cadence",
        "language", "max_chars", "max_items", "focus_topics",
        "delivery_channel", "enabled", "version", "created_at", "updated_at",
    )
    return SubscriptionView(
        **{name: getattr(value, name) for name in original_fields},
        product_kind="product" if product is not None else "legacy",
        product_status=product.status if product is not None else None,
        definition_id=product.definition_id if product is not None else None,
        definition_version=(
            product.definition_version if product is not None else None
        ),
        user_subscription_id=(
            relation.user_subscription_id if relation is not None else None
        ),
        first_briefing_application_run_id=(
            briefing.application_run_id if briefing is not None else None
        ),
        first_briefing_status=briefing_status,
    )


def _failure_projection(record):
    if record.status == "completed":
        return None, None
    if record.failure_stage is not None and record.failure_code is not None:
        return record.failure_stage, record.failure_code
    if record.status in {"reserved", "running", "running_recovery"}:
        return "recovery", "run_already_active"
    if record.reason == "recovery_required" or record.status == "recovery_required":
        return "recovery", "recovery_required"
    if record.reason is not None:
        return "unknown_stage", "legacy_failure"
    return None, None


CONTRACT_DIAGNOSTIC_FIELDS = frozenset({
    "safe_rule_identity", "expected_max_chars", "actual_char_count",
    "expected_max_items", "actual_item_count",
    "invalid_content_ref_count", "invalid_source_ref_count",
    "duplicate_item_count", "topic_focus_mismatch_count",
    "missing_required_field_count", "invalid_marker_count",
    "violation_count",
})


def _safe_failure_diagnostics(record):
    value = record.failure_diagnostics
    if not isinstance(value, dict):
        return None
    if record.failure_stage == "contract":
        if (set(value) != CONTRACT_DIAGNOSTIC_FIELDS
                or not re.fullmatch(
                    r"[0-9a-f]{64}", str(value.get("safe_rule_identity", "")),
                )):
            return None
        for key in CONTRACT_DIAGNOSTIC_FIELDS - {"safe_rule_identity"}:
            item = value.get(key)
            if type(item) is not int or not 0 <= item <= 10_000_000:
                return None
        return copy.deepcopy(value)
    if (record.failure_stage != "generation" or not value
            or not set(value).issubset(GENERATION_DIAGNOSTIC_FIELDS)):
        return None
    if ("schema_mismatch_field" in value
            and value["schema_mismatch_field"] not in CANDIDATE_SCHEMA_FIELDS):
        return None
    if ("payload_source" in value
            and value["payload_source"] != "tool_arguments"):
        return None
    for key in {
        "payload_top_type", "payload_items_type", "payload_items_nested_type",
    } & set(value):
        if value[key] not in SAFE_JSON_TYPES:
            return None
    if ("payload_items_string_chars" in value
            and (type(value["payload_items_string_chars"]) is not int
                 or not 0 <= value["payload_items_string_chars"] <= 1_000_000)):
        return None
    for key in {
        "payload_items_string_starts_array",
        "payload_items_string_ends_array",
        "payload_items_nested_json_parse",
    } & set(value):
        if type(value[key]) is not bool:
            return None
    if ("envelope_error" in value
            and value["envelope_error"] not in ENVELOPE_EXTRACTION_ERRORS):
        return None
    if ("json_lexical_subtype" in value
            and value["json_lexical_subtype"] not in JSON_LEXICAL_SUBTYPES):
        return None
    return copy.deepcopy(value)


class DigestApplication:
    """The public business boundary consumed by future transports and tests."""

    def __init__(self, repository, subscription_service, generation_workflow,
                 delivery_service, feedback_service,
                 conversation_workflow=None, activation_service=None,
                 outbox_worker=None):
        self.repository = repository
        self.subscriptions = subscription_service
        self.generation = generation_workflow
        self.deliveries = delivery_service
        self.feedback = feedback_service
        self.conversations = conversation_workflow
        self.activations = activation_service
        self.outbox = outbox_worker

    @staticmethod
    def _idempotency_key(value):
        if (not isinstance(value, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}", value)):
            raise ApplicationError("invalid_request")
        return value

    def _owned_subscription(self, user_id, subscription_id):
        value = self.repository.get_subscription(subscription_id)
        owns = getattr(
            self.repository, "subscription_belongs_to_user",
            lambda _subscription_id, expected_user: (
                value is not None and value.user_id == expected_user
            ),
        )
        if value is None or not owns(subscription_id, user_id):
            raise ApplicationError("not_found")
        return value

    @staticmethod
    def _conversation_view(execution):
        conversation = execution.conversation
        turn = execution.turn
        outcome = execution.outcome
        payload = outcome.payload if outcome is not None else {}
        waiting = conversation.status == "WAITING_FOR_ANSWER"
        failure_reason = turn.error_code or conversation.terminal_reason
        if failure_reason not in SAFE_CONVERSATION_FAILURES:
            failure_reason = None
        return ConversationView(
            conversation.conversation_id, conversation.status,
            conversation.turn_count, conversation.version,
            outcome.outcome_type if outcome is not None else None,
            payload.get("question") if waiting else None,
            payload.get("reason") if conversation.status == "REJECTED" else None,
            (copy.deepcopy(payload.get("definition"))
             if conversation.status == "DEFINITION_ACCEPTED" else None),
            turn.status in {"reserved", "running"},
            failure_reason,
            execution.reused, conversation.updated_at,
        )

    def _conversation_operation(self, method, *arguments):
        if self.conversations is None:
            raise ApplicationError("configuration_error")
        try:
            return self._conversation_view(method(*arguments))
        except ConversationError as error:
            raise ApplicationError(error.code) from error
        except (DomainError, ValueError) as error:
            raise ApplicationError("invalid_conversation_message") from error

    def start_subscription_conversation(self, user_id, message,
                                        idempotency_key):
        key = self._idempotency_key(idempotency_key)
        return self._conversation_operation(
            self.conversations.start, user_id, message, key,
        )

    def continue_subscription_conversation(self, user_id, conversation_id,
                                           message, idempotency_key):
        key = self._idempotency_key(idempotency_key)
        return self._conversation_operation(
            self.conversations.continue_conversation,
            user_id, conversation_id, message, key,
        )

    def get_subscription_conversation(self, user_id, conversation_id):
        return self._conversation_operation(
            self.conversations.get, user_id, conversation_id,
        )

    def _subscription_commit_view(self, commit):
        _reservation, status, _run, _outbox = self._briefing_resources(
            commit.subscription.subscription_id,
        )
        return SubscriptionCommitView(
            commit.activation.conversation_id,
            commit.activation.definition_outcome_id,
            commit.definition.definition_id,
            commit.definition.definition_version,
            commit.subscription.subscription_id,
            commit.subscription.status,
            commit.relation.user_subscription_id,
            commit.relation.status,
            commit.briefing.application_run_id,
            status,
            "订阅成功，正在准备首篇资讯。",
            commit.reused,
            commit.activation.created_at,
        )

    def commit_subscription_from_definition(self, user_id, conversation_id):
        if self.activations is None:
            raise ApplicationError("configuration_error")
        try:
            return self._subscription_commit_view(
                self.activations.commit(user_id, conversation_id),
            )
        except ActivationError as error:
            raise ApplicationError(error.code) from error
        except (DomainError, ValueError) as error:
            raise ApplicationError("subscription_commit_failed") from error

    def create_subscription(self, user_id, natural_language_request):
        try:
            return _subscription_view(
                self.subscriptions.create_from_natural_language(
                    user_id, natural_language_request,
                )
            )
        except (DomainError, ValueError) as error:
            raise ApplicationError("invalid_subscription") from error

    def update_subscription(self, user_id, subscription_id, expected_version,
                            **changes):
        if "delivery_preference" in changes:
            if "delivery_channel" in changes:
                raise ApplicationError("invalid_subscription")
            changes["delivery_channel"] = changes.pop("delivery_preference")
        try:
            value = self.subscriptions.update(
                user_id, subscription_id, expected_version, **changes,
            )
            return self._project_subscription(value)
        except DomainError as error:
            code = ("version_conflict" if "version conflict" in str(error)
                    else "invalid_subscription")
            raise ApplicationError(code) from error

    def enable_subscription(self, user_id, subscription_id, expected_version):
        return self._set_enabled(
            user_id, subscription_id, True, expected_version,
        )

    def disable_subscription(self, user_id, subscription_id, expected_version):
        return self._set_enabled(
            user_id, subscription_id, False, expected_version,
        )

    def _set_enabled(self, user_id, subscription_id, enabled, expected_version):
        try:
            value = self.subscriptions.set_enabled(
                user_id, subscription_id, enabled, expected_version,
            )
            return self._project_subscription(value)
        except DomainError as error:
            code = ("version_conflict" if "version conflict" in str(error)
                    else "not_found")
            raise ApplicationError(code) from error

    def list_subscriptions(self, user_id):
        values = self.repository.list_subscriptions_for_user(user_id)
        return tuple(self._project_subscription(value) for value in values)

    def get_subscription(self, user_id, subscription_id):
        return self._project_subscription(
            self._owned_subscription(user_id, subscription_id),
        )

    def _project_subscription(self, value):
        product_lookup = getattr(
            self.repository, "get_product_subscription", lambda _value: None,
        )
        product = product_lookup(value.subscription_id)
        relation = (
            getattr(
                self.repository, "get_user_subscription_for_subscription",
                lambda _value: None,
            )(value.subscription_id) if product is not None else None
        )
        briefing, briefing_status, _run, _outbox = (
            self._briefing_resources(value.subscription_id)
            if product is not None else (None, None, None, None)
        )
        return _subscription_view(
            value, product, relation, briefing, briefing_status,
        )

    def _briefing_resources(self, subscription_id):
        getter = getattr(
            self.repository, "get_briefing_reservation_for_subscription",
            lambda _value: None,
        )
        briefing = getter(subscription_id)
        if briefing is None:
            return None, None, None, None
        run = self.repository.get_digest_run(briefing.application_run_id)
        outbox = getattr(
            self.repository, "get_application_outbox_for_run",
            lambda _value: None,
        )(briefing.application_run_id)
        return (
            briefing, DurableOutboxWorker.briefing_status(run, outbox),
            run, outbox,
        )

    def get_first_briefing(self, user_id, subscription_id):
        self._owned_subscription(user_id, subscription_id)
        product = self.repository.get_product_subscription(subscription_id)
        relation = self.repository.get_user_subscription_for_subscription(
            subscription_id,
        )
        briefing, status, run, outbox = self._briefing_resources(
            subscription_id,
        )
        if any(value is None for value in (product, relation, briefing, outbox)):
            raise ApplicationError("not_found")
        failure = None
        if run is not None:
            _stage, failure = _failure_projection(run)
        if failure is None and outbox.status in {"failed", "blocked"}:
            failure = (outbox.last_error_code
                       if outbox.last_error_code in {
                           "recovery_required", "subscription_inactive",
                       } else "recovery_required")
        return FirstBriefingView(
            subscription_id, product.status, relation.status,
            briefing.application_run_id, status,
            run.digest_id if run else None, failure,
            (run.updated_at if run and run.updated_at else outbox.updated_at),
        )

    @staticmethod
    def _outbox_work_view(value):
        return OutboxWorkView(
            value.worker_status, value.outbox_id, value.outbox_status,
            value.subscription_id, value.application_run_id,
            value.briefing_status, value.digest_id, value.failure_reason,
        )

    @staticmethod
    def _outbox_inspection_view(value):
        return OutboxInspectionView(
            value.outbox_id, value.event_type, value.outbox_status,
            value.attempt_number, value.subscription_id,
            value.application_run_id, value.briefing_status,
            value.application_run_status, value.binding_status,
            value.terminal_result_available,
            value.safe_recovery_actions, value.blocking_reason,
            value.updated_at,
        )

    def run_outbox_once(self):
        if self.outbox is None:
            raise ApplicationError("configuration_error")
        try:
            return self._outbox_work_view(self.outbox.run_once())
        except OutboxWorkerError as error:
            raise ApplicationError(error.code) from error
        except (DomainError, ValueError) as error:
            raise ApplicationError("outbox_processing_failed") from error

    def drain_outbox(self, maximum):
        if self.outbox is None:
            raise ApplicationError("configuration_error")
        try:
            return tuple(self._outbox_work_view(value)
                         for value in self.outbox.drain(maximum))
        except OutboxWorkerError as error:
            raise ApplicationError(error.code) from error
        except (DomainError, ValueError) as error:
            raise ApplicationError("outbox_processing_failed") from error

    def inspect_outbox(self, outbox_id=None):
        if self.outbox is None:
            raise ApplicationError("configuration_error")
        try:
            values = (self.outbox.inspect_all() if outbox_id is None else
                      (self.outbox.inspect(outbox_id),))
            return tuple(self._outbox_inspection_view(value)
                         for value in values)
        except OutboxWorkerError as error:
            raise ApplicationError(error.code) from error
        except (DomainError, ValueError) as error:
            raise ApplicationError("outbox_processing_failed") from error

    def recover_outbox(self, outbox_id, action):
        if self.outbox is None:
            raise ApplicationError("configuration_error")
        try:
            return self._outbox_work_view(
                self.outbox.recover(outbox_id, action),
            )
        except OutboxWorkerError as error:
            raise ApplicationError(error.code) from error
        except (DomainError, ValueError) as error:
            raise ApplicationError("outbox_processing_failed") from error

    def _run_view(self, record, reused=False):
        failure_stage, failure_code = _failure_projection(record)
        subtype = (
            record.failure_subtype
            if (
                (failure_stage == "contract"
                 and failure_code == "output_contract_failed"
                 and record.failure_subtype in CONTRACT_FAILURE_SUBTYPES)
                or (failure_stage == "generation"
                    and record.failure_subtype in GENERATION_FAILURE_SUBTYPES)
            )
            else None
        )
        return RunView(
            record.digest_run_id, record.subscription_id,
            record.idempotency_key or record.period_key, record.status,
            failure_code, record.digest_id,
            record.subscription_version, reused, failure_stage, failure_code,
            subtype, _safe_failure_diagnostics(record),
            record.definition_id, record.definition_version,
        )

    def run_subscription(self, user_id, subscription_id, idempotency_key,
                         period_key=None):
        subscription = self._owned_subscription(user_id, subscription_id)
        key = self._idempotency_key(idempotency_key)
        if not subscription.enabled:
            return RunView(
                "", subscription_id, key, "rejected",
                "subscription_disabled", None, subscription.version, False,
                "configuration", "subscription_disabled",
            )
        try:
            outcome = self.generation.run(
                subscription_id, period_key or key, idempotency_key=key,
            )
        except DomainError as error:
            raise ApplicationError("invalid_request") from error
        record = self.repository.get_digest_run(outcome.digest_run_id)
        return self._run_view(record, outcome.reused)

    def recover_run(self, user_id, application_run_id):
        record = self.repository.get_digest_run(application_run_id)
        if record is None:
            raise ApplicationError("not_found")
        self._owned_subscription(user_id, record.subscription_id)
        inspection = self.inspect_run_recovery(application_run_id)
        if len(inspection.safe_recovery_actions) != 1:
            if (inspection.blocking_reason == "NO_SAFE_AUTOMATIC_RECOVERY"
                    and record.status in {"reserved", "running", "running_recovery"}):
                record = self.repository.mark_digest_run_recovery_required(
                    application_run_id, "recovery_required",
                    self.generation.clock(),
                )
            return self._run_view(record, True)
        self.execute_run_recovery(
            application_run_id, inspection.safe_recovery_actions[0],
        )
        return self._run_view(
            self.repository.get_digest_run(application_run_id), True,
        )

    def get_run(self, user_id, application_run_id):
        record = self.repository.get_digest_run(application_run_id)
        if record is None:
            raise ApplicationError("not_found")
        self._owned_subscription(user_id, record.subscription_id)
        return self._run_view(record, True)

    def _recover(self, record):
        if record.status == "reserved" and record.harness_bound_at is None:
            try:
                outcome = self.generation.execute_reserved(record)
            except ValueError:
                current = self.repository.get_digest_run(record.digest_run_id)
                return self._run_view(current, True)
            return self._run_view(
                self.repository.get_digest_run(outcome.digest_run_id), True,
            )
        if record.status == "running" and record.harness_bound_at:
            try:
                outcome = self.generation.recover_application_run(record)
            except ValueError:
                current = self.repository.get_digest_run(record.digest_run_id)
                return self._run_view(current, True)
            return self._run_view(
                self.repository.get_digest_run(outcome.digest_run_id), True,
            )
        return self._run_view(record, True)

    @staticmethod
    def _recovery_operation_id(application_run_id, action):
        return hashlib.sha256(
            f"{application_run_id}\n{action}".encode("utf-8"),
        ).hexdigest()[:32]

    def inspect_run_recovery(self, application_run_id):
        record = self.repository.get_digest_run(application_run_id)
        if record is None:
            raise ApplicationError("not_found")
        facts = self.generation.inspect_recovery_facts(record)
        return RecoveryInspection(
            record.digest_run_id, record.status,
            record.reason or (
                "recovery_required" if not facts["safe_recovery_actions"]
                else "safe_recovery_available"
            ),
            facts["binding_status"], facts["harness_run_status"],
            facts["terminal_result_available"],
            tuple(facts["safe_recovery_actions"]),
            facts["blocking_reason"],
        )

    @staticmethod
    def _recovery_operation_view(record):
        public_status = (
            "already_recovering" if record.status == "started" else
            "recovered" if record.status == "completed" else "failed"
        )
        return RecoveryOperationView(
            record.operation_id, record.application_run_id, record.action,
            public_status, record.before_state, record.after_state,
            record.error_code,
        )

    def execute_run_recovery(self, application_run_id, recovery_action):
        if not isinstance(recovery_action, str):
            raise ApplicationError("unsafe_recovery_action")
        operation_id = self._recovery_operation_id(
            application_run_id, recovery_action,
        )
        existing = self.repository.get_recovery_operation(operation_id)
        if existing is not None:
            return self._recovery_operation_view(existing)
        inspection = self.inspect_run_recovery(application_run_id)
        if recovery_action not in inspection.safe_recovery_actions:
            raise ApplicationError("unsafe_recovery_action")
        timestamp = self.generation.clock()
        candidate = RecoveryOperationRecord(
            operation_id, application_run_id, recovery_action, "started",
            inspection.application_run_status, None, timestamp, None, None,
        )
        operation, created = self.repository.reserve_recovery_operation(candidate)
        if not created:
            return self._recovery_operation_view(operation)
        try:
            record = self.repository.get_digest_run(application_run_id)
            if recovery_action == "resume_original_run":
                self.generation.execute_reserved(record)
            elif recovery_action == "resume_bound_run":
                self.generation.resume_bound_run(record)
            elif recovery_action == "repair_projection":
                self.generation.recover_projection(record)
            else:
                raise ApplicationError("unsafe_recovery_action")
            after = self.repository.get_digest_run(application_run_id)
            finished = RecoveryOperationRecord(
                operation.operation_id, operation.application_run_id,
                operation.action, "completed", operation.before_state,
                after.status, operation.requested_at, self.generation.clock(),
                None,
            )
        except Exception:
            after = self.repository.mark_digest_run_recovery_required(
                application_run_id, "recovery_required",
                self.generation.clock(),
            )
            finished = RecoveryOperationRecord(
                operation.operation_id, operation.application_run_id,
                operation.action, "failed", operation.before_state,
                after.status if after else "recovery_required",
                operation.requested_at, self.generation.clock(),
                "recovery_operation_failed",
            )
        return self._recovery_operation_view(
            self.repository.finish_recovery_operation(finished),
        )

    def list_digests(self, user_id, subscription_id=None):
        if subscription_id is not None:
            self._owned_subscription(user_id, subscription_id)
        return tuple(self._digest_view(item) for item in
                     self.repository.list_digests(user_id, subscription_id))

    def get_digest(self, user_id, digest_id):
        digest = self.repository.get_digest(digest_id)
        if digest is None:
            raise ApplicationError("not_found")
        self._owned_subscription(user_id, digest.subscription_id)
        return self._digest_view(digest)

    def _digest_view(self, digest):
        run = self.repository.get_digest_run(digest.digest_run_id)
        content = copy.deepcopy(digest.payload)
        for source in content.get("source_refs", []):
            source.pop("evidence_id", None)
        profile = content.get("profile_snapshot")
        if isinstance(profile, dict):
            profile.pop("projection_id", None)
        return DigestView(
            digest.digest_id, digest.digest_run_id, digest.subscription_id,
            run.subscription_version, content,
            digest.created_at, run.definition_id, run.definition_version,
        )

    def deliver_digest(self, user_id, digest_id, channel):
        try:
            record = self.deliveries.deliver_digest(user_id, digest_id, channel)
        except DeliveryPersistenceError as error:
            raise ApplicationError("delivery_unknown") from error
        except DomainError as error:
            raise ApplicationError("delivery_rejected") from error
        return DeliveryView(
            record.delivery_id, record.digest_id, record.channel,
            record.status, record.effect_certainty, record.attempt_number,
            ("delivery_unknown" if record.status == "unknown" else
             "delivery_failed" if record.status == "failed" else None),
        )

    def record_feedback(self, user_id, digest_id, feedback_type, event_key,
                        item_id=None):
        try:
            result = self.feedback.record(
                user_id, digest_id, feedback_type, event_key, item_id,
            )
        except DomainError as error:
            raise ApplicationError("invalid_feedback") from error
        return FeedbackView(
            result.feedback_id, result.applied,
            self._profile_view(result.profile),
        )

    def get_profile(self, user_id):
        profile = self.repository.get_profile(user_id)
        if profile is None:
            profile = InterestProfile.empty(user_id, "1970-01-01T00:00:00Z")
        return self._profile_view(profile)

    @staticmethod
    def _profile_view(profile):
        return ProfileView(
            profile.version, profile.rule_version,
            tuple((item.topic_key, item.weight)
                  for item in profile.topic_weights),
            profile.updated_at,
        )
