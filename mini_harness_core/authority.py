"""Harness authority: policy, approval, isolated shell execution, attenuation."""

import os
import shlex
import subprocess

from .verification import LS_OPTION_CHARS, SHELL_OPERATORS, _is_within_workspace


POLICY_ALLOW = "ALLOW"
POLICY_ASK = "ASK"
POLICY_DENY = "DENY"

DANGEROUS_COMMANDS = {
    "bash", "chmod", "chown", "dash", "dd", "doas", "fdisk", "halt",
    "kill", "killall", "mkfs", "mount", "parted", "pkill", "poweroff",
    "reboot", "rm", "sh", "shutdown", "su", "sudo", "umount", "zsh",
}
def _policy_result(action, reason):
    return {"action": action, "reason": reason}


# Verification helpers are imported from mini_harness_core.verification.
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


def request_approval(command, reason=None, run_control=None, save_run_control=None):
    """ASK 命令只有在用户明确输入小写 y 时才获准执行。"""
    print(f"[模型请求的完整命令] {command}")
    print(f"[Policy 分类] {POLICY_ASK}")
    print(f"[Policy 原因] {reason or '命令需要用户明确批准'}")
    answer = input(
        "允许执行？输入 y 批准，pause 暂停，cancel 取消，其他输入拒绝："
    ).strip()
    if answer in {"pause", "cancel"} and run_control is not None:
        from .run_control import (
            mark_cancelled, mark_paused, request_cancel, request_pause,
        )
        updated = (
            mark_paused(request_pause(run_control))
            if answer == "pause"
            else mark_cancelled(request_cancel(run_control))
        )
        run_control.clear()
        run_control.update(updated)
        if save_run_control:
            save_run_control(updated)
        return False
    return answer == "y"

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


# ==================== Structured Handoff / Subagent ====================

def _effective_subagent_authority(requested, main_authority):
    main = main_authority or {
        "allowed_tools": ["shell", "mcp"],
        "can_write_workspace": True,
        "can_use_mcp": True,
        "max_steps": requested["max_steps"],
    }
    main_max_steps = main.get("max_steps", requested["max_steps"])
    if not isinstance(main_max_steps, int) or isinstance(main_max_steps, bool):
        main_max_steps = 0
    main_tools = set(main.get("allowed_tools", []))
    requested_tools = set(requested["allowed_tools"])
    allowed = []
    for tool in requested_tools:
        category = "mcp" if tool.startswith("mcp:") else tool
        if tool in main_tools or category in main_tools:
            allowed.append(tool)
    return {
        "allowed_tools": sorted(allowed),
        "can_write_workspace": bool(
            requested["can_write_workspace"]
            and main.get("can_write_workspace", False)
        ),
        "can_use_mcp": bool(
            requested["can_use_mcp"] and main.get("can_use_mcp", False)
        ),
        "max_steps": min(requested["max_steps"], main_max_steps),
    }


def _tool_allowed(reference, authority):
    allowed = set(authority["allowed_tools"])
    if reference == "shell":
        return "shell" in allowed
    return (
        authority["can_use_mcp"]
        and ("mcp" in allowed or reference in allowed)
    )


