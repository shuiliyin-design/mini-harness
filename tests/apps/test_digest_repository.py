from dataclasses import asdict
import json
import tempfile
import sqlite3
import unittest

from apps.digest_agent.adapters.sqlite import SCHEMA_VERSION, SQLiteDigestRepository
from apps.digest_agent.domain import Subscription
from apps.digest_agent.repositories import DigestRunRecord
from apps.digest_agent.services import SubscriptionService


NOW = "2026-08-23T12:00:00Z"


class SQLiteRepositoryTests(unittest.TestCase):
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
