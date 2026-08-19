import json
import os
import unittest
from io import BytesIO
from unittest.mock import patch

from mini_harness import (
    FakeProvider,
    OpenAICompatibleHTTPClient,
    ProviderError,
    RealProvider,
    classify_shell,
    execute_shell,
    request_approval,
    run_agent,
)


class StubHTTPResponse:
    def __init__(self, body):
        self.body = BytesIO(json.dumps(body).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body.read()


class StubClient:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        return next(self.outputs)


class RealProviderTests(unittest.TestCase):
    def test_tool_call_is_parsed_and_normalized(self):
        client = StubClient([
            json.dumps(
                {"type": "tool_call", "tool": "shell", "command": "pwd"}
            )
        ])

        decision = RealProvider(client).complete(
            [{"role": "user", "content": "当前目录是什么？"}]
        )

        self.assertEqual(decision, {"type": "tool_call", "command": "pwd"})
        self.assertEqual(client.calls[0][0]["role"], "system")
        self.assertEqual(client.calls[0][1]["content"], "当前目录是什么？")

    def test_history_and_observation_are_forwarded(self):
        client = StubClient([
            json.dumps({"type": "final_answer", "final_answer": "完成"})
        ])
        history = [
            {"role": "user", "content": "执行任务"},
            {
                "role": "assistant",
                "content": '{"type":"tool_call","command":"pwd"}',
            },
            {
                "role": "tool",
                "content": '{"stdout":"/tmp\\n","stderr":"","exit_code":0}',
            },
        ]

        decision = RealProvider(client).complete(history)

        self.assertEqual(decision["type"], "final_answer")
        self.assertEqual(client.calls[0][1:], history)

    def test_invalid_json_has_clear_error(self):
        with self.assertRaisesRegex(ProviderError, "必须是单个 JSON 对象"):
            RealProvider(StubClient(["```json\n{}\n```"])).complete([])

    def test_unsupported_tool_has_clear_error(self):
        output = json.dumps(
            {"type": "tool_call", "tool": "python", "command": "pass"}
        )
        with self.assertRaisesRegex(ProviderError, "只支持 tool=shell"):
            RealProvider(StubClient([output])).complete([])

    def test_empty_final_answer_has_clear_error(self):
        output = json.dumps({"type": "final_answer", "final_answer": ""})
        with self.assertRaisesRegex(ProviderError, "必须是非空字符串"):
            RealProvider(StubClient([output])).complete([])


class OpenAICompatibleHTTPClientTests(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_chat_completions_remains_default(self, urlopen):
        urlopen.return_value = StubHTTPResponse({
            "choices": [{"message": {"content": "chat result"}}]
        })
        client = OpenAICompatibleHTTPClient("https://example.test/v1", "model")

        result = client.complete([{"role": "user", "content": "任务"}])

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(result, "chat result")
        self.assertEqual(
            request.full_url,
            "https://example.test/v1/chat/completions",
        )
        self.assertIn("messages", payload)
        self.assertNotIn("prompt", payload)

    @patch("urllib.request.urlopen")
    def test_completions_serializes_history_and_reads_text(self, urlopen):
        urlopen.return_value = StubHTTPResponse({
            "choices": [{"text": '"type":"final_answer","final_answer":"完成"}'}]
        })
        client = OpenAICompatibleHTTPClient(
            "https://example.test/v1", "sonnet-4.6", api_mode="completions"
        )
        messages = [
            {"role": "system", "content": "系统指令"},
            {"role": "user", "content": "用户任务"},
            {"role": "assistant", "content": '{"type":"tool_call","command":"pwd"}'},
            {"role": "tool", "content": '{"stdout":"/tmp","stderr":"","exit_code":0}'},
        ]

        result = client.complete(messages)

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://example.test/v1/completions")
        self.assertNotIn("messages", payload)
        self.assertEqual(payload["model"], "sonnet-4.6")
        self.assertEqual(payload["temperature"], 0)
        self.assertIn("[SYSTEM]\n系统指令", payload["prompt"])
        self.assertIn("[USER TASK]\n用户任务", payload["prompt"])
        self.assertIn("[ASSISTANT TOOL_CALL/HISTORY]", payload["prompt"])
        self.assertIn("[OBSERVATION]", payload["prompt"])
        self.assertIn("first character must be {", payload["prompt"])
        self.assertIn("valid JSON escapes", payload["prompt"])
        self.assertTrue(payload["prompt"].endswith("ASSISTANT: {"))
        self.assertEqual(result, '{"type":"final_answer","final_answer":"完成"}')

    @patch("urllib.request.urlopen")
    def test_completions_does_not_duplicate_echoed_prefill(self, urlopen):
        urlopen.return_value = StubHTTPResponse({
            "choices": [{"text": '{"type":"final_answer","final_answer":"完成"}'}]
        })
        client = OpenAICompatibleHTTPClient(
            "https://example.test/v1", "model", api_mode="completions"
        )

        result = client.complete([])

        self.assertEqual(result, '{"type":"final_answer","final_answer":"完成"}')

    def test_invalid_api_mode_is_rejected(self):
        with self.assertRaisesRegex(ProviderError, "LLM_API_MODE"):
            OpenAICompatibleHTTPClient(
                "https://example.test/v1", "model", api_mode="responses"
            )

class FakeProviderRegressionTests(unittest.TestCase):
    def test_offline_agent_still_completes(self):
        answer = run_agent("运行离线回归", FakeProvider())
        self.assertIn("当前目录是：", answer)


class ToolPolicyTests(unittest.TestCase):
    def test_pwd_is_allowed(self):
        self.assertEqual(classify_shell("pwd")["action"], "ALLOW")

    def test_simple_ls_is_allowed(self):
        self.assertEqual(classify_shell("ls -la .")["action"], "ALLOW")

    def test_touch_is_ask(self):
        self.assertEqual(classify_shell("touch x")["action"], "ASK")

    def test_compound_command_is_not_allowed(self):
        self.assertNotEqual(classify_shell("pwd && touch x")["action"], "ALLOW")

    def test_shell_expansion_is_not_allowed(self):
        self.assertEqual(classify_shell("ls $HOME")["action"], "ASK")

    def test_dangerous_command_is_denied_even_in_compound(self):
        self.assertEqual(classify_shell("pwd && rm -rf x")["action"], "DENY")

    def test_shell_interpreter_is_denied(self):
        self.assertEqual(classify_shell("bash -c 'rm -rf x'")["action"], "DENY")

    @patch("builtins.input", return_value="y")
    def test_approval_requires_y(self, user_input):
        self.assertTrue(request_approval("touch x"))
        user_input.assert_called_once()


class RecordingProvider:
    def __init__(self, command):
        self.command = command
        self.calls = []

    def complete(self, messages):
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            return {"type": "tool_call", "command": self.command}
        observation = json.loads(messages[-1]["content"])
        return {
            "type": "final_answer",
            "final_answer": json.dumps(observation),
        }


class ApprovalGateTests(unittest.TestCase):
    @patch("mini_harness.execute_shell")
    @patch("builtins.input", return_value="y")
    def test_ask_and_user_y_executes(self, user_input, shell):
        shell.return_value = {"stdout": "", "stderr": "", "exit_code": 0}

        run_agent("创建文件", RecordingProvider("touch x"))

        user_input.assert_called_once()
        shell.assert_called_once_with("touch x")

    @patch("mini_harness.execute_shell")
    @patch("builtins.input", return_value="n")
    def test_ask_rejection_becomes_observation(self, user_input, shell):
        provider = RecordingProvider("touch x")

        answer = run_agent("创建文件", provider)

        shell.assert_not_called()
        observation = json.loads(answer)
        self.assertEqual(observation["denied_by"], "user")
        self.assertEqual(
            observation["stderr"], "tool execution was denied by user"
        )

    @patch("mini_harness.request_approval")
    @patch("mini_harness.execute_shell")
    def test_policy_denial_neither_asks_nor_executes(self, shell, approval):
        provider = RecordingProvider("rm -rf x")

        answer = run_agent("删除文件", provider)

        approval.assert_not_called()
        shell.assert_not_called()
        observation = json.loads(answer)
        self.assertEqual(observation["denied_by"], "policy")
        self.assertEqual(
            observation["stderr"], "tool execution was denied by policy"
        )


class ToolExecutorSecretTests(unittest.TestCase):
    def test_harness_secrets_are_not_available_to_shell(self):
        secret = "offline-test-secret-must-not-leak"
        command = (
            "printf '%s' \"$LLM_API_KEY\"; "
            "printf '%s' \"$MINI_HARNESS_TEST_SECRET\" >&2"
        )

        with patch.dict(
            os.environ,
            {
                "LLM_API_KEY": secret,
                "MINI_HARNESS_TEST_SECRET": secret,
            },
        ):
            observation = execute_shell(command)

        self.assertEqual(observation["exit_code"], 0)
        self.assertNotIn(secret, observation["stdout"])
        self.assertNotIn(secret, observation["stderr"])
        self.assertNotIn(secret, json.dumps(observation))
        self.assertEqual(observation["stdout"], "")
        self.assertEqual(observation["stderr"], "")


if __name__ == "__main__":
    unittest.main()
