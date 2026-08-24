"""Opt-in real Vertex smoke for the durable Definition conversation only."""

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import sys
import tempfile
import uuid

from apps.digest_agent.adapters.provider import (
    LLM_API_KEY, LLM_API_MODE, LLM_ENDPOINT, LLM_MODEL,
)
from apps.digest_agent.bootstrap import (
    DigestAppConfig, bootstrap_application, check_readiness,
    load_application_environment,
)


REQUEST = "帮我订阅 AI 行业动态"
ANSWER = (
    "每天，使用中文，每篇 600 字以内，最多 5 条，重点关注 Agent、"
    "模型发布和开发工具，暂时不需要通知。"
)


def _secret_scan(root, environ):
    values = [
        environ[name].encode("utf-8")
        for name in (LLM_API_KEY,)
        if environ.get(name)
    ]
    return not any(
        secret in path.read_bytes()
        for path in Path(root).rglob("*") if path.is_file()
        for secret in values
    )


def main():
    with tempfile.TemporaryDirectory(prefix="digest-definition-smoke-") as root:
        config = DigestAppConfig(
            os.path.join(root, "digest.db"),
            os.path.join(root, "workspace"), os.path.join(root, "audit"),
            search_provider="fake", llm_provider="vertex",
            delivery_provider="fake",
        )
        environ = load_application_environment()
        report = check_readiness(config, environ=environ)
        missing = tuple(
            item.name for item in report.checks if item.status == "MISSING"
        )
        if report.status != "READY":
            print("CONFIGURATION_ERROR: missing=" + ",".join(missing))
            return 2
        app = bootstrap_application(config, environ=environ)
        with redirect_stdout(io.StringIO()):
            view = app.start_subscription_conversation(
                config.user_id, REQUEST, "smoke-" + uuid.uuid4().hex,
            )
            for index in range(1, 5):
                if view.status != "WAITING_FOR_ANSWER":
                    break
                view = app.continue_subscription_conversation(
                    config.user_id, view.conversation_id, ANSWER,
                    f"smoke-answer-{index}-" + uuid.uuid4().hex,
                )
        subscriptions = app.list_subscriptions(config.user_id)
        digests = app.list_digests(config.user_id)
        secret_ok = _secret_scan(root, environ)
        print("provider_identity=vertex")
        print(f"conversation_id={view.conversation_id}")
        print(f"conversation_status={view.status}")
        print(f"turn_count={view.turn_count}")
        print(f"latest_outcome={view.latest_outcome or 'none'}")
        print(f"subscription_count={len(subscriptions)}")
        print(f"digest_count={len(digests)}")
        print(f"secret_scan={'PASS' if secret_ok else 'FAIL'}")
        return 0 if (
            view.status == "DEFINITION_ACCEPTED"
            and len(subscriptions) == 0 and len(digests) == 0 and secret_ok
        ) else 1


if __name__ == "__main__":
    sys.exit(main())
