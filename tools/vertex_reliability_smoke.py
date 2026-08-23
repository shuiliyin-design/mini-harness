"""Bounded real Vertex compatibility gate with safe metadata only."""

from collections import Counter
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
REPETITIONS = 2
SCENARIOS = (
    {
        "name": "two-focus", "max_items": 2, "max_chars": 600,
        "focus": "重点关注 Agent、模型发布和开发工具。",
        "snippet_repeat": 1, "topics": ("Agent", "模型发布"),
    },
    {
        "name": "five-no-focus", "max_items": 5, "max_chars": 900,
        "focus": "",
        "snippet_repeat": 1,
        "topics": ("Agent", "模型发布", "开发工具", "推理", "安全"),
    },
    {
        "name": "long-snippet", "max_items": 3, "max_chars": 900,
        "focus": "重点关注 Agent 和开发工具。",
        "snippet_repeat": 8,
        "topics": ("Agent", "开发工具", "模型发布"),
    },
    {
        "name": "chinese-many-refs", "max_items": 5, "max_chars": 900,
        "focus": "重点关注智能体、模型发布、开发工具、推理和安全。",
        "snippet_repeat": 2,
        "topics": ("智能体", "模型发布", "开发工具", "推理", "安全"),
    },
    {
        "name": "browser-subscription-shape", "max_items": 5,
        "max_chars": 600,
        "focus": "重点关注 Agent、模型发布和开发工具。",
        "snippet_repeat": 3,
        "topics": ("Agent", "模型发布", "开发工具", "基础设施", "安全"),
    },
)

PROVIDER_GATE_CRITERIA = (
    "transport", "envelope", "parse", "schema", "refs_parseable",
    "contract",
)
EXPECTED_MECHANISM = "strict_flat_scalar_tool_requested_prompt_reinforced"


def provider_compatibility_gate_passes(results):
    """Return true only when every call passes the complete provider chain."""
    return bool(results) and all(
        all(result.get(name) is True for name in PROVIDER_GATE_CRITERIA)
        and result.get("safe_ledger") is True
        and result.get("mechanism") == EXPECTED_MECHANISM
        for result in results
    )


def _rows(scenario):
    return [{
        "url": f"https://example.test/acceptance/{scenario['name']}/{index}",
        "title": f"AI 行业动态：{topic}更新 {index}",
        "snippet": (
            f"这是一条关于{topic}的脱敏候选摘要，仅用于验证结构化生成兼容性。"
            * scenario["snippet_repeat"]
        ),
        "topic_tags": ["AI 行业动态", topic],
    } for index, topic in enumerate(scenario["topics"], 1)]


def _request(scenario):
    return (
        f"帮我订阅 AI 行业动态，每天一份，{scenario['max_chars']} 字以内，最多 "
        f"{scenario['max_items']} 条。{scenario['focus']}"
    )


def _run(scenario, repetition):
    with tempfile.TemporaryDirectory(prefix="digest-vertex-acceptance-") as root:
        repository = SQLiteDigestRepository(os.path.join(root, "digest.db"))
        subscription = SubscriptionService(repository).create_from_natural_language(
            USER_ID, _request(scenario),
        )
        provider = VertexDigestProvider.from_environment()
        workflow = DigestGenerationWorkflow(
            repository, FakeSearchClient(_rows(scenario)), provider,
            os.path.join(root, "workspace"), os.path.join(root, "audit"),
            generation_max_attempts=1,
        )
        with redirect_stdout(io.StringIO()):
            outcome = workflow.run(
                subscription.subscription_id,
                f"acceptance-{scenario['name']}-{repetition}",
            )
        attempt = repository.list_generation_attempts(
            outcome.digest_run_id,
        )[0]
        response = attempt.response_metadata or {}
        request = attempt.request_metadata
        safe_ledger = set(response) <= {
            "http_status", "response_bytes", "response_sha256",
            "response_chars", "content_sha256", "finish_reason",
            "json_parse_succeeded", "schema_validation_succeeded",
            "duration_ms", "max_output_tokens", "output_tokens",
            "parse_error_line", "parse_error_column", "starts_with_object",
            "ends_with_object", "failure_subtype", "json_lexical_subtype",
            "schema_mismatch_rule", "schema_mismatch_field",
            "schema_mismatch_item_index", "schema_actual_chars",
            "schema_expected_min_chars", "schema_expected_max_chars",
            "schema_control_char_count", "schema_actual_item_count",
            "schema_actual_ref_count", "choice_count", "message_type",
            "content_presence", "content_type", "tool_calls_presence",
            "tool_call_count", "tool_kind_match", "function_name_match",
            "arguments_presence", "arguments_type", "payload_source",
            "envelope_error", "payload_top_type", "payload_summary_type",
            "payload_items_type", "payload_selected_source_refs_type",
            "payload_items_string_chars",
            "payload_items_string_starts_array",
            "payload_items_string_ends_array",
            "payload_items_nested_json_parse", "payload_items_nested_type",
        }
        result = {
            "scenario": scenario["name"],
            "transport": response.get("http_status") == 200,
            "parse": response.get("json_parse_succeeded") is True,
            "envelope": (
                response.get("payload_source") == "tool_arguments"
                and response.get("envelope_error") is None
            ),
            "schema": response.get("schema_validation_succeeded") is True,
            "refs_parseable": response.get("schema_validation_succeeded") is True,
            "contract": outcome.status == "completed",
            "contract_subtype": (
                outcome.failure_subtype
                if outcome.failure_stage == "contract" else None
            ),
            "timeout": attempt.failure_subtype == "MODEL_TIMEOUT",
            "lexical": response.get("json_lexical_subtype"),
            "schema_subtype": response.get("schema_mismatch_rule"),
            "wire_item_type": response.get("payload_items_type"),
            "wire_ref_type": response.get(
                "payload_selected_source_refs_type"
            ),
            "envelope_subtype": response.get("envelope_error"),
            "latency_ms": response.get("duration_ms"),
            "mechanism": request.get("structured_output_mechanism"),
            "model": request.get("model_identity"),
            "safe_ledger": safe_ledger,
        }
        print(
            f"case={scenario['name']} repetition={repetition} "
            f"transport={result['transport']} parse={result['parse']} "
            f"envelope={result['envelope']} "
            f"schema={result['schema']} refs={result['refs_parseable']} "
            f"contract={result['contract']} "
            f"contract_subtype={result['contract_subtype'] or 'none'} "
            f"lexical={result['lexical'] or 'none'} "
            f"schema_subtype={result['schema_subtype'] or 'none'} "
            f"wire_item_type={result['wire_item_type'] or 'none'} "
            f"wire_ref_type={result['wire_ref_type'] or 'none'} "
            f"envelope_subtype={result['envelope_subtype'] or 'none'} "
            f"latency_ms={result['latency_ms'] or 0}"
        )
        return result


def main():
    if os.environ.get("LLM_API_MODE") != "chat-completions":
        print("acceptance=NOT_RUN reason=native_schema_mode_required")
        return 2
    results = [
        _run(scenario, repetition)
        for scenario in SCENARIOS
        for repetition in range(1, REPETITIONS + 1)
    ]
    latencies = [
        result["latency_ms"] for result in results
        if isinstance(result["latency_ms"], int)
    ]
    lexical = Counter(
        result["lexical"] for result in results if result["lexical"]
    )
    schema_subtypes = Counter(
        result["schema_subtype"] for result in results
        if result["schema_subtype"]
    )
    envelope_subtypes = Counter(
        result["envelope_subtype"] for result in results
        if result["envelope_subtype"]
    )
    print("provider_identity=vertex")
    print(f"model_identity={results[0]['model']}")
    print(f"structured_output_mechanism={results[0]['mechanism']}")
    print(f"total_calls={len(results)}")
    for name in (
        "transport", "envelope", "parse", "schema", "refs_parseable",
        "contract",
    ):
        print(f"{name}_success={sum(result[name] for result in results)}")
    print(f"timeout={sum(result['timeout'] for result in results)}")
    print("lexical_subtypes=" + (
        ",".join(f"{name}:{count}" for name, count in sorted(lexical.items()))
        if lexical else "none"
    ))
    print("schema_subtypes=" + (
        ",".join(
            f"{name}:{count}" for name, count in sorted(schema_subtypes.items())
        ) if schema_subtypes else "none"
    ))
    print("envelope_subtypes=" + (
        ",".join(
            f"{name}:{count}" for name, count in sorted(envelope_subtypes.items())
        ) if envelope_subtypes else "none"
    ))
    print("contract_rejections=" + (
        ",".join(
            result["contract_subtype"] for result in results
            if result["contract_subtype"]
        ) or "none"
    ))
    if latencies:
        print(
            f"latency_ms=min:{min(latencies)},"
            f"median:{int(statistics.median(latencies))},max:{max(latencies)}"
        )
    provider_gate = provider_compatibility_gate_passes(results)
    print(f"real_gateway_provider_gate={'PASS' if provider_gate else 'FAIL'}")
    return 0 if provider_gate else 1


if __name__ == "__main__":
    sys.exit(main())
