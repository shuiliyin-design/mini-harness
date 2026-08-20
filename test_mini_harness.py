import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch

from mini_harness import (
    FakeProvider,
    OpenAICompatibleHTTPClient,
    ProviderError,
    RealProvider,
    classify_shell,
    execute_shell,
    extract_verification_target,
    is_related_verification,
    load_dotenv_local,
    request_approval,
    run_agent,
)


class DotenvLocalTests(unittest.TestCase):
    def load_contents(self, contents, initial_environment=None):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env.local"
            env_path.write_text(contents, encoding="utf-8")
            with patch.dict(os.environ, initial_environment or {}, clear=True):
                output = StringIO()
                with redirect_stdout(output), redirect_stderr(output):
                    load_dotenv_local(str(env_path))
                return dict(os.environ), output.getvalue()

    def test_loads_plain_variable(self):
        environment, _ = self.load_contents("PLAIN_VALUE=loaded\n")
        self.assertEqual(environment["PLAIN_VALUE"], "loaded")

    def test_loads_export_variable(self):
        environment, _ = self.load_contents("export EXPORTED_VALUE=loaded\n")
        self.assertEqual(environment["EXPORTED_VALUE"], "loaded")

    def test_strips_value_whitespace(self):
        environment, _ = self.load_contents(
            "LLM_ENDPOINT=  https://example.test/v1   \n"
        )
        self.assertEqual(environment["LLM_ENDPOINT"], "https://example.test/v1")

    def test_removes_paired_double_quotes(self):
        environment, _ = self.load_contents('DOUBLE_QUOTED="value"\n')
        self.assertEqual(environment["DOUBLE_QUOTED"], "value")

    def test_removes_paired_single_quotes(self):
        environment, _ = self.load_contents("SINGLE_QUOTED='value'\n")
        self.assertEqual(environment["SINGLE_QUOTED"], "value")

    def test_trims_before_removing_paired_quotes(self):
        environment, _ = self.load_contents('SPACED_QUOTED=  "value"  \n')
        self.assertEqual(environment["SPACED_QUOTED"], "value")

    def test_keeps_unpaired_quote(self):
        environment, _ = self.load_contents('UNPAIRED="value\n')
        self.assertEqual(environment["UNPAIRED"], '"value')

    def test_existing_environment_takes_precedence(self):
        environment, _ = self.load_contents(
            "PRIORITY_VALUE=from-file\n",
            {"PRIORITY_VALUE": "from-system"},
        )
        self.assertEqual(environment["PRIORITY_VALUE"], "from-system")

    def test_ignores_comments_and_blank_lines(self):
        environment, _ = self.load_contents(
            "\n  # ignored comment\n\nKEPT_VALUE=yes\n"
        )
        self.assertEqual(environment, {"KEPT_VALUE": "yes"})

    def test_does_not_leak_llm_api_key(self):
        secret = "offline-api-key-must-not-leak"
        environment, output = self.load_contents(f"LLM_API_KEY={secret}\n")
        self.assertEqual(environment["LLM_API_KEY"], secret)
        self.assertNotIn(secret, output)


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

    def test_simple_cat_readme_is_allowed(self):
        self.assertEqual(classify_shell("cat README.md")["action"], "ALLOW")

    def test_simple_cat_verification_file_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            verification_file = Path(directory) / "verify_gate_test.txt"
            verification_file.write_text("verified\n", encoding="utf-8")
            previous_directory = os.getcwd()
            try:
                os.chdir(directory)
                self.assertEqual(
                    classify_shell("cat verify_gate_test.txt")["action"],
                    "ALLOW",
                )
                self.assertNotEqual(
                    classify_shell("cat missing.txt")["action"], "ALLOW"
                )
                self.assertNotEqual(
                    classify_shell("cat /etc/passwd")["action"], "ALLOW"
                )
                self.assertNotEqual(
                    classify_shell("cat ../secret")["action"], "ALLOW"
                )
                for command in (
                    "cat verify_gate_test.txt | grep verified",
                    "cat verify_gate_test.txt > copy.txt",
                    "cat $(pwd)",
                ):
                    with self.subTest(command=command):
                        self.assertNotEqual(
                            classify_shell(command)["action"], "ALLOW"
                        )
            finally:
                os.chdir(previous_directory)

    def test_cat_absolute_path_is_not_allowed(self):
        self.assertNotEqual(classify_shell("cat /etc/passwd")["action"], "ALLOW")

    def test_cat_parent_escape_is_not_allowed(self):
        self.assertNotEqual(classify_shell("cat ../secret")["action"], "ALLOW")

    def test_cat_pipe_is_not_allowed(self):
        self.assertNotEqual(classify_shell("cat x | grep y")["action"], "ALLOW")

    def test_cat_redirection_is_not_allowed(self):
        self.assertNotEqual(classify_shell("cat x > y")["action"], "ALLOW")

    def test_cat_command_substitution_is_not_allowed(self):
        self.assertNotEqual(classify_shell("cat $(pwd)")["action"], "ALLOW")

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


class VerificationQualityTests(unittest.TestCase):
    def test_extracts_supported_targets(self):
        cases = {
            "echo 'hello' > ./file.txt": {
                "target_type": "file", "path": "file.txt",
            },
            "touch file.txt": {"target_type": "file", "path": "file.txt"},
            "mkdir dirname": {
                "target_type": "directory", "path": "dirname",
            },
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(extract_verification_target(command), expected)

    def test_rejects_unsafe_or_ambiguous_targets(self):
        for command in (
            "touch /tmp/file.txt",
            "touch ../file.txt",
            "touch one.txt two.txt",
            "echo hi | tee file.txt",
            "echo hi >> file.txt",
            "echo $(date) > file.txt",
        ):
            with self.subTest(command=command):
                self.assertIsNone(extract_verification_target(command))

    def test_file_evidence_must_cat_same_path(self):
        target = {"target_type": "file", "path": "README.md"}
        self.assertTrue(is_related_verification("cat ./README.md", target))
        self.assertFalse(is_related_verification("cat other.txt", target))
        self.assertFalse(is_related_verification("pwd", target))

    def test_directory_evidence_must_ls_same_path(self):
        target = {"target_type": "directory", "path": "docs"}
        self.assertTrue(is_related_verification("ls -la ./docs", target))
        self.assertFalse(is_related_verification("ls other", target))


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


class SequenceProvider:
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.calls = []

    def complete(self, messages):
        self.calls.append(list(messages))
        return next(self.decisions)


class ApprovalGateTests(unittest.TestCase):
    @patch("mini_harness.execute_shell")
    @patch("builtins.input", return_value="y")
    def test_ask_and_user_y_executes(self, user_input, shell):
        shell.return_value = {"stdout": "", "stderr": "", "exit_code": 0}
        provider = SequenceProvider([
            {"type": "tool_call", "command": "touch README.md"},
            {"type": "tool_call", "command": "cat README.md"},
            {"type": "final_answer", "final_answer": "done"},
        ])

        run_agent("创建文件", provider)

        user_input.assert_called_once()
        self.assertEqual(
            [call.args[0] for call in shell.call_args_list],
            ["touch README.md", "cat README.md"],
        )

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


class VerificationGateTests(unittest.TestCase):
    @patch("mini_harness.execute_shell")
    @patch("mini_harness.request_approval", return_value=True)
    def test_unrelated_allow_is_not_executed_and_feedback_reaches_provider(
        self, approval, shell
    ):
        shell.side_effect = [
            {"stdout": "", "stderr": "", "exit_code": 0},
            {"stdout": "contents", "stderr": "", "exit_code": 0},
        ]
        provider = SequenceProvider([
            {"type": "tool_call", "command": "touch README.md"},
            {"type": "tool_call", "command": "pwd"},
            {"type": "tool_call", "command": "cat README.md"},
            {"type": "final_answer", "final_answer": "verified"},
        ])

        self.assertEqual(run_agent("写入文件", provider), "verified")

        self.assertEqual(
            [call.args[0] for call in shell.call_args_list],
            ["touch README.md", "cat README.md"],
        )
        feedback = json.loads(provider.calls[2][-1]["content"])
        self.assertEqual(feedback["denied_by"], "verification_quality")
        self.assertEqual(
            feedback["stderr"],
            "verification evidence is not related to the modified target",
        )
        self.assertEqual(feedback["verification_target"], {
            "target_type": "file", "path": "README.md",
        })

    @patch("mini_harness.execute_shell")
    @patch("mini_harness.request_approval", return_value=True)
    def test_failed_related_verification_keeps_target(self, approval, shell):
        shell.side_effect = [
            {"stdout": "", "stderr": "", "exit_code": 0},
            {"stdout": "partial", "stderr": "cat failed", "exit_code": 1},
            {"stdout": "contents", "stderr": "", "exit_code": 0},
        ]
        provider = SequenceProvider([
            {"type": "tool_call", "command": "touch README.md"},
            {"type": "tool_call", "command": "cat README.md"},
            {"type": "final_answer", "final_answer": "too early"},
            {"type": "tool_call", "command": "cat README.md"},
            {"type": "final_answer", "final_answer": "verified"},
        ])

        self.assertEqual(run_agent("写入文件", provider), "verified")
        failed = json.loads(provider.calls[2][-1]["content"])
        self.assertEqual(failed["stderr"], "cat failed")
        feedback = json.loads(provider.calls[3][-1]["content"])
        self.assertEqual(feedback["verification_target"], {
            "target_type": "file", "path": "README.md",
        })

    @patch("mini_harness.execute_shell")
    @patch("mini_harness.request_approval", return_value=True)
    def test_unknown_target_explicitly_falls_back_to_v3(self, approval, shell):
        shell.side_effect = [
            {"stdout": "", "stderr": "", "exit_code": 0},
            {"stdout": "/workspace\n", "stderr": "", "exit_code": 0},
        ]
        provider = SequenceProvider([
            {"type": "tool_call", "command": "printf x > fallback.txt"},
            {"type": "tool_call", "command": "pwd"},
            {"type": "final_answer", "final_answer": "verified"},
        ])

        self.assertEqual(run_agent("复杂写入", provider), "verified")
        self.assertEqual(shell.call_count, 2)

    @patch("mini_harness.execute_shell")
    @patch("mini_harness.request_approval", return_value=True)
    def test_real_provider_receives_feedback_and_recovers_offline(
        self, approval, shell
    ):
        shell.side_effect = [
            {"stdout": "", "stderr": "", "exit_code": 0},
            {"stdout": "/workspace\n", "stderr": "", "exit_code": 0},
        ]
        client = StubClient([
            json.dumps({
                "type": "tool_call", "tool": "shell",
                "command": "touch README.md",
            }),
            json.dumps({"type": "final_answer", "final_answer": "too early"}),
            json.dumps({
                "type": "tool_call", "tool": "shell", "command": "cat README.md",
            }),
            json.dumps({"type": "final_answer", "final_answer": "verified"}),
        ])

        answer = run_agent("写入文件", RealProvider(client))

        self.assertEqual(answer, "verified")
        feedback_message = client.calls[2][-1]
        self.assertEqual(feedback_message["role"], "user")
        feedback = json.loads(feedback_message["content"])
        self.assertEqual(feedback["status"], "final_answer_rejected")
        self.assertEqual(feedback["required_next_action"]["policy_must_be"], "ALLOW")

    @patch("mini_harness.execute_shell")
    @patch("mini_harness.request_approval", return_value=True)
    def test_final_is_blocked_then_allow_verification_clears_gate(
        self, approval, shell
    ):
        shell.side_effect = [
            {"stdout": "", "stderr": "", "exit_code": 0},
            {"stdout": "/workspace\n", "stderr": "", "exit_code": 0},
        ]
        provider = SequenceProvider([
            {"type": "tool_call", "command": "touch README.md"},
            {"type": "final_answer", "final_answer": "too early"},
            {"type": "tool_call", "command": "cat README.md"},
            {"type": "final_answer", "final_answer": "verified"},
        ])

        answer = run_agent("写入文件", provider)

        self.assertEqual(answer, "verified")
        self.assertEqual(len(provider.calls), 4)
        feedback = json.loads(provider.calls[2][-1]["content"])
        self.assertEqual(provider.calls[2][-1]["role"], "user")
        self.assertEqual(
            feedback["reason"], "verification required before final answer"
        )
        self.assertFalse(feedback["final_answer_allowed"])
        self.assertEqual(feedback["required_next_action"]["type"], "tool_call")
        self.assertEqual(feedback["required_next_action"]["tool"], "shell")
        self.assertEqual(
            feedback["required_next_action"]["policy_must_be"], "ALLOW"
        )
        self.assertEqual(feedback["write_operation_to_verify"], "touch README.md")
        approval.assert_called_once()

    @patch("mini_harness.execute_shell")
    @patch("mini_harness.request_approval", return_value=True)
    def test_failed_allow_verification_keeps_gate(self, approval, shell):
        shell.side_effect = [
            {"stdout": "", "stderr": "", "exit_code": 0},
            {"stdout": "partial", "stderr": "pwd failed", "exit_code": 1},
            {"stdout": "/workspace\n", "stderr": "", "exit_code": 0},
        ]
        provider = SequenceProvider([
            {"type": "tool_call", "command": "touch README.md"},
            {"type": "tool_call", "command": "cat README.md"},
            {"type": "final_answer", "final_answer": "still too early"},
            {"type": "tool_call", "command": "cat README.md"},
            {"type": "final_answer", "final_answer": "verified"},
        ])

        answer = run_agent("写入文件", provider)

        self.assertEqual(answer, "verified")
        failed_observation = json.loads(provider.calls[2][-1]["content"])
        self.assertEqual(failed_observation["stdout"], "partial")
        self.assertEqual(failed_observation["stderr"], "pwd failed")
        self.assertEqual(failed_observation["exit_code"], 1)
        self.assertEqual(provider.calls[3][-1]["role"], "user")

    @patch("mini_harness.execute_shell")
    @patch("mini_harness.request_approval", return_value=True)
    def test_repeated_rejected_final_answer_fails_without_spinning(
        self, approval, shell
    ):
        shell.return_value = {"stdout": "", "stderr": "", "exit_code": 0}
        provider = SequenceProvider([
            {"type": "tool_call", "command": "touch file.txt"},
            {"type": "final_answer", "final_answer": "too early"},
            {"type": "final_answer", "final_answer": "too early"},
        ])

        with self.assertRaisesRegex(RuntimeError, "重复提交"):
            run_agent("写入文件", provider, max_steps=10)

        self.assertEqual(len(provider.calls), 3)

    def test_fake_provider_can_act_on_verification_feedback(self):
        feedback = {
            "type": "verification_feedback",
            "status": "final_answer_rejected",
        }

        decision = FakeProvider().complete([
            {"role": "user", "content": "写入文件"},
            {"role": "user", "content": json.dumps(feedback)},
        ])

        self.assertEqual(decision, {"type": "tool_call", "command": "pwd"})

    @patch("mini_harness.execute_shell")
    @patch("mini_harness.request_approval", return_value=True)
    def test_ask_during_verification_neither_asks_nor_executes(
        self, approval, shell
    ):
        shell.side_effect = [
            {"stdout": "", "stderr": "", "exit_code": 0},
            {"stdout": "/workspace\n", "stderr": "", "exit_code": 0},
        ]
        provider = SequenceProvider([
            {"type": "tool_call", "command": "touch README.md"},
            {"type": "tool_call", "command": "touch second.txt"},
            {"type": "tool_call", "command": "cat README.md"},
            {"type": "final_answer", "final_answer": "verified"},
        ])

        run_agent("写入文件", provider)

        approval.assert_called_once()
        self.assertEqual(shell.call_count, 2)
        blocked = json.loads(provider.calls[2][-1]["content"])
        self.assertEqual(blocked["stderr"], "verification tool must be read-only")

    @patch("mini_harness.execute_shell")
    @patch("mini_harness.request_approval", return_value=True)
    def test_deny_during_verification_is_not_executed(self, approval, shell):
        shell.side_effect = [
            {"stdout": "", "stderr": "", "exit_code": 0},
            {"stdout": "/workspace\n", "stderr": "", "exit_code": 0},
        ]
        provider = SequenceProvider([
            {"type": "tool_call", "command": "touch README.md"},
            {"type": "tool_call", "command": "rm -rf README.md"},
            {"type": "tool_call", "command": "cat README.md"},
            {"type": "final_answer", "final_answer": "verified"},
        ])

        run_agent("写入文件", provider)

        self.assertEqual(shell.call_count, 2)
        denied = json.loads(provider.calls[2][-1]["content"])
        self.assertEqual(denied["denied_by"], "policy")
        approval.assert_called_once()

    @patch("mini_harness.execute_shell")
    @patch("mini_harness.request_approval", return_value=True)
    def test_failed_ask_does_not_trigger_verification(self, approval, shell):
        shell.return_value = {"stdout": "", "stderr": "failed", "exit_code": 1}
        provider = SequenceProvider([
            {"type": "tool_call", "command": "touch file.txt"},
            {"type": "final_answer", "final_answer": "reported failure"},
        ])

        answer = run_agent("写入文件", provider)

        self.assertEqual(answer, "reported failure")
        self.assertEqual(len(provider.calls), 2)


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
