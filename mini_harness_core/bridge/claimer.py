"""Create immutable Bridge Protocol v1 claim attempts."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os

from .inspector import (
    READY_TO_CLAIM,
    SAFE_TO_RECLAIM_WITH_NEW_NONCE,
    inspect_bridge_task,
)
from .paths import (
    BridgePathReader,
    atomic_rename_no_replace,
    valid_task_id,
)


CLAIMED = "CLAIMED"
TASK_LOCKED = "TASK_LOCKED"
CLAIM_NONCE_EXISTS = "CLAIM_NONCE_EXISTS"
CLAIM_NOT_ALLOWED = "CLAIM_NOT_ALLOWED"


@dataclass(frozen=True, slots=True)
class BridgeClaimResult:
    task_id: str
    claim_nonce: str
    attempt_number: int | None
    previous_claim_nonce: str | None
    status: str
    task_state: str | None = None


def _result(task_id, claim_nonce, status, state=None, attempt=None, previous=None):
    return BridgeClaimResult(
        task_id, claim_nonce, attempt, previous, status, state,
    )


def _release_owned_lock(lock_path, identity):
    """Remove the lock only if the directory is still the one we created."""
    try:
        current = os.lstat(lock_path)
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == identity and os.path.isdir(lock_path):
        os.rmdir(lock_path)


def _ensure_claim_directory(reader, task_id):
    claims_root = reader.path("claims")
    reader.require_directory(claims_root)
    task_directory = reader.path("claims", task_id)
    try:
        os.mkdir(task_directory, 0o700)
    except FileExistsError:
        pass
    return reader.require_directory(task_directory)


def _publish_claim(directory, claim_path, record):
    temporary = os.path.join(
        directory, "." + os.path.basename(claim_path) + ".tmp",
    )
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            json.dump(record, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        atomic_rename_no_replace(temporary, claim_path)
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


def claim_bridge_task(bridge_root, task_id, consumer_id, claim_nonce):
    """Claim attempt ownership; this grants no task execution authority."""
    if not valid_task_id(task_id):
        raise ValueError("task_id is unsafe or invalid")
    if not isinstance(consumer_id, str) or not consumer_id:
        raise ValueError("consumer_id must be a non-empty string")
    if not valid_task_id(claim_nonce):
        raise ValueError("claim_nonce is unsafe or invalid")

    reader = BridgePathReader(bridge_root)
    locks = reader.require_directory(reader.path("locks"))
    lock_path = reader.path("locks", task_id + ".lock")
    try:
        os.mkdir(lock_path, 0o700)
    except FileExistsError:
        # Existing symlinks are checked fail-closed before reporting contention.
        reader.exists(lock_path)
        return _result(task_id, claim_nonce, TASK_LOCKED)

    lock_stat = os.lstat(lock_path)
    lock_identity = (lock_stat.st_dev, lock_stat.st_ino)
    try:
        state = inspect_bridge_task(reader.root, task_id)
        claim_path = reader.path("claims", task_id, claim_nonce + ".json")
        if reader.exists(claim_path):
            return _result(
                task_id, claim_nonce, CLAIM_NONCE_EXISTS, state.state,
            )
        if state.state == READY_TO_CLAIM:
            attempt = 1
            previous = None
        elif state.state == SAFE_TO_RECLAIM_WITH_NEW_NONCE:
            attempt = state.latest_attempt + 1
            previous = state.latest_claim_nonce
        else:
            return _result(
                task_id, claim_nonce, CLAIM_NOT_ALLOWED, state.state,
            )

        directory = _ensure_claim_directory(reader, task_id)
        # Re-check after directory creation to retain no-overwrite semantics.
        if reader.exists(claim_path):
            return _result(
                task_id, claim_nonce, CLAIM_NONCE_EXISTS, state.state,
            )
        record = {
            "claim_schema_version": 1,
            "task_id": task_id,
            "consumer_id": consumer_id,
            "claim_nonce": claim_nonce,
            "attempt_number": attempt,
            "previous_claim_nonce": previous,
            "claimed_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            _publish_claim(directory, claim_path, record)
        except FileExistsError:
            return _result(
                task_id, claim_nonce, CLAIM_NONCE_EXISTS, state.state,
            )
        return _result(
            task_id, claim_nonce, CLAIMED, state.state, attempt, previous,
        )
    finally:
        _release_owned_lock(lock_path, lock_identity)
