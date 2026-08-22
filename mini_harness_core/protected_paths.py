"""Harness-owned protected path ceiling.

This module answers only one question: may an already parsed capability request
name these workspace paths?  It does not grant execution authority.
"""

from dataclasses import dataclass
import os
import shlex


PROTECTED_DIRECTORIES = frozenset({".audit", ".sessions"})
PROTECTED_EXACT_FILES = frozenset({".env", "id_rsa", "id_ed25519"})
PROTECTED_SUFFIXES = (".pem", ".key")
PROTECTED_PREFIXES = ("credentials", "token", "secret")
SHELL_OPERATORS = frozenset({"&&", "||", ";", "|", "&", ">", ">>", "<", "<<"})
READ_COMMANDS = frozenset({"cat", "grep", "sed", "head", "tail", "awk", "ls"})
WRITE_COMMANDS = frozenset({
    "cp", "install", "mkdir", "mv", "tee", "touch", "truncate",
})
PATH_FIELD_MARKERS = (
    "path", "file", "filename", "cwd", "directory", "dir", "root",
    "target", "source", "destination",
)


@dataclass(frozen=True, slots=True)
class ProtectedPathDecision:
    allowed: bool
    reason: str
    paths: tuple[str, ...] = ()


def _deny(reason, paths=()):
    return ProtectedPathDecision(False, reason, tuple(paths))


def _allow(paths=()):
    return ProtectedPathDecision(True, "protected path ceiling passed", tuple(paths))


def _parts(path):
    return tuple(part for part in path.replace("\\", "/").split("/") if part not in {"", "."})


def is_protected_path(path):
    """Match protected names at any depth, including a symlink's real target."""
    if not isinstance(path, str) or not path:
        return False
    for part in _parts(path):
        lowered = part.casefold()
        if lowered in PROTECTED_DIRECTORIES or lowered in PROTECTED_EXACT_FILES:
            return True
        if lowered.startswith(".env."):
            return True
        if lowered.endswith(PROTECTED_SUFFIXES):
            return True
        if lowered.startswith(PROTECTED_PREFIXES):
            return True
    return False


def inspect_workspace_path(path, workspace_root=None, *, allow_directory=True):
    """Fail closed on protected, absolute, escaping, expanded, or symlink paths."""
    if not isinstance(path, str) or not path.strip():
        return _deny("workspace path is empty or invalid")
    path = path.strip()
    if is_protected_path(path):
        return _deny("protected path ceiling", (path,))
    if os.path.isabs(path):
        return _deny("absolute workspace path is not allowed", (path,))
    parts = _parts(path)
    if ".." in parts:
        return _deny("workspace path escape is not allowed", (path,))
    if any(marker in path for marker in ("\x00", "`", "$", "~", "*", "?", "[")):
        return _deny("dynamic or expanded workspace path is not allowed", (path,))

    root = os.path.realpath(workspace_root or os.getcwd())
    lexical = os.path.abspath(os.path.join(root, path))
    resolved = os.path.realpath(lexical)
    try:
        if os.path.commonpath((root, lexical)) != root or os.path.commonpath((root, resolved)) != root:
            return _deny("workspace path or symlink escapes workspace", (path,))
    except ValueError:
        return _deny("workspace path cannot be compared safely", (path,))
    relative_resolved = os.path.relpath(resolved, root)
    if is_protected_path(relative_resolved):
        return _deny("symlink resolves to protected path", (path,))
    if not allow_directory and os.path.isdir(resolved):
        return _deny("expected a workspace file, not a directory", (path,))
    return _allow((os.path.normpath(path).replace(os.sep, "/"),))


def _shell_tokens(command):
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        return list(lexer)
    except (TypeError, ValueError):
        return None


def _non_option(tokens):
    return [token for token in tokens if token != "--" and not token.startswith("-")]


def _command_paths(segment):
    if not segment:
        return []
    command = os.path.basename(segment[0])
    args = segment[1:]
    values = _non_option(args)
    if command in {"cat", "ls", "touch", "mkdir", "tee", "truncate"}:
        return values
    if command in {"cp", "mv", "install"}:
        return values
    if command == "grep":
        # The first positional value is the pattern; remaining values are paths.
        return values[1:]
    if command in {"sed", "awk"}:
        # The first positional value is the program; remaining values are paths.
        return values[1:]
    if command in {"head", "tail"}:
        return [value for value in values if not value.isdecimal()]
    if command in {"echo", "printf", "pwd"}:
        return []
    # Unknown commands do not earn an approval-based protected-path bypass.
    # Their positional arguments are conservatively treated as possible paths.
    if command:
        return values
    return []


def inspect_shell_paths(command, workspace_root=None):
    """Inspect explicit file operands and redirection targets before Policy."""
    if not isinstance(command, str) or not command.strip():
        return _deny("shell command is empty or invalid")
    tokens = _shell_tokens(command)
    if tokens is None:
        return _deny("shell command path analysis failed closed")

    if tokens and os.path.basename(tokens[0]) == "grep" and any(
        token in {"-r", "-R", "--recursive", "--dereference-recursive"}
        or (token.startswith("-") and not token.startswith("--")
            and any(flag in token[1:] for flag in ("r", "R")))
        for token in tokens[1:]
    ):
        return _deny("recursive grep cannot exclude protected paths reliably")

    paths = []
    executable = os.path.basename(tokens[0]) if tokens else ""
    option_file_names = {
        "grep": {"-f", "--file", "--exclude-from"},
        "sed": {"-f", "--file"},
        "awk": {"-f", "--file"},
    }.get(executable, set())
    for index, token in enumerate(tokens[1:], 1):
        if token in option_file_names:
            if index + 1 >= len(tokens):
                return _deny("shell option file is missing")
            paths.append(tokens[index + 1])
        for option in option_file_names:
            if option.startswith("--") and token.startswith(option + "="):
                paths.append(token.split("=", 1)[1])
    segment = []
    heredoc = "<<" in tokens
    for index, token in enumerate(tokens):
        if token in {">", ">>", "<", "<<"}:
            if index + 1 >= len(tokens):
                return _deny("shell redirection target is missing")
            paths.append(tokens[index + 1])
            continue
        if token in {"&&", "||", ";", "|", "&"}:
            if not heredoc:
                paths.extend(_command_paths(segment))
            segment = []
            continue
        if index and tokens[index - 1] in {">", ">>", "<", "<<"}:
            continue
        segment.append(token)
    if not heredoc:
        paths.extend(_command_paths(segment))

    checked = []
    for path in paths:
        decision = inspect_workspace_path(path, workspace_root)
        if not decision.allowed:
            return decision
        checked.extend(decision.paths)
    return _allow(checked)


def _looks_like_path_field(key):
    normalized = str(key).casefold().replace("-", "_")
    return any(marker in normalized.split("_") or normalized.endswith("_" + marker)
               for marker in PATH_FIELD_MARKERS)


def inspect_mcp_paths(arguments, workspace_root=None):
    """Apply the same ceiling to MCP arguments that explicitly declare paths."""
    found = []

    def walk(value, path_field=False):
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, _looks_like_path_field(key))
        elif isinstance(value, list):
            for item in value:
                walk(item, path_field)
        elif path_field and isinstance(value, str):
            found.append(value)

    if not isinstance(arguments, dict):
        return _deny("MCP arguments must be an object")
    walk(arguments)
    checked = []
    for path in found:
        decision = inspect_workspace_path(path, workspace_root)
        if not decision.allowed:
            return decision
        checked.extend(decision.paths)
    return _allow(checked)


def inspect_subagent_paths(handoff, workspace_root=None):
    """Relevant paths are hints, but protected hints must never reach a child."""
    try:
        relevant = handoff["workspace"]["relevant_paths"]
    except (KeyError, TypeError):
        return _deny("subagent workspace path request is invalid")
    checked = []
    for path in relevant:
        decision = inspect_workspace_path(path, workspace_root)
        if not decision.allowed:
            return decision
        checked.extend(decision.paths)
    return _allow(checked)
