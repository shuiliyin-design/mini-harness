"""Application-owned adapters for the Subscription Definition Protocol."""

import copy
import hashlib
import json
import re

from mini_harness_core.security import SECRET_PATTERNS

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
    "delivery_preference",
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
    def _default(context):
        user_messages = [
            item["content"] for item in context["messages"]
            if item["role"] == "user"
        ]
        combined = "。".join(user_messages)
        if any(word in combined for word in ("不要订阅", "取消请求", "拒绝")):
            return {
                "protocol_version": 1, "type": "REJECT",
                "reason": "该请求已按你的表达停止定义。",
            }
        if len(user_messages) == 1 and not re.search(r"\d+\s*字", combined):
            return {
                "protocol_version": 1, "type": "NEXT_QUESTION",
                "question": "每篇资讯希望控制在多少字以内？",
            }
        topic_match = re.search(
            r"订阅\s*(.+?)(?=[，,。；;]|\d+\s*字|重点关注|$)", combined,
        )
        topic = topic_match.group(1).strip() if topic_match else "AI 行业动态"
        chars = re.search(r"(\d+)\s*字", combined)
        items = re.search(r"(?:最多|不超过)\s*(\d+)\s*(?:条|项|篇)", combined)
        focus_match = re.search(r"重点关注\s*(.+?)(?:[。；;]|$)", combined)
        focus = []
        if focus_match:
            focus = [
                item.strip() for item in re.split(
                    r"[、,，]|和|及", focus_match.group(1),
                ) if item.strip()
            ]
        return {
            "protocol_version": 1, "type": "DONE",
            "definition": {
                "topic": topic, "language": "zh-CN", "cadence": "daily",
                "max_chars": int(chars.group(1)) if chars else 600,
                "max_items": int(items.group(1)) if items else 5,
                "focus_topics": list(dict.fromkeys(focus)),
                "delivery_preference": "none",
            },
        }

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
            "{definition}, whose object has topic/language/cadence/max_chars/"
            "max_items/focus_topics/delivery_preference. max_chars and max_items "
            "are JSON integers; focus_topics is a JSON string array. language is "
            "exactly zh-CN or en; cadence is exactly daily; "
            "max_chars is 100..4000; max_items is 1..10; delivery_preference "
            "is exactly none or termux_notification. Do not translate these "
            "enum values. DONE is allowed only when the conversation itself "
            "supplies topic, language, cadence, max_chars, max_items, focus "
            "topics (which may explicitly be empty), and delivery preference. "
            "Never invent defaults or infer omitted choices; ask exactly one "
            "NEXT_QUESTION for missing information. question and reason are "
            "single-line plain text of at most 500 characters with no control "
            "characters. Do not create IDs, "
            "subscriptions, jobs, or claim activation."
            if native else
            "Return one strict JSON object for protocol_version 1. It must be "
            "exactly NEXT_QUESTION {protocol_version,type,question}, REJECT "
            "{protocol_version,type,reason}, or DONE {protocol_version,type,definition}. "
            "DONE definition has exactly topic, language, cadence, max_chars, "
            "max_items, focus_topics, delivery_preference. No prose or fences."
        )
        return (
            "[SYSTEM]\nYou propose one Subscription Definition candidate. "
            "The candidate is not product truth. Ask one safe clarification when "
            "required; reject only unsupported subscription requests; otherwise "
            "propose a complete definition. " + wire + "\n\n[CONVERSATION]\n"
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
                "protocol_version": 1, "type": outcome_type,
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
                "protocol_version": 1, "type": outcome_type,
                "reason": payload["reason"],
            }
        if set(payload) != {"definition"} or not isinstance(
                payload["definition"], dict):
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
