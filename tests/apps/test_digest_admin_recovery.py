from contextlib import redirect_stdout
from dataclasses import fields
import io
import json
import os
import tempfile
import threading
import unittest

from apps.digest_agent import cli
from apps.digest_agent.application import (
    ApplicationError, RecoveryInspection,
)
from apps.digest_agent.adapters.sqlite import SQLiteDigestRepository
from mini_harness_core.audit import AuditWriter
from mini_harness_core.result import ResultStore
from tests.apps.test_digest_application import (
    BlockingSearchClient, DigestApplicationTests, FailFinishOnceRepository,
    USER, NOW, rows,
)


class AdminRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.factory = DigestApplicationTests()

    def test_reserved_inspect_and_recover_original_logical_run_once(self):
        with tempfile.TemporaryDirectory() as root:
            app, repository, workflow, search, provider, _delivery = self.factory.make(root)
            sub = self.factory.create(app)
            reserved = self.factory.reserve_only(
                app, repository, workflow, sub, "admin-a",
            )
            inspection = app.inspect_run_recovery(reserved.digest_run_id)
            self.assertEqual(
                (inspection.binding_status, inspection.harness_run_status,
                 inspection.safe_recovery_actions),
                ("unbound", "not_started", ("resume_original_run",)),
            )
            operation = app.execute_run_recovery(
                reserved.digest_run_id, "resume_original_run",
            )
            duplicate = app.execute_run_recovery(
                reserved.digest_run_id, "resume_original_run",
            )
            self.assertEqual((operation.status, duplicate.status),
                             ("recovered", "recovered"))
            self.assertEqual((len(search.calls), len(provider.calls)), (1, 1))
            with repository.connect() as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM digest_runs"
                ).fetchone()[0], 1)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM digests"
                ).fetchone()[0], 1)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM delivery_records"
                ).fetchone()[0], 0)

    def test_bound_not_started_reuses_same_harness_binding(self):
        with tempfile.TemporaryDirectory() as root:
            app, repository, workflow, search, provider, _delivery = self.factory.make(root)
            sub = self.factory.create(app)
            reserved = self.factory.reserve_only(
                app, repository, workflow, sub, "admin-b",
            )
            bound = repository.bind_digest_run(
                reserved.digest_run_id, reserved.harness_run_id, NOW,
            )
            inspection = app.inspect_run_recovery(bound.digest_run_id)
            self.assertEqual(inspection.safe_recovery_actions,
                             ("resume_bound_run",))
            app.execute_run_recovery(bound.digest_run_id, "resume_bound_run")
            final = repository.get_digest_run(bound.digest_run_id)
            self.assertEqual(final.harness_run_id, bound.harness_run_id)
            self.assertEqual((len(search.calls), len(provider.calls)), (1, 1))

    def test_terminal_result_repairs_projection_without_external_calls(self):
        with tempfile.TemporaryDirectory() as root:
            inner = SQLiteDigestRepository(os.path.join(root, "digest.db"))
            repository = FailFinishOnceRepository(inner)
            app, _repo, workflow, search, provider, _delivery = self.factory.make(
                root, repository=repository,
            )
            sub = self.factory.create(app)
            with self.assertRaises(OSError):
                app.run_subscription(USER, sub.subscription_id, "admin-c")
            with inner.connect() as connection:
                run_id = connection.execute(
                    "SELECT digest_run_id FROM digest_runs",
                ).fetchone()[0]
            search.calls.clear()
            provider.calls.clear()
            inspection = app.inspect_run_recovery(run_id)
            self.assertTrue(inspection.terminal_result_available)
            self.assertEqual(inspection.safe_recovery_actions,
                             ("repair_projection",))
            operation = app.execute_run_recovery(run_id, "repair_projection")
            self.assertEqual(operation.status, "recovered")
            self.assertEqual((len(search.calls), len(provider.calls)), (0, 0))
            run = inner.get_digest_run(run_id)
            self.assertIsNotNone(inner.get_digest(run.digest_id))
            with inner.connect() as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM digests"
                ).fetchone()[0], 1)

    def test_ambiguous_effect_exposes_no_action_and_never_reruns(self):
        with tempfile.TemporaryDirectory() as root:
            app, repository, workflow, search, provider, _delivery = self.factory.make(root)
            sub = self.factory.create(app)
            reserved = self.factory.reserve_only(
                app, repository, workflow, sub, "admin-d",
            )
            bound = repository.bind_digest_run(
                reserved.digest_run_id, reserved.harness_run_id, NOW,
            )
            AuditWriter(USER, bound.harness_run_id, workflow.audit_directory).append(
                "tool_requested", "harness", "mcp:search:web_search", "requested",
            )
            repository.mark_digest_run_recovery_required(
                bound.digest_run_id, "recovery_required", NOW,
            )
            inspection = app.inspect_run_recovery(bound.digest_run_id)
            self.assertEqual(inspection.safe_recovery_actions, ())
            self.assertEqual(inspection.blocking_reason,
                             "NO_SAFE_AUTOMATIC_RECOVERY")
            for action in ("resume_bound_run", "rerun_anyway", "force_completed",
                           "new_harness_run"):
                with self.assertRaises(ApplicationError) as caught:
                    app.execute_run_recovery(bound.digest_run_id, action)
                self.assertEqual(caught.exception.code, "unsafe_recovery_action")
            self.assertEqual((len(search.calls), len(provider.calls)), (0, 0))
            self.assertEqual(repository.get_digest_run(bound.digest_run_id).status,
                             "recovery_required")

    def test_concurrent_admin_recovery_has_single_owner(self):
        with tempfile.TemporaryDirectory() as root:
            search = BlockingSearchClient(rows())
            app, repository, workflow, _search, provider, _delivery = self.factory.make(
                root, search=search,
            )
            sub = self.factory.create(app)
            reserved = self.factory.reserve_only(
                app, repository, workflow, sub, "admin-concurrent",
            )
            outcomes = []

            def recover():
                outcomes.append(app.execute_run_recovery(
                    reserved.digest_run_id, "resume_original_run",
                ))

            worker = threading.Thread(target=recover)
            worker.start()
            self.assertTrue(search.entered.wait(5))
            follower = app.execute_run_recovery(
                reserved.digest_run_id, "resume_original_run",
            )
            self.assertEqual(follower.status, "already_recovering")
            search.release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(outcomes[0].status, "recovered")
            self.assertEqual((len(search.calls), len(provider.calls)), (1, 1))

    def test_projection_failure_preserves_terminal_truth_and_safe_audit(self):
        with tempfile.TemporaryDirectory() as root:
            inner = SQLiteDigestRepository(os.path.join(root, "digest.db"))
            repository = FailFinishOnceRepository(inner)
            app, _repo, workflow, search, provider, _delivery = self.factory.make(
                root, repository=repository,
            )
            sub = self.factory.create(app)
            with self.assertRaises(OSError):
                app.run_subscription(USER, sub.subscription_id, "admin-failure")
            with inner.connect() as connection:
                run_id, harness_id = connection.execute(
                    "SELECT digest_run_id, harness_run_id FROM digest_runs",
                ).fetchone()
            result = ResultStore(os.path.join(root, "audit", "results")).load(
                harness_id,
            )
            artifact_path = os.path.join(
                root, "workspace", "runs", run_id, "digest.json",
            )
            os.remove(artifact_path)
            search.calls.clear()
            provider.calls.clear()
            operation = app.execute_run_recovery(run_id, "repair_projection")
            self.assertEqual((operation.status, operation.failure_reason),
                             ("failed", "recovery_operation_failed"))
            self.assertEqual(inner.get_digest_run(run_id).status,
                             "recovery_required")
            self.assertEqual(ResultStore(
                os.path.join(root, "audit", "results")
            ).load(harness_id), result)
            self.assertEqual((len(search.calls), len(provider.calls)), (0, 0))
            with inner.connect() as connection:
                audit = dict(connection.execute(
                    "SELECT * FROM recovery_operations",
                ).fetchone())
            self.assertEqual(set(audit), {
                "operation_id", "application_run_id", "action", "status",
                "before_state", "after_state", "requested_at", "completed_at",
                "error_code",
            })
            encoded = json.dumps(audit)
            for forbidden in ("evidence", "observation", "provider", "secret",
                              "traceback", "harness_result"):
                self.assertNotIn(forbidden, encoded.casefold())

    def test_inspection_dto_hides_harness_identity_and_internals(self):
        names = {field.name for field in fields(RecoveryInspection)}
        self.assertTrue({
            "application_run_id", "application_run_status", "recovery_reason",
            "binding_status", "harness_run_status", "terminal_result_available",
            "safe_recovery_actions", "blocking_reason",
        }.issubset(names))
        self.assertTrue({
            "harness_run_id", "evidence", "observation", "audit", "checkpoint",
            "result",
        }.isdisjoint(names))

    def test_admin_cli_inspect_and_execute_use_facade(self):
        with tempfile.TemporaryDirectory() as root:
            app, repository, workflow, _search, _provider, _delivery = self.factory.make(root)
            sub = self.factory.create(app)
            reserved = self.factory.reserve_only(
                app, repository, workflow, sub, "admin-cli",
            )
            flags = [
                "--database", os.path.join(root, "digest.db"),
                "--workspace", os.path.join(root, "workspace"),
                "--audit", os.path.join(root, "audit"), "--json",
            ]
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.main(flags + [
                    "run-recovery-inspect", "--application-run-id",
                    reserved.digest_run_id,
                ], environ={})
            inspection = json.loads(output.getvalue())
            self.assertEqual((code, inspection["safe_recovery_actions"]),
                             (0, ["resume_original_run"]))
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.main(flags + [
                    "run-recovery-execute", "--application-run-id",
                    reserved.digest_run_id, "--action", "resume_original_run",
                ], environ={})
            operation = json.loads(output.getvalue())
            self.assertEqual((code, operation["status"]), (0, "recovered"))


if __name__ == "__main__":
    unittest.main()
