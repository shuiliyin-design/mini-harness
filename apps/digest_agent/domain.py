"""Application-owned entities and deterministic candidate rules."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
LANGUAGES = frozenset({"zh-CN", "en"})
DELIVERY_CHANNELS = frozenset({"none", "termux_notification"})
DELIVERY_REQUEST_CHANNELS = frozenset({"fake", "termux_notification"})
DELIVERY_STATUSES = frozenset({"pending", "accepted", "failed", "unknown"})
DELIVERY_CERTAINTIES = frozenset({"not_started", "known_applied", "unknown"})
TRACKING_PARAMETERS = frozenset({
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source",
    "utm_campaign", "utm_content", "utm_medium", "utm_source", "utm_term",
})
FEEDBACK_DELTAS = {
    "opened": 1,
    "liked": 3,
    "dismissed": -3,
    "saved": 4,
}
PROFILE_WEIGHT_MIN = -20
PROFILE_WEIGHT_MAX = 20
PROFILE_RULE_VERSION = 1
SCORE_COMPONENTS = (
    "subscription_topic", "focus_topics", "profile_weight", "freshness",
    "already_seen_penalty",
)


class DomainError(ValueError):
    """An application domain value violated its explicit schema."""


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _strict_int(value, name, minimum, maximum):
    if (not isinstance(value, int) or isinstance(value, bool)
            or not minimum <= value <= maximum):
        raise DomainError(f"{name} 必须是 {minimum}..{maximum} 的整数")
    return value


def _text(value, name, minimum, maximum):
    if not isinstance(value, str):
        raise DomainError(f"{name} 必须是字符串")
    value = value.strip()
    if not minimum <= len(value) <= maximum:
        raise DomainError(f"{name} 长度必须是 {minimum}..{maximum}")
    return value


def normalize_topic(value):
    return _text(value, "topic", 1, 60).casefold()


def _canonical_identity(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class Subscription:
    subscription_id: str
    user_id: str
    topic: str
    natural_language_request: str
    cadence: str
    language: str
    max_chars: int
    max_items: int
    focus_topics: tuple[str, ...]
    delivery_channel: str
    enabled: bool
    version: int
    created_at: str
    updated_at: str
    schema_version: int = 1

    def __post_init__(self):
        if self.schema_version != 1:
            raise DomainError("unsupported Subscription schema")
        for name in ("subscription_id", "user_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"{name} 必须是 32 位小写 hex")
        object.__setattr__(self, "topic", _text(self.topic, "topic", 1, 120))
        object.__setattr__(
            self, "natural_language_request",
            _text(self.natural_language_request, "natural_language_request", 1, 2000),
        )
        if self.cadence != "daily":
            raise DomainError("V1 cadence 只支持 daily")
        if self.language not in LANGUAGES:
            raise DomainError("language 不在 allowlist")
        _strict_int(self.max_chars, "max_chars", 100, 4000)
        _strict_int(self.max_items, "max_items", 1, 10)
        if not isinstance(self.focus_topics, tuple) or len(self.focus_topics) > 10:
            raise DomainError("focus_topics 必须是最多 10 项的 tuple")
        normalized = tuple(
            _text(item, "focus_topic", 1, 60) for item in self.focus_topics
        )
        if len({item.casefold() for item in normalized}) != len(normalized):
            raise DomainError("focus_topics 不允许重复")
        object.__setattr__(self, "focus_topics", normalized)
        if self.delivery_channel not in DELIVERY_CHANNELS:
            raise DomainError("delivery_channel 不在 allowlist")
        if not isinstance(self.enabled, bool):
            raise DomainError("enabled 必须是 boolean")
        _strict_int(self.version, "version", 1, 2**31 - 1)
        _text(self.created_at, "created_at", 1, 80)
        _text(self.updated_at, "updated_at", 1, 80)


@dataclass(frozen=True, slots=True)
class SearchObservation:
    observation_id: str
    query: str
    observed_at: str
    results: tuple[dict, ...]

    def __post_init__(self):
        if not ID_PATTERN.fullmatch(str(self.observation_id)):
            raise DomainError("observation_id 无效")
        _text(self.query, "query", 1, 300)
        _text(self.observed_at, "observed_at", 1, 80)
        if not isinstance(self.results, tuple):
            raise DomainError("Search Observation results 必须是 tuple")


@dataclass(frozen=True, slots=True)
class ContentCandidate:
    candidate_id: str
    canonical_url: str
    title: str
    snippet: str
    published_at: str
    retrieved_at: str
    source_domain: str
    topic_tags: tuple[str, ...]
    content_identity: str
    evidence_id: str

    def __post_init__(self):
        for name in ("candidate_id", "evidence_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"{name} 无效")
        if not re.fullmatch(r"[0-9a-f]{64}", str(self.content_identity)):
            raise DomainError("content_identity 无效")


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    candidate: ContentCandidate
    score: int
    score_breakdown: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class TopicWeight:
    topic_key: str
    weight: int

    def __post_init__(self):
        object.__setattr__(self, "topic_key", normalize_topic(self.topic_key))
        _strict_int(
            self.weight, "weight", PROFILE_WEIGHT_MIN, PROFILE_WEIGHT_MAX,
        )


@dataclass(frozen=True, slots=True)
class InterestProfile:
    user_id: str
    version: int
    rule_version: int
    topic_weights: tuple[TopicWeight, ...]
    updated_at: str

    def __post_init__(self):
        if not ID_PATTERN.fullmatch(str(self.user_id)):
            raise DomainError("profile user_id 无效")
        _strict_int(self.version, "profile version", 0, 2**31 - 1)
        if self.rule_version != PROFILE_RULE_VERSION:
            raise DomainError("unsupported profile rule version")
        if not isinstance(self.topic_weights, tuple):
            raise DomainError("topic_weights 必须是 tuple")
        keys = [item.topic_key for item in self.topic_weights]
        if len(keys) != len(set(keys)):
            raise DomainError("topic_weights 不允许重复 topic")
        object.__setattr__(
            self, "topic_weights",
            tuple(sorted(self.topic_weights, key=lambda item: item.topic_key)),
        )
        _text(self.updated_at, "profile updated_at", 1, 80)

    @classmethod
    def empty(cls, user_id, updated_at):
        return cls(user_id, 0, PROFILE_RULE_VERSION, (), updated_at)


@dataclass(frozen=True, slots=True)
class ProfileProjection:
    profile_version: int
    profile_rule_version: int
    topic_weights: tuple[TopicWeight, ...]
    projection_id: str

    def __post_init__(self):
        _strict_int(self.profile_version, "profile_version", 0, 2**31 - 1)
        if self.profile_rule_version != PROFILE_RULE_VERSION:
            raise DomainError("unsupported profile projection rule")
        if not re.fullmatch(r"[0-9a-f]{64}", str(self.projection_id)):
            raise DomainError("profile projection_id 无效")

    def as_dict(self):
        return {
            "profile_version": self.profile_version,
            "profile_rule_version": self.profile_rule_version,
            "topic_weights": [
                {"topic_key": item.topic_key, "weight": item.weight}
                for item in self.topic_weights
            ],
            "projection_id": self.projection_id,
        }


def project_profile(profile, subscription, limit=10):
    """Return a safe, identity-bound view with no user or interaction history."""
    if not isinstance(profile, InterestProfile):
        raise DomainError("invalid InterestProfile")
    if not isinstance(subscription, Subscription):
        raise DomainError("invalid Subscription")
    _strict_int(limit, "projection limit", 1, 10)
    relevant = {normalize_topic(subscription.topic)}
    relevant.update(normalize_topic(item) for item in subscription.focus_topics)
    selected = [
        item for item in profile.topic_weights if item.topic_key in relevant
    ]
    selected.sort(key=lambda item: (-abs(item.weight), item.topic_key))
    selected = tuple(selected[:limit])
    public = {
        "profile_version": profile.version,
        "profile_rule_version": profile.rule_version,
        "topic_weights": [
            {"topic_key": item.topic_key, "weight": item.weight}
            for item in selected
        ],
    }
    return ProfileProjection(
        profile.version, profile.rule_version, selected,
        _canonical_identity(public),
    )


@dataclass(frozen=True, slots=True)
class Feedback:
    user_id: str
    digest_id: str
    item_id: str | None
    feedback_type: str
    event_key: str

    def __post_init__(self):
        for name in ("user_id", "digest_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"feedback {name} 无效")
        if self.item_id is not None and not ID_PATTERN.fullmatch(str(self.item_id)):
            raise DomainError("feedback item_id 无效")
        if self.feedback_type not in FEEDBACK_DELTAS:
            raise DomainError("feedback_type 不在 allowlist")
        if self.feedback_type != "opened" and self.item_id is None:
            raise DomainError("liked/dismissed/saved 必须绑定 item")
        _text(self.event_key, "feedback event_key", 1, 120)

    @property
    def feedback_id(self):
        return _canonical_identity({
            "user_id": self.user_id,
            "digest_id": self.digest_id,
            "item_id": self.item_id,
            "feedback_type": self.feedback_type,
            "event_key": self.event_key,
        })[:32]


@dataclass(frozen=True, slots=True)
class Interaction:
    feedback_id: str
    user_id: str
    digest_id: str
    item_id: str | None
    feedback_type: str
    event_key: str
    topic_keys: tuple[str, ...]
    delta: int
    created_at: str


@dataclass(frozen=True, slots=True)
class ProfileUpdate:
    feedback_id: str
    before_version: int
    after_version: int
    changes: tuple[tuple[str, int, int], ...]


@dataclass(frozen=True, slots=True)
class FeedbackResult:
    feedback_id: str
    applied: bool
    profile: InterestProfile
    update: ProfileUpdate | None


@dataclass(frozen=True, slots=True)
class DeliveryRequest:
    delivery_id: str
    attempt_id: str
    digest_id: str
    channel: str
    title: str
    content: str

    def __post_init__(self):
        for name in ("delivery_id", "attempt_id", "digest_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"delivery request {name} 无效")
        if self.channel not in DELIVERY_REQUEST_CHANNELS:
            raise DomainError("delivery channel 不在 allowlist")
        _text(self.title, "delivery title", 1, 100)
        _text(self.content, "delivery content", 1, 500)


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    status: str
    effect_certainty: str
    provider_message_id: str | None = None
    error_code: str | None = None

    def __post_init__(self):
        if self.status not in {"accepted", "failed", "unknown"}:
            raise DomainError("delivery outcome status 无效")
        if self.effect_certainty not in DELIVERY_CERTAINTIES:
            raise DomainError("delivery outcome certainty 无效")
        valid = (
            (self.status == "accepted"
             and self.effect_certainty == "known_applied"
             and self.error_code is None)
            or (self.status == "failed"
                and self.effect_certainty == "not_started"
                and self.error_code is not None)
            or (self.status == "unknown"
                and self.effect_certainty == "unknown"
                and self.error_code is not None)
        )
        if not valid:
            raise DomainError("delivery outcome status/certainty 不一致")
        if (self.provider_message_id is not None
                and not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}",
                    str(self.provider_message_id),
                )):
            raise DomainError("provider_message_id 不是 safe external ref")
        if (self.error_code is not None
                and not re.fullmatch(r"[A-Z0-9_:-]{1,80}", str(self.error_code))):
            raise DomainError("delivery error_code 无效")


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    delivery_id: str
    attempt_id: str
    digest_id: str
    user_id: str
    channel: str
    status: str
    attempt_number: int
    provider_message_id: str | None
    requested_at: str
    completed_at: str | None
    error_code: str | None
    effect_certainty: str

    def __post_init__(self):
        for name in ("delivery_id", "attempt_id", "digest_id", "user_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"delivery {name} 无效")
        if self.channel not in DELIVERY_REQUEST_CHANNELS:
            raise DomainError("delivery channel 不在 allowlist")
        if self.status not in DELIVERY_STATUSES:
            raise DomainError("delivery status 无效")
        _strict_int(self.attempt_number, "attempt_number", 1, 2**31 - 1)
        _text(self.requested_at, "requested_at", 1, 80)
        if self.completed_at is not None:
            _text(self.completed_at, "completed_at", 1, 80)
        if (self.provider_message_id is not None
                and not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}",
                    str(self.provider_message_id),
                )):
            raise DomainError("provider_message_id 不是 safe external ref")
        if (self.error_code is not None
                and not re.fullmatch(r"[A-Z0-9_:-]{1,80}", str(self.error_code))):
            raise DomainError("delivery error_code 无效")
        if self.effect_certainty not in DELIVERY_CERTAINTIES:
            raise DomainError("delivery effect_certainty 无效")
        valid = (
            (self.status == "pending" and self.effect_certainty == "not_started"
             and self.completed_at is None)
            or (self.status == "accepted"
                and self.effect_certainty == "known_applied"
                and self.completed_at is not None and self.error_code is None)
            or (self.status == "failed"
                and self.effect_certainty == "not_started"
                and self.completed_at is not None and self.error_code is not None)
            or (self.status == "unknown"
                and self.effect_certainty == "unknown")
        )
        if not valid:
            raise DomainError("delivery record status/certainty 不一致")


def delivery_identity(digest_id, channel):
    if not ID_PATTERN.fullmatch(str(digest_id)):
        raise DomainError("delivery digest_id 无效")
    if channel not in DELIVERY_REQUEST_CHANNELS:
        raise DomainError("delivery channel 不在 allowlist")
    return _canonical_identity({"digest_id": digest_id, "channel": channel})[:32]


def delivery_attempt_identity(delivery_id, attempt_number):
    if not ID_PATTERN.fullmatch(str(delivery_id)):
        raise DomainError("delivery_id 无效")
    _strict_int(attempt_number, "attempt_number", 1, 2**31 - 1)
    return _canonical_identity({
        "delivery_id": delivery_id, "attempt_number": attempt_number,
    })[:32]


def safe_digest_preview(digest, maximum=160):
    """Create a short whitespace-safe hint; Digest remains canonical storage."""
    _strict_int(maximum, "delivery preview maximum", 80, 500)
    if not isinstance(digest, Digest):
        raise DomainError("invalid Digest")
    text = " ".join(str(digest.payload.get("rendered_text", "")).split())
    suffix = f" · Digest {digest.digest_id}"
    budget = max(1, maximum - len(suffix))
    return f"{text[:budget].rstrip()}{suffix}"


@dataclass(frozen=True, slots=True)
class Digest:
    digest_id: str
    digest_run_id: str
    harness_run_id: str
    artifact_id: str
    subscription_id: str
    payload: dict
    created_at: str


@dataclass(frozen=True, slots=True)
class ApplicationResult:
    digest_run_id: str
    harness_run_id: str
    status: str
    reason: str | None
    digest_id: str | None
    artifact_id: str | None
    harness_result: dict
    reused: bool = False


def canonicalize_url(value):
    value = _text(value, "url", 1, 2048)
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise DomainError("candidate URL 必须是 http/https absolute URL")
    host = parts.hostname.casefold()
    port = f":{parts.port}" if parts.port else ""
    netloc = host + port
    path = parts.path or "/"
    query = urlencode(sorted(
        (key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if key.casefold() not in TRACKING_PARAMETERS
    ))
    return urlunsplit((parts.scheme.casefold(), netloc, path, query, ""))


def normalize_candidates(observation, evidence_id):
    """Normalize and exact-deduplicate one accepted Search Observation."""
    if not isinstance(observation, SearchObservation):
        raise DomainError("invalid Search Observation")
    if not ID_PATTERN.fullmatch(str(evidence_id)):
        raise DomainError("accepted evidence_id 无效")
    candidates = []
    for raw in observation.results:
        if not isinstance(raw, dict):
            continue
        try:
            url = canonicalize_url(raw.get("url"))
            title = _text(raw.get("title"), "title", 1, 300)
            snippet = _text(raw.get("snippet", ""), "snippet", 1, 2000)
            published_at = _text(
                raw.get("published_at", observation.observed_at),
                "published_at", 1, 80,
            )
            tags = tuple(dict.fromkeys(
                normalize_topic(item) for item in raw.get("topic_tags", ())
            ))
        except (DomainError, TypeError):
            continue
        stable = f"{url}\n{title.casefold()}".encode("utf-8")
        identity = hashlib.sha256(stable).hexdigest()
        candidates.append(ContentCandidate(
            candidate_id=identity[:32], canonical_url=url, title=title,
            snippet=snippet, published_at=published_at,
            retrieved_at=observation.observed_at,
            source_domain=urlsplit(url).hostname or "", topic_tags=tags,
            content_identity=identity, evidence_id=evidence_id,
        ))
    winners = []
    seen_urls, seen_titles = set(), set()
    for candidate in sorted(
        candidates,
        key=lambda item: (item.published_at, item.candidate_id),
        reverse=True,
    ):
        title_key = " ".join(candidate.title.casefold().split())
        if candidate.canonical_url in seen_urls or title_key in seen_titles:
            continue
        seen_urls.add(candidate.canonical_url)
        seen_titles.add(title_key)
        winners.append(candidate)
    return tuple(sorted(winners, key=lambda item: item.candidate_id))


def _parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def rank_candidates(candidates, subscription, now, profile_projection=None,
                    seen_content_identities=()):
    """Apply the V1 integer score and stable tie-breaks."""
    if not isinstance(subscription, Subscription):
        raise DomainError("invalid Subscription")
    now_value = _parse_time(now)
    topic = normalize_topic(subscription.topic)
    focus = {normalize_topic(item) for item in subscription.focus_topics}
    if profile_projection is None:
        empty = InterestProfile.empty(subscription.user_id, now)
        profile_projection = project_profile(empty, subscription)
    if not isinstance(profile_projection, ProfileProjection):
        raise DomainError("invalid ProfileProjection")
    weights = {
        item.topic_key: item.weight for item in profile_projection.topic_weights
    }
    seen = frozenset(seen_content_identities)
    ranked = []
    for candidate in candidates:
        tags = set(candidate.topic_tags)
        topic_score = 40 if topic in tags else 0
        focus_score = min(30, 15 * len(tags & focus))
        profile_points = max(
            PROFILE_WEIGHT_MIN,
            min(PROFILE_WEIGHT_MAX, sum(weights.get(tag, 0) for tag in tags)),
        )
        profile_score = profile_points * 2
        age = max(0, (now_value - _parse_time(candidate.published_at)).total_seconds())
        if age > 604800:
            continue
        freshness = 20 if age <= 86400 else 10 if age <= 259200 else 5 if age <= 604800 else 0
        seen_penalty = -100 if candidate.content_identity in seen else 0
        breakdown = (
            ("subscription_topic", topic_score),
            ("focus_topics", focus_score),
            ("profile_weight", profile_score),
            ("freshness", freshness),
            ("already_seen_penalty", seen_penalty),
        )
        ranked.append(RankedCandidate(
            candidate, sum(value for _name, value in breakdown), breakdown,
        ))
    ranked.sort(key=lambda item: (
        -item.score,
        -_parse_time(item.candidate.published_at).timestamp(),
        item.candidate.candidate_id,
    ))
    return tuple(ranked[:subscription.max_items])
