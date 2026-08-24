"""One opt-in Real Brave + Vertex async first-Briefing product smoke."""

from contextlib import redirect_stdout
from datetime import datetime
import io
import os
from pathlib import Path
import sys
import tempfile
import uuid

from apps.digest_agent.adapters.provider import LLM_API_KEY
from apps.digest_agent.adapters.search import BRAVE_SEARCH_API_KEY
from apps.digest_agent.bootstrap import (
    DigestAppConfig, bootstrap_application, check_readiness,
    load_application_environment,
)
from mini_harness_core.evidence import EvidenceStore


REQUEST = (
    "帮我订阅 AI 行业动态，每天，使用中文，每篇 600 字以内，"
    "最多 5 条，重点关注 Agent、"
    "模型发布和开发工具，暂时不需要通知。"
)


def _secret_scan(root, environ):
    secrets = tuple(
        environ[name].encode("utf-8")
        for name in (BRAVE_SEARCH_API_KEY, LLM_API_KEY)
        if environ.get(name)
    )
    return not any(
        secret in path.read_bytes()
        for path in Path(root).rglob("*") if path.is_file()
        for secret in secrets
    )


def _accepted_evidence_count(root):
    store = EvidenceStore(os.path.join(root, "audit", "evidence"))
    return sum(
        1 for path in Path(store.directory).glob("*.json")
        for record in (store.load(path.stem),)
        if (record["subject"]["kind"] == "search_candidate_set"
            and record["verification"]["accepted"])
    )


def _configuration_lines(report):
    names = {
        BRAVE_SEARCH_API_KEY, "LLM_API_KEY", "LLM_API_MODE",
        "LLM_ENDPOINT", "LLM_MODEL",
    }
    return tuple(
        (item.name, item.status) for item in report.checks
        if item.name in names
    )


def _reserved_identity_reused(reservation, run):
    return bool(
        reservation is not None and run is not None
        and run.digest_run_id == reservation.application_run_id
        and reservation.harness_run_id == run.harness_run_id
    )


def main():
    with tempfile.TemporaryDirectory(
            prefix="digest-async-first-briefing-smoke-") as root:
        config = DigestAppConfig(
            os.path.join(root, "digest.db"),
            os.path.join(root, "workspace"), os.path.join(root, "audit"),
            search_provider="brave", llm_provider="vertex",
            delivery_provider="fake",
        )
        environ = load_application_environment()
        report = check_readiness(config, environ=environ)
        for name, status in _configuration_lines(report):
            print(f"configuration.{name}={status}")
        if report.status != "READY":
            print("real_async_first_briefing=CONFIGURATION_UNAVAILABLE")
            return 2
        definition_config = DigestAppConfig(
            config.database_path, config.workspace_path, config.audit_path,
            search_provider="fake", llm_provider="fake",
            delivery_provider="fake", user_id=config.user_id,
        )
        definition_app = bootstrap_application(
            definition_config, environ=environ,
        )
        with redirect_stdout(io.StringIO()):
            conversation = definition_app.start_subscription_conversation(
                config.user_id, REQUEST, "real-async-" + uuid.uuid4().hex,
            )
        definition_fixture_calls = len(
            definition_app.conversations.provider.calls
        )
        if conversation.status != "DEFINITION_ACCEPTED":
            print("conversation_status=" + conversation.status)
            print("definition_fixture_calls=" + str(definition_fixture_calls))
            print("real_async_first_briefing=FAILED_BEFORE_COMMIT")
            return 3
        committed = definition_app.commit_subscription_from_definition(
            config.user_id, conversation.conversation_id,
        )

        app = bootstrap_application(config, environ=environ)
        search = app.generation.search_client
        digest_provider = app.generation.provider
        repository = app.repository
        relation = repository.get_user_subscription_for_subscription(
            committed.subscription_id,
        )
        reservation = repository.get_briefing_reservation(
            committed.first_briefing_application_run_id,
        )
        outbox = repository.get_application_outbox_for_run(
            committed.first_briefing_application_run_id,
        )
        digests_at_t0 = repository.list_digests(config.user_id)
        t0_ok = bool(
            committed.status == "ACTIVE"
            and relation is not None and relation.status == "ACTIVE"
            and reservation is not None and reservation.status == "PENDING"
            and outbox is not None and outbox.status == "pending"
            and not digests_at_t0
            and len(search.calls) == 0 and len(digest_provider.calls) == 0
        )
        print("T0.subscription_success=" + ("true" if t0_ok else "false"))
        print("T0.relation=" + (relation.status if relation else "MISSING"))
        print("T0.outbox=" + (outbox.status.upper() if outbox else "MISSING"))
        print("T0.reserved_application_run=" + (
            "PRESENT" if reservation is not None else "MISSING"
        ))
        print("T0.digest_count=" + str(len(digests_at_t0)))
        print("T0.brave_calls=" + str(len(search.calls)))
        print("T0.digest_vertex_calls=" + str(len(digest_provider.calls)))
        print("T0.definition_fixture_calls=" + str(definition_fixture_calls))
        print("subscription_committed_at=" + committed.committed_at)
        if not t0_ok:
            print("real_async_first_briefing=FAILED_T0")
            return 4

        try:
            with redirect_stdout(io.StringIO()):
                work = app.run_outbox_once()
        except Exception as error:  # safe type-only failure projection
            current = repository.get_application_outbox(outbox.outbox_id)
            briefing = app.get_first_briefing(
                config.user_id, committed.subscription_id,
            )
            print("T1.worker_exception=" + type(error).__name__)
            print("T1.subscription=" + briefing.subscription_status)
            print("T1.relation=" + briefing.relation_status)
            print("T1.briefing=" + briefing.status)
            print("T1.outbox=" + current.status.upper())
            print("T1.brave_calls=" + str(len(search.calls)))
            print("T1.digest_vertex_calls=" + str(len(digest_provider.calls)))
            print("secret_scan=" + (
                "PASS" if _secret_scan(root, environ) else "FAIL"
            ))
            print("real_async_first_briefing=EXTERNAL_OR_RUNTIME_FAILURE")
            return 5

        briefing = app.get_first_briefing(
            config.user_id, committed.subscription_id,
        )
        current_outbox = repository.get_application_outbox(outbox.outbox_id)
        run = repository.get_digest_run(reservation.application_run_id)
        current_reservation = repository.get_briefing_reservation(
            reservation.application_run_id,
        )
        digests = repository.list_digests(config.user_id)
        ready_at = briefing.updated_at
        ordered = (
            briefing.status == "READY"
            and datetime.fromisoformat(committed.committed_at.replace("Z", "+00:00"))
            < datetime.fromisoformat(ready_at.replace("Z", "+00:00"))
        )
        identity_reused = _reserved_identity_reused(current_reservation, run)
        secret_ok = _secret_scan(root, environ)
        print("T1.worker=" + work.worker_status)
        print("T1.subscription=" + briefing.subscription_status)
        print("T1.relation=" + briefing.relation_status)
        print("T1.briefing=" + briefing.status)
        print("T1.outbox=" + work.outbox_status)
        print("T1.reserved_application_run_reused=" + (
            "true" if identity_reused else "false"
        ))
        print("T1.accepted_evidence_count=" + str(
            _accepted_evidence_count(root)
        ))
        print("T1.digest_count=" + str(len(digests)))
        print("T1.brave_calls=" + str(len(search.calls)))
        print("T1.digest_vertex_calls=" + str(len(digest_provider.calls)))
        print("briefing_ready_at=" + ready_at)
        print("committed_before_ready=" + ("true" if ordered else "false"))
        print("secret_scan=" + ("PASS" if secret_ok else "FAIL"))
        passed = bool(
            work.outbox_status == "SUCCEEDED"
            and current_outbox.status == "completed"
            and briefing.subscription_status == "ACTIVE"
            and briefing.relation_status == "ACTIVE"
            and briefing.status == "READY"
            and len(digests) == 1 and identity_reused and ordered
            and len(search.calls) >= 1 and len(digest_provider.calls) >= 1
            and _accepted_evidence_count(root) >= 1 and secret_ok
        )
        print("real_async_first_briefing=" + ("PASS" if passed else "FAILED"))
        return 0 if passed else 6


if __name__ == "__main__":
    sys.exit(main())
