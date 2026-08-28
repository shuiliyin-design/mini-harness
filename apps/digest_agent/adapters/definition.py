"""Application-owned adapters for the Subscription Definition Protocol."""

import copy
import hashlib
import json
import re

from mini_harness_core.security import SECRET_PATTERNS

from ..domain import DomainError, normalize_conversation_envelope
from .provider import (
    MAX_RESPONSE_BYTES, DEFAULT_TIMEOUT_SECONDS, ProviderAdapterError,
    VertexDigestProvider,
)


DEFINITION_TOOL_NAME = "submit_subscription_definition_candidate"
DEFINITION_WIRE_FIELDS = ("type", "payload_json")
DEFINITION_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": [
            "NEXT_QUESTION", "REJECT", "DONE",
        ]},
        "payload_json": {"type": "string"},
    },
    "required": list(DEFINITION_WIRE_FIELDS),
    "additionalProperties": False,
}
DEFINITION_TOOL_SCHEMA_IDENTITY = hashlib.sha256(
    json.dumps(
        DEFINITION_TOOL_SCHEMA, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8"),
).hexdigest()
DEFINITION_SCHEMA_MISMATCH_RULES = frozenset({
    "TOP_LEVEL_SHAPE", "FIELD_TYPE", "VARIANT_TYPE", "VARIANT_FIELDS",
    "INTEGER_FORMAT", "FOCUS_JSON", "FOCUS_TYPE", "PAYLOAD_JSON",
})
DEFINITION_SCHEMA_FIELDS = frozenset({
    *DEFINITION_WIRE_FIELDS, "question", "reason", "definition", "topic",
    "language", "cadence", "max_chars", "max_items", "focus_topics",
    "delivery_preference", "intent", "constraints", "goal", "trigger",
    "time_window", "locations", "preferences", "value", "source_turn",
})


def _canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _safe_context(value):
    if (not isinstance(value, dict)
            or set(value) != {"conversation_id", "turn_count", "messages"}
            or not isinstance(value["conversation_id"], str)
            or type(value["turn_count"]) is not int
            or not isinstance(value["messages"], list)):
        raise ProviderAdapterError("INVALID_RESPONSE")
    messages = []
    for item in value["messages"]:
        if (not isinstance(item, dict)
                or set(item) != {"role", "content"}
                or item["role"] not in {"user", "agent"}
                or not isinstance(item["content"], str)
                or not item["content"].strip()
                or len(item["content"]) > 2_000):
            raise ProviderAdapterError("INVALID_RESPONSE")
        messages.append({
            "role": item["role"], "content": item["content"].strip(),
        })
    if not messages or messages[-1]["role"] != "user":
        raise ProviderAdapterError("INVALID_RESPONSE")
    safe = {
        "conversation_id": value["conversation_id"],
        "turn_count": value["turn_count"], "messages": messages,
    }
    encoded = _canonical_bytes(safe).decode("utf-8")
    if any(pattern.search(encoded) for pattern in SECRET_PATTERNS):
        raise ProviderAdapterError("INVALID_RESPONSE")
    return safe


class FakeDefinitionAgentAdapter:
    """Scriptable correctness adapter; outputs candidates, never commits them."""

    provider_identity = "fake"

    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes) if outcomes is not None else None
        self.calls = []

    @staticmethod
    def _source(value, turn):
        return {"value": value, "source_turn": turn}

    @staticmethod
    def _source_turn(user_messages, pattern):
        for number, message in enumerate(user_messages, 1):
            if re.search(pattern, message, re.IGNORECASE):
                return number
        return 1

    @classmethod
    def _intent_done(cls, *, topic, topic_turn=1, constraints=(), goal=None,
                     trigger=None, time_window=None, locations=(),
                     focus_topics=(), preferences=None):
        def sourced(item):
            value, turn = item
            return cls._source(value, turn)

        return {
            "protocol_version": 2, "type": "DONE",
            "intent": {
                "topic": cls._source(topic, topic_turn),
                "constraints": [sourced(item) for item in constraints],
                "goal": sourced(goal) if goal is not None else None,
                "trigger": sourced(trigger) if trigger is not None else None,
                "time_window": (
                    sourced(time_window) if time_window is not None else None
                ),
                "locations": [sourced(item) for item in locations],
                "focus_topics": [sourced(item) for item in focus_topics],
                "preferences": {
                    name: sourced(item)
                    for name, item in (preferences or {}).items()
                },
            },
        }

    @classmethod
    def _default(cls, context):
        user_messages = [
            item["content"] for item in context["messages"]
            if item["role"] == "user"
        ]
        combined = "。".join(user_messages)
        if any(word in combined for word in ("不要订阅", "取消请求", "拒绝")):
            return {
                "protocol_version": 2, "type": "REJECT",
                "reason": "该请求已按你的表达停止定义。",
            }
        is_flight = (
            "机票" in combined
            and re.search(r"深圳.*武汉|武汉.*深圳", combined) is not None
        )
        time_match = re.search(
            r"(\d{1,2}\s*月(?:\s*\d{1,2}\s*(?:日|号)"
            r"(?:\s*[至到-]\s*\d{1,2}\s*(?:日|号))?)?)",
            combined,
        )
        price_match = re.search(
            r"((?:低于|不超过|少于)\s*\d+\s*元)", combined,
        )
        discount_condition = re.search(
            r"(明显降价|价格下降|出现优惠|有优惠)", combined,
        )
        if is_flight and time_match is None:
            return {
                "protocol_version": 2, "type": "NEXT_QUESTION",
                "question": "你计划哪段日期出发和返回？日期会直接影响可比较的票价。",
            }
        if (is_flight and price_match is None
                and discount_condition is None
                and not re.search(r"不设(?:价格)?阈值|不用价格阈值", combined)):
            return {
                "protocol_version": 2, "type": "NEXT_QUESTION",
                "question": "什么价格算优惠：低于某个金额，还是出现明显降价时提醒你？",
            }
        is_openai_event = (
            "openai" in combined.casefold() and "新模型" in combined
            and "提醒" in combined
        )
        is_agent_topic = "ai agent" in combined.casefold()
        focus_match = re.search(r"重点关注\s*(.+?)(?:[。；;]|$)", combined)
        if (is_agent_topic and not is_openai_event
                and len(user_messages) == 1 and focus_match is None):
            return {
                "protocol_version": 2, "type": "NEXT_QUESTION",
                "question": "你更关心产品发布、技术进展，还是行业应用？",
            }
        first = user_messages[0]
        topic = re.sub(
            r"^(?:请)?(?:帮我)?(?:持续)?(?:关注|订阅)", "", first,
        ).strip(" ，,。；;")
        topic = re.split(r"[，,。；;]", topic, maxsplit=1)[0].strip()
        if not topic:
            return {
                "protocol_version": 2, "type": "REJECT",
                "reason": "我还没理解要持续关注的对象，请换一种说法描述。",
            }
        chars = re.search(r"(\d+)\s*字", combined)
        items = re.search(r"(?:最多|不超过)\s*(\d+)\s*(?:条|项|篇)", combined)
        focus = []
        if focus_match:
            focus = [
                item.strip() for item in re.split(
                    r"[、,，]|和|及", focus_match.group(1),
                ) if item.strip()
            ]
        elif is_agent_topic and len(user_messages) > 1:
            focus = [
                re.sub(r"^(?:我)?(?:更)?关心", "", item).strip(" 。")
                for item in re.split(
                    r"[、,，]|和|及|还是", user_messages[-1],
                ) if item.strip(" 。")
            ][:3]
        preferences = {}
        for name, match, cast, pattern in (
            ("max_chars", chars, int, r"\d+\s*字"),
            ("max_items", items, int,
             r"(?:最多|不超过)\s*\d+\s*(?:条|项|篇)"),
        ):
            if match:
                preferences[name] = (
                    cast(match.group(1)), cls._source_turn(
                        user_messages, pattern,
                    ),
                )
        if re.search(r"每天|每日", combined):
            preferences["cadence"] = (
                "daily", cls._source_turn(user_messages, r"每天|每日"),
            )
        if "英文" in combined:
            preferences["language"] = (
                "en", cls._source_turn(user_messages, "英文"),
            )
        elif "中文" in combined:
            preferences["language"] = (
                "zh-CN", cls._source_turn(user_messages, "中文"),
            )
        if "本机通知" in combined:
            preferences["delivery_preference"] = (
                "termux_notification",
                cls._source_turn(user_messages, "本机通知"),
            )
        if is_flight:
            topic = "深圳往返武汉的机票优惠"
        elif is_openai_event:
            topic = "OpenAI 新模型发布"
        constraints = []
        if price_match:
            constraints.append((
                price_match.group(1).replace(" ", ""),
                cls._source_turn(
                    user_messages, r"(?:低于|不超过|少于)\s*\d+\s*元",
                ),
            ))
        elif discount_condition:
            constraints.append((
                discount_condition.group(1),
                cls._source_turn(
                    user_messages, r"明显降价|价格下降|出现优惠|有优惠",
                ),
            ))
        time_window = None
        if time_match:
            time_window = (
                time_match.group(1).strip(),
                cls._source_turn(user_messages, r"\d{1,2}\s*月"),
            )
        trigger = None
        if "提醒" in combined:
            trigger_text = (
                f"票价{price_match.group(1).replace(' ', '')}时提醒"
                if is_flight and price_match else
                f"{discount_condition.group(1)}时提醒"
                if is_flight and discount_condition else
                "出现新模型时提醒" if is_openai_event else "有重要变化时提醒"
            )
            trigger = (
                trigger_text, cls._source_turn(user_messages, "提醒"),
            )
        return cls._intent_done(
            topic=topic,
            constraints=constraints,
            goal=(("寻找深圳往返武汉的机票优惠", 1) if is_flight else None),
            trigger=trigger, time_window=time_window,
            locations=([("深圳", 1), ("武汉", 1)] if is_flight else []),
            focus_topics=[
                (item, 1 if focus_match else len(user_messages))
                for item in dict.fromkeys(focus)
            ],
            preferences=preferences,
        )

    def propose(self, context):
        safe = _safe_context(context)
        self.calls.append({
            "conversation_id": safe["conversation_id"],
            "turn_count": safe["turn_count"],
        })
        if self.outcomes is None:
            candidate = self._default(safe)
        else:
            if not self.outcomes:
                raise ProviderAdapterError("EMPTY_OUTPUT")
            candidate = self.outcomes.pop(0)
        return copy.deepcopy(candidate)


class VertexDefinitionAgentAdapter:
    """Vertex-backed Definition candidate adapter with a strict app contract."""

    provider_identity = "vertex"

    def __init__(self, *, transport=None, environ=None,
                 timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                 maximum_response_bytes=MAX_RESPONSE_BYTES, monotonic=None):
        self.base = VertexDigestProvider(
            transport=transport, environ=environ,
            timeout_seconds=timeout_seconds,
            maximum_response_bytes=maximum_response_bytes,
            monotonic=monotonic,
        )
        self.transport = self.base.transport
        self.timeout_seconds = self.base.timeout_seconds
        self.maximum_response_bytes = self.base.maximum_response_bytes
        self.calls = []
        self.last_attempt = None
        self.model_identity = self.base.model_identity

    @classmethod
    def from_environment(cls, **kwargs):
        return cls(**kwargs)

    @staticmethod
    def _prompt(context, native):
        wire = (
            "Use the required tool with exactly two scalar fields: type and "
            "payload_json. type is NEXT_QUESTION, REJECT, or DONE. payload_json "
            "is a JSON object encoded as a string: NEXT_QUESTION uses exactly "
            "{question}; REJECT uses exactly {reason}; DONE uses exactly "
            "{intent}. intent has exactly topic, constraints, goal, trigger, "
            "time_window, locations, focus_topics, preferences. topic is one "
            "{value,source_turn}; goal, trigger, and time_window are that shape "
            "or null; constraints, locations, and focus_topics are arrays of "
            "that shape. preferences contains only preferences actually stated "
            "by the user: language, cadence, max_chars, max_items, or "
            "delivery_preference, each as {value,source_turn}. source_turn is "
            "the 1-based USER message number supporting the value. Never put "
            "a product default in preferences. The application supplies omitted "
            "product and execution defaults after DONE. Ask exactly one "
            "NEXT_QUESTION only when not knowing the answer would materially "
            "change what to track, when to react, or whether the user's goal is "
            "met. Do not ask for output length, item count, language, cadence, "
            "or delivery configuration merely to complete a schema. If intent "
            "is sufficient, use DONE. A flight-fare request with a route but no "
            "travel date or time window has material ambiguity: ask for the "
            "travel window first. A flight request with route, travel window, "
            "price threshold, and alert trigger is sufficient. A named product "
            "event with an explicit new-event trigger is also sufficient. "
            "A month such as '9 月' is a valid travel window; do not require "
            "exact dates when the user already supplied a month plus a price "
            "threshold and alert trigger. In particular, '关注深圳到武汉 9 月"
            "往返机票，低于 800 元提醒我' must be DONE, not a question. "
            "Ask in the user's language without "
            "mentioning schema, fields, configuration, or defaults. "
            "question and reason are "
            "single-line plain text of at most 500 characters with no control "
            "characters. Do not create IDs, "
            "subscriptions, jobs, or claim activation."
            if native else
            "Return one strict JSON conversation candidate. No prose or fences."
        )
        return (
            "[SYSTEM]\nYou propose one Subscription Definition candidate. "
            "The candidate is not product truth. Ask one safe clarification when "
            "required; reject only unsupported tracking requests; otherwise "
            "propose a sufficient intent candidate. Conversation schema is not "
            "the durable Definition schema. " + wire + "\n\n[CONVERSATION]\n"
            + _canonical_bytes(context).decode("utf-8")
        )

    def describe_attempt(self, context):
        safe = _safe_context(context)
        prompt = self._prompt(safe, True)
        return self.base.describe_structured_protocol(
            prompt, tool_name=DEFINITION_TOOL_NAME,
            tool_description="Submit one Subscription Definition candidate",
            tool_schema=DEFINITION_TOOL_SCHEMA,
            schema_identity=DEFINITION_TOOL_SCHEMA_IDENTITY,
        )

    @staticmethod
    def _schema_failure(metadata, rule, field=None):
        metadata["schema_mismatch_rule"] = rule
        if field is not None:
            metadata["schema_mismatch_field"] = field
        raise ProviderAdapterError(
            "INVALID_RESPONSE", subtype="SCHEMA_MISMATCH",
        )

    @staticmethod
    def _flat_candidate(value, metadata=None):
        metadata = metadata if isinstance(metadata, dict) else {}
        if not isinstance(value, dict) or set(value) != set(DEFINITION_WIRE_FIELDS):
            VertexDefinitionAgentAdapter._schema_failure(
                metadata, "TOP_LEVEL_SHAPE",
            )
        for name in DEFINITION_WIRE_FIELDS:
            if not isinstance(value[name], str):
                VertexDefinitionAgentAdapter._schema_failure(
                    metadata, "FIELD_TYPE", name,
                )
        outcome_type = value["type"]
        if outcome_type not in {"NEXT_QUESTION", "REJECT", "DONE"}:
            VertexDefinitionAgentAdapter._schema_failure(
                metadata, "VARIANT_TYPE", "type",
            )
        try:
            payload = json.loads(value["payload_json"])
        except json.JSONDecodeError:
            VertexDefinitionAgentAdapter._schema_failure(
                metadata, "PAYLOAD_JSON", "payload_json",
            )
        if not isinstance(payload, dict):
            VertexDefinitionAgentAdapter._schema_failure(
                metadata, "TOP_LEVEL_SHAPE", "payload_json",
            )
        if outcome_type == "NEXT_QUESTION":
            if (set(payload) != {"question"}
                    or not isinstance(payload["question"], str)
                    or not payload["question"].strip()):
                VertexDefinitionAgentAdapter._schema_failure(
                    metadata, "VARIANT_FIELDS", "payload_json",
                )
            return {
                "protocol_version": 2, "type": outcome_type,
                "question": payload["question"],
            }
        if outcome_type == "REJECT":
            if (set(payload) != {"reason"}
                    or not isinstance(payload["reason"], str)
                    or not payload["reason"].strip()):
                VertexDefinitionAgentAdapter._schema_failure(
                    metadata, "VARIANT_FIELDS", "payload_json",
                )
            return {
                "protocol_version": 2, "type": outcome_type,
                "reason": payload["reason"],
            }
        if set(payload) == {"intent"} and isinstance(payload["intent"], dict):
            candidate = {
                "protocol_version": 2, "type": outcome_type,
                "intent": payload["intent"],
            }
            try:
                return normalize_conversation_envelope(candidate)
            except (DomainError, TypeError, ValueError):
                VertexDefinitionAgentAdapter._schema_failure(
                    metadata, "VARIANT_FIELDS", "payload_json",
                )
        if set(payload) != {"definition"} or not isinstance(
                payload.get("definition"), dict):
            VertexDefinitionAgentAdapter._schema_failure(
                metadata, "VARIANT_FIELDS", "payload_json",
            )
        definition = payload["definition"]
        fields = {
            "topic", "language", "cadence", "max_chars", "max_items",
            "focus_topics", "delivery_preference",
        }
        if set(definition) != fields:
            VertexDefinitionAgentAdapter._schema_failure(
                metadata, "TOP_LEVEL_SHAPE", "payload_json",
            )
        if (not isinstance(definition["max_chars"], int)
                or isinstance(definition["max_chars"], bool)
                or not isinstance(definition["max_items"], int)
                or isinstance(definition["max_items"], bool)):
            VertexDefinitionAgentAdapter._schema_failure(
                metadata, "INTEGER_FORMAT", "max_chars",
            )
        focus = definition["focus_topics"]
        if not isinstance(focus, list) or not all(
                isinstance(item, str) for item in focus):
            VertexDefinitionAgentAdapter._schema_failure(
                metadata, "FOCUS_TYPE", "payload_json",
            )
        candidate = {
            "protocol_version": 1, "type": outcome_type,
            "definition": {
                "topic": definition["topic"],
                "language": definition["language"],
                "cadence": definition["cadence"],
                "max_chars": definition["max_chars"],
                "max_items": definition["max_items"],
                "focus_topics": focus,
                "delivery_preference": definition["delivery_preference"],
            },
        }
        return candidate

    def propose(self, context):
        safe = _safe_context(context)
        prompt = self._prompt(safe, True)
        metadata = self.describe_attempt(safe)
        self.calls.append({
            "conversation_id": safe["conversation_id"],
            "turn_count": safe["turn_count"],
            "request_identity": metadata["request_sha256"],
            "model_identity": metadata["model_identity"],
            "api_mode": metadata["api_mode"],
        })
        try:
            return self.base.request_structured_protocol(
                prompt, tool_name=DEFINITION_TOOL_NAME,
                tool_description="Submit one Subscription Definition candidate",
                tool_schema=DEFINITION_TOOL_SCHEMA,
                schema_identity=DEFINITION_TOOL_SCHEMA_IDENTITY,
                parser=self._flat_candidate,
            )
        finally:
            self.last_attempt = self.base.last_attempt
            self.model_identity = self.base.last_model_identity
