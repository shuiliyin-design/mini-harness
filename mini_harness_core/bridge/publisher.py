"""Publish Task v1 records to a Bridge inbox, without granting authority."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import re
import uuid

from .paths import (
    BridgePathReader,
    atomic_rename_no_replace,
    valid_task_id,
)


PUBLISHED = "PUBLISHED"
TASK_ALREADY_EXISTS = "TASK_ALREADY_EXISTS"

_SENSITIVE_KEYS = frozenset({
    "authorization", "apikey", "privatekey", "hiddenreasoning",
    "chainofthought", "rawtooloutput",
})
_ENV_KEYS = frozenset({"env", "environ", "environment"})
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")


@dataclass(frozen=True, slots=True)
class BridgePublishResult:
    task_id: str
    task_path: str
    ready_path: str
    status: str


def _normalized_key(value):
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _screen_payload(value, path="payload"):
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path}: JSON object keys must be strings")
            normalized = _normalized_key(key)
            if normalized in _SENSITIVE_KEYS or normalized.endswith("apikey"):
                raise ValueError(f"{path}.{key}: sensitive field is not allowed")
            if normalized in _ENV_KEYS and isinstance(child, dict):
                raise ValueError(f"{path}.{key}: raw environment dump is not allowed")
            _screen_payload(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _screen_payload(child, f"{path}[{index}]")
    elif isinstance(value, str):
        if re.search(r"\b(?:Authorization|Bearer)\b", value, re.IGNORECASE):
            raise ValueError(f"{path}: authorization secret is not allowed")
        if re.search(r"\bapi[ _-]?key\b", value, re.IGNORECASE):
            raise ValueError(f"{path}: API key material is not allowed")
        if re.search(r"\b(?:hidden reasoning|chain of thought)\b", value, re.IGNORECASE):
            raise ValueError(f"{path}: hidden reasoning is not allowed")
        if re.search(r"\braw (?:tool )?output\b", value, re.IGNORECASE):
            raise ValueError(f"{path}: raw tool output is not allowed")
        if re.search(r"\b(?:raw environment|environment dump)\b", value, re.IGNORECASE):
            raise ValueError(f"{path}: raw environment dump is not allowed")
        if _PRIVATE_KEY.search(value):
            raise ValueError(f"{path}: private key is not allowed")


def _json_bytes(value):
    try:
        return (json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        ) + "\n").encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("payload must be JSON-compatible structured data") from error


def _publish_file(directory, final_path, content, nonce):
    temporary = os.path.join(
        directory, "." + os.path.basename(final_path) + ".tmp." + nonce,
    )
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        atomic_rename_no_replace(temporary, final_path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def publish_bridge_task(
    bridge_root, task_id, task_type, payload, publisher_id=None,
):
    """Publish one Task v1 record; this is not approval, claim, or execution."""
    if not valid_task_id(task_id):
        raise ValueError("task_id is unsafe or invalid")
    if not isinstance(task_type, str) or not task_type:
        raise ValueError("task_type must be a non-empty string")
    if publisher_id is not None and (
        not isinstance(publisher_id, str) or not publisher_id
    ):
        raise ValueError("publisher_id must be a non-empty string")
    _screen_payload(payload)
    payload_bytes = _json_bytes(payload)

    reader = BridgePathReader(bridge_root)
    inbox = reader.path("inbox")
    directory = reader.require_directory(inbox)
    task_path = reader.path("inbox", task_id + ".json")
    ready_path = reader.path("inbox", task_id + ".ready")
    if reader.exists(task_path) or reader.exists(ready_path):
        return BridgePublishResult(task_id, task_path, ready_path, TASK_ALREADY_EXISTS)

    record = {
        "task_schema_version": 1,
        "task_id": task_id,
        "task_type": task_type,
        "payload": json.loads(payload_bytes),
        "publisher_id": publisher_id or "bridge-publisher",
        "published_at": datetime.now(timezone.utc).isoformat(),
    }
    nonce = uuid.uuid4().hex
    try:
        _publish_file(directory, task_path, _json_bytes(record), nonce)
        _publish_file(directory, ready_path, b"", nonce)
    except FileExistsError:
        return BridgePublishResult(task_id, task_path, ready_path, TASK_ALREADY_EXISTS)
    return BridgePublishResult(task_id, task_path, ready_path, PUBLISHED)
