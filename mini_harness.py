#!/usr/bin/env python3
"""一个最小化、用于教学的 AI Agent Harness。"""

import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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

    def __init__(self, client):
        # client 只需实现 complete(messages) -> str，可替换为任意厂商或本地模型。
        self.client = client

    def complete(self, messages):
        model_messages = [{"role": "system", "content": self.SYSTEM_PROMPT}]
        model_messages.extend(messages)
        raw_output = self.client.complete(model_messages)
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

def run_agent(task, provider, max_steps=5):
    """Harness 行为：驱动模型、工具和 observation 之间的循环。"""
    messages = [{"role": "user", "content": task}]
    requires_verification = False
    latest_write_command = None
    rejected_final_answer = None

    for step in range(1, max_steps + 1):
        print(f"\n[Harness] 第 {step}/{max_steps} 步：请求模型做决定")
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
                print("[Verification Gate] verification required before final answer")
                continue
            answer = decision.get("final_answer", "")
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
        elif policy["action"] == POLICY_ASK:
            approved = request_approval(command, policy["reason"])

        if approved:
            observation = execute_shell(command)
            print("[Tool Execution] 命令执行完毕")
            if observation["exit_code"] == 0:
                if requires_verification and policy["action"] == POLICY_ALLOW:
                    requires_verification = False
                    print("[Verification Gate] 只读验证成功，已解除门禁")
                elif policy["action"] == POLICY_ASK:
                    requires_verification = True
                    latest_write_command = command
                    print("[Verification Gate] 写操作成功，需要只读验证")
        elif not (requires_verification and policy["action"] == POLICY_ASK):
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

    raise RuntimeError(f"达到最大步数 {max_steps}，Agent 已停止，以防止无限循环。")


def main():
    load_dotenv_local()
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
        provider = RealProvider(client)
    else:
        print(
            "错误：MINI_HARNESS_PROVIDER 只能是 fake 或 real。", file=sys.stderr
        )
        raise SystemExit(2)

    print(f"最小 AI Agent Harness（{provider.__class__.__name__}）")
    try:
        task = input("请输入中文任务（直接回车运行 demo）：").strip()
        if not task:
            task = "演示工具失败后，Provider 如何根据 Observation 改变决策。"
            print(f"[Demo 任务] {task}")
        run_agent(task, provider)
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。", file=sys.stderr)
        raise SystemExit(130)
    except (ValueError, RuntimeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
