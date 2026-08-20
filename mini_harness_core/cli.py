"""Command-line parsing, runtime wiring, and user interaction."""

import argparse
import os
import re
import sys

from .agent import run_agent
from .authority import POLICY_ASK
from .context import parse_context_budget
from .mcp import (
    MCP_EFFECT_READ_ONLY,
    FakeMCPClient,
    MCPRegistry,
    StdioMCPClient,
)
from .memory import MemoryStore, screen_memory_content
from .providers import FakeProvider, OpenAICompatibleHTTPClient, RealProvider
from .session import SessionStore


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


# ==================== MCP External Capabilities ====================

# MCP public symbols are imported from mini_harness_core.mcp.
# Provider public symbols are imported from mini_harness_core.providers.
# ==================== Tool Executor ====================


def list_memories(store):
    memories = store.load()
    if not memories:
        print("[Memory] 暂无长期记忆")
        return []
    for memory in memories:
        print(f"id: {memory['id']}")
        print(f"kind: {memory['kind']}")
        print(f"content: {memory['content']}")
        print(f"updated_at: {memory['updated_at']}")
        print(f"status: {memory['status']}")
        print()
    return memories


def forget_memory_interactively(store, memory_id):
    memories = store.load()
    memory = store._find(memories, memory_id)
    print(f"[Memory Forget] {memory['id']} {memory['kind']}: {memory['content']}")
    if input("设为 inactive？输入 y 批准，其他输入拒绝：").strip() != "y":
        print("memory not forgotten")
        return False
    store.forget(memory_id)
    print("memory forgotten")
    return True


def update_memory_interactively(store, memory_id):
    memories = store.load()
    memory = store._find(memories, memory_id)
    content = input("请输入新的 memory content：").strip()
    allowed, reason = screen_memory_content(content)
    if not allowed:
        raise ValueError(f"Memory Policy DENY：{reason}")
    print(f"[Memory Update] id: {memory['id']}")
    print(f"old: {memory['content']}")
    print(f"new: {content}")
    if input("确认更新？输入 y 批准，其他输入拒绝：").strip() != "y":
        print("memory not updated")
        return False
    store.update(memory_id, content)
    print("memory updated")
    return True


def main():
    parser = argparse.ArgumentParser(description="最小 AI Agent Harness")
    parser.add_argument("--resume", metavar="SESSION_ID", help="恢复指定 session")
    management = parser.add_mutually_exclusive_group()
    management.add_argument(
        "--memory-list", action="store_true", help="列出长期记忆"
    )
    management.add_argument(
        "--memory-forget", metavar="ID", help="将指定长期记忆设为 inactive"
    )
    management.add_argument(
        "--memory-update", metavar="ID", help="交互式更新指定长期记忆"
    )
    args = parser.parse_args()

    if args.resume and (args.memory_list or args.memory_forget or args.memory_update):
        parser.error("--resume 不能与 memory management 参数同时使用")

    try:
        memory_store = MemoryStore()
        if args.memory_list:
            list_memories(memory_store)
            return
        if args.memory_forget:
            forget_memory_interactively(memory_store, args.memory_forget)
            return
        if args.memory_update:
            update_memory_interactively(memory_store, args.memory_update)
            return
        # 新建与 resume 都先验证当前 Store；Session 中不保存 Memory snapshot。
        memory_store.load()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。", file=sys.stderr)
        raise SystemExit(130)
    except (OSError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1)

    load_dotenv_local()
    try:
        context_budget = parse_context_budget(
            os.environ.get("MINI_HARNESS_CONTEXT_BUDGET")
        )
    except ValueError as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(2)
    mcp_registry = MCPRegistry(
        {"demo": FakeMCPClient(), "demo-stdio": StdioMCPClient()},
        {
            "mcp:demo:echo": POLICY_ASK,
            "mcp:demo-stdio:echo": POLICY_ASK,
        },
        {
            "mcp:demo:echo": MCP_EFFECT_READ_ONLY,
            "mcp:demo-stdio:echo": MCP_EFFECT_READ_ONLY,
        },
    )
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
        provider = RealProvider(
            client, context_budget=context_budget, memory_store=memory_store,
            mcp_registry=mcp_registry,
        )
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
            memory_store=memory_store,
            mcp_registry=mcp_registry,
        )
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。", file=sys.stderr)
        raise SystemExit(130)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1)
    finally:
        mcp_registry.close()


