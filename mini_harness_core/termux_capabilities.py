"""Narrow environment adapters for fixed Termux:API capabilities.

This registry describes transport metadata only.  It grants no Harness policy,
approval, AuthorizedAction, or execution authority.
"""

import hashlib
import json
import os
import subprocess

from .security import SECRET_PATTERNS
from .environment_adapters import (
    CAPABILITY_NOT_INSTALLED, COMPANION_UNAVAILABLE, EXECUTION_FAILED,
    INVALID_ARGUMENT, INVALID_RESPONSE, TIMEOUT, UNSUPPORTED_CAPABILITY,
    EnvironmentAdapterResult,
)


BATTERY_EXECUTABLE = "/data/data/com.termux/files/usr/bin/termux-battery-status"
NOTIFICATION_EXECUTABLE = "/data/data/com.termux/files/usr/bin/termux-notification"

DEFAULT_TIMEOUT_SECONDS = 10
MAX_OUTPUT_BYTES = 64 * 1024
MAX_NOTIFICATION_TITLE_BYTES = 256
MAX_NOTIFICATION_CONTENT_BYTES = 4096

ANDROID_ENV_ALLOWLIST = (
    "ANDROID_ART_ROOT", "ANDROID_DATA", "ANDROID_I18N_ROOT", "ANDROID_ROOT",
    "ANDROID_TZDATA_ROOT", "BOOTCLASSPATH", "DEX2OATBOOTCLASSPATH",
    "EXTERNAL_STORAGE", "LANG", "LC_ALL", "LC_CTYPE",
)

BATTERY_FIELDS = frozenset({
    "present", "technology", "health", "plugged", "status",
    "temperature", "voltage", "current", "percentage", "level", "scale",
    "charge_counter", "cycle",
})

LOGICAL_CAPABILITY = "termux:battery_status"
NOTIFICATION_LOGICAL_CAPABILITY = "termux:notification"


def _bytes(value):
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8", errors="replace")


def _digest_fields(stdout, stderr):
    return {
        "stdout_length": len(stdout),
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_length": len(stderr),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
    }


def _result(status, stdout=b"", stderr=b"", exit_code=None,
            observation=None, error_code=None):
    return EnvironmentAdapterResult(
        LOGICAL_CAPABILITY, status, "read_only",
        "not_started" if error_code == CAPABILITY_NOT_INSTALLED else
        "no_side_effect", observation or {}, exit_code,
        **_digest_fields(stdout, stderr), error_code=error_code,
    )


def _has_secret(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def _companion_unavailable(stderr):
    text = stderr.decode("utf-8", errors="replace").lower()
    indicators = (
        "termux:api is not available", "termux:api is not yet available",
        "termux-api package is not installed", "companion app",
        "could not connect", "connection refused",
    )
    return any(indicator in text for indicator in indicators)


def _termux_environment():
    environment = {
        name: os.environ[name]
        for name in ANDROID_ENV_ALLOWLIST if name in os.environ
    }
    environment.update({
        "PATH": "/data/data/com.termux/files/usr/bin:/system/bin",
        "PREFIX": "/data/data/com.termux/files/usr",
    })
    return environment


def _validated_notification_text(name, value, maximum):
    if not isinstance(value, str):
        raise ValueError(f"notification {name} must be a string")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError(f"notification {name} must be valid UTF-8") from error
    if not encoded or len(encoded) > maximum:
        raise ValueError(f"notification {name} size is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"notification {name} contains control characters")
    if _has_secret(value):
        raise ValueError(f"notification {name} failed secret screening")
    return value


def validate_termux_notification_arguments(arguments):
    if not isinstance(arguments, dict) or set(arguments) != {"title", "content"}:
        raise ValueError("notification arguments schema is invalid")
    return {
        "title": _validated_notification_text(
            "title", arguments["title"], MAX_NOTIFICATION_TITLE_BYTES,
        ),
        "content": _validated_notification_text(
            "content", arguments["content"], MAX_NOTIFICATION_CONTENT_BYTES,
        ),
    }


def _notification_result(status, stdout=b"", stderr=b"", exit_code=None,
                         error_code=None, effect_certainty=None):
    observation = ({
            "notification_requested": True,
            "request_accepted": True,
        } if status == "succeeded" else {})
    certainty = {
        "request_accepted": "known_applied",
        "not_started": "not_started", "unknown": "unknown",
    }.get(effect_certainty, effect_certainty or "unknown")
    return EnvironmentAdapterResult(
        NOTIFICATION_LOGICAL_CAPABILITY, status, "side_effecting", certainty,
        observation, exit_code, **_digest_fields(stdout, stderr),
        error_code=error_code,
    )


def invoke_termux_notification(title, content):
    """Submit one fixed notification request; this function grants no Authority."""
    validated = validate_termux_notification_arguments(
        {"title": title, "content": content},
    )
    safe_title, safe_content = validated["title"], validated["content"]
    argv = [
        NOTIFICATION_EXECUTABLE,
        "--title", safe_title,
        "--content", safe_content,
    ]
    try:
        completed = subprocess.run(
            argv, shell=False, stdin=subprocess.DEVNULL, capture_output=True,
            timeout=DEFAULT_TIMEOUT_SECONDS, check=False,
            env=_termux_environment(),
        )
    except FileNotFoundError:
        return _notification_result(
            "failed", error_code=CAPABILITY_NOT_INSTALLED,
            effect_certainty="not_started",
        )
    except subprocess.TimeoutExpired as error:
        stdout, stderr = _bytes(error.stdout), _bytes(error.stderr)
        return _notification_result(
            "failed", stdout[:MAX_OUTPUT_BYTES], stderr[:MAX_OUTPUT_BYTES],
            error_code=TIMEOUT, effect_certainty="unknown",
        )
    except OSError:
        return _notification_result(
            "failed", error_code=EXECUTION_FAILED,
            effect_certainty="unknown",
        )
    stdout, stderr = _bytes(completed.stdout), _bytes(completed.stderr)
    if len(stdout) > MAX_OUTPUT_BYTES or len(stderr) > MAX_OUTPUT_BYTES:
        return _notification_result(
            "failed", stdout[:MAX_OUTPUT_BYTES], stderr[:MAX_OUTPUT_BYTES],
            completed.returncode, EXECUTION_FAILED, "unknown",
        )
    if completed.returncode != 0:
        code = (COMPANION_UNAVAILABLE if _companion_unavailable(stderr)
                else EXECUTION_FAILED)
        return _notification_result(
            "failed", stdout, stderr, completed.returncode, code, "unknown",
        )
    return _notification_result(
        "succeeded", stdout, stderr, completed.returncode,
        effect_certainty="request_accepted",
    )


def _parse_battery(stdout):
    try:
        decoded = stdout.decode("utf-8", errors="strict")
        document = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    observation = {}
    for key in BATTERY_FIELDS:
        if key not in document:
            continue
        value = document[key]
        if value is None or isinstance(value, (bool, int, float, str)):
            observation[key] = value
        else:
            return None
    if not observation or _has_secret(observation):
        return None
    return observation


def invoke_termux_capability(capability_name):
    """Invoke one registry-owned capability with no caller-controlled argv."""
    if capability_name != "battery_status":
        raise ValueError("unknown Termux capability")
    try:
        completed = subprocess.run(
            [BATTERY_EXECUTABLE], shell=False, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=DEFAULT_TIMEOUT_SECONDS,
            check=False, env=_termux_environment(),
        )
    except FileNotFoundError:
        return _result("failed", error_code=CAPABILITY_NOT_INSTALLED)
    except subprocess.TimeoutExpired as error:
        stdout, stderr = _bytes(error.stdout), _bytes(error.stderr)
        return _result("failed", stdout[:MAX_OUTPUT_BYTES],
                       stderr[:MAX_OUTPUT_BYTES], None, error_code=TIMEOUT)
    except OSError:
        return _result("failed", error_code=EXECUTION_FAILED)

    stdout, stderr = _bytes(completed.stdout), _bytes(completed.stderr)
    if len(stdout) > MAX_OUTPUT_BYTES or len(stderr) > MAX_OUTPUT_BYTES:
        return _result("failed", stdout[:MAX_OUTPUT_BYTES],
                       stderr[:MAX_OUTPUT_BYTES], completed.returncode,
                       error_code=INVALID_RESPONSE)
    if completed.returncode != 0:
        code = (COMPANION_UNAVAILABLE if _companion_unavailable(stderr)
                else EXECUTION_FAILED)
        return _result("failed", stdout, stderr, completed.returncode,
                       error_code=code)
    observation = _parse_battery(stdout)
    if observation is None:
        return _result("failed", stdout, stderr, completed.returncode,
                       error_code=INVALID_RESPONSE)
    return _result("succeeded", stdout, stderr, completed.returncode,
                   observation=observation)
