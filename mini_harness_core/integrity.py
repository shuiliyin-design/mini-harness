"""Stable low-level helpers for immutable Harness JSON records."""

import hashlib
import json
import os
import tempfile


class ImmutableRecordConflict(ValueError):
    """An immutable path already contains different bytes."""


def canonical_json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_identity(value):
    payload = value if isinstance(value, bytes) else canonical_json_bytes(value)
    return hashlib.sha256(payload).hexdigest()


def verify_immutable_record(path, payload):
    """Return whether *path* exists with exactly *payload* bytes."""
    try:
        with open(path, "rb") as stream:
            existing = stream.read()
    except FileNotFoundError:
        return False
    if existing != payload:
        raise ImmutableRecordConflict("immutable record duplicate conflict")
    return True


def atomic_json_publish(path, value, *, temporary_prefix=".tmp-",
                        temporary_suffix=".tmp", directory_mode=0o700):
    """Exclusively publish one canonical immutable JSON record."""
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=directory_mode, exist_ok=True)
    payload = canonical_json_bytes(value) + b"\n"
    if verify_immutable_record(path, payload):
        return path
    descriptor, temporary = tempfile.mkstemp(
        prefix=temporary_prefix, suffix=temporary_suffix, dir=directory,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            verify_immutable_record(path, payload)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return path
