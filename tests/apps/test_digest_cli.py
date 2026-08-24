from contextlib import redirect_stdout
import io
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from apps.digest_agent import cli
from apps.digest_agent.adapters.provider import (
    FakeDigestProvider, VertexDigestProvider,
)
from apps.digest_agent.adapters.definition import VertexDefinitionAgentAdapter
from apps.digest_agent.adapters.search import BraveSearchClient, FakeSearchClient
from apps.digest_agent.bootstrap import (
    DigestAppConfig, bootstrap_application, check_readiness,
    load_application_environment,
)
from apps.digest_agent.adapters.sqlite import SQLiteDigestRepository


class DigestCLITests(unittest.TestCase):
    def flags(self, root, *extra):
        return [
            "--database", os.path.join(root, "state", "digest.db"),
            "--workspace", os.path.join(root, "workspace"),
            "--audit", os.path.join(root, "audit"), "--json", *extra,
        ]

    def invoke(self, root, *arguments, environ=None):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(
                self.flags(root, *arguments), environ={} if environ is None else environ,
            )
        text = output.getvalue().strip()
        return code, json.loads(text) if text.startswith(("{", "[")) else text

    def test_full_fake_product_journey_uses_only_cli_contract(self):
        with tempfile.TemporaryDirectory() as root:
            code, sub = self.invoke(
                root, "subscription-create", "--request",
                "帮我订阅 AI 行业动态，每天一份，600 字以内，最多 2 条，"
                "重点关注 Agent、模型发布。",
            )
            self.assertEqual(code, 0)
            code, updated = self.invoke(
                root, "subscription-update", "--subscription-id",
                sub["subscription_id"], "--expected-version", "1",
                "--max-chars", "700",
            )
            self.assertEqual((code, updated["version"]), (0, 2))
            code, run = self.invoke(
                root, "run", "--subscription-id", sub["subscription_id"],
                "--idempotency-key", "cli-journey-1",
            )
            self.assertEqual((code, run["status"]), (0, "completed"))
            code, status = self.invoke(
                root, "run-status", "--application-run-id",
                run["application_run_id"],
            )
            self.assertEqual(status["digest_id"], run["digest_id"])
            code, digest = self.invoke(
                root, "digest-get", "--digest-id", run["digest_id"],
            )
            item = digest["content"]["items"][0]
            self.assertNotIn("evidence_id", json.dumps(digest))
            self.assertNotIn("projection_id", json.dumps(digest))
            code, delivery = self.invoke(
                root, "deliver", "--digest-id", digest["digest_id"],
                "--channel", "fake",
            )
            self.assertEqual((code, delivery["status"]), (0, "accepted"))
            code, feedback = self.invoke(
                root, "feedback", "--digest-id", digest["digest_id"],
                "--type", "liked", "--event-key", "cli-like-1",
                "--item-id", item["item_id"],
            )
            self.assertTrue(feedback["applied"])
            code, profile = self.invoke(root, "profile")
            self.assertEqual((code, profile["version"]), (0, 1))
            code, second = self.invoke(
                root, "run", "--subscription-id", sub["subscription_id"],
                "--idempotency-key", "cli-journey-2",
            )
            self.assertEqual((code, second["status"]), (0, "completed"))
            code, listed = self.invoke(root, "digest-list")
            self.assertEqual((code, len(listed)), (0, 2))

    def test_outbox_commands_delegate_to_application_facade(self):
        class Facade:
            def __init__(self):
                self.calls = []

            def run_outbox_once(self):
                self.calls.append(("run_once",))
                return {"worker_status": "NO_WORK"}

            def drain_outbox(self, maximum):
                self.calls.append(("drain", maximum))
                return ()

            def inspect_outbox(self, outbox_id):
                self.calls.append(("inspect", outbox_id))
                return ()

            def recover_outbox(self, outbox_id, action):
                self.calls.append(("recover", outbox_id, action))
                return {"worker_status": "RETRYABLE"}

        with tempfile.TemporaryDirectory() as root:
            facade = Facade()
            with patch(
                "apps.digest_agent.cli.bootstrap_application",
                return_value=facade,
            ):
                self.assertEqual(self.invoke(root, "outbox-run-once")[0], 0)
                self.assertEqual(self.invoke(
                    root, "outbox-drain", "--max", "3",
                )[0], 0)
                self.assertEqual(self.invoke(
                    root, "outbox-inspect", "--outbox-id", "o" * 32,
                )[0], 0)
                self.assertEqual(self.invoke(
                    root, "outbox-recover", "--outbox-id", "o" * 32,
                    "--action", "release_not_started",
                )[0], 0)
            self.assertEqual(facade.calls, [
                ("run_once",), ("drain", 3), ("inspect", "o" * 32),
                ("recover", "o" * 32, "release_not_started"),
            ])

    def test_relation_event_commands_delegate_to_application_facade(self):
        class Facade:
            def __init__(self):
                self.calls = []

            def publish_relation_event_once(self):
                self.calls.append(("run_once",))
                return {"worker_status": "NO_WORK"}

            def drain_relation_events(self, maximum):
                self.calls.append(("drain", maximum))
                return ()

            def inspect_relation_events(self, event_id):
                self.calls.append(("inspect", event_id))
                return ()

            def recover_relation_event(self, event_id, action):
                self.calls.append(("recover", event_id, action))
                return {"worker_status": "RECOVERED"}

        with tempfile.TemporaryDirectory() as root:
            facade = Facade()
            with patch(
                "apps.digest_agent.cli.bootstrap_application",
                return_value=facade,
            ):
                self.assertEqual(self.invoke(
                    root, "relation-events-run-once",
                )[0], 0)
                self.assertEqual(self.invoke(
                    root, "relation-events-drain", "--max", "3",
                )[0], 0)
                self.assertEqual(self.invoke(
                    root, "relation-events-inspect", "--event-id", "e" * 32,
                )[0], 0)
                self.assertEqual(self.invoke(
                    root, "relation-events-recover", "--event-id", "e" * 32,
                    "--action", "block_unknown",
                )[0], 0)
            self.assertEqual(facade.calls, [
                ("run_once",), ("drain", 3), ("inspect", "e" * 32),
                ("recover", "e" * 32, "block_unknown"),
            ])

    def test_all_fake_is_ready_and_never_calls_external_services(self):
        with tempfile.TemporaryDirectory() as root:
            config = DigestAppConfig(
                os.path.join(root, "digest.db"), os.path.join(root, "workspace"),
                os.path.join(root, "audit"),
            )
            with patch(
                "apps.digest_agent.adapters.search.UrllibSearchTransport.get",
                side_effect=AssertionError("network called"),
            ), patch(
                "apps.digest_agent.adapters.provider.UrllibVertexTransport.post",
                side_effect=AssertionError("network called"),
            ):
                report = check_readiness(config, environ={})
            self.assertEqual(report.status, "READY")

    def test_real_readiness_checks_presence_without_external_probe(self):
        with tempfile.TemporaryDirectory() as root:
            config = DigestAppConfig(
                os.path.join(root, "digest.db"), os.path.join(root, "workspace"),
                os.path.join(root, "audit"), "brave", "vertex", "termux",
            )
            environ = {
                "BRAVE_SEARCH_API_KEY": "configured-brave-key",
                "LLM_API_KEY": "configured-vertex-key",
                "LLM_API_MODE": "chat-completions",
                "LLM_ENDPOINT": "https://example.test/v1",
                "LLM_MODEL": "model",
            }
            with patch(
                "apps.digest_agent.adapters.search.UrllibSearchTransport.get",
                side_effect=AssertionError("Brave probe called"),
            ), patch(
                "apps.digest_agent.adapters.provider.UrllibVertexTransport.post",
                side_effect=AssertionError("Vertex probe called"),
            ):
                report = check_readiness(
                    config, environ=environ,
                    termux_dispatcher=lambda _capability, _arguments: None,
                )
            self.assertEqual(report.status, "READY")

    def test_application_bootstrap_loads_dotenv_once_for_all_real_adapters(self):
        with tempfile.TemporaryDirectory() as root:
            env_path = os.path.join(root, ".env.local")
            with open(env_path, "w", encoding="utf-8") as stream:
                stream.write(
                    "export BRAVE_SEARCH_API_KEY='brave-fixture-key'\n"
                    "LLM_API_KEY=vertex-fixture-key\n"
                    "LLM_API_MODE=chat-completions\n"
                    "LLM_ENDPOINT=https://example.test/v1\n"
                    "LLM_MODEL=file-model\n"
                )
            config = DigestAppConfig(
                os.path.join(root, "digest.db"),
                os.path.join(root, "workspace"), os.path.join(root, "audit"),
                "brave", "vertex", "fake",
            )
            with patch.dict(os.environ, {"LLM_MODEL": "process-model"},
                            clear=True), patch(
                "apps.digest_agent.bootstrap.DEFAULT_ENV_PATH", env_path,
            ):
                resolved = load_application_environment()
                report = check_readiness(config)
                app = bootstrap_application(config)
            self.assertEqual(report.status, "READY")
            self.assertTrue(all(
                item.status == "SET" for item in report.checks
                if item.name in {
                    "BRAVE_SEARCH_API_KEY", "LLM_API_KEY", "LLM_API_MODE",
                    "LLM_ENDPOINT", "LLM_MODEL",
                }
            ))
            self.assertEqual(resolved["LLM_MODEL"], "process-model")
            self.assertIsInstance(app.generation.search_client,
                                  BraveSearchClient)
            self.assertIsInstance(app.generation.provider,
                                  VertexDigestProvider)
            self.assertIsInstance(app.conversations.provider,
                                  VertexDefinitionAgentAdapter)
            self.assertEqual(app.generation.provider.environ["LLM_MODEL"],
                             "process-model")

    def test_real_modes_require_explicit_non_secret_configuration_presence(self):
        with tempfile.TemporaryDirectory() as root:
            base = (os.path.join(root, "digest.db"),
                    os.path.join(root, "workspace"), os.path.join(root, "audit"))
            brave = check_readiness(
                DigestAppConfig(*base, search_provider="brave"), environ={},
            )
            vertex = check_readiness(
                DigestAppConfig(*base, llm_provider="vertex"), environ={},
            )
            termux = check_readiness(
                DigestAppConfig(*base, delivery_provider="termux"), environ={},
            )
            self.assertEqual((brave.status, vertex.status, termux.status),
                             ("NOT_READY", "NOT_READY", "NOT_READY"))
            self.assertIn("MISSING", {item.status for item in brave.checks})
            self.assertIn("MISSING", {item.status for item in vertex.checks})

    def test_readiness_never_prints_secret_value(self):
        with tempfile.TemporaryDirectory() as root:
            secret = "secret-value-that-must-never-appear"
            config = DigestAppConfig(
                os.path.join(root, "digest.db"), os.path.join(root, "workspace"),
                os.path.join(root, "audit"), "brave", "vertex", "fake",
            )
            environ = {
                "BRAVE_SEARCH_API_KEY": secret, "LLM_API_KEY": secret,
                "LLM_API_MODE": "chat-completions", "LLM_ENDPOINT": "https://example.test/v1",
                "LLM_MODEL": "model",
            }
            output = io.StringIO()
            with redirect_stdout(output):
                report = check_readiness(config, environ=environ)
                cli._print(report, True)
            self.assertEqual(report.status, "READY")
            self.assertNotIn(secret, output.getvalue())

    def test_vertex_prompt_only_mode_is_not_product_ready(self):
        with tempfile.TemporaryDirectory() as root:
            config = DigestAppConfig(
                os.path.join(root, "digest.db"),
                os.path.join(root, "workspace"), os.path.join(root, "audit"),
                llm_provider="vertex",
            )
            environ = {
                "LLM_API_KEY": "configured-vertex-key",
                "LLM_API_MODE": "completions",
                "LLM_ENDPOINT": "https://example.test/v1",
                "LLM_MODEL": "sonnet-4.6",
            }
            with patch(
                "apps.digest_agent.adapters.provider.UrllibVertexTransport.post",
                side_effect=AssertionError("Vertex probe called"),
            ):
                report = check_readiness(config, environ=environ)
            self.assertEqual(report.status, "NOT_READY")
            self.assertIn(
                ("llm_structured_tool_mode", "NOT_READY"),
                {(item.name, item.status) for item in report.checks},
            )

    def test_invalid_paths_are_not_ready_and_bootstrap_migrates_schema(self):
        with tempfile.TemporaryDirectory() as root:
            bad_workspace = os.path.join(root, "workspace-file")
            with open(bad_workspace, "w", encoding="utf-8") as stream:
                stream.write("not a directory")
            bad = DigestAppConfig(
                os.path.join(root, "database-dir"), bad_workspace,
                os.path.join(root, "audit"),
            )
            os.mkdir(bad.database_path)
            self.assertEqual(check_readiness(bad, environ={}).status, "NOT_READY")
            good = DigestAppConfig(
                os.path.join(root, "state", "digest.db"),
                os.path.join(root, "good-workspace"), os.path.join(root, "good-audit"),
            )
            bootstrap_application(good, environ={})
            self.assertEqual(check_readiness(good, environ={}).status, "READY")

    def test_bootstrap_forward_migrates_ready_old_schema(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "digest.db")
            connection = sqlite3.connect(path)
            connection.execute("""
                CREATE TABLE schema_migrations(
                    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
                )
            """)
            SQLiteDigestRepository._migrate_v1(connection)
            connection.execute(
                "INSERT INTO schema_migrations VALUES (1, datetime('now'))",
            )
            connection.commit()
            connection.close()
            config = DigestAppConfig(
                path, os.path.join(root, "workspace"), os.path.join(root, "audit"),
            )
            self.assertEqual(check_readiness(config, environ={}).status, "READY")
            bootstrap_application(config, environ={})
            self.assertEqual(check_readiness(config, environ={}).status, "READY")

    def test_keys_never_trigger_implicit_real_provider_selection(self):
        with tempfile.TemporaryDirectory() as root:
            config = DigestAppConfig(
                os.path.join(root, "digest.db"), os.path.join(root, "workspace"),
                os.path.join(root, "audit"), "fake", "fake", "fake",
            )
            app = bootstrap_application(config, environ={
                "BRAVE_SEARCH_API_KEY": "present-but-unused",
                "LLM_API_KEY": "present-but-unused",
            })
            self.assertIsInstance(app.generation.search_client, FakeSearchClient)
            self.assertIsInstance(app.generation.provider, FakeDigestProvider)

    def test_cli_failure_is_stable_without_traceback(self):
        with tempfile.TemporaryDirectory() as root:
            code, output = self.invoke(
                root, "subscription-get", "--subscription-id", "1" * 32,
            )
            self.assertEqual(code, 1)
            self.assertEqual(output, "ERROR code=not_found")
            self.assertNotIn("Traceback", output)


if __name__ == "__main__":
    unittest.main()
