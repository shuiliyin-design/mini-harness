#!/usr/bin/env python3
"""一个最小化、用于教学的 AI Agent Harness。"""

import json
import subprocess
import sys


# ==================== Model / Provider ====================

class FakeProvider:
    """用固定的工具失败恢复行为模拟 LLM，无需 API Key。"""

    def complete(self, messages):
        observations = [
            json.loads(message["content"])
            for message in messages
            if message["role"] == "tool"
        ]

        # 第一步：固定请求一个不存在的路径，让工具产生失败 Observation。
        if not observations:
            return {
                "type": "tool_call",
                "command": "ls /definitely-not-exist-agent-harness",
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


# ==================== Tool Executor ====================

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

    for step in range(1, max_steps + 1):
        print(f"\n[Harness] 第 {step}/{max_steps} 步：请求模型做决定")
        decision = provider.complete(messages)

        if decision.get("type") == "final_answer":
            answer = decision.get("final_answer", "")
            print(f"[模型最终答案] {answer}")
            return answer

        if decision.get("type") != "tool_call" or not decision.get("command"):
            raise ValueError(f"模型返回了无效决定：{decision!r}")

        command = decision["command"]
        print(f"[模型请求执行的命令] {command}")
        observation = execute_shell(command)
        print("[Tool Execution] 命令执行完毕")
        print(f"[Observation] exit_code={observation['exit_code']}")
        print(f"[Observation] stdout={observation['stdout'].rstrip()!r}")
        print(f"[Observation] stderr={observation['stderr'].rstrip()!r}")

        # Harness 行为：保存模型决定，并把工具结果作为 observation 发回模型。
        messages.append({"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)})
        messages.append({"role": "tool", "content": json.dumps(observation, ensure_ascii=False)})

    raise RuntimeError(f"达到最大步数 {max_steps}，Agent 已停止，以防止无限循环。")


def main():
    print("最小 AI Agent Harness（FakeProvider）")
    try:
        task = input("请输入中文任务（直接回车运行 demo）：").strip()
        if not task:
            task = "演示工具失败后，Provider 如何根据 Observation 改变决策。"
            print(f"[Demo 任务] {task}")
        run_agent(task, FakeProvider())
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。", file=sys.stderr)
        raise SystemExit(130)
    except (ValueError, RuntimeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
