"""Opt-in Fake/real Search + real Vertex-backed Digest provider smoke."""

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import re
import sys
import tempfile

from apps.digest_agent.adapters.provider import (
    LLM_API_KEY, LLM_API_MODE, LLM_ENDPOINT, LLM_MODEL,
    ProviderAdapterError, VertexDigestProvider,
)
from apps.digest_agent.adapters.search import (
    BRAVE_SEARCH_API_KEY, BraveSearchClient, FakeSearchClient,
)
from apps.digest_agent.adapters.sqlite import SQLiteDigestRepository
from apps.digest_agent.services import SubscriptionService
from apps.digest_agent.workflows import DigestGenerationWorkflow
from mini_harness_core.evidence import EvidenceStore


REQUEST = (
    "帮我订阅 AI Agent engineering，每天一份，600 字以内，"
    "最多 2 条，重点关注 Agent、模型发布和开发工具。"
)
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _fake_rows():
    return [{
        "url": "https://example.test/agent-runtime",
        "title": "Agent engineering runtime update",
        "snippet": "A bounded update about agent runtime and developer tools.",
        "topic_tags": ["AI Agent engineering", "Agent", "开发工具"],
    }, {
        "url": "https://example.test/model-release",
        "title": "AI model release for agent builders",
        "snippet": "A bounded model release note for agent engineering.",
        "topic_tags": ["AI Agent engineering", "模型发布"],
    }]


def _secret_scan(root):
    secrets = []
    for name in (LLM_API_KEY, BRAVE_SEARCH_API_KEY):
        value = os.environ.get(name)
        if isinstance(value, str) and value:
            secrets.append(value.encode("utf-8"))
    for path in Path(root).rglob("*"):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if any(secret in data for secret in secrets):
            return False
    return True


def _accepted_evidence_ids(root):
    store = EvidenceStore(os.path.join(root, "audit", "evidence"))
    return {
        record["evidence_id"]
        for path in Path(store.directory).glob("*.json")
        for record in (store.load(path.stem),)
        if (record["subject"]["kind"] == "search_candidate_set"
            and record["verification"]["accepted"])
    }


def _run(label, search_client, period_key):
    with tempfile.TemporaryDirectory(prefix="digest-vertex-smoke-") as root:
        repository = SQLiteDigestRepository(os.path.join(root, "digest.db"))
        subscription = SubscriptionService(
            repository,
        ).create_from_natural_language("a" * 32, REQUEST)
        try:
            provider = VertexDigestProvider.from_environment()
        except ProviderAdapterError as error:
            print(f"slice={label}")
            print("provider_identity=vertex")
            print(f"provider_error={error.code}")
            return False
        workflow = DigestGenerationWorkflow(
            repository, search_client, provider,
            os.path.join(root, "workspace"), os.path.join(root, "audit"),
        )
        with redirect_stdout(io.StringIO()):
            outcome = workflow.run(subscription.subscription_id, period_key)
        digest = (
            repository.get_digest(outcome.digest_id)
            if outcome.digest_id is not None else None
        )
        secret_ok = _secret_scan(root)
        accepted_ids = _accepted_evidence_ids(root)
        payload = digest.payload if digest is not None else {}
        items = payload.get("items", [])
        refs = payload.get("source_refs", [])
        candidate_ids = {item.get("candidate_id") for item in items}
        refs_valid = bool(refs) and all(
            ref.get("candidate_id") in candidate_ids
            and ref.get("evidence_id") in accepted_ids
            for ref in refs
        )

        print(f"slice={label}")
        print(f"provider_identity={provider.provider_identity}")
        print(f"model_identity={provider.last_model_identity or provider.model_identity}")
        print(f"candidate_item_count={len(items)}")
        print(f"source_refs_valid={'yes' if refs_valid else 'no'}")
        print(
            "character_count="
            f"{payload.get('character_count', 0)}/{subscription.max_chars}"
        )
        print(f"harness_result={outcome.harness_result.get('status', outcome.status)}")
        print(
            "digest_identity="
            f"{digest.digest_id if digest is not None else 'none'}"
        )
        if outcome.status != "completed":
            print(f"provider_or_contract_error={outcome.reason or 'INCOMPLETE'}")
            if provider.last_error is not None:
                print(f"provider_error_stage={provider.last_error['stage']}")
                diagnostics = provider.last_error.get("diagnostics")
                if diagnostics is not None:
                    print(
                        "provider_response_shape="
                        f"len:{diagnostics['content_length']},"
                        f"object:{diagnostics['starts_with_object']}/"
                        f"{diagnostics['ends_with_object']},"
                        f"fence:{diagnostics['starts_with_fence']},"
                        f"chars:{diagnostics['second_character_code']}/"
                        f"{diagnostics['last_character_code']},"
                        f"finish:{diagnostics['finish_reason']},"
                        f"error:{diagnostics['error_line']}:"
                        f"{diagnostics['error_column']}"
                    )
        print(f"secret_scan={'PASS' if secret_ok else 'FAIL'}")
        return bool(
            outcome.status == "completed" and digest is not None
            and ID_PATTERN.fullmatch(digest.digest_id)
            and refs_valid and secret_ok
        )


def main():
    missing = [
        name for name in (
            LLM_API_KEY, LLM_API_MODE, LLM_ENDPOINT, LLM_MODEL,
            BRAVE_SEARCH_API_KEY,
        )
        if not os.environ.get(name)
    ]
    if missing:
        print("CONFIGURATION_ERROR: missing=" + ",".join(missing))
        return 2
    fake_ok = _run(
        "fake-search-real-vertex", FakeSearchClient(_fake_rows()),
        "vertex-smoke-fake-search",
    )
    if not fake_ok:
        return 1
    real_ok = _run(
        "real-brave-real-vertex", BraveSearchClient.from_environment(),
        "vertex-smoke-real-brave",
    )
    return 0 if real_ok else 1


if __name__ == "__main__":
    sys.exit(main())
