#!/usr/bin/env python3
"""一个最小化、用于教学的 AI Agent Harness。"""

from mini_harness_core.session import (
    SESSIONS_DIR,
    SESSION_ID_PATTERN,
    SESSION_VERSION,
    SessionStore,
    utc_now,
)
from mini_harness_core.memory import (
    _SECRET_PATTERNS,
    MEMORY_CONTENT_LIMIT,
    MEMORY_CONTEXT_HEADER,
    MEMORY_FILE,
    MEMORY_KINDS,
    MEMORY_LIMIT,
    MEMORY_STORE_LIMIT,
    MemoryStore,
    format_memory_context,
    request_memory_approval,
    screen_memory_content,
    select_memories,
    validate_memory_candidate,
)
from mini_harness_core.project_context import (
    PROJECT_INSTRUCTIONS_FILE,
    SKILLS_DIRECTORY,
    SKILL_NAME_PATTERN,
    discover_skills,
    load_project_instructions,
    load_skill_body,
    select_skill,
)
from mini_harness_core.verification import (
    LS_OPTION_CHARS,
    SHELL_OPERATORS,
    _is_within_workspace,
    _normalized_workspace_path,
    _parse_shell_tokens,
    build_verification_feedback,
    extract_verification_target,
    is_related_verification,
)
from mini_harness_core.context import (
    COMPACTION_EXCERPT_CHARACTERS,
    COMPACTION_RECENT_MESSAGES,
    COMPACTION_SUMMARY_ENTRIES,
    RUNTIME_CONTEXT_PREFIXES,
    RuntimeContextAssembler,
    compact_messages,
    measure_context,
    parse_context_budget,
    print_context_stats,
)
from mini_harness_core.mcp import (
    MCP_DEFAULT_TIMEOUT,
    MCP_EFFECT_READ_ONLY,
    MCP_EFFECT_SIDE_EFFECTING,
    MCP_EFFECT_UNKNOWN,
    MCP_PROTOCOL_VERSION,
    MCP_TOOL_REFERENCE,
    FakeMCPClient,
    MCPClient,
    MCPError,
    MCPRegistry,
    StdioMCPClient,
    execute_mcp_tool,
    validate_json_schema,
)
from mini_harness_core.authority import (
    DANGEROUS_COMMANDS,
    POLICY_ALLOW,
    POLICY_ASK,
    POLICY_DENY,
    SHELL_ENV_ALLOWLIST,
    _effective_subagent_authority,
    _policy_result,
    _tool_allowed,
    build_shell_environment,
    classify_shell,
    execute_shell,
    request_approval,
)
from mini_harness_core.providers import (
    FakeProvider,
    OpenAICompatibleHTTPClient,
    ProviderError,
    RealProvider,
    _ProtocolError,
)
from mini_harness_core.handoff import (
    HANDOFF_AUTHORITY_FIELDS,
    HANDOFF_FIELDS,
    HANDOFF_RETURN_FIELDS,
    HANDOFF_WORKSPACE_FIELDS,
    _contains_secret,
    _safe_result,
    _validate_string_list,
    create_handoff,
    validate_handoff,
)
from mini_harness_core.agent import run_agent, run_subagent
from mini_harness_core.cli import (
    ENV_NAME_PATTERN,
    PROJECT_ROOT,
    forget_memory_interactively,
    list_memories,
    load_dotenv_local,
    main,
    update_memory_interactively,
)


if __name__ == "__main__":
    main()
