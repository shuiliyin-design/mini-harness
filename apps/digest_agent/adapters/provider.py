"""Offline model candidate adapter for deterministic Digest synthesis tests."""

import copy

from ..domain import InterestProfile, project_profile


class FakeDigestProvider:
    """Propose Digest payloads; it never validates or persists them."""

    MODES = frozenset({"valid", "overlong", "invalid_source"})

    def __init__(self, mode="valid"):
        if mode not in self.MODES:
            raise ValueError("unknown FakeDigestProvider mode")
        self.mode = mode
        self.calls = []

    def synthesize(self, subscription, selected, period_key, digest_id,
                   profile_projection=None):
        if profile_projection is None:
            profile_projection = project_profile(
                InterestProfile.empty(subscription.user_id, subscription.updated_at),
                subscription,
            )
        self.calls.append({
            "subscription_id": subscription.subscription_id,
            "candidate_ids": [item.candidate.candidate_id for item in selected],
            "profile_projection": copy.deepcopy(profile_projection.as_dict()),
        })
        items, refs, rendered = [], [], []
        for index, ranked in enumerate(selected, 1):
            candidate = ranked.candidate
            source_id = f"S{index}"
            text = f"{candidate.title}：{candidate.snippet} [{source_id}]"
            rendered.append(text)
            items.append({
                "item_id": candidate.content_identity[32:],
                "candidate_id": candidate.candidate_id,
                "content_identity": candidate.content_identity,
                "topic_tags": list(candidate.topic_tags),
                "rank": index,
                "score": ranked.score,
                "score_breakdown": [
                    {"component": name, "value": value}
                    for name, value in ranked.score_breakdown
                ],
                "recommendation_reason": (
                    "按订阅匹配、兴趣权重、新鲜度与已读状态确定性排序"
                ),
                "text": text,
                "source_ref_ids": [source_id],
            })
            refs.append({
                "source_ref_id": source_id,
                "candidate_id": candidate.candidate_id,
                "canonical_url": candidate.canonical_url,
                "evidence_id": candidate.evidence_id,
            })
        rendered_text = "\n".join(rendered)
        if self.mode == "overlong":
            rendered_text = "超" * (subscription.max_chars + 1) + rendered_text
        if self.mode == "invalid_source" and refs:
            refs[0]["candidate_id"] = "f" * 32
        return {
            "schema_version": 1, "digest_id": digest_id,
            "subscription_id": subscription.subscription_id,
            "subscription_version": subscription.version,
            "period_key": period_key, "language": subscription.language,
            "profile_snapshot": profile_projection.as_dict(),
            "rendered_text": rendered_text,
            "character_count": len(rendered_text),
            "items": copy.deepcopy(items), "source_refs": copy.deepcopy(refs),
        }


class FinalCandidateProvider:
    """One-shot Agent-loop Provider for authoritative Result binding."""

    def __init__(self, answer, artifact_refs=(), evidence_refs=()):
        self.answer = answer
        self.artifact_refs = list(artifact_refs)
        self.evidence_refs = list(evidence_refs)
        self.calls = 0

    def complete(self, _messages):
        self.calls += 1
        return {
            "type": "final_answer", "final_answer": self.answer,
            "claimed_status": "completed",
            "artifact_refs": self.artifact_refs,
            "evidence_refs": self.evidence_refs,
        }
