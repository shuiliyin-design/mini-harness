"""Offline/Brave Search adapters; safe observations are not Evidence."""

from dataclasses import dataclass
from datetime import datetime
import copy
import hashlib
import json
import os
import socket
import unicodedata
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from mini_harness_core.mcp import MCPClient
from mini_harness_core.security import SECRET_PATTERNS

from ..domain import DomainError, canonicalize_url, normalize_topic


BRAVE_SEARCH_API_KEY = "BRAVE_SEARCH_API_KEY"
BRAVE_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
BRAVE_USER_AGENT = "mini-harness-digest-agent/1.0"
DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 1_000_000
MAX_QUERY_CHARS = 400
MAX_QUERY_WORDS = 50
MAX_RESULTS = 20
MAX_TITLE_CHARS = 240
MAX_SNIPPET_CHARS = 320
MAX_RETRY_AFTER_SECONDS = 3_600
SEARCH_ERROR_CODES = frozenset({
    "CONFIGURATION_ERROR", "TIMEOUT", "RATE_LIMITED", "AUTH_FAILED",
    "NETWORK_ERROR", "INVALID_RESPONSE", "OVERSIZED_RESPONSE",
    "EMPTY_RESULTS",
})
RETRYABLE_SEARCH_ERRORS = frozenset({
    "TIMEOUT", "RATE_LIMITED", "NETWORK_ERROR",
})


class SearchAdapterError(RuntimeError):
    """One allowlisted failure code; raw provider details never escape."""

    def __init__(self, code, *, retry_after_seconds=None):
        if code not in SEARCH_ERROR_CODES:
            raise ValueError("unknown Search adapter error code")
        self.code = code
        self.retryable = code in RETRYABLE_SEARCH_ERRORS
        self.retry_after_seconds = retry_after_seconds
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SearchHTTPResponse:
    """Transient transport response; only the adapter may inspect it."""

    status: int
    headers: object
    body: bytes


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibSearchTransport:
    """Fixed GET transport with no redirects and a bounded body read."""

    def __init__(self, opener=None):
        self.opener = opener or urllib_request.build_opener(_NoRedirectHandler())

    def get(self, url, headers, timeout, maximum_bytes):
        request = urllib_request.Request(url, headers=dict(headers), method="GET")
        try:
            with self.opener.open(request, timeout=timeout) as response:
                length = response.headers.get("Content-Length")
                if length is not None:
                    try:
                        if int(length) > maximum_bytes:
                            raise SearchAdapterError("OVERSIZED_RESPONSE")
                    except ValueError:
                        pass
                body = response.read(maximum_bytes + 1)
                if len(body) > maximum_bytes:
                    raise SearchAdapterError("OVERSIZED_RESPONSE")
                return SearchHTTPResponse(
                    int(response.status), response.headers, body,
                )
        except urllib_error.HTTPError as error:
            # Error bodies are deliberately not read.
            return SearchHTTPResponse(int(error.code), error.headers, b"")
        except (TimeoutError, socket.timeout) as error:
            raise SearchAdapterError("TIMEOUT") from error
        except urllib_error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise SearchAdapterError("TIMEOUT") from error
            raise SearchAdapterError("NETWORK_ERROR") from error
        except SearchAdapterError:
            raise
        except OSError as error:
            raise SearchAdapterError("NETWORK_ERROR") from error


def _canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _identity(value):
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def normalize_search_query(value):
    if not isinstance(value, str):
        raise SearchAdapterError("INVALID_RESPONSE")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise SearchAdapterError("INVALID_RESPONSE")
    normalized = unicodedata.normalize("NFC", " ".join(value.split()))
    try:
        normalized.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise SearchAdapterError("INVALID_RESPONSE") from error
    if (not normalized or len(normalized) > MAX_QUERY_CHARS
            or len(normalized.split()) > MAX_QUERY_WORDS
            or any(pattern.search(normalized) for pattern in SECRET_PATTERNS)):
        raise SearchAdapterError("INVALID_RESPONSE")
    return normalized


def search_query_identity(query):
    return hashlib.sha256(
        normalize_search_query(query).encode("utf-8"),
    ).hexdigest()


def _result_limit(value):
    if (not isinstance(value, int) or isinstance(value, bool)
            or not 1 <= value <= MAX_RESULTS):
        raise SearchAdapterError("INVALID_RESPONSE")
    return value


def _bounded_text(value, maximum):
    if not isinstance(value, str):
        raise ValueError("invalid text")
    text = " ".join(value.split()).strip()
    if not text:
        raise ValueError("empty text")
    return text[:maximum]


def _published_at(value):
    if not isinstance(value, str) or not value:
        return None
    # Brave documents `age`, but only ISO-like values are treated as reliable.
    text = value.strip()
    if len(text) > 80 or "T" not in text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return text


def _safe_topic_tags(value):
    if not isinstance(value, (list, tuple)):
        return []
    tags = []
    for item in value[:10]:
        try:
            tag = normalize_topic(item)
        except (DomainError, TypeError):
            continue
        if tag not in tags:
            tags.append(tag)
    return tags


def _normalize_rows(rows, maximum, *, allow_fixture_topics=False):
    if not isinstance(rows, list):
        raise SearchAdapterError("INVALID_RESPONSE")
    results, seen_urls = [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            url = canonicalize_url(row.get("url"))
            title = _bounded_text(row.get("title"), MAX_TITLE_CHARS)
            snippet = _bounded_text(
                row.get("description", row.get("snippet")),
                MAX_SNIPPET_CHARS,
            )
        except (DomainError, TypeError, ValueError):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        item = {
            "source_id": hashlib.sha256(url.encode("utf-8")).hexdigest(),
            "title": title,
            "url": url,
            "snippet": snippet,
            "topic_tags": (
                _safe_topic_tags(row.get("topic_tags"))
                if allow_fixture_topics else []
            ),
        }
        published = _published_at(
            row.get("published_at", row.get("age")),
        )
        if published is not None:
            item["published_at"] = published
        results.append(item)
        if len(results) == maximum:
            break
    if not results:
        raise SearchAdapterError("EMPTY_RESULTS")
    return results


def _safe_search_result(provider, query, maximum, rows, response_metadata,
                        *, allow_fixture_topics=False):
    normalized_query = normalize_search_query(query)
    maximum = _result_limit(maximum)
    normalized = _normalize_rows(
        rows, maximum, allow_fixture_topics=allow_fixture_topics,
    )
    safe = {
        "provider": provider,
        "query_identity": search_query_identity(normalized_query),
        "result_count": len(normalized),
        "request_metadata": {"result_limit": maximum},
        "response_metadata": copy.deepcopy(response_metadata),
        "results": normalized,
    }
    safe["observation_identity"] = _identity(safe)
    return safe


def validate_safe_search_result(value, query, maximum):
    """Validate exact cross-adapter output and its deterministic identities."""
    if not isinstance(value, dict) or set(value) != {
        "provider", "query_identity", "result_count", "request_metadata",
        "response_metadata", "results", "observation_identity",
    }:
        raise SearchAdapterError("INVALID_RESPONSE")
    if value["provider"] not in {"fake", "brave"}:
        raise SearchAdapterError("INVALID_RESPONSE")
    if (not isinstance(value["query_identity"], str)
            or len(value["query_identity"]) != 64
            or value["query_identity"] != search_query_identity(query)):
        raise SearchAdapterError("INVALID_RESPONSE")
    maximum = _result_limit(maximum)
    request_metadata = value["request_metadata"]
    if (not isinstance(request_metadata, dict)
            or set(request_metadata) != {"result_limit"}
            or type(request_metadata["result_limit"]) is not int
            or request_metadata["result_limit"] != maximum):
        raise SearchAdapterError("INVALID_RESPONSE")
    if (not isinstance(value["results"], list)
            or type(value["result_count"]) is not int
            or value["result_count"] != len(value["results"])
            or not 1 <= value["result_count"] <= maximum):
        raise SearchAdapterError("INVALID_RESPONSE")
    metadata = value["response_metadata"]
    if not isinstance(metadata, dict) or set(metadata) != {
        "http_status", "response_bytes", "retry_after_seconds",
    }:
        raise SearchAdapterError("INVALID_RESPONSE")
    if (metadata["http_status"] is not None
            and (type(metadata["http_status"]) is not int
                 or not 100 <= metadata["http_status"] <= 599)):
        raise SearchAdapterError("INVALID_RESPONSE")
    if (type(metadata["response_bytes"]) is not int
            or not 0 <= metadata["response_bytes"] <= 5_000_000):
        raise SearchAdapterError("INVALID_RESPONSE")
    retry_after = metadata["retry_after_seconds"]
    if (retry_after is not None
            and (type(retry_after) is not int
                 or not 0 <= retry_after <= MAX_RETRY_AFTER_SECONDS)):
        raise SearchAdapterError("INVALID_RESPONSE")
    if (not isinstance(value["observation_identity"], str)
            or len(value["observation_identity"]) != 64):
        raise SearchAdapterError("INVALID_RESPONSE")
    stable = {key: copy.deepcopy(item) for key, item in value.items()
              if key != "observation_identity"}
    if value["observation_identity"] != _identity(stable):
        raise SearchAdapterError("INVALID_RESPONSE")
    checked = _normalize_rows(
        copy.deepcopy(value["results"]), maximum,
        allow_fixture_topics=True,
    )
    if checked != value["results"]:
        raise SearchAdapterError("INVALID_RESPONSE")
    return copy.deepcopy(value)


class FakeSearchClient(MCPClient):
    """MCP-shaped Fake Search with fixed, injectable result fixtures."""

    provider = "fake"

    def __init__(self, results):
        self.results = tuple(copy.deepcopy(tuple(results)))
        self.calls = []
        self.last_safe_result = None

    def list_tools(self):
        return _search_tool_catalog("Offline deterministic web search fixture")

    def call_tool(self, name, arguments):
        if name != "web_search":
            raise ValueError("Fake Search tool 不存在")
        if not isinstance(arguments, dict) or set(arguments) != {
            "query", "max_results",
        }:
            raise SearchAdapterError("INVALID_RESPONSE")
        query = normalize_search_query(arguments.get("query"))
        maximum = _result_limit(arguments.get("max_results"))
        self.calls.append({
            "query_identity": search_query_identity(query),
            "result_limit": maximum,
        })
        raw_size = len(_canonical_bytes(list(self.results)))
        result = _safe_search_result(
            "fake", query, maximum, copy.deepcopy(list(self.results)),
            {"http_status": None, "response_bytes": raw_size,
             "retry_after_seconds": None},
            allow_fixture_topics=True,
        )
        self.last_safe_result = copy.deepcopy(result)
        return result


class BraveSearchClient(MCPClient):
    """App-owned Brave Web Search adapter with an injectable HTTP transport."""

    provider = "brave"

    def __init__(self, *, transport=None, environ=None,
                 timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                 maximum_response_bytes=MAX_RESPONSE_BYTES):
        if (not isinstance(timeout_seconds, (int, float))
                or isinstance(timeout_seconds, bool)
                or not 0 < timeout_seconds <= 30):
            raise ValueError("invalid Brave timeout")
        if (not isinstance(maximum_response_bytes, int)
                or isinstance(maximum_response_bytes, bool)
                or not 1_024 <= maximum_response_bytes <= 5_000_000):
            raise ValueError("invalid Brave response bound")
        self.transport = transport or UrllibSearchTransport()
        self.environ = os.environ if environ is None else environ
        self.timeout_seconds = float(timeout_seconds)
        self.maximum_response_bytes = maximum_response_bytes
        self.calls = []
        self.last_safe_result = None
        self.last_error = None

    @classmethod
    def from_environment(cls, **kwargs):
        return cls(**kwargs)

    def list_tools(self):
        return _search_tool_catalog("Brave Web Search safe normalized results")

    def _credential(self):
        key = self.environ.get(BRAVE_SEARCH_API_KEY)
        if (not isinstance(key, str) or key != key.strip()
                or not 8 <= len(key) <= 512
                or any(unicodedata.category(ch) == "Cc" for ch in key)):
            raise SearchAdapterError("CONFIGURATION_ERROR")
        return key

    @staticmethod
    def _retry_after(headers):
        value = headers.get("Retry-After") if headers is not None else None
        if not isinstance(value, str) or not value.isdigit():
            return None
        return min(int(value), MAX_RETRY_AFTER_SECONDS)

    def call_tool(self, name, arguments):
        if name != "web_search":
            raise ValueError("Brave Search tool 不存在")
        if not isinstance(arguments, dict) or set(arguments) != {
            "query", "max_results",
        }:
            raise SearchAdapterError("INVALID_RESPONSE")
        query = normalize_search_query(arguments.get("query"))
        maximum = _result_limit(arguments.get("max_results"))
        query_id = search_query_identity(query)
        self.calls.append({
            "query_identity": query_id, "result_limit": maximum,
        })
        self.last_safe_result = None
        self.last_error = None
        try:
            key = self._credential()
            url = BRAVE_SEARCH_ENDPOINT + "?" + urllib_parse.urlencode({
                "q": query, "count": maximum, "safesearch": "moderate",
            })
            response = self.transport.get(
                url,
                {"Accept": "application/json",
                 "User-Agent": BRAVE_USER_AGENT,
                 "X-Subscription-Token": key},
                self.timeout_seconds, self.maximum_response_bytes,
            )
            if (not isinstance(response, SearchHTTPResponse)
                    or type(response.status) is not int
                    or not hasattr(response.headers, "get")
                    or not isinstance(response.body, bytes)):
                raise SearchAdapterError("INVALID_RESPONSE")
            retry_after = self._retry_after(response.headers)
            if response.status == 429:
                raise SearchAdapterError(
                    "RATE_LIMITED", retry_after_seconds=retry_after,
                )
            if response.status in {401, 403}:
                raise SearchAdapterError("AUTH_FAILED")
            if 500 <= response.status <= 599:
                raise SearchAdapterError("NETWORK_ERROR")
            if response.status != 200:
                raise SearchAdapterError("INVALID_RESPONSE")
            if len(response.body) > self.maximum_response_bytes:
                raise SearchAdapterError("OVERSIZED_RESPONSE")
            try:
                decoded = response.body.decode("utf-8", errors="strict")
                payload = json.loads(decoded)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise SearchAdapterError("INVALID_RESPONSE") from error
            web = payload.get("web") if isinstance(payload, dict) else None
            rows = web.get("results") if isinstance(web, dict) else None
            result = _safe_search_result(
                "brave", query, maximum, rows,
                {"http_status": 200, "response_bytes": len(response.body),
                 "retry_after_seconds": retry_after},
            )
            self.last_safe_result = copy.deepcopy(result)
            return result
        except SearchAdapterError as error:
            self.last_error = {
                "code": error.code, "retryable": error.retryable,
                "retry_after_seconds": error.retry_after_seconds,
                "query_identity": query_id,
            }
            raise


def _search_tool_catalog(description):
    return [{
        "name": "web_search", "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["query", "max_results"],
            "additionalProperties": False,
        },
    }]
