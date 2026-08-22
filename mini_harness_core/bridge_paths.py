"""Shared fail-closed path checks for Bridge protocol tools."""

import ctypes
import errno
import json
import os
import re
import stat


TASK_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def atomic_rename_no_replace(source, destination):
    """Atomically rename without replacing an existing destination."""
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError:
        renameat2 = None
    if renameat2 is not None:
        renameat2.argtypes = (
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        result = renameat2(
            -100, os.fsencode(source), -100, os.fsencode(destination), 1,
        )
        if result == 0:
            return
        error_number = ctypes.get_errno()
        if error_number != errno.ENOSYS:
            raise OSError(error_number, os.strerror(error_number), destination)

    # Hard-link publication preserves atomic, no-replace visibility when the
    # platform does not expose renameat2.
    os.link(source, destination, follow_symlinks=False)
    os.unlink(source)


def valid_task_id(task_id):
    return (
        isinstance(task_id, str)
        and task_id not in {".", ".."}
        and "\x00" not in task_id
        and "/" not in task_id
        and "\\" not in task_id
        and not os.path.isabs(task_id)
        and TASK_ID_PATTERN.fullmatch(task_id) is not None
    )


class BridgePathReader:
    """Resolve every observed path beneath one canonical Bridge root."""

    def __init__(self, bridge_root):
        if not isinstance(bridge_root, (str, os.PathLike)):
            raise ValueError("bridge_root must be a filesystem path")
        lexical = os.path.abspath(os.fspath(bridge_root))
        self.root = os.path.realpath(lexical)
        if not os.path.isdir(self.root):
            raise ValueError("bridge_root is not a directory")

    def path(self, *parts):
        lexical = os.path.abspath(os.path.join(self.root, *parts))
        try:
            if os.path.commonpath((self.root, lexical)) != self.root:
                raise ValueError("path escapes bridge_root")
        except ValueError as error:
            raise ValueError("path cannot be contained in bridge_root") from error
        return lexical

    def checked_existing(self, path):
        resolved = os.path.realpath(path)
        try:
            contained = os.path.commonpath((self.root, resolved)) == self.root
        except ValueError:
            contained = False
        if not contained:
            raise ValueError("symlink escapes bridge_root: " + path)
        return resolved

    def exists(self, path):
        if not os.path.lexists(path):
            return False
        self.checked_existing(path)
        return True

    def require_directory(self, path):
        if not os.path.lexists(path):
            raise ValueError("expected directory: " + path)
        resolved = self.checked_existing(path)
        if not os.path.isdir(resolved):
            raise ValueError("expected directory: " + path)
        return resolved

    def list_json(self, directory):
        if not os.path.lexists(directory):
            return []
        resolved = self.require_directory(directory)
        paths = []
        with os.scandir(resolved) as entries:
            for entry in entries:
                if entry.name.endswith(".json"):
                    paths.append(self.path(os.path.relpath(resolved, self.root), entry.name))
        return sorted(paths)

    def read_json(self, path):
        if not os.path.lexists(path):
            raise FileNotFoundError(path)
        resolved = self.checked_existing(path)
        mode = os.stat(resolved).st_mode
        if not stat.S_ISREG(mode):
            raise ValueError("expected regular file: " + path)
        with open(resolved, "r", encoding="utf-8") as stream:
            return json.load(stream)
