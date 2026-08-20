"""Read and select untrusted AGENTS.md and Skill project context."""

import os
import re


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PROJECT_INSTRUCTIONS_FILE = "AGENTS.md"
SKILLS_DIRECTORY = "skills"


def _read_project_file(project_root, path):
    """Read only a regular file whose resolved location stays in the project."""
    root = os.path.realpath(project_root)
    resolved = os.path.realpath(path)
    try:
        if os.path.commonpath((root, resolved)) != root or not os.path.isfile(resolved):
            return None
    except ValueError:
        return None
    try:
        with open(resolved, encoding="utf-8") as project_file:
            return project_file.read()
    except (FileNotFoundError, OSError, UnicodeError):
        return None


def load_project_instructions(project_root=PROJECT_ROOT):
    """Read current project instructions; absence is an ordinary empty state."""
    path = os.path.join(project_root, PROJECT_INSTRUCTIONS_FILE)
    return _read_project_file(project_root, path) or ""


def _parse_skill_metadata(path, project_root):
    """Parse the tiny V7 frontmatter format without a YAML dependency."""
    text = _read_project_file(project_root, path)
    if text is None:
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    metadata = {}
    closing_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            closing_index = index
            break
        if ":" not in line:
            return None
        key, value = line.split(":", 1)
        key = key.strip()
        if key not in {"name", "description"} or key in metadata:
            return None
        metadata[key] = value.strip()
    if closing_index is None:
        return None
    name = metadata.get("name", "")
    description = metadata.get("description", "")
    directory_name = os.path.basename(os.path.dirname(path))
    if (
        not SKILL_NAME_PATTERN.fullmatch(name)
        or name != directory_name
        or not description
    ):
        return None
    body = "\n".join(lines[closing_index + 1:]).strip()
    return {"name": name, "description": description, "body": body}


def discover_skills(project_root=PROJECT_ROOT):
    """Return only the public V7 catalog: name and description."""
    skills_root = os.path.join(project_root, SKILLS_DIRECTORY)
    try:
        names = sorted(os.listdir(skills_root))
    except (FileNotFoundError, NotADirectoryError, OSError):
        return []
    catalog = []
    for directory_name in names:
        if not SKILL_NAME_PATTERN.fullmatch(directory_name):
            continue
        path = os.path.join(skills_root, directory_name, "SKILL.md")
        metadata = _parse_skill_metadata(path, project_root)
        if metadata is not None:
            catalog.append({
                "name": metadata["name"],
                "description": metadata["description"],
            })
    return catalog


def _description_terms(description):
    return {
        term.casefold()
        for term in re.findall(r"[A-Za-z0-9]+|[\u3400-\u9fff]{2,}", description)
        if len(term) >= 2
    }


_NEGATED_SKILL_SCOPE = re.compile(
    r"(?:"
    r"不要讨论|不涉及|无需|不需要|不使用|不要使用|"
    r"\bdo\s+not\s+discuss\b|\bdon['’]t\s+discuss\b|"
    r"\bno\b|\bwithout\b"
    r")\s*.*?"
    r"(?=(?:[，,。.;；!?！？\n]|但是|但|不过|\bbut\b|\bhowever\b|\byet\b)|$)",
    re.IGNORECASE,
)


def _task_without_negated_skill_scopes(task):
    """Remove only simple, explicit negated clauses before keyword matching."""
    return _NEGATED_SKILL_SCOPE.sub(" ", task)


def select_skill(task, catalog):
    """Deterministic name/keyword matching; deliberately not semantic search."""
    folded_task = _task_without_negated_skill_scopes(task).casefold()
    explicit = [
        skill for skill in catalog
        if re.search(
            rf"(?<![a-z0-9-]){re.escape(skill['name'].casefold())}(?![a-z0-9-])",
            folded_task,
        )
    ]
    if len(explicit) == 1:
        return explicit[0]["name"]
    if explicit:
        return None

    scored = []
    for skill in catalog:
        score = sum(
            term in folded_task
            for term in _description_terms(skill["description"])
        )
        if score:
            scored.append((score, skill["name"]))
    if not scored:
        return None
    best_score = max(score for score, _ in scored)
    winners = [name for score, name in scored if score == best_score]
    return winners[0] if len(winners) == 1 else None


def load_skill_body(project_root, skill_name):
    """Load a selected catalog member through its fixed, validated path."""
    if not SKILL_NAME_PATTERN.fullmatch(skill_name):
        return None
    path = os.path.join(project_root, SKILLS_DIRECTORY, skill_name, "SKILL.md")
    metadata = _parse_skill_metadata(path, project_root)
    if metadata is None or metadata["name"] != skill_name:
        return None
    return metadata["body"]
