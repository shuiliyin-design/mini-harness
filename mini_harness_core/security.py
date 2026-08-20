"""Shared deterministic patterns for screening persisted untrusted content."""

import re


SECRET_PATTERNS = (
    re.compile(r"\b(?:api[_ -]?key|token|password|authorization|bearer)\b", re.I),
    re.compile(r"\bprivate\s+key\b|-----BEGIN [A-Z ]*PRIVATE KEY-----", re.I),
    re.compile(r"(?:^|[/\\])\.env\.local\b|\bLLM_API_KEY\b", re.I),
    re.compile(
        r"\b(?:credential|credentials|client_secret|access_token|refresh_token|"
        r"secret_key)\b\s*[:=]",
        re.I,
    ),
    re.compile(r"\b(?:sk|ghp|xox[baprs])[-_][A-Za-z0-9_-]{8,}\b", re.I),
)
