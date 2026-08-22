import subprocess
import unittest
from unittest import mock

from mini_harness_core.termux_capabilities import (
    CAPABILITY_NOT_INSTALLED,
    COMPANION_UNAVAILABLE,
    EXECUTION_FAILED,
    MAX_NOTIFICATION_CONTENT_BYTES,
    MAX_NOTIFICATION_TITLE_BYTES,
    NOTIFICATION_EXECUTABLE,
    NOTIFICATION_LOGICAL_CAPABILITY,
    TIMEOUT,
    invoke_termux_notification,
)


def completed(stdout=b"", stderr=b"", returncode=0):
    return subprocess.CompletedProcess(
        [NOTIFICATION_EXECUTABLE], returncode, stdout, stderr,
    )


class TermuxNotificationAdapterTests(unittest.TestCase):
    def invoke(self, value, title="Title", content="Content"):
        with mock.patch(
            "mini_harness_core.termux_capabilities.subprocess.run",
            return_value=value,
        ) as runner:
            result = invoke_termux_notification(title, content)
        return result, runner

    def test_normal_notification_fixed_argv_and_shell_false(self):
        result, runner = self.invoke(completed(), "Phase 2", "works")
        args, kwargs = runner.call_args
        self.assertEqual(args[0], [
            NOTIFICATION_EXECUTABLE, "--title", "Phase 2",
            "--content", "works",
        ])
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(result["capability"], NOTIFICATION_LOGICAL_CAPABILITY)
        self.assertEqual(result["effect"], "side_effecting")

    def test_title_and_content_are_preserved(self):
        _, runner = self.invoke(completed(), "标题", "通知内容")
        self.assertEqual(runner.call_args.args[0][2:], [
            "标题", "--content", "通知内容",
        ])

    def test_injection_text_remains_one_literal_argv_value(self):
        injection = "$(touch owned); echo injected"
        _, runner = self.invoke(completed(), injection, "a && rm -rf x")
        argv = runner.call_args.args[0]
        self.assertEqual(argv[2], injection)
        self.assertEqual(argv[4], "a && rm -rf x")
        self.assertEqual(len(argv), 5)

    def test_secret_rejected_before_subprocess(self):
        for title, content in (("API key", "x"), ("safe", "Bearer token-value"),
                               ("private key", "safe")):
            with self.subTest(title=title), mock.patch(
                "mini_harness_core.termux_capabilities.subprocess.run",
            ) as runner, self.assertRaises(ValueError):
                invoke_termux_notification(title, content)
            runner.assert_not_called()

    def test_oversized_and_control_text_rejected(self):
        values = (
            ("x" * (MAX_NOTIFICATION_TITLE_BYTES + 1), "ok"),
            ("ok", "x" * (MAX_NOTIFICATION_CONTENT_BYTES + 1)),
            ("bad\nline", "ok"), ("ok", "bad\x00content"),
        )
        for title, content in values:
            with self.subTest(title=title[:10]), self.assertRaises(ValueError):
                invoke_termux_notification(title, content)

    def test_missing_executable(self):
        with mock.patch(
            "mini_harness_core.termux_capabilities.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            result = invoke_termux_notification("Title", "Content")
        self.assertEqual(result["error_code"], CAPABILITY_NOT_INSTALLED)
        self.assertEqual(result["effect_certainty"], "not_started")

    def test_timeout_preserves_unknown_effect(self):
        error = subprocess.TimeoutExpired(
            [NOTIFICATION_EXECUTABLE], 10, output=b"partial", stderr=b"late",
        )
        with mock.patch(
            "mini_harness_core.termux_capabilities.subprocess.run",
            side_effect=error,
        ):
            result = invoke_termux_notification("Title", "Content")
        self.assertEqual(result["error_code"], TIMEOUT)
        self.assertEqual(result["effect_certainty"], "unknown")

    def test_nonzero_exit_and_companion_unavailable(self):
        ordinary, _ = self.invoke(completed(stderr=b"failure", returncode=2))
        companion, _ = self.invoke(completed(
            stderr=b"Termux:API is not yet available", returncode=1,
        ))
        self.assertEqual(ordinary["error_code"], EXECUTION_FAILED)
        self.assertEqual(companion["error_code"], COMPANION_UNAVAILABLE)
        self.assertEqual(ordinary["effect_certainty"], "unknown")

    def test_no_raw_streams_and_effect_always_side_effecting(self):
        for value in (completed(b"accepted", b"warning"),
                      completed(b"", b"failure", 1)):
            result, _ = self.invoke(value)
            self.assertNotIn("stdout", result)
            self.assertNotIn("stderr", result)
            self.assertEqual(result["effect"], "side_effecting")

    def test_success_claims_request_acceptance_not_user_visibility(self):
        result, _ = self.invoke(completed())
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["observation"]["notification_requested"])
        self.assertTrue(result["observation"]["request_accepted"])
        self.assertNotIn("notification_seen", result["observation"])

    def test_caller_cannot_override_executable_or_argv(self):
        with self.assertRaises(TypeError):
            invoke_termux_notification("Title", "Content", executable="evil")
        with self.assertRaises(TypeError):
            invoke_termux_notification("Title", "Content", ["--id", "x"])


if __name__ == "__main__":
    unittest.main()
