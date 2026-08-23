"""Opt-in real Brave Search + FakeProvider application smoke."""

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit

from apps.digest_agent.adapters.provider import FakeDigestProvider
from apps.digest_agent.adapters.search import (
    BRAVE_SEARCH_API_KEY, BraveSearchClient,
)
from apps.digest_agent.adapters.sqlite import SQLiteDigestRepository
from apps.digest_agent.services import SubscriptionService
from apps.digest_agent.workflows import DigestGenerationWorkflow
from mini_harness_core.evidence import EvidenceStore


QUERY = "AI agent engineering latest developments"


def main():
    if not os.environ.get(BRAVE_SEARCH_API_KEY):
        print("CONFIGURATION_ERROR: BRAVE_SEARCH_API_KEY is not set")
        return 2
    with tempfile.TemporaryDirectory(prefix="digest-brave-smoke-") as root:
        repository = SQLiteDigestRepository(os.path.join(root, "digest.db"))
        subscription = SubscriptionService(repository).create_from_natural_language(
            "a" * 32,
            f"帮我订阅 {QUERY}，每天一份，600 字以内，最多 1 条。",
        )
        search = BraveSearchClient.from_environment()
        workflow = DigestGenerationWorkflow(
            repository, search, FakeDigestProvider(),
            os.path.join(root, "workspace"), os.path.join(root, "audit"),
        )
        with redirect_stdout(io.StringIO()):
            outcome = workflow.run(subscription.subscription_id, "manual-smoke")
        safe = search.last_safe_result
        if safe is None:
            code = (search.last_error or {}).get("code", "INVALID_RESPONSE")
            print(f"search_error={code}")
            return 1

        store = EvidenceStore(os.path.join(root, "audit", "evidence"))
        records = [store.load(path.stem)
                   for path in Path(store.directory).glob("*.json")]
        accepted = next(
            item for item in records
            if item["subject"]["kind"] == "search_candidate_set"
            and item["verification"]["accepted"]
        )
        print(f"query={QUERY}")
        print(f"normalized_result_count={safe['result_count']}")
        for item in safe["results"]:
            print(f"result={item['title']} | {urlsplit(item['url']).hostname}")
        print(f"observation_identity={safe['observation_identity']}")
        print(f"candidate_set_identity={accepted['subject']['target']}")
        print(f"evidence_id={accepted['evidence_id']}")
        print(f"harness_result={outcome.harness_result['status']}")
        if outcome.status != "completed" or outcome.digest_id is None:
            print(f"digest_error={outcome.reason or 'INCOMPLETE'}")
            return 1
        digest = repository.get_digest(outcome.digest_id)
        if digest is None:
            print("digest_error=PROJECTION_MISSING")
            return 1
        print(f"digest_id={digest.digest_id}")
        print(
            "output_contract="
            f"{digest.payload['character_count']}<={subscription.max_chars}"
        )
        print(
            "candidate_provenance="
            f"{len(digest.payload['source_refs'])} source refs -> "
            f"{accepted['evidence_id']}"
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
