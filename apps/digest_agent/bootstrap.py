"""Application-owned configuration, readiness and dependency composition."""

from dataclasses import dataclass
import os
import sqlite3

from .application import DigestApplication
from .adapters.delivery import (
    FakeDeliveryAdapter, TermuxNotificationDeliveryAdapter,
)
from .adapters.provider import (
    LLM_API_KEY, LLM_API_MODE, LLM_ENDPOINT, LLM_MODEL,
    FakeDigestProvider, VertexDigestProvider,
)
from .adapters.search import (
    BRAVE_SEARCH_API_KEY, BraveSearchClient, FakeSearchClient,
)
from .adapters.sqlite import SCHEMA_VERSION, SQLiteDigestRepository
from .services import DeliveryService, FeedbackService, SubscriptionService
from .workflows import DigestGenerationWorkflow


PROVIDER_MODES = {
    "search": frozenset({"fake", "brave"}),
    "llm": frozenset({"fake", "vertex"}),
    "delivery": frozenset({"fake", "termux"}),
}


def _fake_rows():
    return [{
        "url": "https://example.test/agent",
        "title": "AI Agent Engineering Update",
        "snippet": "AI 行业动态与 Agent 工具发布。",
        "published_at": "2026-08-23T10:00:00Z",
        "topic_tags": ["AI 行业动态", "Agent"],
    }, {
        "url": "https://example.test/model",
        "title": "模型发布更新",
        "snippet": "AI 行业动态中的新模型发布。",
        "published_at": "2026-08-23T09:00:00Z",
        "topic_tags": ["AI 行业动态", "模型发布"],
    }]


@dataclass(frozen=True, slots=True)
class DigestAppConfig:
    database_path: str
    workspace_path: str
    audit_path: str
    search_provider: str = "fake"
    llm_provider: str = "fake"
    delivery_provider: str = "fake"
    user_id: str = "a" * 32

    def __post_init__(self):
        for name in ("database_path", "workspace_path", "audit_path"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"invalid {name}")
        for name, choices in PROVIDER_MODES.items():
            value = getattr(self, f"{name}_provider")
            if value not in choices:
                raise ValueError(f"invalid {name} provider")
        if (not isinstance(self.user_id, str) or len(self.user_id) != 32
                or any(ch not in "0123456789abcdef" for ch in self.user_id)):
            raise ValueError("invalid local user id")


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    name: str
    status: str


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    status: str
    checks: tuple[ReadinessCheck, ...]
    search_provider: str
    llm_provider: str
    delivery_provider: str


class BootstrapError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _path_ready(path, directory=False):
    candidate = os.path.realpath(path)
    if os.path.exists(candidate):
        return os.path.isdir(candidate) if directory else os.path.isfile(candidate)
    parent = os.path.dirname(candidate) or os.curdir
    while not os.path.exists(parent):
        ancestor = os.path.dirname(parent)
        if ancestor == parent:
            return False
        parent = ancestor
    return os.path.isdir(parent) and os.access(parent, os.W_OK)


def _schema_ready(path):
    if not os.path.exists(path):
        return True
    try:
        connection = sqlite3.connect(f"file:{os.path.realpath(path)}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT MAX(version) FROM schema_migrations",
            ).fetchone()
            return (row is not None and isinstance(row[0], int)
                    and 1 <= row[0] <= SCHEMA_VERSION)
        finally:
            connection.close()
    except sqlite3.Error:
        return False


def check_readiness(config, environ=None, termux_dispatcher=None):
    """Check startup configuration only; never call external services."""
    environ = os.environ if environ is None else environ
    checks = []

    def add(name, ready):
        checks.append(ReadinessCheck(name, "READY" if ready else "NOT_READY"))

    add("database_path", _path_ready(config.database_path))
    add("database_schema", _schema_ready(config.database_path))
    add("workspace", _path_ready(config.workspace_path, directory=True))
    add("audit", _path_ready(config.audit_path, directory=True))
    add("search_adapter", config.search_provider in PROVIDER_MODES["search"])
    if config.search_provider == "brave":
        checks.append(ReadinessCheck(
            BRAVE_SEARCH_API_KEY,
            "SET" if bool(environ.get(BRAVE_SEARCH_API_KEY)) else "MISSING",
        ))
    add("llm_adapter", config.llm_provider in PROVIDER_MODES["llm"])
    if config.llm_provider == "vertex":
        for name in (LLM_API_KEY, LLM_API_MODE, LLM_ENDPOINT, LLM_MODEL):
            checks.append(ReadinessCheck(
                name, "SET" if bool(environ.get(name)) else "MISSING",
            ))
    add("delivery_adapter", (
        config.delivery_provider == "fake"
        or config.delivery_provider == "termux" and callable(termux_dispatcher)
    ))
    ready = all(item.status in {"READY", "SET"} for item in checks)
    return ReadinessReport(
        "READY" if ready else "NOT_READY", tuple(checks),
        config.search_provider, config.llm_provider, config.delivery_provider,
    )


def bootstrap_application(config, environ=None, termux_dispatcher=None):
    report = check_readiness(config, environ, termux_dispatcher)
    if report.status != "READY":
        raise BootstrapError("startup_not_ready")
    os.makedirs(config.workspace_path, exist_ok=True)
    os.makedirs(config.audit_path, exist_ok=True)
    database_parent = os.path.dirname(os.path.realpath(config.database_path))
    os.makedirs(database_parent, exist_ok=True)
    try:
        repository = SQLiteDigestRepository(config.database_path)
    except (OSError, sqlite3.Error) as error:
        raise BootstrapError("startup_not_ready") from error
    search = (FakeSearchClient(_fake_rows()) if config.search_provider == "fake"
              else BraveSearchClient.from_environment(environ=environ))
    provider = (FakeDigestProvider() if config.llm_provider == "fake"
                else VertexDigestProvider.from_environment(environ=environ))
    delivery = (FakeDeliveryAdapter() if config.delivery_provider == "fake"
                else TermuxNotificationDeliveryAdapter(termux_dispatcher))
    subscriptions = SubscriptionService(repository)
    workflow = DigestGenerationWorkflow(
        repository, search, provider, config.workspace_path, config.audit_path,
    )
    return DigestApplication(
        repository, subscriptions, workflow,
        DeliveryService(repository, [delivery]), FeedbackService(repository),
    )
