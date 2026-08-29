"""Application-owned entities and deterministic candidate rules."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import copy
import hashlib
import json
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo


ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
LANGUAGES = frozenset({"zh-CN", "en"})
DELIVERY_CHANNELS = frozenset({"none", "termux_notification"})
DELIVERY_REQUEST_CHANNELS = frozenset({"fake", "termux_notification"})
DELIVERY_STATUSES = frozenset({"pending", "accepted", "failed", "unknown"})
DELIVERY_CERTAINTIES = frozenset({"not_started", "known_applied", "unknown"})
CONDITION_NOTIFICATION_POLICIES = frozenset({
    "none", "feed_only", "termux_notification",
})
CONVERSATION_STATUSES = frozenset({
    "COLLECTING", "WAITING_FOR_ANSWER", "REJECTED",
    "DEFINITION_ACCEPTED", "INCOMPLETE",
})
CONVERSATION_TURN_STATUSES = frozenset({
    "reserved", "running", "completed", "failed", "blocked",
})
DEFINITION_OUTCOME_TYPES = frozenset({
    "NEXT_QUESTION", "REJECT", "DONE",
})
FIELD_PROVENANCE = frozenset({
    "USER_EXPLICIT", "USER_CONFIRMED", "PRODUCT_DEFAULT", "POLICY_DEFAULT",
})
PRODUCT_DEFINITION_DEFAULTS = {
    "language": "zh-CN", "max_chars": 600, "max_items": 5,
    "delivery_preference": "none",
}
POLICY_DEFINITION_DEFAULTS = {"cadence": "daily"}
INTERNAL_CLARIFICATION_PATTERNS = (
    re.compile(r"(?:多少|几|最多|上限).{0,8}(?:字|条|项|篇)(?:资讯|内容)?", re.I),
    re.compile(
        r"(?:字数|篇幅|条数|max_chars|max_items|schema|config|配置项|"
        r"字段名|语言设置|中文还是英文|英文还是中文|本[地机]通知|投递方式|"
        r"delivery_preference)",
        re.I,
    ),
)
PRODUCT_SUBSCRIPTION_STATUSES = frozenset({"ACTIVE", "DISABLED"})
USER_SUBSCRIPTION_STATUSES = frozenset({"ACTIVE", "DISABLED"})
TRACKING_WORKFLOW_KINDS = frozenset({"BRIEFING", "CONDITION", "EVENT"})
CONDITION_REQUEST_STATUSES = frozenset({"PENDING", "EVALUATED", "FAILED"})
CONDITION_RESULTS = frozenset({"NO_UPDATE", "MATCHED"})
FLIGHT_CADENCES = {"1h": 3_600, "6h": 21_600, "12h": 43_200,
                   "24h": 86_400}
DEFINITION_CADENCES = frozenset({"daily", *FLIGHT_CADENCES})
CONDITION_TEMPORAL_LIFECYCLES = frozenset({
    "ACTIVE", "PAUSED", "COMPLETED",
})
CONDITION_TRUTHS = frozenset({"UNKNOWN", "FALSE", "TRUE"})
CONDITION_CYCLE_KINDS = frozenset({
    "INITIAL", "SCHEDULED", "CATCH_UP", "RESUME", "MANUAL",
})
CONDITION_CYCLE_STATUSES = frozenset({
    "PENDING", "STARTED", "SUCCEEDED", "FAILED", "SUPERSEDED",
})
CONDITION_EMISSION_DECISIONS = frozenset({
    "EMIT_FIRST_MATCH", "EMIT_THRESHOLD_CROSSING", "SUPPRESS_FALSE",
    "SUPPRESS_STILL_MATCHED", "SUPPRESS_REARMED",
    "DUPLICATE_OBSERVATION",
})
CONDITION_CYCLE_FAILURES = frozenset({
    "INVALID_OBSERVATION", "STALE_OBSERVATION", "OUT_OF_ORDER_OBSERVATION",
    "OBSERVATION_CONFLICT", "PROVIDER_TIMEOUT", "PROVIDER_ERROR",
    "EVIDENCE_PERSIST_FAILED",
})
EVENT_TEMPORAL_LIFECYCLES = frozenset({"ACTIVE", "PAUSED"})
EVENT_CYCLE_KINDS = frozenset({
    "INITIAL", "SCHEDULED", "CATCH_UP", "RESUME",
})
EVENT_CYCLE_STATUSES = frozenset({
    "PENDING", "STARTED", "SUCCEEDED", "INCOMPLETE", "FAILED",
    "SUPERSEDED",
})
EVENT_VERIFICATION_OUTCOMES = frozenset({
    "VERIFIED", "NO_UPDATE", "VERIFICATION_INCOMPLETE",
})
EVENT_VERIFICATION_REASONS = frozenset({
    "VERIFIED_NEW_EVENT", "NO_EVENT_FOUND", "DUPLICATE_VERIFIED_EVENT",
    "OUTSIDE_SCOPE", "INSUFFICIENT_OFFICIAL_SUPPORT",
    "CONFLICTING_EVIDENCE", "SOURCE_TIME_UNCONFIRMED",
    "COVERAGE_INCOMPLETE", "RELEASE_SEMANTICS_UNCONFIRMED",
    "MODEL_NAME_UNCONFIRMED",
})
EVENT_CYCLE_FAILURES = frozenset({
    "INVALID_OBSERVATION", "PROVIDER_TIMEOUT", "PROVIDER_ERROR",
    "EVIDENCE_PERSIST_FAILED", "AGENT_CONTRACT_FAILED",
    "HARNESS_INCOMPLETE", "VERIFICATION_EVIDENCE_PERSIST_FAILED",
})
BRIEFING_RESERVATION_STATUSES = frozenset({"PENDING"})
APPLICATION_OUTBOX_STATUSES = frozenset({
    "pending", "claimed", "retry_wait", "completed", "failed", "blocked",
})
RELATION_EVENT_OUTBOX_STATUSES = frozenset({
    "pending", "claimed", "retry_wait", "completed", "failed", "blocked",
})
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
TOPIC_LEXICAL_STOP_WORDS = frozenset({
    "and", "current", "development", "developments", "for", "in",
    "latest", "new", "news", "of", "recent", "the", "update", "updates",
})


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


def _safe_protocol_text(value, name, maximum=500):
    value = _text(value, name, 1, maximum)
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise DomainError(f"{name} 包含 control character")
    return value


@dataclass(frozen=True, slots=True)
class DefinitionCandidate:
    """Validated application candidate; still not Subscription truth."""

    topic: str
    language: str
    cadence: str
    max_chars: int
    max_items: int
    focus_topics: tuple[str, ...]
    delivery_preference: str
    constraints: tuple[str, ...] = ()
    goal: str | None = None
    trigger: str | None = None
    time_window: str | None = None
    locations: tuple[str, ...] = ()
    provenance: dict | None = None
    schema_version: int = 1

    def __post_init__(self):
        if (type(self.schema_version) is not int
                or self.schema_version not in {1, 2}):
            raise DomainError("unsupported Definition schema")
        object.__setattr__(
            self, "topic", _safe_protocol_text(self.topic, "topic", 120),
        )
        if not isinstance(self.language, str) or self.language not in LANGUAGES:
            raise DomainError("language 不在 allowlist")
        if self.cadence not in DEFINITION_CADENCES:
            raise DomainError("cadence 不在 allowlist")
        _strict_int(self.max_chars, "max_chars", 100, 4000)
        _strict_int(self.max_items, "max_items", 1, 10)
        if not isinstance(self.focus_topics, tuple) or len(self.focus_topics) > 10:
            raise DomainError("focus_topics 必须是最多 10 项的 tuple")
        normalized = tuple(
            _safe_protocol_text(item, "focus_topic", 60)
            for item in self.focus_topics
        )
        if len({item.casefold() for item in normalized}) != len(normalized):
            raise DomainError("focus_topics 不允许重复")
        object.__setattr__(self, "focus_topics", normalized)
        if (not isinstance(self.delivery_preference, str)
                or self.delivery_preference not in DELIVERY_CHANNELS):
            raise DomainError("delivery_preference 不在 allowlist")
        constraints = self._intent_values(
            self.constraints, "constraint", 10, 200,
        )
        locations = self._intent_values(
            self.locations, "location", 10, 80,
        )
        object.__setattr__(self, "constraints", constraints)
        object.__setattr__(self, "locations", locations)
        for name in ("goal", "trigger", "time_window"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self, name, _safe_protocol_text(value, name, 300),
                )
        if self.schema_version == 1:
            if (constraints or locations or any(
                    getattr(self, name) is not None
                    for name in ("goal", "trigger", "time_window"))
                    or self.provenance is not None):
                raise DomainError("V1 Definition 不支持 intent metadata")
            return
        expected = {
            "topic", "constraints", "goal", "trigger", "time_window",
            "locations", "focus_topics", "language", "cadence",
            "max_chars", "max_items", "delivery_preference",
        }
        if (not isinstance(self.provenance, dict)
                or set(self.provenance) != expected
                or any(value not in FIELD_PROVENANCE
                       for value in self.provenance.values())):
            raise DomainError("Definition provenance 无效")
        object.__setattr__(self, "provenance", copy.deepcopy(self.provenance))

    @staticmethod
    def _intent_values(values, name, maximum_items, maximum_chars):
        if not isinstance(values, tuple) or len(values) > maximum_items:
            raise DomainError(f"{name} 必须是最多 {maximum_items} 项的 tuple")
        normalized = tuple(
            _safe_protocol_text(value, name, maximum_chars)
            for value in values
        )
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise DomainError(f"{name} 不允许重复")
        return normalized

    def as_dict(self):
        legacy = {
            "topic": self.topic,
            "language": self.language,
            "cadence": self.cadence,
            "max_chars": self.max_chars,
            "max_items": self.max_items,
            "focus_topics": list(self.focus_topics),
            "delivery_preference": self.delivery_preference,
        }
        if self.schema_version == 1:
            return legacy
        return {
            "topic": self.topic,
            "constraints": list(self.constraints),
            "goal": self.goal,
            "trigger": self.trigger,
            "time_window": self.time_window,
            "locations": list(self.locations),
            "focus_topics": list(self.focus_topics),
            "language": self.language,
            "cadence": self.cadence,
            "max_chars": self.max_chars,
            "max_items": self.max_items,
            "delivery_preference": self.delivery_preference,
            "provenance": copy.deepcopy(self.provenance),
        }


def normalize_definition_envelope(value):
    """Validate protocol shape without granting business acceptance."""
    if (not isinstance(value, dict)
            or type(value.get("protocol_version")) is not int
            or value.get("protocol_version") not in {1, 2}):
        raise DomainError("Definition Protocol version 无效")
    version = value["protocol_version"]
    outcome_type = value.get("type")
    if (not isinstance(outcome_type, str)
            or outcome_type not in DEFINITION_OUTCOME_TYPES):
        raise DomainError("Definition Protocol type 无效")
    if outcome_type == "NEXT_QUESTION":
        if set(value) != {"protocol_version", "type", "question"}:
            raise DomainError("NEXT_QUESTION schema 无效")
        return {
            "protocol_version": version, "type": outcome_type,
            "question": _safe_protocol_text(value["question"], "question"),
        }
    if outcome_type == "REJECT":
        if set(value) != {"protocol_version", "type", "reason"}:
            raise DomainError("REJECT schema 无效")
        return {
            "protocol_version": version, "type": outcome_type,
            "reason": _safe_protocol_text(value["reason"], "reason"),
        }
    if set(value) != {"protocol_version", "type", "definition"}:
        raise DomainError("DONE schema 无效")
    definition = value["definition"]
    allowed = ({
        "topic", "language", "cadence", "max_chars", "max_items",
        "focus_topics", "delivery_preference",
    } if version == 1 else {
        "topic", "constraints", "goal", "trigger", "time_window",
        "locations", "focus_topics", "language", "cadence", "max_chars",
        "max_items", "delivery_preference", "provenance",
    })
    if not isinstance(definition, dict) or set(definition) != allowed:
        raise DomainError("DONE definition schema 无效")
    return {
        "protocol_version": version, "type": outcome_type,
        "definition": copy.deepcopy(definition),
    }


def _sourced(value, name, *, optional=False):
    if optional and value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"value", "source_turn"}
            or type(value["source_turn"]) is not int
            or not 1 <= value["source_turn"] <= 10_000):
        raise DomainError(f"{name} source 无效")
    return copy.deepcopy(value)


def normalize_conversation_envelope(value):
    """Validate Agent conversation output, which is not a full Definition."""
    if not isinstance(value, dict):
        raise DomainError("Conversation candidate schema 无效")
    if value.get("protocol_version") == 1:
        return normalize_definition_envelope(value)
    if value.get("protocol_version") != 2:
        raise DomainError("Conversation candidate version 无效")
    outcome_type = value.get("type")
    if outcome_type in {"NEXT_QUESTION", "REJECT"}:
        normalized = normalize_definition_envelope(value)
        if outcome_type == "NEXT_QUESTION" and any(
                pattern.search(normalized["question"])
                for pattern in INTERNAL_CLARIFICATION_PATTERNS):
            raise DomainError("NEXT_QUESTION 暴露内部配置")
        return normalized
    if outcome_type != "DONE" or set(value) != {
            "protocol_version", "type", "intent"}:
        raise DomainError("Conversation DONE schema 无效")
    intent = value["intent"]
    fields = {
        "topic", "constraints", "goal", "trigger", "time_window",
        "locations", "focus_topics", "preferences",
    }
    if not isinstance(intent, dict) or set(intent) != fields:
        raise DomainError("Conversation intent schema 无效")
    normalized = {
        "topic": _sourced(intent["topic"], "topic"),
        "goal": _sourced(intent["goal"], "goal", optional=True),
        "trigger": _sourced(intent["trigger"], "trigger", optional=True),
        "time_window": _sourced(
            intent["time_window"], "time_window", optional=True,
        ),
    }
    for name in ("constraints", "locations", "focus_topics"):
        values = intent[name]
        if not isinstance(values, list) or len(values) > 10:
            raise DomainError(f"Conversation {name} schema 无效")
        normalized[name] = [_sourced(item, name) for item in values]
    preferences = intent["preferences"]
    allowed = {
        "language", "cadence", "max_chars", "max_items",
        "delivery_preference",
    }
    if not isinstance(preferences, dict) or not set(preferences) <= allowed:
        raise DomainError("Conversation preferences schema 无效")
    normalized["preferences"] = {
        name: _sourced(item, name) for name, item in preferences.items()
    }
    return {"protocol_version": 2, "type": "DONE", "intent": normalized}


def _provenance_for_sources(values):
    sources = [value["source_turn"] for value in values if value is not None]
    if not sources:
        return "PRODUCT_DEFAULT"
    return "USER_EXPLICIT" if 1 in sources else "USER_CONFIRMED"


def _preference_claim_supported(name, value, text):
    """Verify user-owned execution preferences without trusting the Model."""
    if name == "language":
        return ((value == "zh-CN" and "中文" in text)
                or (value == "en" and "英文" in text))
    if name == "cadence":
        patterns = {
            "daily": r"每天|每日", "24h": r"每天|每日|每\s*24\s*小时",
            "12h": r"每\s*12\s*小时", "6h": r"每\s*6\s*小时",
            "1h": r"每(?:隔)?\s*1?\s*小时|每小时",
        }
        return value in patterns and re.search(patterns[value], text) is not None
    if name == "max_chars":
        return type(value) is int and any(
            int(match.group(1)) == value
            for match in re.finditer(r"(\d+)\s*字", text)
        )
    if name == "max_items":
        return type(value) is int and any(
            int(match.group(1)) == value
            for match in re.finditer(r"(\d+)\s*(?:条|项|篇)", text)
        )
    if name == "delivery_preference":
        if value == "termux_notification":
            return "本机通知" in text
        return value == "none" and re.search(
            r"(?:不|无需|不用|暂不).{0,4}(?:通知|提醒)|产品内查看|站内",
            text,
        ) is not None
    return False


def materialize_conversation_definition(value, turn_count, user_messages=None):
    """Apply product/policy defaults after the Agent has understood intent."""
    normalized = normalize_conversation_envelope(value)
    if normalized["type"] != "DONE" or normalized["protocol_version"] == 1:
        return validate_definition_protocol(normalized)[0]
    if type(turn_count) is not int or not 1 <= turn_count <= 10_000:
        raise DomainError("Conversation turn_count 无效")
    intent = normalized["intent"]
    sourced = [
        intent["topic"], intent["goal"], intent["trigger"],
        intent["time_window"], *intent["constraints"],
        *intent["locations"], *intent["focus_topics"],
        *intent["preferences"].values(),
    ]
    if any(item is not None and item["source_turn"] > turn_count
           for item in sourced):
        raise DomainError("Conversation source_turn 超出历史")
    if user_messages is not None:
        if (not isinstance(user_messages, (tuple, list))
                or len(user_messages) != turn_count
                or not all(isinstance(item, str) and item.strip()
                           for item in user_messages)):
            raise DomainError("Conversation user history 无效")
        for name, item in intent["preferences"].items():
            text = user_messages[item["source_turn"] - 1]
            if not _preference_claim_supported(name, item["value"], text):
                raise DomainError(
                    f"{name} 没有 explicit user preference evidence",
                )

    def scalar(name):
        item = intent[name]
        return item["value"] if item is not None else None

    def values(name):
        return [item["value"] for item in intent[name]]

    preferences = intent["preferences"]
    is_flight = (
        intent["topic"]["value"] == "深圳往返武汉的机票优惠"
        and [item["value"] for item in intent["locations"]]
        == ["深圳", "武汉"]
    )
    is_event = (
        intent["topic"]["value"] == "OpenAI 新模型发布"
        and scalar("trigger") == "出现新模型时提醒"
    )
    settings = dict(POLICY_DEFINITION_DEFAULTS)
    if is_flight or is_event:
        settings["cadence"] = "6h"
    settings.update(PRODUCT_DEFINITION_DEFAULTS)
    explicit_settings = {
        name: ("24h" if name == "cadence" and item["value"] == "daily"
               and (is_flight or is_event) else item["value"])
        for name, item in preferences.items()
    }
    settings.update(explicit_settings)
    provenance = {
        "topic": _provenance_for_sources([intent["topic"]]),
        "constraints": _provenance_for_sources(intent["constraints"]),
        "goal": _provenance_for_sources([intent["goal"]]),
        "trigger": _provenance_for_sources([intent["trigger"]]),
        "time_window": _provenance_for_sources([intent["time_window"]]),
        "locations": _provenance_for_sources(intent["locations"]),
        "focus_topics": _provenance_for_sources(intent["focus_topics"]),
        "language": "PRODUCT_DEFAULT",
        "cadence": (
            "PRODUCT_DEFAULT" if is_flight or is_event else "POLICY_DEFAULT"
        ),
        "max_chars": "PRODUCT_DEFAULT",
        "max_items": "PRODUCT_DEFAULT",
        "delivery_preference": "PRODUCT_DEFAULT",
    }
    for name, item in preferences.items():
        provenance[name] = _provenance_for_sources([item])
    definition = {
        "topic": intent["topic"]["value"],
        "constraints": values("constraints"),
        "goal": scalar("goal"),
        "trigger": scalar("trigger"),
        "time_window": scalar("time_window"),
        "locations": values("locations"),
        "focus_topics": values("focus_topics"),
        **settings,
        "provenance": provenance,
    }
    return validate_definition_protocol({
        "protocol_version": 2, "type": "DONE", "definition": definition,
    })[0]


def validate_definition_protocol(value):
    """Turn a protocol envelope into a deterministic application candidate."""
    normalized = normalize_definition_envelope(value)
    if normalized["type"] != "DONE":
        return normalized, None
    raw = dict(normalized["definition"])
    version = normalized["protocol_version"]
    focus_topics = raw.get("focus_topics")
    if not isinstance(focus_topics, list):
        raise DomainError("focus_topics 必须是 array")
    raw["focus_topics"] = tuple(focus_topics)
    if version == 2:
        for name in ("constraints", "locations"):
            if not isinstance(raw.get(name), list):
                raise DomainError(f"{name} 必须是 array")
            raw[name] = tuple(raw[name])
        raw["schema_version"] = 2
    candidate = DefinitionCandidate(**raw)
    normalized["definition"] = candidate.as_dict()
    return normalized, candidate


@dataclass(frozen=True, slots=True)
class Conversation:
    conversation_id: str
    user_id: str
    status: str
    turn_count: int
    created_at: str
    updated_at: str
    version: int
    start_idempotency_key: str
    terminal_reason: str | None = None

    def __post_init__(self):
        for name in ("conversation_id", "user_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"{name} 无效")
        if self.status not in CONVERSATION_STATUSES:
            raise DomainError("Conversation status 无效")
        _strict_int(self.turn_count, "turn_count", 0, 10_000)
        _strict_int(self.version, "conversation version", 1, 2**31 - 1)
        _text(self.created_at, "created_at", 1, 80)
        _text(self.updated_at, "updated_at", 1, 80)
        _text(self.start_idempotency_key, "start idempotency key", 1, 120)
        if self.terminal_reason is not None:
            _text(self.terminal_reason, "terminal_reason", 1, 80)


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    turn_id: str
    conversation_id: str
    turn_number: int
    role: str
    safe_text: str
    message_idempotency_key: str
    harness_run_id: str
    status: str
    outcome_id: str | None
    error_code: str | None
    claim_owner_id: str | None
    created_at: str
    updated_at: str
    failure_stage: str | None = None
    failure_subtype: str | None = None

    def __post_init__(self):
        for name in ("turn_id", "conversation_id", "harness_run_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"{name} 无效")
        _strict_int(self.turn_number, "turn_number", 1, 10_000)
        if self.role != "user":
            raise DomainError("Conversation turn role 无效")
        _safe_protocol_text(self.safe_text, "safe_text", 2000)
        _text(self.message_idempotency_key, "message idempotency key", 1, 120)
        if self.status not in CONVERSATION_TURN_STATUSES:
            raise DomainError("Conversation turn status 无效")
        if self.outcome_id is not None and not ID_PATTERN.fullmatch(self.outcome_id):
            raise DomainError("outcome_id 无效")
        if self.error_code is not None:
            _text(self.error_code, "turn error_code", 1, 80)
        if self.claim_owner_id is not None and not ID_PATTERN.fullmatch(
                self.claim_owner_id):
            raise DomainError("claim_owner_id 无效")
        if self.failure_stage is not None and self.failure_stage not in {
                "definition_generation", "protocol_validation",
                "definition_validation", "recovery"}:
            raise DomainError("turn failure_stage 无效")
        if self.failure_subtype is not None:
            _text(self.failure_subtype, "turn failure_subtype", 1, 80)
        _text(self.created_at, "created_at", 1, 80)
        _text(self.updated_at, "updated_at", 1, 80)


@dataclass(frozen=True, slots=True)
class DefinitionOutcome:
    outcome_id: str
    conversation_id: str
    turn_id: str
    outcome_type: str
    payload: dict
    candidate_identity: str
    created_at: str

    def __post_init__(self):
        for name in ("outcome_id", "conversation_id", "turn_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"{name} 无效")
        if self.outcome_type not in DEFINITION_OUTCOME_TYPES:
            raise DomainError("Definition outcome type 无效")
        normalized, _candidate = validate_definition_protocol(self.payload)
        if normalized["type"] != self.outcome_type:
            raise DomainError("Definition outcome payload mismatch")
        object.__setattr__(self, "payload", copy.deepcopy(normalized))
        expected = _canonical_identity(normalized)
        if self.candidate_identity != expected:
            raise DomainError("Definition outcome identity mismatch")
        _text(self.created_at, "created_at", 1, 80)


def definition_candidate_identity(payload):
    normalized, _candidate = validate_definition_protocol(payload)
    return _canonical_identity(normalized)


def definition_outcome_identity(turn_id):
    if not ID_PATTERN.fullmatch(str(turn_id)):
        raise DomainError("turn_id 无效")
    return hashlib.sha256(f"definition-outcome\n{turn_id}".encode()).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class SubscriptionDefinition:
    definition_id: str
    definition_version: int
    conversation_id: str
    definition_outcome_id: str
    snapshot: dict
    snapshot_identity: str
    created_at: str

    def __post_init__(self):
        for name in ("definition_id", "conversation_id", "definition_outcome_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"{name} 无效")
        _strict_int(self.definition_version, "definition_version", 1, 2**31 - 1)
        version = (
            2 if isinstance(self.snapshot, dict)
            and "provenance" in self.snapshot else 1
        )
        normalized, candidate = validate_definition_protocol({
            "protocol_version": version, "type": "DONE",
            "definition": copy.deepcopy(self.snapshot),
        })
        if candidate is None:
            raise DomainError("Definition snapshot 无效")
        snapshot = normalized["definition"]
        object.__setattr__(self, "snapshot", copy.deepcopy(snapshot))
        if self.snapshot_identity != _canonical_identity(snapshot):
            raise DomainError("Definition snapshot identity mismatch")
        _text(self.created_at, "created_at", 1, 80)


@dataclass(frozen=True, slots=True)
class ProductSubscription:
    subscription_id: str
    definition_id: str
    definition_version: int
    status: str
    created_at: str
    updated_at: str
    workflow_kind: str = "BRIEFING"

    def __post_init__(self):
        for name in ("subscription_id", "definition_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"{name} 无效")
        _strict_int(self.definition_version, "definition_version", 1, 2**31 - 1)
        if self.status not in PRODUCT_SUBSCRIPTION_STATUSES:
            raise DomainError("Product Subscription status 无效")
        if self.workflow_kind not in TRACKING_WORKFLOW_KINDS:
            raise DomainError("Tracking workflow kind 无效")
        _text(self.created_at, "created_at", 1, 80)
        _text(self.updated_at, "updated_at", 1, 80)


@dataclass(frozen=True, slots=True)
class UserSubscription:
    user_subscription_id: str
    user_id: str
    subscription_id: str
    status: str
    created_at: str
    updated_at: str

    def __post_init__(self):
        for name in ("user_subscription_id", "user_id", "subscription_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"{name} 无效")
        if self.status not in USER_SUBSCRIPTION_STATUSES:
            raise DomainError("UserSubscription status 无效")
        _text(self.created_at, "created_at", 1, 80)
        _text(self.updated_at, "updated_at", 1, 80)


def user_subscription_relation_identity(relation, relation_version=1):
    if not isinstance(relation, UserSubscription):
        raise DomainError("invalid UserSubscription relation")
    _strict_int(relation_version, "relation_version", 1, 2**31 - 1)
    return _canonical_identity({
        "user_subscription_id": relation.user_subscription_id,
        "user_id": relation.user_id,
        "subscription_id": relation.subscription_id,
        "relation_version": relation_version,
        "status": relation.status,
    })


def relation_event_identity(user_subscription_id, relation_version=1):
    if not ID_PATTERN.fullmatch(str(user_subscription_id)):
        raise DomainError("user_subscription_id 无效")
    _strict_int(relation_version, "relation_version", 1, 2**31 - 1)
    return _canonical_identity({
        "event_type": "USER_SUBSCRIPTION_CREATED",
        "user_subscription_id": user_subscription_id,
        "relation_version": relation_version,
    })[:32]


def relation_event_attempt_identity(event_id, attempt_number):
    if not ID_PATTERN.fullmatch(str(event_id)):
        raise DomainError("relation event_id 无效")
    _strict_int(attempt_number, "publication attempt_number", 1, 2**31 - 1)
    return _canonical_identity({
        "event_id": event_id, "attempt_number": attempt_number,
    })[:32]


@dataclass(frozen=True, slots=True)
class RelationEventOutbox:
    event_id: str
    event_type: str
    user_subscription_id: str
    user_id: str
    subscription_id: str
    relation_version: int
    relation_identity: str
    payload: dict
    payload_identity: str
    status: str
    attempt_number: int
    created_at: str
    available_at: str
    last_error_code: str | None
    version: int
    updated_at: str

    def __post_init__(self):
        for name in (
                "event_id", "user_subscription_id", "user_id",
                "subscription_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"relation event {name} 无效")
        if self.event_type != "USER_SUBSCRIPTION_CREATED":
            raise DomainError("relation event type 无效")
        _strict_int(self.relation_version, "relation_version", 1, 2**31 - 1)
        if not re.fullmatch(r"[0-9a-f]{64}", str(self.relation_identity)):
            raise DomainError("relation identity 无效")
        if self.status not in RELATION_EVENT_OUTBOX_STATUSES:
            raise DomainError("relation event status 无效")
        _strict_int(self.attempt_number, "attempt_number", 0, 1_000_000)
        _strict_int(self.version, "relation event version", 1, 2**31 - 1)
        expected = {
            "event_id": self.event_id, "event_type": self.event_type,
            "user_subscription_id": self.user_subscription_id,
            "user_id": self.user_id,
            "subscription_id": self.subscription_id,
            "relation_version": self.relation_version,
            "relation_identity": self.relation_identity,
            "created_at": self.created_at,
        }
        if self.payload != expected:
            raise DomainError("relation event payload 无效")
        object.__setattr__(self, "payload", copy.deepcopy(expected))
        if self.payload_identity != _canonical_identity(expected):
            raise DomainError("relation event payload identity mismatch")
        if self.last_error_code is not None:
            _text(self.last_error_code, "relation event error", 1, 80)
        for name in ("created_at", "available_at", "updated_at"):
            _text(getattr(self, name), name, 1, 80)


@dataclass(frozen=True, slots=True)
class RelationEventAttempt:
    attempt_id: str
    event_id: str
    attempt_number: int
    status: str
    effect_certainty: str
    requested_at: str
    completed_at: str | None
    error_code: str | None

    def __post_init__(self):
        for name in ("attempt_id", "event_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"relation publication {name} 无效")
        _strict_int(self.attempt_number, "attempt_number", 1, 2**31 - 1)
        if self.status not in {"prepared", "unknown", "accepted", "failed"}:
            raise DomainError("relation publication attempt status 无效")
        if self.effect_certainty not in {
                "not_started", "unknown", "known_applied"}:
            raise DomainError("relation publication certainty 无效")
        valid = (
            self.status == "prepared"
            and self.effect_certainty == "not_started"
            and self.completed_at is None and self.error_code is None
        ) or (
            self.status == "unknown"
            and self.effect_certainty == "unknown"
        ) or (
            self.status == "accepted"
            and self.effect_certainty == "known_applied"
            and self.completed_at is not None and self.error_code is None
        ) or (
            self.status == "failed"
            and self.effect_certainty == "not_started"
            and self.completed_at is not None and self.error_code is not None
        )
        if not valid:
            raise DomainError("relation publication attempt state 无效")
        _text(self.requested_at, "requested_at", 1, 80)
        if self.completed_at is not None:
            _text(self.completed_at, "completed_at", 1, 80)
        if self.error_code is not None:
            _text(self.error_code, "publication error_code", 1, 80)


@dataclass(frozen=True, slots=True)
class BriefingReservation:
    application_run_id: str
    subscription_id: str
    definition_id: str
    definition_version: int
    status: str
    harness_run_id: str | None
    created_at: str
    updated_at: str

    def __post_init__(self):
        for name in ("application_run_id", "subscription_id", "definition_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"{name} 无效")
        _strict_int(self.definition_version, "definition_version", 1, 2**31 - 1)
        if self.status not in BRIEFING_RESERVATION_STATUSES:
            raise DomainError("Briefing reservation status 无效")
        if (self.harness_run_id is not None
                and not ID_PATTERN.fullmatch(str(self.harness_run_id))):
            raise DomainError("harness_run_id 无效")
        _text(self.created_at, "created_at", 1, 80)
        _text(self.updated_at, "updated_at", 1, 80)


@dataclass(frozen=True, slots=True)
class ApplicationOutbox:
    outbox_id: str
    event_type: str
    subscription_id: str
    application_run_id: str
    payload_refs: dict
    payload_identity: str
    status: str
    attempt_number: int
    created_at: str
    available_at: str
    last_error_code: str | None
    version: int
    updated_at: str

    def __post_init__(self):
        for name in ("outbox_id", "subscription_id", "application_run_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"{name} 无效")
        if self.event_type != "FIRST_BRIEFING_REQUESTED":
            raise DomainError("Outbox event type 无效")
        if self.status not in APPLICATION_OUTBOX_STATUSES:
            raise DomainError("Outbox status 无效")
        _strict_int(self.attempt_number, "attempt_number", 0, 1_000_000)
        _strict_int(self.version, "outbox version", 1, 2**31 - 1)
        expected_fields = {
            "activation_id", "definition_id", "definition_version",
            "application_run_id",
        }
        if not isinstance(self.payload_refs, dict) or set(self.payload_refs) != expected_fields:
            raise DomainError("Outbox payload refs 无效")
        for name in ("activation_id", "definition_id", "application_run_id"):
            if not ID_PATTERN.fullmatch(str(self.payload_refs[name])):
                raise DomainError(f"Outbox {name} ref 无效")
        _strict_int(
            self.payload_refs["definition_version"],
            "Outbox definition_version", 1, 2**31 - 1,
        )
        refs = copy.deepcopy(self.payload_refs)
        object.__setattr__(self, "payload_refs", refs)
        if self.payload_identity != _canonical_identity(refs):
            raise DomainError("Outbox payload identity mismatch")
        if self.last_error_code is not None:
            _text(self.last_error_code, "last_error_code", 1, 80)
        _text(self.created_at, "created_at", 1, 80)
        _text(self.available_at, "available_at", 1, 80)
        _text(self.updated_at, "updated_at", 1, 80)


@dataclass(frozen=True, slots=True)
class SubscriptionActivation:
    activation_id: str
    conversation_id: str
    definition_outcome_id: str
    definition_id: str
    subscription_id: str
    user_subscription_id: str
    application_run_id: str
    outbox_id: str
    created_at: str

    def __post_init__(self):
        for name in (
            "activation_id", "conversation_id", "definition_outcome_id",
            "definition_id", "subscription_id", "user_subscription_id",
            "application_run_id", "outbox_id",
        ):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"{name} 无效")
        _text(self.created_at, "created_at", 1, 80)


@dataclass(frozen=True, slots=True)
class SubscriptionCommit:
    definition: SubscriptionDefinition
    legacy_subscription: "Subscription"
    subscription: ProductSubscription
    relation: UserSubscription
    relation_event: RelationEventOutbox | None
    briefing: BriefingReservation
    outbox: ApplicationOutbox
    activation: SubscriptionActivation
    reused: bool = False


@dataclass(frozen=True, slots=True)
class TrackingDefinition:
    """Application-selected tracking truth, separate from execution policy."""

    definition_id: str
    definition_version: int
    subscription_id: str
    workflow_kind: str
    snapshot: dict
    snapshot_identity: str
    created_at: str

    def __post_init__(self):
        for name in ("definition_id", "subscription_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"Tracking Definition {name} 无效")
        _strict_int(
            self.definition_version, "Tracking Definition version",
            1, 2**31 - 1,
        )
        if self.workflow_kind == "CONDITION":
            normalized = normalize_flight_condition_snapshot(self.snapshot)
        elif self.workflow_kind == "EVENT":
            normalized = normalize_openai_event_snapshot(self.snapshot)
        else:
            raise DomainError("Tracking Definition workflow 不受支持")
        object.__setattr__(self, "snapshot", copy.deepcopy(normalized))
        if self.snapshot_identity != _canonical_identity(normalized):
            raise DomainError("Tracking Definition identity mismatch")
        _text(self.created_at, "created_at", 1, 80)


@dataclass(frozen=True, slots=True)
class TrackingPolicySnapshot:
    subscription_id: str
    definition_id: str
    definition_version: int
    execution: dict
    presentation: dict
    distribution: dict
    snapshot_identity: str
    created_at: str

    def __post_init__(self):
        for name in ("subscription_id", "definition_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"Tracking Policy {name} 无效")
        _strict_int(
            self.definition_version, "Tracking Policy version", 1, 2**31 - 1,
        )
        legacy_execution = {
            "observation_source": "fake_flight_price",
            "observation_cadence": "manual_once",
            "freshness_seconds": 86_400,
            "evaluator_version": "flight_price_lt_v1",
        }
        continuous_keys = {
            "execution_policy_version", "observation_source",
            "observation_cadence", "cadence_seconds", "cadence_provenance",
            "timezone", "schedule_anchor_at", "freshness_seconds",
            "evaluator_version", "travel_year", "window_start_at",
            "window_end_exclusive",
        }
        continuous = (
            isinstance(self.execution, dict)
            and set(self.execution) == continuous_keys
            and self.execution["execution_policy_version"] == 1
            and self.execution["observation_source"] == "fake_flight_price"
            and self.execution["observation_cadence"] in FLIGHT_CADENCES
            and self.execution["cadence_seconds"] == FLIGHT_CADENCES.get(
                self.execution["observation_cadence"])
            and self.execution["cadence_provenance"] in {
                "USER_EXPLICIT", "USER_CONFIRMED", "PRODUCT_DEFAULT",
            }
            and self.execution["timezone"] == "Asia/Shanghai"
            and self.execution["freshness_seconds"] == 86_400
            and self.execution["evaluator_version"] == "flight_price_lt_v1"
            and type(self.execution["travel_year"]) is int
        )
        event_keys = {
            "execution_policy_version", "observation_source",
            "observation_cadence", "cadence_seconds", "cadence_provenance",
            "timezone", "schedule_anchor_at", "freshness_seconds",
            "verification_policy", "overlap_seconds", "temporal_scope",
        }
        event = (
            isinstance(self.execution, dict)
            and set(self.execution) == event_keys
            and self.execution["execution_policy_version"] == 1
            and self.execution["observation_source"] == "fake_openai_event"
            and self.execution["observation_cadence"] in FLIGHT_CADENCES
            and self.execution["cadence_seconds"] == FLIGHT_CADENCES.get(
                self.execution["observation_cadence"])
            and self.execution["cadence_provenance"] in {
                "USER_EXPLICIT", "USER_CONFIRMED", "PRODUCT_DEFAULT",
            }
            and self.execution["timezone"] == "Asia/Shanghai"
            and self.execution["freshness_seconds"] == 86_400
            and self.execution["verification_policy"]
            == "openai_model_release_v1"
            and self.execution["overlap_seconds"] == 86_400
            and self.execution["temporal_scope"]
            == "FUTURE_FROM_ACTIVATION"
        )
        if self.execution != legacy_execution and not continuous and not event:
            raise DomainError("Tracking execution policy 无效")
        if continuous:
            for name in (
                    "schedule_anchor_at", "window_start_at",
                    "window_end_exclusive"):
                _parse_utc_timestamp(self.execution[name], name)
            year = self.execution["travel_year"]
            if not 2020 <= year <= 2200:
                raise DomainError("Flight travel_year 无效")
            zone = ZoneInfo("Asia/Shanghai")
            start = _parse_utc_timestamp(
                self.execution["window_start_at"], "window_start_at",
            ).astimezone(zone)
            end = _parse_utc_timestamp(
                self.execution["window_end_exclusive"],
                "window_end_exclusive",
            ).astimezone(zone)
            if (start != datetime(year, 9, 1, tzinfo=zone)
                    or end != datetime(year, 10, 1, tzinfo=zone)):
                raise DomainError("Flight lifetime window 无效")
        if event:
            _parse_utc_timestamp(
                self.execution["schedule_anchor_at"], "schedule_anchor_at",
            )
        if (not isinstance(self.presentation, dict)
                or set(self.presentation) != {
                    "language", "max_chars", "max_items", "provenance",
                }
                or self.presentation["language"] not in LANGUAGES
                or type(self.presentation["max_chars"]) is not int
                or type(self.presentation["max_items"]) is not int
                or self.presentation["provenance"] not in {
                    "USER_EXPLICIT", "USER_CONFIRMED", "PRODUCT_DEFAULT",
                    "POLICY_DEFAULT",
                }):
            raise DomainError("Tracking presentation policy 无效")
        _strict_int(self.presentation["max_chars"], "max_chars", 100, 4000)
        _strict_int(self.presentation["max_items"], "max_items", 1, 10)
        if (not isinstance(self.distribution, dict)
                or set(self.distribution) != {"notification", "provenance"}
                or self.distribution["notification"]
                not in CONDITION_NOTIFICATION_POLICIES
                or self.distribution["provenance"] not in {
                    "USER_EXPLICIT", "USER_CONFIRMED", "PRODUCT_DEFAULT",
                    "POLICY_DEFAULT",
                }):
            raise DomainError("Tracking distribution policy 无效")
        expected_identity = _canonical_identity({
            "execution": self.execution,
            "presentation": self.presentation,
            "distribution": self.distribution,
        })
        if self.snapshot_identity != expected_identity:
            raise DomainError("Tracking Policy identity mismatch")
        object.__setattr__(self, "execution", copy.deepcopy(self.execution))
        object.__setattr__(self, "presentation", copy.deepcopy(self.presentation))
        object.__setattr__(self, "distribution", copy.deepcopy(self.distribution))
        _text(self.created_at, "created_at", 1, 80)


@dataclass(frozen=True, slots=True)
class ConditionObservationRequest:
    request_id: str
    subscription_id: str
    definition_id: str
    definition_version: int
    idempotency_key: str
    status: str
    evaluation_id: str | None
    failure_code: str | None
    created_at: str
    updated_at: str

    def __post_init__(self):
        for name in ("request_id", "subscription_id", "definition_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"Condition request {name} 无效")
        _strict_int(self.definition_version, "Condition definition version", 1,
                    2**31 - 1)
        _text(self.idempotency_key, "Condition idempotency key", 1, 120)
        if self.status not in CONDITION_REQUEST_STATUSES:
            raise DomainError("Condition request status 无效")
        if (self.evaluation_id is not None
                and not ID_PATTERN.fullmatch(str(self.evaluation_id))):
            raise DomainError("Condition evaluation ref 无效")
        valid = (
            self.status == "PENDING" and self.evaluation_id is None
            and self.failure_code is None
        ) or (
            self.status == "EVALUATED" and self.evaluation_id is not None
            and self.failure_code is None
        ) or (
            self.status == "FAILED" and self.evaluation_id is None
            and self.failure_code in {
                "INVALID_OBSERVATION", "STALE_OBSERVATION",
            }
        )
        if not valid:
            raise DomainError("Condition request state 无效")
        _text(self.created_at, "created_at", 1, 80)
        _text(self.updated_at, "updated_at", 1, 80)


@dataclass(frozen=True, slots=True)
class ConditionTemporalState:
    subscription_id: str
    definition_id: str
    definition_version: int
    execution_policy_version: int
    lifecycle_status: str
    cadence_seconds: int
    cadence_provenance: str
    timezone_name: str
    schedule_anchor_at: str
    window_start_at: str
    window_end_exclusive: str
    next_due_at: str | None
    last_attempted_cycle_id: str | None
    last_attempted_at: str | None
    last_successful_cycle_id: str | None
    last_successful_cycle_at: str | None
    last_failure_code: str | None
    last_failure_at: str | None
    last_observation_id: str | None
    last_evaluation_id: str | None
    last_observed_at: str | None
    previous_truth: str
    armed: bool
    last_emitted_evaluation_id: str | None
    last_emitted_update_id: str | None
    last_emitted_at: str | None
    paused_at: str | None
    completed_at: str | None
    completion_reason: str | None
    version: int
    created_at: str
    updated_at: str

    def __post_init__(self):
        for name in ("subscription_id", "definition_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"Condition temporal {name} 无效")
        _strict_int(self.definition_version, "definition_version", 1,
                    2**31 - 1)
        _strict_int(self.execution_policy_version, "execution_policy_version",
                    1, 2**31 - 1)
        if self.lifecycle_status not in CONDITION_TEMPORAL_LIFECYCLES:
            raise DomainError("Condition temporal lifecycle 无效")
        if self.cadence_seconds not in FLIGHT_CADENCES.values():
            raise DomainError("Condition cadence_seconds 无效")
        if self.cadence_provenance not in {
                "USER_EXPLICIT", "USER_CONFIRMED", "PRODUCT_DEFAULT"}:
            raise DomainError("Condition cadence provenance 无效")
        if self.timezone_name != "Asia/Shanghai":
            raise DomainError("Condition timezone 无效")
        for name in (
                "schedule_anchor_at", "window_start_at",
                "window_end_exclusive", "created_at", "updated_at"):
            _parse_utc_timestamp(getattr(self, name), name)
        for name in (
                "next_due_at", "last_attempted_at", "last_successful_cycle_at",
                "last_failure_at", "last_observed_at", "last_emitted_at",
                "paused_at", "completed_at"):
            value = getattr(self, name)
            if value is not None:
                _parse_utc_timestamp(value, name)
        for name in (
                "last_attempted_cycle_id", "last_successful_cycle_id",
                "last_observation_id", "last_evaluation_id",
                "last_emitted_evaluation_id", "last_emitted_update_id"):
            value = getattr(self, name)
            if value is not None and not ID_PATTERN.fullmatch(str(value)):
                raise DomainError(f"Condition temporal {name} 无效")
        if self.previous_truth not in CONDITION_TRUTHS:
            raise DomainError("Condition previous truth 无效")
        if type(self.armed) is not bool:
            raise DomainError("Condition armed 无效")
        if self.armed != (self.previous_truth != "TRUE"):
            raise DomainError("Condition armed/truth mismatch")
        if (self.last_failure_code is not None
                and self.last_failure_code not in CONDITION_CYCLE_FAILURES):
            raise DomainError("Condition last failure 无效")
        lifecycle_valid = (
            self.lifecycle_status == "ACTIVE" and self.next_due_at is not None
            and self.paused_at is None and self.completed_at is None
            and self.completion_reason is None
        ) or (
            self.lifecycle_status == "PAUSED" and self.next_due_at is None
            and self.paused_at is not None and self.completed_at is None
            and self.completion_reason is None
        ) or (
            self.lifecycle_status == "COMPLETED" and self.next_due_at is None
            and self.completed_at is not None
            and self.completion_reason == "TIME_WINDOW_ENDED"
        )
        if not lifecycle_valid:
            raise DomainError("Condition temporal lifecycle fields mismatch")
        _strict_int(self.version, "Condition temporal version", 1, 2**31 - 1)


@dataclass(frozen=True, slots=True)
class ConditionObservationCycle:
    cycle_id: str
    request_id: str
    subscription_id: str
    definition_id: str
    definition_version: int
    execution_policy_version: int
    cycle_kind: str
    scheduled_due_at: str
    coalesced_from_at: str
    coalesced_to_at: str
    coalesced_count: int
    status: str
    claim_token: str | None
    claimed_at: str | None
    observation_id: str | None
    evaluation_id: str | None
    predicate_truth: str | None
    emission_decision: str | None
    update_id: str | None
    distribution_id: str | None
    failure_code: str | None
    created_at: str
    updated_at: str

    def __post_init__(self):
        for name in (
                "cycle_id", "request_id", "subscription_id", "definition_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"Condition cycle {name} 无效")
        _strict_int(self.definition_version, "definition_version", 1,
                    2**31 - 1)
        _strict_int(self.execution_policy_version, "execution_policy_version",
                    1, 2**31 - 1)
        if self.cycle_kind not in CONDITION_CYCLE_KINDS:
            raise DomainError("Condition cycle kind 无效")
        for name in (
                "scheduled_due_at", "coalesced_from_at", "coalesced_to_at",
                "created_at", "updated_at"):
            _parse_utc_timestamp(getattr(self, name), name)
        _strict_int(self.coalesced_count, "coalesced_count", 1, 1_000_000)
        if self.status not in CONDITION_CYCLE_STATUSES:
            raise DomainError("Condition cycle status 无效")
        for name in (
                "claim_token", "observation_id", "evaluation_id", "update_id",
                "distribution_id"):
            value = getattr(self, name)
            if value is not None and not ID_PATTERN.fullmatch(str(value)):
                raise DomainError(f"Condition cycle {name} 无效")
        if self.claimed_at is not None:
            _parse_utc_timestamp(self.claimed_at, "claimed_at")
        if (self.predicate_truth is not None
                and self.predicate_truth not in {"FALSE", "TRUE"}):
            raise DomainError("Condition cycle predicate truth 无效")
        if (self.emission_decision is not None
                and self.emission_decision not in CONDITION_EMISSION_DECISIONS):
            raise DomainError("Condition cycle emission decision 无效")
        if (self.failure_code is not None
                and self.failure_code not in CONDITION_CYCLE_FAILURES):
            raise DomainError("Condition cycle failure 无效")
        expected_id = condition_cycle_identity(
            self.subscription_id, self.execution_policy_version,
            self.scheduled_due_at, self.cycle_kind,
        )
        if self.cycle_id != expected_id:
            raise DomainError("Condition cycle identity mismatch")


@dataclass(frozen=True, slots=True)
class ConditionSubscriptionActivation:
    activation_id: str
    conversation_id: str
    definition_outcome_id: str
    definition_id: str
    subscription_id: str
    user_subscription_id: str
    condition_request_id: str
    created_at: str

    def __post_init__(self):
        for name in (
            "activation_id", "conversation_id", "definition_outcome_id",
            "definition_id", "subscription_id", "user_subscription_id",
            "condition_request_id",
        ):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"Condition activation {name} 无效")
        _text(self.created_at, "created_at", 1, 80)


@dataclass(frozen=True, slots=True)
class ConditionSubscriptionCommit:
    definition: SubscriptionDefinition
    legacy_subscription: "Subscription"
    subscription: ProductSubscription
    relation: UserSubscription
    relation_event: RelationEventOutbox
    tracking_definition: TrackingDefinition
    policies: TrackingPolicySnapshot
    condition_request: ConditionObservationRequest
    activation: ConditionSubscriptionActivation
    temporal_state: ConditionTemporalState | None
    initial_cycle: ConditionObservationCycle | None
    reused: bool = False


@dataclass(frozen=True, slots=True)
class EventSourceResult:
    source_ref: str
    canonical_url: str
    publisher: str
    source_kind: str
    title: str
    snippet: str
    published_at: str | None
    content_fingerprint: str

    def __post_init__(self):
        _text(self.source_ref, "EVENT source_ref", 1, 120)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}",
                            self.source_ref):
            raise DomainError("EVENT source_ref 无效")
        parsed = urlsplit(_text(
            self.canonical_url, "EVENT canonical_url", 1, 500,
        ))
        if parsed.scheme != "https" or not parsed.hostname:
            raise DomainError("EVENT source URL 无效")
        _safe_protocol_text(self.publisher, "EVENT publisher", 100)
        if self.source_kind not in {"official_primary", "secondary"}:
            raise DomainError("EVENT source_kind 无效")
        _safe_protocol_text(self.title, "EVENT title", 300)
        _safe_protocol_text(self.snippet, "EVENT snippet", 1000)
        if self.published_at is not None:
            _parse_utc_timestamp(self.published_at, "published_at")
        expected = _canonical_identity({
            "canonical_url": self.canonical_url,
            "publisher": self.publisher,
            "source_kind": self.source_kind,
            "title": self.title,
            "snippet": self.snippet,
            "published_at": self.published_at,
        })
        if self.content_fingerprint != expected:
            raise DomainError("EVENT content fingerprint mismatch")

    def as_dict(self):
        return {
            "source_ref": self.source_ref,
            "canonical_url": self.canonical_url,
            "publisher": self.publisher,
            "source_kind": self.source_kind,
            "title": self.title,
            "snippet": self.snippet,
            "published_at": self.published_at,
            "content_fingerprint": self.content_fingerprint,
        }


def event_source_content_fingerprint(*, canonical_url, publisher,
                                     source_kind, title, snippet,
                                     published_at):
    return _canonical_identity({
        "canonical_url": canonical_url, "publisher": publisher,
        "source_kind": source_kind, "title": title, "snippet": snippet,
        "published_at": published_at,
    })


@dataclass(frozen=True, slots=True)
class EventObservationQuery:
    entity_key: str
    window_start_at: str
    window_end_at: str

    def __post_init__(self):
        if self.entity_key != "openai":
            raise DomainError("EVENT query entity 不受支持")
        start = _parse_utc_timestamp(self.window_start_at, "window_start_at")
        end = _parse_utc_timestamp(self.window_end_at, "window_end_at")
        if start > end:
            raise DomainError("EVENT query window 无效")


@dataclass(frozen=True, slots=True)
class EventSourceObservation:
    observation_id: str
    entity_key: str
    window_start_at: str
    window_end_at: str
    retrieved_at: str
    coverage_complete: bool
    truncated: bool
    results: tuple[EventSourceResult, ...]
    provider: str = "fake_event_search"

    def __post_init__(self):
        if self.entity_key != "openai" or self.provider != "fake_event_search":
            raise DomainError("EVENT Observation source 不受支持")
        EventObservationQuery(
            self.entity_key, self.window_start_at, self.window_end_at,
        )
        _parse_utc_timestamp(self.retrieved_at, "retrieved_at")
        if type(self.coverage_complete) is not bool or type(self.truncated) is not bool:
            raise DomainError("EVENT Observation coverage 无效")
        if not isinstance(self.results, tuple) or len(self.results) > 20:
            raise DomainError("EVENT Observation results 无效")
        if not all(isinstance(item, EventSourceResult) for item in self.results):
            raise DomainError("EVENT Observation result type 无效")
        refs = [item.source_ref for item in self.results]
        if len(refs) != len(set(refs)):
            raise DomainError("EVENT Observation source_ref 重复")
        expected = event_observation_identity(
            self.entity_key, self.window_start_at, self.window_end_at,
            self.retrieved_at, self.coverage_complete, self.truncated,
            self.results, self.provider,
        )
        if self.observation_id != expected:
            raise DomainError("EVENT Observation identity mismatch")

    def as_dict(self):
        return {
            "observation_id": self.observation_id,
            "entity_key": self.entity_key,
            "window_start_at": self.window_start_at,
            "window_end_at": self.window_end_at,
            "retrieved_at": self.retrieved_at,
            "coverage": {
                "complete": self.coverage_complete,
                "truncated": self.truncated,
            },
            "provider": self.provider,
            "results": [item.as_dict() for item in self.results],
        }


@dataclass(frozen=True, slots=True)
class EventCandidateSupport:
    source_ref: str
    exact_span: str

    def __post_init__(self):
        _text(self.source_ref, "EVENT candidate source_ref", 1, 120)
        _safe_protocol_text(self.exact_span, "EVENT candidate exact_span", 500)


@dataclass(frozen=True, slots=True)
class EventCandidate:
    candidate_id: str
    observation_id: str
    harness_run_id: str
    entity_key: str
    event_type: str
    object_type: str
    display_name: str
    canonical_name_candidate: str
    occurred_at_candidate: str | None
    support: tuple[EventCandidateSupport, ...]

    def __post_init__(self):
        for name in ("candidate_id", "observation_id", "harness_run_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"EVENT candidate {name} 无效")
        _text(self.entity_key, "EVENT candidate entity", 1, 80)
        _text(self.event_type, "EVENT candidate type", 1, 80)
        _text(self.object_type, "EVENT candidate object type", 1, 80)
        _safe_protocol_text(self.display_name, "EVENT model display name", 120)
        _safe_protocol_text(
            self.canonical_name_candidate, "EVENT canonical name", 120,
        )
        if self.occurred_at_candidate is not None:
            _parse_utc_timestamp(
                self.occurred_at_candidate, "occurred_at_candidate",
            )
        if (not isinstance(self.support, tuple) or not self.support
                or len(self.support) > 10
                or not all(isinstance(item, EventCandidateSupport)
                           for item in self.support)):
            raise DomainError("EVENT candidate support 无效")
        expected = event_candidate_identity(
            self.observation_id, self.harness_run_id, self.entity_key,
            self.event_type, self.object_type, self.display_name,
            self.canonical_name_candidate, self.occurred_at_candidate,
            self.support,
        )
        if self.candidate_id != expected:
            raise DomainError("EVENT candidate identity mismatch")


@dataclass(frozen=True, slots=True)
class EventVerification:
    verification_id: str
    subscription_id: str
    definition_id: str
    definition_version: int
    observation_id: str
    observation_evidence_id: str
    candidate_id: str | None
    outcome: str
    reason_code: str
    policy_version: str
    logical_event_identity: str | None
    canonical_model_key: str | None
    verification_evidence_id: str
    verified_at: str

    def __post_init__(self):
        for name in (
                "verification_id", "subscription_id", "definition_id",
                "observation_id", "observation_evidence_id",
                "verification_evidence_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"EVENT verification {name} 无效")
        if self.candidate_id is not None and not ID_PATTERN.fullmatch(
                self.candidate_id):
            raise DomainError("EVENT verification candidate ref 无效")
        _strict_int(self.definition_version, "EVENT definition version", 1,
                    2**31 - 1)
        if self.outcome not in EVENT_VERIFICATION_OUTCOMES:
            raise DomainError("EVENT verification outcome 无效")
        if self.reason_code not in EVENT_VERIFICATION_REASONS:
            raise DomainError("EVENT verification reason 无效")
        if self.policy_version != "openai_model_release_v1":
            raise DomainError("EVENT verification policy 无效")
        verified = self.outcome == "VERIFIED"
        if verified != (self.logical_event_identity is not None
                         and self.canonical_model_key is not None):
            raise DomainError("EVENT verification truth binding 无效")
        if self.logical_event_identity is not None and not re.fullmatch(
                r"[0-9a-f]{64}", self.logical_event_identity):
            raise DomainError("EVENT logical identity 无效")
        if self.canonical_model_key is not None:
            _text(self.canonical_model_key, "canonical_model_key", 1, 120)
        expected = event_verification_identity(
            self.subscription_id, self.definition_id,
            self.definition_version, self.observation_id, self.candidate_id,
            self.policy_version,
        )
        if self.verification_id != expected:
            raise DomainError("EVENT verification identity mismatch")
        _parse_utc_timestamp(self.verified_at, "verified_at")


@dataclass(frozen=True, slots=True)
class VerifiedEvent:
    event_id: str
    logical_event_identity: str
    entity_key: str
    event_type: str
    object_type: str
    canonical_model_key: str
    display_name: str
    occurred_at: str
    verification_id: str
    verification_evidence_id: str
    created_at: str

    def __post_init__(self):
        for name in (
                "event_id", "verification_id", "verification_evidence_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"Verified Event {name} 无效")
        expected_identity = verified_event_identity(
            self.entity_key, self.event_type, self.object_type,
            self.canonical_model_key,
        )
        if self.logical_event_identity != expected_identity:
            raise DomainError("Verified Event logical identity mismatch")
        if self.event_id != expected_identity[:32]:
            raise DomainError("Verified Event identity mismatch")
        if (self.entity_key != "openai"
                or self.event_type != "MODEL_RELEASED"
                or self.object_type != "MODEL"):
            raise DomainError("Verified Event criterion 不受支持")
        _safe_protocol_text(self.display_name, "Verified Event display name", 120)
        _parse_utc_timestamp(self.occurred_at, "occurred_at")
        _parse_utc_timestamp(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class EventTemporalState:
    subscription_id: str
    definition_id: str
    definition_version: int
    execution_policy_version: int
    lifecycle_status: str
    cadence_seconds: int
    cadence_provenance: str
    timezone_name: str
    schedule_anchor_at: str
    activation_at: str
    next_due_at: str | None
    verified_through: str | None
    last_attempted_cycle_id: str | None
    last_attempted_at: str | None
    last_successful_cycle_id: str | None
    last_successful_cycle_at: str | None
    last_failure_code: str | None
    last_failure_at: str | None
    last_verification_id: str | None
    last_update_id: str | None
    paused_at: str | None
    version: int
    created_at: str
    updated_at: str

    def __post_init__(self):
        for name in ("subscription_id", "definition_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"EVENT temporal {name} 无效")
        _strict_int(self.definition_version, "EVENT definition version", 1,
                    2**31 - 1)
        _strict_int(self.execution_policy_version, "EVENT policy version", 1,
                    2**31 - 1)
        if self.lifecycle_status not in EVENT_TEMPORAL_LIFECYCLES:
            raise DomainError("EVENT temporal lifecycle 无效")
        if self.cadence_seconds not in FLIGHT_CADENCES.values():
            raise DomainError("EVENT cadence 无效")
        if self.cadence_provenance not in {
                "USER_EXPLICIT", "USER_CONFIRMED", "PRODUCT_DEFAULT"}:
            raise DomainError("EVENT cadence provenance 无效")
        if self.timezone_name != "Asia/Shanghai":
            raise DomainError("EVENT timezone 无效")
        for name in (
                "schedule_anchor_at", "activation_at", "created_at",
                "updated_at"):
            _parse_utc_timestamp(getattr(self, name), name)
        for name in (
                "next_due_at", "verified_through", "last_attempted_at",
                "last_successful_cycle_at", "last_failure_at", "paused_at"):
            value = getattr(self, name)
            if value is not None:
                _parse_utc_timestamp(value, name)
        for name in (
                "last_attempted_cycle_id", "last_successful_cycle_id",
                "last_verification_id", "last_update_id"):
            value = getattr(self, name)
            if value is not None and not ID_PATTERN.fullmatch(str(value)):
                raise DomainError(f"EVENT temporal {name} 无效")
        if (self.last_failure_code is not None
                and self.last_failure_code not in EVENT_CYCLE_FAILURES):
            raise DomainError("EVENT temporal failure 无效")
        if self.lifecycle_status == "ACTIVE":
            valid = self.next_due_at is not None and self.paused_at is None
        else:
            valid = self.next_due_at is None and self.paused_at is not None
        if not valid:
            raise DomainError("EVENT temporal lifecycle fields mismatch")
        _strict_int(self.version, "EVENT temporal version", 1, 2**31 - 1)


@dataclass(frozen=True, slots=True)
class EventObservationCycle:
    cycle_id: str
    subscription_id: str
    definition_id: str
    definition_version: int
    execution_policy_version: int
    cycle_kind: str
    scheduled_due_at: str
    coalesced_from_at: str
    coalesced_to_at: str
    coalesced_count: int
    window_start_at: str
    window_end_at: str
    status: str
    harness_run_id: str
    claim_token: str | None
    claimed_at: str | None
    observation_id: str | None
    candidate_id: str | None
    verification_id: str | None
    outcome: str | None
    reason_code: str | None
    event_id: str | None
    update_id: str | None
    distribution_id: str | None
    failure_code: str | None
    created_at: str
    updated_at: str

    def __post_init__(self):
        for name in (
                "cycle_id", "subscription_id", "definition_id",
                "harness_run_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"EVENT cycle {name} 无效")
        _strict_int(self.definition_version, "EVENT definition version", 1,
                    2**31 - 1)
        _strict_int(self.execution_policy_version, "EVENT policy version", 1,
                    2**31 - 1)
        if self.cycle_kind not in EVENT_CYCLE_KINDS:
            raise DomainError("EVENT cycle kind 无效")
        for name in (
                "scheduled_due_at", "coalesced_from_at", "coalesced_to_at",
                "window_start_at", "window_end_at", "created_at",
                "updated_at"):
            _parse_utc_timestamp(getattr(self, name), name)
        _strict_int(self.coalesced_count, "EVENT coalesced_count", 1,
                    1_000_000)
        if self.status not in EVENT_CYCLE_STATUSES:
            raise DomainError("EVENT cycle status 无效")
        for name in (
                "claim_token", "observation_id", "candidate_id",
                "verification_id", "event_id", "update_id",
                "distribution_id"):
            value = getattr(self, name)
            if value is not None and not ID_PATTERN.fullmatch(str(value)):
                raise DomainError(f"EVENT cycle {name} 无效")
        if self.claimed_at is not None:
            _parse_utc_timestamp(self.claimed_at, "claimed_at")
        if self.outcome is not None and self.outcome not in EVENT_VERIFICATION_OUTCOMES:
            raise DomainError("EVENT cycle outcome 无效")
        if self.reason_code is not None and self.reason_code not in EVENT_VERIFICATION_REASONS:
            raise DomainError("EVENT cycle reason 无效")
        if self.failure_code is not None and self.failure_code not in EVENT_CYCLE_FAILURES:
            raise DomainError("EVENT cycle failure 无效")
        expected = event_cycle_identity(
            self.subscription_id, self.execution_policy_version,
            self.scheduled_due_at, self.cycle_kind,
        )
        if self.cycle_id != expected:
            raise DomainError("EVENT cycle identity mismatch")


@dataclass(frozen=True, slots=True)
class EventSubscriptionActivation:
    activation_id: str
    conversation_id: str
    definition_outcome_id: str
    definition_id: str
    subscription_id: str
    user_subscription_id: str
    initial_cycle_id: str
    created_at: str

    def __post_init__(self):
        for name in (
                "activation_id", "conversation_id", "definition_outcome_id",
                "definition_id", "subscription_id", "user_subscription_id",
                "initial_cycle_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"EVENT activation {name} 无效")
        _parse_utc_timestamp(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class EventSubscriptionCommit:
    definition: SubscriptionDefinition
    legacy_subscription: "Subscription"
    subscription: ProductSubscription
    relation: UserSubscription
    relation_event: RelationEventOutbox
    tracking_definition: TrackingDefinition
    policies: TrackingPolicySnapshot
    activation: EventSubscriptionActivation
    temporal_state: EventTemporalState
    initial_cycle: EventObservationCycle
    reused: bool = False


@dataclass(frozen=True, slots=True)
class FlightObservationQuery:
    origin: str
    destination: str
    trip_type: str
    travel_month: int

    def __post_init__(self):
        if (self.origin, self.destination, self.trip_type, self.travel_month) != (
                "深圳", "武汉", "round_trip", 9):
            raise DomainError("P4.3 flight observation query 不受支持")


@dataclass(frozen=True, slots=True)
class FlightPriceQuote:
    source_signal_id: str
    origin: str
    destination: str
    trip_type: str
    travel_month: int
    metric: str
    price: int
    currency: str
    observed_at: str

    def __post_init__(self):
        _text(self.source_signal_id, "source_signal_id", 1, 120)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}",
                            self.source_signal_id):
            raise DomainError("flight source signal identity 无效")
        FlightObservationQuery(
            self.origin, self.destination, self.trip_type, self.travel_month,
        )
        if self.metric != "round_trip_price":
            raise DomainError("flight price metric 无效")
        _strict_int(self.price, "flight price", 1, 1_000_000)
        if self.currency != "CNY":
            raise DomainError("flight price currency 无效")
        _parse_utc_timestamp(self.observed_at, "observed_at")

    def as_dict(self):
        return {
            "source_signal_id": self.source_signal_id,
            "origin": self.origin,
            "destination": self.destination,
            "trip_type": self.trip_type,
            "travel_month": self.travel_month,
            "metric": self.metric,
            "price": self.price,
            "currency": self.currency,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True, slots=True)
class AcceptedFlightPriceObservation:
    observation_id: str
    subscription_id: str
    quote: FlightPriceQuote
    evidence_id: str
    signal_identity: str
    accepted_at: str

    def __post_init__(self):
        for name in ("observation_id", "subscription_id", "evidence_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"Accepted flight observation {name} 无效")
        if not isinstance(self.quote, FlightPriceQuote):
            raise DomainError("typed FlightPriceQuote 必须先通过 validation")
        expected = flight_price_signal_identity(self.subscription_id, self.quote)
        if self.signal_identity != expected:
            raise DomainError("flight signal identity mismatch")
        if self.observation_id != expected[:32]:
            raise DomainError("flight observation identity mismatch")
        _parse_utc_timestamp(self.accepted_at, "accepted_at")


@dataclass(frozen=True, slots=True)
class ConditionEvaluation:
    evaluation_id: str
    subscription_id: str
    definition_id: str
    definition_version: int
    observation_id: str
    evidence_id: str
    observed_price: int
    threshold: int
    currency: str
    operator: str
    result: str
    evaluator_version: str
    evaluated_at: str

    def __post_init__(self):
        for name in (
                "evaluation_id", "subscription_id", "definition_id",
                "observation_id", "evidence_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"Condition evaluation {name} 无效")
        _strict_int(self.definition_version, "Condition definition version", 1,
                    2**31 - 1)
        _strict_int(self.observed_price, "observed price", 1, 1_000_000)
        _strict_int(self.threshold, "threshold", 1, 1_000_000)
        if (self.currency != "CNY" or self.operator != "lt"
                or self.evaluator_version != "flight_price_lt_v1"):
            raise DomainError("Condition predicate 无效")
        expected_result = (
            "MATCHED" if self.observed_price < self.threshold else "NO_UPDATE"
        )
        if self.result not in CONDITION_RESULTS or self.result != expected_result:
            raise DomainError("Condition deterministic result mismatch")
        expected_id = condition_evaluation_identity(
            self.subscription_id, self.definition_id,
            self.definition_version, self.observation_id,
        )
        if self.evaluation_id != expected_id:
            raise DomainError("Condition evaluation identity mismatch")
        _parse_utc_timestamp(self.evaluated_at, "evaluated_at")


@dataclass(frozen=True, slots=True)
class TrackingUpdate:
    update_id: str
    subscription_id: str
    definition_id: str
    definition_version: int
    evaluation_id: str
    evidence_id: str
    update_type: str
    payload: dict
    occurred_at: str
    created_at: str
    verified_event_id: str | None = None

    def __post_init__(self):
        for name in (
                "update_id", "subscription_id", "definition_id",
                "evaluation_id", "evidence_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"Update {name} 无效")
        _strict_int(self.definition_version, "Update definition version", 1,
                    2**31 - 1)
        if self.verified_event_id is not None and not ID_PATTERN.fullmatch(
                str(self.verified_event_id)):
            raise DomainError("Update verified_event_id 无效")
        if self.update_type == "CONDITION":
            required = {
                "title", "summary", "origin", "destination", "travel_month",
                "observed_price", "threshold", "currency", "observed_at",
            }
            if self.verified_event_id is not None:
                raise DomainError("CONDITION Update 不得绑定 Verified Event")
            if not isinstance(self.payload, dict) or set(self.payload) != required:
                raise DomainError("CONDITION Update payload 无效")
            for name in ("title", "summary", "origin", "destination", "currency"):
                _safe_protocol_text(self.payload[name], f"Update {name}", 500)
            _strict_int(self.payload["travel_month"], "travel_month", 1, 12)
            _strict_int(self.payload["observed_price"], "observed_price", 1, 1_000_000)
            _strict_int(self.payload["threshold"], "threshold", 1, 1_000_000)
            _parse_utc_timestamp(self.payload["observed_at"], "observed_at")
            expected_id = condition_update_identity(self.evaluation_id)
        elif self.update_type == "EVENT":
            required = {
                "title", "summary", "entity", "model_name", "event_type",
                "occurred_at", "source_title", "source_url",
            }
            if self.verified_event_id is None:
                raise DomainError("EVENT Update 必须绑定 Verified Event")
            if not isinstance(self.payload, dict) or set(self.payload) != required:
                raise DomainError("EVENT Update payload 无效")
            for name in required:
                _safe_protocol_text(self.payload[name], f"EVENT Update {name}", 500)
            if (self.payload["entity"] != "OpenAI"
                    or self.payload["event_type"] != "MODEL_RELEASED"):
                raise DomainError("EVENT Update criterion 无效")
            _parse_utc_timestamp(self.payload["occurred_at"], "occurred_at")
            parsed = urlsplit(self.payload["source_url"])
            if parsed.scheme != "https" or parsed.hostname not in {
                    "openai.com", "www.openai.com"}:
                raise DomainError("EVENT Update source URL 无效")
            expected_id = event_update_identity(
                self.subscription_id, self.verified_event_id,
            )
        else:
            raise DomainError("Update type 无效")
        if self.update_id != expected_id:
            raise DomainError("Update identity mismatch")
        object.__setattr__(self, "payload", copy.deepcopy(self.payload))
        _parse_utc_timestamp(self.occurred_at, "occurred_at")
        _parse_utc_timestamp(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class UpdateDistribution:
    distribution_id: str
    update_id: str
    user_subscription_id: str
    status: str
    created_at: str

    def __post_init__(self):
        for name in ("distribution_id", "update_id", "user_subscription_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"Distribution {name} 无效")
        if self.status != "AVAILABLE":
            raise DomainError("P4.3 Distribution status 无效")
        if self.distribution_id != update_distribution_identity(
                self.update_id, self.user_subscription_id):
            raise DomainError("Distribution identity mismatch")
        _parse_utc_timestamp(self.created_at, "created_at")


def _parse_utc_timestamp(value, name):
    value = _text(value, name, 1, 80)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise DomainError(f"{name} 无效") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DomainError(f"{name} 必须包含 timezone")
    return parsed.astimezone(timezone.utc)


def normalize_flight_condition_snapshot(value):
    if not isinstance(value, dict) or set(value) != {
            "schema_version", "subject", "route", "travel_month", "signal",
            "provenance"}:
        raise DomainError("Flight CONDITION Tracking Definition schema 无效")
    if value["schema_version"] != 1 or value["subject"] != "深圳往返武汉的机票优惠":
        raise DomainError("Flight CONDITION subject 无效")
    if value["route"] != {
            "origin": "深圳", "destination": "武汉",
            "trip_type": "round_trip"} or value["travel_month"] != 9:
        raise DomainError("Flight CONDITION route/date 无效")
    signal = value["signal"]
    if (not isinstance(signal, dict)
            or set(signal) != {"kind", "criterion"}
            or signal["kind"] != "CONDITION"):
        raise DomainError("Flight CONDITION signal 无效")
    criterion = signal["criterion"]
    if (not isinstance(criterion, dict)
            or set(criterion) != {"metric", "operator", "value", "unit"}
            or criterion["metric"] != "round_trip_price"
            or criterion["operator"] != "lt"
            or criterion["unit"] != "CNY"):
        raise DomainError("Flight CONDITION criterion 无效")
    _strict_int(criterion["value"], "Flight threshold", 1, 1_000_000)
    provenance = value["provenance"]
    if (not isinstance(provenance, dict)
            or set(provenance) != {
                "subject", "route", "travel_month", "signal.criterion"}
            or any(item not in {"USER_EXPLICIT", "USER_CONFIRMED"}
                   for item in provenance.values())):
        raise DomainError("Flight CONDITION provenance 无效")
    return copy.deepcopy(value)


def normalize_openai_event_snapshot(value):
    expected = {
        "schema_version": 1,
        "subject": "OpenAI 新模型发布",
        "signal": {
            "kind": "EVENT",
            "criterion": {
                "entity": {
                    "kind": "ORGANIZATION", "key": "openai",
                    "name": "OpenAI",
                },
                "event_type": "MODEL_RELEASED",
                "constraints": {
                    "object_type": "MODEL",
                    "release_scope": "PUBLIC_AVAILABILITY",
                },
            },
        },
        "temporal_scope": {
            "mode": "FUTURE_FROM_ACTIVATION", "end_at": None,
        },
    }
    if not isinstance(value, dict) or set(value) != {
            *expected, "provenance"}:
        raise DomainError("OpenAI EVENT Tracking Definition schema 无效")
    for name, item in expected.items():
        if value[name] != item:
            raise DomainError("OpenAI EVENT criterion 不受支持")
    provenance = value["provenance"]
    if (not isinstance(provenance, dict)
            or set(provenance) != {
                "subject", "signal.criterion", "temporal_scope",
            }
            or provenance["subject"] not in {
                "USER_EXPLICIT", "USER_CONFIRMED",
            }
            or provenance["signal.criterion"] not in {
                "USER_EXPLICIT", "USER_CONFIRMED",
            }
            or provenance["temporal_scope"] != "PRODUCT_DEFAULT"):
        raise DomainError("OpenAI EVENT provenance 无效")
    return copy.deepcopy(value)


def select_tracking_workflow(candidate):
    """Select only an explicitly supported workflow; unknown signals fail closed."""
    if not isinstance(candidate, DefinitionCandidate):
        raise DomainError("invalid Definition candidate")
    combined = " ".join(filter(None, (
        candidate.topic, candidate.goal, candidate.trigger,
        *candidate.constraints, *candidate.locations,
    )))
    looks_flight = "机票" in combined or any(
        location in {"深圳", "武汉"} for location in candidate.locations
    )
    looks_event = (
        candidate.trigger is not None
        and re.search(r"(?:发布|出现|发生).*(?:提醒|告诉)|新模型", combined, re.I)
        is not None
    )
    if looks_flight:
        if candidate.schema_version != 2 or candidate.provenance is None:
            raise DomainError(
                "Flight CONDITION 必须来自 provenance-aware Definition",
            )
        if candidate.topic != "深圳往返武汉的机票优惠":
            raise DomainError("Flight CONDITION subject 不受支持")
        if candidate.locations != ("深圳", "武汉"):
            raise DomainError("Flight CONDITION route 不完整")
        if candidate.cadence not in FLIGHT_CADENCES:
            raise DomainError("Flight CONDITION cadence 不受支持")
        if (candidate.time_window is None
                or re.fullmatch(
                    r"\s*0?9\s*月\s*", candidate.time_window,
                ) is None):
            raise DomainError("Flight CONDITION date 不受支持")
        if len(candidate.constraints) != 1:
            raise DomainError("Flight CONDITION 只支持单一价格阈值")
        match = re.fullmatch(r"低于\s*(\d+)\s*元", candidate.constraints[0])
        if match is None:
            raise DomainError("Flight CONDITION 只支持低于金额")
        threshold = int(match.group(1))
        _strict_int(threshold, "Flight threshold", 1, 1_000_000)
        trigger = re.sub(r"\s+", "", candidate.trigger or "")
        if trigger != f"票价低于{threshold}元时提醒":
            raise DomainError("Flight CONDITION trigger 与阈值不一致")
        source_by_field = {
            "subject": candidate.provenance["topic"],
            "route": candidate.provenance["locations"],
            "travel_month": candidate.provenance["time_window"],
            "signal.criterion": candidate.provenance["constraints"],
        }
        if any(value not in {"USER_EXPLICIT", "USER_CONFIRMED"}
               for value in source_by_field.values()):
            raise DomainError("Flight CONDITION 不允许默认 intent")
        snapshot = normalize_flight_condition_snapshot({
            "schema_version": 1,
            "subject": candidate.topic,
            "route": {
                "origin": "深圳", "destination": "武汉",
                "trip_type": "round_trip",
            },
            "travel_month": 9,
            "signal": {
                "kind": "CONDITION",
                "criterion": {
                    "metric": "round_trip_price", "operator": "lt",
                    "value": threshold, "unit": "CNY",
                },
            },
            "provenance": source_by_field,
        })
        return "CONDITION", snapshot

    if looks_event:
        if candidate.schema_version != 2 or candidate.provenance is None:
            raise DomainError("EVENT 必须来自 provenance-aware Definition")
        if (candidate.topic != "OpenAI 新模型发布"
                or candidate.trigger != "出现新模型时提醒"
                or candidate.constraints or candidate.locations
                or candidate.time_window is not None):
            raise DomainError("EVENT Tracking Definition 不受支持")
        if candidate.cadence not in FLIGHT_CADENCES:
            raise DomainError("EVENT cadence 不受支持")
        if candidate.provenance["topic"] not in {
                "USER_EXPLICIT", "USER_CONFIRMED"} or candidate.provenance[
                    "trigger"] not in {
                        "USER_EXPLICIT", "USER_CONFIRMED"}:
            raise DomainError("EVENT 不允许默认 criterion")
        snapshot = normalize_openai_event_snapshot({
            "schema_version": 1,
            "subject": "OpenAI 新模型发布",
            "signal": {
                "kind": "EVENT",
                "criterion": {
                    "entity": {
                        "kind": "ORGANIZATION", "key": "openai",
                        "name": "OpenAI",
                    },
                    "event_type": "MODEL_RELEASED",
                    "constraints": {
                        "object_type": "MODEL",
                        "release_scope": "PUBLIC_AVAILABILITY",
                    },
                },
            },
            "temporal_scope": {
                "mode": "FUTURE_FROM_ACTIVATION", "end_at": None,
            },
            "provenance": {
                "subject": candidate.provenance["topic"],
                "signal.criterion": candidate.provenance["trigger"],
                "temporal_scope": "PRODUCT_DEFAULT",
            },
        })
        return "EVENT", snapshot

    # V1 is the legacy BRIEFING-only Definition schema. V2 is BRIEFING only
    # when it contains no reactive signal; scoped topics may still carry a
    # goal, time window, locations, or focus topics.
    explicit_briefing = (
        candidate.schema_version == 1
        or (candidate.schema_version == 2
            and not candidate.constraints and candidate.trigger is None)
    )
    if explicit_briefing:
        if candidate.cadence != "daily":
            raise DomainError("BRIEFING cadence 尚不受支持")
        return "BRIEFING", None

    if candidate.constraints or candidate.trigger is not None:
        raise DomainError("CONDITION workflow 尚不支持该 Tracking Definition")
    raise DomainError("Tracking Definition workflow 不受支持")


def tracking_definition_identity(snapshot):
    try:
        normalized = normalize_flight_condition_snapshot(snapshot)
    except DomainError:
        normalized = normalize_openai_event_snapshot(snapshot)
    return _canonical_identity(normalized)


def tracking_policy_identity(execution, presentation, distribution):
    return _canonical_identity({
        "execution": execution,
        "presentation": presentation,
        "distribution": distribution,
    })


def flight_price_signal_identity(subscription_id, quote):
    if not ID_PATTERN.fullmatch(str(subscription_id)):
        raise DomainError("flight signal subscription_id 无效")
    if not isinstance(quote, FlightPriceQuote):
        raise DomainError("invalid FlightPriceQuote")
    return _canonical_identity({
        "subscription_id": subscription_id,
        "source_signal_id": quote.source_signal_id,
        "quote": quote.as_dict(),
    })


def condition_evaluation_identity(subscription_id, definition_id,
                                  definition_version, observation_id):
    for value in (subscription_id, definition_id, observation_id):
        if not ID_PATTERN.fullmatch(str(value)):
            raise DomainError("Condition evaluation identity input 无效")
    _strict_int(definition_version, "definition_version", 1, 2**31 - 1)
    return _canonical_identity({
        "subscription_id": subscription_id,
        "definition_id": definition_id,
        "definition_version": definition_version,
        "observation_id": observation_id,
    })[:32]


def condition_update_identity(evaluation_id):
    if not ID_PATTERN.fullmatch(str(evaluation_id)):
        raise DomainError("evaluation_id 无效")
    return hashlib.sha256(
        f"condition-update\n{evaluation_id}".encode("utf-8"),
    ).hexdigest()[:32]


def normalize_event_model_name(value):
    value = _safe_protocol_text(value, "EVENT model name", 120)
    normalized = re.sub(r"[\s_\-]+", " ", value).strip().casefold()
    if not normalized or not re.fullmatch(r"[a-z0-9. ]{1,120}", normalized):
        raise DomainError("EVENT canonical model name 无效")
    return normalized


def event_observation_identity(entity_key, window_start_at, window_end_at,
                               retrieved_at, coverage_complete, truncated,
                               results, provider):
    if entity_key != "openai" or provider != "fake_event_search":
        raise DomainError("EVENT Observation identity input 无效")
    return _canonical_identity({
        "entity_key": entity_key,
        "window_start_at": utc_timestamp(
            _parse_utc_timestamp(window_start_at, "window_start_at")),
        "window_end_at": utc_timestamp(
            _parse_utc_timestamp(window_end_at, "window_end_at")),
        "retrieved_at": utc_timestamp(
            _parse_utc_timestamp(retrieved_at, "retrieved_at")),
        "coverage_complete": coverage_complete,
        "truncated": truncated,
        "provider": provider,
        "results": [item.as_dict() for item in results],
    })[:32]


def event_candidate_identity(observation_id, harness_run_id, entity_key,
                             event_type, object_type, display_name,
                             canonical_name_candidate, occurred_at_candidate,
                             support):
    for value in (observation_id, harness_run_id):
        if not ID_PATTERN.fullmatch(str(value)):
            raise DomainError("EVENT candidate identity input 无效")
    return _canonical_identity({
        "observation_id": observation_id,
        "harness_run_id": harness_run_id,
        "entity_key": entity_key,
        "event_type": event_type,
        "object_type": object_type,
        "display_name": display_name,
        "canonical_name_candidate": canonical_name_candidate,
        "occurred_at_candidate": occurred_at_candidate,
        "support": [
            {"source_ref": item.source_ref, "exact_span": item.exact_span}
            for item in support
        ],
    })[:32]


def event_verification_identity(subscription_id, definition_id,
                                definition_version, observation_id,
                                candidate_id, policy_version):
    for value in (subscription_id, definition_id, observation_id):
        if not ID_PATTERN.fullmatch(str(value)):
            raise DomainError("EVENT verification identity input 无效")
    _strict_int(definition_version, "definition_version", 1, 2**31 - 1)
    if candidate_id is not None and not ID_PATTERN.fullmatch(str(candidate_id)):
        raise DomainError("EVENT candidate identity input 无效")
    return _canonical_identity({
        "subscription_id": subscription_id,
        "definition_id": definition_id,
        "definition_version": definition_version,
        "observation_id": observation_id,
        "candidate_id": candidate_id,
        "policy_version": policy_version,
    })[:32]


def verified_event_identity(entity_key, event_type, object_type,
                            canonical_model_key):
    if (entity_key != "openai" or event_type != "MODEL_RELEASED"
            or object_type != "MODEL"):
        raise DomainError("Verified Event identity criterion 无效")
    canonical_model_key = normalize_event_model_name(canonical_model_key)
    return _canonical_identity({
        "entity_key": entity_key,
        "event_type": event_type,
        "object_type": object_type,
        "canonical_model_key": canonical_model_key,
    })


def event_update_identity(subscription_id, event_id):
    for value in (subscription_id, event_id):
        if not ID_PATTERN.fullmatch(str(value)):
            raise DomainError("EVENT Update identity input 无效")
    return _canonical_identity({
        "subscription_id": subscription_id, "event_id": event_id,
    })[:32]


def event_harness_run_identity(cycle_id):
    if not ID_PATTERN.fullmatch(str(cycle_id)):
        raise DomainError("EVENT cycle identity 无效")
    return _canonical_identity({"event_cycle_id": cycle_id})[:32]


def condition_cycle_identity(subscription_id, execution_policy_version,
                             scheduled_due_at, cycle_kind):
    if not ID_PATTERN.fullmatch(str(subscription_id)):
        raise DomainError("Condition cycle subscription_id 无效")
    _strict_int(execution_policy_version, "execution_policy_version", 1,
                2**31 - 1)
    if cycle_kind not in CONDITION_CYCLE_KINDS:
        raise DomainError("Condition cycle kind 无效")
    instant = _parse_utc_timestamp(scheduled_due_at, "scheduled_due_at")
    canonical = instant.isoformat().replace("+00:00", "Z")
    return _canonical_identity({
        "subscription_id": subscription_id,
        "execution_policy_version": execution_policy_version,
        "scheduled_due_at": canonical,
        "cycle_kind": cycle_kind,
    })[:32]


def event_cycle_identity(subscription_id, execution_policy_version,
                         scheduled_due_at, cycle_kind):
    if not ID_PATTERN.fullmatch(str(subscription_id)):
        raise DomainError("EVENT cycle subscription_id 无效")
    _strict_int(execution_policy_version, "execution_policy_version", 1,
                2**31 - 1)
    if cycle_kind not in EVENT_CYCLE_KINDS:
        raise DomainError("EVENT cycle kind 无效")
    instant = _parse_utc_timestamp(scheduled_due_at, "scheduled_due_at")
    return _canonical_identity({
        "subscription_id": subscription_id,
        "execution_policy_version": execution_policy_version,
        "scheduled_due_at": utc_timestamp(instant),
        "cycle_kind": cycle_kind,
    })[:32]


def utc_timestamp(value):
    """Canonical UTC serialization for deterministic temporal identities."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DomainError("timestamp 必须包含 timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_flight_travel_window(timestamp):
    """Resolve bare September to the current-or-next non-ended window."""
    now = _parse_utc_timestamp(timestamp, "activation timestamp")
    zone = ZoneInfo("Asia/Shanghai")
    local = now.astimezone(zone)
    year = local.year
    end = datetime(year, 10, 1, tzinfo=zone)
    if local >= end:
        year += 1
    start = datetime(year, 9, 1, tzinfo=zone)
    end = datetime(year, 10, 1, tzinfo=zone)
    return year, utc_timestamp(start), utc_timestamp(end)


def next_flight_due(timestamp, cadence_seconds):
    base = _parse_utc_timestamp(timestamp, "schedule anchor")
    if cadence_seconds not in FLIGHT_CADENCES.values():
        raise DomainError("cadence_seconds 无效")
    return utc_timestamp(base + timedelta(seconds=cadence_seconds))


def update_distribution_identity(update_id, user_subscription_id):
    for value in (update_id, user_subscription_id):
        if not ID_PATTERN.fullmatch(str(value)):
            raise DomainError("Distribution identity input 无效")
    return _canonical_identity({
        "update_id": update_id,
        "user_subscription_id": user_subscription_id,
    })[:32]


def definition_snapshot_identity(snapshot):
    version = 2 if isinstance(snapshot, dict) and "provenance" in snapshot else 1
    normalized, candidate = validate_definition_protocol({
        "protocol_version": version, "type": "DONE",
        "definition": copy.deepcopy(snapshot),
    })
    if candidate is None:
        raise DomainError("Definition snapshot 无效")
    return _canonical_identity(normalized["definition"])


def outbox_payload_identity(payload_refs):
    if not isinstance(payload_refs, dict):
        raise DomainError("Outbox payload refs 无效")
    return _canonical_identity(payload_refs)


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
        if self.cadence not in DEFINITION_CADENCES:
            raise DomainError("cadence 不在 allowlist")
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
    provider: str = "fake"
    query_identity: str | None = None
    result_count: int | None = None
    request_metadata: dict | None = None
    response_metadata: dict | None = None
    observation_identity: str | None = None

    def __post_init__(self):
        if not ID_PATTERN.fullmatch(str(self.observation_id)):
            raise DomainError("observation_id 无效")
        query = _text(self.query, "query", 1, 400)
        _text(self.observed_at, "observed_at", 1, 80)
        if not isinstance(self.results, tuple):
            raise DomainError("Search Observation results 必须是 tuple")
        results = tuple(copy.deepcopy(self.results))
        object.__setattr__(self, "results", results)
        if self.provider not in {"fake", "brave"}:
            raise DomainError("Search Observation provider 无效")
        query_identity = hashlib.sha256(query.encode("utf-8")).hexdigest()
        if self.query_identity is None:
            object.__setattr__(self, "query_identity", query_identity)
        elif self.query_identity != query_identity:
            raise DomainError("Search Observation query identity mismatch")
        if self.result_count is None:
            object.__setattr__(self, "result_count", len(results))
        elif self.result_count != len(results):
            raise DomainError("Search Observation result_count mismatch")
        request_metadata = copy.deepcopy(
            self.request_metadata or {"result_limit": len(self.results)},
        )
        response_metadata = copy.deepcopy(self.response_metadata or {
            "http_status": None, "response_bytes": 0,
            "retry_after_seconds": None,
        })
        if set(request_metadata) != {"result_limit"}:
            raise DomainError("Search Observation request metadata 无效")
        if set(response_metadata) != {
            "http_status", "response_bytes", "retry_after_seconds",
        }:
            raise DomainError("Search Observation response metadata 无效")
        object.__setattr__(self, "request_metadata", request_metadata)
        object.__setattr__(self, "response_metadata", response_metadata)
        stable = {
            "provider": self.provider,
            "query_identity": self.query_identity,
            "result_count": self.result_count,
            "request_metadata": request_metadata,
            "response_metadata": response_metadata,
            "results": list(results),
        }
        identity = _canonical_identity(stable)
        if self.observation_identity is None:
            object.__setattr__(self, "observation_identity", identity)
        elif self.observation_identity != identity:
            raise DomainError("Search Observation identity mismatch")


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
    digest_id: str | None
    channel: str
    title: str
    content: str
    distribution_id: str | None = None

    def __post_init__(self):
        for name in ("delivery_id", "attempt_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"delivery request {name} 无效")
        targets = (self.digest_id, self.distribution_id)
        if sum(value is not None for value in targets) != 1:
            raise DomainError("delivery request 必须绑定一个 target")
        for name in ("digest_id", "distribution_id"):
            value = getattr(self, name)
            if value is not None and not ID_PATTERN.fullmatch(str(value)):
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
    safe_observation: dict | None = None

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
        safe = copy.deepcopy(self.safe_observation or {})
        if (not isinstance(safe, dict)
                or (self.status == "accepted" and safe != {
                    "notification_requested": True,
                    "request_accepted": True,
                })
                or (self.status != "accepted" and safe)):
            raise DomainError("delivery safe observation 无效")
        object.__setattr__(self, "safe_observation", safe)
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
    digest_id: str | None
    user_id: str
    channel: str
    status: str
    attempt_number: int
    provider_message_id: str | None
    requested_at: str
    completed_at: str | None
    error_code: str | None
    effect_certainty: str
    distribution_id: str | None = None
    evidence_id: str | None = None

    def __post_init__(self):
        for name in ("delivery_id", "attempt_id", "user_id"):
            if not ID_PATTERN.fullmatch(str(getattr(self, name))):
                raise DomainError(f"delivery {name} 无效")
        if sum(value is not None for value in (
                self.digest_id, self.distribution_id)) != 1:
            raise DomainError("delivery 必须绑定一个 target")
        for name in ("digest_id", "distribution_id", "evidence_id"):
            value = getattr(self, name)
            if value is not None and not ID_PATTERN.fullmatch(str(value)):
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
        if (self.distribution_id is not None and self.status == "accepted"
                and self.evidence_id is None):
            raise DomainError("accepted Distribution notification 缺 Evidence")


def delivery_identity(digest_id, channel):
    if not ID_PATTERN.fullmatch(str(digest_id)):
        raise DomainError("delivery digest_id 无效")
    if channel not in DELIVERY_REQUEST_CHANNELS:
        raise DomainError("delivery channel 不在 allowlist")
    return _canonical_identity({"digest_id": digest_id, "channel": channel})[:32]


def distribution_notification_identity(distribution_id, channel):
    """Stable logical Notification identity; attempts have separate ids."""
    if not ID_PATTERN.fullmatch(str(distribution_id)):
        raise DomainError("notification distribution_id 无效")
    if channel != "termux_notification":
        raise DomainError("Distribution notification channel 不受支持")
    return _canonical_identity({
        "distribution_id": distribution_id, "channel": channel,
    })[:32]


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


def safe_condition_update_preview(update, maximum=240):
    """Create user copy from immutable Update content, without internal ids."""
    _strict_int(maximum, "notification preview maximum", 80, 500)
    if not isinstance(update, TrackingUpdate):
        raise DomainError("invalid Tracking Update")
    text = " ".join(str(update.payload.get("summary", "")).split())
    if not text:
        raise DomainError("Tracking Update notification content 缺失")
    return text[:maximum].rstrip()


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
    failure_stage: str | None = None
    failure_code: str | None = None
    failure_subtype: str | None = None
    failure_diagnostics: dict | None = None


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


def _topic_matches_candidate(topic, combined_text):
    """Conservatively derive a topic tag from bounded candidate text."""
    normalized_topic = normalize_topic(topic)
    normalized_text = " ".join(combined_text.casefold().split())
    if normalized_topic in normalized_text:
        return True
    topic_tokens = {
        token for token in re.findall(r"[^\W_]+", normalized_topic)
        if token not in TOPIC_LEXICAL_STOP_WORDS
    }
    if len(topic_tokens) < 2:
        return False
    text_tokens = set(re.findall(r"[^\W_]+", normalized_text))
    required = max(2, (len(topic_tokens) * 2 + 4) // 5)
    return len(topic_tokens & text_tokens) >= required


def normalize_candidates(observation, evidence_id, relevant_topics=()):
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
            tags = list(dict.fromkeys(
                normalize_topic(item) for item in raw.get("topic_tags", ())
            ))
        except (DomainError, TypeError):
            continue
        combined = f"{title} {snippet}".casefold()
        for topic in relevant_topics:
            try:
                normalized_topic = normalize_topic(topic)
            except (DomainError, TypeError):
                continue
            if (_topic_matches_candidate(normalized_topic, combined)
                    and normalized_topic not in tags):
                tags.append(normalized_topic)
        source_id = hashlib.sha256(url.encode("utf-8")).hexdigest()
        if raw.get("source_id") not in {None, source_id}:
            continue
        stable = f"{url}\n{title.casefold()}".encode("utf-8")
        identity = hashlib.sha256(stable).hexdigest()
        candidates.append(ContentCandidate(
            candidate_id=identity[:32], canonical_url=url, title=title,
            snippet=snippet, published_at=published_at,
            retrieved_at=observation.observed_at, source_domain=urlsplit(url).hostname or "",
            topic_tags=tuple(tags),
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
