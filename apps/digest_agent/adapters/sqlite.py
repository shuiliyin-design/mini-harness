"""Teaching-grade stdlib SQLite repositories for the current vertical slice."""

from dataclasses import asdict
from contextlib import contextmanager
import json
import sqlite3

from ..domain import (
    FEEDBACK_DELTAS, PROFILE_RULE_VERSION, PROFILE_WEIGHT_MAX,
    PROFILE_WEIGHT_MIN, DeliveryRecord, Digest, FeedbackResult,
    InterestProfile, Interaction, ProfileUpdate, Subscription, TopicWeight,
    normalize_topic,
)
from ..repositories import (
    DigestRunRecord, GenerationAttemptRecord, RecoveryOperationRecord,
)


SCHEMA_VERSION = 8


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

    def list_subscriptions(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM subscriptions ORDER BY created_at, subscription_id"
            ).fetchall()
        return tuple(self._subscription_from(json.loads(row[0])) for row in rows)

    def list_subscriptions_for_user(self, user_id):
        with self.connect() as connection:
            rows = connection.execute("""
                SELECT payload_json FROM subscriptions
                WHERE user_id=? ORDER BY created_at, subscription_id
            """, (user_id,)).fetchall()
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
            failure_subtype=row["failure_subtype"],
            failure_diagnostics=(
                json.loads(row["failure_diagnostics_json"])
                if row["failure_diagnostics_json"] else None
            ),
        )

    def reserve_digest_run(self, record):
        with self.connect() as connection:
            cursor = connection.execute("""
                INSERT OR IGNORE INTO digest_runs(
                    digest_run_id, subscription_id, period_key,
                    harness_run_id, status, profile_version,
                    profile_projection_id, profile_projection_json,
                    idempotency_key, subscription_version,
                    subscription_snapshot_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ))
            row = connection.execute("""
                SELECT * FROM digest_runs
                WHERE subscription_id=? AND idempotency_key=?
            """, (
                record.subscription_id,
                record.idempotency_key or record.period_key,
            )).fetchone()
        return self._run_from(row), cursor.rowcount == 1

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
                    failure_diagnostics_json=?
                WHERE digest_run_id=?
            """, (
                record.status, record.reason, record.digest_id,
                record.artifact_id, encoded_result, record.updated_at,
                record.failure_stage, record.failure_code,
                record.failure_subtype,
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
            WHERE s.user_id=?
        """
        arguments = [user_id]
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
