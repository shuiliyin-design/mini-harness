#!/usr/bin/env python3
"""一个最小化、用于教学的 AI Agent Harness。"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SESSIONS_DIR = os.path.join(PROJECT_ROOT, ".sessions")
SESSION_VERSION = 1
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SESSION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
COMPACTION_RECENT_MESSAGES = 6
COMPACTION_SUMMARY_ENTRIES = 12
COMPACTION_EXCERPT_CHARACTERS = 48


def measure_context(messages):
    """教学级上下文粗估；不是任何模型真实 tokenizer 的结果。"""
    def is_cjk(character):
        return any(
            start <= character <= end
            for start, end in (
                ("\u3400", "\u4dbf"),  # CJK Unified Ideographs Extension A
                ("\u4e00", "\u9fff"),  # CJK Unified Ideographs
                ("\u3040", "\u309f"),  # Hiragana
                ("\u30a0", "\u30ff"),  # Katakana
                ("\uac00", "\ud7af"),  # Hangul Syllables
                ("\uf900", "\ufaff"),  # CJK Compatibility Ideographs
            )
        )

    contents = [message.get("content", "") for message in messages]
    total_characters = sum(len(content) for content in contents)
    cjk_characters = sum(
        1
        for content in contents
        for character in content
        if is_cjk(character)
    )
    other_characters = total_characters - cjk_characters
    approximate_tokens = cjk_characters + (other_characters + 3) // 4
    return {
        "message_count": len(messages),
        "total_characters": total_characters,
        "approximate_tokens": approximate_tokens,
    }


def parse_context_budget(value):
    """Parse an optional positive estimated-token budget."""
    if value is None or value == "":
        return None
    try:
        budget = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "MINI_HARNESS_CONTEXT_BUDGET 必须是正整数"
        ) from error
    if budget <= 0:
        raise ValueError("MINI_HARNESS_CONTEXT_BUDGET 必须是正整数")
    return budget


def print_context_stats(messages, budget=None, label=None, warn=True):
    """只输出聚合统计；绝不输出消息正文或认证信息。"""
    stats = measure_context(messages)
    prefix = f"[Context] {label}:" if label else "[Context]"
    print(
        f"{prefix} "
        f"messages={stats['message_count']} "
        f"characters={stats['total_characters']} "
        f"approx_tokens≈{stats['approximate_tokens']}"
    )
    if warn and budget is not None and stats["approximate_tokens"] > budget:
        print("[Context Warning] estimated context exceeds budget")
    return stats


def _parse_structured_content(message):
    try:
        value = json.loads(message.get("content", ""))
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _short_text(value, limit=COMPACTION_EXCERPT_CHARACTERS):
    """Return a deterministic, single-line excerpt for model input, never logs."""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "…"


def _is_control_feedback(message):
    value = _parse_structured_content(message)
    return bool(
        message.get("role") == "user"
        and value
        and (
            value.get("type") == "verification_feedback"
            or value.get("status") == "denied"
            or value.get("denied_by") is not None
        )
    )


def _summarize_message(message, previous_command=None):
    """Extract explicit fields only; this deliberately makes no semantic claims."""
    role = message.get("role")
    value = _parse_structured_content(message)
    if role == "user":
        if value is not None and _is_control_feedback(message):
            result = {}
            for key in ("status", "denied_by", "verification_target"):
                if key in value:
                    result[key] = value[key]
            if value.get("type") == "verification_feedback":
                result["verification"] = True
            return result
        return {"user": _short_text(message.get("content", ""))}
    if role == "tool" and value is not None:
        result = {"exit_code": value.get("exit_code")}
        if previous_command == "pwd" and value.get("exit_code") == 0:
            stdout = value.get("stdout")
            if isinstance(stdout, str) and stdout.strip() and "\n" not in stdout.strip():
                result["cwd"] = _short_text(stdout.strip())
        for key in ("status", "denied_by", "verification_target"):
            if key in value:
                result[key] = value[key]
        return result
    if role == "assistant" and value is not None:
        if value.get("type") == "tool_call":
            return {"command": _short_text(value.get("command", ""))}
        if value.get("type") == "final_answer":
            return {"final": _short_text(value.get("final_answer", ""))}
    return {str(role or "message"): _short_text(message.get("content", ""))}


def _active_control_message(control_state):
    if not control_state or not control_state.get("requires_verification"):
        return None
    control = {
        "type": "active_control_state",
        "requires_verification": True,
        "verification_target": control_state.get("verification_target"),
        "latest_write_command": control_state.get("latest_write_command"),
        "instruction": "Do not give a final answer until a qualifying read-only verification succeeds.",
    }
    return {"role": "system", "content": json.dumps(control, ensure_ascii=False, separators=(",", ":"))}


def compact_messages(messages, control_state=None):
    """Build a one-shot working context without modifying full session history."""
    protected = {index for index, message in enumerate(messages) if message.get("role") == "system"}
    protected.update(range(max(0, len(messages) - COMPACTION_RECENT_MESSAGES), len(messages)))

    # Keep the newest non-control user message even if a tool exchange pushed the
    # current task outside the recent window.
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user" and not _is_control_feedback(messages[index]):
            protected.add(index)
            break

    omitted = [message for index, message in enumerate(messages) if index not in protected]
    entries = []
    previous_command = None
    for message in omitted[-COMPACTION_SUMMARY_ENTRIES:]:
        entry = _summarize_message(message, previous_command)
        entries.append(entry)
        previous_command = entry.get("command") if message.get("role") == "assistant" else None
    summary = {
        "type": "deterministic_compacted_history",
        "omitted_message_count": len(omitted),
        "entries": entries,
    }

    result = []
    inserted_summary = False
    for index, message in enumerate(messages):
        if index in protected:
            result.append(message)
        elif not inserted_summary:
            result.append({"role": "system", "content": json.dumps(summary, ensure_ascii=False, separators=(",", ":"))})
            inserted_summary = True

    control_message = _active_control_message(control_state)
    if control_message is not None:
        result.append(control_message)
    return result


def utc_now():
    """Return a stable, JSON-friendly UTC timestamp."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SessionStore:
    """Persist explicit Agent sessions as small, atomic JSON files."""

    def __init__(self, directory=SESSIONS_DIR):
        self.directory = directory

    def _path(self, session_id):
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            raise ValueError("无效的 session_id（应为 32 位小写十六进制字符串）")
        return os.path.join(self.directory, f"{session_id}.json")

    def create(self):
        now = utc_now()
        session = {
            "version": SESSION_VERSION,
            "session_id": uuid.uuid4().hex,
            "created_at": now,
            "updated_at": now,
            "messages": [],
            "verification": {
                "requires_verification": False,
                "verification_target": None,
                "latest_write_command": None,
            },
        }
        self.save(session)
        return session

    def load(self, session_id):
        path = self._path(session_id)
        try:
            with open(path, encoding="utf-8") as session_file:
                session = json.load(session_file)
        except FileNotFoundError as error:
            raise ValueError(f"session 不存在：{session_id}") from error
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"无法读取 session：{error}") from error
        self._validate(session, expected_id=session_id)
        return session

    def save(self, session):
        self._validate(session)
        os.makedirs(self.directory, exist_ok=True)
        session["updated_at"] = utc_now()
        path = self._path(session["session_id"])
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{session['session_id']}.", suffix=".tmp", dir=self.directory
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as session_file:
                json.dump(session, session_file, ensure_ascii=False, indent=2)
                session_file.write("\n")
                session_file.flush()
                os.fsync(session_file.fileno())
            os.replace(temporary_path, path)
        except Exception:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _validate(session, expected_id=None):
        if not isinstance(session, dict):
            raise ValueError("session JSON 必须是对象")
        if session.get("version") != SESSION_VERSION:
            raise ValueError(f"不支持的 session version：{session.get('version')!r}")
        session_id = session.get("session_id")
        if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
            raise ValueError("session JSON 中的 session_id 无效")
        if expected_id is not None and session_id != expected_id:
            raise ValueError("session 文件名与内容中的 session_id 不一致")
        if not isinstance(session.get("created_at"), str):
            raise ValueError("session 缺少 created_at")
        if not isinstance(session.get("updated_at"), str):
            raise ValueError("session 缺少 updated_at")
        messages = session.get("messages")
        if not isinstance(messages, list) or not all(
            isinstance(message, dict)
            and message.get("role") in {"user", "assistant", "tool"}
            and isinstance(message.get("content"), str)
            for message in messages
        ):
            raise ValueError("session messages 格式无效")
        verification = session.get("verification")
        if not isinstance(verification, dict):
            raise ValueError("session verification 格式无效")
        if not isinstance(verification.get("requires_verification"), bool):
            raise ValueError("session verification 状态无效")


def load_dotenv_local(path=None):
    """Load simple KEY=value entries without overriding the process environment."""
    env_path = path or os.path.join(PROJECT_ROOT, ".env.local")
    try:
        with open(env_path, encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                if "=" not in line:
                    continue
                name, value = line.split("=", 1)
                name = name.strip()
                if not ENV_NAME_PATTERN.fullmatch(name):
                    continue
                value = value.strip()
                if (
                    len(value) >= 2
                    and value[0] == value[-1]
                    and value[0] in ("'", '"')
                ):
                    value = value[1:-1]
                os.environ.setdefault(name, value)
    except FileNotFoundError:
        pass


# ==================== Model / Provider ====================

class FakeProvider:
    """用固定的工具失败恢复行为模拟 LLM，无需 API Key。"""

    def complete(self, messages):
        # 离线 Provider 也理解 Harness 注入的结构化 verification feedback，
        # 便于不调用真实 LLM 地覆盖写后验证路径。
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message["role"] != "user":
                continue
            try:
                feedback = json.loads(message["content"])
            except (json.JSONDecodeError, TypeError):
                continue
            if feedback.get("type") == "verification_feedback":
                if not any(
                    later["role"] == "tool" for later in messages[index + 1:]
                ):
                    return {"type": "tool_call", "command": "pwd"}
                break

        observations = [
            json.loads(message["content"])
            for message in messages
            if message["role"] == "tool"
        ]

        # 第一步：固定请求一个不存在的路径，让工具产生失败 Observation。
        if not observations:
            return {
                "type": "tool_call",
                "command": "ls definitely-not-exist-agent-harness",
            }

        # 读取失败 Observation 的 exit_code 和 stderr，并据此改变策略。
        first_observation = observations[0]
        first_exit_code = first_observation["exit_code"]
        first_stderr = first_observation["stderr"].strip()
        if len(observations) == 1 and first_exit_code != 0:
            return {"type": "tool_call", "command": "pwd"}

        # pwd 成功后，结合两次 Observation 返回最终答案。
        latest_observation = observations[-1]
        current_directory = latest_observation["stdout"].strip()
        return {
            "type": "final_answer",
            "final_answer": (
                "第一次工具调用失败；"
                f"stderr 告诉我们失败原因：{first_stderr}；"
                "Provider 根据 Observation 改变了决策；"
                "第二次工具调用成功；"
                f"当前目录是：{current_directory}"
            ),
        }


class ProviderError(RuntimeError):
    """Provider 无法调用模型或无法得到合法决定。"""


class OpenAICompatibleHTTPClient:
    """使用标准库调用 OpenAI Chat/Completions 兼容接口。"""

    API_MODES = ("chat-completions", "completions")
    COMPLETIONS_PREFILL = "{"

    def __init__(
        self, endpoint, model, api_key="", timeout=60,
        api_mode="chat-completions",
    ):
        if api_mode not in self.API_MODES:
            raise ProviderError(
                "LLM_API_MODE 只能是 chat-completions 或 completions"
            )
        self.api_mode = api_mode
        self.endpoint = self._resolve_endpoint(endpoint, api_mode)
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    @staticmethod
    def _resolve_endpoint(endpoint, api_mode):
        """既接受 /v1 base URL，也兼容原先传入完整 endpoint 的配置。"""
        endpoint = endpoint.rstrip("/")
        suffix = (
            "/chat/completions"
            if api_mode == "chat-completions"
            else "/completions"
        )
        if endpoint.endswith("/v1"):
            return endpoint + suffix
        if api_mode == "completions" and endpoint.endswith("/v1/chat/completions"):
            return endpoint[:-len("/chat/completions")] + "/completions"
        if api_mode == "chat-completions" and endpoint.endswith("/v1/completions"):
            return endpoint[:-len("/completions")] + "/chat/completions"
        return endpoint

    @staticmethod
    def _serialize_prompt(messages):
        """把对话及 Observation 无损地表示成 Completions 纯文本 prompt。"""
        role_names = {
            "system": "SYSTEM",
            "user": "USER TASK",
            "assistant": "ASSISTANT TOOL_CALL/HISTORY",
            "tool": "OBSERVATION",
        }
        sections = []
        for message in messages:
            role = role_names.get(
                message.get("role"),
                str(message.get("role", "UNKNOWN")).upper(),
            )
            sections.append(f"[{role}]\n{message.get('content', '')}")
        sections.append(
            "[ASSISTANT NEXT DECISION]\n"
            "Return exactly one JSON object matching the SYSTEM protocol now. "
            "The first character must be { and the last character must be }. "
            "Use only valid JSON escapes (never escape an apostrophe as \\'). "
            "Keep final_answer concise and plain text without Markdown. "
            "Do not include prose, Markdown, or code fences outside the JSON.\n"
            "ASSISTANT: {"
        )
        return "\n\n".join(sections)

    def complete(self, messages):
        if self.api_mode == "completions":
            request_body = {
                "model": self.model,
                "prompt": self._serialize_prompt(messages),
                "temperature": 0,
            }
        else:
            request_body = {
                "model": self.model,
                "messages": messages,
                "temperature": 0,
            }
        payload = json.dumps(
            request_body, ensure_ascii=False,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.endpoint, data=payload, headers=headers, method="POST"
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise ProviderError(
                f"LLM 接口返回 HTTP {error.code}：{detail}"
            ) from error
        except (OSError, json.JSONDecodeError) as error:
            raise ProviderError(f"调用 LLM 接口失败：{error}") from error

        try:
            choice = body["choices"][0]
            content = (
                choice["text"]
                if self.api_mode == "completions"
                else choice["message"]["content"]
            )
        except (KeyError, IndexError, TypeError) as error:
            raise ProviderError(f"LLM 接口响应结构无效：{body!r}") from error
        if not isinstance(content, str):
            raise ProviderError(f"LLM 接口未返回文本内容：{content!r}")
        if self.api_mode == "completions":
            # Completions 只返回 prefill 之后的新 token，补回 JSON 的首字符。
            content = content.lstrip()
            if not content.startswith(self.COMPLETIONS_PREFILL):
                content = self.COMPLETIONS_PREFILL + content
        return content


class RealProvider:
    """让真实 LLM 根据任务、历史 Action 和 Observation 决定下一步。"""

    SYSTEM_PROMPT = """你是 Mini Harness 的决策模型。请根据用户任务和全部历史记录决定下一步。
你只能返回一个 JSON 对象，不要返回 Markdown、代码围栏或解释。
仅允许以下两种格式：
1. 调用 shell：{"type":"tool_call","tool":"shell","command":"一条 shell 命令"}
2. 完成任务：{"type":"final_answer","final_answer":"给用户的中文答案"}
你会在历史记录中看到先前的 tool_call，以及 role=tool 的 Observation；Observation 包含 stdout、stderr 和 exit_code。
必须利用 Observation 判断命令是否成功及下一步操作。不要虚构工具执行结果。"""

    def __init__(self, client, context_budget=None):
        # client 只需实现 complete(messages) -> str，可替换为任意厂商或本地模型。
        self.client = client
        self.context_budget = context_budget
        self.control_state = None

    def set_control_state(self, verification):
        # Copy runtime constraints so working-context construction cannot mutate session state.
        self.control_state = dict(verification) if verification else None

    def complete(self, messages):
        model_messages = [{"role": "system", "content": self.SYSTEM_PROMPT}]
        model_messages.extend(messages)
        control_message = _active_control_message(self.control_state)
        if control_message is not None:
            model_messages.append(control_message)
        before = measure_context(model_messages)
        if self.context_budget is not None and before["approximate_tokens"] > self.context_budget:
            print_context_stats(model_messages, label="before", warn=False)
            print("[Compaction] triggered")
            candidate_messages = compact_messages(model_messages)
            after = print_context_stats(candidate_messages, label="after", warn=False)
            if after["approximate_tokens"] >= before["approximate_tokens"]:
                print("[Compaction] skipped: compacted context was not smaller")
                working_messages = model_messages
            else:
                working_messages = candidate_messages
            if working_messages is candidate_messages and after["approximate_tokens"] > self.context_budget:
                print("[Context Warning] compacted context still exceeds budget; sending once without recursive compaction")
        else:
            working_messages = model_messages
            print_context_stats(working_messages, self.context_budget)
        raw_output = self.client.complete(working_messages)
        return self._parse_decision(raw_output)

    @staticmethod
    def _parse_decision(raw_output):
        try:
            decision = json.loads(raw_output)
        except (json.JSONDecodeError, TypeError) as error:
            raise ProviderError(
                f"模型输出格式错误：必须是单个 JSON 对象，实际输出为 {raw_output!r}"
            ) from error

        if not isinstance(decision, dict):
            raise ProviderError(f"模型输出格式错误：JSON 顶层必须是对象，实际为 {decision!r}")

        decision_type = decision.get("type")
        if decision_type == "tool_call":
            if decision.get("tool") != "shell":
                raise ProviderError("模型输出格式错误：V1 只支持 tool=shell")
            command = decision.get("command")
            if not isinstance(command, str) or not command.strip():
                raise ProviderError("模型输出格式错误：tool_call.command 必须是非空字符串")
            return {"type": "tool_call", "command": command}

        if decision_type == "final_answer":
            answer = decision.get("final_answer")
            if not isinstance(answer, str) or not answer.strip():
                raise ProviderError("模型输出格式错误：final_answer 必须是非空字符串")
            return {"type": "final_answer", "final_answer": answer}

        raise ProviderError(
            "模型输出格式错误：type 必须是 tool_call 或 final_answer"
        )


# ==================== Tool Executor ====================

POLICY_ALLOW = "ALLOW"
POLICY_ASK = "ASK"
POLICY_DENY = "DENY"

DANGEROUS_COMMANDS = {
    "bash", "chmod", "chown", "dash", "dd", "doas", "fdisk", "halt",
    "kill", "killall", "mkfs", "mount", "parted", "pkill", "poweroff",
    "reboot", "rm", "sh", "shutdown", "su", "sudo", "umount", "zsh",
}
SHELL_OPERATORS = {"&&", "||", ";", "|", "&", ">", ">>", "<", "<<"}
LS_OPTION_CHARS = frozenset("aAlh1")


def _policy_result(action, reason):
    return {"action": action, "reason": reason}


def _is_within_workspace(path):
    workspace = os.path.realpath(os.getcwd())
    candidate = os.path.realpath(os.path.abspath(path))
    try:
        return os.path.commonpath((workspace, candidate)) == workspace
    except ValueError:
        return False


def _parse_shell_tokens(command):
    """用与教学级 Policy 相同的规则拆分一条 shell 命令。"""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        return list(lexer)
    except (TypeError, ValueError):
        return None


def _normalized_workspace_path(path):
    """返回安全的 workspace 相对路径；不解析或猜测 shell 展开。"""
    if not isinstance(path, str) or not path or path.startswith("-"):
        return None
    path_parts = path.replace("\\", "/").split("/")
    if os.path.isabs(path) or ".." in path_parts:
        return None
    if any(marker in path for marker in ("`", "$", "~", "*", "?", "[")):
        return None
    if not _is_within_workspace(path):
        return None
    normalized = os.path.normpath(path)
    if normalized in ("", "."):
        return None
    return normalized.replace(os.sep, "/")


def extract_verification_target(command):
    """从少量严格白名单写法提取单一目标；不确定时返回 None。"""
    if not isinstance(command, str) or any(
        marker in command for marker in ("`", "$", "~", "*", "?", "[")
    ):
        return None
    tokens = _parse_shell_tokens(command)
    if not tokens:
        return None

    target_type = None
    raw_path = None
    if tokens[0] == "echo":
        if tokens.count(">") != 1 or tokens[-2:-1] != [">"]:
            return None
        if len(tokens) < 4 or any(
            token in SHELL_OPERATORS for token in tokens[1:-2]
        ):
            return None
        target_type = "file"
        raw_path = tokens[-1]
    elif tokens[0] == "touch" and len(tokens) == 2:
        target_type = "file"
        raw_path = tokens[1]
    elif tokens[0] == "mkdir" and len(tokens) == 2:
        target_type = "directory"
        raw_path = tokens[1]
    else:
        return None

    path = _normalized_workspace_path(raw_path)
    if path is None:
        return None
    return {"target_type": target_type, "path": path}


def is_related_verification(command, target):
    """判断最小只读证据是否明确读取了同一个 file/directory。"""
    if not isinstance(target, dict):
        return False
    tokens = _parse_shell_tokens(command)
    if not tokens:
        return False

    raw_path = None
    if target.get("target_type") == "file":
        if len(tokens) != 2 or tokens[0] != "cat":
            return False
        raw_path = tokens[1]
    elif target.get("target_type") == "directory":
        if tokens[0] != "ls":
            return False
        paths = []
        for token in tokens[1:]:
            if token.startswith("-"):
                if token == "--" or not set(token[1:]).issubset(LS_OPTION_CHARS):
                    return False
            else:
                paths.append(token)
        if len(paths) != 1:
            return False
        raw_path = paths[0]
    else:
        return False

    path = _normalized_workspace_path(raw_path)
    return path is not None and path == target.get("path")


def classify_shell(command):
    """教学级 shell policy：窄 ALLOW、危险操作 DENY，其余 ASK。"""
    if not isinstance(command, str) or not command.strip():
        return _policy_result(POLICY_DENY, "命令为空或格式无效")

    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return _policy_result(POLICY_ASK, "无法可靠解析 shell 命令")

    # DENY 优先检查整条命令，而不是只看第一个程序名。
    for token in tokens:
        if os.path.basename(token) in DANGEROUS_COMMANDS:
            return _policy_result(POLICY_DENY, f"包含明确危险命令 {token!r}")

    if any(token in SHELL_OPERATORS for token in tokens):
        return _policy_result(POLICY_ASK, "包含组合、管道或重定向等 shell 语法")
    if any(marker in command for marker in ("`", "$", "~", "*", "?", "[")):
        return _policy_result(POLICY_ASK, "包含 shell 展开、通配符或命令替换")

    if not tokens:
        return _policy_result(POLICY_DENY, "命令为空或格式无效")
    if tokens[0] == "pwd" and all(arg in ("-L", "-P") for arg in tokens[1:]):
        return _policy_result(POLICY_ALLOW, "明确可识别的简单只读命令")
    if tokens[0] == "cat":
        if len(tokens) != 2:
            return _policy_result(POLICY_ASK, "cat 只自动放行单个明确文件")
        path = tokens[1]
        path_parts = path.replace("\\", "/").split("/")
        if (
            path.startswith("-")
            or os.path.isabs(path)
            or ".." in path_parts
            or not _is_within_workspace(path)
            or not os.path.isfile(path)
        ):
            return _policy_result(
                POLICY_ASK, "cat 目标不是 workspace 内明确的普通相对路径文件"
            )
        return _policy_result(
            POLICY_ALLOW, "workspace 内单个明确的普通相对路径文件"
        )
    if tokens[0] == "ls":
        paths = []
        for arg in tokens[1:]:
            if arg.startswith("-"):
                if arg == "--" or not set(arg[1:]).issubset(LS_OPTION_CHARS):
                    return _policy_result(POLICY_ASK, "ls 参数不在自动放行范围")
            else:
                paths.append(arg)
        if all(_is_within_workspace(path) for path in paths):
            return _policy_result(POLICY_ALLOW, "受限于当前 workspace 的简单只读 ls")
        return _policy_result(POLICY_ASK, "ls 目标超出当前 workspace")

    return _policy_result(POLICY_ASK, "不属于有限的自动放行命令")


def request_approval(command, reason=None):
    """ASK 命令只有在用户明确输入小写 y 时才获准执行。"""
    print(f"[模型请求的完整命令] {command}")
    print(f"[Policy 分类] {POLICY_ASK}")
    print(f"[Policy 原因] {reason or '命令需要用户明确批准'}")
    return input("允许执行？输入 y 批准，其他输入拒绝：").strip() == "y"

SHELL_ENV_ALLOWLIST = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
)


def build_shell_environment():
    """只向工具进程传递运行 shell 所需的非敏感环境变量。"""
    environment = {
        name: os.environ[name]
        for name in SHELL_ENV_ALLOWLIST
        if name in os.environ
    }
    environment.setdefault("PATH", os.defpath)
    return environment


def execute_shell(command):
    """真正的 Tool Execution：执行命令并产生 observation。"""
    try:
        result = subprocess.run(
            command,
            shell=True,
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=build_shell_environment(),
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }
    except Exception as error:
        return {"stdout": "", "stderr": str(error), "exit_code": -1}


# ==================== Agent Loop ====================

def run_agent(
    task, provider, max_steps=5, messages=None, verification=None,
    save_checkpoint=None,
):
    """Harness 行为：驱动模型、工具和 observation 之间的循环。"""
    messages = messages if messages is not None else []
    verification = verification if verification is not None else {
        "requires_verification": False,
        "latest_write_command": None,
        "verification_target": None,
    }
    messages.append({"role": "user", "content": task})
    if save_checkpoint:
        save_checkpoint()
    requires_verification = verification["requires_verification"]
    latest_write_command = verification.get("latest_write_command")
    verification_target = verification.get("verification_target")
    rejected_final_answer = None

    def checkpoint():
        verification["requires_verification"] = requires_verification
        verification["latest_write_command"] = latest_write_command
        verification["verification_target"] = verification_target
        if save_checkpoint:
            save_checkpoint()

    for step in range(1, max_steps + 1):
        print(f"\n[Harness] 第 {step}/{max_steps} 步：请求模型做决定")
        set_control_state = getattr(provider, "set_control_state", None)
        if callable(set_control_state):
            set_control_state({
                "requires_verification": requires_verification,
                "latest_write_command": latest_write_command,
                "verification_target": verification_target,
            })
        decision = provider.complete(messages)

        if decision.get("type") == "final_answer":
            if requires_verification:
                if decision == rejected_final_answer:
                    raise RuntimeError(
                        "模型在没有新 tool_call 的情况下重复提交了被 Verification "
                        "Gate 拒绝的 final_answer"
                    )
                feedback = {
                    "type": "verification_feedback",
                    "status": "final_answer_rejected",
                    "final_answer_allowed": False,
                    "reason": "verification required before final answer",
                    "required_next_action": {
                        "type": "tool_call",
                        "tool": "shell",
                        "policy_must_be": POLICY_ALLOW,
                        "command_must_be": "read-only",
                        "purpose": "verify the most recent successful write operation",
                    },
                    "write_operation_to_verify": latest_write_command,
                    "verification_target": verification_target,
                    "instruction": (
                        "Do not submit final_answer now. Request one read-only shell "
                        "tool_call classified as Policy=ALLOW to verify the write "
                        "operation above. Submit final_answer only after that tool "
                        "call succeeds."
                    ),
                }
                messages.append({
                    "role": "assistant",
                    "content": json.dumps(decision, ensure_ascii=False),
                })
                messages.append({
                    "role": "user",
                    "content": json.dumps(feedback, ensure_ascii=False),
                })
                rejected_final_answer = decision
                checkpoint()
                print("[Verification Gate] verification required before final answer")
                continue
            answer = decision.get("final_answer", "")
            messages.append({
                "role": "assistant",
                "content": json.dumps(decision, ensure_ascii=False),
            })
            checkpoint()
            print(f"[模型最终答案] {answer}")
            return answer

        if decision.get("type") != "tool_call" or not decision.get("command"):
            raise ValueError(f"模型返回了无效决定：{decision!r}")

        command = decision["command"]
        rejected_final_answer = None
        print(f"[模型请求执行的命令] {command}")
        policy = classify_shell(command)
        print(f"[Policy] {policy['action']}：{policy['reason']}")

        approved = policy["action"] == POLICY_ALLOW
        if requires_verification and policy["action"] == POLICY_ASK:
            approved = False
            observation = {
                "status": "denied",
                "denied_by": "verification_gate",
                "stdout": "",
                "stderr": "verification tool must be read-only",
                "exit_code": 126,
            }
            print("[Verification Gate] 验证工具必须是只读 ALLOW 命令")
        elif (
            requires_verification
            and policy["action"] == POLICY_ALLOW
            and verification_target is not None
            and not is_related_verification(command, verification_target)
        ):
            approved = False
            observation = {
                "status": "denied",
                "denied_by": "verification_quality",
                "stdout": "",
                "stderr": (
                    "verification evidence is not related to the modified target"
                ),
                "exit_code": 126,
                "verification_target": verification_target,
            }
            print("[Verification Quality] 验证证据与修改目标无关")
        elif policy["action"] == POLICY_ASK:
            approved = request_approval(command, policy["reason"])

        if approved:
            observation = execute_shell(command)
            print("[Tool Execution] 命令执行完毕")
            if observation["exit_code"] == 0:
                if requires_verification and policy["action"] == POLICY_ALLOW:
                    requires_verification = False
                    verification_target = None
                    print("[Verification Gate] 只读验证成功，已解除门禁")
                elif policy["action"] == POLICY_ASK:
                    requires_verification = True
                    latest_write_command = command
                    verification_target = extract_verification_target(command)
                    if verification_target is None:
                        print(
                            "[Verification Quality] 无法可靠识别目标，"
                            "显式降级为 V3 验证行为"
                        )
                    print("[Verification Gate] 写操作成功，需要只读验证")
        elif not (
            requires_verification
            and (
                policy["action"] == POLICY_ASK
                or (
                    policy["action"] == POLICY_ALLOW
                    and verification_target is not None
                    and not is_related_verification(command, verification_target)
                )
            )
        ):
            denied_by = "policy" if policy["action"] == POLICY_DENY else "user"
            observation = {
                "status": "denied",
                "denied_by": denied_by,
                "stdout": "",
                "stderr": f"tool execution was denied by {denied_by}",
                "exit_code": 126,
            }
            print(f"[Tool Execution] 未执行：denied_by={denied_by}")
        print(f"[Observation] exit_code={observation['exit_code']}")
        print(f"[Observation] stdout={observation['stdout'].rstrip()!r}")
        print(f"[Observation] stderr={observation['stderr'].rstrip()!r}")

        # Harness 行为：保存模型决定，并把工具结果作为 observation 发回模型。
        messages.append({"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)})
        messages.append({"role": "tool", "content": json.dumps(observation, ensure_ascii=False)})
        checkpoint()

    raise RuntimeError(f"达到最大步数 {max_steps}，Agent 已停止，以防止无限循环。")


def main():
    parser = argparse.ArgumentParser(description="最小 AI Agent Harness")
    parser.add_argument("--resume", metavar="SESSION_ID", help="恢复指定 session")
    args = parser.parse_args()

    load_dotenv_local()
    try:
        context_budget = parse_context_budget(
            os.environ.get("MINI_HARNESS_CONTEXT_BUDGET")
        )
    except ValueError as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(2)
    provider_name = os.environ.get("MINI_HARNESS_PROVIDER", "fake").lower()
    if provider_name == "fake":
        provider = FakeProvider()
    elif provider_name == "real":
        endpoint = os.environ.get("LLM_ENDPOINT", "")
        model = os.environ.get("LLM_MODEL", "")
        if not endpoint or not model:
            print(
                "错误：RealProvider 需要设置 LLM_ENDPOINT 和 LLM_MODEL。",
                file=sys.stderr,
            )
            raise SystemExit(2)
        client = OpenAICompatibleHTTPClient(
            endpoint=endpoint,
            model=model,
            api_key=os.environ.get("LLM_API_KEY", ""),
            api_mode=os.environ.get("LLM_API_MODE", "chat-completions").lower(),
        )
        provider = RealProvider(client, context_budget=context_budget)
    else:
        print(
            "错误：MINI_HARNESS_PROVIDER 只能是 fake 或 real。", file=sys.stderr
        )
        raise SystemExit(2)

    try:
        store = SessionStore()
        session = store.load(args.resume) if args.resume else store.create()
        action = "已恢复" if args.resume else "已创建"
        print(f"最小 AI Agent Harness（{provider.__class__.__name__}）")
        print(f"[Session] {action}：{session['session_id']}")
        task = input("请输入中文任务（直接回车运行 demo）：").strip()
        if not task:
            task = "演示工具失败后，Provider 如何根据 Observation 改变决策。"
            print(f"[Demo 任务] {task}")
        run_agent(
            task,
            provider,
            messages=session["messages"],
            verification=session["verification"],
            save_checkpoint=lambda: store.save(session),
        )
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。", file=sys.stderr)
        raise SystemExit(130)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
