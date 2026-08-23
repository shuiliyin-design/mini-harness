"""Run exactly three opt-in real Vertex generations with safe summaries."""

from contextlib import redirect_stdout
import io
import os
import statistics
import sys
import tempfile

from apps.digest_agent.adapters.provider import VertexDigestProvider
from apps.digest_agent.adapters.search import FakeSearchClient
from apps.digest_agent.adapters.sqlite import SQLiteDigestRepository
from apps.digest_agent.services import SubscriptionService
from apps.digest_agent.workflows import DigestGenerationWorkflow


USER_ID = "a" * 32


def _rows():
    topics = ("Agent", "模型发布", "开发工具", "推理", "安全")
    return [{
        "url": f"https://example.test/reliability/{index}",
        "title": f"AI 行业动态 {topic} update {index}",
        "snippet": (
            f"A bounded synthetic candidate about {topic}; "
            "used only to isolate real generation reliability. " * 3
        ),
        "topic_tags": ["AI 行业动态", topic],
    } for index, topic in enumerate(topics, 1)]


def _request(max_items):
    request = (
        "帮我订阅 AI 行业动态，每天一份，600 字以内，"
        f"最多 {max_items} 条。"
    )
    if max_items == 2:
        request += "重点关注 Agent、模型发布和开发工具。"
    return request


def _run(index, max_items):
    with tempfile.TemporaryDirectory(
        prefix="digest-vertex-reliability-",
    ) as root:
        repository = SQLiteDigestRepository(os.path.join(root, "digest.db"))
        subscription = SubscriptionService(
            repository,
        ).create_from_natural_language(USER_ID, _request(max_items))
        provider = VertexDigestProvider.from_environment()
        workflow = DigestGenerationWorkflow(
            repository, FakeSearchClient(_rows()), provider,
            os.path.join(root, "workspace"), os.path.join(root, "audit"),
        )
        with redirect_stdout(io.StringIO()):
            outcome = workflow.run(
                subscription.subscription_id,
                f"vertex-reliability-{index}",
            )
        attempts = repository.list_generation_attempts(outcome.digest_run_id)
        durations = [
            item.response_metadata.get("duration_ms")
            for item in attempts if item.response_metadata is not None
            and isinstance(item.response_metadata.get("duration_ms"), int)
        ]
        first_request = attempts[0].request_metadata if attempts else {}
        diagnostics = outcome.failure_diagnostics or {}
        print(
            f"run={index} status={outcome.status} "
            f"failure={outcome.failure_code or 'none'} "
            f"contract_subtype={outcome.failure_subtype or 'none'} "
            f"candidates={first_request.get('candidate_count', 0)} "
            f"prompt_chars={first_request.get('prompt_chars', 0)} "
            f"attempts={len(attempts)} "
            "subtypes=" + ",".join(
                item.failure_subtype or "none" for item in attempts
            ) + " latency_ms=" + ",".join(map(str, durations))
            + f" chars={diagnostics.get('actual_char_count', 0)}/"
            + str(diagnostics.get("expected_max_chars", 0))
        )
        return outcome, attempts, durations, provider.model_identity


def main():
    results = [_run(index, max_items) for index, max_items in (
        (1, 2), (2, 5), (3, 5),
    )]
    outcomes = [item[0] for item in results]
    attempts = [attempt for item in results for attempt in item[1]]
    durations = [duration for item in results for duration in item[2]]
    invalid = sum(
        attempt.failure_subtype in {
            "NON_JSON", "JSON_PARSE", "SCHEMA_MISMATCH",
        } for attempt in attempts
    )
    timeouts = sum(
        attempt.failure_subtype == "MODEL_TIMEOUT" for attempt in attempts
    )
    print("provider_identity=vertex")
    print(f"model_identity={results[0][3]}")
    print(f"logical_success={sum(value.status == 'completed' for value in outcomes)}/3")
    print(f"invalid_response_attempts={invalid}")
    print(f"timeout_attempts={timeouts}")
    print("contract_rejections=" + ",".join(
        value.failure_subtype for value in outcomes
        if value.failure_subtype is not None
    ))
    if durations:
        print(
            f"latency_ms=min:{min(durations)},"
            f"median:{int(statistics.median(durations))},max:{max(durations)}"
        )
    return 2 if any(
        value.failure_stage == "configuration" for value in outcomes
    ) else 0


if __name__ == "__main__":
    sys.exit(main())
