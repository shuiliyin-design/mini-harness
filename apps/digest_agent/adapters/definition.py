"""Application-owned adapters for the Subscription Definition Protocol."""

import copy
import hashlib
import json
import re
import time

from mini_harness_core.security import SECRET_PATTERNS

from ..domain import DomainError, normalize_definition_envelope
from .provider import (
    MAX_RESPONSE_BYTES, DEFAULT_TIMEOUT_SECONDS,
    REFUSAL_FINISH_REASONS, ProviderAdapterError, VertexDigestProvider,
    VertexHTTPResponse,
)


DEFINITION_TOOL_NAME = "submit_subscription_definition_candidate"
DEFINITION_WIRE_FIELDS = (
    "type", "question", "reason", "topic", "language", "cadence",
    "max_chars", "max_items", "focus_topics_json", "delivery_preference",
)
DEFINITION_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        name: {"type": "string"} for name in DEFINITION_WIRE_FIELDS
    },
    "required": list(DEFINITION_WIRE_FIELDS),
    "additionalProperties": False,
}


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
        self.monotonic = monotonic or time.monotonic
        self.calls = []
        self.last_attempt = None
        self.model_identity = self.base.model_identity

    @classmethod
    def from_environment(cls, **kwargs):
        return cls(**kwargs)

    @staticmethod
    def _prompt(context, native):
        wire = (
            "Use the required tool. Set every declared field. type is exactly "
            "NEXT_QUESTION, REJECT, or DONE. For NEXT_QUESTION set only question "
            "non-empty and every definition/reason field to an empty string. For "
            "REJECT set only reason non-empty. For DONE set question and reason to "
            "empty strings; set topic/language/cadence/max_chars/max_items/"
            "focus_topics_json/delivery_preference. Integer fields are canonical "
            "decimal strings. focus_topics_json is a JSON array encoded as a "
            "string. Do not create IDs, subscriptions, jobs, or claim activation."
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

    @staticmethod
    def _request_body(model, mode, prompt):
        if mode == "completions":
            return {
                "model": model,
                "prompt": prompt + "\n\n[ASSISTANT]\n{",
                "temperature": 0, "max_tokens": 1_024,
            }
        return {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0, "max_tokens": 1_024,
            "tools": [{
                "type": "function",
                "function": {
                    "name": DEFINITION_TOOL_NAME,
                    "description": "Submit one Subscription Definition candidate",
                    "strict": True, "parameters": DEFINITION_TOOL_SCHEMA,
                },
            }],
            "tool_choice": {
                "type": "function", "function": {"name": DEFINITION_TOOL_NAME},
            },
        }

    @staticmethod
    def _flat_candidate(value):
        if not isinstance(value, dict) or set(value) != set(DEFINITION_WIRE_FIELDS):
            raise ProviderAdapterError("INVALID_RESPONSE")
        if not all(isinstance(value[name], str) for name in DEFINITION_WIRE_FIELDS):
            raise ProviderAdapterError("INVALID_RESPONSE")
        outcome_type = value["type"]
        if outcome_type == "NEXT_QUESTION":
            if (not value["question"].strip()
                    or any(value[name] for name in DEFINITION_WIRE_FIELDS
                           if name not in {"type", "question"})):
                raise ProviderAdapterError("INVALID_RESPONSE")
            return {
                "protocol_version": 1, "type": outcome_type,
                "question": value["question"],
            }
        if outcome_type == "REJECT":
            if (not value["reason"].strip()
                    or any(value[name] for name in DEFINITION_WIRE_FIELDS
                           if name not in {"type", "reason"})):
                raise ProviderAdapterError("INVALID_RESPONSE")
            return {
                "protocol_version": 1, "type": outcome_type,
                "reason": value["reason"],
            }
        if outcome_type != "DONE" or value["question"] or value["reason"]:
            raise ProviderAdapterError("INVALID_RESPONSE")
        if (re.fullmatch(r"(?:0|[1-9][0-9]*)", value["max_chars"]) is None
                or re.fullmatch(r"(?:0|[1-9][0-9]*)",
                                value["max_items"]) is None):
            raise ProviderAdapterError("INVALID_RESPONSE")
        try:
            focus = json.loads(value["focus_topics_json"])
        except json.JSONDecodeError as error:
            raise ProviderAdapterError("INVALID_RESPONSE") from error
        candidate = {
            "protocol_version": 1, "type": outcome_type,
            "definition": {
                "topic": value["topic"], "language": value["language"],
                "cadence": value["cadence"],
                "max_chars": int(value["max_chars"]),
                "max_items": int(value["max_items"]),
                "focus_topics": focus,
                "delivery_preference": value["delivery_preference"],
            },
        }
        return candidate

    @staticmethod
    def _extract(response_body, mode):
        choices = response_body.get("choices") if isinstance(
            response_body, dict,
        ) else None
        if not isinstance(choices, list) or len(choices) != 1:
            raise ProviderAdapterError("INVALID_RESPONSE")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ProviderAdapterError("INVALID_RESPONSE")
        finish_reason = str(choice.get("finish_reason") or "").casefold()
        if finish_reason in REFUSAL_FINISH_REASONS:
            raise ProviderAdapterError("MODEL_REFUSAL")
        if mode == "completions":
            content = choice.get("text")
            if not isinstance(content, str) or not content.strip():
                raise ProviderAdapterError("EMPTY_OUTPUT")
            content = content.strip()
            if not content.startswith("{"):
                content = "{" + content
            try:
                return json.loads(content)
            except json.JSONDecodeError as error:
                raise ProviderAdapterError("INVALID_RESPONSE") from error
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ProviderAdapterError("INVALID_RESPONSE")
        if message.get("content") not in {None, ""}:
            raise ProviderAdapterError("INVALID_RESPONSE")
        calls = message.get("tool_calls")
        if finish_reason != "tool_calls" or not isinstance(calls, list) or len(calls) != 1:
            raise ProviderAdapterError("INVALID_RESPONSE")
        call = calls[0]
        if not isinstance(call, dict):
            raise ProviderAdapterError("INVALID_RESPONSE")
        function = call.get("function")
        if (call.get("type") != "function" or not isinstance(function, dict)
                or function.get("name") != DEFINITION_TOOL_NAME
                or not isinstance(function.get("arguments"), str)):
            raise ProviderAdapterError("INVALID_RESPONSE")
        try:
            return VertexDefinitionAgentAdapter._flat_candidate(
                json.loads(function["arguments"]),
            )
        except json.JSONDecodeError as error:
            raise ProviderAdapterError("INVALID_RESPONSE") from error

    def propose(self, context):
        safe = _safe_context(context)
        key, endpoint, model, mode = self.base._configuration()
        prompt = self._prompt(safe, mode == "chat-completions")
        request_body = self._request_body(model, mode, prompt)
        encoded = _canonical_bytes(request_body)
        self.calls.append({
            "conversation_id": safe["conversation_id"],
            "turn_count": safe["turn_count"],
            "request_identity": hashlib.sha256(encoded).hexdigest(),
            "model_identity": model, "api_mode": mode,
        })
        self.last_attempt = {
            "request_sha256": hashlib.sha256(encoded).hexdigest(),
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "api_mode": mode, "model_identity": model,
        }
        started = self.monotonic()
        try:
            response = self.transport.post(
                endpoint,
                {"Content-Type": "application/json", "Accept": "application/json",
                 "Authorization": f"Bearer {key}"},
                encoded, self.timeout_seconds, self.maximum_response_bytes,
            )
            if (not isinstance(response, VertexHTTPResponse)
                    or type(response.status) is not int
                    or not hasattr(response.headers, "get")
                    or not isinstance(response.body, bytes)):
                raise ProviderAdapterError("INVALID_RESPONSE")
            self.last_attempt.update({
                "http_status": response.status,
                "response_bytes": len(response.body),
                "response_sha256": hashlib.sha256(response.body).hexdigest(),
            })
            if response.status in {401, 403}:
                raise ProviderAdapterError("AUTH_FAILED")
            if response.status == 429:
                raise ProviderAdapterError(
                    "RATE_LIMITED",
                    retry_after_seconds=self.base._retry_after(response.headers),
                )
            if response.status in {408, 504}:
                raise ProviderAdapterError("TIMEOUT")
            if 500 <= response.status <= 599:
                raise ProviderAdapterError("NETWORK_ERROR")
            if response.status != 200:
                raise ProviderAdapterError("INVALID_RESPONSE")
            try:
                decoded = json.loads(response.body.decode("utf-8", errors="strict"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ProviderAdapterError("INVALID_RESPONSE") from error
            candidate = self._extract(decoded, mode)
            try:
                return normalize_definition_envelope(candidate)
            except DomainError as error:
                raise ProviderAdapterError("INVALID_RESPONSE") from error
        finally:
            self.last_attempt["duration_ms"] = max(
                0, int((self.monotonic() - started) * 1000),
            )
