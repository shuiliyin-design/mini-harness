import copy
import unittest

from apps.digest_agent.adapters.provider import FakeDigestProvider
from apps.digest_agent.contracts import evaluate_digest_contract
from apps.digest_agent.domain import (
    DefinitionCandidate, DomainError, InterestProfile, SearchObservation,
    Subscription, TopicWeight, normalize_candidates,
    materialize_conversation_definition, normalize_conversation_envelope,
    normalize_definition_envelope,
    project_profile, rank_candidates, validate_definition_protocol,
)


NOW = "2026-08-23T12:00:00Z"
EVIDENCE = "e" * 32


def subscription(**changes):
    values = {
        "subscription_id": "1" * 32, "user_id": "2" * 32,
        "topic": "AI 行业动态", "natural_language_request": "订阅 AI 行业动态",
        "cadence": "daily", "language": "zh-CN", "max_chars": 600,
        "max_items": 5, "focus_topics": ("Agent", "开发工具"),
        "delivery_channel": "none", "enabled": True, "version": 1,
        "created_at": NOW, "updated_at": NOW,
    }
    values.update(changes)
    return Subscription(**values)


def observation(results):
    return SearchObservation("3" * 32, "AI Agent", NOW, tuple(results))


def raw(url, title, published="2026-08-23T10:00:00Z", tags=None):
    return {
        "url": url, "title": title, "snippet": f"{title} 摘要",
        "published_at": published,
        "topic_tags": tags or ["AI 行业动态", "Agent"],
    }


class SubscriptionValidationTests(unittest.TestCase):
    def test_subscription_accepts_first_class_limits(self):
        value = subscription(max_chars=600, max_items=3)
        self.assertEqual((value.max_chars, value.max_items), (600, 3))

    def test_subscription_rejects_bool_and_out_of_range_limits(self):
        for changes in ({"max_chars": True}, {"max_chars": 99},
                        {"max_items": False}, {"max_items": 11}):
            with self.subTest(changes=changes), self.assertRaises(DomainError):
                subscription(**changes)


class DefinitionProtocolValidationTests(unittest.TestCase):
    def test_strict_done_becomes_validated_candidate(self):
        payload = {
            "protocol_version": 1, "type": "DONE",
            "definition": {
                "topic": "AI 行业动态", "language": "zh-CN",
                "cadence": "daily", "max_chars": 600, "max_items": 5,
                "focus_topics": ["Agent"],
                "delivery_preference": "none",
            },
        }
        normalized, candidate = validate_definition_protocol(payload)
        self.assertIsInstance(candidate, DefinitionCandidate)
        self.assertEqual(normalized, payload)

    def test_protocol_rejects_extra_fields_and_invalid_business_values(self):
        with self.assertRaises(DomainError):
            normalize_definition_envelope({
                "protocol_version": True, "type": "REJECT", "reason": "no",
            })
        with self.assertRaises(DomainError):
            normalize_definition_envelope({
                "protocol_version": 1, "type": "NEXT_QUESTION",
                "question": "需要补充吗？", "extra": True,
            })
        with self.assertRaises(DomainError):
            validate_definition_protocol({
                "protocol_version": 1, "type": "DONE",
                "definition": {
                    "topic": "AI", "language": "zh-CN",
                    "cadence": "weekly", "max_chars": 600,
                    "max_items": 5, "focus_topics": [],
                    "delivery_preference": "none",
                },
            })

    def test_conversation_intent_materializes_defaults_and_provenance(self):
        payload = {
            "protocol_version": 2, "type": "DONE",
            "intent": {
                "topic": {"value": "深圳往返武汉的机票优惠", "source_turn": 1},
                "constraints": [
                    {"value": "低于800元", "source_turn": 2},
                ],
                "goal": {"value": "找到合适的往返机票", "source_turn": 1},
                "trigger": {"value": "票价低于800元时提醒", "source_turn": 2},
                "time_window": {"value": "9 月", "source_turn": 2},
                "locations": [
                    {"value": "深圳", "source_turn": 1},
                    {"value": "武汉", "source_turn": 1},
                ],
                "focus_topics": [],
                "preferences": {
                    "max_chars": {
                        "value": 800, "source_turn": 1,
                    },
                    "delivery_preference": {
                        "value": "termux_notification", "source_turn": 2,
                    },
                },
            },
        }
        definition = materialize_conversation_definition(
            payload, 2, (
                "帮我关注深圳往返武汉的机票优惠，内容不超过 800 字",
                "9 月往返，低于 800 元时提醒我，并使用本机通知",
            ),
        )["definition"]
        self.assertEqual(
            (definition["language"], definition["max_chars"],
             definition["max_items"], definition["cadence"]),
            ("zh-CN", 800, 5, "daily"),
        )
        self.assertEqual(definition["provenance"], {
            "topic": "USER_EXPLICIT",
            "constraints": "USER_CONFIRMED",
            "goal": "USER_EXPLICIT",
            "trigger": "USER_CONFIRMED",
            "time_window": "USER_CONFIRMED",
            "locations": "USER_EXPLICIT",
            "focus_topics": "PRODUCT_DEFAULT",
            "language": "PRODUCT_DEFAULT",
            "cadence": "POLICY_DEFAULT",
            "max_chars": "USER_EXPLICIT",
            "max_items": "PRODUCT_DEFAULT",
            "delivery_preference": "USER_CONFIRMED",
        })

    def test_conversation_source_cannot_claim_a_future_turn(self):
        payload = {
            "protocol_version": 2, "type": "DONE",
            "intent": {
                "topic": {"value": "OpenAI 新模型发布", "source_turn": 2},
                "constraints": [], "goal": None,
                "trigger": None, "time_window": None,
                "locations": [], "focus_topics": [], "preferences": {},
            },
        }
        with self.assertRaises(DomainError):
            materialize_conversation_definition(payload, 1)

    def test_v2_clarification_rejects_internal_schema_questions(self):
        for question in (
            "最多几条资讯？", "每篇最多多少字？", "需要本机通知吗？",
            "请填写 max_chars 字段。", "希望中文还是英文？",
        ):
            with self.subTest(question=question), self.assertRaises(DomainError):
                normalize_conversation_envelope({
                    "protocol_version": 2,
                    "type": "NEXT_QUESTION", "question": question,
                })
        self.assertEqual(
            normalize_conversation_envelope({
                "protocol_version": 2, "type": "NEXT_QUESTION",
                "question": "你计划哪段日期出发和返回？",
            })["type"],
            "NEXT_QUESTION",
        )

    def test_model_cannot_label_an_unstated_default_as_user_preference(self):
        payload = {
            "protocol_version": 2, "type": "DONE",
            "intent": {
                "topic": {"value": "OpenAI 新模型发布", "source_turn": 1},
                "constraints": [], "goal": None, "trigger": None,
                "time_window": None, "locations": [], "focus_topics": [],
                "preferences": {
                    "max_chars": {"value": 600, "source_turn": 1},
                },
            },
        }
        with self.assertRaisesRegex(DomainError, "explicit user preference"):
            materialize_conversation_definition(
                payload, 1, ("关注 OpenAI 新模型发布",),
            )


class CandidateRuleTests(unittest.TestCase):
    def test_normalization_canonicalizes_and_deduplicates(self):
        candidates = normalize_candidates(observation([
            raw("https://EXAMPLE.test/a?utm_source=x", "Agent Release"),
            raw("https://example.test/a", "Agent Release"),
        ]), EVIDENCE)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].canonical_url, "https://example.test/a")
        self.assertEqual(candidates[0].evidence_id, EVIDENCE)

    def test_ranking_is_deterministic_and_respects_max_items(self):
        candidates = normalize_candidates(observation([
            raw("https://example.test/tools", "Tools", tags=["开发工具"]),
            raw("https://example.test/agent", "Agent", tags=["AI 行业动态", "Agent"]),
            raw("https://example.test/old", "Old", "2026-08-10T10:00:00Z"),
        ]), EVIDENCE)
        ranked = rank_candidates(candidates, subscription(max_items=2), NOW)
        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked[0].candidate.title, "Agent")
        self.assertGreater(ranked[0].score, ranked[1].score)
        self.assertEqual(ranked, rank_candidates(candidates, subscription(max_items=2), NOW))


class DigestContractTests(unittest.TestCase):
    def setUp(self):
        self.subscription = subscription()
        candidates = normalize_candidates(observation([
            raw("https://example.test/one", "One"),
            raw("https://example.test/two", "Two"),
        ]), EVIDENCE)
        self.ranked = rank_candidates(candidates, self.subscription, NOW)

    def payload(self, provider=None, selected=None, sub=None):
        provider = provider or FakeDigestProvider()
        selected = selected or self.ranked
        sub = sub or self.subscription
        return provider.synthesize(sub, selected, "2026-08-23", "4" * 32)

    def evaluate(self, payload, sub=None, selected=None):
        selected = selected or self.ranked
        return evaluate_digest_contract(
            payload, sub or self.subscription,
            selected, {EVIDENCE},
        )

    def test_max_chars_is_computed_not_trusted(self):
        payload = self.payload(FakeDigestProvider("overlong"))
        payload["character_count"] = 1
        result = self.evaluate(payload)
        self.assertFalse(result.satisfied)
        self.assertIn("character_count_mismatch", result.violations)
        self.assertIn("max_chars_exceeded", result.violations)
        self.assertEqual(result.failure_subtype, "too_long")
        self.assertEqual(
            (result.diagnostics["expected_max_chars"],
             result.diagnostics["actual_char_count"]),
            (600, len(payload["rendered_text"])),
        )

    def test_max_items_is_deterministic(self):
        sub = subscription(max_items=1)
        payload = self.payload(selected=self.ranked, sub=sub)
        result = self.evaluate(payload, sub=sub, selected=self.ranked)
        self.assertFalse(result.satisfied)
        self.assertIn("max_items_exceeded", result.violations)
        self.assertEqual(result.failure_subtype, "too_many_items")

    def test_invalid_source_ref_is_rejected(self):
        result = self.evaluate(self.payload(FakeDigestProvider("invalid_source")))
        self.assertFalse(result.satisfied)
        self.assertIn("invalid_source_candidate", result.violations)
        self.assertEqual(result.failure_subtype, "invalid_source_ref")

    def test_duplicate_item_is_rejected(self):
        payload = self.payload()
        payload["items"].append(copy.deepcopy(payload["items"][0]))
        result = self.evaluate(payload)
        self.assertFalse(result.satisfied)
        self.assertIn("duplicate_item", result.violations)
        self.assertEqual(result.failure_subtype, "duplicate_item")

    def test_invalid_content_ref_subtype_is_deterministic(self):
        payload = self.payload()
        payload["items"][0]["candidate_id"] = "f" * 32
        result = self.evaluate(payload)
        self.assertEqual(result.failure_subtype, "invalid_content_ref")
        self.assertGreater(
            result.diagnostics["invalid_content_ref_count"], 0,
        )

    def test_topic_focus_mismatch_subtype_uses_ranked_candidate_facts(self):
        candidates = normalize_candidates(observation([
            raw("https://example.test/other", "Other", tags=["unrelated"]),
        ]), EVIDENCE)
        ranked = rank_candidates(candidates, self.subscription, NOW)
        payload = self.payload(selected=ranked)
        result = self.evaluate(payload, selected=ranked)
        self.assertEqual(result.failure_subtype, "topic_focus_mismatch")
        self.assertEqual(
            result.diagnostics["topic_focus_mismatch_count"], 1,
        )

    def test_missing_field_and_invalid_marker_have_distinct_subtypes(self):
        missing = self.payload()
        missing["rendered_text"] = ""
        missing["character_count"] = 0
        marker = self.payload()
        marker["rendered_text"] = marker["rendered_text"].replace("[S1]", "")
        marker["character_count"] = len(marker["rendered_text"])
        self.assertEqual(
            self.evaluate(missing).failure_subtype,
            "missing_required_field",
        )
        self.assertEqual(
            self.evaluate(marker).failure_subtype, "invalid_marker",
        )

    def test_model_cannot_change_deterministic_ranking(self):
        payload = self.payload()
        payload["items"][0]["score"] += 1
        result = self.evaluate(payload)
        self.assertFalse(result.satisfied)
        self.assertIn("score_mismatch", result.violations)
        self.assertEqual(result.failure_subtype, "other_contract_failure")

    def test_model_cannot_change_profile_snapshot(self):
        payload = self.payload()
        payload["profile_snapshot"]["profile_version"] += 1
        result = self.evaluate(payload)
        self.assertFalse(result.satisfied)
        self.assertIn("profile_snapshot_mismatch", result.violations)


if __name__ == "__main__":
    unittest.main()
