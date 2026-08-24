import ast
import unittest

from tests._paths import REPO_ROOT


APP = REPO_ROOT / "apps" / "digest_agent"
CORE = REPO_ROOT / "mini_harness_core"


def imported_roots(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def imported_modules(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


class DigestArchitectureTests(unittest.TestCase):
    def test_core_never_imports_application(self):
        for path in CORE.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            modules = {
                node.module for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            }
            modules.update(
                alias.name for node in ast.walk(tree)
                if isinstance(node, ast.Import) for alias in node.names
            )
            self.assertFalse(any(
                name == "apps" or name.startswith("apps.") for name in modules
            ), path)

    def test_subscription_business_protocol_stays_out_of_core(self):
        source = "\n".join(
            path.read_text(encoding="utf-8") for path in CORE.rglob("*.py")
        )
        for business_term in (
            "NEXT_QUESTION", "DEFINITION_ACCEPTED", "DefinitionOutcome",
            "UserSubscription", "SubscriptionActivation",
            "FIRST_BRIEFING_REQUESTED", "application_outbox",
            "definition_outcomes",
        ):
            self.assertNotIn(business_term, source)

    def test_domain_and_contracts_do_not_import_harness_or_infrastructure(self):
        for name in ("domain.py", "contracts.py"):
            roots = imported_roots(APP / name)
            self.assertTrue({"sqlite3", "mini_harness_core"}.isdisjoint(roots))

    def test_only_external_service_adapters_own_network_modules(self):
        names = {path.name for path in APP.rglob("*.py")}
        self.assertNotIn("api.py", names)
        for path in APP.rglob("*.py"):
            modules = imported_modules(path)
            network = modules & {
                "urllib", "urllib.request", "http.client", "http.server", "socket",
            }
            if path in {
                APP / "adapters" / "search.py",
                APP / "adapters" / "provider.py",
            }:
                self.assertEqual(network, {"urllib", "socket"})
            elif path == APP / "web.py":
                self.assertEqual(network, {"http.server"})
            else:
                self.assertFalse(network, path)

    def test_application_cli_and_bootstrap_do_not_import_harness_internals(self):
        cli_modules = imported_modules(APP / "cli.py")
        bootstrap_modules = imported_modules(APP / "bootstrap.py")
        self.assertFalse(any(name.startswith("mini_harness_core")
                             for name in cli_modules | bootstrap_modules))
        forbidden = {"sqlite3", "repositories", "workflows", "services"}
        self.assertTrue(forbidden.isdisjoint(cli_modules))
        source = (APP / "cli.py").read_text(encoding="utf-8")
        for internal in ("Evidence", "Artifact", "ResultStore", "run_agent",
                         "checkpoint", "AuditWriter"):
            self.assertNotIn(internal, source)

    def test_http_is_a_thin_application_client(self):
        modules = imported_modules(APP / "web.py")
        self.assertFalse(any(name.startswith("mini_harness_core") for name in modules))
        self.assertTrue({"application", "bootstrap"}.issubset(modules))
        forbidden_modules = {"repositories", "workflows", "services", "adapters.sqlite"}
        self.assertTrue(forbidden_modules.isdisjoint(modules))
        source = (APP / "web.py").read_text(encoding="utf-8")
        for internal in ("Evidence", "Artifact", "ResultStore", "run_agent",
                         "checkpoint", "AuditWriter", "harness_run_id"):
            self.assertNotIn(internal, source)

    def test_all_real_application_entrypoints_use_bootstrap_contract(self):
        paths = (
            APP / "cli.py",
            APP / "web.py",
            REPO_ROOT / "tools" / "vertex_conversation_smoke.py",
            REPO_ROOT / "tools" / "async_first_briefing_smoke.py",
        )
        for path in paths:
            source = path.read_text(encoding="utf-8")
            self.assertIn("bootstrap_application", source, path)
            self.assertNotIn(".from_environment(", source, path)


if __name__ == "__main__":
    unittest.main()
