"""Pure Subscription/Digest contract evaluation."""

from dataclasses import dataclass
import re

from .domain import (
    ID_PATTERN, SCORE_COMPONENTS, InterestProfile, ProfileProjection,
    RankedCandidate, Subscription, normalize_topic, project_profile,
)


DIGEST_FIELDS = frozenset({
    "schema_version", "digest_id", "subscription_id", "subscription_version",
    "period_key", "language", "rendered_text", "character_count", "items",
    "source_refs", "profile_snapshot",
})
ITEM_FIELDS = frozenset({
    "item_id", "candidate_id", "content_identity", "topic_tags", "rank",
    "score", "score_breakdown", "recommendation_reason", "text",
    "source_ref_ids",
})
SOURCE_FIELDS = frozenset({
    "source_ref_id", "candidate_id", "canonical_url", "evidence_id",
})


@dataclass(frozen=True, slots=True)
class DigestContractResult:
    satisfied: bool
    violations: tuple[str, ...]
    character_count: int
    item_count: int


def _unique(values):
    return len(values) == len(set(values))


def evaluate_digest_contract(payload, subscription, selected,
                             accepted_evidence_ids, profile_projection=None):
    """Evaluate only deterministic facts; never trust Model self-report."""
    violations = []
    if not isinstance(subscription, Subscription):
        raise TypeError("subscription must be Subscription")
    if profile_projection is None:
        profile_projection = project_profile(
            InterestProfile.empty(subscription.user_id, subscription.updated_at),
            subscription,
        )
    if not isinstance(profile_projection, ProfileProjection):
        raise TypeError("profile_projection must be ProfileProjection")
    if not isinstance(payload, dict) or set(payload) != DIGEST_FIELDS:
        return DigestContractResult(False, ("invalid_schema",), 0, 0)
    rendered = payload.get("rendered_text")
    items = payload.get("items")
    refs = payload.get("source_refs")
    character_count = len(rendered) if isinstance(rendered, str) else 0
    item_count = len(items) if isinstance(items, list) else 0
    if payload.get("schema_version") != 1:
        violations.append("invalid_schema_version")
    if not ID_PATTERN.fullmatch(str(payload.get("digest_id", ""))):
        violations.append("invalid_digest_id")
    if payload.get("subscription_id") != subscription.subscription_id:
        violations.append("subscription_mismatch")
    if payload.get("subscription_version") != subscription.version:
        violations.append("subscription_version_mismatch")
    if payload.get("language") != subscription.language:
        violations.append("language_mismatch")
    if payload.get("profile_snapshot") != profile_projection.as_dict():
        violations.append("profile_snapshot_mismatch")
    if not isinstance(payload.get("period_key"), str) or not payload["period_key"]:
        violations.append("invalid_period_key")
    if not isinstance(rendered, str) or not rendered:
        violations.append("rendered_text_required")
    if payload.get("character_count") != character_count:
        violations.append("character_count_mismatch")
    if character_count > subscription.max_chars:
        violations.append("max_chars_exceeded")
    if not isinstance(items, list) or not items:
        violations.append("items_required")
        items = []
    if item_count > subscription.max_items:
        violations.append("max_items_exceeded")
    if not isinstance(refs, list) or not refs:
        violations.append("source_refs_required")
        refs = []
    selected_by_id = {
        item.candidate.candidate_id: item for item in selected
        if isinstance(item, RankedCandidate)
    }
    required_topics = {normalize_topic(subscription.topic)}
    required_topics.update(
        normalize_topic(item) for item in subscription.focus_topics
    )
    item_ids, candidate_ids = [], []
    for position, item in enumerate(items, 1):
        if not isinstance(item, dict) or set(item) != ITEM_FIELDS:
            violations.append("invalid_item_schema")
            continue
        item_ids.append(item.get("item_id"))
        candidate_ids.append(item.get("candidate_id"))
        ranked = selected_by_id.get(item.get("candidate_id"))
        if ranked is None:
            violations.append("unselected_candidate")
        else:
            candidate = ranked.candidate
            if not (set(candidate.topic_tags) & required_topics):
                violations.append("topic_focus_mismatch")
            if item.get("content_identity") != candidate.content_identity:
                violations.append("content_identity_mismatch")
            if item.get("topic_tags") != list(candidate.topic_tags):
                violations.append("topic_tags_mismatch")
            if item.get("rank") != position:
                violations.append("rank_mismatch")
            if item.get("score") != ranked.score:
                violations.append("score_mismatch")
            expected_breakdown = [
                {"component": name, "value": value}
                for name, value in ranked.score_breakdown
            ]
            if item.get("score_breakdown") != expected_breakdown:
                violations.append("score_breakdown_mismatch")
            if tuple(name for name, _value in ranked.score_breakdown) != SCORE_COMPONENTS:
                violations.append("score_components_mismatch")
        if not isinstance(item.get("recommendation_reason"), str):
            violations.append("recommendation_reason_invalid")
        if (not isinstance(item.get("source_ref_ids"), list)
                or not item["source_ref_ids"]):
            violations.append("item_source_required")
        if not isinstance(item.get("text"), str) or not item["text"]:
            violations.append("item_text_required")
    if not _unique(item_ids) or not _unique(candidate_ids):
        violations.append("duplicate_item")
    refs_by_id = {}
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != SOURCE_FIELDS:
            violations.append("invalid_source_schema")
            continue
        ref_id = ref.get("source_ref_id")
        if ref_id in refs_by_id:
            violations.append("duplicate_source_ref")
        refs_by_id[ref_id] = ref
        ranked = selected_by_id.get(ref.get("candidate_id"))
        if ranked is None:
            violations.append("invalid_source_candidate")
        else:
            candidate = ranked.candidate
            if ref.get("canonical_url") != candidate.canonical_url:
                violations.append("source_url_mismatch")
            if ref.get("evidence_id") != candidate.evidence_id:
                violations.append("source_evidence_mismatch")
        if ref.get("evidence_id") not in accepted_evidence_ids:
            violations.append("unaccepted_source_evidence")
    used_refs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for ref_id in item.get("source_ref_ids", []):
            used_refs.append(ref_id)
            ref = refs_by_id.get(ref_id)
            if ref is None:
                violations.append("missing_source_ref")
            elif ref.get("candidate_id") != item.get("candidate_id"):
                violations.append("item_source_candidate_mismatch")
    if set(refs_by_id) != set(used_refs):
        violations.append("orphan_source_ref")
    expected_order = [item.candidate.candidate_id for item in selected]
    if candidate_ids != expected_order[:len(candidate_ids)]:
        violations.append("selection_order_mismatch")
    markers = set(re.findall(r"\[S\d+\]", rendered or ""))
    expected_markers = {f"[{item}]" for item in refs_by_id}
    if markers != expected_markers:
        violations.append("source_marker_mismatch")
    return DigestContractResult(
        not violations, tuple(dict.fromkeys(violations)),
        character_count, item_count,
    )
