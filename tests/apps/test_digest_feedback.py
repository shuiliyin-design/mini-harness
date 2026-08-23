import os
import sqlite3
import tempfile
import unittest

from apps.digest_agent.adapters.provider import FakeDigestProvider
from apps.digest_agent.adapters.search import FakeSearchClient
from apps.digest_agent.adapters.sqlite import SQLiteDigestRepository
from apps.digest_agent.domain import (
    FEEDBACK_DELTAS, PROFILE_WEIGHT_MAX, PROFILE_WEIGHT_MIN,
    Feedback, InterestProfile, TopicWeight, normalize_candidates,
    project_profile, rank_candidates, SearchObservation,
)
from apps.digest_agent.services import FeedbackService, SubscriptionService
from apps.digest_agent.workflows import DigestGenerationWorkflow


NOW = "2026-08-23T12:00:00Z"


class IdFactory:
    def __init__(self):
        self.value = 100

    def __call__(self):
        value = f"{self.value:032x}"
        self.value += 1
        return value


def balanced_rows():
    return [{
        "url": "https://example.test/agent",
        "title": "Agent 工具更新",
        "snippet": "Agent 工具新增教学模式。",
        "published_at": "2026-08-23T10:00:00Z",
        "topic_tags": ["AI 行业动态", "Agent"],
    }, {
        "url": "https://example.test/model",
        "title": "模型发布更新",
        "snippet": "一个新模型发布。",
        "published_at": "2026-08-23T10:00:00Z",
        "topic_tags": ["AI 行业动态", "模型发布"],
    }]


class FeedbackSliceCase(unittest.TestCase):
    def make(self, root):
        ids = IdFactory()
        repository = SQLiteDigestRepository(os.path.join(root, "digest.db"))
        subscriptions = SubscriptionService(
            repository, id_factory=ids, clock=lambda: NOW,
        )
        subscription = subscriptions.create_from_natural_language(
            "a" * 32,
            "帮我订阅 AI 行业动态，每天一份，600 字以内，最多 2 条，"
            "重点关注 Agent、模型发布。",
        )
        provider = FakeDigestProvider()
        workflow = DigestGenerationWorkflow(
            repository, FakeSearchClient(balanced_rows()), provider,
            os.path.join(root, "workspace"), os.path.join(root, "audit"),
            id_factory=ids, clock=lambda: NOW,
        )
        feedback = FeedbackService(repository, clock=lambda: NOW)
        return repository, subscription, provider, workflow, feedback

    @staticmethod
    def weights(profile):
        return {item.topic_key: item.weight for item in profile.topic_weights}

    def generated(self, repository, workflow, subscription, period="2026-08-23"):
        result = workflow.run(subscription.subscription_id, period)
        self.assertEqual(result.status, "completed")
        return result, repository.get_digest(result.digest_id)


class FeedbackDomainTests(FeedbackSliceCase):
    def test_feedback_identity_is_stable_and_event_scoped(self):
        values = dict(
            user_id="a" * 32, digest_id="b" * 32, item_id="c" * 32,
            feedback_type="liked", event_key="tap-1",
        )
        self.assertEqual(Feedback(**values).feedback_id, Feedback(**values).feedback_id)
        values["event_key"] = "tap-2"
        self.assertNotEqual(
            Feedback(**{**values, "event_key": "tap-1"}).feedback_id,
            Feedback(**values).feedback_id,
        )

    def test_profile_projection_has_only_safe_bounded_state(self):
        with tempfile.TemporaryDirectory() as root:
            _repo, subscription, _provider, _workflow, _feedback = self.make(root)
            profile = InterestProfile(
                subscription.user_id, 3, 1,
                (TopicWeight("Agent", 7), TopicWeight("unrelated", 19)), NOW,
            )
            payload = project_profile(profile, subscription).as_dict()
            self.assertEqual(payload["topic_weights"], [
                {"topic_key": "agent", "weight": 7},
            ])
            self.assertNotIn("user_id", payload)
            self.assertNotIn("interactions", payload)
            self.assertNotIn("updated_at", payload)

    def test_score_breakdown_is_complete_and_deterministic(self):
        with tempfile.TemporaryDirectory() as root:
            _repo, subscription, _provider, _workflow, _feedback = self.make(root)
            observation = SearchObservation(
                "b" * 32, "AI", NOW, tuple(balanced_rows()),
            )
            candidates = normalize_candidates(observation, "c" * 32)
            profile = InterestProfile(
                subscription.user_id, 1, 1, (TopicWeight("agent", 3),), NOW,
            )
            projection = project_profile(profile, subscription)
            seen = {candidates[0].content_identity}
            first = rank_candidates(candidates, subscription, NOW, projection, seen)
            second = rank_candidates(candidates, subscription, NOW, projection, seen)
            self.assertEqual(first, second)
            for ranked in first:
                self.assertEqual(
                    tuple(name for name, _value in ranked.score_breakdown),
                    ("subscription_topic", "focus_topics", "profile_weight",
                     "freshness", "already_seen_penalty"),
                )
                self.assertEqual(ranked.score, sum(
                    value for _name, value in ranked.score_breakdown
                ))


class FeedbackPersistenceTests(FeedbackSliceCase):
    def test_liked_increases_configured_weight(self):
        with tempfile.TemporaryDirectory() as root:
            repository, sub, _provider, workflow, service = self.make(root)
            _result, digest = self.generated(repository, workflow, sub)
            item = digest.payload["items"][0]
            result = service.record(
                sub.user_id, digest.digest_id, "liked", "like-1", item["item_id"],
            )
            self.assertTrue(result.applied)
            self.assertEqual(
                self.weights(result.profile)[item["topic_tags"][1].casefold()],
                FEEDBACK_DELTAS["liked"],
            )

    def test_dismissed_decreases_configured_weight(self):
        with tempfile.TemporaryDirectory() as root:
            repository, sub, _provider, workflow, service = self.make(root)
            _result, digest = self.generated(repository, workflow, sub)
            item = digest.payload["items"][0]
            result = service.record(
                sub.user_id, digest.digest_id, "dismissed", "dismiss-1",
                item["item_id"],
            )
            self.assertEqual(
                self.weights(result.profile)[item["topic_tags"][1].casefold()],
                FEEDBACK_DELTAS["dismissed"],
            )

    def test_opened_is_not_liked_and_saved_has_its_own_semantics(self):
        with tempfile.TemporaryDirectory() as root:
            repository, sub, _provider, workflow, service = self.make(root)
            _result, digest = self.generated(repository, workflow, sub)
            opened = service.record(
                sub.user_id, digest.digest_id, "opened", "open-1",
            )
            item = digest.payload["items"][0]
            saved = service.record(
                sub.user_id, digest.digest_id, "saved", "save-1", item["item_id"],
            )
            topic = item["topic_tags"][1].casefold()
            self.assertEqual(self.weights(opened.profile)[topic], 1)
            self.assertEqual(self.weights(saved.profile)[topic], 5)
            self.assertNotEqual(FEEDBACK_DELTAS["opened"], FEEDBACK_DELTAS["liked"])
            self.assertEqual(FEEDBACK_DELTAS["saved"], 4)

    def test_duplicate_feedback_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            repository, sub, _provider, workflow, service = self.make(root)
            _result, digest = self.generated(repository, workflow, sub)
            item = digest.payload["items"][0]
            args = (
                sub.user_id, digest.digest_id, "liked", "same-event",
                item["item_id"],
            )
            first = service.record(*args)
            duplicate = service.record(*args)
            self.assertTrue(first.applied)
            self.assertFalse(duplicate.applied)
            self.assertEqual(duplicate.feedback_id, first.feedback_id)
            self.assertEqual(duplicate.profile, first.profile)

    def test_weights_are_bounded_and_profile_persists(self):
        with tempfile.TemporaryDirectory() as root:
            repository, sub, _provider, workflow, service = self.make(root)
            _result, digest = self.generated(repository, workflow, sub)
            item = digest.payload["items"][0]
            for index in range(8):
                service.record(
                    sub.user_id, digest.digest_id, "saved", f"save-{index}",
                    item["item_id"],
                )
            topic = item["topic_tags"][1].casefold()
            self.assertEqual(
                self.weights(repository.get_profile(sub.user_id))[topic],
                PROFILE_WEIGHT_MAX,
            )
            for index in range(20):
                service.record(
                    sub.user_id, digest.digest_id, "dismissed", f"dismiss-{index}",
                    item["item_id"],
                )
            reopened = SQLiteDigestRepository(repository.path)
            self.assertEqual(
                self.weights(reopened.get_profile(sub.user_id))[topic],
                PROFILE_WEIGHT_MIN,
            )

    def test_feedback_failure_does_not_change_profile_or_old_result(self):
        with tempfile.TemporaryDirectory() as root:
            repository, sub, _provider, workflow, _service = self.make(root)
            result, digest = self.generated(repository, workflow, sub)
            original_run = repository.get_digest_run(result.digest_run_id)

            with repository.connect() as connection:
                connection.execute("""
                    CREATE TRIGGER fail_profile_update
                    BEFORE INSERT ON profile_updates
                    BEGIN SELECT RAISE(ABORT, 'simulated persistence failure'); END
                """)
            service = FeedbackService(repository, clock=lambda: NOW)
            with self.assertRaises(sqlite3.IntegrityError):
                service.record(
                    sub.user_id, digest.digest_id, "liked", "failure",
                    digest.payload["items"][0]["item_id"],
                )
            self.assertIsNone(repository.get_profile(sub.user_id))
            with repository.connect() as connection:
                count = connection.execute(
                    "SELECT count(*) FROM interactions"
                ).fetchone()[0]
            self.assertEqual(count, 0)
            self.assertEqual(repository.get_digest_run(result.digest_run_id), original_run)


class FeedbackRankingE2ETests(FeedbackSliceCase):
    def test_e2e_a_initial_profile_is_bound_to_digest(self):
        with tempfile.TemporaryDirectory() as root:
            repository, sub, provider, workflow, _service = self.make(root)
            result, digest = self.generated(repository, workflow, sub)
            snapshot = digest.payload["profile_snapshot"]
            run = repository.get_digest_run(result.digest_run_id)
            self.assertEqual(snapshot["profile_version"], 0)
            self.assertEqual(snapshot["topic_weights"], [])
            self.assertEqual(run.profile_projection_id, snapshot["projection_id"])
            self.assertEqual(provider.calls[0]["profile_projection"], snapshot)

    def test_e2e_b_liked_causes_explainable_next_run_rise(self):
        with tempfile.TemporaryDirectory() as root:
            repository, sub, provider, workflow, service = self.make(root)
            first_result, first = self.generated(repository, workflow, sub)
            target = first.payload["items"][1]
            before_score = target["score"]
            service.record(
                sub.user_id, first.digest_id, "liked", "like-target",
                target["item_id"],
            )
            _second_result, second = self.generated(
                repository, workflow, sub, "2026-08-24",
            )
            self.assertEqual(second.payload["items"][0]["candidate_id"],
                             target["candidate_id"])
            after = second.payload["items"][0]
            breakdown = {
                item["component"]: item["value"]
                for item in after["score_breakdown"]
            }
            self.assertEqual(breakdown["profile_weight"], 12)
            self.assertEqual(breakdown["already_seen_penalty"], -100)
            self.assertEqual(second.payload["profile_snapshot"]["profile_version"], 1)
            self.assertEqual(first.payload["profile_snapshot"]["profile_version"], 0)
            self.assertEqual(repository.get_digest(first_result.digest_id), first)
            self.assertEqual(provider.calls[0]["candidate_ids"][1], target["candidate_id"])
            self.assertEqual(provider.calls[1]["candidate_ids"][0], target["candidate_id"])
            self.assertLess(before_score - 100, after["score"])

    def test_e2e_c_dismissed_causes_explainable_next_run_fall(self):
        with tempfile.TemporaryDirectory() as root:
            repository, sub, provider, workflow, service = self.make(root)
            _first_result, first = self.generated(repository, workflow, sub)
            target = first.payload["items"][0]
            service.record(
                sub.user_id, first.digest_id, "dismissed", "dismiss-target",
                target["item_id"],
            )
            _second_result, second = self.generated(
                repository, workflow, sub, "2026-08-24",
            )
            self.assertEqual(second.payload["items"][1]["candidate_id"],
                             target["candidate_id"])
            after = second.payload["items"][1]
            breakdown = {
                item["component"]: item["value"]
                for item in after["score_breakdown"]
            }
            self.assertEqual(breakdown["profile_weight"], -12)
            self.assertEqual(provider.calls[0]["candidate_ids"][0], target["candidate_id"])
            self.assertEqual(provider.calls[1]["candidate_ids"][1], target["candidate_id"])


if __name__ == "__main__":
    unittest.main()
