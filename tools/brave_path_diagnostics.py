"""Safe real Brave comparison for direct and HTTP-derived query shapes."""

import json
import os

from apps.digest_agent.adapters.search import (
    BRAVE_SEARCH_API_KEY, BraveSearchClient, SearchAdapterError,
    _normalize_rows, validate_safe_search_result,
)


CASES = (
    ("direct", "AI agent engineering latest developments", 3),
    ("application", "AI Agent engineering Agent 模型发布 开发工具", 3),
    ("http-derived", "AI 行业动态 Agent 模型发布 开发工具", 5),
)


def main():
    if not os.environ.get(BRAVE_SEARCH_API_KEY):
        print("CONFIGURATION_ERROR: BRAVE_SEARCH_API_KEY=MISSING")
        return 2
    passed = True
    for label, query, count in CASES:
        client = BraveSearchClient.from_environment()
        try:
            result = client.call_tool(
                "web_search", {"query": query, "max_results": count},
            )
            validate_safe_search_result(result, query, count)
            outcome = {"status": "success", "normalized_result_count": result["result_count"]}
        except SearchAdapterError as error:
            passed = False
            outcome = {"status": "failed", "error": error.code}
            if client.last_safe_result is not None:
                original = client.last_safe_result["results"]
                checked = _normalize_rows(original, count, allow_fixture_topics=True)
                mismatch_fields = sorted({
                    key for left, right in zip(original, checked)
                    for key in set(left) | set(right)
                    if left.get(key) != right.get(key)
                })
                outcome["revalidation_mismatch_fields"] = mismatch_fields
        print("path=" + label)
        print("query=" + query)
        print("safe_diagnostics=" + json.dumps(
            client.last_diagnostics, ensure_ascii=False, sort_keys=True,
        ))
        print("outcome=" + json.dumps(outcome, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
