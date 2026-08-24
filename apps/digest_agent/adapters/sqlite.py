"""Teaching-grade stdlib SQLite repositories for the current vertical slice."""

from dataclasses import asdict
from contextlib import contextmanager
import json
import sqlite3

from ..domain import (
    FEEDBACK_DELTAS, PROFILE_RULE_VERSION, PROFILE_WEIGHT_MAX,
    PROFILE_WEIGHT_MIN, ApplicationOutbox, BriefingReservation, Conversation,
    ConversationTurn, DefinitionOutcome, DeliveryRecord, Digest,
    FeedbackResult, InterestProfile, Interaction, ProductSubscription,
    ProfileUpdate, Subscription, SubscriptionActivation, SubscriptionCommit,
    SubscriptionDefinition, TopicWeight, UserSubscription, normalize_topic,
)
from ..repositories import (
    DefinitionAttemptRecord, DigestRunRecord, GenerationAttemptRecord,
    RecoveryOperationRecord,
)


SCHEMA_VERSION = 12


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
            self._briefing_reservation_from(briefing_row),
            self._application_outbox_from(outbox_row), activation, reused,
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
        briefing = proposed.briefing
        outbox = proposed.outbox
        activation = proposed.activation
        new_ids = {
            definition.definition_id, subscription.subscription_id,
            relation.user_subscription_id, briefing.application_run_id,
            outbox.outbox_id, activation.activation_id,
        }
        if len(new_ids) != 6 or new_ids & {
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
        if (subscription.user_id != user_id
                or subscription.natural_language_request != first_turn["safe_text"]
                or not subscription.enabled
                or expected_subscription != definition.snapshot):
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
            proposed.subscription, proposed.relation, proposed.briefing,
            proposed.outbox, proposed.activation, False,
        )

    def get_subscription_commit_for_outcome(self, outcome_id):
        with self.connect() as connection:
            return self._commit_from_connection(connection, outcome_id, True)

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
            cursor = connection.execute("""
                UPDATE subscriptions SET payload_json=?, version=?, updated_at=?
                WHERE subscription_id=? AND user_id=? AND version=?
            """, (
                encoded, subscription.version, subscription.updated_at,
                subscription.subscription_id, subscription.user_id,
                expected_version,
            ))
            if cursor.rowcount == 1:
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
                    or definition_snapshot != projected_definition
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
            SELECT r.delivery_id, r.digest_id, r.user_id, r.channel,
                   r.status, r.current_attempt_number AS attempt_number,
                   r.current_attempt_id AS attempt_id,
                   a.provider_message_id, a.requested_at, a.completed_at,
                   a.error_code, a.effect_certainty
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
        )

    def reserve_delivery(self, record):
        with self.connect() as connection:
            cursor = connection.execute("""
                INSERT OR IGNORE INTO delivery_records(
                    delivery_id, digest_id, user_id, channel, status,
                    current_attempt_number, current_attempt_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.delivery_id, record.digest_id, record.user_id,
                record.channel, record.status, record.attempt_number,
                record.attempt_id, record.requested_at, record.requested_at,
            ))
            if cursor.rowcount == 1:
                connection.execute("""
                    INSERT INTO delivery_attempts(
                        attempt_id, delivery_id, attempt_number, status,
                        provider_message_id, requested_at, completed_at,
                        error_code, effect_certainty
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    record.attempt_id, record.delivery_id,
                    record.attempt_number, record.status,
                    record.provider_message_id, record.requested_at,
                    record.completed_at, record.error_code,
                    record.effect_certainty,
                ))
            row = connection.execute(
                self._delivery_select("r.digest_id=? AND r.channel=?"),
                (record.digest_id, record.channel),
            ).fetchone()
        return self._delivery_from(row), cursor.rowcount == 1

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
                    error_code, effect_certainty
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.attempt_id, record.delivery_id, record.attempt_number,
                record.status, record.provider_message_id,
                record.requested_at, record.completed_at, record.error_code,
                record.effect_certainty,
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
                    error_code=?, effect_certainty=?
                WHERE delivery_id=? AND attempt_id=? AND status='unknown'
            """, (
                record.status, record.provider_message_id,
                record.completed_at, record.error_code,
                record.effect_certainty, record.delivery_id,
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
