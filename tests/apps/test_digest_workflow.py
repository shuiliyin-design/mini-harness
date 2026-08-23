import os
import tempfile
import unittest

from apps.digest_agent.adapters.provider import FakeDigestProvider
from apps.digest_agent.adapters.search import FakeSearchClient
from apps.digest_agent.adapters.sqlite import SQLiteDigestRepository
from apps.digest_agent.services import SubscriptionService
from apps.digest_agent.workflows import DigestGenerationWorkflow
from mini_harness_core.artifacts import ArtifactStore
from mini_harness_core.evidence import EvidenceStore
from mini_harness_core.result import result_integrity_check


NOW = "2026-08-23T12:00:00Z"


class IdFactory:
    def __init__(self):
        self.value = 10

    def __call__(self):
        value = f"{self.value:032x}"
        self.value += 1
        return value


def search_rows():
    return [{
        "url": "https://example.test/agent?utm_source=test",
        "title": "Agent Runtime 发布",
        "snippet": "一个新的 Agent Runtime 与开发工具发布。",
        "published_at": "2026-08-23T10:00:00Z",
        "topic_tags": ["AI 行业动态", "Agent", "开发工具"],
    }, {
        "url": "https://example.test/model",
        "title": "模型发布",
        "snippet": "新的教学模型正式发布。",
        "published_at": "2026-08-22T10:00:00Z",
        "topic_tags": ["AI 行业动态", "模型发布"],
    }]


class DigestWorkflowTests(unittest.TestCase):
    def make(self, root, mode="valid", rows=None, max_chars=600):
        ids = IdFactory()
        repository = SQLiteDigestRepository(os.path.join(root, "digest.db"))
        service = SubscriptionService(repository, id_factory=ids, clock=lambda: NOW)
        request = (
            f"帮我订阅 AI 行业动态，每天一份，{max_chars} 字以内，"
            "重点关注 Agent、模型发布和开发工具。"
        )
        subscription = service.create_from_natural_language("a" * 32, request)
        search = FakeSearchClient(search_rows() if rows is None else rows)
        provider = FakeDigestProvider(mode)
        workflow = DigestGenerationWorkflow(
            repository, search, provider, os.path.join(root, "workspace"),
            os.path.join(root, "audit"), id_factory=ids, clock=lambda: NOW,
        )
        return repository, subscription, search, provider, workflow

    def test_e2e_a_normal_generation_binds_digest_artifact_and_result(self):
        with tempfile.TemporaryDirectory() as root:
            repository, subscription, search, provider, workflow = self.make(root)
            result = workflow.run(subscription.subscription_id, "2026-08-23")
            self.assertEqual(result.status, "completed")
            self.assertIsNotNone(result.digest_id)
            digest = repository.get_digest(result.digest_id)
            self.assertEqual(digest.artifact_id, result.artifact_id)
            self.assertEqual(digest.harness_run_id, result.harness_run_id)
            self.assertIn(result.artifact_id, result.harness_result["artifact_ids"])
            self.assertEqual(search.calls[0]["query"],
                             "AI 行业动态 Agent 模型发布 开发工具")
            self.assertEqual(provider.calls[0]["candidate_ids"], [
                item["candidate_id"] for item in digest.payload["items"]
            ])
            artifact = ArtifactStore(os.path.join(
                root, "audit", "artifacts"
            )).load(result.artifact_id)
            self.assertEqual(artifact["run_id"], result.harness_run_id)
            self.assertTrue(result_integrity_check(
                result.harness_run_id,
                os.path.join(root, "audit", "results"),
                os.path.join(root, "audit", "artifacts"),
                os.path.join(root, "audit", "evidence"),
                os.path.join(root, "audit"),
            ))

    def test_e2e_b_overlong_model_candidate_is_authoritative_incomplete(self):
        with tempfile.TemporaryDirectory() as root:
            repository, subscription, _search, _provider, workflow = self.make(
                root, mode="overlong",
            )
            result = workflow.run(subscription.subscription_id, "2026-08-23")
            self.assertEqual(result.status, "incomplete")
            self.assertIn("max_chars_exceeded", result.reason)
            self.assertIsNone(result.digest_id)
            self.assertEqual(result.harness_result["artifact_ids"], [])
            self.assertIsNone(repository.get_digest_run(result.digest_run_id).digest_id)

    def test_e2e_c_unknown_source_is_authoritative_incomplete(self):
        with tempfile.TemporaryDirectory() as root:
            repository, subscription, _search, _provider, workflow = self.make(
                root, mode="invalid_source",
            )
            result = workflow.run(subscription.subscription_id, "2026-08-23")
            self.assertEqual(result.status, "incomplete")
            self.assertIn("invalid_source_candidate", result.reason)
            self.assertIsNone(result.artifact_id)
            self.assertIsNone(repository.get_digest_run(result.digest_run_id).digest_id)

    def test_search_observation_is_not_automatically_accepted_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            _repository, subscription, _search, _provider, workflow = self.make(root)
            result = workflow.run(subscription.subscription_id, "2026-08-23")
            store = EvidenceStore(os.path.join(root, "audit", "evidence"))
            records = [store.load(name[:-5]) for name in os.listdir(store.directory)]
            observation = next(item for item in records
                               if item["evidence_type"] == "mcp_observation"
                               and item["source"]["server"] == "search")
            accepted = next(item for item in records
                            if item["subject"]["kind"] == "search_candidate_set")
            self.assertTrue(observation["verification"]["untrusted_external"])
            self.assertNotIn(observation["evidence_id"], result.harness_result["evidence_ids"])
            self.assertTrue(accepted["verification"]["accepted"])
            self.assertIn(accepted["evidence_id"], result.harness_result["evidence_ids"])
            self.assertEqual(
                accepted["references"]["candidate_evidence_id"],
                observation["evidence_id"],
            )

    def test_no_results_is_incomplete_without_model_or_digest(self):
        with tempfile.TemporaryDirectory() as root:
            repository, subscription, _search, provider, workflow = self.make(
                root, rows=[],
            )
            result = workflow.run(subscription.subscription_id, "2026-08-23")
            self.assertEqual((result.status, result.reason), ("incomplete", "no_results"))
            self.assertEqual(provider.calls, [])
            self.assertIsNone(result.digest_id)
            self.assertIsNone(repository.get_digest_run(result.digest_run_id).digest_id)

    def test_duplicate_run_returns_existing_without_search_or_synthesis(self):
        with tempfile.TemporaryDirectory() as root:
            _repository, subscription, search, provider, workflow = self.make(root)
            first = workflow.run(subscription.subscription_id, "2026-08-23")
            duplicate = workflow.run(subscription.subscription_id, "2026-08-23")
            self.assertTrue(duplicate.reused)
            self.assertEqual(duplicate.digest_run_id, first.digest_run_id)
            self.assertEqual(len(search.calls), 1)
            self.assertEqual(len(provider.calls), 1)


if __name__ == "__main__":
    unittest.main()
