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

    def test_domain_and_contracts_do_not_import_harness_or_infrastructure(self):
        for name in ("domain.py", "contracts.py"):
            roots = imported_roots(APP / name)
            self.assertTrue({"sqlite3", "mini_harness_core"}.isdisjoint(roots))

    def test_slice_has_no_network_or_http_module(self):
        names = {path.name for path in APP.rglob("*.py")}
        self.assertNotIn("api.py", names)
        for path in APP.rglob("*.py"):
            modules = imported_modules(path)
            self.assertTrue({
                "urllib.request", "http.client", "socket",
            }.isdisjoint(modules))


if __name__ == "__main__":
    unittest.main()
