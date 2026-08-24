from dataclasses import asdict
import hashlib
import json
import tempfile
import sqlite3
import unittest

from apps.digest_agent.adapters.sqlite import SCHEMA_VERSION, SQLiteDigestRepository
from apps.digest_agent.domain import Subscription, definition_candidate_identity
from apps.digest_agent.repositories import DigestRunRecord
from apps.digest_agent.services import SubscriptionService


NOW = "2026-08-23T12:00:00Z"


def canonical_identity(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def create_v11_history_fixture(path):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("""
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
        )
    """)
    for version in range(1, 12):
        getattr(SQLiteDigestRepository, f"_migrate_v{version}")(
            connection,
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?, ?)", (version, NOW),
        )
    ids = {
        "subscription": "1" * 32, "user": "2" * 32,
        "run": "3" * 32, "harness": "4" * 32,
        "digest": "5" * 32, "artifact": "6" * 32,
        "conversation": "7" * 32, "turn": "8" * 32,
        "outcome": "9" * 32, "definition": "a" * 32,
        "relation": "b" * 32, "outbox": "c" * 32,
        "activation": "d" * 32, "delivery": "e" * 32,
        "delivery_attempt": "f" * 32, "feedback": "0" * 32,
    }
    definition = {
        "topic": "AI", "language": "zh-CN", "cadence": "daily",
        "max_chars": 600, "max_items": 5, "focus_topics": ["Agent"],
        "delivery_preference": "none",
    }
    outcome = {
        "protocol_version": 1, "type": "DONE", "definition": definition,
    }
    subscription = Subscription(
        ids["subscription"], ids["user"], "AI", "订阅 AI", "daily",
        "zh-CN", 600, 5, ("Agent",), "none", True, 1, NOW, NOW,
    )
    subscription_payload = asdict(subscription)
    subscription_payload["focus_topics"] = ["Agent"]
    refs = {
        "activation_id": ids["activation"],
        "definition_id": ids["definition"], "definition_version": 1,
        "application_run_id": ids["run"],
    }
    connection.execute("""
        INSERT INTO conversations VALUES (?, ?, 'DEFINITION_ACCEPTED', 1, 2,
            'fixture-start', NULL, ?, ?)
    """, (ids["conversation"], ids["user"], NOW, NOW))
    connection.execute("""
        INSERT INTO conversation_turns VALUES (
            ?, ?, 1, 'user', '订阅 AI', 'fixture-turn', ?, 'completed',
            ?, NULL, NULL, ?, ?
        )
    """, (
        ids["turn"], ids["conversation"], ids["harness"], ids["outcome"],
        NOW, NOW,
    ))
    connection.execute("""
        INSERT INTO definition_outcomes VALUES (?, ?, ?, 'DONE', ?, ?, ?)
    """, (
        ids["outcome"], ids["conversation"], ids["turn"],
        json.dumps(outcome, ensure_ascii=False),
        definition_candidate_identity(outcome), NOW,
    ))
    connection.execute("""
        INSERT INTO subscriptions VALUES (?, ?, ?, 1, ?, ?)
    """, (
        ids["subscription"], ids["user"],
        json.dumps(subscription_payload, ensure_ascii=False), NOW, NOW,
    ))
    connection.execute("""
        INSERT INTO subscription_definitions VALUES (?, 1, ?, ?, ?, ?, ?)
    """, (
        ids["definition"], ids["conversation"], ids["outcome"],
        json.dumps(definition, ensure_ascii=False),
        canonical_identity(definition), NOW,
    ))
    connection.execute("""
        INSERT INTO subscription_aggregates VALUES (?, ?, 1, 'ACTIVE', ?, ?)
    """, (ids["subscription"], ids["definition"], NOW, NOW))
    connection.execute("""
        INSERT INTO user_subscriptions VALUES (?, ?, ?, 'ACTIVE', ?, ?)
    """, (ids["relation"], ids["user"], ids["subscription"], NOW, NOW))
    connection.execute("""
        INSERT INTO briefing_reservations VALUES (?, ?, ?, 1, 'PENDING', ?, ?, ?)
    """, (
        ids["run"], ids["subscription"], ids["definition"],
        ids["harness"], NOW, NOW,
    ))
    connection.execute("""
        INSERT INTO application_outbox VALUES (
            ?, 'FIRST_BRIEFING_REQUESTED', ?, ?, ?, ?, 'completed', 1,
            ?, ?, NULL, 2, ?
        )
    """, (
        ids["outbox"], ids["subscription"], ids["run"],
        json.dumps(refs), canonical_identity(refs), NOW, NOW, NOW,
    ))
    connection.execute("""
        INSERT INTO subscription_activations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ids["activation"], ids["conversation"], ids["outcome"],
        ids["definition"], ids["subscription"], ids["relation"],
        ids["run"], ids["outbox"], NOW,
    ))
    connection.execute("""
        INSERT INTO digest_runs(
            digest_run_id, subscription_id, period_key, harness_run_id,
            status, digest_id, artifact_id, harness_result_json,
            profile_version, idempotency_key, subscription_version,
            subscription_snapshot_json, harness_bound_at, started_at,
            updated_at, definition_id, definition_version
        ) VALUES (?, ?, 'first', ?, 'completed', ?, ?, ?, 1, 'first', 1,
                  ?, ?, ?, ?, ?, 1)
    """, (
        ids["run"], ids["subscription"], ids["harness"], ids["digest"],
        ids["artifact"], json.dumps({"status": "completed"}),
        json.dumps(subscription_payload, ensure_ascii=False), NOW, NOW, NOW,
        ids["definition"],
    ))
    connection.execute("""
        INSERT INTO digests VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        ids["digest"], ids["run"], ids["subscription"], ids["harness"],
        ids["artifact"], json.dumps({"rendered_text": "fixture"}), NOW,
    ))
    connection.execute("""
        INSERT INTO delivery_records VALUES (?, ?, ?, 'fake', 'accepted', 1, ?, ?, ?)
    """, (
        ids["delivery"], ids["digest"], ids["user"],
        ids["delivery_attempt"], NOW, NOW,
    ))
    connection.execute("""
        INSERT INTO delivery_attempts VALUES (?, ?, 1, 'accepted', 'fixture-ref',
            ?, ?, NULL, 'known_applied')
    """, (ids["delivery_attempt"], ids["delivery"], NOW, NOW))
    connection.execute(
        "INSERT INTO interest_profiles VALUES (?, 1, 1, ?)",
        (ids["user"], NOW),
    )
    connection.execute(
        "INSERT INTO profile_topic_weights VALUES (?, 'agent', 3)",
        (ids["user"],),
    )
    connection.execute("""
        INSERT INTO interactions VALUES (?, ?, ?, NULL, 'opened', 'fixture',
            '[]', 1, ?)
    """, (ids["feedback"], ids["user"], ids["digest"], NOW))
    connection.execute(
        "INSERT INTO profile_updates VALUES (?, 0, 1, '[]')",
        (ids["feedback"],),
    )
    connection.commit()
    connection.close()
    return ids


class SQLiteRepositoryTests(unittest.TestCase):
    def test_v11_to_latest_preserves_all_historical_aggregates_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            path = f"{root}/digest.db"
            ids = create_v11_history_fixture(path)
            tracked = (
                "subscriptions", "conversations", "conversation_turns",
                "definition_outcomes", "subscription_definitions",
                "subscription_aggregates", "user_subscriptions",
                "briefing_reservations", "application_outbox",
                "subscription_activations", "digest_runs", "digests",
                "delivery_records", "delivery_attempts",
                "interest_profiles", "profile_topic_weights",
                "interactions", "profile_updates",
            )
            before = sqlite3.connect(path)
            counts = {
                table: before.execute(
                    f"SELECT COUNT(*) FROM {table}",
                ).fetchone()[0] for table in tracked
            }
            before.close()

            repository = SQLiteDigestRepository(path)
            self.assertEqual(
                repository.get_conversation(ids["conversation"]).conversation_id,
                ids["conversation"],
            )
            self.assertEqual(
                repository.get_definition_outcome(ids["outcome"]).outcome_id,
                ids["outcome"],
            )
            self.assertEqual(
                repository.get_subscription(ids["subscription"]).subscription_id,
                ids["subscription"],
            )
            self.assertEqual(
                repository.get_subscription_definition(
                    ids["definition"], 1,
                ).definition_id,
                ids["definition"],
            )
            self.assertEqual(
                repository.get_user_subscription_for_subscription(
                    ids["subscription"],
                ).user_subscription_id,
                ids["relation"],
            )
            self.assertEqual(
                repository.get_application_outbox_for_run(
                    ids["run"],
                ).outbox_id,
                ids["outbox"],
            )
            self.assertEqual(repository.get_digest(
                ids["digest"],
            ).digest_id, ids["digest"])
            self.assertEqual(repository.get_delivery(
                ids["delivery"],
            ).delivery_id, ids["delivery"])
            self.assertEqual(repository.get_profile(
                ids["user"],
            ).version, 1)
            repository.migrate()
            with repository.connect() as connection:
                after = {
                    table: connection.execute(
                        f"SELECT COUNT(*) FROM {table}",
                    ).fetchone()[0] for table in tracked
                }
                ledger = connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version",
                ).fetchall()
                attempts = connection.execute(
                    "SELECT COUNT(*) FROM definition_attempts",
                ).fetchone()[0]
                relation_events = connection.execute(
                    "SELECT COUNT(*) FROM relation_event_outbox",
                ).fetchone()[0]
                unique_indexes = [row for row in connection.execute(
                    "PRAGMA index_list(definition_attempts)",
                ) if row[2] == 1]
                foreign_keys = connection.execute(
                    "PRAGMA foreign_key_check",
                ).fetchall()
            self.assertEqual(after, counts)
            self.assertEqual([row[0] for row in ledger], list(range(1, 14)))
            self.assertEqual(attempts, 0)
            self.assertEqual(relation_events, 0)
            self.assertTrue(unique_indexes)
            self.assertEqual(foreign_keys, [])

    def test_v12_partial_ddl_and_ledger_are_rolled_back_together(self):
        for target in (
                "after_failure_stage", "after_failure_subtype",
                "after_definition_attempts"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as root:
                path = f"{root}/digest.db"
                ids = create_v11_history_fixture(path)
                connection = sqlite3.connect(path)

                def fail(stage):
                    if stage == target:
                        raise RuntimeError("synthetic migration failure")

                with self.assertRaisesRegex(RuntimeError, "migration failure"):
                    SQLiteDigestRepository._apply_v12(
                        connection, fault_injector=fail,
                    )
                columns = {row[1] for row in connection.execute(
                    "PRAGMA table_info(conversation_turns)",
                )}
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'",
                )}
                ledger = [row[0] for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version",
                )]
                subscription_id = connection.execute(
                    "SELECT subscription_id FROM subscriptions",
                ).fetchone()[0]
                connection.close()
                self.assertNotIn("failure_stage", columns)
                self.assertNotIn("failure_subtype", columns)
                self.assertNotIn("definition_attempts", tables)
                self.assertEqual(ledger, list(range(1, 12)))
                self.assertEqual(subscription_id, ids["subscription"])
                repository = SQLiteDigestRepository(path)
                self.assertEqual(
                    repository.get_subscription(ids["subscription"]).subscription_id,
                    ids["subscription"],
                )

    def test_v13_partial_ddl_and_ledger_are_rolled_back_together(self):
        for target in (
                "after_relation_event_outbox",
                "after_relation_event_attempts"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as root:
                path = f"{root}/digest.db"
                ids = create_v11_history_fixture(path)
                repository = SQLiteDigestRepository(path)
                with repository.connect() as connection:
                    connection.execute(
                        "DELETE FROM schema_migrations WHERE version=13",
                    )
                    connection.execute("DROP TABLE relation_event_attempts")
                    connection.execute("DROP TABLE relation_event_outbox")
                connection = sqlite3.connect(path)

                def fail(stage):
                    if stage == target:
                        raise RuntimeError("synthetic v13 migration failure")

                with self.assertRaisesRegex(RuntimeError, "migration failure"):
                    SQLiteDigestRepository._apply_v13(
                        connection, fault_injector=fail,
                    )
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'",
                )}
                ledger = [row[0] for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version",
                )]
                subscription_id = connection.execute(
                    "SELECT subscription_id FROM subscriptions",
                ).fetchone()[0]
                connection.close()
                self.assertNotIn("relation_event_outbox", tables)
                self.assertNotIn("relation_event_attempts", tables)
                self.assertEqual(ledger, list(range(1, 13)))
                self.assertEqual(subscription_id, ids["subscription"])
                migrated = SQLiteDigestRepository(path)
                self.assertEqual(
                    migrated.get_subscription(ids["subscription"]).subscription_id,
                    ids["subscription"],
                )

    def test_subscription_crud_is_plain_sqlite_and_migrated(self):
        with tempfile.TemporaryDirectory() as root:
            repository = SQLiteDigestRepository(f"{root}/digest.db")
            service = SubscriptionService(
                repository, id_factory=lambda: "1" * 32, clock=lambda: NOW,
            )
            value = service.create_from_natural_language(
                "2" * 32,
                "帮我订阅 AI 行业动态，每天一份，600 字以内，"
                "重点关注 Agent、模型发布和开发工具。",
            )
            self.assertEqual(value.topic, "AI 行业动态")
            self.assertEqual(value.max_chars, 600)
            self.assertEqual(value.focus_topics, ("Agent", "模型发布", "开发工具"))
            self.assertEqual(repository.get_subscription(value.subscription_id), value)
            self.assertEqual(repository.list_subscriptions(), (value,))
            with repository.connect() as connection:
                versions = [row[0] for row in connection.execute(
                    "SELECT version FROM schema_migrations"
                )]
            self.assertEqual(versions, list(range(1, SCHEMA_VERSION + 1)))

    def test_duplicate_period_reservation_returns_existing_run(self):
        with tempfile.TemporaryDirectory() as root:
            repository = SQLiteDigestRepository(f"{root}/digest.db")
            service = SubscriptionService(
                repository, id_factory=lambda: "1" * 32, clock=lambda: NOW,
            )
            subscription = service.create_from_natural_language(
                "2" * 32, "订阅 AI 行业动态，600 字以内",
            )
            first = DigestRunRecord(
                "3" * 32, subscription.subscription_id, "2026-08-23",
                "4" * 32, "reserved", None, None, None, None,
            )
            second = DigestRunRecord(
                "5" * 32, subscription.subscription_id, "2026-08-23",
                "6" * 32, "reserved", None, None, None, None,
            )
            stored, created = repository.reserve_digest_run(first)
            duplicate, duplicate_created = repository.reserve_digest_run(second)
            self.assertTrue(created)
            self.assertFalse(duplicate_created)
            self.assertEqual(duplicate, stored)

    def test_schema_v1_is_forward_migrated_to_current(self):
        with tempfile.TemporaryDirectory() as root:
            path = f"{root}/digest.db"
            connection = sqlite3.connect(path)
            connection.execute("""
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
                )
            """)
            SQLiteDigestRepository._migrate_v1(connection)
            connection.execute(
                "INSERT INTO schema_migrations VALUES (1, datetime('now'))"
            )
            connection.commit()
            connection.close()
            repository = SQLiteDigestRepository(path)
            with repository.connect() as migrated:
                versions = [row[0] for row in migrated.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )]
                tables = {row[0] for row in migrated.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
                run_columns = {row[1] for row in migrated.execute(
                    "PRAGMA table_info(digest_runs)"
                )}
            self.assertEqual(versions, list(range(1, SCHEMA_VERSION + 1)))
            self.assertTrue({
                "interest_profiles", "profile_topic_weights", "interactions",
                "profile_updates", "seen_content", "delivery_records",
                "delivery_attempts",
                "generation_attempts", "conversations",
                "conversation_turns", "definition_outcomes",
                "definition_attempts",
                "subscription_definitions", "subscription_aggregates",
                "user_subscriptions", "briefing_reservations",
                "application_outbox", "subscription_activations",
            }.issubset(tables))
            self.assertTrue({
                "idempotency_key", "subscription_version",
                "subscription_snapshot_json", "harness_bound_at",
                "started_at", "updated_at", "failure_stage", "failure_code",
                "failure_subtype", "failure_diagnostics_json",
                "generation_failure_subtype",
                "definition_id", "definition_version",
            }.issubset(run_columns))
            self.assertIn("recovery_operations", tables)
            with repository.connect() as migrated:
                turn_columns = {row[1] for row in migrated.execute(
                    "PRAGMA table_info(conversation_turns)"
                )}
            self.assertTrue({
                "failure_stage", "failure_subtype",
            }.issubset(turn_columns))

    def test_v10_legacy_subscription_and_digest_are_not_backfilled(self):
        with tempfile.TemporaryDirectory() as root:
            path = f"{root}/digest.db"
            connection = sqlite3.connect(path)
            connection.execute("""
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
                )
            """)
            for version in range(1, 11):
                getattr(SQLiteDigestRepository, f"_migrate_v{version}")(
                    connection,
                )
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (?, datetime('now'))",
                    (version,),
                )
            subscription = Subscription(
                "1" * 32, "2" * 32, "AI", "订阅 AI", "daily", "zh-CN",
                600, 5, (), "none", True, 1, NOW, NOW,
            )
            payload = asdict(subscription)
            payload["focus_topics"] = []
            connection.execute("""
                INSERT INTO subscriptions VALUES (?, ?, ?, ?, ?, ?)
            """, (
                subscription.subscription_id, subscription.user_id,
                json.dumps(payload, ensure_ascii=False), 1, NOW, NOW,
            ))
            connection.execute("""
                INSERT INTO digest_runs(
                    digest_run_id, subscription_id, period_key,
                    harness_run_id, status, digest_id, artifact_id,
                    idempotency_key, subscription_version, updated_at
                ) VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, 1, ?)
            """, (
                "3" * 32, subscription.subscription_id, "legacy",
                "4" * 32, "5" * 32, "6" * 32, "legacy", NOW,
            ))
            connection.execute("""
                INSERT INTO digests(
                    digest_id, digest_run_id, subscription_id,
                    harness_run_id, artifact_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                "5" * 32, "3" * 32, subscription.subscription_id,
                "4" * 32, "6" * 32,
                json.dumps({"rendered_text": "legacy"}), NOW,
            ))
            connection.commit()
            connection.close()

            repository = SQLiteDigestRepository(path)
            self.assertEqual(repository.get_subscription(
                subscription.subscription_id,
            ), subscription)
            self.assertEqual(repository.get_digest("5" * 32).payload,
                             {"rendered_text": "legacy"})
            self.assertIsNone(repository.get_product_subscription(
                subscription.subscription_id,
            ))
            self.assertIsNone(
                repository.get_user_subscription_for_subscription(
                    subscription.subscription_id,
                )
            )


if __name__ == "__main__":
    unittest.main()
