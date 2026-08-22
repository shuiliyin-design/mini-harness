import hashlib
import json
import subprocess
import unittest
from unittest import mock

from mini_harness_core.termux_capabilities import (
    BATTERY_EXECUTABLE,
    CAPABILITY_NOT_INSTALLED,
    COMPANION_UNAVAILABLE,
    EXECUTION_FAILED,
    INVALID_RESPONSE,
    MAX_OUTPUT_BYTES,
    TIMEOUT,
    invoke_termux_capability,
)


def completed(stdout=b"{}", stderr=b"", returncode=0):
    return subprocess.CompletedProcess([BATTERY_EXECUTABLE], returncode,
                                       stdout, stderr)


class TermuxCapabilityTests(unittest.TestCase):
    def invoke(self, value):
        with mock.patch("mini_harness_core.termux_capabilities.subprocess.run",
                        return_value=value) as runner:
            result = invoke_termux_capability("battery_status")
        return result, runner

    def test_battery_json_success(self):
        raw = json.dumps({"present": True, "percentage": 73,
                          "status": "DISCHARGING"}).encode()
        result, runner = self.invoke(completed(raw))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["effect"], "read_only")
        self.assertEqual(result["observation"]["percentage"], 73)
        self.assertEqual(result["stdout_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertNotIn("stdout", result)
        args, kwargs = runner.call_args
        self.assertEqual(args[0], [BATTERY_EXECUTABLE])
        self.assertIs(kwargs["shell"], False)
        self.assertNotIn("Authorization", kwargs["env"])
        self.assertNotIn("API_KEY", kwargs["env"])

    def test_executable_missing(self):
        with mock.patch("mini_harness_core.termux_capabilities.subprocess.run",
                        side_effect=FileNotFoundError):
            result = invoke_termux_capability("battery_status")
        self.assertEqual(result["error_code"], CAPABILITY_NOT_INSTALLED)

    def test_timeout(self):
        error = subprocess.TimeoutExpired([BATTERY_EXECUTABLE], 10,
                                          output=b"partial", stderr=b"late")
        with mock.patch("mini_harness_core.termux_capabilities.subprocess.run",
                        side_effect=error):
            result = invoke_termux_capability("battery_status")
        self.assertEqual(result["error_code"], TIMEOUT)
        self.assertNotIn("stdout", result)

    def test_malformed_json(self):
        result, _ = self.invoke(completed(b"not json"))
        self.assertEqual(result["error_code"], INVALID_RESPONSE)

    def test_nonzero_exit(self):
        result, _ = self.invoke(completed(b"", b"ordinary failure", 2))
        self.assertEqual(result["error_code"], EXECUTION_FAILED)
        self.assertEqual(result["effect"], "read_only")

    def test_companion_unavailable(self):
        result, _ = self.invoke(completed(
            b"", b"Termux:API is not yet available", 1))
        self.assertEqual(result["error_code"], COMPANION_UNAVAILABLE)

    def test_oversized_output(self):
        result, _ = self.invoke(completed(b"x" * (MAX_OUTPUT_BYTES + 1)))
        self.assertEqual(result["error_code"], INVALID_RESPONSE)
        self.assertNotIn("stdout", result)

    def test_unknown_capability_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown"):
            invoke_termux_capability("shell")

    def test_caller_cannot_inject_argv(self):
        with self.assertRaises(TypeError):
            invoke_termux_capability("battery_status", ["--help"])

    def test_raw_streams_are_never_returned(self):
        result, _ = self.invoke(completed(b'{"percentage": 42}', b"warning"))
        self.assertNotIn("stdout", result)
        self.assertNotIn("stderr", result)
        self.assertEqual(result["stderr_length"], 7)

    def test_effect_is_read_only_on_every_failure(self):
        for value in (completed(b"bad"), completed(b"", b"failure", 1)):
            with self.subTest(value=value):
                result, _ = self.invoke(value)
                self.assertEqual(result["effect"], "read_only")

    def test_unexpected_fields_are_ignored(self):
        raw = json.dumps({"percentage": 88, "vendor_blob": {"x": 1}}).encode()
        result, _ = self.invoke(completed(raw))
        self.assertEqual(result["observation"], {"percentage": 88})

    def test_secret_like_allowed_field_fails_closed(self):
        raw = json.dumps({"percentage": 88,
                          "status": "Authorization: Bearer abc"}).encode()
        result, _ = self.invoke(completed(raw))
        self.assertEqual(result["error_code"], INVALID_RESPONSE)
        self.assertEqual(result["observation"], {})
        self.assertNotIn("stdout", result)


if __name__ == "__main__":
    unittest.main()
