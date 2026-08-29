"""Teaching-grade stdlib SQLite repositories for the current vertical slice."""

from dataclasses import asdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3

from ..domain import (
    FEEDBACK_DELTAS, PROFILE_RULE_VERSION, PROFILE_WEIGHT_MAX,
    PROFILE_WEIGHT_MIN, AcceptedFlightPriceObservation, ApplicationOutbox,
    BriefingReservation, ConditionEvaluation, ConditionObservationCycle,
    ConditionObservationRequest, ConditionSubscriptionActivation,
    ConditionSubscriptionCommit, ConditionTemporalState, Conversation,
    ContentCandidate, ConversationTurn, DefinitionOutcome, DeliveryRecord,
    Digest, FlightPriceQuote,
    FeedbackResult, InterestProfile, Interaction, ProductSubscription,
    ProfileUpdate, RelationEventAttempt, RelationEventOutbox, Subscription,
    SubscriptionActivation, SubscriptionCommit, SubscriptionDefinition,
    TopicWeight, TrackingDefinition, TrackingPolicySnapshot, TrackingUpdate,
    UpdateDistribution, UserSubscription, normalize_topic,
    EventCandidate, EventCandidateSupport, EventObservationCycle,
    EventSourceObservation, EventSourceResult, EventSubscriptionActivation,
    EventSubscriptionCommit, EventTemporalState, EventVerification,
    VerifiedEvent, condition_cycle_identity, event_cycle_identity,
    event_harness_run_identity, utc_timestamp,
    relation_event_attempt_identity, relation_event_identity,
    user_subscription_relation_identity,
)
from ..repositories import (
    DefinitionAttemptRecord, DigestRunRecord, GenerationAttemptRecord,
    RecoveryOperationRecord,
)


SCHEMA_VERSION = 17


class SQLiteDigestRepository:
    def __init__(self, path):
        self.path = str(path)
        self.migrate()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def migrate(self):
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
            """)
            versions = {
                row[0] for row in connection.execute(
                    "SELECT version FROM schema_migrations"
                )
            }
            if 1 not in versions:
                self._migrate_v1(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (1, datetime('now'))"
                )
            if 2 not in versions:
                self._migrate_v2(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (2, datetime('now'))"
                )
            if 3 not in versions:
                self._migrate_v3(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (3, datetime('now'))"
                )
            if 4 not in versions:
                self._migrate_v4(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (4, datetime('now'))"
                )
            if 5 not in versions:
                self._migrate_v5(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (5, datetime('now'))"
                )
            if 6 not in versions:
                self._migrate_v6(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (6, datetime('now'))"
                )
            if 7 not in versions:
                self._migrate_v7(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (7, datetime('now'))"
                )
            if 8 not in versions:
                self._migrate_v8(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (8, datetime('now'))"
                )
            if 9 not in versions:
                self._migrate_v9(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (9, datetime('now'))"
                )
            if 10 not in versions:
                self._migrate_v10(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (10, datetime('now'))"
                )
            if 11 not in versions:
                self._migrate_v11(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (11, datetime('now'))"
                )
            if 12 not in versions:
                self._apply_v12(connection)
            if 13 not in versions:
                self._apply_v13(connection)
            if 14 not in versions:
                self._apply_v14(connection)
            if 15 not in versions:
                self._apply_v15(connection)
            if 16 not in versions:
                self._apply_v16(connection)
            if 17 not in versions:
                self._apply_v17(connection)

    @staticmethod
    def _migrate_v1(connection):
        connection.executescript("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    subscription_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS digest_runs (
                    digest_run_id TEXT PRIMARY KEY,
                    subscription_id TEXT NOT NULL REFERENCES subscriptions(subscription_id),
                    period_key TEXT NOT NULL,
                    harness_run_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    reason TEXT,
                    digest_id TEXT,
                    artifact_id TEXT,
                    harness_result_json TEXT,
                    UNIQUE(subscription_id, period_key)
                );
                CREATE TABLE IF NOT EXISTS content_candidates (
                    digest_run_id TEXT NOT NULL REFERENCES digest_runs(digest_run_id),
                    candidate_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(digest_run_id, candidate_id)
                );
                CREATE TABLE IF NOT EXISTS digests (
                    digest_id TEXT PRIMARY KEY,
                    digest_run_id TEXT NOT NULL UNIQUE REFERENCES digest_runs(digest_run_id),
                    subscription_id TEXT NOT NULL REFERENCES subscriptions(subscription_id),
                    harness_run_id TEXT NOT NULL UNIQUE,
                    artifact_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _migrate_v2(connection):
        connection.executescript("""
            ALTER TABLE digest_runs ADD COLUMN profile_version INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE digest_runs ADD COLUMN profile_projection_id TEXT;
            ALTER TABLE digest_runs ADD COLUMN profile_projection_json TEXT;
            CREATE TABLE interest_profiles (
                user_id TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                rule_version INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE profile_topic_weights (
                user_id TEXT NOT NULL REFERENCES interest_profiles(user_id),
                topic_key TEXT NOT NULL,
                weight INTEGER NOT NULL CHECK(weight BETWEEN -20 AND 20),
                PRIMARY KEY(user_id, topic_key)
            );
            CREATE TABLE interactions (
                feedback_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                digest_id TEXT NOT NULL REFERENCES digests(digest_id),
                item_id TEXT,
                feedback_type TEXT NOT NULL CHECK(
                    feedback_type IN ('opened', 'liked', 'dismissed', 'saved')
                ),
                event_key TEXT NOT NULL,
                topic_keys_json TEXT NOT NULL,
                delta INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, digest_id, item_id, feedback_type, event_key)
            );
            CREATE TABLE profile_updates (
                feedback_id TEXT PRIMARY KEY REFERENCES interactions(feedback_id),
                before_version INTEGER NOT NULL,
                after_version INTEGER NOT NULL,
                changes_json TEXT NOT NULL
            );
            CREATE TABLE seen_content (
                user_id TEXT NOT NULL,
                content_identity TEXT NOT NULL,
                first_digest_id TEXT NOT NULL REFERENCES digests(digest_id),
                PRIMARY KEY(user_id, content_identity)
            );
        """)

    @staticmethod
    def _migrate_v3(connection):
        connection.executescript("""
            CREATE TABLE delivery_records (
                delivery_id TEXT PRIMARY KEY,
                digest_id TEXT NOT NULL REFERENCES digests(digest_id),
                user_id TEXT NOT NULL,
                channel TEXT NOT NULL CHECK(
                    channel IN ('fake', 'termux_notification')
                ),
                status TEXT NOT NULL CHECK(
                    status IN ('pending', 'accepted', 'failed', 'unknown')
                ),
                current_attempt_number INTEGER NOT NULL,
                current_attempt_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(digest_id, channel)
            );
            CREATE TABLE delivery_attempts (
                attempt_id TEXT PRIMARY KEY,
                delivery_id TEXT NOT NULL REFERENCES delivery_records(delivery_id),
                attempt_number INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(
                    status IN ('pending', 'accepted', 'failed', 'unknown')
                ),
                provider_message_id TEXT,
                requested_at TEXT NOT NULL,
                completed_at TEXT,
                error_code TEXT,
                effect_certainty TEXT NOT NULL CHECK(
                    effect_certainty IN ('not_started', 'known_applied', 'unknown')
                ),
                UNIQUE(delivery_id, attempt_number)
            );
        """)

    @staticmethod
    def _migrate_v4(connection):
        connection.executescript("""
            ALTER TABLE digest_runs ADD COLUMN idempotency_key TEXT;
            ALTER TABLE digest_runs ADD COLUMN subscription_version INTEGER NOT NULL DEFAULT 1;
            ALTER TABLE digest_runs ADD COLUMN subscription_snapshot_json TEXT;
            ALTER TABLE digest_runs ADD COLUMN harness_bound_at TEXT;
            ALTER TABLE digest_runs ADD COLUMN started_at TEXT;
            ALTER TABLE digest_runs ADD COLUMN updated_at TEXT;
            UPDATE digest_runs SET idempotency_key=period_key
                WHERE idempotency_key IS NULL;
            CREATE UNIQUE INDEX digest_runs_idempotency
                ON digest_runs(subscription_id, idempotency_key);
        """)

    @staticmethod
    def _migrate_v5(connection):
        connection.executescript("""
            CREATE TABLE recovery_operations (
                operation_id TEXT PRIMARY KEY,
                application_run_id TEXT NOT NULL REFERENCES digest_runs(digest_run_id),
                action TEXT NOT NULL CHECK(action IN (
                    'resume_original_run', 'resume_bound_run', 'repair_projection'
                )),
                status TEXT NOT NULL CHECK(status IN ('started', 'completed', 'failed')),
                before_state TEXT NOT NULL,
                after_state TEXT,
                requested_at TEXT NOT NULL,
                completed_at TEXT,
                error_code TEXT,
                UNIQUE(application_run_id, action)
            );
        """)

    @staticmethod
    def _migrate_v6(connection):
        connection.executescript("""
            ALTER TABLE digest_runs ADD COLUMN failure_stage TEXT CHECK(
                failure_stage IS NULL OR failure_stage IN (
                    'configuration', 'search', 'generation', 'contract',
                    'persistence', 'delivery', 'recovery'
                )
            );
            ALTER TABLE digest_runs ADD COLUMN failure_code TEXT;
        """)

    @staticmethod
    def _migrate_v7(connection):
        connection.executescript("""
            CREATE TABLE generation_attempts (
                attempt_id TEXT PRIMARY KEY,
                application_run_id TEXT NOT NULL REFERENCES digest_runs(digest_run_id),
                attempt_number INTEGER NOT NULL CHECK(attempt_number IN (1, 2)),
                status TEXT NOT NULL CHECK(status IN ('started', 'succeeded', 'failed')),
                request_metadata_json TEXT NOT NULL,
                response_metadata_json TEXT,
                failure_subtype TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(application_run_id, attempt_number)
            );
        """)

    @staticmethod
    def _migrate_v8(connection):
        connection.executescript("""
            ALTER TABLE digest_runs ADD COLUMN failure_subtype TEXT CHECK(
                failure_subtype IS NULL OR failure_subtype IN (
                    'too_long', 'too_many_items', 'invalid_content_ref',
                    'invalid_source_ref', 'duplicate_item',
                    'topic_focus_mismatch', 'missing_required_field',
                    'invalid_marker', 'other_contract_failure'
                )
            );
            ALTER TABLE digest_runs ADD COLUMN failure_diagnostics_json TEXT;
        """)

    @staticmethod
    def _migrate_v9(connection):
        connection.executescript("""
            ALTER TABLE digest_runs ADD COLUMN generation_failure_subtype TEXT CHECK(
                generation_failure_subtype IS NULL OR
                generation_failure_subtype IN (
                    'TOP_LEVEL_SHAPE', 'SUMMARY_TYPE', 'SUMMARY_EMPTY',
                    'SUMMARY_TOO_LONG', 'SUMMARY_CONTROL', 'ITEMS_TYPE',
                    'ITEM_COUNT', 'ITEM_SHAPE', 'ITEM_STRING_TYPE',
                    'ITEM_STRING_EMPTY', 'ITEM_STRING_TOO_LONG',
                    'ITEM_STRING_CONTROL', 'ITEM_SOURCE_REFS_TYPE',
                    'ITEM_SOURCE_REFS_COUNT', 'SELECTED_REFS_TYPE',
                    'SELECTED_REFS_COUNT', 'SELECTED_REF_SHAPE',
                    'SELECTED_REF_STRING_TYPE', 'SELECTED_REF_STRING_EMPTY',
                    'SELECTED_REF_STRING_TOO_LONG',
                    'SELECTED_REF_STRING_CONTROL', 'EXPECTING_COMMA',
                    'UNTERMINATED_STRING', 'INVALID_ESCAPE',
                    'EXPECTING_PROPERTY_NAME', 'EXTRA_DATA',
                    'OTHER_JSON_SYNTAX', 'ENVELOPE_EXTRACTION'
                )
            );
        """)

    @staticmethod
    def _migrate_v10(connection):
        connection.executescript("""
            CREATE TABLE conversations (
                conversation_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'COLLECTING', 'WAITING_FOR_ANSWER', 'REJECTED',
                    'DEFINITION_ACCEPTED', 'INCOMPLETE'
                )),
                turn_count INTEGER NOT NULL CHECK(turn_count >= 0),
                version INTEGER NOT NULL CHECK(version >= 1),
                start_idempotency_key TEXT NOT NULL,
                terminal_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, start_idempotency_key)
            );
            CREATE TABLE conversation_turns (
                turn_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
                turn_number INTEGER NOT NULL CHECK(turn_number >= 1),
                role TEXT NOT NULL CHECK(role = 'user'),
                safe_text TEXT NOT NULL,
                message_idempotency_key TEXT NOT NULL,
                harness_run_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK(status IN (
                    'reserved', 'running', 'completed', 'failed', 'blocked'
                )),
                outcome_id TEXT,
                error_code TEXT,
                claim_owner_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(conversation_id, turn_number),
                UNIQUE(conversation_id, message_idempotency_key)
            );
            CREATE TABLE definition_outcomes (
                outcome_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
                turn_id TEXT NOT NULL UNIQUE REFERENCES conversation_turns(turn_id),
                outcome_type TEXT NOT NULL CHECK(outcome_type IN (
                    'NEXT_QUESTION', 'REJECT', 'DONE'
                )),
                payload_json TEXT NOT NULL,
                candidate_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)

    @staticmethod
    def _migrate_v11(connection):
        connection.executescript("""
            ALTER TABLE digest_runs ADD COLUMN definition_id TEXT;
            ALTER TABLE digest_runs ADD COLUMN definition_version INTEGER
                CHECK(definition_version IS NULL OR definition_version >= 1);
            CREATE TABLE subscription_definitions (
                definition_id TEXT NOT NULL,
                definition_version INTEGER NOT NULL CHECK(definition_version >= 1),
                conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
                definition_outcome_id TEXT NOT NULL UNIQUE
                    REFERENCES definition_outcomes(outcome_id),
                snapshot_json TEXT NOT NULL,
                snapshot_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(definition_id, definition_version),
                UNIQUE(definition_id, definition_outcome_id)
            );
            CREATE TABLE subscription_aggregates (
                subscription_id TEXT PRIMARY KEY REFERENCES subscriptions(subscription_id),
                definition_id TEXT NOT NULL,
                definition_version INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'DISABLED')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(definition_id, definition_version)
                    REFERENCES subscription_definitions(definition_id, definition_version)
            );
            CREATE TABLE user_subscriptions (
                user_subscription_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                subscription_id TEXT NOT NULL UNIQUE
                    REFERENCES subscription_aggregates(subscription_id),
                status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'DISABLED')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, subscription_id)
            );
            CREATE TABLE briefing_reservations (
                application_run_id TEXT PRIMARY KEY,
                subscription_id TEXT NOT NULL UNIQUE
                    REFERENCES subscription_aggregates(subscription_id),
                definition_id TEXT NOT NULL,
                definition_version INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status = 'PENDING'),
                harness_run_id TEXT UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(definition_id, definition_version)
                    REFERENCES subscription_definitions(definition_id, definition_version)
            );
            CREATE TABLE application_outbox (
                outbox_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL CHECK(event_type = 'FIRST_BRIEFING_REQUESTED'),
                subscription_id TEXT NOT NULL
                    REFERENCES subscription_aggregates(subscription_id),
                application_run_id TEXT NOT NULL UNIQUE
                    REFERENCES briefing_reservations(application_run_id),
                payload_ref_json TEXT NOT NULL,
                payload_identity TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'pending', 'claimed', 'retry_wait', 'completed',
                    'failed', 'blocked'
                )),
                attempt_number INTEGER NOT NULL CHECK(attempt_number >= 0),
                created_at TEXT NOT NULL,
                available_at TEXT NOT NULL,
                last_error_code TEXT,
                version INTEGER NOT NULL CHECK(version >= 1),
                updated_at TEXT NOT NULL,
                UNIQUE(event_type, subscription_id)
            );
            CREATE INDEX application_outbox_ready
                ON application_outbox(status, available_at, created_at);
            CREATE TABLE subscription_activations (
                activation_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
                definition_outcome_id TEXT NOT NULL UNIQUE
                    REFERENCES definition_outcomes(outcome_id),
                definition_id TEXT NOT NULL,
                subscription_id TEXT NOT NULL UNIQUE
                    REFERENCES subscription_aggregates(subscription_id),
                user_subscription_id TEXT NOT NULL UNIQUE
                    REFERENCES user_subscriptions(user_subscription_id),
                application_run_id TEXT NOT NULL UNIQUE
                    REFERENCES briefing_reservations(application_run_id),
                outbox_id TEXT NOT NULL UNIQUE REFERENCES application_outbox(outbox_id),
                created_at TEXT NOT NULL,
                FOREIGN KEY(definition_id, definition_outcome_id)
                    REFERENCES subscription_definitions(definition_id, definition_outcome_id)
            );
        """)

    @classmethod
    def _apply_v12(cls, connection, fault_injector=None):
        """Atomically publish v12 DDL and its migration-ledger row."""
        connection.commit()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cls._migrate_v12(connection, fault_injector=fault_injector)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) "
                "VALUES (12, datetime('now'))"
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _migrate_v12(connection, fault_injector=None):
        connection.execute("""
            ALTER TABLE conversation_turns ADD COLUMN failure_stage TEXT CHECK(
                failure_stage IS NULL OR failure_stage IN (
                    'definition_generation', 'protocol_validation',
                    'definition_validation', 'recovery'
                )
            )
        """)
        if fault_injector is not None:
            fault_injector("after_failure_stage")
        connection.execute("""
            ALTER TABLE conversation_turns ADD COLUMN failure_subtype TEXT
        """)
        if fault_injector is not None:
            fault_injector("after_failure_subtype")
        connection.execute("""
            CREATE TABLE definition_attempts (
                attempt_id TEXT PRIMARY KEY,
                turn_id TEXT NOT NULL REFERENCES conversation_turns(turn_id),
                attempt_number INTEGER NOT NULL CHECK(attempt_number IN (1, 2)),
                status TEXT NOT NULL CHECK(status IN (
                    'started', 'succeeded', 'failed'
                )),
                request_metadata_json TEXT NOT NULL,
                response_metadata_json TEXT,
                candidate_payload_json TEXT,
                failure_stage TEXT CHECK(failure_stage IS NULL OR failure_stage IN (
                    'definition_generation', 'protocol_validation'
                )),
                failure_subtype TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(turn_id, attempt_number)
            )
        """)
        if fault_injector is not None:
            fault_injector("after_definition_attempts")

    @classmethod
    def _apply_v13(cls, connection, fault_injector=None):
        """Atomically publish typed relation-event outbox schema and ledger."""
        connection.commit()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cls._migrate_v13(connection, fault_injector=fault_injector)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) "
                "VALUES (13, datetime('now'))"
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _migrate_v13(connection, fault_injector=None):
        connection.execute("""
            CREATE TABLE relation_event_outbox (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL CHECK(
                    event_type = 'USER_SUBSCRIPTION_CREATED'
                ),
                user_subscription_id TEXT NOT NULL
                    REFERENCES user_subscriptions(user_subscription_id),
                user_id TEXT NOT NULL,
                subscription_id TEXT NOT NULL
                    REFERENCES subscription_aggregates(subscription_id),
                relation_version INTEGER NOT NULL CHECK(relation_version >= 1),
                relation_identity TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_identity TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'pending', 'claimed', 'retry_wait', 'completed',
                    'failed', 'blocked'
                )),
                attempt_number INTEGER NOT NULL CHECK(attempt_number >= 0),
                created_at TEXT NOT NULL,
                available_at TEXT NOT NULL,
                last_error_code TEXT,
                version INTEGER NOT NULL CHECK(version >= 1),
                updated_at TEXT NOT NULL,
                UNIQUE(event_type, user_subscription_id, relation_version)
            )
        """)
        if fault_injector is not None:
            fault_injector("after_relation_event_outbox")
        connection.execute("""
            CREATE INDEX relation_event_outbox_ready
            ON relation_event_outbox(status, available_at, created_at)
        """)
        connection.execute("""
            CREATE TABLE relation_event_attempts (
                attempt_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL
                    REFERENCES relation_event_outbox(event_id),
                attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1),
                status TEXT NOT NULL CHECK(status IN (
                    'prepared', 'unknown', 'accepted', 'failed'
                )),
                effect_certainty TEXT NOT NULL CHECK(effect_certainty IN (
                    'not_started', 'unknown', 'known_applied'
                )),
                requested_at TEXT NOT NULL,
                completed_at TEXT,
                error_code TEXT,
                UNIQUE(event_id, attempt_number)
            )
        """)
        if fault_injector is not None:
            fault_injector("after_relation_event_attempts")

    @classmethod
    def _apply_v14(cls, connection, fault_injector=None):
        """Atomically add the narrow P4.3 CONDITION product boundary."""
        connection.commit()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cls._migrate_v14(connection, fault_injector=fault_injector)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) "
                "VALUES (14, datetime('now'))"
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _migrate_v14(connection, fault_injector=None):
        connection.execute("""
            ALTER TABLE subscription_aggregates
            ADD COLUMN workflow_kind TEXT NOT NULL DEFAULT 'BRIEFING'
            CHECK(workflow_kind IN ('BRIEFING', 'CONDITION', 'EVENT'))
        """)
        if fault_injector is not None:
            fault_injector("after_workflow_kind")
        schema = """
            CREATE TABLE tracking_definitions (
                definition_id TEXT NOT NULL,
                definition_version INTEGER NOT NULL CHECK(definition_version >= 1),
                subscription_id TEXT NOT NULL UNIQUE
                    REFERENCES subscription_aggregates(subscription_id),
                workflow_kind TEXT NOT NULL CHECK(workflow_kind = 'CONDITION'),
                snapshot_json TEXT NOT NULL,
                snapshot_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(definition_id, definition_version),
                FOREIGN KEY(definition_id, definition_version)
                    REFERENCES subscription_definitions(definition_id, definition_version)
            );
            CREATE TABLE tracking_policy_snapshots (
                subscription_id TEXT NOT NULL
                    REFERENCES subscription_aggregates(subscription_id),
                definition_id TEXT NOT NULL,
                definition_version INTEGER NOT NULL CHECK(definition_version >= 1),
                execution_json TEXT NOT NULL,
                presentation_json TEXT NOT NULL,
                distribution_json TEXT NOT NULL,
                snapshot_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(subscription_id, definition_id, definition_version),
                FOREIGN KEY(definition_id, definition_version)
                    REFERENCES tracking_definitions(definition_id, definition_version)
            );
            CREATE TABLE flight_price_observations (
                observation_id TEXT PRIMARY KEY,
                subscription_id TEXT NOT NULL
                    REFERENCES subscription_aggregates(subscription_id),
                source_signal_id TEXT NOT NULL,
                signal_identity TEXT NOT NULL,
                origin TEXT NOT NULL,
                destination TEXT NOT NULL,
                trip_type TEXT NOT NULL CHECK(trip_type = 'round_trip'),
                travel_month INTEGER NOT NULL CHECK(travel_month BETWEEN 1 AND 12),
                metric TEXT NOT NULL CHECK(metric = 'round_trip_price'),
                price INTEGER NOT NULL CHECK(price > 0),
                currency TEXT NOT NULL CHECK(currency = 'CNY'),
                observed_at TEXT NOT NULL,
                evidence_id TEXT NOT NULL UNIQUE,
                accepted_at TEXT NOT NULL,
                UNIQUE(subscription_id, signal_identity)
            );
            CREATE TABLE condition_evaluations (
                evaluation_id TEXT PRIMARY KEY,
                subscription_id TEXT NOT NULL
                    REFERENCES subscription_aggregates(subscription_id),
                definition_id TEXT NOT NULL,
                definition_version INTEGER NOT NULL CHECK(definition_version >= 1),
                observation_id TEXT NOT NULL
                    REFERENCES flight_price_observations(observation_id),
                evidence_id TEXT NOT NULL,
                observed_price INTEGER NOT NULL CHECK(observed_price > 0),
                threshold INTEGER NOT NULL CHECK(threshold > 0),
                currency TEXT NOT NULL CHECK(currency = 'CNY'),
                operator TEXT NOT NULL CHECK(operator = 'lt'),
                result TEXT NOT NULL CHECK(result IN ('NO_UPDATE', 'MATCHED')),
                evaluator_version TEXT NOT NULL
                    CHECK(evaluator_version = 'flight_price_lt_v1'),
                evaluated_at TEXT NOT NULL,
                UNIQUE(subscription_id, definition_id, definition_version,
                       observation_id),
                FOREIGN KEY(definition_id, definition_version)
                    REFERENCES tracking_definitions(definition_id, definition_version)
            );
            CREATE TABLE condition_observation_requests (
                request_id TEXT PRIMARY KEY,
                subscription_id TEXT NOT NULL
                    REFERENCES subscription_aggregates(subscription_id),
                definition_id TEXT NOT NULL,
                definition_version INTEGER NOT NULL CHECK(definition_version >= 1),
                idempotency_key TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'PENDING', 'EVALUATED', 'FAILED'
                )),
                evaluation_id TEXT REFERENCES condition_evaluations(evaluation_id),
                failure_code TEXT CHECK(failure_code IS NULL OR failure_code IN (
                    'INVALID_OBSERVATION', 'STALE_OBSERVATION'
                )),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(subscription_id, idempotency_key),
                FOREIGN KEY(definition_id, definition_version)
                    REFERENCES tracking_definitions(definition_id, definition_version)
            );
            CREATE INDEX condition_requests_ready
                ON condition_observation_requests(status, created_at, request_id);
            CREATE TABLE tracking_updates (
                update_id TEXT PRIMARY KEY,
                subscription_id TEXT NOT NULL
                    REFERENCES subscription_aggregates(subscription_id),
                definition_id TEXT NOT NULL,
                definition_version INTEGER NOT NULL CHECK(definition_version >= 1),
                evaluation_id TEXT NOT NULL UNIQUE
                    REFERENCES condition_evaluations(evaluation_id),
                evidence_id TEXT NOT NULL,
                update_type TEXT NOT NULL CHECK(update_type = 'CONDITION'),
                payload_json TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(definition_id, definition_version)
                    REFERENCES tracking_definitions(definition_id, definition_version)
            );
            CREATE TABLE update_distributions (
                distribution_id TEXT PRIMARY KEY,
                update_id TEXT NOT NULL
                    REFERENCES tracking_updates(update_id),
                user_subscription_id TEXT NOT NULL
                    REFERENCES user_subscriptions(user_subscription_id),
                status TEXT NOT NULL CHECK(status = 'AVAILABLE'),
                created_at TEXT NOT NULL,
                UNIQUE(update_id, user_subscription_id)
            );
            CREATE TABLE condition_subscription_activations (
                activation_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
                definition_outcome_id TEXT NOT NULL UNIQUE
                    REFERENCES definition_outcomes(outcome_id),
                definition_id TEXT NOT NULL,
                subscription_id TEXT NOT NULL UNIQUE
                    REFERENCES subscription_aggregates(subscription_id),
                user_subscription_id TEXT NOT NULL UNIQUE
                    REFERENCES user_subscriptions(user_subscription_id),
                condition_request_id TEXT NOT NULL UNIQUE
                    REFERENCES condition_observation_requests(request_id),
                created_at TEXT NOT NULL,
                FOREIGN KEY(definition_id, definition_outcome_id)
                    REFERENCES subscription_definitions(
                        definition_id, definition_outcome_id
                    )
            );
        """
        # ``executescript`` commits an open transaction before running. Apply
        # each statement through the transaction opened by ``_apply_v14`` so
        # a fault cannot leave a partially installed CONDITION schema.
        for statement in schema.split(";"):
            statement = statement.strip()
            if statement:
                connection.execute(statement)
        if fault_injector is not None:
            fault_injector("after_condition_tables")

    @classmethod
    def _apply_v15(cls, connection, fault_injector=None):
        """Atomically add P4.4 Flight CONDITION temporal truth."""
        connection.commit()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cls._migrate_v15(connection, fault_injector=fault_injector)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) "
                "VALUES (15, datetime('now'))"
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _migrate_v15(connection, fault_injector=None):
        statements = (
            """
            CREATE TABLE condition_observation_cycles (
                cycle_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE
                    REFERENCES condition_observation_requests(request_id),
                subscription_id TEXT NOT NULL
                    REFERENCES subscription_aggregates(subscription_id),
                definition_id TEXT NOT NULL,
                definition_version INTEGER NOT NULL CHECK(definition_version >= 1),
                execution_policy_version INTEGER NOT NULL
                    CHECK(execution_policy_version >= 1),
                cycle_kind TEXT NOT NULL CHECK(cycle_kind IN (
                    'INITIAL', 'SCHEDULED', 'CATCH_UP', 'RESUME', 'MANUAL'
                )),
                scheduled_due_at TEXT NOT NULL,
                coalesced_from_at TEXT NOT NULL,
                coalesced_to_at TEXT NOT NULL,
                coalesced_count INTEGER NOT NULL CHECK(coalesced_count >= 1),
                status TEXT NOT NULL CHECK(status IN (
                    'PENDING', 'STARTED', 'SUCCEEDED', 'FAILED', 'SUPERSEDED'
                )),
                claim_token TEXT,
                claimed_at TEXT,
                observation_id TEXT REFERENCES flight_price_observations(observation_id),
                evaluation_id TEXT REFERENCES condition_evaluations(evaluation_id),
                predicate_truth TEXT CHECK(
                    predicate_truth IS NULL OR predicate_truth IN ('FALSE', 'TRUE')
                ),
                emission_decision TEXT CHECK(
                    emission_decision IS NULL OR emission_decision IN (
                        'EMIT_FIRST_MATCH', 'EMIT_THRESHOLD_CROSSING',
                        'SUPPRESS_FALSE', 'SUPPRESS_STILL_MATCHED',
                        'SUPPRESS_REARMED', 'DUPLICATE_OBSERVATION'
                    )
                ),
                update_id TEXT REFERENCES tracking_updates(update_id),
                distribution_id TEXT
                    REFERENCES update_distributions(distribution_id),
                failure_code TEXT CHECK(
                    failure_code IS NULL OR failure_code IN (
                        'INVALID_OBSERVATION', 'STALE_OBSERVATION',
                        'OUT_OF_ORDER_OBSERVATION', 'OBSERVATION_CONFLICT',
                        'PROVIDER_TIMEOUT', 'PROVIDER_ERROR',
                        'EVIDENCE_PERSIST_FAILED'
                    )
                ),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(subscription_id, execution_policy_version,
                       scheduled_due_at, cycle_kind),
                FOREIGN KEY(definition_id, definition_version)
                    REFERENCES tracking_definitions(definition_id, definition_version)
            )
            """,
            """
            CREATE INDEX condition_cycles_ready
            ON condition_observation_cycles(status, scheduled_due_at, cycle_id)
            """,
            """
            CREATE TABLE condition_temporal_states (
                subscription_id TEXT PRIMARY KEY
                    REFERENCES subscription_aggregates(subscription_id),
                definition_id TEXT NOT NULL,
                definition_version INTEGER NOT NULL CHECK(definition_version >= 1),
                execution_policy_version INTEGER NOT NULL
                    CHECK(execution_policy_version >= 1),
                lifecycle_status TEXT NOT NULL CHECK(lifecycle_status IN (
                    'ACTIVE', 'PAUSED', 'COMPLETED'
                )),
                cadence_seconds INTEGER NOT NULL CHECK(cadence_seconds IN (
                    3600, 21600, 43200, 86400
                )),
                cadence_provenance TEXT NOT NULL CHECK(cadence_provenance IN (
                    'USER_EXPLICIT', 'USER_CONFIRMED', 'PRODUCT_DEFAULT'
                )),
                timezone_name TEXT NOT NULL CHECK(timezone_name='Asia/Shanghai'),
                schedule_anchor_at TEXT NOT NULL,
                window_start_at TEXT NOT NULL,
                window_end_exclusive TEXT NOT NULL,
                next_due_at TEXT,
                last_attempted_cycle_id TEXT,
                last_attempted_at TEXT,
                last_successful_cycle_id TEXT,
                last_successful_cycle_at TEXT,
                last_failure_code TEXT,
                last_failure_at TEXT,
                last_observation_id TEXT,
                last_evaluation_id TEXT,
                last_observed_at TEXT,
                previous_truth TEXT NOT NULL CHECK(previous_truth IN (
                    'UNKNOWN', 'FALSE', 'TRUE'
                )),
                armed INTEGER NOT NULL CHECK(armed IN (0, 1)),
                last_emitted_evaluation_id TEXT,
                last_emitted_update_id TEXT,
                last_emitted_at TEXT,
                paused_at TEXT,
                completed_at TEXT,
                completion_reason TEXT CHECK(
                    completion_reason IS NULL OR
                    completion_reason='TIME_WINDOW_ENDED'
                ),
                version INTEGER NOT NULL CHECK(version >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(definition_id, definition_version)
                    REFERENCES tracking_definitions(definition_id, definition_version)
            )
            """,
            """
            CREATE INDEX condition_temporal_due
            ON condition_temporal_states(lifecycle_status, next_due_at,
                                         subscription_id)
            """,
        )
        for index, statement in enumerate(statements):
            connection.execute(statement)
            if fault_injector is not None:
                fault_injector(f"after_condition_temporal_{index + 1}")

    @classmethod
    def _apply_v16(cls, connection, fault_injector=None):
        """Atomically bind the existing Delivery model to Distribution."""
        connection.commit()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cls._migrate_v16(connection, fault_injector=fault_injector)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) "
                "VALUES (16, datetime('now'))"
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _migrate_v16(connection, fault_injector=None):
        connection.execute("""
            CREATE TABLE delivery_records_v16 (
                delivery_id TEXT PRIMARY KEY,
                digest_id TEXT REFERENCES digests(digest_id),
                distribution_id TEXT REFERENCES update_distributions(distribution_id),
                user_id TEXT NOT NULL,
                channel TEXT NOT NULL CHECK(
                    channel IN ('fake', 'termux_notification')
                ),
                status TEXT NOT NULL CHECK(
                    status IN ('pending', 'accepted', 'failed', 'unknown')
                ),
                current_attempt_number INTEGER NOT NULL,
                current_attempt_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK(
                    (digest_id IS NOT NULL AND distribution_id IS NULL) OR
                    (digest_id IS NULL AND distribution_id IS NOT NULL)
                ),
                UNIQUE(digest_id, channel),
                UNIQUE(distribution_id, channel)
            )
        """)
        connection.execute("""
            CREATE TABLE delivery_attempts_v16 (
                attempt_id TEXT PRIMARY KEY,
                delivery_id TEXT NOT NULL
                    REFERENCES delivery_records_v16(delivery_id),
                attempt_number INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(
                    status IN ('pending', 'accepted', 'failed', 'unknown')
                ),
                provider_message_id TEXT,
                requested_at TEXT NOT NULL,
                completed_at TEXT,
                error_code TEXT,
                effect_certainty TEXT NOT NULL CHECK(
                    effect_certainty IN ('not_started', 'known_applied', 'unknown')
                ),
                evidence_id TEXT,
                UNIQUE(delivery_id, attempt_number)
            )
        """)
        if fault_injector is not None:
            fault_injector("after_delivery_v16_tables")
        connection.execute("""
            INSERT INTO delivery_records_v16(
                delivery_id, digest_id, distribution_id, user_id, channel,
                status, current_attempt_number, current_attempt_id,
                created_at, updated_at
            )
            SELECT delivery_id, digest_id, NULL, user_id, channel, status,
                   current_attempt_number, current_attempt_id,
                   created_at, updated_at
            FROM delivery_records
        """)
        connection.execute("""
            INSERT INTO delivery_attempts_v16(
                attempt_id, delivery_id, attempt_number, status,
                provider_message_id, requested_at, completed_at, error_code,
                effect_certainty, evidence_id
            )
            SELECT attempt_id, delivery_id, attempt_number, status,
                   provider_message_id, requested_at, completed_at,
                   error_code, effect_certainty, NULL
            FROM delivery_attempts
        """)
        if fault_injector is not None:
            fault_injector("after_delivery_v16_copy")
        connection.execute("DROP TABLE delivery_attempts")
        connection.execute("DROP TABLE delivery_records")
        connection.execute(
            "ALTER TABLE delivery_records_v16 RENAME TO delivery_records",
        )
        connection.execute(
            "ALTER TABLE delivery_attempts_v16 RENAME TO delivery_attempts",
        )
        connection.execute("""
            CREATE INDEX delivery_records_distribution
            ON delivery_records(distribution_id, channel)
        """)
        if fault_injector is not None:
            fault_injector("after_delivery_v16_swap")

    @classmethod
    def _apply_v17(cls, connection, fault_injector=None):
        """Atomically add the narrow P4.6 verified EVENT boundary."""
        connection.commit()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cls._migrate_v17(connection, fault_injector=fault_injector)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) "
                "VALUES (17, datetime('now'))"
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _migrate_v17(connection, fault_injector=None):
        statements = (
            """
            CREATE TABLE event_tracking_definitions (
                definition_id TEXT NOT NULL,
                definition_version INTEGER NOT NULL CHECK(definition_version >= 1),
                subscription_id TEXT NOT NULL UNIQUE
                    REFERENCES subscription_aggregates(subscription_id),
                workflow_kind TEXT NOT NULL CHECK(workflow_kind='EVENT'),
                snapshot_json TEXT NOT NULL,
                snapshot_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(definition_id, definition_version),
                FOREIGN KEY(definition_id, definition_version)
                    REFERENCES subscription_definitions(definition_id, definition_version)
            )
            """,
            """
            CREATE TABLE event_tracking_policy_snapshots (
                subscription_id TEXT NOT NULL
                    REFERENCES subscription_aggregates(subscription_id),
                definition_id TEXT NOT NULL,
                definition_version INTEGER NOT NULL CHECK(definition_version >= 1),
                execution_json TEXT NOT NULL,
                presentation_json TEXT NOT NULL,
                distribution_json TEXT NOT NULL,
                snapshot_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(subscription_id, definition_id, definition_version),
                FOREIGN KEY(definition_id, definition_version)
                    REFERENCES event_tracking_definitions(
                        definition_id, definition_version
                    )
            )
            """,
            """
            CREATE TABLE event_source_observations (
                observation_id TEXT PRIMARY KEY,
                entity_key TEXT NOT NULL CHECK(entity_key='openai'),
                window_start_at TEXT NOT NULL,
                window_end_at TEXT NOT NULL,
                retrieved_at TEXT NOT NULL,
                coverage_complete INTEGER NOT NULL CHECK(coverage_complete IN (0,1)),
                truncated INTEGER NOT NULL CHECK(truncated IN (0,1)),
                provider TEXT NOT NULL CHECK(provider='fake_event_search'),
                results_json TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE event_candidates (
                candidate_id TEXT PRIMARY KEY,
                observation_id TEXT NOT NULL
                    REFERENCES event_source_observations(observation_id),
                harness_run_id TEXT NOT NULL,
                entity_key TEXT NOT NULL,
                event_type TEXT NOT NULL,
                object_type TEXT NOT NULL,
                display_name TEXT NOT NULL,
                canonical_name_candidate TEXT NOT NULL,
                occurred_at_candidate TEXT,
                support_json TEXT NOT NULL,
                UNIQUE(observation_id, harness_run_id)
            )
            """,
            """
            CREATE TABLE event_verifications (
                verification_id TEXT PRIMARY KEY,
                subscription_id TEXT NOT NULL
                    REFERENCES subscription_aggregates(subscription_id),
                definition_id TEXT NOT NULL,
                definition_version INTEGER NOT NULL,
                observation_id TEXT NOT NULL
                    REFERENCES event_source_observations(observation_id),
                observation_evidence_id TEXT NOT NULL,
                candidate_id TEXT REFERENCES event_candidates(candidate_id),
                outcome TEXT NOT NULL CHECK(outcome IN (
                    'VERIFIED','NO_UPDATE','VERIFICATION_INCOMPLETE'
                )),
                reason_code TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                logical_event_identity TEXT,
                canonical_model_key TEXT,
                verification_evidence_id TEXT NOT NULL UNIQUE,
                verified_at TEXT NOT NULL,
                UNIQUE(subscription_id, definition_id, definition_version,
                       observation_id, candidate_id)
            )
            """,
            """
            CREATE TABLE verified_events (
                event_id TEXT PRIMARY KEY,
                logical_event_identity TEXT NOT NULL UNIQUE,
                entity_key TEXT NOT NULL CHECK(entity_key='openai'),
                event_type TEXT NOT NULL CHECK(event_type='MODEL_RELEASED'),
                object_type TEXT NOT NULL CHECK(object_type='MODEL'),
                canonical_model_key TEXT NOT NULL,
                display_name TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                verification_id TEXT NOT NULL UNIQUE
                    REFERENCES event_verifications(verification_id),
                verification_evidence_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE event_temporal_states (
                subscription_id TEXT PRIMARY KEY
                    REFERENCES subscription_aggregates(subscription_id),
                definition_id TEXT NOT NULL,
                definition_version INTEGER NOT NULL,
                execution_policy_version INTEGER NOT NULL,
                lifecycle_status TEXT NOT NULL CHECK(lifecycle_status IN ('ACTIVE','PAUSED')),
                cadence_seconds INTEGER NOT NULL CHECK(cadence_seconds IN (3600,21600,43200,86400)),
                cadence_provenance TEXT NOT NULL,
                timezone_name TEXT NOT NULL CHECK(timezone_name='Asia/Shanghai'),
                schedule_anchor_at TEXT NOT NULL,
                activation_at TEXT NOT NULL,
                next_due_at TEXT,
                verified_through TEXT,
                last_attempted_cycle_id TEXT,
                last_attempted_at TEXT,
                last_successful_cycle_id TEXT,
                last_successful_cycle_at TEXT,
                last_failure_code TEXT,
                last_failure_at TEXT,
                last_verification_id TEXT,
                last_update_id TEXT,
                paused_at TEXT,
                version INTEGER NOT NULL CHECK(version >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(definition_id, definition_version)
                    REFERENCES event_tracking_definitions(
                        definition_id, definition_version
                    )
            )
            """,
            """
            CREATE TABLE event_observation_cycles (
                cycle_id TEXT PRIMARY KEY,
                subscription_id TEXT NOT NULL
                    REFERENCES subscription_aggregates(subscription_id),
                definition_id TEXT NOT NULL,
                definition_version INTEGER NOT NULL,
                execution_policy_version INTEGER NOT NULL,
                cycle_kind TEXT NOT NULL CHECK(cycle_kind IN ('INITIAL','SCHEDULED','CATCH_UP','RESUME')),
                scheduled_due_at TEXT NOT NULL,
                coalesced_from_at TEXT NOT NULL,
                coalesced_to_at TEXT NOT NULL,
                coalesced_count INTEGER NOT NULL CHECK(coalesced_count >= 1),
                window_start_at TEXT NOT NULL,
                window_end_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('PENDING','STARTED','SUCCEEDED','INCOMPLETE','FAILED','SUPERSEDED')),
                harness_run_id TEXT NOT NULL UNIQUE,
                claim_token TEXT,
                claimed_at TEXT,
                observation_id TEXT REFERENCES event_source_observations(observation_id),
                candidate_id TEXT REFERENCES event_candidates(candidate_id),
                verification_id TEXT REFERENCES event_verifications(verification_id),
                outcome TEXT,
                reason_code TEXT,
                event_id TEXT REFERENCES verified_events(event_id),
                update_id TEXT,
                distribution_id TEXT,
                failure_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(subscription_id, execution_policy_version,
                       scheduled_due_at, cycle_kind)
            )
            """,
            """
            CREATE INDEX event_cycles_ready
            ON event_observation_cycles(status, scheduled_due_at, cycle_id)
            """,
            """
            CREATE INDEX event_temporal_due
            ON event_temporal_states(lifecycle_status, next_due_at, subscription_id)
            """,
            """
            CREATE TABLE event_subscription_activations (
                activation_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
                definition_outcome_id TEXT NOT NULL UNIQUE
                    REFERENCES definition_outcomes(outcome_id),
                definition_id TEXT NOT NULL,
                subscription_id TEXT NOT NULL UNIQUE
                    REFERENCES subscription_aggregates(subscription_id),
                user_subscription_id TEXT NOT NULL UNIQUE
                    REFERENCES user_subscriptions(user_subscription_id),
                initial_cycle_id TEXT NOT NULL UNIQUE
                    REFERENCES event_observation_cycles(cycle_id),
                created_at TEXT NOT NULL
            )
            """,
        )
        for index, statement in enumerate(statements):
            connection.execute(statement)
            if fault_injector is not None:
                fault_injector(f"after_event_table_{index + 1}")

        # Keep the shared Update/Distribution/Delivery chain.  EVENT updates
        # bind to a Verified Event; CONDITION rows retain their exact shape.
        connection.execute("""
            CREATE TABLE tracking_updates_v17 (
                update_id TEXT PRIMARY KEY,
                subscription_id TEXT NOT NULL
                    REFERENCES subscription_aggregates(subscription_id),
                definition_id TEXT NOT NULL,
                definition_version INTEGER NOT NULL CHECK(definition_version >= 1),
                evaluation_id TEXT NOT NULL UNIQUE,
                evidence_id TEXT NOT NULL,
                update_type TEXT NOT NULL CHECK(update_type IN ('CONDITION','EVENT')),
                payload_json TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                verified_event_id TEXT UNIQUE REFERENCES verified_events(event_id),
                CHECK(
                    (update_type='CONDITION' AND verified_event_id IS NULL) OR
                    (update_type='EVENT' AND verified_event_id IS NOT NULL)
                )
            )
        """)
        connection.execute("""
            INSERT INTO tracking_updates_v17(
                update_id, subscription_id, definition_id,
                definition_version, evaluation_id, evidence_id,
                update_type, payload_json, occurred_at, created_at,
                verified_event_id
            )
            SELECT update_id, subscription_id, definition_id,
                   definition_version, evaluation_id, evidence_id,
                   update_type, payload_json, occurred_at, created_at, NULL
            FROM tracking_updates
        """)
        connection.execute("PRAGMA legacy_alter_table=ON")
        connection.execute(
            "ALTER TABLE tracking_updates RENAME TO tracking_updates_v14"
        )
        connection.execute(
            "ALTER TABLE tracking_updates_v17 RENAME TO tracking_updates"
        )
        connection.execute("""
            CREATE TABLE update_distributions_v17 (
                distribution_id TEXT PRIMARY KEY,
                update_id TEXT NOT NULL REFERENCES tracking_updates(update_id),
                user_subscription_id TEXT NOT NULL
                    REFERENCES user_subscriptions(user_subscription_id),
                status TEXT NOT NULL CHECK(status='AVAILABLE'),
                created_at TEXT NOT NULL,
                UNIQUE(update_id, user_subscription_id)
            )
        """)
        connection.execute("""
            INSERT INTO update_distributions_v17
            SELECT * FROM update_distributions
        """)
        connection.execute("""
            CREATE TABLE condition_observation_cycles_v17 (
                cycle_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE
                    REFERENCES condition_observation_requests(request_id),
                subscription_id TEXT NOT NULL
                    REFERENCES subscription_aggregates(subscription_id),
                definition_id TEXT NOT NULL,
                definition_version INTEGER NOT NULL CHECK(definition_version >= 1),
                execution_policy_version INTEGER NOT NULL CHECK(execution_policy_version >= 1),
                cycle_kind TEXT NOT NULL CHECK(cycle_kind IN ('INITIAL','SCHEDULED','CATCH_UP','RESUME','MANUAL')),
                scheduled_due_at TEXT NOT NULL,
                coalesced_from_at TEXT NOT NULL,
                coalesced_to_at TEXT NOT NULL,
                coalesced_count INTEGER NOT NULL CHECK(coalesced_count >= 1),
                status TEXT NOT NULL CHECK(status IN ('PENDING','STARTED','SUCCEEDED','FAILED','SUPERSEDED')),
                claim_token TEXT,
                claimed_at TEXT,
                observation_id TEXT REFERENCES flight_price_observations(observation_id),
                evaluation_id TEXT REFERENCES condition_evaluations(evaluation_id),
                predicate_truth TEXT CHECK(predicate_truth IS NULL OR predicate_truth IN ('FALSE','TRUE')),
                emission_decision TEXT CHECK(emission_decision IS NULL OR emission_decision IN (
                    'EMIT_FIRST_MATCH','EMIT_THRESHOLD_CROSSING','SUPPRESS_FALSE',
                    'SUPPRESS_STILL_MATCHED','SUPPRESS_REARMED','DUPLICATE_OBSERVATION'
                )),
                update_id TEXT REFERENCES tracking_updates(update_id),
                distribution_id TEXT REFERENCES update_distributions_v17(distribution_id),
                failure_code TEXT CHECK(failure_code IS NULL OR failure_code IN (
                    'INVALID_OBSERVATION','STALE_OBSERVATION','OUT_OF_ORDER_OBSERVATION',
                    'OBSERVATION_CONFLICT','PROVIDER_TIMEOUT','PROVIDER_ERROR',
                    'EVIDENCE_PERSIST_FAILED'
                )),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(subscription_id, execution_policy_version,
                       scheduled_due_at, cycle_kind),
                FOREIGN KEY(definition_id, definition_version)
                    REFERENCES tracking_definitions(definition_id, definition_version)
            )
        """)
        connection.execute("""
            INSERT INTO condition_observation_cycles_v17
            SELECT * FROM condition_observation_cycles
        """)
        connection.execute("""
            CREATE TABLE delivery_records_v17 (
                delivery_id TEXT PRIMARY KEY,
                digest_id TEXT REFERENCES digests(digest_id),
                distribution_id TEXT
                    REFERENCES update_distributions_v17(distribution_id),
                user_id TEXT NOT NULL,
                channel TEXT NOT NULL CHECK(channel IN ('fake','termux_notification')),
                status TEXT NOT NULL CHECK(status IN ('pending','accepted','failed','unknown')),
                current_attempt_number INTEGER NOT NULL,
                current_attempt_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK((digest_id IS NOT NULL AND distribution_id IS NULL) OR
                      (digest_id IS NULL AND distribution_id IS NOT NULL)),
                UNIQUE(digest_id, channel), UNIQUE(distribution_id, channel)
            )
        """)
        connection.execute("""
            INSERT INTO delivery_records_v17 SELECT * FROM delivery_records
        """)
        connection.execute("""
            CREATE TABLE delivery_attempts_v17 (
                attempt_id TEXT PRIMARY KEY,
                delivery_id TEXT NOT NULL
                    REFERENCES delivery_records_v17(delivery_id),
                attempt_number INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','accepted','failed','unknown')),
                provider_message_id TEXT,
                requested_at TEXT NOT NULL,
                completed_at TEXT,
                error_code TEXT,
                effect_certainty TEXT NOT NULL CHECK(effect_certainty IN ('not_started','known_applied','unknown')),
                evidence_id TEXT,
                UNIQUE(delivery_id, attempt_number)
            )
        """)
        connection.execute("""
            INSERT INTO delivery_attempts_v17 SELECT * FROM delivery_attempts
        """)
        connection.execute("DROP TABLE delivery_attempts")
        connection.execute("DROP TABLE delivery_records")
        connection.execute("DROP TABLE condition_observation_cycles")
        connection.execute("DROP TABLE update_distributions")
        connection.execute("DROP TABLE tracking_updates_v14")
        connection.execute(
            "ALTER TABLE update_distributions_v17 RENAME TO update_distributions"
        )
        connection.execute(
            "ALTER TABLE condition_observation_cycles_v17 "
            "RENAME TO condition_observation_cycles"
        )
        connection.execute(
            "ALTER TABLE delivery_records_v17 RENAME TO delivery_records"
        )
        connection.execute(
            "ALTER TABLE delivery_attempts_v17 RENAME TO delivery_attempts"
        )
        connection.execute("""
            CREATE INDEX delivery_records_distribution
            ON delivery_records(distribution_id, channel)
        """)
        connection.execute("""
            CREATE INDEX condition_cycles_ready
            ON condition_observation_cycles(status, scheduled_due_at, cycle_id)
        """)
        connection.execute("PRAGMA legacy_alter_table=OFF")
        if fault_injector is not None:
            fault_injector("after_event_update_swap")

    @staticmethod
    def _conversation_from(row):
        if row is None:
            return None
        return Conversation(
            row["conversation_id"], row["user_id"], row["status"],
            row["turn_count"], row["created_at"], row["updated_at"],
            row["version"], row["start_idempotency_key"],
            row["terminal_reason"],
        )

    @staticmethod
    def _conversation_turn_from(row):
        if row is None:
            return None
        return ConversationTurn(
            row["turn_id"], row["conversation_id"], row["turn_number"],
            row["role"], row["safe_text"], row["message_idempotency_key"],
            row["harness_run_id"], row["status"], row["outcome_id"],
            row["error_code"], row["claim_owner_id"], row["created_at"],
            row["updated_at"], row["failure_stage"], row["failure_subtype"],
        )

    @staticmethod
    def _definition_outcome_from(row):
        if row is None:
            return None
        return DefinitionOutcome(
            row["outcome_id"], row["conversation_id"], row["turn_id"],
            row["outcome_type"], json.loads(row["payload_json"]),
            row["candidate_identity"], row["created_at"],
        )

    @staticmethod
    def _insert_conversation_turn(connection, turn):
        connection.execute("""
            INSERT INTO conversation_turns(
                turn_id, conversation_id, turn_number, role, safe_text,
                message_idempotency_key, harness_run_id, status, outcome_id,
                error_code, claim_owner_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            turn.turn_id, turn.conversation_id, turn.turn_number, turn.role,
            turn.safe_text, turn.message_idempotency_key, turn.harness_run_id,
            turn.status, turn.outcome_id, turn.error_code,
            turn.claim_owner_id, turn.created_at, turn.updated_at,
        ))

    def reserve_conversation(self, conversation, turn):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute("""
                INSERT OR IGNORE INTO conversations(
                    conversation_id, user_id, status, turn_count, version,
                    start_idempotency_key, terminal_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                conversation.conversation_id, conversation.user_id,
                conversation.status, conversation.turn_count,
                conversation.version, conversation.start_idempotency_key,
                conversation.terminal_reason, conversation.created_at,
                conversation.updated_at,
            ))
            created = cursor.rowcount == 1
            if created:
                self._insert_conversation_turn(connection, turn)
            conversation_row = connection.execute("""
                SELECT * FROM conversations
                WHERE user_id=? AND start_idempotency_key=?
            """, (
                conversation.user_id, conversation.start_idempotency_key,
            )).fetchone()
            if conversation_row is None:
                raise ValueError("conversation identity conflict")
            turn_row = connection.execute("""
                SELECT * FROM conversation_turns
                WHERE conversation_id=? AND turn_number=1
            """, (conversation_row["conversation_id"],)).fetchone()
        return (
            self._conversation_from(conversation_row),
            self._conversation_turn_from(turn_row), created,
        )

    def reserve_conversation_turn(self, conversation_id, user_id, turn,
                                  maximum_turns, timestamp):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            conversation_row = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            if conversation_row is None or conversation_row["user_id"] != user_id:
                raise ValueError("conversation not found")
            existing = connection.execute("""
                SELECT * FROM conversation_turns
                WHERE conversation_id=? AND message_idempotency_key=?
            """, (
                conversation_id, turn.message_idempotency_key,
            )).fetchone()
            if existing is not None:
                return (
                    self._conversation_from(conversation_row),
                    self._conversation_turn_from(existing), False,
                )
            if (conversation_row["status"] != "WAITING_FOR_ANSWER"
                    or conversation_row["turn_count"] >= maximum_turns):
                raise ValueError("conversation cannot accept message")
            expected_number = conversation_row["turn_count"] + 1
            if turn.turn_number != expected_number:
                raise ValueError("conversation turn number mismatch")
            self._insert_conversation_turn(connection, turn)
            cursor = connection.execute("""
                UPDATE conversations SET status='COLLECTING', turn_count=?,
                    version=version+1, terminal_reason=NULL, updated_at=?
                WHERE conversation_id=? AND version=?
                  AND status='WAITING_FOR_ANSWER'
            """, (
                expected_number, timestamp, conversation_id,
                conversation_row["version"],
            ))
            if cursor.rowcount != 1:
                raise ValueError("conversation message CAS failed")
            conversation_row = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        return (
            self._conversation_from(conversation_row), turn, True,
        )

    def reserve_conversation_adjustment(self, conversation_id, user_id, turn,
                                        maximum_turns, timestamp):
        """Reserve a revision turn for an uncommitted proposal."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            conversation_row = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            if conversation_row is None or conversation_row["user_id"] != user_id:
                raise ValueError("conversation not found")
            existing = connection.execute("""
                SELECT * FROM conversation_turns
                WHERE conversation_id=? AND message_idempotency_key=?
            """, (
                conversation_id, turn.message_idempotency_key,
            )).fetchone()
            if existing is not None:
                return (
                    self._conversation_from(conversation_row),
                    self._conversation_turn_from(existing), False,
                )
            committed = connection.execute("""
                SELECT 1 FROM subscription_activations
                WHERE conversation_id=? LIMIT 1
            """, (conversation_id,)).fetchone()
            if committed is not None:
                raise ValueError("conversation already committed")
            if (conversation_row["status"] != "DEFINITION_ACCEPTED"
                    or conversation_row["turn_count"] >= maximum_turns):
                raise ValueError("conversation cannot be adjusted")
            expected_number = conversation_row["turn_count"] + 1
            if turn.turn_number != expected_number:
                raise ValueError("conversation turn number mismatch")
            self._insert_conversation_turn(connection, turn)
            cursor = connection.execute("""
                UPDATE conversations SET status='COLLECTING', turn_count=?,
                    version=version+1, terminal_reason=NULL, updated_at=?
                WHERE conversation_id=? AND version=?
                  AND status='DEFINITION_ACCEPTED'
            """, (
                expected_number, timestamp, conversation_id,
                conversation_row["version"],
            ))
            if cursor.rowcount != 1:
                raise ValueError("conversation adjustment CAS failed")
            conversation_row = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        return (
            self._conversation_from(conversation_row), turn, True,
        )

    def claim_conversation_turn(self, turn_id, owner_id, timestamp):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute("""
                UPDATE conversation_turns SET status='running',
                    claim_owner_id=?, updated_at=?
                WHERE turn_id=? AND (
                    status='reserved'
                    OR (status='running' AND claim_owner_id<>?)
                )
            """, (owner_id, timestamp, turn_id, owner_id))
            row = connection.execute(
                "SELECT * FROM conversation_turns WHERE turn_id=?",
                (turn_id,),
            ).fetchone()
        if row is None:
            return None
        return self._conversation_turn_from(row) if cursor.rowcount == 1 else None

    def finish_conversation_turn(self, turn, outcome, conversation_status,
                                 terminal_reason, timestamp):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM definition_outcomes WHERE turn_id=?",
                (turn.turn_id,),
            ).fetchone()
            if existing is not None:
                existing_outcome = self._definition_outcome_from(existing)
                if (existing_outcome.outcome_id != outcome.outcome_id
                        or existing_outcome.candidate_identity
                        != outcome.candidate_identity):
                    raise ValueError("conflicting definition outcome")
            else:
                connection.execute("""
                    INSERT INTO definition_outcomes(
                        outcome_id, conversation_id, turn_id, outcome_type,
                        payload_json, candidate_identity, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    outcome.outcome_id, outcome.conversation_id,
                    outcome.turn_id, outcome.outcome_type,
                    json.dumps(outcome.payload, ensure_ascii=False,
                               sort_keys=True),
                    outcome.candidate_identity, outcome.created_at,
                ))
            cursor = connection.execute("""
                UPDATE conversation_turns SET status='completed', outcome_id=?,
                    error_code=NULL, failure_stage=NULL, failure_subtype=NULL,
                    updated_at=?
                WHERE turn_id=? AND status IN ('reserved', 'running')
            """, (outcome.outcome_id, timestamp, turn.turn_id))
            current_turn = connection.execute(
                "SELECT * FROM conversation_turns WHERE turn_id=?",
                (turn.turn_id,),
            ).fetchone()
            if cursor.rowcount != 1 and current_turn["outcome_id"] != outcome.outcome_id:
                raise ValueError("conversation turn cannot finish")
            connection.execute("""
                UPDATE conversations SET status=?, terminal_reason=?,
                    version=version+1, updated_at=?
                WHERE conversation_id=? AND status='COLLECTING'
            """, (
                conversation_status, terminal_reason, timestamp,
                outcome.conversation_id,
            ))
            conversation_row = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id=?",
                (outcome.conversation_id,),
            ).fetchone()
            current_turn = connection.execute(
                "SELECT * FROM conversation_turns WHERE turn_id=?",
                (turn.turn_id,),
            ).fetchone()
            outcome_row = connection.execute(
                "SELECT * FROM definition_outcomes WHERE turn_id=?",
                (turn.turn_id,),
            ).fetchone()
        return (
            self._conversation_from(conversation_row),
            self._conversation_turn_from(current_turn),
            self._definition_outcome_from(outcome_row),
        )

    def fail_conversation_turn(self, turn_id, error_code,
                               conversation_status, timestamp,
                               failure_stage=None, failure_subtype=None):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM conversation_turns WHERE turn_id=?",
                (turn_id,),
            ).fetchone()
            if row is None:
                raise ValueError("conversation turn not found")
            if row["status"] in {"reserved", "running"}:
                connection.execute("""
                    UPDATE conversation_turns SET status='failed', error_code=?,
                        failure_stage=?, failure_subtype=?, updated_at=?
                    WHERE turn_id=?
                """, (
                    error_code, failure_stage, failure_subtype, timestamp,
                    turn_id,
                ))
                terminal_reason = (
                    error_code if conversation_status == "INCOMPLETE" else None
                )
                connection.execute("""
                    UPDATE conversations SET status=?, terminal_reason=?,
                        version=version+1, updated_at=?
                    WHERE conversation_id=? AND status='COLLECTING'
                """, (
                    conversation_status, terminal_reason, timestamp,
                    row["conversation_id"],
                ))
            conversation_row = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id=?",
                (row["conversation_id"],),
            ).fetchone()
            turn_row = connection.execute(
                "SELECT * FROM conversation_turns WHERE turn_id=?",
                (turn_id,),
            ).fetchone()
        return (
            self._conversation_from(conversation_row),
            self._conversation_turn_from(turn_row),
        )

    def get_conversation(self, conversation_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        return self._conversation_from(row)

    def get_conversation_turn(self, turn_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversation_turns WHERE turn_id=?",
                (turn_id,),
            ).fetchone()
        return self._conversation_turn_from(row)

    def get_conversation_turn_by_key(self, conversation_id, idempotency_key):
        with self.connect() as connection:
            row = connection.execute("""
                SELECT * FROM conversation_turns
                WHERE conversation_id=? AND message_idempotency_key=?
            """, (conversation_id, idempotency_key)).fetchone()
        return self._conversation_turn_from(row)

    def get_definition_outcome_for_turn(self, turn_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM definition_outcomes WHERE turn_id=?",
                (turn_id,),
            ).fetchone()
        return self._definition_outcome_from(row)

    def get_definition_outcome(self, outcome_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM definition_outcomes WHERE outcome_id=?",
                (outcome_id,),
            ).fetchone()
        return self._definition_outcome_from(row)

    def list_conversation_turns(self, conversation_id):
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT * FROM conversation_turns WHERE conversation_id=?
                ORDER BY turn_number
            """, (conversation_id,)).fetchall()
        return tuple(self._conversation_turn_from(row) for row in rows)

    def list_definition_outcomes(self, conversation_id):
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT * FROM definition_outcomes WHERE conversation_id=?
                ORDER BY created_at, outcome_id
            """, (conversation_id,)).fetchall()
        return tuple(self._definition_outcome_from(row) for row in rows)

    @staticmethod
    def _definition_attempt_from(row):
        if row is None:
            return None
        return DefinitionAttemptRecord(
            row["attempt_id"], row["turn_id"], row["attempt_number"],
            row["status"], json.loads(row["request_metadata_json"]),
            (json.loads(row["response_metadata_json"])
             if row["response_metadata_json"] else None),
            (json.loads(row["candidate_payload_json"])
             if row["candidate_payload_json"] else None),
            row["failure_stage"], row["failure_subtype"],
            row["started_at"], row["completed_at"],
        )

    def reserve_definition_attempt(self, record):
        request = json.dumps(
            record.request_metadata, ensure_ascii=False, sort_keys=True,
        )
        with self.connect() as connection:
            connection.execute("""
                INSERT OR IGNORE INTO definition_attempts(
                    attempt_id, turn_id, attempt_number, status,
                    request_metadata_json, started_at
                ) VALUES (?, ?, ?, 'started', ?, ?)
            """, (
                record.attempt_id, record.turn_id, record.attempt_number,
                request, record.started_at,
            ))
            row = connection.execute(
                "SELECT * FROM definition_attempts WHERE attempt_id=?",
                (record.attempt_id,),
            ).fetchone()
        current = self._definition_attempt_from(row)
        if (current is None or current.turn_id != record.turn_id
                or current.attempt_number != record.attempt_number
                or current.request_metadata != record.request_metadata):
            raise ValueError("definition attempt identity conflict")
        return current

    def finish_definition_attempt(self, record):
        response = json.dumps(
            record.response_metadata, ensure_ascii=False, sort_keys=True,
        ) if record.response_metadata is not None else None
        candidate = json.dumps(
            record.candidate_payload, ensure_ascii=False, sort_keys=True,
        ) if record.candidate_payload is not None else None
        with self.connect() as connection:
            connection.execute("""
                UPDATE definition_attempts SET status=?,
                    response_metadata_json=?, candidate_payload_json=?,
                    failure_stage=?, failure_subtype=?, completed_at=?
                WHERE attempt_id=? AND status='started'
            """, (
                record.status, response, candidate, record.failure_stage,
                record.failure_subtype, record.completed_at,
                record.attempt_id,
            ))
            row = connection.execute(
                "SELECT * FROM definition_attempts WHERE attempt_id=?",
                (record.attempt_id,),
            ).fetchone()
        current = self._definition_attempt_from(row)
        if current is None or current.status != record.status:
            raise ValueError("definition attempt cannot finish")
        return current

    def list_definition_attempts(self, turn_id):
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT * FROM definition_attempts
                WHERE turn_id=? ORDER BY attempt_number
            """, (turn_id,)).fetchall()
        return tuple(self._definition_attempt_from(row) for row in rows)

    @staticmethod
    def _subscription_definition_from(row):
        if row is None:
            return None
        return SubscriptionDefinition(
            row["definition_id"], row["definition_version"],
            row["conversation_id"], row["definition_outcome_id"],
            json.loads(row["snapshot_json"]), row["snapshot_identity"],
            row["created_at"],
        )

    @staticmethod
    def _product_subscription_from(row):
        if row is None:
            return None
        return ProductSubscription(
            row["subscription_id"], row["definition_id"],
            row["definition_version"], row["status"], row["created_at"],
            row["updated_at"],
            (row["workflow_kind"]
             if "workflow_kind" in row.keys() else "BRIEFING"),
        )

    @staticmethod
    def _tracking_definition_from(row):
        if row is None:
            return None
        return TrackingDefinition(
            row["definition_id"], row["definition_version"],
            row["subscription_id"], row["workflow_kind"],
            json.loads(row["snapshot_json"]), row["snapshot_identity"],
            row["created_at"],
        )

    @staticmethod
    def _tracking_policy_from(row):
        if row is None:
            return None
        return TrackingPolicySnapshot(
            row["subscription_id"], row["definition_id"],
            row["definition_version"], json.loads(row["execution_json"]),
            json.loads(row["presentation_json"]),
            json.loads(row["distribution_json"]), row["snapshot_identity"],
            row["created_at"],
        )

    @staticmethod
    def _condition_request_from(row):
        if row is None:
            return None
        return ConditionObservationRequest(
            row["request_id"], row["subscription_id"], row["definition_id"],
            row["definition_version"], row["idempotency_key"], row["status"],
            row["evaluation_id"], row["failure_code"], row["created_at"],
            row["updated_at"],
        )

    @staticmethod
    def _condition_cycle_from(row):
        if row is None:
            return None
        return ConditionObservationCycle(
            row["cycle_id"], row["request_id"], row["subscription_id"],
            row["definition_id"], row["definition_version"],
            row["execution_policy_version"], row["cycle_kind"],
            row["scheduled_due_at"], row["coalesced_from_at"],
            row["coalesced_to_at"], row["coalesced_count"], row["status"],
            row["claim_token"], row["claimed_at"], row["observation_id"],
            row["evaluation_id"], row["predicate_truth"],
            row["emission_decision"], row["update_id"],
            row["distribution_id"], row["failure_code"], row["created_at"],
            row["updated_at"],
        )

    @staticmethod
    def _condition_temporal_from(row):
        if row is None:
            return None
        return ConditionTemporalState(
            row["subscription_id"], row["definition_id"],
            row["definition_version"], row["execution_policy_version"],
            row["lifecycle_status"], row["cadence_seconds"],
            row["cadence_provenance"], row["timezone_name"],
            row["schedule_anchor_at"], row["window_start_at"],
            row["window_end_exclusive"], row["next_due_at"],
            row["last_attempted_cycle_id"], row["last_attempted_at"],
            row["last_successful_cycle_id"],
            row["last_successful_cycle_at"], row["last_failure_code"],
            row["last_failure_at"], row["last_observation_id"],
            row["last_evaluation_id"], row["last_observed_at"],
            row["previous_truth"], bool(row["armed"]),
            row["last_emitted_evaluation_id"],
            row["last_emitted_update_id"], row["last_emitted_at"],
            row["paused_at"], row["completed_at"],
            row["completion_reason"], row["version"], row["created_at"],
            row["updated_at"],
        )

    @staticmethod
    def _event_source_observation_from(row):
        if row is None:
            return None
        results = tuple(
            EventSourceResult(**item) for item in json.loads(row["results_json"])
        )
        return EventSourceObservation(
            row["observation_id"], row["entity_key"],
            row["window_start_at"], row["window_end_at"],
            row["retrieved_at"], bool(row["coverage_complete"]),
            bool(row["truncated"]), results, row["provider"],
        )

    @staticmethod
    def _event_candidate_from(row):
        if row is None:
            return None
        support = tuple(
            EventCandidateSupport(**item)
            for item in json.loads(row["support_json"])
        )
        return EventCandidate(
            row["candidate_id"], row["observation_id"],
            row["harness_run_id"], row["entity_key"], row["event_type"],
            row["object_type"], row["display_name"],
            row["canonical_name_candidate"], row["occurred_at_candidate"],
            support,
        )

    @staticmethod
    def _event_verification_from(row):
        if row is None:
            return None
        return EventVerification(
            row["verification_id"], row["subscription_id"],
            row["definition_id"], row["definition_version"],
            row["observation_id"], row["observation_evidence_id"],
            row["candidate_id"], row["outcome"], row["reason_code"],
            row["policy_version"], row["logical_event_identity"],
            row["canonical_model_key"], row["verification_evidence_id"],
            row["verified_at"],
        )

    @staticmethod
    def _verified_event_from(row):
        if row is None:
            return None
        return VerifiedEvent(
            row["event_id"], row["logical_event_identity"],
            row["entity_key"], row["event_type"], row["object_type"],
            row["canonical_model_key"], row["display_name"],
            row["occurred_at"], row["verification_id"],
            row["verification_evidence_id"], row["created_at"],
        )

    @staticmethod
    def _event_temporal_from(row):
        if row is None:
            return None
        return EventTemporalState(
            row["subscription_id"], row["definition_id"],
            row["definition_version"], row["execution_policy_version"],
            row["lifecycle_status"], row["cadence_seconds"],
            row["cadence_provenance"], row["timezone_name"],
            row["schedule_anchor_at"], row["activation_at"],
            row["next_due_at"], row["verified_through"],
            row["last_attempted_cycle_id"], row["last_attempted_at"],
            row["last_successful_cycle_id"], row["last_successful_cycle_at"],
            row["last_failure_code"], row["last_failure_at"],
            row["last_verification_id"], row["last_update_id"],
            row["paused_at"], row["version"], row["created_at"],
            row["updated_at"],
        )

    @staticmethod
    def _event_cycle_from(row):
        if row is None:
            return None
        return EventObservationCycle(
            row["cycle_id"], row["subscription_id"], row["definition_id"],
            row["definition_version"], row["execution_policy_version"],
            row["cycle_kind"], row["scheduled_due_at"],
            row["coalesced_from_at"], row["coalesced_to_at"],
            row["coalesced_count"], row["window_start_at"],
            row["window_end_at"], row["status"], row["harness_run_id"],
            row["claim_token"], row["claimed_at"], row["observation_id"],
            row["candidate_id"], row["verification_id"], row["outcome"],
            row["reason_code"], row["event_id"], row["update_id"],
            row["distribution_id"], row["failure_code"], row["created_at"],
            row["updated_at"],
        )

    @staticmethod
    def _flight_observation_from(row):
        if row is None:
            return None
        quote = FlightPriceQuote(
            row["source_signal_id"], row["origin"], row["destination"],
            row["trip_type"], row["travel_month"], row["metric"],
            row["price"], row["currency"], row["observed_at"],
        )
        return AcceptedFlightPriceObservation(
            row["observation_id"], row["subscription_id"], quote,
            row["evidence_id"], row["signal_identity"], row["accepted_at"],
        )

    @staticmethod
    def _condition_evaluation_from(row):
        if row is None:
            return None
        return ConditionEvaluation(
            row["evaluation_id"], row["subscription_id"],
            row["definition_id"], row["definition_version"],
            row["observation_id"], row["evidence_id"],
            row["observed_price"], row["threshold"], row["currency"],
            row["operator"], row["result"], row["evaluator_version"],
            row["evaluated_at"],
        )

    @staticmethod
    def _tracking_update_from(row):
        if row is None:
            return None
        return TrackingUpdate(
            row["update_id"], row["subscription_id"], row["definition_id"],
            row["definition_version"], row["evaluation_id"],
            row["evidence_id"], row["update_type"],
            json.loads(row["payload_json"]), row["occurred_at"],
            row["created_at"],
            (row["verified_event_id"]
             if "verified_event_id" in row.keys() else None),
        )

    @staticmethod
    def _distribution_from(row):
        if row is None:
            return None
        return UpdateDistribution(
            row["distribution_id"], row["update_id"],
            row["user_subscription_id"], row["status"], row["created_at"],
        )

    @staticmethod
    def _condition_activation_from(row):
        if row is None:
            return None
        return ConditionSubscriptionActivation(
            row["activation_id"], row["conversation_id"],
            row["definition_outcome_id"], row["definition_id"],
            row["subscription_id"], row["user_subscription_id"],
            row["condition_request_id"], row["created_at"],
        )

    @staticmethod
    def _event_activation_from(row):
        if row is None:
            return None
        return EventSubscriptionActivation(
            row["activation_id"], row["conversation_id"],
            row["definition_outcome_id"], row["definition_id"],
            row["subscription_id"], row["user_subscription_id"],
            row["initial_cycle_id"], row["created_at"],
        )

    @staticmethod
    def _user_subscription_from(row):
        if row is None:
            return None
        return UserSubscription(
            row["user_subscription_id"], row["user_id"],
            row["subscription_id"], row["status"], row["created_at"],
            row["updated_at"],
        )

    @staticmethod
    def _briefing_reservation_from(row):
        if row is None:
            return None
        return BriefingReservation(
            row["application_run_id"], row["subscription_id"],
            row["definition_id"], row["definition_version"], row["status"],
            row["harness_run_id"], row["created_at"], row["updated_at"],
        )

    @staticmethod
    def _application_outbox_from(row):
        if row is None:
            return None
        return ApplicationOutbox(
            row["outbox_id"], row["event_type"], row["subscription_id"],
            row["application_run_id"], json.loads(row["payload_ref_json"]),
            row["payload_identity"], row["status"], row["attempt_number"],
            row["created_at"], row["available_at"], row["last_error_code"],
            row["version"], row["updated_at"],
        )

    @staticmethod
    def _relation_event_from(row):
        if row is None:
            return None
        return RelationEventOutbox(
            row["event_id"], row["event_type"],
            row["user_subscription_id"], row["user_id"],
            row["subscription_id"], row["relation_version"],
            row["relation_identity"], json.loads(row["payload_json"]),
            row["payload_identity"], row["status"], row["attempt_number"],
            row["created_at"], row["available_at"], row["last_error_code"],
            row["version"], row["updated_at"],
        )

    @staticmethod
    def _relation_event_attempt_from(row):
        if row is None:
            return None
        return RelationEventAttempt(
            row["attempt_id"], row["event_id"], row["attempt_number"],
            row["status"], row["effect_certainty"], row["requested_at"],
            row["completed_at"], row["error_code"],
        )

    @staticmethod
    def _subscription_activation_from(row):
        if row is None:
            return None
        return SubscriptionActivation(
            row["activation_id"], row["conversation_id"],
            row["definition_outcome_id"], row["definition_id"],
            row["subscription_id"], row["user_subscription_id"],
            row["application_run_id"], row["outbox_id"], row["created_at"],
        )

    def _commit_from_connection(self, connection, outcome_id, reused):
        activation_row = connection.execute(
            "SELECT * FROM subscription_activations WHERE definition_outcome_id=?",
            (outcome_id,),
        ).fetchone()
        if activation_row is None:
            return None
        activation = self._subscription_activation_from(activation_row)
        definition_row = connection.execute("""
            SELECT * FROM subscription_definitions
            WHERE definition_id=? AND definition_outcome_id=?
        """, (activation.definition_id, outcome_id)).fetchone()
        subscription_row = connection.execute(
            "SELECT payload_json FROM subscriptions WHERE subscription_id=?",
            (activation.subscription_id,),
        ).fetchone()
        product_row = connection.execute(
            "SELECT * FROM subscription_aggregates WHERE subscription_id=?",
            (activation.subscription_id,),
        ).fetchone()
        relation_row = connection.execute(
            "SELECT * FROM user_subscriptions WHERE user_subscription_id=?",
            (activation.user_subscription_id,),
        ).fetchone()
        relation_event_row = connection.execute(
            "SELECT * FROM relation_event_outbox WHERE user_subscription_id=?",
            (activation.user_subscription_id,),
        ).fetchone()
        briefing_row = connection.execute(
            "SELECT * FROM briefing_reservations WHERE application_run_id=?",
            (activation.application_run_id,),
        ).fetchone()
        outbox_row = connection.execute(
            "SELECT * FROM application_outbox WHERE outbox_id=?",
            (activation.outbox_id,),
        ).fetchone()
        if any(row is None for row in (
                definition_row, subscription_row, product_row, relation_row,
                briefing_row, outbox_row)):
            raise ValueError("incomplete Subscription product commit")
        return SubscriptionCommit(
            self._subscription_definition_from(definition_row),
            self._subscription_from(json.loads(subscription_row[0])),
            self._product_subscription_from(product_row),
            self._user_subscription_from(relation_row),
            self._relation_event_from(relation_event_row),
            self._briefing_reservation_from(briefing_row),
            self._application_outbox_from(outbox_row), activation, reused,
        )

    def _condition_commit_from_connection(self, connection, outcome_id,
                                          reused):
        activation_row = connection.execute("""
            SELECT * FROM condition_subscription_activations
            WHERE definition_outcome_id=?
        """, (outcome_id,)).fetchone()
        if activation_row is None:
            return None
        activation = self._condition_activation_from(activation_row)
        definition_row = connection.execute("""
            SELECT * FROM subscription_definitions
            WHERE definition_id=? AND definition_outcome_id=?
        """, (activation.definition_id, outcome_id)).fetchone()
        subscription_row = connection.execute(
            "SELECT payload_json FROM subscriptions WHERE subscription_id=?",
            (activation.subscription_id,),
        ).fetchone()
        product_row = connection.execute(
            "SELECT * FROM subscription_aggregates WHERE subscription_id=?",
            (activation.subscription_id,),
        ).fetchone()
        relation_row = connection.execute(
            "SELECT * FROM user_subscriptions WHERE user_subscription_id=?",
            (activation.user_subscription_id,),
        ).fetchone()
        relation_event_row = connection.execute(
            "SELECT * FROM relation_event_outbox WHERE user_subscription_id=?",
            (activation.user_subscription_id,),
        ).fetchone()
        tracking_row = connection.execute("""
            SELECT * FROM tracking_definitions
            WHERE definition_id=? AND definition_version=1
        """, (activation.definition_id,)).fetchone()
        policy_row = connection.execute("""
            SELECT * FROM tracking_policy_snapshots
            WHERE subscription_id=? AND definition_id=? AND definition_version=1
        """, (activation.subscription_id, activation.definition_id)).fetchone()
        request_row = connection.execute(
            "SELECT * FROM condition_observation_requests WHERE request_id=?",
            (activation.condition_request_id,),
        ).fetchone()
        temporal_row = connection.execute("""
            SELECT * FROM condition_temporal_states WHERE subscription_id=?
        """, (activation.subscription_id,)).fetchone()
        cycle_row = connection.execute("""
            SELECT * FROM condition_observation_cycles WHERE request_id=?
        """, (activation.condition_request_id,)).fetchone()
        if any(row is None for row in (
                definition_row, subscription_row, product_row, relation_row,
                relation_event_row, tracking_row, policy_row, request_row)):
            raise ValueError("incomplete CONDITION Subscription commit")
        return ConditionSubscriptionCommit(
            self._subscription_definition_from(definition_row),
            self._subscription_from(json.loads(subscription_row[0])),
            self._product_subscription_from(product_row),
            self._user_subscription_from(relation_row),
            self._relation_event_from(relation_event_row),
            self._tracking_definition_from(tracking_row),
            self._tracking_policy_from(policy_row),
            self._condition_request_from(request_row), activation,
            self._condition_temporal_from(temporal_row),
            self._condition_cycle_from(cycle_row), reused,
        )

    def _event_commit_from_connection(self, connection, outcome_id, reused):
        row = connection.execute("""
            SELECT * FROM event_subscription_activations
            WHERE definition_outcome_id=?
        """, (outcome_id,)).fetchone()
        if row is None:
            return None
        activation = self._event_activation_from(row)
        definition_row = connection.execute("""
            SELECT * FROM subscription_definitions
            WHERE definition_id=? AND definition_outcome_id=?
        """, (activation.definition_id, outcome_id)).fetchone()
        subscription_row = connection.execute(
            "SELECT payload_json FROM subscriptions WHERE subscription_id=?",
            (activation.subscription_id,),
        ).fetchone()
        product_row = connection.execute(
            "SELECT * FROM subscription_aggregates WHERE subscription_id=?",
            (activation.subscription_id,),
        ).fetchone()
        relation_row = connection.execute(
            "SELECT * FROM user_subscriptions WHERE user_subscription_id=?",
            (activation.user_subscription_id,),
        ).fetchone()
        relation_event_row = connection.execute(
            "SELECT * FROM relation_event_outbox WHERE user_subscription_id=?",
            (activation.user_subscription_id,),
        ).fetchone()
        tracking_row = connection.execute("""
            SELECT * FROM event_tracking_definitions
            WHERE definition_id=? AND definition_version=1
        """, (activation.definition_id,)).fetchone()
        policy_row = connection.execute("""
            SELECT * FROM event_tracking_policy_snapshots
            WHERE subscription_id=? AND definition_id=? AND definition_version=1
        """, (activation.subscription_id, activation.definition_id)).fetchone()
        temporal_row = connection.execute(
            "SELECT * FROM event_temporal_states WHERE subscription_id=?",
            (activation.subscription_id,),
        ).fetchone()
        cycle_row = connection.execute(
            "SELECT * FROM event_observation_cycles WHERE cycle_id=?",
            (activation.initial_cycle_id,),
        ).fetchone()
        if any(item is None for item in (
                definition_row, subscription_row, product_row, relation_row,
                relation_event_row, tracking_row, policy_row, temporal_row,
                cycle_row)):
            raise ValueError("incomplete EVENT Subscription commit")
        return EventSubscriptionCommit(
            self._subscription_definition_from(definition_row),
            self._subscription_from(json.loads(subscription_row[0])),
            self._product_subscription_from(product_row),
            self._user_subscription_from(relation_row),
            self._relation_event_from(relation_event_row),
            self._tracking_definition_from(tracking_row),
            self._tracking_policy_from(policy_row), activation,
            self._event_temporal_from(temporal_row),
            self._event_cycle_from(cycle_row), reused,
        )

    @staticmethod
    def _validate_subscription_commit(user_id, proposed, conversation,
                                      outcome, first_turn):
        if not isinstance(proposed, SubscriptionCommit):
            raise ValueError("invalid Subscription commit")
        definition = proposed.definition
        subscription = proposed.legacy_subscription
        product = proposed.subscription
        relation = proposed.relation
        relation_event = proposed.relation_event
        briefing = proposed.briefing
        outbox = proposed.outbox
        activation = proposed.activation
        if not isinstance(relation_event, RelationEventOutbox):
            raise ValueError("Relation event binding mismatch")
        new_ids = {
            definition.definition_id, subscription.subscription_id,
            relation.user_subscription_id, relation_event.event_id,
            briefing.application_run_id, outbox.outbox_id,
            activation.activation_id,
        }
        if len(new_ids) != 7 or new_ids & {
                conversation["conversation_id"], outcome.outcome_id}:
            raise ValueError("Subscription commit identities must be distinct")
        if (conversation["user_id"] != user_id
                or conversation["status"] != "DEFINITION_ACCEPTED"
                or outcome.outcome_type != "DONE"):
            raise ValueError("Definition outcome is not accepted")
        if (definition.conversation_id != conversation["conversation_id"]
                or definition.definition_outcome_id != outcome.outcome_id
                or definition.definition_version != 1
                or definition.snapshot != outcome.payload["definition"]):
            raise ValueError("Definition does not bind durable outcome")
        expected_subscription = {
            "topic": subscription.topic, "language": subscription.language,
            "cadence": subscription.cadence,
            "max_chars": subscription.max_chars,
            "max_items": subscription.max_items,
            "focus_topics": list(subscription.focus_topics),
            "delivery_preference": subscription.delivery_channel,
        }
        definition_execution = {
            name: definition.snapshot[name] for name in expected_subscription
        }
        if (subscription.user_id != user_id
                or subscription.natural_language_request != first_turn["safe_text"]
                or not subscription.enabled
                or expected_subscription != definition_execution):
            raise ValueError("Subscription payload does not match Definition")
        if (product.subscription_id != subscription.subscription_id
                or product.definition_id != definition.definition_id
                or product.definition_version != definition.definition_version
                or product.status != "ACTIVE"):
            raise ValueError("Subscription aggregate binding mismatch")
        if (relation.user_id != user_id
                or relation.subscription_id != subscription.subscription_id
                or relation.status != "ACTIVE"):
            raise ValueError("UserSubscription binding mismatch")
        expected_relation_identity = user_subscription_relation_identity(
            relation, 1,
        )
        if (relation_event.event_id != relation_event_identity(
                    relation.user_subscription_id, 1,
                )
                or relation_event.event_type != "USER_SUBSCRIPTION_CREATED"
                or relation_event.user_subscription_id
                != relation.user_subscription_id
                or relation_event.user_id != user_id
                or relation_event.subscription_id
                != subscription.subscription_id
                or relation_event.relation_version != 1
                or relation_event.relation_identity
                != expected_relation_identity
                or relation_event.status != "pending"
                or relation_event.attempt_number != 0
                or relation_event.version != 1
                or relation_event.last_error_code is not None
                or relation_event.created_at != relation.created_at
                or relation_event.available_at != relation.created_at
                or relation_event.updated_at != relation.created_at):
            raise ValueError("Relation event binding mismatch")
        if (briefing.subscription_id != subscription.subscription_id
                or briefing.definition_id != definition.definition_id
                or briefing.definition_version != definition.definition_version
                or briefing.status != "PENDING"
                or briefing.harness_run_id is not None):
            raise ValueError("Briefing reservation binding mismatch")
        if (outbox.subscription_id != subscription.subscription_id
                or outbox.application_run_id != briefing.application_run_id
                or outbox.status != "pending" or outbox.attempt_number != 0
                or outbox.payload_refs != {
                    "activation_id": activation.activation_id,
                    "definition_id": definition.definition_id,
                    "definition_version": definition.definition_version,
                    "application_run_id": briefing.application_run_id,
                }):
            raise ValueError("Outbox binding mismatch")
        if (activation.conversation_id != conversation["conversation_id"]
                or activation.definition_outcome_id != outcome.outcome_id
                or activation.definition_id != definition.definition_id
                or activation.subscription_id != subscription.subscription_id
                or activation.user_subscription_id
                != relation.user_subscription_id
                or activation.application_run_id != briefing.application_run_id
                or activation.outbox_id != outbox.outbox_id):
            raise ValueError("Activation binding mismatch")

    def commit_subscription_product(self, user_id, proposed,
                                    fault_injector=None):
        def fault(stage):
            if fault_injector is not None:
                fault_injector(stage, proposed)

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            outcome_row = connection.execute("""
                SELECT o.* FROM definition_outcomes o
                WHERE o.outcome_id=?
            """, (proposed.activation.definition_outcome_id,)).fetchone()
            conversation = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id=?",
                (proposed.activation.conversation_id,),
            ).fetchone()
            if outcome_row is None or conversation is None:
                raise ValueError("Definition outcome not found")
            outcome = self._definition_outcome_from(outcome_row)
            existing = self._commit_from_connection(
                connection, outcome.outcome_id, True,
            )
            if existing is not None:
                if existing.relation.user_id != user_id:
                    raise ValueError("Subscription activation ownership mismatch")
                return existing
            first_turn = connection.execute("""
                SELECT * FROM conversation_turns
                WHERE conversation_id=? ORDER BY turn_number LIMIT 1
            """, (conversation["conversation_id"],)).fetchone()
            if first_turn is None:
                raise ValueError("Conversation has no durable user turn")
            self._validate_subscription_commit(
                user_id, proposed, conversation, outcome, first_turn,
            )
            definition = proposed.definition
            connection.execute("""
                INSERT INTO subscription_definitions(
                    definition_id, definition_version, conversation_id,
                    definition_outcome_id, snapshot_json, snapshot_identity,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                definition.definition_id, definition.definition_version,
                definition.conversation_id, definition.definition_outcome_id,
                json.dumps(definition.snapshot, ensure_ascii=False,
                           sort_keys=True),
                definition.snapshot_identity, definition.created_at,
            ))
            fault("after_definition")
            subscription = proposed.legacy_subscription
            connection.execute("""
                INSERT INTO subscriptions(
                    subscription_id, user_id, payload_json, version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                subscription.subscription_id, subscription.user_id,
                json.dumps(self._subscription_payload(subscription),
                           ensure_ascii=False, sort_keys=True),
                subscription.version, subscription.created_at,
                subscription.updated_at,
            ))
            product = proposed.subscription
            connection.execute("""
                INSERT INTO subscription_aggregates(
                    subscription_id, definition_id, definition_version,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                product.subscription_id, product.definition_id,
                product.definition_version, product.status,
                product.created_at, product.updated_at,
            ))
            fault("after_subscription")
            relation = proposed.relation
            connection.execute("""
                INSERT INTO user_subscriptions(
                    user_subscription_id, user_id, subscription_id, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                relation.user_subscription_id, relation.user_id,
                relation.subscription_id, relation.status,
                relation.created_at, relation.updated_at,
            ))
            fault("after_relation")
            relation_event = proposed.relation_event
            connection.execute("""
                INSERT INTO relation_event_outbox(
                    event_id, event_type, user_subscription_id, user_id,
                    subscription_id, relation_version, relation_identity,
                    payload_json, payload_identity, status, attempt_number,
                    created_at, available_at, last_error_code, version,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                relation_event.event_id, relation_event.event_type,
                relation_event.user_subscription_id, relation_event.user_id,
                relation_event.subscription_id, relation_event.relation_version,
                relation_event.relation_identity,
                json.dumps(relation_event.payload, sort_keys=True),
                relation_event.payload_identity, relation_event.status,
                relation_event.attempt_number, relation_event.created_at,
                relation_event.available_at, relation_event.last_error_code,
                relation_event.version, relation_event.updated_at,
            ))
            fault("after_relation_event")
            briefing = proposed.briefing
            connection.execute("""
                INSERT INTO briefing_reservations(
                    application_run_id, subscription_id, definition_id,
                    definition_version, status, harness_run_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                briefing.application_run_id, briefing.subscription_id,
                briefing.definition_id, briefing.definition_version,
                briefing.status, briefing.harness_run_id,
                briefing.created_at, briefing.updated_at,
            ))
            fault("after_briefing_reservation")
            outbox = proposed.outbox
            connection.execute("""
                INSERT INTO application_outbox(
                    outbox_id, event_type, subscription_id,
                    application_run_id, payload_ref_json, payload_identity,
                    status, attempt_number, created_at, available_at,
                    last_error_code, version, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                outbox.outbox_id, outbox.event_type, outbox.subscription_id,
                outbox.application_run_id,
                json.dumps(outbox.payload_refs, sort_keys=True),
                outbox.payload_identity, outbox.status,
                outbox.attempt_number, outbox.created_at,
                outbox.available_at, outbox.last_error_code,
                outbox.version, outbox.updated_at,
            ))
            fault("after_outbox")
            activation = proposed.activation
            connection.execute("""
                INSERT INTO subscription_activations(
                    activation_id, conversation_id, definition_outcome_id,
                    definition_id, subscription_id, user_subscription_id,
                    application_run_id, outbox_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                activation.activation_id, activation.conversation_id,
                activation.definition_outcome_id, activation.definition_id,
                activation.subscription_id, activation.user_subscription_id,
                activation.application_run_id, activation.outbox_id,
                activation.created_at,
            ))
            fault("after_activation_binding")
        return SubscriptionCommit(
            proposed.definition, proposed.legacy_subscription,
            proposed.subscription, proposed.relation, proposed.relation_event,
            proposed.briefing, proposed.outbox, proposed.activation, False,
        )

    @staticmethod
    def _validate_condition_subscription_commit(
            user_id, proposed, conversation, outcome, first_turn):
        if not isinstance(proposed, ConditionSubscriptionCommit):
            raise ValueError("invalid CONDITION Subscription commit")
        definition = proposed.definition
        subscription = proposed.legacy_subscription
        product = proposed.subscription
        relation = proposed.relation
        relation_event = proposed.relation_event
        tracking = proposed.tracking_definition
        policies = proposed.policies
        request = proposed.condition_request
        activation = proposed.activation
        temporal = proposed.temporal_state
        cycle = proposed.initial_cycle
        new_ids = {
            definition.definition_id, subscription.subscription_id,
            relation.user_subscription_id, relation_event.event_id,
            request.request_id, activation.activation_id,
            cycle.cycle_id if cycle is not None else "",
        }
        if (temporal is None or cycle is None or len(new_ids) != 7
                or new_ids & {
                    conversation["conversation_id"], outcome.outcome_id}):
            raise ValueError("CONDITION commit identities must be distinct")
        if (conversation["user_id"] != user_id
                or conversation["status"] != "DEFINITION_ACCEPTED"
                or outcome.outcome_type != "DONE"):
            raise ValueError("Definition outcome is not accepted")
        if (definition.conversation_id != conversation["conversation_id"]
                or definition.definition_outcome_id != outcome.outcome_id
                or definition.definition_version != 1
                or definition.snapshot != outcome.payload["definition"]):
            raise ValueError("Definition does not bind durable outcome")
        expected_subscription = {
            "topic": subscription.topic, "language": subscription.language,
            "cadence": subscription.cadence,
            "max_chars": subscription.max_chars,
            "max_items": subscription.max_items,
            "focus_topics": list(subscription.focus_topics),
            "delivery_preference": subscription.delivery_channel,
        }
        definition_execution = {
            name: definition.snapshot[name] for name in expected_subscription
        }
        if (subscription.user_id != user_id
                or subscription.natural_language_request
                != first_turn["safe_text"]
                or not subscription.enabled
                or expected_subscription != definition_execution):
            raise ValueError("Subscription payload does not match Definition")
        if (product.subscription_id != subscription.subscription_id
                or product.definition_id != definition.definition_id
                or product.definition_version != 1
                or product.status != "ACTIVE"
                or product.workflow_kind != "CONDITION"):
            raise ValueError("CONDITION aggregate binding mismatch")
        if (relation.user_id != user_id
                or relation.subscription_id != subscription.subscription_id
                or relation.status != "ACTIVE"):
            raise ValueError("UserSubscription binding mismatch")
        expected_relation_identity = user_subscription_relation_identity(
            relation, 1,
        )
        if (relation_event.event_id != relation_event_identity(
                    relation.user_subscription_id, 1)
                or relation_event.event_type != "USER_SUBSCRIPTION_CREATED"
                or relation_event.user_subscription_id
                != relation.user_subscription_id
                or relation_event.user_id != user_id
                or relation_event.subscription_id != subscription.subscription_id
                or relation_event.relation_identity
                != expected_relation_identity
                or relation_event.status != "pending"):
            raise ValueError("Relation event binding mismatch")
        if (tracking.definition_id != definition.definition_id
                or tracking.definition_version != definition.definition_version
                or tracking.subscription_id != subscription.subscription_id
                or tracking.workflow_kind != "CONDITION"):
            raise ValueError("Tracking Definition binding mismatch")
        if (policies.subscription_id != subscription.subscription_id
                or policies.definition_id != definition.definition_id
                or policies.definition_version != definition.definition_version):
            raise ValueError("Tracking Policy binding mismatch")
        if (request.subscription_id != subscription.subscription_id
                or request.definition_id != definition.definition_id
                or request.definition_version != definition.definition_version
                or request.status != "PENDING"):
            raise ValueError("Condition request binding mismatch")
        if (temporal.subscription_id != subscription.subscription_id
                or temporal.definition_id != definition.definition_id
                or temporal.definition_version != definition.definition_version
                or temporal.execution_policy_version != 1
                or temporal.lifecycle_status != "ACTIVE"
                or cycle.request_id != request.request_id
                or cycle.subscription_id != subscription.subscription_id
                or cycle.definition_id != definition.definition_id
                or cycle.definition_version != definition.definition_version
                or cycle.execution_policy_version != 1
                or cycle.cycle_kind != "INITIAL"
                or cycle.status != "PENDING"):
            raise ValueError("Condition temporal binding mismatch")
        if (activation.conversation_id != conversation["conversation_id"]
                or activation.definition_outcome_id != outcome.outcome_id
                or activation.definition_id != definition.definition_id
                or activation.subscription_id != subscription.subscription_id
                or activation.user_subscription_id
                != relation.user_subscription_id
                or activation.condition_request_id != request.request_id):
            raise ValueError("Condition activation binding mismatch")

    def commit_condition_subscription_product(self, user_id, proposed,
                                              fault_injector=None):
        def fault(stage):
            if fault_injector is not None:
                fault_injector(stage, proposed)

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            outcome_row = connection.execute(
                "SELECT * FROM definition_outcomes WHERE outcome_id=?",
                (proposed.activation.definition_outcome_id,),
            ).fetchone()
            conversation = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id=?",
                (proposed.activation.conversation_id,),
            ).fetchone()
            if outcome_row is None or conversation is None:
                raise ValueError("Definition outcome not found")
            outcome = self._definition_outcome_from(outcome_row)
            existing = self._condition_commit_from_connection(
                connection, outcome.outcome_id, True,
            )
            if existing is not None:
                if existing.relation.user_id != user_id:
                    raise ValueError("Subscription activation ownership mismatch")
                return existing
            first_turn = connection.execute("""
                SELECT * FROM conversation_turns
                WHERE conversation_id=? ORDER BY turn_number LIMIT 1
            """, (conversation["conversation_id"],)).fetchone()
            if first_turn is None:
                raise ValueError("Conversation has no durable user turn")
            self._validate_condition_subscription_commit(
                user_id, proposed, conversation, outcome, first_turn,
            )
            definition = proposed.definition
            connection.execute("""
                INSERT INTO subscription_definitions(
                    definition_id, definition_version, conversation_id,
                    definition_outcome_id, snapshot_json, snapshot_identity,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                definition.definition_id, definition.definition_version,
                definition.conversation_id, definition.definition_outcome_id,
                json.dumps(definition.snapshot, ensure_ascii=False,
                           sort_keys=True),
                definition.snapshot_identity, definition.created_at,
            ))
            fault("after_definition")
            subscription = proposed.legacy_subscription
            connection.execute("""
                INSERT INTO subscriptions(
                    subscription_id, user_id, payload_json, version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                subscription.subscription_id, subscription.user_id,
                json.dumps(self._subscription_payload(subscription),
                           ensure_ascii=False, sort_keys=True),
                subscription.version, subscription.created_at,
                subscription.updated_at,
            ))
            product = proposed.subscription
            connection.execute("""
                INSERT INTO subscription_aggregates(
                    subscription_id, definition_id, definition_version,
                    status, created_at, updated_at, workflow_kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                product.subscription_id, product.definition_id,
                product.definition_version, product.status,
                product.created_at, product.updated_at,
                product.workflow_kind,
            ))
            fault("after_subscription")
            relation = proposed.relation
            connection.execute("""
                INSERT INTO user_subscriptions(
                    user_subscription_id, user_id, subscription_id, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                relation.user_subscription_id, relation.user_id,
                relation.subscription_id, relation.status,
                relation.created_at, relation.updated_at,
            ))
            fault("after_relation")
            relation_event = proposed.relation_event
            connection.execute("""
                INSERT INTO relation_event_outbox(
                    event_id, event_type, user_subscription_id, user_id,
                    subscription_id, relation_version, relation_identity,
                    payload_json, payload_identity, status, attempt_number,
                    created_at, available_at, last_error_code, version,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                relation_event.event_id, relation_event.event_type,
                relation_event.user_subscription_id, relation_event.user_id,
                relation_event.subscription_id, relation_event.relation_version,
                relation_event.relation_identity,
                json.dumps(relation_event.payload, sort_keys=True),
                relation_event.payload_identity, relation_event.status,
                relation_event.attempt_number, relation_event.created_at,
                relation_event.available_at, relation_event.last_error_code,
                relation_event.version, relation_event.updated_at,
            ))
            fault("after_relation_event")
            tracking = proposed.tracking_definition
            connection.execute("""
                INSERT INTO tracking_definitions(
                    definition_id, definition_version, subscription_id,
                    workflow_kind, snapshot_json, snapshot_identity, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                tracking.definition_id, tracking.definition_version,
                tracking.subscription_id, tracking.workflow_kind,
                json.dumps(tracking.snapshot, ensure_ascii=False,
                           sort_keys=True),
                tracking.snapshot_identity, tracking.created_at,
            ))
            policies = proposed.policies
            connection.execute("""
                INSERT INTO tracking_policy_snapshots(
                    subscription_id, definition_id, definition_version,
                    execution_json, presentation_json, distribution_json,
                    snapshot_identity, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                policies.subscription_id, policies.definition_id,
                policies.definition_version,
                json.dumps(policies.execution, sort_keys=True),
                json.dumps(policies.presentation, sort_keys=True),
                json.dumps(policies.distribution, sort_keys=True),
                policies.snapshot_identity, policies.created_at,
            ))
            fault("after_tracking_definition")
            request = proposed.condition_request
            connection.execute("""
                INSERT INTO condition_observation_requests(
                    request_id, subscription_id, definition_id,
                    definition_version, idempotency_key, status,
                    evaluation_id, failure_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                request.request_id, request.subscription_id,
                request.definition_id, request.definition_version,
                request.idempotency_key, request.status,
                request.evaluation_id, request.failure_code,
                request.created_at, request.updated_at,
            ))
            fault("after_condition_request")
            temporal = proposed.temporal_state
            connection.execute("""
                INSERT INTO condition_temporal_states(
                    subscription_id, definition_id, definition_version,
                    execution_policy_version, lifecycle_status,
                    cadence_seconds, cadence_provenance, timezone_name,
                    schedule_anchor_at, window_start_at,
                    window_end_exclusive, next_due_at,
                    last_attempted_cycle_id, last_attempted_at,
                    last_successful_cycle_id, last_successful_cycle_at,
                    last_failure_code, last_failure_at, last_observation_id,
                    last_evaluation_id, last_observed_at, previous_truth,
                    armed, last_emitted_evaluation_id,
                    last_emitted_update_id, last_emitted_at, paused_at,
                    completed_at, completion_reason, version, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                temporal.subscription_id, temporal.definition_id,
                temporal.definition_version,
                temporal.execution_policy_version,
                temporal.lifecycle_status, temporal.cadence_seconds,
                temporal.cadence_provenance, temporal.timezone_name,
                temporal.schedule_anchor_at, temporal.window_start_at,
                temporal.window_end_exclusive, temporal.next_due_at,
                temporal.last_attempted_cycle_id,
                temporal.last_attempted_at,
                temporal.last_successful_cycle_id,
                temporal.last_successful_cycle_at,
                temporal.last_failure_code, temporal.last_failure_at,
                temporal.last_observation_id, temporal.last_evaluation_id,
                temporal.last_observed_at, temporal.previous_truth,
                int(temporal.armed), temporal.last_emitted_evaluation_id,
                temporal.last_emitted_update_id, temporal.last_emitted_at,
                temporal.paused_at, temporal.completed_at,
                temporal.completion_reason, temporal.version,
                temporal.created_at, temporal.updated_at,
            ))
            cycle = proposed.initial_cycle
            connection.execute("""
                INSERT INTO condition_observation_cycles(
                    cycle_id, request_id, subscription_id, definition_id,
                    definition_version, execution_policy_version, cycle_kind,
                    scheduled_due_at, coalesced_from_at, coalesced_to_at,
                    coalesced_count, status, claim_token, claimed_at,
                    observation_id, evaluation_id, predicate_truth,
                    emission_decision, update_id, distribution_id,
                    failure_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?)
            """, (
                cycle.cycle_id, cycle.request_id, cycle.subscription_id,
                cycle.definition_id, cycle.definition_version,
                cycle.execution_policy_version, cycle.cycle_kind,
                cycle.scheduled_due_at, cycle.coalesced_from_at,
                cycle.coalesced_to_at, cycle.coalesced_count, cycle.status,
                cycle.claim_token, cycle.claimed_at, cycle.observation_id,
                cycle.evaluation_id, cycle.predicate_truth,
                cycle.emission_decision, cycle.update_id,
                cycle.distribution_id, cycle.failure_code, cycle.created_at,
                cycle.updated_at,
            ))
            fault("after_condition_temporal")
            activation = proposed.activation
            connection.execute("""
                INSERT INTO condition_subscription_activations(
                    activation_id, conversation_id, definition_outcome_id,
                    definition_id, subscription_id, user_subscription_id,
                    condition_request_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                activation.activation_id, activation.conversation_id,
                activation.definition_outcome_id, activation.definition_id,
                activation.subscription_id, activation.user_subscription_id,
                activation.condition_request_id, activation.created_at,
            ))
            fault("after_activation_binding")
        return ConditionSubscriptionCommit(
            proposed.definition, proposed.legacy_subscription,
            proposed.subscription, proposed.relation, proposed.relation_event,
            proposed.tracking_definition, proposed.policies,
            proposed.condition_request, proposed.activation,
            proposed.temporal_state, proposed.initial_cycle, False,
        )

    def commit_event_subscription_product(self, user_id, proposed,
                                          fault_injector=None):
        if not isinstance(proposed, EventSubscriptionCommit):
            raise ValueError("invalid EVENT Subscription commit")

        def fault(stage):
            if fault_injector is not None:
                fault_injector(stage, proposed)

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._event_commit_from_connection(
                connection, proposed.activation.definition_outcome_id, True,
            )
            if existing is not None:
                if existing.relation.user_id != user_id:
                    raise ValueError("Subscription activation ownership mismatch")
                return existing
            outcome = connection.execute(
                "SELECT 1 FROM definition_outcomes WHERE outcome_id=?",
                (proposed.activation.definition_outcome_id,),
            ).fetchone()
            if outcome is None or proposed.relation.user_id != user_id:
                raise ValueError("EVENT Definition outcome not found")
            if (proposed.subscription.workflow_kind != "EVENT"
                    or proposed.tracking_definition.workflow_kind != "EVENT"
                    or proposed.temporal_state.subscription_id
                    != proposed.subscription.subscription_id
                    or proposed.initial_cycle.subscription_id
                    != proposed.subscription.subscription_id):
                raise ValueError("EVENT Subscription binding mismatch")

            definition = proposed.definition
            connection.execute("""
                INSERT INTO subscription_definitions(
                    definition_id, definition_version, conversation_id,
                    definition_outcome_id, snapshot_json, snapshot_identity,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                definition.definition_id, definition.definition_version,
                definition.conversation_id, definition.definition_outcome_id,
                json.dumps(definition.snapshot, ensure_ascii=False,
                           sort_keys=True), definition.snapshot_identity,
                definition.created_at,
            ))
            fault("after_definition")
            subscription = proposed.legacy_subscription
            connection.execute("""
                INSERT INTO subscriptions(
                    subscription_id, user_id, payload_json, version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                subscription.subscription_id, subscription.user_id,
                json.dumps(self._subscription_payload(subscription),
                           ensure_ascii=False, sort_keys=True),
                subscription.version, subscription.created_at,
                subscription.updated_at,
            ))
            product = proposed.subscription
            connection.execute("""
                INSERT INTO subscription_aggregates(
                    subscription_id, definition_id, definition_version,
                    status, created_at, updated_at, workflow_kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                product.subscription_id, product.definition_id,
                product.definition_version, product.status,
                product.created_at, product.updated_at, product.workflow_kind,
            ))
            relation = proposed.relation
            connection.execute("""
                INSERT INTO user_subscriptions(
                    user_subscription_id, user_id, subscription_id, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                relation.user_subscription_id, relation.user_id,
                relation.subscription_id, relation.status,
                relation.created_at, relation.updated_at,
            ))
            relation_event = proposed.relation_event
            connection.execute("""
                INSERT INTO relation_event_outbox(
                    event_id, event_type, user_subscription_id, user_id,
                    subscription_id, relation_version, relation_identity,
                    payload_json, payload_identity, status, attempt_number,
                    created_at, available_at, last_error_code, version,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                relation_event.event_id, relation_event.event_type,
                relation_event.user_subscription_id, relation_event.user_id,
                relation_event.subscription_id, relation_event.relation_version,
                relation_event.relation_identity,
                json.dumps(relation_event.payload, sort_keys=True),
                relation_event.payload_identity, relation_event.status,
                relation_event.attempt_number, relation_event.created_at,
                relation_event.available_at, relation_event.last_error_code,
                relation_event.version, relation_event.updated_at,
            ))
            tracking = proposed.tracking_definition
            connection.execute("""
                INSERT INTO event_tracking_definitions(
                    definition_id, definition_version, subscription_id,
                    workflow_kind, snapshot_json, snapshot_identity, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                tracking.definition_id, tracking.definition_version,
                tracking.subscription_id, tracking.workflow_kind,
                json.dumps(tracking.snapshot, ensure_ascii=False,
                           sort_keys=True), tracking.snapshot_identity,
                tracking.created_at,
            ))
            policy = proposed.policies
            connection.execute("""
                INSERT INTO event_tracking_policy_snapshots(
                    subscription_id, definition_id, definition_version,
                    execution_json, presentation_json, distribution_json,
                    snapshot_identity, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                policy.subscription_id, policy.definition_id,
                policy.definition_version,
                json.dumps(policy.execution, sort_keys=True),
                json.dumps(policy.presentation, sort_keys=True),
                json.dumps(policy.distribution, sort_keys=True),
                policy.snapshot_identity, policy.created_at,
            ))
            temporal = proposed.temporal_state
            connection.execute("""
                INSERT INTO event_temporal_states(
                    subscription_id, definition_id, definition_version,
                    execution_policy_version, lifecycle_status,
                    cadence_seconds, cadence_provenance, timezone_name,
                    schedule_anchor_at, activation_at, next_due_at,
                    verified_through, last_attempted_cycle_id,
                    last_attempted_at, last_successful_cycle_id,
                    last_successful_cycle_at, last_failure_code,
                    last_failure_at, last_verification_id, last_update_id,
                    paused_at, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?)
            """, tuple(asdict(temporal).values()))
            cycle = proposed.initial_cycle
            connection.execute("""
                INSERT INTO event_observation_cycles(
                    cycle_id, subscription_id, definition_id,
                    definition_version, execution_policy_version, cycle_kind,
                    scheduled_due_at, coalesced_from_at, coalesced_to_at,
                    coalesced_count, window_start_at, window_end_at, status,
                    harness_run_id, claim_token, claimed_at, observation_id,
                    candidate_id, verification_id, outcome, reason_code,
                    event_id, update_id, distribution_id, failure_code,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, tuple(asdict(cycle).values()))
            activation = proposed.activation
            connection.execute("""
                INSERT INTO event_subscription_activations(
                    activation_id, conversation_id, definition_outcome_id,
                    definition_id, subscription_id, user_subscription_id,
                    initial_cycle_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, tuple(asdict(activation).values()))
            fault("after_activation_binding")
        return EventSubscriptionCommit(
            proposed.definition, proposed.legacy_subscription,
            proposed.subscription, proposed.relation, proposed.relation_event,
            proposed.tracking_definition, proposed.policies,
            proposed.activation, proposed.temporal_state,
            proposed.initial_cycle, False,
        )

    def get_subscription_commit_for_outcome(self, outcome_id):
        with self.connect() as connection:
            value = self._commit_from_connection(connection, outcome_id, True)
            return (value or self._condition_commit_from_connection(
                connection, outcome_id, True,
            ) or self._event_commit_from_connection(
                connection, outcome_id, True,
            ))

    def get_product_subscription(self, subscription_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM subscription_aggregates WHERE subscription_id=?",
                (subscription_id,),
            ).fetchone()
        return self._product_subscription_from(row)

    def get_user_subscription_for_subscription(self, subscription_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM user_subscriptions WHERE subscription_id=?",
                (subscription_id,),
            ).fetchone()
        return self._user_subscription_from(row)

    def get_subscription_definition(self, definition_id, definition_version):
        with self.connect() as connection:
            row = connection.execute("""
                SELECT * FROM subscription_definitions
                WHERE definition_id=? AND definition_version=?
            """, (definition_id, definition_version)).fetchone()
        return self._subscription_definition_from(row)

    def get_tracking_definition(self, definition_id, definition_version):
        with self.connect() as connection:
            row = connection.execute("""
                SELECT * FROM tracking_definitions
                WHERE definition_id=? AND definition_version=?
            """, (definition_id, definition_version)).fetchone()
            if row is None:
                row = connection.execute("""
                    SELECT * FROM event_tracking_definitions
                    WHERE definition_id=? AND definition_version=?
                """, (definition_id, definition_version)).fetchone()
        return self._tracking_definition_from(row)

    def get_tracking_policy(self, subscription_id, definition_id,
                            definition_version):
        with self.connect() as connection:
            row = connection.execute("""
                SELECT * FROM tracking_policy_snapshots
                WHERE subscription_id=? AND definition_id=?
                  AND definition_version=?
            """, (
                subscription_id, definition_id, definition_version,
            )).fetchone()
            if row is None:
                row = connection.execute("""
                    SELECT * FROM event_tracking_policy_snapshots
                    WHERE subscription_id=? AND definition_id=?
                      AND definition_version=?
                """, (
                    subscription_id, definition_id, definition_version,
                )).fetchone()
        return self._tracking_policy_from(row)

    def get_event_temporal_state(self, subscription_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM event_temporal_states WHERE subscription_id=?",
                (subscription_id,),
            ).fetchone()
        return self._event_temporal_from(row)

    def get_event_cycle(self, cycle_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM event_observation_cycles WHERE cycle_id=?",
                (cycle_id,),
            ).fetchone()
        return self._event_cycle_from(row)

    def list_due_event_temporal_states(self, timestamp, maximum=100):
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT s.* FROM event_temporal_states s
                JOIN subscription_aggregates a
                  ON a.subscription_id=s.subscription_id
                JOIN user_subscriptions u
                  ON u.subscription_id=s.subscription_id
                WHERE s.lifecycle_status='ACTIVE' AND s.next_due_at<=?
                  AND a.status='ACTIVE' AND u.status='ACTIVE'
                ORDER BY s.next_due_at, s.subscription_id LIMIT ?
            """, (timestamp, maximum)).fetchall()
        return tuple(self._event_temporal_from(row) for row in rows)

    def reserve_event_cycle(self, expected_state_version, cycle, next_due_at):
        if not isinstance(cycle, EventObservationCycle):
            raise ValueError("invalid EVENT cycle")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM event_observation_cycles WHERE cycle_id=?",
                (cycle.cycle_id,),
            ).fetchone()
            if existing is not None:
                return self._event_cycle_from(existing), False
            cursor = connection.execute("""
                UPDATE event_temporal_states
                SET next_due_at=?, version=version+1, updated_at=?
                WHERE subscription_id=? AND lifecycle_status='ACTIVE'
                  AND version=? AND next_due_at=?
            """, (
                next_due_at, cycle.updated_at, cycle.subscription_id,
                expected_state_version, cycle.coalesced_from_at,
            ))
            if cursor.rowcount != 1:
                return None, False
            connection.execute("""
                INSERT INTO event_observation_cycles(
                    cycle_id, subscription_id, definition_id,
                    definition_version, execution_policy_version, cycle_kind,
                    scheduled_due_at, coalesced_from_at, coalesced_to_at,
                    coalesced_count, window_start_at, window_end_at, status,
                    harness_run_id, claim_token, claimed_at, observation_id,
                    candidate_id, verification_id, outcome, reason_code,
                    event_id, update_id, distribution_id, failure_code,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, tuple(asdict(cycle).values()))
            row = connection.execute(
                "SELECT * FROM event_observation_cycles WHERE cycle_id=?",
                (cycle.cycle_id,),
            ).fetchone()
        return self._event_cycle_from(row), True

    def claim_event_cycle(self, claim_token, timestamp, recovery_before):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("""
                SELECT c.* FROM event_observation_cycles c
                JOIN event_temporal_states s
                  ON s.subscription_id=c.subscription_id
                JOIN subscription_aggregates a
                  ON a.subscription_id=c.subscription_id
                JOIN user_subscriptions u
                  ON u.subscription_id=c.subscription_id
                WHERE s.lifecycle_status='ACTIVE' AND a.status='ACTIVE'
                  AND u.status='ACTIVE' AND (
                    c.status='PENDING' OR
                    (c.status='STARTED' AND c.claimed_at<?)
                  )
                ORDER BY c.scheduled_due_at, c.cycle_id LIMIT 1
            """, (recovery_before,)).fetchone()
            if row is None:
                return None
            cycle = self._event_cycle_from(row)
            connection.execute("""
                UPDATE event_observation_cycles
                SET status='STARTED', claim_token=?, claimed_at=?, updated_at=?
                WHERE cycle_id=?
            """, (claim_token, timestamp, timestamp, cycle.cycle_id))
            connection.execute("""
                UPDATE event_temporal_states
                SET last_attempted_cycle_id=?, last_attempted_at=?,
                    version=version+1, updated_at=?
                WHERE subscription_id=?
            """, (cycle.cycle_id, timestamp, timestamp,
                  cycle.subscription_id))
            cycle_row = connection.execute(
                "SELECT * FROM event_observation_cycles WHERE cycle_id=?",
                (cycle.cycle_id,),
            ).fetchone()
            state_row = connection.execute(
                "SELECT * FROM event_temporal_states WHERE subscription_id=?",
                (cycle.subscription_id,),
            ).fetchone()
        return self._event_cycle_from(cycle_row), self._event_temporal_from(state_row)

    def release_event_cycle_claim(self, cycle_id, claim_token, timestamp):
        with self.connect() as connection:
            connection.execute("""
                UPDATE event_observation_cycles
                SET status='PENDING', claim_token=NULL, claimed_at=NULL,
                    updated_at=?
                WHERE cycle_id=? AND status='STARTED' AND claim_token=?
            """, (timestamp, cycle_id, claim_token))

    def fail_event_cycle(self, cycle_id, claim_token, failure_code, timestamp):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM event_observation_cycles WHERE cycle_id=?",
                (cycle_id,),
            ).fetchone()
            cycle = self._event_cycle_from(row)
            if cycle is None:
                raise ValueError("EVENT cycle not found")
            if cycle.status == "STARTED" and cycle.claim_token == claim_token:
                connection.execute("""
                    UPDATE event_observation_cycles
                    SET status='FAILED', failure_code=?, updated_at=?
                    WHERE cycle_id=? AND claim_token=?
                """, (failure_code, timestamp, cycle_id, claim_token))
                connection.execute("""
                    UPDATE event_temporal_states
                    SET last_failure_code=?, last_failure_at=?,
                        version=version+1, updated_at=?
                    WHERE subscription_id=?
                """, (failure_code, timestamp, timestamp,
                      cycle.subscription_id))
            row = connection.execute(
                "SELECT * FROM event_observation_cycles WHERE cycle_id=?",
                (cycle_id,),
            ).fetchone()
        return self._event_cycle_from(row)

    def get_event_source_observation(self, observation_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM event_source_observations WHERE observation_id=?",
                (observation_id,),
            ).fetchone()
        return self._event_source_observation_from(row)

    def get_event_candidate(self, candidate_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM event_candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
        return self._event_candidate_from(row)

    def get_event_verification(self, verification_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM event_verifications WHERE verification_id=?",
                (verification_id,),
            ).fetchone()
        return self._event_verification_from(row)

    def get_verified_event_by_identity(self, logical_identity):
        with self.connect() as connection:
            row = connection.execute("""
                SELECT * FROM verified_events WHERE logical_event_identity=?
            """, (logical_identity,)).fetchone()
        return self._verified_event_from(row)

    def list_verified_events(self):
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT * FROM verified_events ORDER BY created_at, event_id
            """).fetchall()
        return tuple(self._verified_event_from(row) for row in rows)

    def complete_event_cycle(self, cycle_id, claim_token, observation,
                             candidate, verification, verified_event, update,
                             distribution, timestamp):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cycle_row = connection.execute(
                "SELECT * FROM event_observation_cycles WHERE cycle_id=?",
                (cycle_id,),
            ).fetchone()
            cycle = self._event_cycle_from(cycle_row)
            if cycle is None:
                raise ValueError("EVENT cycle not found")
            if cycle.status in {"SUCCEEDED", "INCOMPLETE"}:
                return cycle, True
            if cycle.status != "STARTED" or cycle.claim_token != claim_token:
                raise ValueError("EVENT cycle claim lost")
            connection.execute("""
                INSERT OR IGNORE INTO event_source_observations(
                    observation_id, entity_key, window_start_at,
                    window_end_at, retrieved_at, coverage_complete,
                    truncated, provider, results_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                observation.observation_id, observation.entity_key,
                observation.window_start_at, observation.window_end_at,
                observation.retrieved_at, int(observation.coverage_complete),
                int(observation.truncated), observation.provider,
                json.dumps([item.as_dict() for item in observation.results],
                           ensure_ascii=False, sort_keys=True),
            ))
            if candidate is not None:
                connection.execute("""
                    INSERT OR IGNORE INTO event_candidates(
                        candidate_id, observation_id, harness_run_id,
                        entity_key, event_type, object_type, display_name,
                        canonical_name_candidate, occurred_at_candidate,
                        support_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    candidate.candidate_id, candidate.observation_id,
                    candidate.harness_run_id, candidate.entity_key,
                    candidate.event_type, candidate.object_type,
                    candidate.display_name,
                    candidate.canonical_name_candidate,
                    candidate.occurred_at_candidate,
                    json.dumps([asdict(item) for item in candidate.support],
                               ensure_ascii=False, sort_keys=True),
                ))
            connection.execute("""
                INSERT OR IGNORE INTO event_verifications(
                    verification_id, subscription_id, definition_id,
                    definition_version, observation_id,
                    observation_evidence_id, candidate_id, outcome,
                    reason_code, policy_version, logical_event_identity,
                    canonical_model_key, verification_evidence_id, verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, tuple(asdict(verification).values()))
            if verified_event is not None:
                connection.execute("""
                    INSERT OR IGNORE INTO verified_events(
                        event_id, logical_event_identity, entity_key,
                        event_type, object_type, canonical_model_key,
                        display_name, occurred_at, verification_id,
                        verification_evidence_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, tuple(asdict(verified_event).values()))
            if update is not None:
                connection.execute("""
                    INSERT OR IGNORE INTO tracking_updates(
                        update_id, subscription_id, definition_id,
                        definition_version, evaluation_id, evidence_id,
                        update_type, payload_json, occurred_at, created_at,
                        verified_event_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    update.update_id, update.subscription_id,
                    update.definition_id, update.definition_version,
                    update.evaluation_id, update.evidence_id,
                    update.update_type,
                    json.dumps(update.payload, ensure_ascii=False,
                               sort_keys=True), update.occurred_at,
                    update.created_at, update.verified_event_id,
                ))
            if distribution is not None:
                connection.execute("""
                    INSERT OR IGNORE INTO update_distributions(
                        distribution_id, update_id, user_subscription_id,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                """, tuple(asdict(distribution).values()))
            status = (
                "INCOMPLETE" if verification.outcome == "VERIFICATION_INCOMPLETE"
                else "SUCCEEDED"
            )
            connection.execute("""
                UPDATE event_observation_cycles
                SET status=?, observation_id=?, candidate_id=?,
                    verification_id=?, outcome=?, reason_code=?, event_id=?,
                    update_id=?, distribution_id=?, failure_code=NULL,
                    updated_at=?
                WHERE cycle_id=? AND status='STARTED' AND claim_token=?
            """, (
                status, observation.observation_id,
                candidate.candidate_id if candidate else None,
                verification.verification_id, verification.outcome,
                verification.reason_code,
                verified_event.event_id if verified_event else None,
                update.update_id if update else None,
                distribution.distribution_id if distribution else None,
                timestamp, cycle_id, claim_token,
            ))
            if status == "SUCCEEDED":
                connection.execute("""
                    UPDATE event_temporal_states
                    SET verified_through=?, last_successful_cycle_id=?,
                        last_successful_cycle_at=?, last_failure_code=NULL,
                        last_failure_at=NULL, last_verification_id=?,
                        last_update_id=COALESCE(?, last_update_id),
                        version=version+1, updated_at=?
                    WHERE subscription_id=?
                """, (
                    cycle.window_end_at, cycle_id, timestamp,
                    verification.verification_id,
                    update.update_id if update else None, timestamp,
                    cycle.subscription_id,
                ))
            else:
                connection.execute("""
                    UPDATE event_temporal_states
                    SET last_verification_id=?, version=version+1,
                        updated_at=? WHERE subscription_id=?
                """, (verification.verification_id, timestamp,
                      cycle.subscription_id))
            row = connection.execute(
                "SELECT * FROM event_observation_cycles WHERE cycle_id=?",
                (cycle_id,),
            ).fetchone()
        return self._event_cycle_from(row), False

    def get_condition_temporal_state(self, subscription_id):
        with self.connect() as connection:
            row = connection.execute("""
                SELECT * FROM condition_temporal_states WHERE subscription_id=?
            """, (subscription_id,)).fetchone()
        return self._condition_temporal_from(row)

    def get_condition_cycle(self, cycle_id):
        with self.connect() as connection:
            row = connection.execute("""
                SELECT * FROM condition_observation_cycles WHERE cycle_id=?
            """, (cycle_id,)).fetchone()
        return self._condition_cycle_from(row)

    def get_condition_cycle_for_request(self, request_id):
        with self.connect() as connection:
            row = connection.execute("""
                SELECT * FROM condition_observation_cycles WHERE request_id=?
            """, (request_id,)).fetchone()
        return self._condition_cycle_from(row)

    def list_condition_cycles(self, subscription_id):
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT * FROM condition_observation_cycles
                WHERE subscription_id=?
                ORDER BY scheduled_due_at, cycle_id
            """, (subscription_id,)).fetchall()
        return tuple(self._condition_cycle_from(row) for row in rows)

    def list_due_condition_temporal_states(self, timestamp, maximum=100):
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT s.* FROM condition_temporal_states s
                WHERE s.lifecycle_status='ACTIVE'
                  AND s.next_due_at IS NOT NULL AND s.next_due_at <= ?
                  AND NOT EXISTS (
                    SELECT 1 FROM condition_observation_cycles c
                    WHERE c.subscription_id=s.subscription_id
                      AND c.status IN ('PENDING', 'STARTED')
                  )
                ORDER BY s.next_due_at, s.subscription_id LIMIT ?
            """, (timestamp, maximum)).fetchall()
        return tuple(self._condition_temporal_from(row) for row in rows)

    @staticmethod
    def _insert_condition_request_cycle(connection, request, cycle):
        connection.execute("""
            INSERT OR IGNORE INTO condition_observation_requests(
                request_id, subscription_id, definition_id,
                definition_version, idempotency_key, status,
                evaluation_id, failure_code, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            request.request_id, request.subscription_id,
            request.definition_id, request.definition_version,
            request.idempotency_key, request.status, request.evaluation_id,
            request.failure_code, request.created_at, request.updated_at,
        ))
        connection.execute("""
            INSERT OR IGNORE INTO condition_observation_cycles(
                cycle_id, request_id, subscription_id, definition_id,
                definition_version, execution_policy_version, cycle_kind,
                scheduled_due_at, coalesced_from_at, coalesced_to_at,
                coalesced_count, status, claim_token, claimed_at,
                observation_id, evaluation_id, predicate_truth,
                emission_decision, update_id, distribution_id, failure_code,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?)
        """, (
            cycle.cycle_id, cycle.request_id, cycle.subscription_id,
            cycle.definition_id, cycle.definition_version,
            cycle.execution_policy_version, cycle.cycle_kind,
            cycle.scheduled_due_at, cycle.coalesced_from_at,
            cycle.coalesced_to_at, cycle.coalesced_count, cycle.status,
            cycle.claim_token, cycle.claimed_at, cycle.observation_id,
            cycle.evaluation_id, cycle.predicate_truth,
            cycle.emission_decision, cycle.update_id, cycle.distribution_id,
            cycle.failure_code, cycle.created_at, cycle.updated_at,
        ))

    def reserve_condition_cycle(self, state_version, request, cycle,
                                next_due_at):
        if (not isinstance(request, ConditionObservationRequest)
                or not isinstance(cycle, ConditionObservationCycle)):
            raise ValueError("invalid Condition cycle reservation")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state_row = connection.execute("""
                SELECT * FROM condition_temporal_states WHERE subscription_id=?
            """, (request.subscription_id,)).fetchone()
            state = self._condition_temporal_from(state_row)
            existing = connection.execute("""
                SELECT * FROM condition_observation_cycles WHERE cycle_id=?
            """, (cycle.cycle_id,)).fetchone()
            if existing is not None:
                return self._condition_cycle_from(existing), False
            if (state is None or state.version != state_version
                    or state.lifecycle_status != "ACTIVE"
                    or state.next_due_at != cycle.coalesced_from_at):
                return None, False
            self._insert_condition_request_cycle(connection, request, cycle)
            connection.execute("""
                UPDATE condition_temporal_states
                SET next_due_at=?, version=version+1, updated_at=?
                WHERE subscription_id=? AND version=?
                  AND lifecycle_status='ACTIVE'
            """, (
                next_due_at, cycle.created_at, request.subscription_id,
                state_version,
            ))
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise ValueError("Condition due reservation conflict")
        return cycle, True

    def reserve_manual_condition_cycle(self, request, cycle):
        if (not isinstance(request, ConditionObservationRequest)
                or not isinstance(cycle, ConditionObservationCycle)
                or cycle.cycle_kind != "MANUAL"):
            raise ValueError("invalid manual Condition cycle")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute("""
                SELECT * FROM condition_temporal_states
                WHERE subscription_id=? AND lifecycle_status='ACTIVE'
            """, (request.subscription_id,)).fetchone()
            if state is None:
                raise ValueError("Condition temporal state inactive")
            self._insert_condition_request_cycle(connection, request, cycle)
            row = connection.execute("""
                SELECT * FROM condition_observation_cycles
                WHERE request_id=?
            """, (request.request_id,)).fetchone()
        return self._condition_cycle_from(row)

    def claim_condition_cycle(self, claim_token, timestamp, recovery_before):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("""
                SELECT c.* FROM condition_observation_cycles c
                JOIN condition_temporal_states s
                  ON s.subscription_id=c.subscription_id
                WHERE s.lifecycle_status='ACTIVE' AND (
                    c.status='PENDING' OR
                    (c.status='STARTED' AND c.claimed_at <= ?)
                )
                ORDER BY c.scheduled_due_at, c.cycle_id LIMIT 1
            """, (recovery_before,)).fetchone()
            if row is None:
                return None
            cycle = self._condition_cycle_from(row)
            connection.execute("""
                UPDATE condition_observation_cycles
                SET status='STARTED', claim_token=?, claimed_at=?, updated_at=?
                WHERE cycle_id=? AND (
                    status='PENDING' OR
                    (status='STARTED' AND claimed_at <= ?)
                )
            """, (
                claim_token, timestamp, timestamp, cycle.cycle_id,
                recovery_before,
            ))
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                return None
            connection.execute("""
                UPDATE condition_temporal_states
                SET last_attempted_cycle_id=?, last_attempted_at=?,
                    version=version+1, updated_at=?
                WHERE subscription_id=? AND lifecycle_status='ACTIVE'
            """, (
                cycle.cycle_id, timestamp, timestamp, cycle.subscription_id,
            ))
            request_row = connection.execute("""
                SELECT * FROM condition_observation_requests WHERE request_id=?
            """, (cycle.request_id,)).fetchone()
            cycle_row = connection.execute("""
                SELECT * FROM condition_observation_cycles WHERE cycle_id=?
            """, (cycle.cycle_id,)).fetchone()
            state_row = connection.execute("""
                SELECT * FROM condition_temporal_states WHERE subscription_id=?
            """, (cycle.subscription_id,)).fetchone()
        return (
            self._condition_request_from(request_row),
            self._condition_cycle_from(cycle_row),
            self._condition_temporal_from(state_row),
        )

    def release_condition_cycle_claim(self, cycle_id, claim_token, timestamp):
        with self.connect() as connection:
            connection.execute("""
                UPDATE condition_observation_cycles
                SET status='PENDING', claim_token=NULL, claimed_at=NULL,
                    updated_at=?
                WHERE cycle_id=? AND status='STARTED' AND claim_token=?
            """, (timestamp, cycle_id, claim_token))

    def fail_condition_cycle(self, cycle_id, claim_token, failure_code,
                             timestamp):
        legacy_code = (
            "STALE_OBSERVATION" if failure_code in {
                "STALE_OBSERVATION", "OUT_OF_ORDER_OBSERVATION",
            } else "INVALID_OBSERVATION"
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cycle_row = connection.execute("""
                SELECT * FROM condition_observation_cycles WHERE cycle_id=?
            """, (cycle_id,)).fetchone()
            cycle = self._condition_cycle_from(cycle_row)
            if cycle is None:
                raise ValueError("Condition cycle not found")
            if cycle.status == "FAILED":
                if cycle.failure_code != failure_code:
                    raise ValueError("Condition cycle failure conflict")
            elif (cycle.status == "STARTED"
                  and cycle.claim_token == claim_token):
                connection.execute("""
                    UPDATE condition_observation_cycles
                    SET status='FAILED', failure_code=?, claim_token=NULL,
                        updated_at=?
                    WHERE cycle_id=? AND status='STARTED' AND claim_token=?
                """, (failure_code, timestamp, cycle_id, claim_token))
                connection.execute("""
                    UPDATE condition_observation_requests
                    SET status='FAILED', failure_code=?, updated_at=?
                    WHERE request_id=? AND status='PENDING'
                """, (legacy_code, timestamp, cycle.request_id))
                connection.execute("""
                    UPDATE condition_temporal_states
                    SET last_failure_code=?, last_failure_at=?,
                        version=version+1, updated_at=?
                    WHERE subscription_id=?
                """, (
                    failure_code, timestamp, timestamp, cycle.subscription_id,
                ))
            elif cycle.status == "SUPERSEDED":
                return cycle
            else:
                raise ValueError("Condition cycle cannot fail")
            row = connection.execute("""
                SELECT * FROM condition_observation_cycles WHERE cycle_id=?
            """, (cycle_id,)).fetchone()
        return self._condition_cycle_from(row)

    def expire_condition_temporal_states(self, timestamp):
        expired = []
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("""
                SELECT * FROM condition_temporal_states
                WHERE lifecycle_status IN ('ACTIVE', 'PAUSED')
                  AND window_end_exclusive <= ?
                ORDER BY subscription_id
            """, (timestamp,)).fetchall()
            for row in rows:
                subscription_id = row["subscription_id"]
                connection.execute("""
                    UPDATE condition_temporal_states
                    SET lifecycle_status='COMPLETED', next_due_at=NULL,
                        completed_at=?, completion_reason='TIME_WINDOW_ENDED',
                        version=version+1, updated_at=?
                    WHERE subscription_id=?
                      AND lifecycle_status IN ('ACTIVE', 'PAUSED')
                """, (timestamp, timestamp, subscription_id))
                connection.execute("""
                    UPDATE condition_observation_cycles
                    SET status='SUPERSEDED', claim_token=NULL, updated_at=?
                    WHERE subscription_id=? AND status IN ('PENDING', 'STARTED')
                """, (timestamp, subscription_id))
                connection.execute("""
                    UPDATE condition_observation_requests
                    SET status='FAILED', failure_code='INVALID_OBSERVATION',
                        updated_at=?
                    WHERE subscription_id=? AND status='PENDING'
                """, (timestamp, subscription_id))
                connection.execute("""
                    UPDATE subscription_aggregates
                    SET status='DISABLED', updated_at=? WHERE subscription_id=?
                """, (timestamp, subscription_id))
                connection.execute("""
                    UPDATE user_subscriptions
                    SET status='DISABLED', updated_at=? WHERE subscription_id=?
                """, (timestamp, subscription_id))
                payload_row = connection.execute("""
                    SELECT payload_json, version FROM subscriptions
                    WHERE subscription_id=?
                """, (subscription_id,)).fetchone()
                if payload_row is not None:
                    payload = json.loads(payload_row["payload_json"])
                    payload["enabled"] = False
                    payload["version"] = payload_row["version"] + 1
                    payload["updated_at"] = timestamp
                    connection.execute("""
                        UPDATE subscriptions
                        SET payload_json=?, version=version+1, updated_at=?
                        WHERE subscription_id=?
                    """, (
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        timestamp, subscription_id,
                    ))
                expired.append(subscription_id)
        return tuple(expired)

    def complete_condition_cycle(
            self, request, cycle, claim_token, state_version, observation,
            evaluation, emission_decision, update, distribution,
            fault_injector=None):
        def fault(stage):
            if fault_injector is not None:
                fault_injector(stage, evaluation)

        if (not isinstance(request, ConditionObservationRequest)
                or not isinstance(cycle, ConditionObservationCycle)
                or not isinstance(observation, AcceptedFlightPriceObservation)
                or not isinstance(evaluation, ConditionEvaluation)):
            raise ValueError("invalid Condition cycle completion")
        emit = emission_decision in {
            "EMIT_FIRST_MATCH", "EMIT_THRESHOLD_CROSSING",
        }
        if emit != (update is not None and distribution is not None):
            raise ValueError("Condition emission binding mismatch")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cycle_row = connection.execute("""
                SELECT * FROM condition_observation_cycles WHERE cycle_id=?
            """, (cycle.cycle_id,)).fetchone()
            current_cycle = self._condition_cycle_from(cycle_row)
            request_row = connection.execute("""
                SELECT * FROM condition_observation_requests WHERE request_id=?
            """, (request.request_id,)).fetchone()
            current_request = self._condition_request_from(request_row)
            state_row = connection.execute("""
                SELECT * FROM condition_temporal_states WHERE subscription_id=?
            """, (cycle.subscription_id,)).fetchone()
            state = self._condition_temporal_from(state_row)
            if current_cycle is None or current_request is None or state is None:
                raise ValueError("Condition cycle completion facts missing")
            if current_cycle.status == "SUCCEEDED":
                stored_evaluation = self._condition_evaluation_from(
                    connection.execute("""
                        SELECT * FROM condition_evaluations WHERE evaluation_id=?
                    """, (current_cycle.evaluation_id,)).fetchone(),
                )
                stored_update = self._tracking_update_from(
                    connection.execute("""
                        SELECT * FROM tracking_updates WHERE update_id=?
                    """, (current_cycle.update_id,)).fetchone(),
                ) if current_cycle.update_id else None
                stored_distribution = self._distribution_from(
                    connection.execute("""
                        SELECT * FROM update_distributions
                        WHERE distribution_id=?
                    """, (current_cycle.distribution_id,)).fetchone(),
                ) if current_cycle.distribution_id else None
                return (
                    current_request, stored_evaluation, stored_update,
                    stored_distribution, current_cycle, state, True,
                    "SUCCEEDED",
                )
            if (current_cycle.status == "SUPERSEDED"
                    or state.lifecycle_status != "ACTIVE"):
                if current_cycle.status == "STARTED":
                    connection.execute("""
                        UPDATE condition_observation_cycles
                        SET status='SUPERSEDED', claim_token=NULL, updated_at=?
                        WHERE cycle_id=? AND status='STARTED'
                    """, (evaluation.evaluated_at, cycle.cycle_id))
                    connection.execute("""
                        UPDATE condition_observation_requests
                        SET status='FAILED',
                            failure_code='INVALID_OBSERVATION', updated_at=?
                        WHERE request_id=? AND status='PENDING'
                    """, (evaluation.evaluated_at, request.request_id))
                superseded = self._condition_cycle_from(connection.execute("""
                    SELECT * FROM condition_observation_cycles WHERE cycle_id=?
                """, (cycle.cycle_id,)).fetchone())
                return (
                    self._condition_request_from(connection.execute("""
                        SELECT * FROM condition_observation_requests
                        WHERE request_id=?
                    """, (request.request_id,)).fetchone()),
                    None, None, None, superseded, state, False, "SUPERSEDED",
                )
            if (current_cycle.status != "STARTED"
                    or current_cycle.claim_token != claim_token
                    or state.version != state_version
                    or current_request.status != "PENDING"):
                raise ValueError("Condition cycle completion conflict")
            if (cycle.subscription_id != observation.subscription_id
                    or cycle.subscription_id != evaluation.subscription_id
                    or cycle.definition_id != evaluation.definition_id
                    or cycle.definition_version != evaluation.definition_version
                    or evaluation.observation_id != observation.observation_id
                    or evaluation.evidence_id != observation.evidence_id):
                raise ValueError("Condition cycle completion binding mismatch")

            existing_evaluation_row = connection.execute("""
                SELECT * FROM condition_evaluations WHERE evaluation_id=?
            """, (evaluation.evaluation_id,)).fetchone()
            duplicate = existing_evaluation_row is not None
            if duplicate:
                stored_evaluation = self._condition_evaluation_from(
                    existing_evaluation_row,
                )
                if stored_evaluation != evaluation:
                    raise ValueError("immutable Condition evaluation conflict")
                stored_update = self._tracking_update_from(
                    connection.execute("""
                        SELECT * FROM tracking_updates WHERE evaluation_id=?
                    """, (evaluation.evaluation_id,)).fetchone(),
                )
                stored_distribution = (
                    self._distribution_from(connection.execute("""
                        SELECT d.* FROM update_distributions d
                        JOIN tracking_updates u ON u.update_id=d.update_id
                        WHERE u.evaluation_id=?
                    """, (evaluation.evaluation_id,)).fetchone())
                    if stored_update is not None else None
                )
                truth = (
                    "TRUE" if stored_evaluation.result == "MATCHED" else "FALSE"
                )
                connection.execute("""
                    UPDATE condition_observation_requests
                    SET status='EVALUATED', evaluation_id=?, updated_at=?
                    WHERE request_id=? AND status='PENDING'
                """, (
                    evaluation.evaluation_id, observation.accepted_at,
                    request.request_id,
                ))
                connection.execute("""
                    UPDATE condition_observation_cycles
                    SET status='SUCCEEDED', claim_token=NULL,
                        observation_id=?, evaluation_id=?, predicate_truth=?,
                        emission_decision='DUPLICATE_OBSERVATION', update_id=?,
                        distribution_id=?, updated_at=?
                    WHERE cycle_id=? AND status='STARTED' AND claim_token=?
                """, (
                    evaluation.observation_id, evaluation.evaluation_id, truth,
                    stored_update.update_id if stored_update else None,
                    (stored_distribution.distribution_id
                     if stored_distribution else None),
                    observation.accepted_at, cycle.cycle_id, claim_token,
                ))
                connection.execute("""
                    UPDATE condition_temporal_states
                    SET last_successful_cycle_id=?,
                        last_successful_cycle_at=?, version=version+1,
                        updated_at=?
                    WHERE subscription_id=? AND version=?
                """, (
                    cycle.cycle_id, observation.accepted_at,
                    observation.accepted_at, cycle.subscription_id,
                    state_version,
                ))
                completed_cycle = self._condition_cycle_from(
                    connection.execute("""
                        SELECT * FROM condition_observation_cycles
                        WHERE cycle_id=?
                    """, (cycle.cycle_id,)).fetchone(),
                )
                updated_state = self._condition_temporal_from(
                    connection.execute("""
                        SELECT * FROM condition_temporal_states
                        WHERE subscription_id=?
                    """, (cycle.subscription_id,)).fetchone(),
                )
                completed_request = self._condition_request_from(
                    connection.execute("""
                        SELECT * FROM condition_observation_requests
                        WHERE request_id=?
                    """, (request.request_id,)).fetchone(),
                )
                return (
                    completed_request, stored_evaluation, stored_update,
                    stored_distribution, completed_cycle, updated_state, True,
                    "SUCCEEDED",
                )

            observation_row = connection.execute("""
                SELECT * FROM flight_price_observations WHERE observation_id=?
            """, (observation.observation_id,)).fetchone()
            if observation_row is None:
                quote = observation.quote
                connection.execute("""
                    INSERT INTO flight_price_observations(
                        observation_id, subscription_id, source_signal_id,
                        signal_identity, origin, destination, trip_type,
                        travel_month, metric, price, currency, observed_at,
                        evidence_id, accepted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    observation.observation_id, observation.subscription_id,
                    quote.source_signal_id, observation.signal_identity,
                    quote.origin, quote.destination, quote.trip_type,
                    quote.travel_month, quote.metric, quote.price,
                    quote.currency, quote.observed_at,
                    observation.evidence_id, observation.accepted_at,
                ))
            elif self._flight_observation_from(observation_row) != observation:
                raise ValueError("immutable Flight Observation conflict")
            fault("after_observation")
            connection.execute("""
                INSERT INTO condition_evaluations(
                    evaluation_id, subscription_id, definition_id,
                    definition_version, observation_id, evidence_id,
                    observed_price, threshold, currency, operator, result,
                    evaluator_version, evaluated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                evaluation.evaluation_id, evaluation.subscription_id,
                evaluation.definition_id, evaluation.definition_version,
                evaluation.observation_id, evaluation.evidence_id,
                evaluation.observed_price, evaluation.threshold,
                evaluation.currency, evaluation.operator, evaluation.result,
                evaluation.evaluator_version, evaluation.evaluated_at,
            ))
            fault("after_evaluation")
            if emit:
                relation = connection.execute("""
                    SELECT * FROM user_subscriptions
                    WHERE user_subscription_id=?
                """, (distribution.user_subscription_id,)).fetchone()
                if (relation is None or relation["subscription_id"]
                        != update.subscription_id
                        or relation["status"] != "ACTIVE"
                        or distribution.update_id != update.update_id):
                    raise ValueError("Update/Distribution binding mismatch")
                connection.execute("""
                    INSERT INTO tracking_updates(
                        update_id, subscription_id, definition_id,
                        definition_version, evaluation_id, evidence_id,
                        update_type, payload_json, occurred_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    update.update_id, update.subscription_id,
                    update.definition_id, update.definition_version,
                    update.evaluation_id, update.evidence_id,
                    update.update_type,
                    json.dumps(update.payload, ensure_ascii=False,
                               sort_keys=True),
                    update.occurred_at, update.created_at,
                ))
                fault("after_update")
                connection.execute("""
                    INSERT INTO update_distributions(
                        distribution_id, update_id, user_subscription_id,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                """, (
                    distribution.distribution_id, distribution.update_id,
                    distribution.user_subscription_id, distribution.status,
                    distribution.created_at,
                ))
                fault("after_distribution")
            truth = "TRUE" if evaluation.result == "MATCHED" else "FALSE"
            connection.execute("""
                UPDATE condition_observation_requests
                SET status='EVALUATED', evaluation_id=?, failure_code=NULL,
                    updated_at=?
                WHERE request_id=? AND status='PENDING'
            """, (
                evaluation.evaluation_id, evaluation.evaluated_at,
                request.request_id,
            ))
            connection.execute("""
                UPDATE condition_observation_cycles
                SET status='SUCCEEDED', claim_token=NULL, observation_id=?,
                    evaluation_id=?, predicate_truth=?, emission_decision=?,
                    update_id=?, distribution_id=?, failure_code=NULL,
                    updated_at=?
                WHERE cycle_id=? AND status='STARTED' AND claim_token=?
            """, (
                observation.observation_id, evaluation.evaluation_id, truth,
                emission_decision, update.update_id if update else None,
                distribution.distribution_id if distribution else None,
                evaluation.evaluated_at, cycle.cycle_id, claim_token,
            ))
            connection.execute("""
                UPDATE condition_temporal_states
                SET last_successful_cycle_id=?, last_successful_cycle_at=?,
                    last_observation_id=?, last_evaluation_id=?,
                    last_observed_at=?, previous_truth=?, armed=?,
                    last_emitted_evaluation_id=CASE WHEN ? THEN ?
                        ELSE last_emitted_evaluation_id END,
                    last_emitted_update_id=CASE WHEN ? THEN ?
                        ELSE last_emitted_update_id END,
                    last_emitted_at=CASE WHEN ? THEN ?
                        ELSE last_emitted_at END,
                    version=version+1, updated_at=?
                WHERE subscription_id=? AND version=?
                  AND lifecycle_status='ACTIVE'
            """, (
                cycle.cycle_id, evaluation.evaluated_at,
                observation.observation_id, evaluation.evaluation_id,
                observation.quote.observed_at, truth,
                0 if truth == "TRUE" else 1,
                int(emit), evaluation.evaluation_id,
                int(emit), update.update_id if update else None,
                int(emit), evaluation.evaluated_at,
                evaluation.evaluated_at, cycle.subscription_id, state_version,
            ))
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise ValueError("Condition temporal completion conflict")
            fault("after_request_completion")
            completed_request = self._condition_request_from(
                connection.execute("""
                    SELECT * FROM condition_observation_requests
                    WHERE request_id=?
                """, (request.request_id,)).fetchone(),
            )
            completed_cycle = self._condition_cycle_from(
                connection.execute("""
                    SELECT * FROM condition_observation_cycles WHERE cycle_id=?
                """, (cycle.cycle_id,)).fetchone(),
            )
            updated_state = self._condition_temporal_from(
                connection.execute("""
                    SELECT * FROM condition_temporal_states
                    WHERE subscription_id=?
                """, (cycle.subscription_id,)).fetchone(),
            )
        return (
            completed_request, evaluation, update, distribution,
            completed_cycle, updated_state, False, "SUCCEEDED",
        )

    def get_pending_condition_request(self):
        with self.connect() as connection:
            row = connection.execute("""
                SELECT * FROM condition_observation_requests
                WHERE status='PENDING'
                ORDER BY created_at, request_id LIMIT 1
            """).fetchone()
        return self._condition_request_from(row)

    def get_pending_legacy_condition_request(self):
        with self.connect() as connection:
            row = connection.execute("""
                SELECT r.* FROM condition_observation_requests r
                LEFT JOIN condition_observation_cycles c
                  ON c.request_id=r.request_id
                WHERE r.status='PENDING' AND c.cycle_id IS NULL
                ORDER BY r.created_at, r.request_id LIMIT 1
            """).fetchone()
        return self._condition_request_from(row)

    def get_latest_condition_request_for_subscription(self, subscription_id):
        with self.connect() as connection:
            row = connection.execute("""
                SELECT * FROM condition_observation_requests
                WHERE subscription_id=?
                ORDER BY created_at DESC, request_id DESC LIMIT 1
            """, (subscription_id,)).fetchone()
        return self._condition_request_from(row)

    def reserve_condition_request(self, record):
        if not isinstance(record, ConditionObservationRequest):
            raise ValueError("invalid Condition request")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                INSERT OR IGNORE INTO condition_observation_requests(
                    request_id, subscription_id, definition_id,
                    definition_version, idempotency_key, status,
                    evaluation_id, failure_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.request_id, record.subscription_id,
                record.definition_id, record.definition_version,
                record.idempotency_key, record.status, record.evaluation_id,
                record.failure_code, record.created_at, record.updated_at,
            ))
            row = connection.execute("""
                SELECT * FROM condition_observation_requests
                WHERE subscription_id=? AND idempotency_key=?
            """, (record.subscription_id, record.idempotency_key)).fetchone()
        stored = self._condition_request_from(row)
        if (stored is None or stored.definition_id != record.definition_id
                or stored.definition_version != record.definition_version):
            raise ValueError("Condition request idempotency conflict")
        return stored, stored.request_id == record.request_id

    def fail_condition_request(self, request_id, failure_code, timestamp):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("""
                SELECT * FROM condition_observation_requests WHERE request_id=?
            """, (request_id,)).fetchone()
            current = self._condition_request_from(row)
            if current is None:
                raise ValueError("Condition request not found")
            if current.status == "PENDING":
                connection.execute("""
                    UPDATE condition_observation_requests
                    SET status='FAILED', failure_code=?, updated_at=?
                    WHERE request_id=? AND status='PENDING'
                """, (failure_code, timestamp, request_id))
            elif current.status != "FAILED" or current.failure_code != failure_code:
                raise ValueError("Condition request cannot fail")
            row = connection.execute("""
                SELECT * FROM condition_observation_requests WHERE request_id=?
            """, (request_id,)).fetchone()
        return self._condition_request_from(row)

    def get_flight_observation(self, observation_id):
        with self.connect() as connection:
            row = connection.execute("""
                SELECT * FROM flight_price_observations WHERE observation_id=?
            """, (observation_id,)).fetchone()
        return self._flight_observation_from(row)

    def get_condition_evaluation(self, evaluation_id):
        with self.connect() as connection:
            row = connection.execute("""
                SELECT * FROM condition_evaluations WHERE evaluation_id=?
            """, (evaluation_id,)).fetchone()
        return self._condition_evaluation_from(row)

    def get_latest_condition_evaluation_for_subscription(self,
                                                         subscription_id):
        with self.connect() as connection:
            row = connection.execute("""
                SELECT e.* FROM condition_evaluations e
                JOIN condition_observation_requests r
                  ON r.evaluation_id=e.evaluation_id
                WHERE r.subscription_id=?
                ORDER BY e.evaluated_at DESC, e.evaluation_id DESC LIMIT 1
            """, (subscription_id,)).fetchone()
        return self._condition_evaluation_from(row)

    def link_condition_request(self, request_id, evaluation_id, timestamp):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            evaluation = connection.execute("""
                SELECT * FROM condition_evaluations WHERE evaluation_id=?
            """, (evaluation_id,)).fetchone()
            request = connection.execute("""
                SELECT * FROM condition_observation_requests WHERE request_id=?
            """, (request_id,)).fetchone()
            if evaluation is None or request is None:
                raise ValueError("Condition request/evaluation not found")
            if request["subscription_id"] != evaluation["subscription_id"]:
                raise ValueError("Condition request/evaluation mismatch")
            if request["status"] == "PENDING":
                connection.execute("""
                    UPDATE condition_observation_requests
                    SET status='EVALUATED', evaluation_id=?, updated_at=?
                    WHERE request_id=? AND status='PENDING'
                """, (evaluation_id, timestamp, request_id))
            elif (request["status"] != "EVALUATED"
                  or request["evaluation_id"] != evaluation_id):
                raise ValueError("Condition request cannot link evaluation")
            row = connection.execute("""
                SELECT * FROM condition_observation_requests WHERE request_id=?
            """, (request_id,)).fetchone()
        return self._condition_request_from(row)

    def complete_condition_request(self, request, observation, evaluation,
                                   update, distribution,
                                   fault_injector=None):
        def fault(stage):
            if fault_injector is not None:
                fault_injector(stage, evaluation)

        if (not isinstance(request, ConditionObservationRequest)
                or not isinstance(observation, AcceptedFlightPriceObservation)
                or not isinstance(evaluation, ConditionEvaluation)):
            raise ValueError("invalid Condition completion")
        if evaluation.result == "MATCHED":
            if (not isinstance(update, TrackingUpdate)
                    or not isinstance(distribution, UpdateDistribution)):
                raise ValueError("matched Condition requires Update/Distribution")
        elif update is not None or distribution is not None:
            raise ValueError("NO_UPDATE cannot persist Update/Distribution")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("""
                SELECT * FROM condition_observation_requests WHERE request_id=?
            """, (request.request_id,)).fetchone()
            current = self._condition_request_from(row)
            if current is None:
                raise ValueError("Condition request not found")
            if current.status == "EVALUATED":
                stored_evaluation = self._condition_evaluation_from(
                    connection.execute("""
                        SELECT * FROM condition_evaluations WHERE evaluation_id=?
                    """, (current.evaluation_id,)).fetchone(),
                )
                stored_update = self._tracking_update_from(
                    connection.execute("""
                        SELECT * FROM tracking_updates WHERE evaluation_id=?
                    """, (current.evaluation_id,)).fetchone(),
                )
                stored_distribution = self._distribution_from(
                    connection.execute("""
                        SELECT d.* FROM update_distributions d
                        JOIN tracking_updates u ON u.update_id=d.update_id
                        WHERE u.evaluation_id=?
                    """, (current.evaluation_id,)).fetchone(),
                )
                return (current, stored_evaluation, stored_update,
                        stored_distribution, True)
            if (current.status != "PENDING"
                    or current.subscription_id != observation.subscription_id
                    or current.subscription_id != evaluation.subscription_id
                    or current.definition_id != evaluation.definition_id
                    or current.definition_version != evaluation.definition_version
                    or evaluation.observation_id != observation.observation_id
                    or evaluation.evidence_id != observation.evidence_id):
                raise ValueError("Condition completion binding mismatch")
            observation_row = connection.execute("""
                SELECT * FROM flight_price_observations WHERE observation_id=?
            """, (observation.observation_id,)).fetchone()
            if observation_row is None:
                quote = observation.quote
                connection.execute("""
                    INSERT INTO flight_price_observations(
                        observation_id, subscription_id, source_signal_id,
                        signal_identity, origin, destination, trip_type,
                        travel_month, metric, price, currency, observed_at,
                        evidence_id, accepted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    observation.observation_id, observation.subscription_id,
                    quote.source_signal_id, observation.signal_identity,
                    quote.origin, quote.destination, quote.trip_type,
                    quote.travel_month, quote.metric, quote.price,
                    quote.currency, quote.observed_at,
                    observation.evidence_id, observation.accepted_at,
                ))
            elif self._flight_observation_from(observation_row) != observation:
                raise ValueError("immutable Flight Observation conflict")
            fault("after_observation")
            existing_evaluation = connection.execute("""
                SELECT * FROM condition_evaluations WHERE evaluation_id=?
            """, (evaluation.evaluation_id,)).fetchone()
            if existing_evaluation is None:
                connection.execute("""
                    INSERT INTO condition_evaluations(
                        evaluation_id, subscription_id, definition_id,
                        definition_version, observation_id, evidence_id,
                        observed_price, threshold, currency, operator, result,
                        evaluator_version, evaluated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    evaluation.evaluation_id, evaluation.subscription_id,
                    evaluation.definition_id, evaluation.definition_version,
                    evaluation.observation_id, evaluation.evidence_id,
                    evaluation.observed_price, evaluation.threshold,
                    evaluation.currency, evaluation.operator,
                    evaluation.result, evaluation.evaluator_version,
                    evaluation.evaluated_at,
                ))
            elif self._condition_evaluation_from(existing_evaluation) != evaluation:
                raise ValueError("immutable Condition evaluation conflict")
            fault("after_evaluation")
            if update is not None:
                relation = connection.execute("""
                    SELECT * FROM user_subscriptions
                    WHERE user_subscription_id=?
                """, (distribution.user_subscription_id,)).fetchone()
                if (relation is None or relation["subscription_id"]
                        != update.subscription_id
                        or relation["status"] != "ACTIVE"
                        or update.evaluation_id != evaluation.evaluation_id
                        or update.definition_id != evaluation.definition_id
                        or update.definition_version
                        != evaluation.definition_version
                        or update.evidence_id != evaluation.evidence_id
                        or distribution.update_id != update.update_id):
                    raise ValueError("Update/Distribution binding mismatch")
                connection.execute("""
                    INSERT INTO tracking_updates(
                        update_id, subscription_id, definition_id,
                        definition_version, evaluation_id, evidence_id,
                        update_type, payload_json, occurred_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    update.update_id, update.subscription_id,
                    update.definition_id, update.definition_version,
                    update.evaluation_id, update.evidence_id,
                    update.update_type,
                    json.dumps(update.payload, ensure_ascii=False,
                               sort_keys=True),
                    update.occurred_at, update.created_at,
                ))
                fault("after_update")
                connection.execute("""
                    INSERT INTO update_distributions(
                        distribution_id, update_id, user_subscription_id,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                """, (
                    distribution.distribution_id, distribution.update_id,
                    distribution.user_subscription_id, distribution.status,
                    distribution.created_at,
                ))
                fault("after_distribution")
            connection.execute("""
                UPDATE condition_observation_requests
                SET status='EVALUATED', evaluation_id=?, failure_code=NULL,
                    updated_at=?
                WHERE request_id=? AND status='PENDING'
            """, (
                evaluation.evaluation_id, evaluation.evaluated_at,
                request.request_id,
            ))
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise ValueError("Condition request completion conflict")
            fault("after_request_completion")
            completed_row = connection.execute("""
                SELECT * FROM condition_observation_requests WHERE request_id=?
            """, (request.request_id,)).fetchone()
        return (
            self._condition_request_from(completed_row), evaluation,
            update, distribution, False,
        )

    def get_update_for_evaluation(self, evaluation_id):
        with self.connect() as connection:
            row = connection.execute("""
                SELECT * FROM tracking_updates WHERE evaluation_id=?
            """, (evaluation_id,)).fetchone()
        return self._tracking_update_from(row)

    def get_tracking_update(self, update_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM tracking_updates WHERE update_id=?",
                (update_id,),
            ).fetchone()
        return self._tracking_update_from(row)

    def list_tracking_updates(self, user_id, subscription_id=None):
        query = """
            SELECT u.* FROM tracking_updates u
            JOIN update_distributions d ON d.update_id=u.update_id
            JOIN user_subscriptions r
              ON r.user_subscription_id=d.user_subscription_id
            WHERE r.user_id=?
        """
        values = [user_id]
        if subscription_id is not None:
            query += " AND u.subscription_id=?"
            values.append(subscription_id)
        query += " ORDER BY u.occurred_at DESC, u.update_id DESC"
        with self.connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return tuple(self._tracking_update_from(row) for row in rows)

    def get_distribution_for_update(self, update_id):
        with self.connect() as connection:
            row = connection.execute("""
                SELECT * FROM update_distributions WHERE update_id=?
            """, (update_id,)).fetchone()
        return self._distribution_from(row)

    def get_update_distribution(self, distribution_id):
        with self.connect() as connection:
            row = connection.execute("""
                SELECT * FROM update_distributions WHERE distribution_id=?
            """, (distribution_id,)).fetchone()
        return self._distribution_from(row)

    def list_notification_candidate_distributions(self, maximum=100):
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT d.* FROM update_distributions d
                LEFT JOIN delivery_records r
                  ON r.distribution_id=d.distribution_id
                 AND r.channel='termux_notification'
                WHERE d.status='AVAILABLE'
                  AND (r.delivery_id IS NULL OR r.status='pending')
                ORDER BY d.created_at, d.distribution_id
                LIMIT ?
            """, (maximum,)).fetchall()
        return tuple(self._distribution_from(row) for row in rows)

    def get_briefing_reservation(self, application_run_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM briefing_reservations WHERE application_run_id=?",
                (application_run_id,),
            ).fetchone()
        return self._briefing_reservation_from(row)

    def get_briefing_reservation_for_subscription(self, subscription_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM briefing_reservations WHERE subscription_id=?",
                (subscription_id,),
            ).fetchone()
        return self._briefing_reservation_from(row)

    def get_application_outbox(self, outbox_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM application_outbox WHERE outbox_id=?",
                (outbox_id,),
            ).fetchone()
        return self._application_outbox_from(row)

    def get_application_outbox_for_run(self, application_run_id):
        with self.connect() as connection:
            row = connection.execute("""
                SELECT * FROM application_outbox WHERE application_run_id=?
            """, (application_run_id,)).fetchone()
        return self._application_outbox_from(row)

    def list_application_outbox(self):
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT * FROM application_outbox
                ORDER BY created_at, outbox_id
            """).fetchall()
        return tuple(self._application_outbox_from(row) for row in rows)

    def claim_application_outbox(self, timestamp):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("""
                SELECT * FROM application_outbox
                WHERE status IN ('pending', 'retry_wait')
                  AND available_at<=?
                ORDER BY available_at, created_at, outbox_id
                LIMIT 1
            """, (timestamp,)).fetchone()
            if row is None:
                return None
            cursor = connection.execute("""
                UPDATE application_outbox
                SET status='claimed', attempt_number=attempt_number+1,
                    last_error_code=NULL, version=version+1, updated_at=?
                WHERE outbox_id=? AND version=?
                  AND status IN ('pending', 'retry_wait')
            """, (timestamp, row["outbox_id"], row["version"]))
            claimed = connection.execute(
                "SELECT * FROM application_outbox WHERE outbox_id=?",
                (row["outbox_id"],),
            ).fetchone()
        return (self._application_outbox_from(claimed)
                if cursor.rowcount == 1 else None)

    def finalize_application_outbox(self, outbox_id, expected_version,
                                    status, error_code, available_at,
                                    timestamp):
        if status not in {"retry_wait", "completed", "failed", "blocked"}:
            raise ValueError("invalid Outbox final status")
        if status == "completed" and error_code is not None:
            raise ValueError("completed Outbox cannot have an error")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM application_outbox WHERE outbox_id=?",
                (outbox_id,),
            ).fetchone()
            if (current is None or current["status"] != "claimed"
                    or current["version"] != expected_version):
                raise ValueError("Outbox claim is not owned")
            if status == "completed":
                run = connection.execute(
                    "SELECT * FROM digest_runs WHERE digest_run_id=?",
                    (current["application_run_id"],),
                ).fetchone()
                if run is None or run["status"] not in {
                        "completed", "incomplete", "failed"}:
                    raise ValueError("Outbox completion lacks terminal run")
                if run["status"] == "completed":
                    digest = connection.execute("""
                        SELECT digest_id FROM digests
                        WHERE digest_run_id=? AND digest_id=?
                    """, (
                        current["application_run_id"], run["digest_id"],
                    )).fetchone()
                    if run["digest_id"] is None or digest is None:
                        raise ValueError("Outbox completion lacks durable Digest")
            cursor = connection.execute("""
                UPDATE application_outbox
                SET status=?, last_error_code=?, available_at=?,
                    version=version+1, updated_at=?
                WHERE outbox_id=? AND status='claimed' AND version=?
            """, (
                status, error_code, available_at, timestamp, outbox_id,
                expected_version,
            ))
            row = connection.execute(
                "SELECT * FROM application_outbox WHERE outbox_id=?",
                (outbox_id,),
            ).fetchone()
        if cursor.rowcount != 1:
            raise ValueError("Outbox claim finalize conflict")
        return self._application_outbox_from(row)

    def get_relation_event(self, event_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM relation_event_outbox WHERE event_id=?",
                (event_id,),
            ).fetchone()
        return self._relation_event_from(row)

    def get_relation_event_for_relation(self, user_subscription_id):
        with self.connect() as connection:
            row = connection.execute("""
                SELECT * FROM relation_event_outbox
                WHERE user_subscription_id=?
                ORDER BY relation_version DESC LIMIT 1
            """, (user_subscription_id,)).fetchone()
        return self._relation_event_from(row)

    def list_relation_events(self):
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT * FROM relation_event_outbox
                ORDER BY created_at, event_id
            """).fetchall()
        return tuple(self._relation_event_from(row) for row in rows)

    def get_relation_event_attempt(self, attempt_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM relation_event_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
        return self._relation_event_attempt_from(row)

    def get_current_relation_event_attempt(self, event_id):
        with self.connect() as connection:
            row = connection.execute("""
                SELECT * FROM relation_event_attempts WHERE event_id=?
                ORDER BY attempt_number DESC LIMIT 1
            """, (event_id,)).fetchone()
        return self._relation_event_attempt_from(row)

    def list_relation_event_attempts(self, event_id):
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT * FROM relation_event_attempts WHERE event_id=?
                ORDER BY attempt_number
            """, (event_id,)).fetchall()
        return tuple(self._relation_event_attempt_from(row) for row in rows)

    def claim_relation_event(self, timestamp):
        """Claim one eligible typed event and reserve a publication attempt."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("""
                SELECT * FROM relation_event_outbox
                WHERE status IN ('pending', 'retry_wait')
                  AND available_at<=?
                ORDER BY available_at, created_at, event_id
                LIMIT 1
            """, (timestamp,)).fetchone()
            if row is None:
                return None
            attempt_number = row["attempt_number"] + 1
            attempt_id = relation_event_attempt_identity(
                row["event_id"], attempt_number,
            )
            cursor = connection.execute("""
                UPDATE relation_event_outbox
                SET status='claimed', attempt_number=?, last_error_code=NULL,
                    version=version+1, updated_at=?
                WHERE event_id=? AND version=?
                  AND status IN ('pending', 'retry_wait')
            """, (
                attempt_number, timestamp, row["event_id"], row["version"],
            ))
            if cursor.rowcount != 1:
                return None
            connection.execute("""
                INSERT INTO relation_event_attempts(
                    attempt_id, event_id, attempt_number, status,
                    effect_certainty, requested_at, completed_at, error_code
                ) VALUES (?, ?, ?, 'prepared', 'not_started', ?, NULL, NULL)
            """, (attempt_id, row["event_id"], attempt_number, timestamp))
            claimed = connection.execute(
                "SELECT * FROM relation_event_outbox WHERE event_id=?",
                (row["event_id"],),
            ).fetchone()
        return self._relation_event_from(claimed)

    def mark_relation_event_dispatch_started(self, event_id,
                                             expected_version, attempt_id,
                                             timestamp):
        """Persist the unknown-effect fence before invoking the publisher."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            event = connection.execute(
                "SELECT * FROM relation_event_outbox WHERE event_id=?",
                (event_id,),
            ).fetchone()
            attempt = connection.execute(
                "SELECT * FROM relation_event_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if (event is None or event["status"] != "claimed"
                    or event["version"] != expected_version
                    or attempt is None or attempt["event_id"] != event_id
                    or attempt["attempt_number"] != event["attempt_number"]
                    or attempt["status"] != "prepared"):
                raise ValueError("relation event claim is not owned")
            cursor = connection.execute("""
                UPDATE relation_event_attempts
                SET status='unknown', effect_certainty='unknown'
                WHERE attempt_id=? AND status='prepared'
            """, (attempt_id,))
            if cursor.rowcount != 1:
                raise ValueError("relation event dispatch fence conflict")
            row = connection.execute(
                "SELECT * FROM relation_event_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
        return self._relation_event_attempt_from(row)

    def finalize_relation_event(self, event_id, expected_version, attempt_id,
                                outcome, error_code, available_at, timestamp):
        transitions = {
            "accepted": ("completed", "accepted", "known_applied", None),
            "explicit_failure": (
                "retry_wait", "failed", "not_started", error_code,
            ),
            "timeout_unknown": (
                "blocked", "unknown", "unknown", error_code,
            ),
        }
        if outcome not in transitions:
            raise ValueError("invalid relation publication outcome")
        event_status, attempt_status, certainty, stored_error = transitions[outcome]
        if outcome == "accepted" and error_code is not None:
            raise ValueError("accepted publication cannot have error")
        if outcome != "accepted" and not error_code:
            raise ValueError("failed publication requires safe error")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            event = connection.execute(
                "SELECT * FROM relation_event_outbox WHERE event_id=?",
                (event_id,),
            ).fetchone()
            attempt = connection.execute(
                "SELECT * FROM relation_event_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if (event is None or event["status"] != "claimed"
                    or event["version"] != expected_version
                    or attempt is None or attempt["event_id"] != event_id
                    or attempt["attempt_number"] != event["attempt_number"]
                    or attempt["status"] != "unknown"):
                raise ValueError("relation event claim is not owned")
            connection.execute("""
                UPDATE relation_event_attempts
                SET status=?, effect_certainty=?, completed_at=?, error_code=?
                WHERE attempt_id=? AND status='unknown'
            """, (
                attempt_status, certainty, timestamp, stored_error, attempt_id,
            ))
            cursor = connection.execute("""
                UPDATE relation_event_outbox
                SET status=?, last_error_code=?, available_at=?,
                    version=version+1, updated_at=?
                WHERE event_id=? AND status='claimed' AND version=?
            """, (
                event_status, stored_error, available_at, timestamp,
                event_id, expected_version,
            ))
            row = connection.execute(
                "SELECT * FROM relation_event_outbox WHERE event_id=?",
                (event_id,),
            ).fetchone()
        if cursor.rowcount != 1:
            raise ValueError("relation event finalize conflict")
        return self._relation_event_from(row)

    def recover_relation_event(self, event_id, expected_version, action,
                               timestamp):
        if action not in {"release_not_started", "block_unknown"}:
            raise ValueError("unsafe relation event recovery action")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            event = connection.execute(
                "SELECT * FROM relation_event_outbox WHERE event_id=?",
                (event_id,),
            ).fetchone()
            attempt = connection.execute("""
                SELECT * FROM relation_event_attempts WHERE event_id=?
                ORDER BY attempt_number DESC LIMIT 1
            """, (event_id,)).fetchone()
            expected_attempt_status = (
                "prepared" if action == "release_not_started" else "unknown"
            )
            if (event is None or event["status"] != "claimed"
                    or event["version"] != expected_version
                    or attempt is None
                    or attempt["attempt_number"] != event["attempt_number"]
                    or attempt["status"] != expected_attempt_status):
                raise ValueError("relation event recovery truth changed")
            if action == "release_not_started":
                status, code, attempt_code = (
                    "retry_wait", "PUBLISH_NOT_STARTED", "PUBLISH_NOT_STARTED",
                )
                connection.execute("""
                    UPDATE relation_event_attempts
                    SET status='failed', effect_certainty='not_started',
                        completed_at=?, error_code=? WHERE attempt_id=?
                """, (timestamp, attempt_code, attempt["attempt_id"]))
            else:
                status, code = "blocked", "PUBLICATION_UNKNOWN"
                connection.execute("""
                    UPDATE relation_event_attempts
                    SET completed_at=?, error_code=? WHERE attempt_id=?
                """, (timestamp, code, attempt["attempt_id"]))
            cursor = connection.execute("""
                UPDATE relation_event_outbox
                SET status=?, last_error_code=?, available_at=?,
                    version=version+1, updated_at=?
                WHERE event_id=? AND status='claimed' AND version=?
            """, (status, code, timestamp, timestamp, event_id, expected_version))
            row = connection.execute(
                "SELECT * FROM relation_event_outbox WHERE event_id=?",
                (event_id,),
            ).fetchone()
        if cursor.rowcount != 1:
            raise ValueError("relation event recovery conflict")
        return self._relation_event_from(row)

    def get_subscription_activation(self, activation_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM subscription_activations WHERE activation_id=?",
                (activation_id,),
            ).fetchone()
        return self._subscription_activation_from(row)

    @staticmethod
    def _subscription_payload(subscription):
        payload = asdict(subscription)
        payload["focus_topics"] = list(subscription.focus_topics)
        return payload

    @staticmethod
    def _subscription_from(payload):
        payload = dict(payload)
        payload["focus_topics"] = tuple(payload["focus_topics"])
        return Subscription(**payload)

    def save_subscription(self, subscription):
        payload = self._subscription_payload(subscription)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.connect() as connection:
            connection.execute("""
                INSERT INTO subscriptions(
                    subscription_id, user_id, payload_json, version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                subscription.subscription_id, subscription.user_id, encoded,
                subscription.version, subscription.created_at,
                subscription.updated_at,
            ))

    def get_subscription(self, subscription_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM subscriptions WHERE subscription_id=?",
                (subscription_id,),
            ).fetchone()
        return self._subscription_from(json.loads(row[0])) if row else None

    def subscription_belongs_to_user(self, subscription_id, user_id):
        with self.connect() as connection:
            row = connection.execute("""
                SELECT
                    CASE
                        WHEN a.subscription_id IS NULL THEN s.user_id=?
                        ELSE EXISTS(
                            SELECT 1 FROM user_subscriptions u
                            WHERE u.subscription_id=s.subscription_id
                              AND u.user_id=?
                        )
                    END AS owned
                FROM subscriptions s
                LEFT JOIN subscription_aggregates a
                  ON a.subscription_id=s.subscription_id
                WHERE s.subscription_id=?
            """, (user_id, user_id, subscription_id)).fetchone()
        return bool(row["owned"]) if row is not None else False

    def list_subscriptions(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM subscriptions ORDER BY created_at, subscription_id"
            ).fetchall()
        return tuple(self._subscription_from(json.loads(row[0])) for row in rows)

    def list_subscriptions_for_user(self, user_id):
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT s.payload_json FROM subscriptions s
                LEFT JOIN subscription_aggregates a
                  ON a.subscription_id=s.subscription_id
                LEFT JOIN user_subscriptions u
                  ON u.subscription_id=s.subscription_id
                WHERE (a.subscription_id IS NULL AND s.user_id=?)
                   OR (a.subscription_id IS NOT NULL AND u.user_id=?)
                ORDER BY s.created_at, s.subscription_id
            """, (user_id, user_id)).fetchall()
        return tuple(self._subscription_from(json.loads(row[0])) for row in rows)

    def update_subscription(self, subscription, expected_version):
        payload = self._subscription_payload(subscription)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.connect() as connection:
            temporal_row = connection.execute("""
                SELECT * FROM condition_temporal_states WHERE subscription_id=?
            """, (subscription.subscription_id,)).fetchone()
            temporal = self._condition_temporal_from(temporal_row)
            event_row = connection.execute("""
                SELECT * FROM event_temporal_states WHERE subscription_id=?
            """, (subscription.subscription_id,)).fetchone()
            event_temporal = self._event_temporal_from(event_row)
            if (temporal is not None
                    and temporal.lifecycle_status == "COMPLETED"
                    and subscription.enabled):
                raise ValueError("condition_subscription_completed")
            cursor = connection.execute("""
                UPDATE subscriptions SET payload_json=?, version=?, updated_at=?
                WHERE subscription_id=? AND user_id=? AND version=?
            """, (
                encoded, subscription.version, subscription.updated_at,
                subscription.subscription_id, subscription.user_id,
                expected_version,
            ))
            if cursor.rowcount == 1:
                if temporal is not None and not subscription.enabled:
                    connection.execute("""
                        UPDATE condition_temporal_states
                        SET lifecycle_status='PAUSED', next_due_at=NULL,
                            paused_at=?, version=version+1, updated_at=?
                        WHERE subscription_id=? AND lifecycle_status='ACTIVE'
                    """, (
                        subscription.updated_at, subscription.updated_at,
                        subscription.subscription_id,
                    ))
                    connection.execute("""
                        UPDATE condition_observation_cycles
                        SET status='SUPERSEDED', claim_token=NULL, updated_at=?
                        WHERE subscription_id=?
                          AND status IN ('PENDING', 'STARTED')
                    """, (
                        subscription.updated_at, subscription.subscription_id,
                    ))
                    connection.execute("""
                        UPDATE condition_observation_requests
                        SET status='FAILED',
                            failure_code='INVALID_OBSERVATION', updated_at=?
                        WHERE subscription_id=? AND status='PENDING'
                    """, (
                        subscription.updated_at, subscription.subscription_id,
                    ))
                elif (temporal is not None and subscription.enabled
                      and temporal.lifecycle_status == "PAUSED"):
                    now = datetime.fromisoformat(
                        subscription.updated_at.replace("Z", "+00:00"),
                    ).astimezone(timezone.utc)
                    anchor = datetime.fromisoformat(
                        temporal.schedule_anchor_at.replace("Z", "+00:00"),
                    ).astimezone(timezone.utc)
                    elapsed = max(0.0, (now - anchor).total_seconds())
                    slots = int(elapsed // temporal.cadence_seconds) + 1
                    next_due = utc_timestamp(
                        anchor + slots * timedelta(
                            seconds=temporal.cadence_seconds,
                        )
                    )
                    cycle_id = condition_cycle_identity(
                        temporal.subscription_id,
                        temporal.execution_policy_version,
                        subscription.updated_at, "RESUME",
                    )
                    request_id = hashlib.sha256(
                        f"condition-cycle-request\n{cycle_id}".encode("utf-8"),
                    ).hexdigest()[:32]
                    request = ConditionObservationRequest(
                        request_id, temporal.subscription_id,
                        temporal.definition_id, temporal.definition_version,
                        f"condition-cycle:{cycle_id}", "PENDING", None, None,
                        subscription.updated_at, subscription.updated_at,
                    )
                    cycle = ConditionObservationCycle(
                        cycle_id, request_id, temporal.subscription_id,
                        temporal.definition_id, temporal.definition_version,
                        temporal.execution_policy_version, "RESUME",
                        subscription.updated_at, subscription.updated_at,
                        subscription.updated_at, 1, "PENDING", None, None,
                        None, None, None, None, None, None, None,
                        subscription.updated_at, subscription.updated_at,
                    )
                    self._insert_condition_request_cycle(
                        connection, request, cycle,
                    )
                    connection.execute("""
                        UPDATE condition_temporal_states
                        SET lifecycle_status='ACTIVE', next_due_at=?,
                            paused_at=NULL, version=version+1, updated_at=?
                        WHERE subscription_id=? AND lifecycle_status='PAUSED'
                    """, (
                        next_due, subscription.updated_at,
                        subscription.subscription_id,
                    ))
                if event_temporal is not None and not subscription.enabled:
                    connection.execute("""
                        UPDATE event_temporal_states
                        SET lifecycle_status='PAUSED', next_due_at=NULL,
                            paused_at=?, version=version+1, updated_at=?
                        WHERE subscription_id=? AND lifecycle_status='ACTIVE'
                    """, (
                        subscription.updated_at, subscription.updated_at,
                        subscription.subscription_id,
                    ))
                    connection.execute("""
                        UPDATE event_observation_cycles
                        SET status='SUPERSEDED', claim_token=NULL,
                            claimed_at=NULL, updated_at=?
                        WHERE subscription_id=?
                          AND status IN ('PENDING','STARTED')
                    """, (
                        subscription.updated_at, subscription.subscription_id,
                    ))
                elif (event_temporal is not None and subscription.enabled
                      and event_temporal.lifecycle_status == "PAUSED"):
                    now = datetime.fromisoformat(
                        subscription.updated_at.replace("Z", "+00:00"),
                    ).astimezone(timezone.utc)
                    anchor = datetime.fromisoformat(
                        event_temporal.schedule_anchor_at.replace(
                            "Z", "+00:00",
                        ),
                    ).astimezone(timezone.utc)
                    elapsed = max(0.0, (now - anchor).total_seconds())
                    slots = int(elapsed // event_temporal.cadence_seconds) + 1
                    next_due = utc_timestamp(
                        anchor + slots * timedelta(
                            seconds=event_temporal.cadence_seconds,
                        )
                    )
                    cycle_id = event_cycle_identity(
                        event_temporal.subscription_id,
                        event_temporal.execution_policy_version,
                        subscription.updated_at, "RESUME",
                    )
                    cycle = EventObservationCycle(
                        cycle_id, event_temporal.subscription_id,
                        event_temporal.definition_id,
                        event_temporal.definition_version,
                        event_temporal.execution_policy_version, "RESUME",
                        subscription.updated_at, subscription.updated_at,
                        subscription.updated_at, 1, subscription.updated_at,
                        subscription.updated_at, "PENDING",
                        event_harness_run_identity(cycle_id), None, None,
                        None, None, None, None, None, None, None, None, None,
                        subscription.updated_at, subscription.updated_at,
                    )
                    connection.execute("""
                        INSERT INTO event_observation_cycles(
                            cycle_id, subscription_id, definition_id,
                            definition_version, execution_policy_version,
                            cycle_kind, scheduled_due_at, coalesced_from_at,
                            coalesced_to_at, coalesced_count, window_start_at,
                            window_end_at, status, harness_run_id, claim_token,
                            claimed_at, observation_id, candidate_id,
                            verification_id, outcome, reason_code, event_id,
                            update_id, distribution_id, failure_code,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, tuple(asdict(cycle).values()))
                    connection.execute("""
                        UPDATE event_temporal_states
                        SET lifecycle_status='ACTIVE', next_due_at=?,
                            verified_through=?, paused_at=NULL,
                            version=version+1, updated_at=?
                        WHERE subscription_id=? AND lifecycle_status='PAUSED'
                    """, (
                        next_due, subscription.updated_at,
                        subscription.updated_at, subscription.subscription_id,
                    ))
                product_status = "ACTIVE" if subscription.enabled else "DISABLED"
                connection.execute("""
                    UPDATE subscription_aggregates SET status=?, updated_at=?
                    WHERE subscription_id=?
                """, (
                    product_status, subscription.updated_at,
                    subscription.subscription_id,
                ))
                connection.execute("""
                    UPDATE user_subscriptions SET status=?, updated_at=?
                    WHERE subscription_id=?
                """, (
                    product_status, subscription.updated_at,
                    subscription.subscription_id,
                ))
        return cursor.rowcount == 1

    @staticmethod
    def _run_from(row):
        return DigestRunRecord(
            digest_run_id=row["digest_run_id"],
            subscription_id=row["subscription_id"], period_key=row["period_key"],
            harness_run_id=row["harness_run_id"], status=row["status"],
            reason=row["reason"], digest_id=row["digest_id"],
            artifact_id=row["artifact_id"],
            harness_result=(json.loads(row["harness_result_json"])
                            if row["harness_result_json"] else None),
            profile_version=row["profile_version"],
            profile_projection_id=row["profile_projection_id"],
            profile_projection=(json.loads(row["profile_projection_json"])
                                if row["profile_projection_json"] else None),
            idempotency_key=row["idempotency_key"],
            subscription_version=row["subscription_version"],
            subscription_snapshot=(json.loads(row["subscription_snapshot_json"])
                                   if row["subscription_snapshot_json"] else None),
            harness_bound_at=row["harness_bound_at"],
            started_at=row["started_at"], updated_at=row["updated_at"],
            failure_stage=row["failure_stage"],
            failure_code=row["failure_code"],
            failure_subtype=(
                row["generation_failure_subtype"]
                if (row["failure_stage"] == "generation"
                    and row["generation_failure_subtype"] is not None)
                else row["failure_subtype"]
            ),
            failure_diagnostics=(
                json.loads(row["failure_diagnostics_json"])
                if row["failure_diagnostics_json"] else None
            ),
            definition_id=row["definition_id"],
            definition_version=row["definition_version"],
        )

    def reserve_digest_run(self, record):
        with self.connect() as connection:
            cursor = connection.execute("""
                INSERT OR IGNORE INTO digest_runs(
                    digest_run_id, subscription_id, period_key,
                    harness_run_id, status, profile_version,
                    profile_projection_id, profile_projection_json,
                    idempotency_key, subscription_version,
                    subscription_snapshot_json, updated_at,
                    definition_id, definition_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.digest_run_id, record.subscription_id,
                record.period_key, record.harness_run_id, record.status,
                record.profile_version, record.profile_projection_id,
                (json.dumps(record.profile_projection, ensure_ascii=False,
                            sort_keys=True)
                 if record.profile_projection is not None else None),
                record.idempotency_key or record.period_key,
                record.subscription_version,
                (json.dumps(record.subscription_snapshot, ensure_ascii=False,
                            sort_keys=True)
                 if record.subscription_snapshot is not None else None),
                record.updated_at,
                record.definition_id, record.definition_version,
            ))
            row = connection.execute("""
                SELECT * FROM digest_runs
                WHERE subscription_id=? AND idempotency_key=?
            """, (
                record.subscription_id,
                record.idempotency_key or record.period_key,
            )).fetchone()
        return self._run_from(row), cursor.rowcount == 1

    def reserve_first_briefing_run(self, outbox_id, record):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            outbox = connection.execute("""
                SELECT * FROM application_outbox
                WHERE outbox_id=? AND status='claimed'
            """, (outbox_id,)).fetchone()
            if outbox is None or record.digest_run_id != outbox["application_run_id"]:
                raise ValueError("claimed Outbox does not bind application run")
            briefing = connection.execute("""
                SELECT * FROM briefing_reservations
                WHERE application_run_id=?
            """, (record.digest_run_id,)).fetchone()
            product = connection.execute("""
                SELECT * FROM subscription_aggregates WHERE subscription_id=?
            """, (record.subscription_id,)).fetchone()
            relation = connection.execute("""
                SELECT * FROM user_subscriptions WHERE subscription_id=?
            """, (record.subscription_id,)).fetchone()
            definition = connection.execute("""
                SELECT * FROM subscription_definitions
                WHERE definition_id=? AND definition_version=?
            """, (record.definition_id, record.definition_version)).fetchone()
            subscription = connection.execute("""
                SELECT payload_json FROM subscriptions WHERE subscription_id=?
            """, (record.subscription_id,)).fetchone()
            if any(value is None for value in (
                    briefing, product, relation, definition, subscription)):
                raise ValueError("first Briefing canonical refs are incomplete")
            payload = json.loads(subscription["payload_json"])
            definition_snapshot = json.loads(definition["snapshot_json"])
            projected_definition = {
                "topic": payload["topic"], "language": payload["language"],
                "cadence": payload["cadence"],
                "max_chars": payload["max_chars"],
                "max_items": payload["max_items"],
                "focus_topics": payload["focus_topics"],
                "delivery_preference": payload["delivery_channel"],
            }
            definition_execution = {
                name: definition_snapshot[name]
                for name in projected_definition
            }
            expected_snapshot = dict(record.subscription_snapshot or {})
            expected_snapshot["focus_topics"] = list(
                expected_snapshot.get("focus_topics", ()),
            )
            if (briefing["subscription_id"] != record.subscription_id
                    or briefing["definition_id"] != record.definition_id
                    or briefing["definition_version"] != record.definition_version
                    or product["definition_id"] != record.definition_id
                    or product["definition_version"] != record.definition_version
                    or product["status"] != "ACTIVE"
                    or relation["status"] != "ACTIVE"
                    or not payload["enabled"]
                    or definition_execution != projected_definition
                    or expected_snapshot != payload
                    or record.status != "reserved"
                    or record.harness_bound_at is not None
                    or record.idempotency_key
                    != "first-briefing:" + record.digest_run_id):
                raise ValueError("first Briefing run binding mismatch")
            existing = connection.execute(
                "SELECT * FROM digest_runs WHERE digest_run_id=?",
                (record.digest_run_id,),
            ).fetchone()
            if existing is not None:
                if (existing["subscription_id"] != record.subscription_id
                        or existing["harness_run_id"]
                        != briefing["harness_run_id"]):
                    raise ValueError("existing first Briefing run mismatch")
                return self._run_from(existing), False
            if briefing["harness_run_id"] is not None:
                raise ValueError("Briefing binding exists without application run")
            connection.execute("""
                INSERT INTO digest_runs(
                    digest_run_id, subscription_id, period_key,
                    harness_run_id, status, profile_version,
                    profile_projection_id, profile_projection_json,
                    idempotency_key, subscription_version,
                    subscription_snapshot_json, updated_at,
                    definition_id, definition_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.digest_run_id, record.subscription_id,
                record.period_key, record.harness_run_id, record.status,
                record.profile_version, record.profile_projection_id,
                (json.dumps(record.profile_projection, ensure_ascii=False,
                            sort_keys=True)
                 if record.profile_projection is not None else None),
                record.idempotency_key, record.subscription_version,
                json.dumps(record.subscription_snapshot, ensure_ascii=False,
                           sort_keys=True),
                record.updated_at, record.definition_id,
                record.definition_version,
            ))
            cursor = connection.execute("""
                UPDATE briefing_reservations SET harness_run_id=?, updated_at=?
                WHERE application_run_id=? AND harness_run_id IS NULL
            """, (
                record.harness_run_id, record.updated_at,
                record.digest_run_id,
            ))
            row = connection.execute(
                "SELECT * FROM digest_runs WHERE digest_run_id=?",
                (record.digest_run_id,),
            ).fetchone()
            if cursor.rowcount != 1:
                raise ValueError("Briefing Harness binding conflict")
        return self._run_from(row), True

    def bind_digest_run(self, digest_run_id, harness_run_id, timestamp):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute("""
                UPDATE digest_runs SET status='running', harness_bound_at=?,
                    started_at=COALESCE(started_at, ?), updated_at=?
                WHERE digest_run_id=? AND harness_run_id=?
                  AND status='reserved' AND harness_bound_at IS NULL
            """, (timestamp, timestamp, timestamp, digest_run_id, harness_run_id))
            row = connection.execute(
                "SELECT * FROM digest_runs WHERE digest_run_id=?",
                (digest_run_id,),
            ).fetchone()
        if row is None or cursor.rowcount != 1:
            raise ValueError("application run cannot bind Harness run")
        return self._run_from(row)

    def mark_digest_run_recovery_required(self, digest_run_id, reason, timestamp):
        with self.connect() as connection:
            connection.execute("""
                UPDATE digest_runs SET status='recovery_required', reason=?,
                    failure_stage='recovery', failure_code='recovery_required',
                    failure_subtype=NULL, failure_diagnostics_json=NULL,
                    generation_failure_subtype=NULL,
                    updated_at=?
                WHERE digest_run_id=? AND status IN (
                    'reserved', 'running', 'running_recovery', 'recovery_required'
                )
            """, (reason, timestamp, digest_run_id))
            row = connection.execute(
                "SELECT * FROM digest_runs WHERE digest_run_id=?",
                (digest_run_id,),
            ).fetchone()
        return self._run_from(row) if row else None

    @staticmethod
    def _recovery_from(row):
        if row is None:
            return None
        return RecoveryOperationRecord(
            row["operation_id"], row["application_run_id"], row["action"],
            row["status"], row["before_state"], row["after_state"],
            row["requested_at"], row["completed_at"], row["error_code"],
        )

    def reserve_recovery_operation(self, record):
        with self.connect() as connection:
            cursor = connection.execute("""
                INSERT OR IGNORE INTO recovery_operations(
                    operation_id, application_run_id, action, status,
                    before_state, requested_at
                ) VALUES (?, ?, ?, 'started', ?, ?)
            """, (
                record.operation_id, record.application_run_id,
                record.action, record.before_state, record.requested_at,
            ))
            row = connection.execute(
                "SELECT * FROM recovery_operations WHERE operation_id=?",
                (record.operation_id,),
            ).fetchone()
        return self._recovery_from(row), cursor.rowcount == 1

    def finish_recovery_operation(self, record):
        with self.connect() as connection:
            cursor = connection.execute("""
                UPDATE recovery_operations SET status=?, after_state=?,
                    completed_at=?, error_code=?
                WHERE operation_id=? AND status='started'
            """, (
                record.status, record.after_state, record.completed_at,
                record.error_code, record.operation_id,
            ))
            row = connection.execute(
                "SELECT * FROM recovery_operations WHERE operation_id=?",
                (record.operation_id,),
            ).fetchone()
        if row is None or cursor.rowcount != 1:
            raise ValueError("recovery operation cannot finish")
        return self._recovery_from(row)

    def get_recovery_operation(self, operation_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM recovery_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        return self._recovery_from(row)

    @staticmethod
    def _generation_attempt_from(row):
        return GenerationAttemptRecord(
            row["attempt_id"], row["application_run_id"],
            row["attempt_number"], row["status"],
            json.loads(row["request_metadata_json"]),
            (json.loads(row["response_metadata_json"])
             if row["response_metadata_json"] else None),
            row["failure_subtype"], row["started_at"], row["completed_at"],
        )

    def reserve_generation_attempt(self, record):
        encoded = json.dumps(
            record.request_metadata, ensure_ascii=False, sort_keys=True,
        )
        with self.connect() as connection:
            connection.execute("""
                INSERT INTO generation_attempts(
                    attempt_id, application_run_id, attempt_number, status,
                    request_metadata_json, started_at
                ) VALUES (?, ?, ?, 'started', ?, ?)
            """, (
                record.attempt_id, record.application_run_id,
                record.attempt_number, encoded, record.started_at,
            ))
            row = connection.execute(
                "SELECT * FROM generation_attempts WHERE attempt_id=?",
                (record.attempt_id,),
            ).fetchone()
        return self._generation_attempt_from(row)

    def finish_generation_attempt(self, record):
        encoded = json.dumps(
            record.response_metadata, ensure_ascii=False, sort_keys=True,
        ) if record.response_metadata is not None else None
        with self.connect() as connection:
            cursor = connection.execute("""
                UPDATE generation_attempts SET status=?,
                    response_metadata_json=?, failure_subtype=?, completed_at=?
                WHERE attempt_id=? AND status='started'
            """, (
                record.status, encoded, record.failure_subtype,
                record.completed_at, record.attempt_id,
            ))
            row = connection.execute(
                "SELECT * FROM generation_attempts WHERE attempt_id=?",
                (record.attempt_id,),
            ).fetchone()
        if row is None or cursor.rowcount != 1:
            raise ValueError("generation attempt cannot finish")
        return self._generation_attempt_from(row)

    def list_generation_attempts(self, application_run_id):
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT * FROM generation_attempts
                WHERE application_run_id=? ORDER BY attempt_number
            """, (application_run_id,)).fetchall()
        return tuple(self._generation_attempt_from(row) for row in rows)

    def claim_bound_digest_run_recovery(self, digest_run_id, timestamp):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute("""
                UPDATE digest_runs SET status='running_recovery', updated_at=?
                WHERE digest_run_id=? AND status='running'
                  AND harness_bound_at IS NOT NULL
            """, (timestamp, digest_run_id))
            row = connection.execute(
                "SELECT * FROM digest_runs WHERE digest_run_id=?",
                (digest_run_id,),
            ).fetchone()
        if row is None or cursor.rowcount != 1:
            raise ValueError("bound application run recovery already claimed")
        return self._run_from(row)

    def save_candidates(self, digest_run_id, candidates):
        with self.connect() as connection:
            connection.executemany("""
                INSERT INTO content_candidates(
                    digest_run_id, candidate_id, payload_json
                ) VALUES (?, ?, ?)
            """, [(
                digest_run_id, candidate.candidate_id,
                json.dumps(asdict(candidate), ensure_ascii=False, sort_keys=True),
            ) for candidate in candidates])

    def list_content_candidates(self, digest_run_id):
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT payload_json FROM content_candidates
                WHERE digest_run_id=? ORDER BY candidate_id
            """, (digest_run_id,)).fetchall()
        values = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload["topic_tags"] = tuple(payload["topic_tags"])
            values.append(ContentCandidate(**payload))
        return tuple(values)

    def finish_digest_run(self, record, digest=None):
        encoded_result = (
            json.dumps(record.harness_result, ensure_ascii=False, sort_keys=True)
            if record.harness_result is not None else None
        )
        with self.connect() as connection:
            if digest is not None:
                connection.execute("""
                    INSERT INTO digests(
                        digest_id, digest_run_id, subscription_id,
                        harness_run_id, artifact_id, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    digest.digest_id, digest.digest_run_id,
                    digest.subscription_id, digest.harness_run_id,
                    digest.artifact_id,
                    json.dumps(digest.payload, ensure_ascii=False, sort_keys=True),
                    digest.created_at,
                ))
                subscription = connection.execute(
                    "SELECT user_id FROM subscriptions WHERE subscription_id=?",
                    (digest.subscription_id,),
                ).fetchone()
                for item in digest.payload.get("items", []):
                    identity = item.get("content_identity")
                    if identity:
                        connection.execute("""
                            INSERT OR IGNORE INTO seen_content(
                                user_id, content_identity, first_digest_id
                            ) VALUES (?, ?, ?)
                        """, (subscription[0], identity, digest.digest_id))
            connection.execute("""
                UPDATE digest_runs SET
                    status=?, reason=?, digest_id=?, artifact_id=?,
                    harness_result_json=?, updated_at=?, failure_stage=?,
                    failure_code=?, failure_subtype=?,
                    generation_failure_subtype=?,
                    failure_diagnostics_json=?
                WHERE digest_run_id=?
            """, (
                record.status, record.reason, record.digest_id,
                record.artifact_id, encoded_result, record.updated_at,
                record.failure_stage, record.failure_code,
                (record.failure_subtype
                 if record.failure_stage == "contract" else None),
                (record.failure_subtype
                 if record.failure_stage == "generation" else None),
                (json.dumps(record.failure_diagnostics, ensure_ascii=False,
                            sort_keys=True)
                 if record.failure_diagnostics is not None else None),
                record.digest_run_id,
            ))

    def get_digest_run(self, digest_run_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM digest_runs WHERE digest_run_id=?",
                (digest_run_id,),
            ).fetchone()
        return self._run_from(row) if row else None

    def get_digest(self, digest_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM digests WHERE digest_id=?", (digest_id,),
            ).fetchone()
        if row is None:
            return None
        return Digest(
            digest_id=row["digest_id"], digest_run_id=row["digest_run_id"],
            harness_run_id=row["harness_run_id"], artifact_id=row["artifact_id"],
            subscription_id=row["subscription_id"],
            payload=json.loads(row["payload_json"]), created_at=row["created_at"],
        )

    def list_digests(self, user_id, subscription_id=None):
        query = """
            SELECT d.* FROM digests AS d
            JOIN subscriptions AS s ON s.subscription_id=d.subscription_id
            LEFT JOIN subscription_aggregates a
              ON a.subscription_id=s.subscription_id
            LEFT JOIN user_subscriptions u
              ON u.subscription_id=s.subscription_id
            WHERE ((a.subscription_id IS NULL AND s.user_id=?)
                OR (a.subscription_id IS NOT NULL AND u.user_id=?))
        """
        arguments = [user_id, user_id]
        if subscription_id is not None:
            query += " AND d.subscription_id=?"
            arguments.append(subscription_id)
        query += " ORDER BY d.created_at DESC, d.digest_id DESC"
        with self.connect() as connection:
            rows = connection.execute(query, tuple(arguments)).fetchall()
        return tuple(Digest(
            digest_id=row["digest_id"], digest_run_id=row["digest_run_id"],
            harness_run_id=row["harness_run_id"], artifact_id=row["artifact_id"],
            subscription_id=row["subscription_id"],
            payload=json.loads(row["payload_json"]), created_at=row["created_at"],
        ) for row in rows)

    @staticmethod
    def _profile_from_rows(head, weights):
        if head is None:
            return None
        return InterestProfile(
            user_id=head["user_id"], version=head["version"],
            rule_version=head["rule_version"],
            topic_weights=tuple(
                TopicWeight(row["topic_key"], row["weight"]) for row in weights
            ),
            updated_at=head["updated_at"],
        )

    def get_profile(self, user_id):
        with self.connect() as connection:
            head = connection.execute(
                "SELECT * FROM interest_profiles WHERE user_id=?", (user_id,),
            ).fetchone()
            weights = connection.execute("""
                SELECT topic_key, weight FROM profile_topic_weights
                WHERE user_id=? ORDER BY topic_key
            """, (user_id,)).fetchall()
        return self._profile_from_rows(head, weights)

    def get_seen_content(self, user_id):
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT content_identity FROM seen_content
                WHERE user_id=? ORDER BY content_identity
            """, (user_id,)).fetchall()
        return frozenset(row[0] for row in rows)

    @staticmethod
    def _load_profile(connection, user_id):
        head = connection.execute(
            "SELECT * FROM interest_profiles WHERE user_id=?", (user_id,),
        ).fetchone()
        weights = connection.execute("""
            SELECT topic_key, weight FROM profile_topic_weights
            WHERE user_id=? ORDER BY topic_key
        """, (user_id,)).fetchall()
        return SQLiteDigestRepository._profile_from_rows(head, weights)

    def apply_feedback(self, feedback, topic_keys, timestamp):
        """Atomically persist one immutable event and its bounded profile update."""
        normalized_topics = tuple(sorted({
            normalize_topic(item) for item in topic_keys
        }))
        delta = FEEDBACK_DELTAS[feedback.feedback_type]
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT feedback_id FROM interactions WHERE feedback_id=?",
                (feedback.feedback_id,),
            ).fetchone()
            if existing:
                profile = self._load_profile(connection, feedback.user_id)
                update_row = connection.execute(
                    "SELECT * FROM profile_updates WHERE feedback_id=?",
                    (feedback.feedback_id,),
                ).fetchone()
                update = ProfileUpdate(
                    feedback.feedback_id, update_row["before_version"],
                    update_row["after_version"],
                    tuple(tuple(item) for item in json.loads(
                        update_row["changes_json"]
                    )),
                ) if update_row else None
                return FeedbackResult(feedback.feedback_id, False, profile, update)
            profile = self._load_profile(connection, feedback.user_id)
            if profile is None:
                profile = InterestProfile.empty(feedback.user_id, timestamp)
                connection.execute("""
                    INSERT INTO interest_profiles(user_id, version, rule_version, updated_at)
                    VALUES (?, 0, ?, ?)
                """, (feedback.user_id, PROFILE_RULE_VERSION, timestamp))
            before = {item.topic_key: item.weight for item in profile.topic_weights}
            changes = []
            for topic_key in normalized_topics:
                old = before.get(topic_key, 0)
                new = max(
                    PROFILE_WEIGHT_MIN, min(PROFILE_WEIGHT_MAX, old + delta),
                )
                changes.append((topic_key, old, new))
                connection.execute("""
                    INSERT INTO profile_topic_weights(user_id, topic_key, weight)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id, topic_key) DO UPDATE SET weight=excluded.weight
                """, (feedback.user_id, topic_key, new))
            after_version = profile.version + 1
            connection.execute("""
                UPDATE interest_profiles
                SET version=?, rule_version=?, updated_at=? WHERE user_id=?
            """, (
                after_version, PROFILE_RULE_VERSION, timestamp, feedback.user_id,
            ))
            interaction = Interaction(
                feedback.feedback_id, feedback.user_id, feedback.digest_id,
                feedback.item_id, feedback.feedback_type, feedback.event_key,
                normalized_topics, delta, timestamp,
            )
            connection.execute("""
                INSERT INTO interactions(
                    feedback_id, user_id, digest_id, item_id, feedback_type,
                    event_key, topic_keys_json, delta, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                interaction.feedback_id, interaction.user_id,
                interaction.digest_id, interaction.item_id,
                interaction.feedback_type, interaction.event_key,
                json.dumps(interaction.topic_keys, ensure_ascii=False),
                interaction.delta, interaction.created_at,
            ))
            update = ProfileUpdate(
                feedback.feedback_id, profile.version, after_version,
                tuple(changes),
            )
            connection.execute("""
                INSERT INTO profile_updates(
                    feedback_id, before_version, after_version, changes_json
                ) VALUES (?, ?, ?, ?)
            """, (
                update.feedback_id, update.before_version,
                update.after_version,
                json.dumps(update.changes, ensure_ascii=False),
            ))
            result = self._load_profile(connection, feedback.user_id)
        return FeedbackResult(feedback.feedback_id, True, result, update)

    @staticmethod
    def _delivery_select(where):
        return f"""
            SELECT r.delivery_id, r.digest_id, r.distribution_id,
                   r.user_id, r.channel,
                   r.status, r.current_attempt_number AS attempt_number,
                   r.current_attempt_id AS attempt_id,
                   a.provider_message_id, a.requested_at, a.completed_at,
                   a.error_code, a.effect_certainty, a.evidence_id
            FROM delivery_records AS r
            JOIN delivery_attempts AS a
              ON a.attempt_id = r.current_attempt_id
            WHERE {where}
        """

    @staticmethod
    def _delivery_from(row):
        if row is None:
            return None
        return DeliveryRecord(
            delivery_id=row["delivery_id"], attempt_id=row["attempt_id"],
            digest_id=row["digest_id"], user_id=row["user_id"],
            channel=row["channel"], status=row["status"],
            attempt_number=row["attempt_number"],
            provider_message_id=row["provider_message_id"],
            requested_at=row["requested_at"], completed_at=row["completed_at"],
            error_code=row["error_code"],
            effect_certainty=row["effect_certainty"],
            distribution_id=row["distribution_id"],
            evidence_id=row["evidence_id"],
        )

    def reserve_delivery(self, record):
        with self.connect() as connection:
            cursor = connection.execute("""
                INSERT OR IGNORE INTO delivery_records(
                    delivery_id, digest_id, distribution_id, user_id,
                    channel, status,
                    current_attempt_number, current_attempt_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.delivery_id, record.digest_id, record.distribution_id,
                record.user_id, record.channel, record.status,
                record.attempt_number, record.attempt_id,
                record.requested_at, record.requested_at,
            ))
            if cursor.rowcount == 1:
                connection.execute("""
                    INSERT INTO delivery_attempts(
                        attempt_id, delivery_id, attempt_number, status,
                        provider_message_id, requested_at, completed_at,
                        error_code, effect_certainty, evidence_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    record.attempt_id, record.delivery_id,
                    record.attempt_number, record.status,
                    record.provider_message_id, record.requested_at,
                    record.completed_at, record.error_code,
                    record.effect_certainty, record.evidence_id,
                ))
            row = connection.execute(
                self._delivery_select("r.delivery_id=?"),
                (record.delivery_id,),
            ).fetchone()
        stored = self._delivery_from(row)
        if (stored is None or stored.digest_id != record.digest_id
                or stored.distribution_id != record.distribution_id
                or stored.user_id != record.user_id
                or stored.channel != record.channel):
            raise ValueError("delivery identity conflict")
        return stored, cursor.rowcount == 1

    def reserve_delivery_retry(self, previous, record):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                self._delivery_select("r.delivery_id=?"),
                (previous.delivery_id,),
            ).fetchone()
            current = self._delivery_from(current)
            if (current is None or current.attempt_id != previous.attempt_id
                    or current.status != "failed"
                    or current.effect_certainty != "not_started"):
                raise ValueError("delivery is not safely retryable")
            connection.execute("""
                INSERT INTO delivery_attempts(
                    attempt_id, delivery_id, attempt_number, status,
                    provider_message_id, requested_at, completed_at,
                    error_code, effect_certainty, evidence_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.attempt_id, record.delivery_id, record.attempt_number,
                record.status, record.provider_message_id,
                record.requested_at, record.completed_at, record.error_code,
                record.effect_certainty, record.evidence_id,
            ))
            connection.execute("""
                UPDATE delivery_records SET
                    status=?, current_attempt_number=?, current_attempt_id=?,
                    updated_at=? WHERE delivery_id=?
            """, (
                record.status, record.attempt_number, record.attempt_id,
                record.requested_at, record.delivery_id,
            ))
            row = connection.execute(
                self._delivery_select("r.delivery_id=?"),
                (record.delivery_id,),
            ).fetchone()
        return self._delivery_from(row)

    def mark_delivery_dispatch_started(self, delivery_id, attempt_id):
        with self.connect() as connection:
            cursor = connection.execute("""
                UPDATE delivery_attempts SET status='unknown',
                    effect_certainty='unknown'
                WHERE delivery_id=? AND attempt_id=?
                  AND status='pending' AND effect_certainty='not_started'
            """, (delivery_id, attempt_id))
            if cursor.rowcount != 1:
                raise ValueError("delivery attempt is not pending")
            connection.execute("""
                UPDATE delivery_records SET status='unknown'
                WHERE delivery_id=? AND current_attempt_id=?
            """, (delivery_id, attempt_id))
            row = connection.execute(
                self._delivery_select("r.delivery_id=?"), (delivery_id,),
            ).fetchone()
        return self._delivery_from(row)

    def finish_delivery(self, record):
        if record.status not in {"accepted", "failed", "unknown"}:
            raise ValueError("delivery terminal status required")
        with self.connect() as connection:
            cursor = connection.execute("""
                UPDATE delivery_attempts SET
                    status=?, provider_message_id=?, completed_at=?,
                    error_code=?, effect_certainty=?, evidence_id=?
                WHERE delivery_id=? AND attempt_id=? AND status='unknown'
            """, (
                record.status, record.provider_message_id,
                record.completed_at, record.error_code,
                record.effect_certainty, record.evidence_id,
                record.delivery_id,
                record.attempt_id,
            ))
            if cursor.rowcount != 1:
                raise ValueError("delivery attempt cannot be finalized")
            connection.execute("""
                UPDATE delivery_records SET status=?, updated_at=?
                WHERE delivery_id=? AND current_attempt_id=?
            """, (
                record.status, record.completed_at or record.requested_at,
                record.delivery_id, record.attempt_id,
            ))
            row = connection.execute(
                self._delivery_select("r.delivery_id=?"),
                (record.delivery_id,),
            ).fetchone()
        return self._delivery_from(row)

    def get_delivery(self, delivery_id):
        with self.connect() as connection:
            row = connection.execute(
                self._delivery_select("r.delivery_id=?"), (delivery_id,),
            ).fetchone()
        return self._delivery_from(row)

    def get_delivery_for_digest(self, digest_id, channel):
        with self.connect() as connection:
            row = connection.execute(
                self._delivery_select("r.digest_id=? AND r.channel=?"),
                (digest_id, channel),
            ).fetchone()
        return self._delivery_from(row)

    def get_delivery_for_distribution(self, distribution_id, channel):
        with self.connect() as connection:
            row = connection.execute(
                self._delivery_select(
                    "r.distribution_id=? AND r.channel=?",
                ),
                (distribution_id, channel),
            ).fetchone()
        return self._delivery_from(row)
