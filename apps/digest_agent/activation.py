"""Application-owned Subscription product commit boundary."""

import copy
import uuid

from .domain import (
    ApplicationOutbox, BriefingReservation, DomainError,
    ConditionObservationRequest, ConditionSubscriptionActivation,
    ConditionSubscriptionCommit,
    ProductSubscription, RelationEventOutbox, Subscription,
    SubscriptionActivation,
    SubscriptionCommit, SubscriptionDefinition, UserSubscription,
    definition_snapshot_identity, outbox_payload_identity,
    relation_event_identity, user_subscription_relation_identity,
    select_tracking_workflow, TrackingDefinition, TrackingPolicySnapshot,
    tracking_definition_identity, tracking_policy_identity,
    validate_definition_protocol, utc_now,
)


class ActivationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class SubscriptionActivationService:
    """Commit one accepted DONE outcome without executing external work."""

    def __init__(self, repository, *, id_factory=None, clock=None,
                 fault_injector=None):
        self.repository = repository
        self.id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self.clock = clock or utc_now
        self.fault_injector = fault_injector

    def _fault(self, stage, value):
        if self.fault_injector is not None:
            self.fault_injector(stage, value)

    def commit(self, user_id, conversation_id):
        conversation = self.repository.get_conversation(conversation_id)
        if conversation is None or conversation.user_id != user_id:
            raise ActivationError("not_found")
        if conversation.status != "DEFINITION_ACCEPTED":
            raise ActivationError("definition_not_accepted")
        turns = self.repository.list_conversation_turns(conversation_id)
        if not turns:
            raise ActivationError("definition_not_accepted")
        outcome = self.repository.get_definition_outcome_for_turn(
            turns[-1].turn_id,
        )
        if outcome is None or outcome.outcome_type != "DONE":
            raise ActivationError("definition_not_accepted")
        try:
            normalized, candidate = validate_definition_protocol(outcome.payload)
        except DomainError as error:
            raise ActivationError("definition_not_accepted") from error
        if candidate is None:
            raise ActivationError("definition_not_accepted")

        try:
            workflow_kind, tracking_snapshot = select_tracking_workflow(
                candidate,
            )
        except DomainError as error:
            raise ActivationError("unsupported_tracking_intent") from error
        if workflow_kind == "CONDITION":
            return self._commit_condition(
                user_id, conversation_id, turns, outcome, normalized,
                candidate, tracking_snapshot,
            )

        timestamp = self.clock()
        definition_id = self.id_factory()
        subscription_id = self.id_factory()
        relation_id = self.id_factory()
        application_run_id = self.id_factory()
        activation_id = self.id_factory()
        outbox_id = self.id_factory()
        definition = SubscriptionDefinition(
            definition_id, 1, conversation_id, outcome.outcome_id,
            copy.deepcopy(normalized["definition"]),
            definition_snapshot_identity(normalized["definition"]), timestamp,
        )
        legacy_subscription = Subscription(
            subscription_id=subscription_id, user_id=user_id,
            natural_language_request=turns[0].safe_text,
            topic=candidate.topic, cadence=candidate.cadence,
            language=candidate.language, max_chars=candidate.max_chars,
            max_items=candidate.max_items,
            focus_topics=candidate.focus_topics,
            delivery_channel=candidate.delivery_preference,
            enabled=True, version=1, created_at=timestamp,
            updated_at=timestamp,
        )
        product = ProductSubscription(
            subscription_id, definition_id, 1, "ACTIVE",
            timestamp, timestamp,
        )
        relation = UserSubscription(
            relation_id, user_id, subscription_id, "ACTIVE",
            timestamp, timestamp,
        )
        relation_identity = user_subscription_relation_identity(relation, 1)
        event_id = relation_event_identity(relation_id, 1)
        event_payload = {
            "event_id": event_id,
            "event_type": "USER_SUBSCRIPTION_CREATED",
            "user_subscription_id": relation_id,
            "user_id": user_id,
            "subscription_id": subscription_id,
            "relation_version": 1,
            "relation_identity": relation_identity,
            "created_at": timestamp,
        }
        relation_event = RelationEventOutbox(
            event_id, "USER_SUBSCRIPTION_CREATED", relation_id, user_id,
            subscription_id, 1, relation_identity, event_payload,
            outbox_payload_identity(event_payload), "pending", 0,
            timestamp, timestamp, None, 1, timestamp,
        )
        briefing = BriefingReservation(
            application_run_id, subscription_id, definition_id, 1,
            "PENDING", None, timestamp, timestamp,
        )
        activation = SubscriptionActivation(
            activation_id, conversation_id, outcome.outcome_id,
            definition_id, subscription_id, relation_id,
            application_run_id, outbox_id, timestamp,
        )
        refs = {
            "activation_id": activation_id,
            "definition_id": definition_id,
            "definition_version": 1,
            "application_run_id": application_run_id,
        }
        outbox = ApplicationOutbox(
            outbox_id, "FIRST_BRIEFING_REQUESTED", subscription_id,
            application_run_id, refs, outbox_payload_identity(refs),
            "pending", 0, timestamp, timestamp, None, 1, timestamp,
        )
        proposed = SubscriptionCommit(
            definition, legacy_subscription, product, relation,
            relation_event, briefing, outbox, activation,
        )
        committed = self.repository.commit_subscription_product(
            user_id, proposed, self.fault_injector,
        )
        self._fault("after_commit", committed)
        return committed

    def _commit_condition(self, user_id, conversation_id, turns, outcome,
                          normalized, candidate, tracking_snapshot):
        timestamp = self.clock()
        definition_id = self.id_factory()
        subscription_id = self.id_factory()
        relation_id = self.id_factory()
        activation_id = self.id_factory()
        request_id = self.id_factory()
        definition = SubscriptionDefinition(
            definition_id, 1, conversation_id, outcome.outcome_id,
            copy.deepcopy(normalized["definition"]),
            definition_snapshot_identity(normalized["definition"]), timestamp,
        )
        legacy_subscription = Subscription(
            subscription_id=subscription_id, user_id=user_id,
            natural_language_request=turns[0].safe_text,
            topic=candidate.topic, cadence=candidate.cadence,
            language=candidate.language, max_chars=candidate.max_chars,
            max_items=candidate.max_items,
            focus_topics=candidate.focus_topics,
            delivery_channel=candidate.delivery_preference,
            enabled=True, version=1, created_at=timestamp,
            updated_at=timestamp,
        )
        product = ProductSubscription(
            subscription_id, definition_id, 1, "ACTIVE",
            timestamp, timestamp, "CONDITION",
        )
        relation = UserSubscription(
            relation_id, user_id, subscription_id, "ACTIVE",
            timestamp, timestamp,
        )
        relation_identity = user_subscription_relation_identity(relation, 1)
        event_id = relation_event_identity(relation_id, 1)
        event_payload = {
            "event_id": event_id,
            "event_type": "USER_SUBSCRIPTION_CREATED",
            "user_subscription_id": relation_id,
            "user_id": user_id,
            "subscription_id": subscription_id,
            "relation_version": 1,
            "relation_identity": relation_identity,
            "created_at": timestamp,
        }
        relation_event = RelationEventOutbox(
            event_id, "USER_SUBSCRIPTION_CREATED", relation_id, user_id,
            subscription_id, 1, relation_identity, event_payload,
            outbox_payload_identity(event_payload), "pending", 0,
            timestamp, timestamp, None, 1, timestamp,
        )
        tracking = TrackingDefinition(
            definition_id, 1, subscription_id, "CONDITION",
            tracking_snapshot,
            tracking_definition_identity(tracking_snapshot), timestamp,
        )
        execution = {
            "observation_source": "fake_flight_price",
            "observation_cadence": "manual_once",
            "freshness_seconds": 86_400,
            "evaluator_version": "flight_price_lt_v1",
        }
        presentation = {
            "language": candidate.language,
            "max_chars": candidate.max_chars,
            "max_items": candidate.max_items,
            "provenance": candidate.provenance["language"],
        }
        distribution = {
            "notification": "none", "provenance": "PRODUCT_DEFAULT",
        }
        policies = TrackingPolicySnapshot(
            subscription_id, definition_id, 1,
            execution, presentation, distribution,
            tracking_policy_identity(execution, presentation, distribution),
            timestamp,
        )
        request = ConditionObservationRequest(
            request_id, subscription_id, definition_id, 1,
            f"first-condition:{definition_id}:1", "PENDING", None, None,
            timestamp, timestamp,
        )
        activation = ConditionSubscriptionActivation(
            activation_id, conversation_id, outcome.outcome_id,
            definition_id, subscription_id, relation_id, request_id,
            timestamp,
        )
        proposed = ConditionSubscriptionCommit(
            definition, legacy_subscription, product, relation,
            relation_event, tracking, policies, request, activation,
        )
        committed = self.repository.commit_condition_subscription_product(
            user_id, proposed, self.fault_injector,
        )
        self._fault("after_commit", committed)
        return committed
