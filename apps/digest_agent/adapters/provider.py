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
    "NON_JSON", "JSON_PARSE", "SCHEMA_MISMATCH",
})
PROVIDER_FAILURE_SUBTYPES = frozenset({
    "TRANSPORT", "MODEL_TIMEOUT", "EMPTY_RESPONSE", "NON_JSON",
    "JSON_PARSE", "SCHEMA_MISMATCH", "INVALID_CONTENT_REF",
    "INVALID_SOURCE_REF", "DUPLICATE_ITEM", "OUTPUT_TOO_LONG",
    "MODEL_REFUSAL", "OTHER_SAFE_CODE",
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


def _bounded_string(value, minimum, maximum):
    if not isinstance(value, str):
        raise ProviderAdapterError("INVALID_RESPONSE")
    value = value.strip()
    if (not minimum <= len(value) <= maximum
            or any(unicodedata.category(ch) == "Cc" for ch in value)):
        raise ProviderAdapterError("INVALID_RESPONSE")
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
        raise ProviderAdapterError("INVALID_RESPONSE")
    summary = _bounded_string(value["summary"], 1, 8_000)
    items = value["items"]
    refs = value["selected_source_refs"]
    if not isinstance(items, list) or not 1 <= len(items) <= 10:
        raise ProviderAdapterError("INVALID_RESPONSE")
    if not isinstance(refs, list) or len(refs) > 10:
        raise ProviderAdapterError("INVALID_RESPONSE")
    checked_items = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            "candidate_id", "content_identity", "content",
            "recommendation_reason", "source_ref_ids",
        }:
            raise ProviderAdapterError("INVALID_RESPONSE")
        source_ref_ids = item["source_ref_ids"]
        if not isinstance(source_ref_ids, list) or len(source_ref_ids) > 10:
            raise ProviderAdapterError("INVALID_RESPONSE")
        checked_items.append({
            "candidate_id": _bounded_string(item["candidate_id"], 1, 128),
            "content_identity": _bounded_string(
                item["content_identity"], 1, 128,
            ),
            "content": _bounded_string(item["content"], 1, 8_000),
            "recommendation_reason": _bounded_string(
                item["recommendation_reason"], 1, 500,
            ),
            "source_ref_ids": [
                _bounded_string(ref, 1, 40) for ref in source_ref_ids
            ],
        })
    checked_refs = []
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {
            "source_ref_id", "candidate_id",
        }:
            raise ProviderAdapterError("INVALID_RESPONSE")
        checked_refs.append({
            "source_ref_id": _bounded_string(
                ref["source_ref_id"], 1, 40,
            ),
            "candidate_id": _bounded_string(ref["candidate_id"], 1, 128),
        })
    return {
        "summary": summary, "items": checked_items,
        "selected_source_refs": checked_refs,
    }


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
    def _safe_input(subscription, selected, period_key, profile_projection):
        ranked = []
        for index, item in enumerate(selected, 1):
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
                item.candidate.evidence_id for item in selected
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
    def _request_body(model, mode, prompt):
        if mode == "completions":
            return {
                "model": model, "prompt": prompt,
                "temperature": 0, "max_tokens": 2_048,
            }
        return {
            "model": model,
            "messages": [{
                "role": "user", "content": prompt[:-1],
            }],
            "temperature": 0, "max_tokens": 2_048,
            "response_format": {"type": "json_object"},
        }

    def describe_attempt(self, subscription, selected, period_key,
                         profile_projection):
        _key, _endpoint, model, mode = self._configuration()
        safe_input = self._safe_input(
            subscription, selected, period_key, profile_projection,
        )
        prompt = self._prompt(safe_input)
        request_body = self._request_body(model, mode, prompt)
        encoded = _canonical_bytes(request_body)
        return {
            "provider_identity": self.provider_identity,
            "model_identity": model,
            "api_mode": mode,
            "prompt_chars": len(prompt),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "request_sha256": hashlib.sha256(encoded).hexdigest(),
            "candidate_count": len(selected),
            "schema_identity": STRUCTURED_CANDIDATE_SCHEMA_IDENTITY,
            "structured_output_mechanism": (
                "json_object" if mode == "chat-completions"
                else "prompt_strict_json"
            ),
            "timeout_seconds": self.timeout_seconds,
            "max_output_tokens": request_body["max_tokens"],
        }

    @staticmethod
    def _retry_after(headers):
        value = headers.get("Retry-After") if headers is not None else None
        if not isinstance(value, str) or not value.isdigit():
            return None
        return min(int(value), MAX_RETRY_AFTER_SECONDS)

    @staticmethod
    def _content(response_body, mode):
        try:
            choices = response_body["choices"]
            choice = choices[0]
            finish_reason = str(choice.get("finish_reason") or "").casefold()
        except (KeyError, IndexError, TypeError, AttributeError) as error:
            raise ProviderAdapterError("INVALID_RESPONSE") from error
        if (finish_reason in REFUSAL_FINISH_REASONS
                or choice.get("refusal") is not None):
            raise ProviderAdapterError("MODEL_REFUSAL")
        try:
            content = (
                choice["text"] if mode == "completions"
                else choice["message"]["content"]
            )
        except (KeyError, TypeError) as error:
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
        )
        prompt = self._prompt(safe_input)
        request_body = self._request_body(model, mode, prompt)
        encoded_request = _canonical_bytes(request_body)
        self.calls.append({
            "provider_identity": self.provider_identity,
            "model_identity": model,
            "api_mode": mode,
            "request_identity": hashlib.sha256(encoded_request).hexdigest(),
            "candidate_ids": [
                item.candidate.candidate_id for item in selected
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
                response_body = json.loads(
                    response.body.decode("utf-8", errors="strict"),
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
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
                }
                self.last_attempt.update({
                    "parse_error_line": error.lineno,
                    "parse_error_column": error.colno,
                    "starts_with_object": raw_candidate.startswith("{"),
                    "ends_with_object": raw_candidate.endswith("}"),
                })
                raise ProviderAdapterError(
                    "INVALID_RESPONSE", subtype="JSON_PARSE",
                ) from error
            self.last_attempt["json_parse_succeeded"] = True
            stage = "candidate_schema"
            try:
                candidate = _parse_structured_candidate(parsed)
            except ProviderAdapterError:
                diagnostics = {
                    "object": isinstance(parsed, dict),
                    "exact_top_level": (
                        isinstance(parsed, dict) and set(parsed) == {
                            "summary", "items", "selected_source_refs",
                        }
                    ),
                    "summary_string": (
                        isinstance(parsed, dict)
                        and isinstance(parsed.get("summary"), str)
                    ),
                    "items_list": (
                        isinstance(parsed, dict)
                        and isinstance(parsed.get("items"), list)
                    ),
                    "item_count": (
                        len(parsed.get("items", []))
                        if isinstance(parsed, dict)
                        and isinstance(parsed.get("items"), list) else None
                    ),
                    "exact_item_shapes": (
                        isinstance(parsed, dict)
                        and isinstance(parsed.get("items"), list)
                        and all(isinstance(item, dict) and set(item) == {
                            "candidate_id", "content_identity", "content",
                            "recommendation_reason", "source_ref_ids",
                        } for item in parsed["items"])
                    ),
                    "source_refs_list": (
                        isinstance(parsed, dict)
                        and isinstance(parsed.get("selected_source_refs"), list)
                    ),
                    "exact_source_ref_shapes": (
                        isinstance(parsed, dict)
                        and isinstance(parsed.get("selected_source_refs"), list)
                        and all(isinstance(ref, dict) and set(ref) == {
                            "source_ref_id", "candidate_id",
                        } for ref in parsed["selected_source_refs"])
                    ),
                }
                raise ProviderAdapterError(
                    "INVALID_RESPONSE", subtype="SCHEMA_MISMATCH",
                )
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
