"""Stable application façade and public DTOs for the Digest product."""

from dataclasses import dataclass
import copy
import hashlib
import re

from .domain import (
    ConditionObservationRequest, ConditionSubscriptionCommit, DomainError,
    InterestProfile,
)
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
from .relation_events import RelationEventPublisherError
from .conditions import ConditionProcessingError


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
    workflow_kind: str


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
    failure_stage: str | None = None
    failure_subtype: str | None = None


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
    first_briefing_application_run_id: str | None
    first_briefing_status: str | None
    message: str
    reused: bool
    committed_at: str
    workflow_kind: str = "BRIEFING"
    condition_request_id: str | None = None
    condition_status: str | None = None


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
class FeedDefinitionView:
    topic: str
    focus_topics: tuple[str, ...]
    language: str
    cadence: str
    max_items: int
    max_chars: int
    delivery_preference: str
    constraints: tuple[str, ...] = ()
    goal: str | None = None
    trigger: str | None = None
    time_window: str | None = None
    locations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FeedSourceView:
    title: str
    domain: str
    url: str
    published_at: str | None


@dataclass(frozen=True, slots=True)
class FeedItemView:
    item_id: str
    title: str
    summary: str
    sources: tuple[FeedSourceView, ...]
    why_recommended: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FeedBriefingView:
    update_id: str
    created_at: str
    period_label: str
    item_count: int
    items: tuple[FeedItemView, ...]
    definition: FeedDefinitionView
    update_kind: str = "BRIEFING"


@dataclass(frozen=True, slots=True)
class ConditionUpdateView:
    update_id: str
    created_at: str
    title: str
    summary: str
    origin: str
    destination: str
    travel_month: int
    observed_price: int
    threshold: int
    currency: str
    observed_at: str
    definition_id: str
    definition_version: int
    update_kind: str = "CONDITION"


@dataclass(frozen=True, slots=True)
class ConditionMonitoringView:
    status: str
    message: str
    origin: str
    destination: str
    travel_month: int
    threshold: int
    latest_price: int | None
    currency: str
    observed_at: str | None
    condition_met: bool | None
    update_id: str | None


@dataclass(frozen=True, slots=True)
class ConditionWorkView:
    worker_status: str
    subscription_id: str | None
    monitoring_status: str | None
    update_id: str | None
    distribution_id: str | None
    reused: bool
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class FeedSummaryView:
    feed_id: str
    topic: str
    feed_state: str
    update_state: str
    message: str
    update_id: str | None
    preview: str | None
    item_count: int
    updated_at: str
    workflow_kind: str = "BRIEFING"
    latest_price: int | None = None
    currency: str | None = None
    threshold: int | None = None
    condition_met: bool | None = None


@dataclass(frozen=True, slots=True)
class UpdatesHomeView:
    ready_updates: tuple[FeedSummaryView, ...]
    needs_attention: tuple[FeedSummaryView, ...]
    preparing: tuple[FeedSummaryView, ...]
    no_updates: tuple[FeedSummaryView, ...]
    has_feeds: bool


@dataclass(frozen=True, slots=True)
class FeedDetailView:
    feed_id: str
    topic: str
    feed_state: str
    feed_message: str
    update_state: str
    update_message: str
    current_definition: FeedDefinitionView
    history: tuple[FeedBriefingView | ConditionUpdateView, ...]
    enabled: bool
    settings_version: int
    workflow_kind: str = "BRIEFING"
    condition_monitoring: ConditionMonitoringView | None = None


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
class RelationEventWorkView:
    worker_status: str
    event_id: str | None
    publication_status: str | None
    user_subscription_id: str | None
    subscription_id: str | None
    relation_status: str | None
    attempt_number: int | None
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class RelationEventInspectionView:
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
        workflow_kind=(
            product.workflow_kind if product is not None else "BRIEFING"
        ),
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


def _feed_definition(snapshot):
    delivery = snapshot.get(
        "delivery_preference", snapshot.get("delivery_channel", "none"),
    )
    return FeedDefinitionView(
        topic=str(snapshot.get("topic") or ""),
        focus_topics=tuple(
            str(item) for item in snapshot.get("focus_topics", ())
        ),
        language=str(snapshot.get("language") or ""),
        cadence=str(snapshot.get("cadence") or ""),
        max_items=int(snapshot.get("max_items") or 0),
        max_chars=int(snapshot.get("max_chars") or 0),
        delivery_preference=str(delivery),
        constraints=tuple(
            str(item) for item in snapshot.get("constraints", ())
        ),
        goal=(str(snapshot["goal"]) if snapshot.get("goal") else None),
        trigger=(
            str(snapshot["trigger"]) if snapshot.get("trigger") else None
        ),
        time_window=(
            str(snapshot["time_window"])
            if snapshot.get("time_window") else None
        ),
        locations=tuple(
            str(item) for item in snapshot.get("locations", ())
        ),
    )


def _confirmation_definition(value):
    """Project durable Definition fields without inventing user ownership."""
    projected = copy.deepcopy(value)
    if "provenance" in projected:
        return projected
    projected.update({
        "constraints": [], "goal": None, "trigger": None,
        "time_window": None, "locations": [],
        "provenance": {
            "topic": "SYSTEM_INFERRED", "focus_topics": "SYSTEM_INFERRED",
            "constraints": "PRODUCT_DEFAULT", "goal": "PRODUCT_DEFAULT",
            "trigger": "PRODUCT_DEFAULT", "time_window": "PRODUCT_DEFAULT",
            "locations": "PRODUCT_DEFAULT", "language": "PRODUCT_DEFAULT",
            "cadence": "POLICY_DEFAULT", "max_chars": "PRODUCT_DEFAULT",
            "max_items": "PRODUCT_DEFAULT",
            "delivery_preference": "PRODUCT_DEFAULT",
        },
    })
    return projected


def _feed_relationship_state(subscription, product, relation):
    if product is None:
        return "active" if subscription.enabled else "paused"
    if (product.status == "ACTIVE" and relation is not None
            and relation.status == "ACTIVE" and subscription.enabled):
        return "active"
    if (product.status == "DISABLED" and relation is not None
            and relation.status == "DISABLED" and not subscription.enabled):
        return "paused"
    return "needs_attention"


def _update_product_projection(status, failure_code=None, has_content=False):
    """Map durable briefing facts to sealed user state and copy."""
    if has_content:
        return "ready", "有新的资讯可以阅读。"
    if status in {"PENDING", "RUNNING"}:
        return "preparing", "正在查找并整理资讯，完成后会出现在这里。"
    if status == "INCOMPLETE" and failure_code == "search_empty_results":
        return "no_update", "这期没有发现值得推荐的新内容，你的关注仍然有效。"
    if status in {"INCOMPLETE", "FAILED"}:
        return (
            "failed",
            "这期资讯暂时没有准备好。你的关注仍然有效，可以稍后再看。",
        )
    if status == "BLOCKED":
        return (
            "needs_attention",
            "这次更新的状态暂时无法确认。为避免重复内容，我们不会自动重做。",
        )
    if status is None:
        return "no_update", "暂时还没有资讯更新。"
    return "needs_attention", "这次更新需要稍后再查看。"


def _clean_item_text(value):
    text = " ".join(str(value or "").split())
    return re.sub(r"(?:\s*\[[A-Za-z]+\d+\])+\s*$", "", text).strip()


def _why_recommended(item, definition, profile_snapshot):
    breakdown = {
        value.get("component"): value.get("value")
        for value in item.get("score_breakdown", ())
        if isinstance(value, dict)
    }
    tags = tuple(
        str(value) for value in item.get("topic_tags", ())
        if isinstance(value, str)
    )
    tags_by_key = {value.strip().casefold(): value for value in tags}
    scope = (*definition.focus_topics, definition.topic)
    matched = []
    for value in scope:
        tag = tags_by_key.get(value.strip().casefold())
        if tag is not None and tag not in matched:
            matched.append(tag)
    reasons = []
    if matched:
        reasons.append("与你当时确认的关注匹配：" + "、".join(matched[:3]))
    profile_weights = {}
    if isinstance(profile_snapshot, dict):
        for value in profile_snapshot.get("topic_weights", ()):
            if (isinstance(value, dict)
                    and isinstance(value.get("topic_key"), str)
                    and type(value.get("weight")) is int):
                profile_weights[value["topic_key"].casefold()] = value["weight"]
    preferred = [
        tag for tag in tags
        if profile_weights.get(tag.casefold(), 0) > 0
    ]
    if breakdown.get("profile_weight", 0) > 0 and preferred:
        reasons.append(
            "生成本期时的兴趣偏好提高了「" + preferred[0] + "」的排序",
        )
    if breakdown.get("freshness", 0) > 0:
        reasons.append("内容在生成本期资讯时仍然较新")
    if (len(reasons) < 3
            and breakdown.get("already_seen_penalty") == 0):
        reasons.append("生成本期资讯时，此内容此前未被推荐过")
    return tuple(reasons[:3] or ("符合本期已确认的关注与排序规则",))


class DigestApplication:
    """The public business boundary consumed by future transports and tests."""

    def __init__(self, repository, subscription_service, generation_workflow,
                 delivery_service, feedback_service,
                 conversation_workflow=None, activation_service=None,
                 outbox_worker=None, relation_event_publisher=None,
                 condition_service=None):
        self.repository = repository
        self.subscriptions = subscription_service
        self.generation = generation_workflow
        self.deliveries = delivery_service
        self.feedback = feedback_service
        self.conversations = conversation_workflow
        self.activations = activation_service
        self.outbox = outbox_worker
        self.relation_events = relation_event_publisher
        self.conditions = condition_service

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
            (_confirmation_definition(payload.get("definition"))
             if conversation.status == "DEFINITION_ACCEPTED" else None),
            turn.status in {"reserved", "running"},
            failure_reason,
            execution.reused, conversation.updated_at,
            turn.failure_stage, turn.failure_subtype,
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

    def adjust_subscription_conversation(self, user_id, conversation_id,
                                         message, idempotency_key):
        key = self._idempotency_key(idempotency_key)
        return self._conversation_operation(
            self.conversations.adjust_conversation,
            user_id, conversation_id, message, key,
        )

    def get_subscription_conversation(self, user_id, conversation_id):
        return self._conversation_operation(
            self.conversations.get, user_id, conversation_id,
        )

    def _subscription_commit_view(self, commit):
        if isinstance(commit, ConditionSubscriptionCommit):
            return SubscriptionCommitView(
                commit.activation.conversation_id,
                commit.activation.definition_outcome_id,
                commit.definition.definition_id,
                commit.definition.definition_version,
                commit.subscription.subscription_id,
                commit.subscription.status,
                commit.relation.user_subscription_id,
                commit.relation.status,
                None, None,
                "已开始监测机票价格，正在检查最近价格。",
                commit.reused,
                commit.activation.created_at,
                "CONDITION",
                commit.condition_request.request_id,
                commit.condition_request.status,
            )
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

    def run_condition_once(self):
        if self.conditions is None:
            raise ApplicationError("configuration_error")
        try:
            value = self.conditions.run_once()
        except ConditionProcessingError as error:
            raise ApplicationError(error.code) from error
        return ConditionWorkView(
            value.worker_status, value.subscription_id,
            value.monitoring_status, value.update_id,
            value.distribution_id, value.reused, value.failure_code,
        )

    def request_condition_check(self, user_id, subscription_id,
                                idempotency_key):
        self._owned_subscription(user_id, subscription_id)
        product = self.repository.get_product_subscription(subscription_id)
        relation = self.repository.get_user_subscription_for_subscription(
            subscription_id,
        )
        if (product is None or relation is None
                or product.workflow_kind != "CONDITION"
                or product.status != "ACTIVE" or relation.status != "ACTIVE"):
            raise ApplicationError("invalid_request")
        key = self._idempotency_key(idempotency_key)
        request_id = hashlib.sha256(
            f"condition-request\n{subscription_id}\n{key}".encode("utf-8"),
        ).hexdigest()[:32]
        timestamp = self.conditions.clock()
        record = ConditionObservationRequest(
            request_id, subscription_id, product.definition_id,
            product.definition_version, key, "PENDING", None, None,
            timestamp, timestamp,
        )
        stored, _created = self.repository.reserve_condition_request(record)
        return stored.request_id

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

    def _feed_facts(self, subscription):
        product = self.repository.get_product_subscription(
            subscription.subscription_id,
        )
        relation = (
            self.repository.get_user_subscription_for_subscription(
                subscription.subscription_id,
            ) if product is not None else None
        )
        briefing, status, run, outbox = (
            self._briefing_resources(subscription.subscription_id)
            if product is not None else (None, None, None, None)
        )
        failure = None
        if run is not None:
            _stage, failure = _failure_projection(run)
        if (failure is None and outbox is not None
                and outbox.status in {"failed", "blocked"}):
            failure = (
                outbox.last_error_code
                if outbox.last_error_code in {
                    "recovery_required", "subscription_inactive",
                } else "recovery_required"
            )
        return product, relation, briefing, status, run, outbox, failure

    def _current_feed_definition(self, subscription, product):
        if product is not None:
            definition = self.repository.get_subscription_definition(
                product.definition_id, product.definition_version,
            )
            if definition is not None:
                return _feed_definition(definition.snapshot)
        return _feed_definition({
            "topic": subscription.topic,
            "focus_topics": subscription.focus_topics,
            "language": subscription.language,
            "cadence": subscription.cadence,
            "max_items": subscription.max_items,
            "max_chars": subscription.max_chars,
            "delivery_channel": subscription.delivery_channel,
        })

    def _condition_monitoring(self, product):
        tracking = self.repository.get_tracking_definition(
            product.definition_id, product.definition_version,
        )
        request = self.repository.get_latest_condition_request_for_subscription(
            product.subscription_id,
        )
        if tracking is None:
            raise ApplicationError("condition_binding_invalid")
        snapshot = tracking.snapshot
        route = snapshot["route"]
        criterion = snapshot["signal"]["criterion"]
        base = {
            "origin": route["origin"], "destination": route["destination"],
            "travel_month": snapshot["travel_month"],
            "threshold": criterion["value"], "currency": criterion["unit"],
        }
        if request is None:
            return ConditionMonitoringView(
                "NEEDS_ATTENTION", "当前监测状态需要稍后确认。",
                **base, latest_price=None, observed_at=None,
                condition_met=None, update_id=None,
            )
        if request.status == "PENDING":
            return ConditionMonitoringView(
                "MONITORING", "正在检查最近价格。",
                **base, latest_price=None, observed_at=None,
                condition_met=None, update_id=None,
            )
        if request.status == "FAILED":
            return ConditionMonitoringView(
                "NEEDS_ATTENTION",
                "最近一次价格检查未能通过数据验证，你的关注仍然有效。",
                **base, latest_price=None, observed_at=None,
                condition_met=None, update_id=None,
            )
        evaluation = self.repository.get_condition_evaluation(
            request.evaluation_id,
        )
        if evaluation is None:
            return ConditionMonitoringView(
                "NEEDS_ATTENTION", "当前监测状态需要稍后确认。",
                **base, latest_price=None, observed_at=None,
                condition_met=None, update_id=None,
            )
        observation = self.repository.get_flight_observation(
            evaluation.observation_id,
        )
        update = self.repository.get_update_for_evaluation(
            evaluation.evaluation_id,
        )
        if observation is None or (
                evaluation.result == "MATCHED" and update is None):
            return ConditionMonitoringView(
                "NEEDS_ATTENTION", "当前监测状态需要稍后确认。",
                **base, latest_price=None, observed_at=None,
                condition_met=None, update_id=None,
            )
        met = evaluation.result == "MATCHED"
        message = (
            f"最近价格 ¥{evaluation.observed_price}，已达到低于 "
            f"¥{evaluation.threshold} 的提醒条件。"
            if met else
            f"最近价格 ¥{evaluation.observed_price}，未达到低于 "
            f"¥{evaluation.threshold} 的提醒条件。"
        )
        return ConditionMonitoringView(
            "MATCHED" if met else "NO_UPDATE", message,
            **base, latest_price=evaluation.observed_price,
            observed_at=observation.quote.observed_at,
            condition_met=met,
            update_id=update.update_id if update is not None else None,
        )

    @staticmethod
    def _condition_update_view(update):
        payload = update.payload
        return ConditionUpdateView(
            update.update_id, update.created_at, payload["title"],
            payload["summary"], payload["origin"], payload["destination"],
            payload["travel_month"], payload["observed_price"],
            payload["threshold"], payload["currency"],
            payload["observed_at"], update.definition_id,
            update.definition_version,
        )

    def _condition_feed_summary(self, subscription, product, relation):
        feed_state = _feed_relationship_state(subscription, product, relation)
        monitoring = self._condition_monitoring(product)
        state = {
            "MONITORING": "preparing",
            "NO_UPDATE": "no_update",
            "MATCHED": "ready",
            "NEEDS_ATTENTION": "needs_attention",
        }[monitoring.status]
        latest = self.repository.list_tracking_updates(
            subscription.user_id, subscription.subscription_id,
        )
        update = latest[0] if latest else None
        preview = (
            update.payload["summary"] if update is not None else
            f"最近价格 ¥{monitoring.latest_price}"
            if monitoring.latest_price is not None else None
        )
        updated_at = (
            update.created_at if update is not None else
            monitoring.observed_at or subscription.updated_at
        )
        return FeedSummaryView(
            subscription.subscription_id, subscription.topic, feed_state,
            state, monitoring.message,
            update.update_id if update is not None else None,
            preview, 0, updated_at, "CONDITION",
            monitoring.latest_price, monitoring.currency,
            monitoring.threshold, monitoring.condition_met,
        )

    def _feed_summary(self, subscription, latest_digest):
        product = self.repository.get_product_subscription(
            subscription.subscription_id,
        )
        if product is not None and product.workflow_kind == "CONDITION":
            relation = self.repository.get_user_subscription_for_subscription(
                subscription.subscription_id,
            )
            return self._condition_feed_summary(
                subscription, product, relation,
            )
        (product, relation, briefing, status, run, outbox,
         failure) = self._feed_facts(subscription)
        feed_state = _feed_relationship_state(
            subscription, product, relation,
        )
        update_state, message = _update_product_projection(
            status, failure, latest_digest is not None,
        )
        if (latest_digest is None and product is not None
                and any(value is None for value in (
                    relation, briefing, outbox,
                ))):
            update_state = "needs_attention"
            message = "这个关注的状态需要稍后确认，请先不要重复创建。"
        preview = None
        item_count = 0
        update_id = None
        updated_at = subscription.updated_at
        if latest_digest is not None:
            content = latest_digest.payload
            items = content.get("items", ())
            item_count = len(items) if isinstance(items, list) else 0
            text = (
                items[0].get("text")
                if item_count and isinstance(items[0], dict)
                else content.get("rendered_text")
            )
            preview = _clean_item_text(text)[:180] or None
            update_id = latest_digest.digest_id
            updated_at = latest_digest.created_at
        elif run is not None and run.updated_at:
            updated_at = run.updated_at
        elif outbox is not None:
            updated_at = outbox.updated_at
        return FeedSummaryView(
            subscription.subscription_id, subscription.topic, feed_state,
            update_state, message, update_id, preview, item_count, updated_at,
        )

    @staticmethod
    def _summary_order(value):
        return value.updated_at, value.update_id or "", value.feed_id

    def get_updates_home(self, user_id):
        subscriptions = self.repository.list_subscriptions_for_user(user_id)
        latest_by_subscription = {}
        for digest in self.repository.list_digests(user_id):
            latest_by_subscription.setdefault(digest.subscription_id, digest)
        groups = {
            "ready": [], "failed": [], "needs_attention": [],
            "preparing": [], "no_update": [],
        }
        for subscription in subscriptions:
            value = self._feed_summary(
                subscription,
                latest_by_subscription.get(subscription.subscription_id),
            )
            groups[value.update_state].append(value)
        ordered = {
            name: tuple(sorted(values, key=self._summary_order, reverse=True))
            for name, values in groups.items()
        }
        attention = tuple(sorted(
            (*ordered["needs_attention"], *ordered["failed"]),
            key=self._summary_order, reverse=True,
        ))
        return UpdatesHomeView(
            ordered["ready"], attention,
            ordered["preparing"], ordered["no_update"],
            bool(subscriptions),
        )

    def _feed_briefing(self, digest):
        run = self.repository.get_digest_run(digest.digest_run_id)
        snapshot = (
            run.subscription_snapshot
            if run is not None and isinstance(run.subscription_snapshot, dict)
            else {}
        )
        definition = _feed_definition(snapshot)
        content = copy.deepcopy(digest.payload)
        candidates = {
            value.candidate_id: value
            for value in self.repository.list_content_candidates(
                digest.digest_run_id,
            )
        }
        refs = {
            value.get("source_ref_id"): value
            for value in content.get("source_refs", ())
            if isinstance(value, dict)
        }
        profile_snapshot = content.get("profile_snapshot")
        items = []
        for raw in content.get("items", ()):
            if not isinstance(raw, dict):
                continue
            candidate = candidates.get(raw.get("candidate_id"))
            text = _clean_item_text(raw.get("text"))
            title = candidate.title if candidate is not None else (
                text.split("：", 1)[0] if "：" in text else "资讯更新"
            )
            summary = text
            prefix = title + "："
            if summary.startswith(prefix):
                summary = summary[len(prefix):].strip()
            sources = []
            for source_id in raw.get("source_ref_ids", ()):
                ref = refs.get(source_id)
                if not isinstance(ref, dict):
                    continue
                source_candidate = candidates.get(ref.get("candidate_id"))
                url = str(ref.get("canonical_url") or "")
                sources.append(FeedSourceView(
                    (source_candidate.title if source_candidate is not None
                     else "原始来源"),
                    (source_candidate.source_domain
                     if source_candidate is not None else ""),
                    url,
                    (source_candidate.published_at
                     if source_candidate is not None else None),
                ))
            items.append(FeedItemView(
                str(raw.get("item_id") or ""), title, summary,
                tuple(sources),
                _why_recommended(raw, definition, profile_snapshot),
            ))
        period_label = (
            run.period_key if run is not None
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", run.period_key)
            else digest.created_at[:10]
        )
        return FeedBriefingView(
            digest.digest_id, digest.created_at, period_label,
            len(items), tuple(items), definition,
        )

    def get_feed_detail(self, user_id, subscription_id):
        subscription = self._owned_subscription(user_id, subscription_id)
        product = self.repository.get_product_subscription(subscription_id)
        if product is not None and product.workflow_kind == "CONDITION":
            relation = self.repository.get_user_subscription_for_subscription(
                subscription_id,
            )
            monitoring = self._condition_monitoring(product)
            history = tuple(
                self._condition_update_view(update)
                for update in self.repository.list_tracking_updates(
                    user_id, subscription_id,
                )
            )
            update_state = {
                "MONITORING": "preparing",
                "NO_UPDATE": "no_update",
                "MATCHED": "ready",
                "NEEDS_ATTENTION": "needs_attention",
            }[monitoring.status]
            feed_state = _feed_relationship_state(
                subscription, product, relation,
            )
            feed_message = {
                "active": "正在监测机票价格",
                "paused": "已暂停，历史更新仍然保留",
                "needs_attention": "关注状态需要稍后确认",
            }[feed_state]
            return FeedDetailView(
                subscription_id, subscription.topic,
                feed_state, feed_message, update_state, monitoring.message,
                self._current_feed_definition(subscription, product), history,
                subscription.enabled, subscription.version,
                "CONDITION", monitoring,
            )
        (product, relation, briefing, status, run, outbox,
         failure) = self._feed_facts(subscription)
        history = tuple(
            self._feed_briefing(digest)
            for digest in self.repository.list_digests(
                user_id, subscription.subscription_id,
            )
        )
        update_state, update_message = _update_product_projection(
            status, failure, bool(history),
        )
        if (not history and product is not None
                and any(value is None for value in (
                    relation, briefing, outbox,
                ))):
            update_state = "needs_attention"
            update_message = "这个关注的状态需要稍后确认，请先不要重复创建。"
        feed_state = _feed_relationship_state(
            subscription, product, relation,
        )
        feed_message = {
            "active": "正在关注",
            "paused": "已暂停，历史资讯仍然保留",
            "needs_attention": "关注状态需要稍后确认",
        }[feed_state]
        return FeedDetailView(
            subscription.subscription_id, subscription.topic,
            feed_state, feed_message, update_state, update_message,
            self._current_feed_definition(subscription, product), history,
            subscription.enabled, subscription.version,
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

    @staticmethod
    def _relation_event_work_view(value):
        return RelationEventWorkView(
            value.worker_status, value.event_id, value.publication_status,
            value.user_subscription_id, value.subscription_id,
            value.relation_status, value.attempt_number,
            value.failure_reason,
        )

    @staticmethod
    def _relation_event_inspection_view(value):
        return RelationEventInspectionView(
            value.event_id, value.event_type, value.publication_status,
            value.outbox_status, value.attempt_number,
            value.attempt_status, value.effect_certainty,
            value.user_subscription_id, value.subscription_id,
            value.relation_status, value.safe_recovery_actions,
            value.blocking_reason, value.updated_at,
        )

    def publish_relation_event_once(self):
        if self.relation_events is None:
            raise ApplicationError("configuration_error")
        try:
            return self._relation_event_work_view(
                self.relation_events.run_once(),
            )
        except RelationEventPublisherError as error:
            raise ApplicationError(error.code) from error
        except (DomainError, ValueError) as error:
            raise ApplicationError("relation_event_processing_failed") from error

    def drain_relation_events(self, maximum):
        if self.relation_events is None:
            raise ApplicationError("configuration_error")
        try:
            return tuple(self._relation_event_work_view(value)
                         for value in self.relation_events.drain(maximum))
        except RelationEventPublisherError as error:
            raise ApplicationError(error.code) from error
        except (DomainError, ValueError) as error:
            raise ApplicationError("relation_event_processing_failed") from error

    def inspect_relation_events(self, event_id=None):
        if self.relation_events is None:
            raise ApplicationError("configuration_error")
        try:
            values = (
                self.relation_events.inspect_all() if event_id is None
                else (self.relation_events.inspect(event_id),)
            )
            return tuple(self._relation_event_inspection_view(value)
                         for value in values)
        except RelationEventPublisherError as error:
            raise ApplicationError(error.code) from error
        except (DomainError, ValueError) as error:
            raise ApplicationError("relation_event_processing_failed") from error

    def recover_relation_event(self, event_id, action):
        if self.relation_events is None:
            raise ApplicationError("configuration_error")
        try:
            return self._relation_event_work_view(
                self.relation_events.recover(event_id, action),
            )
        except RelationEventPublisherError as error:
            raise ApplicationError(error.code) from error
        except (DomainError, ValueError) as error:
            raise ApplicationError("relation_event_processing_failed") from error

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
        product = self.repository.get_product_subscription(subscription_id)
        if product is not None and product.workflow_kind != "BRIEFING":
            raise ApplicationError("invalid_request")
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
