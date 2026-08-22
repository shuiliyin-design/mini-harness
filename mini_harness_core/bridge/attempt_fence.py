"""Atomic per-attempt exclusion for execution and reconciliation.

The fence is deliberately not a lease.  A crash may leave the directory in
place, and v1 then fails closed until an operator performs explicit recovery.
"""

from dataclasses import dataclass
import hashlib
import os

from .paths import BridgePathReader, valid_task_id


ATTEMPT_FENCE_ACQUIRED = "ATTEMPT_FENCE_ACQUIRED"
ATTEMPT_FENCE_LOCKED = "ATTEMPT_FENCE_LOCKED"


@dataclass(slots=True)
class BridgeAttemptFence:
    path: str
    acquired: bool
    device: int | None = None
    inode: int | None = None

    def release(self):
        """Remove only the exact directory created by this owner."""
        if not self.acquired:
            return False
        try:
            current = os.lstat(self.path)
        except FileNotFoundError:
            self.acquired = False
            return False
        if not os.path.isdir(self.path) or (
            current.st_dev, current.st_ino
        ) != (self.device, self.inode):
            self.acquired = False
            return False
        try:
            os.rmdir(self.path)
        except OSError:
            return False
        self.acquired = False
        return True


def attempt_fence_path(reader, task_id, claim_nonce):
    if not valid_task_id(task_id) or not valid_task_id(claim_nonce):
        raise ValueError("unsafe Bridge attempt identity")
    reader.require_directory(reader.path("locks"))
    return reader.path(
        "locks", f"{task_id}.{claim_nonce}.attempt.lock",
    )


def harness_terminal_marker_path(reader, task_id, claim_nonce):
    """Return the immutable marker that excludes Bridge reconciliation.

    The hashed filename avoids exceeding filesystem component limits while the
    marker content (owned by the Adapter) retains the full identities.
    """
    if not valid_task_id(task_id) or not valid_task_id(claim_nonce):
        raise ValueError("unsafe Bridge attempt identity")
    reader.require_directory(reader.path("locks"))
    identity = hashlib.sha256(
        (task_id + "\0" + claim_nonce).encode("utf-8")
    ).hexdigest()
    return reader.path("locks", f"harness-terminal-{identity}.json")


def harness_terminal_truth_exists(bridge_root, task_id, claim_nonce):
    """Fail-closed indication that Harness terminal truth outranks reconcile."""
    reader = (
        bridge_root if isinstance(bridge_root, BridgePathReader)
        else BridgePathReader(bridge_root)
    )
    return reader.exists(harness_terminal_marker_path(
        reader, task_id, claim_nonce,
    ))


def acquire_bridge_attempt_fence(bridge_root, task_id, claim_nonce):
    """Try once to own an attempt; never wait, steal, or clean stale locks."""
    reader = (
        bridge_root if isinstance(bridge_root, BridgePathReader)
        else BridgePathReader(bridge_root)
    )
    path = attempt_fence_path(reader, task_id, claim_nonce)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        # Existing directories, files, and symlinks all fail closed.
        return BridgeAttemptFence(path, False), ATTEMPT_FENCE_LOCKED
    identity = os.lstat(path)
    return (
        BridgeAttemptFence(path, True, identity.st_dev, identity.st_ino),
        ATTEMPT_FENCE_ACQUIRED,
    )
