"""Offline/Vertex model adapters that only propose Digest candidates."""

from dataclasses import dataclass
import copy
import hashlib
import json
import os
import socket
import time
import unicodedata
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from mini_harness_core.security import SECRET_PATTERNS

from ..domain import SCORE_COMPONENTS
from ..domain import InterestProfile, project_profile


LLM_API_KEY = "LLM_API_KEY"
LLM_API_MODE = "LLM_API_MODE"
LLM_ENDPOINT = "LLM_ENDPOINT"
LLM_MODEL = "LLM_MODEL"
VERTEX_PROVIDER_IDENTITY = "vertex"
DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_RESPONSE_BYTES = 1_000_000
MAX_RETRY_AFTER_SECONDS = 3_600
PROVIDER_ERROR_CODES = frozenset({
    "CONFIGURATION_ERROR", "AUTH_FAILED", "TIMEOUT", "RATE_LIMITED",
    "NETWORK_ERROR", "INVALID_RESPONSE", "MODEL_REFUSAL", "EMPTY_OUTPUT",
})
RETRYABLE_PROVIDER_ERRORS = frozenset({
    "TIMEOUT", "RATE_LIMITED", "NETWORK_ERROR",
})
STRUCTURED_RETRY_SUBTYPES = frozenset({
    "NON_JSON", "JSON_PARSE", "SCHEMA_MISMATCH", "ENVELOPE_EXTRACTION",
})
PROVIDER_FAILURE_SUBTYPES = frozenset({
    "TRANSPORT", "MODEL_TIMEOUT", "EMPTY_RESPONSE", "NON_JSON",
    "JSON_PARSE", "SCHEMA_MISMATCH", "ENVELOPE_EXTRACTION",
    "INVALID_CONTENT_REF",
    "INVALID_SOURCE_REF", "DUPLICATE_ITEM", "OUTPUT_TOO_LONG",
    "MODEL_REFUSAL", "OTHER_SAFE_CODE",
})
JSON_LEXICAL_SUBTYPES = frozenset({
    "EXPECTING_COMMA", "UNTERMINATED_STRING", "INVALID_ESCAPE",
    "EXPECTING_PROPERTY_NAME", "EXTRA_DATA", "OTHER_JSON_SYNTAX",
})
CANDIDATE_SCHEMA_MISMATCH_RULES = frozenset({
    "TOP_LEVEL_SHAPE", "SUMMARY_TYPE", "SUMMARY_EMPTY",
    "SUMMARY_TOO_LONG", "SUMMARY_CONTROL", "ITEMS_TYPE", "ITEM_COUNT",
    "ITEM_SHAPE", "ITEM_STRING_TYPE", "ITEM_STRING_EMPTY",
    "ITEM_STRING_TOO_LONG", "ITEM_STRING_CONTROL",
    "ITEM_SOURCE_REFS_TYPE", "ITEM_SOURCE_REFS_COUNT",
    "SELECTED_REFS_TYPE", "SELECTED_REFS_COUNT", "SELECTED_REF_SHAPE",
    "SELECTED_REF_STRING_TYPE", "SELECTED_REF_STRING_EMPTY",
    "SELECTED_REF_STRING_TOO_LONG", "SELECTED_REF_STRING_CONTROL",
})
CANDIDATE_SCHEMA_FIELDS = frozenset({
    "summary", "candidate_id", "content_identity", "content",
    "recommendation_reason", "source_ref_ids", "source_ref_id",
    "items", "selected_source_refs",
})
GENERATION_FAILURE_SUBTYPES = frozenset(
    set(CANDIDATE_SCHEMA_MISMATCH_RULES)
    | set(JSON_LEXICAL_SUBTYPES)
    | {"ENVELOPE_EXTRACTION"}
)
GENERATION_DIAGNOSTIC_FIELDS = frozenset({
    "schema_mismatch_field", "payload_source", "payload_top_type",
    "payload_items_type", "payload_items_string_chars",
    "payload_items_string_starts_array", "payload_items_string_ends_array",
    "payload_items_nested_json_parse", "payload_items_nested_type",
    "envelope_error", "json_lexical_subtype",
})
ENVELOPE_EXTRACTION_ERRORS = frozenset({
    "CHOICES_SHAPE", "FINISH_REASON_MISMATCH", "MESSAGE_SHAPE",
    "MISSING_TOOL_CALL", "TOOL_CALLS_TYPE", "TOOL_CALL_COUNT",
    "CONTENT_TOOL_AMBIGUITY", "TOOL_CALL_SHAPE", "TOOL_KIND_MISMATCH",
    "FUNCTION_SHAPE", "TOOL_NAME_MISMATCH", "MISSING_ARGUMENTS",
    "ARGUMENTS_TYPE", "EMPTY_ARGUMENTS",
})
SAFE_JSON_TYPES = frozenset({
    "null", "boolean", "string", "array", "object", "number", "other",
})
STRUCTURED_CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string"},
                    "content_identity": {"type": "string"},
                    "content": {"type": "string"},
                    "recommendation_reason": {"type": "string"},
                    "source_ref_ids": {
                        "type": "array", "items": {"type": "string"},
                    },
                },
                "required": [
                    "candidate_id", "content_identity", "content",
                    "recommendation_reason", "source_ref_ids",
                ],
                "additionalProperties": False,
            },
        },
        "selected_source_refs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_ref_id": {"type": "string"},
                    "candidate_id": {"type": "string"},
                },
                "required": ["source_ref_id", "candidate_id"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "items", "selected_source_refs"],
    "additionalProperties": False,
}
VERTEX_TOOL_CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        name: {"type": "string"} for name in (
            "summary", "candidate_id", "content_identity", "content",
            "recommendation_reason", "source_ref_id",
        )
    },
    "required": [
        "summary", "candidate_id", "content_identity", "content",
        "recommendation_reason", "source_ref_id",
    ],
    "additionalProperties": False,
}
REFUSAL_FINISH_REASONS = frozenset({
    "content_filter", "model_refusal", "prohibited_content", "recitation",
    "refusal", "safety", "spii",
})


class ProviderAdapterError(RuntimeError):
    """Safe allowlisted synthesis failure; raw provider details never escape."""

    def __init__(self, code, *, retry_after_seconds=None, subtype=None):
        if code not in PROVIDER_ERROR_CODES:
            raise ValueError("unknown Provider adapter error code")
        if subtype is not None and subtype not in PROVIDER_FAILURE_SUBTYPES:
            raise ValueError("unknown Provider adapter error subtype")
        self.code = code
        self.retryable = code in RETRYABLE_PROVIDER_ERRORS
        self.retry_after_seconds = retry_after_seconds
        self.subtype = subtype
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class VertexHTTPResponse:
    """Transient HTTP response inspected only inside the Vertex adapter."""

    status: int
    headers: object
    body: bytes


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibVertexTransport:
    """Bounded stdlib POST transport with redirects disabled."""

    def __init__(self, opener=None):
        self.opener = opener or urllib_request.build_opener(_NoRedirectHandler())

    def post(self, url, headers, body, timeout, maximum_bytes):
        request = urllib_request.Request(
            url, data=body, headers=dict(headers), method="POST",
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                length = response.headers.get("Content-Length")
                if length is not None:
                    try:
                        if int(length) > maximum_bytes:
                            raise ProviderAdapterError("INVALID_RESPONSE")
                    except ValueError:
                        pass
                response_body = response.read(maximum_bytes + 1)
                if len(response_body) > maximum_bytes:
                    raise ProviderAdapterError("INVALID_RESPONSE")
                return VertexHTTPResponse(
                    int(response.status), response.headers, response_body,
                )
        except urllib_error.HTTPError as error:
            # Error bodies can contain provider details and are not read.
            return VertexHTTPResponse(int(error.code), error.headers, b"")
        except (TimeoutError, socket.timeout) as error:
            raise ProviderAdapterError("TIMEOUT") from error
        except urllib_error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise ProviderAdapterError("TIMEOUT") from error
            raise ProviderAdapterError("NETWORK_ERROR") from error
        except ProviderAdapterError:
            raise
        except OSError as error:
            raise ProviderAdapterError("NETWORK_ERROR") from error


def _canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _identity(value):
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


STRUCTURED_CANDIDATE_SCHEMA_IDENTITY = _identity(STRUCTURED_CANDIDATE_SCHEMA)
VERTEX_TOOL_CANDIDATE_SCHEMA_IDENTITY = _identity(VERTEX_TOOL_CANDIDATE_SCHEMA)


def _json_lexical_subtype(error):
    """Reduce JSON parser detail to a fixed, non-content-bearing category."""
    message = error.msg
    if message == "Expecting ',' delimiter":
        return "EXPECTING_COMMA"
    if message.startswith("Unterminated string"):
        return "UNTERMINATED_STRING"
    if message == "Invalid \\escape":
        return "INVALID_ESCAPE"
    if message == "Expecting property name enclosed in double quotes":
        return "EXPECTING_PROPERTY_NAME"
    if message == "Extra data":
        return "EXTRA_DATA"
    return "OTHER_JSON_SYNTAX"


def _safe_json_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "number"
    return "other"


def _safe_payload_shape(value):
    metadata = {"payload_top_type": _safe_json_type(value)}
    if isinstance(value, dict):
        flat_wire = "candidate_id" in value
        items = value.get("candidate_id" if flat_wire else "items")
        metadata.update({
            "payload_summary_type": _safe_json_type(value.get("summary")),
            "payload_items_type": _safe_json_type(items),
            "payload_selected_source_refs_type": _safe_json_type(
                value.get(
                    "source_ref_id" if flat_wire
                    else "selected_source_refs"
                ),
            ),
        })
        if isinstance(items, str) and not flat_wire:
            stripped = items.strip()
            metadata.update({
                "payload_items_string_chars": len(items),
                "payload_items_string_starts_array": stripped.startswith("["),
                "payload_items_string_ends_array": stripped.endswith("]"),
                "payload_items_nested_json_parse": False,
            })
            try:
                nested = json.loads(items)
            except json.JSONDecodeError:
                pass
            else:
                metadata.update({
                    "payload_items_nested_json_parse": True,
                    "payload_items_nested_type": _safe_json_type(nested),
                })
    return metadata


def safe_provider_attempt_metadata(value, allowed, *, schema_rules=None,
                                   schema_fields=None):
    """Project provider diagnostics through one shared safe allowlist."""
    if not isinstance(value, dict):
        return {}
    schema_rules = (CANDIDATE_SCHEMA_MISMATCH_RULES
                    if schema_rules is None else schema_rules)
    schema_fields = (CANDIDATE_SCHEMA_FIELDS
                     if schema_fields is None else schema_fields)
    return {
        key: item for key, item in value.items()
        if key in allowed and (
            item is None or isinstance(item, (str, int, float, bool))
        )
        and (key != "json_lexical_subtype"
             or item in JSON_LEXICAL_SUBTYPES)
        and (key != "schema_mismatch_rule" or item in schema_rules)
        and (key != "schema_mismatch_field" or item in schema_fields)
        and (key != "envelope_error"
             or item in ENVELOPE_EXTRACTION_ERRORS)
        and (key not in {
            "message_type", "content_type", "arguments_type",
            "payload_top_type", "payload_summary_type",
            "payload_items_type", "payload_selected_source_refs_type",
            "payload_items_nested_type",
        } or item in SAFE_JSON_TYPES)
        and (key != "payload_source" or item == "tool_arguments")
    }


def structured_provider_retryable(error):
    return bool(
        isinstance(error, ProviderAdapterError)
        and (
            error.code == "TIMEOUT"
            or (error.code == "INVALID_RESPONSE"
                and error.subtype in STRUCTURED_RETRY_SUBTYPES)
        )
    )


def provider_attempt_identity(logical_id, stage, attempt_number):
    return hashlib.sha256(
        f"{logical_id}:{stage}:{attempt_number}".encode("utf-8"),
    ).hexdigest()[:32]


class _CandidateSchemaMismatch(ValueError):
    def __init__(self, rule, **diagnostics):
        if rule not in CANDIDATE_SCHEMA_MISMATCH_RULES:
            raise ValueError("unknown candidate schema mismatch rule")
        self.diagnostics = {"schema_mismatch_rule": rule, **diagnostics}
        super().__init__(rule)


def _bounded_string(value, minimum, maximum, *, scope, field, item_index=None):
    diagnostics = {"schema_mismatch_field": field}
    if item_index is not None:
        diagnostics["schema_mismatch_item_index"] = item_index
    if not isinstance(value, str):
        raise _CandidateSchemaMismatch(
            f"{scope}_TYPE", **diagnostics,
        )
    value = value.strip()
    diagnostics["schema_actual_chars"] = len(value)
    diagnostics["schema_expected_min_chars"] = minimum
    diagnostics["schema_expected_max_chars"] = maximum
    if len(value) < minimum:
        raise _CandidateSchemaMismatch(
            f"{scope}_EMPTY", **diagnostics,
        )
    if len(value) > maximum:
        raise _CandidateSchemaMismatch(
            f"{scope}_TOO_LONG", **diagnostics,
        )
    control_count = sum(
        unicodedata.category(ch) == "Cc" for ch in value
    )
    if control_count:
        raise _CandidateSchemaMismatch(
            f"{scope}_CONTROL", schema_control_char_count=control_count,
            **diagnostics,
        )
    return value


def _safe_model_identity(value, fallback):
    if not isinstance(value, str):
        return fallback
    value = value.strip()
    if (not 1 <= len(value) <= 160
            or not all(ch.isalnum() or ch in "._:/-" for ch in value)):
        return fallback
    return value


def _parse_structured_candidate(value):
    if not isinstance(value, dict) or set(value) != {
        "summary", "items", "selected_source_refs",
    }:
        raise _CandidateSchemaMismatch("TOP_LEVEL_SHAPE")
    summary = _bounded_string(
        value["summary"], 1, 8_000, scope="SUMMARY", field="summary",
    )
    items = value["items"]
    refs = value["selected_source_refs"]
    if not isinstance(items, list):
        raise _CandidateSchemaMismatch(
            "ITEMS_TYPE", schema_mismatch_field="items",
        )
    if not 1 <= len(items) <= 10:
        raise _CandidateSchemaMismatch(
            "ITEM_COUNT", schema_mismatch_field="items",
            schema_actual_item_count=len(items),
        )
    if not isinstance(refs, list):
        raise _CandidateSchemaMismatch(
            "SELECTED_REFS_TYPE",
            schema_mismatch_field="selected_source_refs",
        )
    if len(refs) > 10:
        raise _CandidateSchemaMismatch(
            "SELECTED_REFS_COUNT",
            schema_mismatch_field="selected_source_refs",
            schema_actual_ref_count=len(refs),
        )
    checked_items = []
    for item_index, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != {
            "candidate_id", "content_identity", "content",
            "recommendation_reason", "source_ref_ids",
        }:
            raise _CandidateSchemaMismatch(
                "ITEM_SHAPE", schema_mismatch_item_index=item_index,
            )
        source_ref_ids = item["source_ref_ids"]
        if not isinstance(source_ref_ids, list):
            raise _CandidateSchemaMismatch(
                "ITEM_SOURCE_REFS_TYPE",
                schema_mismatch_field="source_ref_ids",
                schema_mismatch_item_index=item_index,
            )
        if len(source_ref_ids) > 10:
            raise _CandidateSchemaMismatch(
                "ITEM_SOURCE_REFS_COUNT",
                schema_mismatch_field="source_ref_ids",
                schema_mismatch_item_index=item_index,
                schema_actual_ref_count=len(source_ref_ids),
            )
        checked_items.append({
            "candidate_id": _bounded_string(
                item["candidate_id"], 1, 128, scope="ITEM_STRING",
                field="candidate_id", item_index=item_index,
            ),
            "content_identity": _bounded_string(
                item["content_identity"], 1, 128, scope="ITEM_STRING",
                field="content_identity", item_index=item_index,
            ),
            "content": _bounded_string(
                item["content"], 1, 8_000, scope="ITEM_STRING",
                field="content", item_index=item_index,
            ),
            "recommendation_reason": _bounded_string(
                item["recommendation_reason"], 1, 500,
                scope="ITEM_STRING", field="recommendation_reason",
                item_index=item_index,
            ),
            "source_ref_ids": [
                _bounded_string(
                    ref, 1, 40, scope="ITEM_STRING",
                    field="source_ref_ids", item_index=item_index,
                ) for ref in source_ref_ids
            ],
        })
    checked_refs = []
    for ref_index, ref in enumerate(refs):
        if not isinstance(ref, dict) or set(ref) != {
            "source_ref_id", "candidate_id",
        }:
            raise _CandidateSchemaMismatch(
                "SELECTED_REF_SHAPE", schema_mismatch_item_index=ref_index,
            )
        checked_refs.append({
            "source_ref_id": _bounded_string(
                ref["source_ref_id"], 1, 40,
                scope="SELECTED_REF_STRING", field="source_ref_id",
                item_index=ref_index,
            ),
            "candidate_id": _bounded_string(
                ref["candidate_id"], 1, 128,
                scope="SELECTED_REF_STRING", field="candidate_id",
                item_index=ref_index,
            ),
        })
    return {
        "summary": summary, "items": checked_items,
        "selected_source_refs": checked_refs,
    }


def _parse_vertex_tool_candidate(value):
    """Validate flat scalar tool arguments, then derive canonical lists."""
    if not isinstance(value, dict) or set(value) != {
        "summary", "candidate_id", "content_identity", "content",
        "recommendation_reason", "source_ref_id",
    }:
        raise _CandidateSchemaMismatch("TOP_LEVEL_SHAPE")
    canonical_item = {
        "candidate_id": value["candidate_id"],
        "content_identity": value["content_identity"],
        "content": value["content"],
        "recommendation_reason": value["recommendation_reason"],
        "source_ref_ids": [value["source_ref_id"]],
    }
    ref = {
        "source_ref_id": value["source_ref_id"],
        "candidate_id": value["candidate_id"],
    }
    return _parse_structured_candidate({
        "summary": value["summary"],
        "items": [canonical_item],
        "selected_source_refs": [ref],
    })


class VertexDigestProvider:
    """App-owned Vertex-backed LiteLLM synthesis candidate adapter."""

    provider_identity = VERTEX_PROVIDER_IDENTITY

    def __init__(self, *, transport=None, environ=None,
                 timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                 maximum_response_bytes=MAX_RESPONSE_BYTES, monotonic=None):
        if (not isinstance(timeout_seconds, (int, float))
                or isinstance(timeout_seconds, bool)
                or not 0 < timeout_seconds <= 180):
            raise ValueError("invalid Vertex timeout")
        if (not isinstance(maximum_response_bytes, int)
                or isinstance(maximum_response_bytes, bool)
                or not 1_024 <= maximum_response_bytes <= 5_000_000):
            raise ValueError("invalid Vertex response bound")
        self.transport = transport or UrllibVertexTransport()
        self.environ = os.environ if environ is None else environ
        self.timeout_seconds = float(timeout_seconds)
        self.maximum_response_bytes = maximum_response_bytes
        self.monotonic = monotonic or time.monotonic
        self.calls = []
        self.last_error = None
        self.last_model_identity = None
        self.last_attempt = None
        self.model_identity = _safe_model_identity(
            self.environ.get(LLM_MODEL), "unconfigured",
        )

    @classmethod
    def from_environment(cls, **kwargs):
        return cls(**kwargs)

    def _safe_config_value(self, name, minimum, maximum):
        value = self.environ.get(name)
        if not isinstance(value, str):
            raise ProviderAdapterError("CONFIGURATION_ERROR")
        value = value.strip()
        if (not minimum <= len(value) <= maximum
                or any(unicodedata.category(ch) == "Cc" for ch in value)):
            raise ProviderAdapterError("CONFIGURATION_ERROR")
        return value

    def _configuration(self):
        key = self._safe_config_value(LLM_API_KEY, 8, 4_096)
        endpoint = self._safe_config_value(LLM_ENDPOINT, 8, 2_000).rstrip("/")
        model = self._safe_config_value(LLM_MODEL, 1, 120)
        mode = self._safe_config_value(LLM_API_MODE, 1, 40).casefold()
        if mode not in {"chat-completions", "completions"}:
            raise ProviderAdapterError("CONFIGURATION_ERROR")
        if _safe_model_identity(model, "") != model:
            raise ProviderAdapterError("CONFIGURATION_ERROR")
        parts = urllib_parse.urlsplit(endpoint)
        if (parts.scheme != "https" or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment):
            raise ProviderAdapterError("CONFIGURATION_ERROR")
        suffix = "/chat/completions" if mode == "chat-completions" else "/completions"
        if endpoint.endswith("/v1"):
            endpoint += suffix
        elif mode == "completions" and endpoint.endswith("/v1/chat/completions"):
            endpoint = endpoint[:-len("/chat/completions")] + "/completions"
        elif mode == "chat-completions" and endpoint.endswith("/v1/completions"):
            endpoint = endpoint[:-len("/completions")] + "/chat/completions"
        return key, endpoint, model, mode

    @staticmethod
    def _safe_input(subscription, selected, period_key, profile_projection,
                    candidate_limit=None):
        selected_for_prompt = (
            selected[:candidate_limit] if candidate_limit is not None
            else selected
        )
        ranked = []
        for index, item in enumerate(selected_for_prompt, 1):
            candidate = item.candidate
            ranked.append({
                "rank": index,
                "candidate_id": candidate.candidate_id,
                "content_identity": candidate.content_identity,
                "title": candidate.title,
                "snippet": candidate.snippet,
                "published_at": candidate.published_at,
                "topic_tags": list(candidate.topic_tags),
                "score": item.score,
                "score_breakdown": [
                    {"component": name, "value": value}
                    for name, value in item.score_breakdown
                ],
                "source_ref": {
                    "source_ref_id": f"S{index}",
                    "canonical_url": candidate.canonical_url,
                    "evidence_id": candidate.evidence_id,
                },
            })
        value = {
            "task": "digest_synthesis_candidate",
            "subscription": {
                "subscription_id": subscription.subscription_id,
                "version": subscription.version,
                "topic": subscription.topic,
                "focus_topics": list(subscription.focus_topics),
                "language": subscription.language,
                "max_chars": subscription.max_chars,
                "max_items": subscription.max_items,
            },
            "period_key": period_key,
            "ranked_candidates": ranked,
            "accepted_evidence_refs": sorted({
                item.candidate.evidence_id for item in selected_for_prompt
            }),
            "profile": copy.deepcopy(profile_projection.as_dict()),
        }
        encoded = _canonical_bytes(value).decode("utf-8")
        if any(pattern.search(encoded) for pattern in SECRET_PATTERNS):
            raise ProviderAdapterError("INVALID_RESPONSE")
        return value

    @staticmethod
    def _prompt(safe_input):
        return (
            "[SYSTEM]\n"
            "You propose one digest synthesis candidate. Return exactly one JSON "
            "object with keys summary, items, selected_source_refs and no prose. "
            "Use only ranked candidate IDs, content identities, and source_ref IDs "
            "from INPUT. Do not add facts beyond the cited candidate title/snippet. "
            "Each item has candidate_id, content_identity, content, "
            "recommendation_reason, source_ref_ids. Each selected source ref has "
            "source_ref_id and candidate_id. Preserve ranked order. Every JSON "
            "string value must be single-line: do not include literal newline "
            "characters or escaped \\n sequences. Emit the entire JSON object on "
            "exactly one physical line.\n\n"
            "[USER TASK]\n"
            + _canonical_bytes(safe_input).decode("utf-8")
            + "\n\n[ASSISTANT NEXT CANDIDATE]\n"
            "Return only strict JSON. The first character has already been emitted. "
            "Continue the object without Markdown fences or explanation.\n"
            "ASSISTANT: {"
        )

    @staticmethod
    def _native_schema_prompt(safe_input):
        return (
            "[SYSTEM]\n"
            "You propose one digest synthesis candidate. The response is "
            "constrained by the supplied JSON Schema. Fill only its declared "
            "fields. Use only ranked candidate IDs, content identities, and "
            "source_ref IDs from INPUT. Do not add facts beyond the cited "
            "candidate title/snippet. Preserve ranked order. Keep every string "
            "value on one physical line and return no explanation. In the tool "
            "arguments, use exactly six top-level string fields and no nested "
            "objects or arrays. Use this exact type skeleton: {summary: string, "
            "candidate_id: string, content_identity: string, content: string, "
            "recommendation_reason: string, source_ref_id: string}. Choose one "
            "provided rank-1 candidate and its matching source_ref_id; it is the "
            "only candidate in INPUT. Copy its candidate_id and content_identity "
            "exactly. Do not JSON-stringify "
            "any field and do not return extra keys. Keep the combined summary "
            "and item content materially below subscription.max_chars so the "
            "deterministic renderer has room for source markers. Submit the "
            "candidate through the required submit_digest_candidate tool.\n\n"
            "[USER TASK]\n"
            + _canonical_bytes(safe_input).decode("utf-8")
        )

    @staticmethod
    def structured_request_body(model, mode, prompt, *, tool_name,
                                tool_description, tool_schema,
                                max_output_tokens):
        if mode == "completions":
            return {
                "model": model, "prompt": prompt,
                "temperature": 0, "max_tokens": max_output_tokens,
            }
        return {
            "model": model,
            "messages": [{
                "role": "user", "content": prompt,
            }],
            "temperature": 0, "max_tokens": max_output_tokens,
            "tools": [{
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": tool_description,
                    "strict": True,
                    "parameters": tool_schema,
                },
            }],
            "tool_choice": {
                "type": "function", "function": {"name": tool_name},
            },
        }

    @classmethod
    def _request_body(cls, model, mode, prompt):
        return cls.structured_request_body(
            model, mode, prompt,
            tool_name="submit_digest_candidate",
            tool_description="Submit the final Digest synthesis candidate",
            tool_schema=VERTEX_TOOL_CANDIDATE_SCHEMA,
            max_output_tokens=2_048,
        )

    def describe_attempt(self, subscription, selected, period_key,
                         profile_projection):
        _key, _endpoint, model, mode = self._configuration()
        safe_input = self._safe_input(
            subscription, selected, period_key, profile_projection,
            1 if mode == "chat-completions" else None,
        )
        prompt = (
            self._prompt(safe_input) if mode == "completions"
            else self._native_schema_prompt(safe_input)
        )
        request_body = self._request_body(model, mode, prompt)
        encoded = _canonical_bytes(request_body)
        return {
            "provider_identity": self.provider_identity,
            "model_identity": model,
            "api_mode": mode,
            "prompt_chars": len(prompt),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "request_sha256": hashlib.sha256(encoded).hexdigest(),
            "candidate_count": len(safe_input["ranked_candidates"]),
            "schema_identity": (
                VERTEX_TOOL_CANDIDATE_SCHEMA_IDENTITY
                if mode == "chat-completions"
                else STRUCTURED_CANDIDATE_SCHEMA_IDENTITY
            ),
            "structured_output_mechanism": (
                "strict_flat_scalar_tool_requested_prompt_reinforced"
                if mode == "chat-completions"
                else "prompt_strict_json"
            ),
            "timeout_seconds": self.timeout_seconds,
            "max_output_tokens": request_body["max_tokens"],
            "temperature": request_body["temperature"],
        }

    @staticmethod
    def _retry_after(headers):
        value = headers.get("Retry-After") if headers is not None else None
        if not isinstance(value, str) or not value.isdigit():
            return None
        return min(int(value), MAX_RETRY_AFTER_SECONDS)

    def _envelope_failure(self, safe_error):
        if safe_error not in ENVELOPE_EXTRACTION_ERRORS:
            raise ValueError("unknown envelope extraction error")
        self.last_attempt["envelope_error"] = safe_error
        raise ProviderAdapterError(
            "INVALID_RESPONSE", subtype="ENVELOPE_EXTRACTION",
        )

    def _content(self, response_body, mode,
                 expected_tool_name="submit_digest_candidate"):
        choices = response_body.get("choices") if isinstance(
            response_body, dict,
        ) else None
        self.last_attempt["choice_count"] = (
            len(choices) if isinstance(choices, list) else None
        )
        if not isinstance(choices, list) or len(choices) != 1:
            self._envelope_failure("CHOICES_SHAPE")
        choice = choices[0]
        if not isinstance(choice, dict):
            self._envelope_failure("CHOICES_SHAPE")
        finish_reason = str(choice.get("finish_reason") or "").casefold()
        if (finish_reason in REFUSAL_FINISH_REASONS
                or choice.get("refusal") is not None):
            raise ProviderAdapterError("MODEL_REFUSAL")
        try:
            if mode == "completions":
                content = choice["text"]
            else:
                message = choice.get("message")
                self.last_attempt["message_type"] = _safe_json_type(message)
                if not isinstance(message, dict):
                    self._envelope_failure("MESSAGE_SHAPE")
                message_content = message.get("content")
                self.last_attempt.update({
                    "content_presence": message_content is not None,
                    "content_type": _safe_json_type(message_content),
                    "tool_calls_presence": "tool_calls" in message,
                })
                tool_calls = message.get("tool_calls")
                self.last_attempt["tool_call_count"] = (
                    len(tool_calls) if isinstance(tool_calls, list) else None
                )
                if tool_calls is None or tool_calls == []:
                    self._envelope_failure("MISSING_TOOL_CALL")
                if not isinstance(tool_calls, list):
                    self._envelope_failure("TOOL_CALLS_TYPE")
                meaningful_content = (
                    message_content is not None
                    and (not isinstance(message_content, str)
                         or bool(message_content.strip()))
                )
                if meaningful_content:
                    self._envelope_failure("CONTENT_TOOL_AMBIGUITY")
                if finish_reason != "tool_calls":
                    self._envelope_failure("FINISH_REASON_MISMATCH")
                if len(tool_calls) != 1:
                    self._envelope_failure("TOOL_CALL_COUNT")
                tool_call = tool_calls[0]
                if not isinstance(tool_call, dict):
                    self._envelope_failure("TOOL_CALL_SHAPE")
                self.last_attempt["tool_kind_match"] = (
                    tool_call.get("type") == "function"
                )
                if not self.last_attempt["tool_kind_match"]:
                    self._envelope_failure("TOOL_KIND_MISMATCH")
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    self._envelope_failure("FUNCTION_SHAPE")
                self.last_attempt["function_name_match"] = (
                    function.get("name") == expected_tool_name
                )
                if not self.last_attempt["function_name_match"]:
                    self._envelope_failure("TOOL_NAME_MISMATCH")
                self.last_attempt["arguments_presence"] = (
                    "arguments" in function
                )
                if "arguments" not in function:
                    self._envelope_failure("MISSING_ARGUMENTS")
                content = function["arguments"]
                self.last_attempt["arguments_type"] = _safe_json_type(content)
                if not isinstance(content, str):
                    self._envelope_failure("ARGUMENTS_TYPE")
                if not content.strip():
                    self._envelope_failure("EMPTY_ARGUMENTS")
                self.last_attempt["payload_source"] = "tool_arguments"
        except ProviderAdapterError:
            raise
        except (KeyError, TypeError, AttributeError) as error:
            raise ProviderAdapterError("INVALID_RESPONSE") from error
        if not isinstance(content, str) or not content.strip():
            raise ProviderAdapterError("EMPTY_OUTPUT")
        content = content.strip()
        if mode == "completions" and not content.startswith("{"):
            if content.startswith('"'):
                content = "{" + content
            else:
                raise ProviderAdapterError(
                    "INVALID_RESPONSE", subtype="NON_JSON",
                )
        return content, finish_reason

    def describe_structured_protocol(self, prompt, *, tool_name,
                                     tool_description, tool_schema,
                                     schema_identity,
                                     max_output_tokens=1_024):
        _key, _endpoint, model, mode = self._configuration()
        if mode != "chat-completions":
            raise ProviderAdapterError("CONFIGURATION_ERROR")
        request = self.structured_request_body(
            model, mode, prompt, tool_name=tool_name,
            tool_description=tool_description, tool_schema=tool_schema,
            max_output_tokens=max_output_tokens,
        )
        encoded = _canonical_bytes(request)
        return {
            "provider_identity": self.provider_identity,
            "model_identity": model,
            "api_mode": mode,
            "prompt_chars": len(prompt),
            "prompt_sha256": hashlib.sha256(
                prompt.encode("utf-8"),
            ).hexdigest(),
            "request_sha256": hashlib.sha256(encoded).hexdigest(),
            "schema_identity": schema_identity,
            "structured_output_mechanism": (
                "strict_flat_scalar_tool_requested_prompt_reinforced"
            ),
            "timeout_seconds": self.timeout_seconds,
            "max_output_tokens": max_output_tokens,
            "temperature": 0,
        }

    def request_structured_protocol(self, prompt, *, tool_name,
                                    tool_description, tool_schema,
                                    schema_identity, parser,
                                    max_output_tokens=1_024):
        """Run the shared strict-tool envelope/JSON/schema pipeline once."""
        key, endpoint, model, mode = self._configuration()
        metadata = self.describe_structured_protocol(
            prompt, tool_name=tool_name, tool_description=tool_description,
            tool_schema=tool_schema, schema_identity=schema_identity,
            max_output_tokens=max_output_tokens,
        )
        request = self.structured_request_body(
            model, mode, prompt, tool_name=tool_name,
            tool_description=tool_description, tool_schema=tool_schema,
            max_output_tokens=max_output_tokens,
        )
        encoded = _canonical_bytes(request)
        self.last_attempt = metadata
        self.last_error = None
        self.last_model_identity = None
        started = self.monotonic()
        stage = "transport"
        try:
            response = self.transport.post(
                endpoint,
                {"Content-Type": "application/json",
                 "Accept": "application/json",
                 "Authorization": f"Bearer {key}"},
                encoded, self.timeout_seconds, self.maximum_response_bytes,
            )
            stage = "response_envelope"
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
                    retry_after_seconds=self._retry_after(response.headers),
                )
            if response.status in {408, 504}:
                raise ProviderAdapterError("TIMEOUT")
            if 500 <= response.status <= 599:
                raise ProviderAdapterError("NETWORK_ERROR")
            if response.status != 200:
                raise ProviderAdapterError("INVALID_RESPONSE")
            stage = "response_json"
            try:
                response_body = json.loads(
                    response.body.decode("utf-8", errors="strict"),
                )
            except UnicodeDecodeError as error:
                raise ProviderAdapterError("INVALID_RESPONSE") from error
            except json.JSONDecodeError as error:
                self.last_attempt["json_lexical_subtype"] = (
                    _json_lexical_subtype(error)
                )
                raise ProviderAdapterError(
                    "INVALID_RESPONSE", subtype="JSON_PARSE",
                ) from error
            stage = "model_content"
            usage = response_body.get("usage") if isinstance(
                response_body, dict,
            ) else None
            if isinstance(usage, dict):
                output_tokens = usage.get("completion_tokens")
                if type(output_tokens) is int and 0 <= output_tokens <= 1_000_000:
                    self.last_attempt["output_tokens"] = output_tokens
            raw_candidate, finish_reason = self._content(
                response_body, mode, tool_name,
            )
            self.last_attempt.update({
                "response_chars": len(raw_candidate),
                "content_sha256": hashlib.sha256(
                    raw_candidate.encode("utf-8"),
                ).hexdigest(),
                "finish_reason": finish_reason[:40],
                "json_parse_succeeded": False,
                "schema_validation_succeeded": False,
            })
            stage = "model_json"
            try:
                parsed = json.loads(raw_candidate)
            except json.JSONDecodeError as error:
                self.last_attempt.update({
                    "parse_error_line": error.lineno,
                    "parse_error_column": error.colno,
                    "starts_with_object": raw_candidate.startswith("{"),
                    "ends_with_object": raw_candidate.endswith("}"),
                    "json_lexical_subtype": _json_lexical_subtype(error),
                })
                raise ProviderAdapterError(
                    "INVALID_RESPONSE", subtype="JSON_PARSE",
                ) from error
            self.last_attempt["payload_top_type"] = _safe_json_type(parsed)
            self.last_attempt["json_parse_succeeded"] = True
            stage = "candidate_schema"
            candidate = parser(parsed, self.last_attempt)
            self.last_attempt["schema_validation_succeeded"] = True
            self.last_model_identity = _safe_model_identity(
                response_body.get("model"), model,
            ) if isinstance(response_body, dict) else model
            return candidate
        except ProviderAdapterError as error:
            subtype = error.subtype
            if subtype is None and error.code == "TIMEOUT":
                subtype = "MODEL_TIMEOUT"
            elif subtype is None and error.code == "NETWORK_ERROR":
                subtype = "TRANSPORT"
            elif subtype is None and error.code == "EMPTY_OUTPUT":
                subtype = "EMPTY_RESPONSE"
            elif subtype is None and error.code == "MODEL_REFUSAL":
                subtype = "MODEL_REFUSAL"
            elif subtype is None and error.code == "INVALID_RESPONSE":
                subtype = "OTHER_SAFE_CODE"
            error.subtype = subtype
            self.last_attempt.update({
                "failure_subtype": subtype or error.code,
                "duration_ms": max(
                    0, int((self.monotonic() - started) * 1000),
                ),
            })
            self.last_error = {
                "code": error.code, "retryable": error.retryable,
                "retry_after_seconds": error.retry_after_seconds,
                "provider_identity": self.provider_identity,
                "model_identity": model, "stage": stage,
                "subtype": subtype,
            }
            raise
        finally:
            if "duration_ms" not in self.last_attempt:
                self.last_attempt["duration_ms"] = max(
                    0, int((self.monotonic() - started) * 1000),
                )

    @staticmethod
    def _digest_payload(candidate, subscription, selected, period_key,
                        digest_id, profile_projection):
        selected_by_id = {
            item.candidate.candidate_id: item for item in selected
        }
        selected_by_ref = {
            f"S{index}": item for index, item in enumerate(selected, 1)
        }
        items = []
        for position, proposed in enumerate(candidate["items"], 1):
            ranked = selected_by_id.get(proposed["candidate_id"])
            if ranked is None:
                item_id = hashlib.sha256(
                    proposed["candidate_id"].encode("utf-8"),
                ).hexdigest()[:32]
                topic_tags = []
                score = 0
                breakdown = [
                    {"component": name, "value": 0}
                    for name in SCORE_COMPONENTS
                ]
            else:
                item_id = ranked.candidate.content_identity[32:]
                topic_tags = list(ranked.candidate.topic_tags)
                score = ranked.score
                breakdown = [
                    {"component": name, "value": value}
                    for name, value in ranked.score_breakdown
                ]
            markers = " ".join(
                f"[{source_ref_id}]"
                for source_ref_id in proposed["source_ref_ids"]
            )
            text = proposed["content"] + (f" {markers}" if markers else "")
            items.append({
                "item_id": item_id,
                "candidate_id": proposed["candidate_id"],
                "content_identity": proposed["content_identity"],
                "topic_tags": topic_tags,
                "rank": position,
                "score": score,
                "score_breakdown": breakdown,
                "recommendation_reason": proposed["recommendation_reason"],
                "text": text,
                "source_ref_ids": list(proposed["source_ref_ids"]),
            })
        refs = []
        for proposed in candidate["selected_source_refs"]:
            ranked = selected_by_ref.get(proposed["source_ref_id"])
            refs.append({
                "source_ref_id": proposed["source_ref_id"],
                "candidate_id": proposed["candidate_id"],
                "canonical_url": (
                    ranked.candidate.canonical_url if ranked is not None
                    else "https://invalid.local/unbound"
                ),
                "evidence_id": (
                    ranked.candidate.evidence_id if ranked is not None
                    else "0" * 32
                ),
            })
        rendered_text = "\n".join((
            candidate["summary"], *(item["text"] for item in items),
        ))
        return {
            "schema_version": 1,
            "digest_id": digest_id,
            "subscription_id": subscription.subscription_id,
            "subscription_version": subscription.version,
            "period_key": period_key,
            "language": subscription.language,
            "profile_snapshot": copy.deepcopy(profile_projection.as_dict()),
            "rendered_text": rendered_text,
            "character_count": len(rendered_text),
            "items": items,
            "source_refs": refs,
        }

    def synthesize(self, subscription, selected, period_key, digest_id,
                   profile_projection=None):
        if profile_projection is None:
            profile_projection = project_profile(
                InterestProfile.empty(
                    subscription.user_id, subscription.updated_at,
                ),
                subscription,
            )
        key, endpoint, model, mode = self._configuration()
        safe_input = self._safe_input(
            subscription, selected, period_key, profile_projection,
            1 if mode == "chat-completions" else None,
        )
        prompt = (
            self._prompt(safe_input) if mode == "completions"
            else self._native_schema_prompt(safe_input)
        )
        request_body = self._request_body(model, mode, prompt)
        encoded_request = _canonical_bytes(request_body)
        self.calls.append({
            "provider_identity": self.provider_identity,
            "model_identity": model,
            "api_mode": mode,
            "request_identity": hashlib.sha256(encoded_request).hexdigest(),
            "candidate_ids": [
                item["candidate_id"] for item in safe_input["ranked_candidates"]
            ],
            "accepted_evidence_refs": safe_input["accepted_evidence_refs"],
            "profile_projection_id": profile_projection.projection_id,
        })
        self.last_error = None
        self.last_model_identity = None
        self.last_attempt = self.describe_attempt(
            subscription, selected, period_key, profile_projection,
        )
        started = self.monotonic()
        stage = "transport"
        diagnostics = None
        try:
            response = self.transport.post(
                endpoint,
                {"Content-Type": "application/json",
                 "Accept": "application/json",
                 "Authorization": f"Bearer {key}"},
                encoded_request, self.timeout_seconds,
                self.maximum_response_bytes,
            )
            stage = "response_envelope"
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
            retry_after = self._retry_after(response.headers)
            if response.status in {401, 403}:
                raise ProviderAdapterError("AUTH_FAILED")
            if response.status == 429:
                raise ProviderAdapterError(
                    "RATE_LIMITED", retry_after_seconds=retry_after,
                )
            if response.status in {408, 504}:
                raise ProviderAdapterError("TIMEOUT")
            if 500 <= response.status <= 599:
                raise ProviderAdapterError("NETWORK_ERROR")
            if response.status != 200:
                raise ProviderAdapterError("INVALID_RESPONSE")
            if len(response.body) > self.maximum_response_bytes:
                raise ProviderAdapterError("INVALID_RESPONSE")
            stage = "response_json"
            try:
                decoded_response = response.body.decode(
                    "utf-8", errors="strict",
                )
                response_body = json.loads(decoded_response)
            except UnicodeDecodeError as error:
                raise ProviderAdapterError("INVALID_RESPONSE") from error
            except json.JSONDecodeError as error:
                self.last_attempt["json_lexical_subtype"] = (
                    _json_lexical_subtype(error)
                )
                raise ProviderAdapterError("INVALID_RESPONSE") from error
            stage = "model_content"
            usage = response_body.get("usage") if isinstance(
                response_body, dict,
            ) else None
            if isinstance(usage, dict):
                output_tokens = usage.get("completion_tokens")
                if (type(output_tokens) is int
                        and 0 <= output_tokens <= 1_000_000):
                    self.last_attempt["output_tokens"] = output_tokens
            raw_candidate, finish_reason = self._content(response_body, mode)
            self.last_attempt.update({
                "response_chars": len(raw_candidate),
                "content_sha256": hashlib.sha256(
                    raw_candidate.encode("utf-8"),
                ).hexdigest(),
                "finish_reason": finish_reason[:40],
                "json_parse_succeeded": False,
                "schema_validation_succeeded": False,
            })
            stage = "model_json"
            try:
                parsed = json.loads(raw_candidate)
            except json.JSONDecodeError as error:
                lexical_subtype = _json_lexical_subtype(error)
                diagnostics = {
                    "content_length": len(raw_candidate),
                    "starts_with_object": raw_candidate.startswith("{"),
                    "ends_with_object": raw_candidate.endswith("}"),
                    "starts_with_fence": raw_candidate.startswith("```"),
                    "second_character_code": (
                        ord(raw_candidate[1]) if len(raw_candidate) > 1 else None
                    ),
                    "last_character_code": ord(raw_candidate[-1]),
                    "finish_reason": finish_reason[:40],
                    "error_line": error.lineno,
                    "error_column": error.colno,
                    "json_lexical_subtype": lexical_subtype,
                }
                self.last_attempt.update({
                    "parse_error_line": error.lineno,
                    "parse_error_column": error.colno,
                    "starts_with_object": raw_candidate.startswith("{"),
                    "ends_with_object": raw_candidate.endswith("}"),
                    "json_lexical_subtype": lexical_subtype,
                })
                raise ProviderAdapterError(
                    "INVALID_RESPONSE", subtype="JSON_PARSE",
                ) from error
            self.last_attempt.update(_safe_payload_shape(parsed))
            self.last_attempt["json_parse_succeeded"] = True
            stage = "candidate_schema"
            try:
                candidate = (
                    _parse_vertex_tool_candidate(parsed)
                    if mode == "chat-completions"
                    else _parse_structured_candidate(parsed)
                )
            except _CandidateSchemaMismatch as schema_error:
                wire_item_fields = {
                    "candidate_id", "content_identity", "content",
                    "recommendation_reason",
                }
                expected_item_fields = (
                    {
                        "candidate_id", "content_identity", "content",
                        "recommendation_reason",
                    }
                    if mode == "chat-completions" else {
                        "candidate_id", "content_identity", "content",
                        "recommendation_reason", "source_ref_ids",
                    }
                )
                diagnostics = {
                    "object": isinstance(parsed, dict),
                    "exact_top_level": (
                        isinstance(parsed, dict) and set(parsed) == {
                            "summary", "candidate_id", "content_identity",
                            "content", "recommendation_reason", "source_ref_id",
                        } if mode == "chat-completions" else (
                            isinstance(parsed, dict) and set(parsed) == {
                                "summary", "items", "selected_source_refs",
                            }
                        )
                    ),
                    "summary_string": (
                        isinstance(parsed, dict)
                        and isinstance(parsed.get("summary"), str)
                    ),
                    "items_list": (
                        isinstance(parsed, dict)
                        and wire_item_fields.issubset(parsed)
                        if mode == "chat-completions" else (
                            isinstance(parsed, dict)
                            and isinstance(parsed.get("items"), list)
                        )
                    ),
                    "item_count": (
                        1 if mode == "chat-completions"
                        and isinstance(parsed, dict)
                        and wire_item_fields.issubset(parsed) else (
                            len(parsed.get("items", []))
                            if isinstance(parsed, dict)
                            and isinstance(parsed.get("items"), list) else None
                        )
                    ),
                    "exact_item_shapes": (
                        isinstance(parsed, dict)
                        and wire_item_fields.issubset(parsed)
                        and all(
                            isinstance(parsed.get(name), str)
                            for name in wire_item_fields
                        )
                        if mode == "chat-completions" else (
                            isinstance(parsed, dict)
                            and isinstance(parsed.get("items"), list)
                            and all(
                                isinstance(item, dict)
                                and set(item) == expected_item_fields
                                for item in parsed["items"]
                            )
                        )
                    ),
                    "source_refs_list": (
                        isinstance(parsed, dict)
                        and "source_ref_id" in parsed
                        if mode == "chat-completions" else (
                            isinstance(parsed, dict)
                            and isinstance(
                                parsed.get("selected_source_refs"), list,
                            )
                        )
                    ),
                    "exact_source_ref_shapes": (
                        isinstance(parsed, dict)
                        and isinstance(parsed.get("source_ref_id"), str)
                        if mode == "chat-completions" else (
                            isinstance(parsed, dict)
                            and isinstance(
                                parsed.get("selected_source_refs"), list,
                            )
                            and all(
                                isinstance(ref, dict) and set(ref) == {
                                    "source_ref_id", "candidate_id",
                                } for ref in parsed["selected_source_refs"]
                            )
                        )
                    ),
                }
                diagnostics.update(schema_error.diagnostics)
                self.last_attempt.update(schema_error.diagnostics)
                raise ProviderAdapterError(
                    "INVALID_RESPONSE", subtype="SCHEMA_MISMATCH",
                ) from schema_error
            self.last_attempt["schema_validation_succeeded"] = True
            self.last_model_identity = _safe_model_identity(
                response_body.get("model"), model,
            ) if isinstance(response_body, dict) else model
            return self._digest_payload(
                candidate, subscription, selected, period_key, digest_id,
                profile_projection,
            )
        except ProviderAdapterError as error:
            subtype = error.subtype
            if subtype is None and error.code == "TIMEOUT":
                subtype = "MODEL_TIMEOUT"
            elif subtype is None and error.code == "NETWORK_ERROR":
                subtype = "TRANSPORT"
            elif subtype is None and error.code == "EMPTY_OUTPUT":
                subtype = "EMPTY_RESPONSE"
            elif subtype is None and error.code == "MODEL_REFUSAL":
                subtype = "MODEL_REFUSAL"
            elif (subtype is None and error.code == "INVALID_RESPONSE"
                  and stage == "response_json"):
                subtype = "JSON_PARSE"
            elif (subtype is None and error.code == "INVALID_RESPONSE"
                  and stage == "model_content"):
                subtype = "SCHEMA_MISMATCH"
            elif subtype is None and error.code == "INVALID_RESPONSE":
                subtype = "OTHER_SAFE_CODE"
            error.subtype = subtype
            if self.last_attempt is not None:
                self.last_attempt.update({
                    "failure_subtype": subtype or error.code,
                    "duration_ms": max(
                        0, int((self.monotonic() - started) * 1000),
                    ),
                })
            self.last_error = {
                "code": error.code,
                "retryable": error.retryable,
                "retry_after_seconds": error.retry_after_seconds,
                "provider_identity": self.provider_identity,
                "model_identity": model,
                "stage": stage,
                "subtype": subtype,
            }
            if diagnostics is not None:
                self.last_error["diagnostics"] = diagnostics
            raise
        finally:
            if (self.last_attempt is not None
                    and "duration_ms" not in self.last_attempt):
                self.last_attempt["duration_ms"] = max(
                    0, int((self.monotonic() - started) * 1000),
                )


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
