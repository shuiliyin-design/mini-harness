import hashlib
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from mini_harness_core.dispatch import (
    authorize_action, environment_invocation_from_authorized,
)
from mini_harness_core.durability import create_action_checkpoint
from mini_harness_core.environment.contracts import (
    EnvironmentAdapterResult, EnvironmentCapabilitySpec,
    EnvironmentInvocation, input_schema_identity,
)
from mini_harness_core.environment.registry import (
    BATTERY, ENVIRONMENT_REGISTRY, NOTIFICATION,
    UnsupportedEnvironmentCapability,
)
from mini_harness_core.integrity import canonical_json_bytes, sha256_identity
from mini_harness_core.environment.termux import (
    BATTERY_EXECUTABLE, NOTIFICATION_EXECUTABLE,
    invoke_termux_capability, invoke_termux_notification,
)


EMPTY = hashlib.sha256(b"").hexdigest()


class EnvironmentContractTests(unittest.TestCase):
    def test_battery_conforms_and_has_no_side_effect(self):
        completed = subprocess.CompletedProcess(
            [BATTERY_EXECUTABLE], 0, b'{"percentage":71}', b"",
        )
        with mock.patch("mini_harness_core.environment.termux.subprocess.run",
                        return_value=completed):
            result = invoke_termux_capability("battery_status")
        self.assertIsInstance(result, EnvironmentAdapterResult)
        self.assertEqual(result.effect_certainty, "no_side_effect")
        self.assertEqual(result.safe_observation["percentage"], 71)

    def test_notification_success_conforms_and_is_known_applied(self):
        completed = subprocess.CompletedProcess([NOTIFICATION_EXECUTABLE], 0,
                                                b"", b"")
        with mock.patch("mini_harness_core.environment.termux.subprocess.run",
                        return_value=completed):
            result = invoke_termux_notification("Title", "Content")
        self.assertIsInstance(result, EnvironmentAdapterResult)
        self.assertEqual(result.effect_certainty, "known_applied")
        self.assertTrue(result.safe_observation["request_accepted"])

    def test_notification_timeout_unknown_and_missing_is_not_started(self):
        with mock.patch(
            "mini_harness_core.environment.termux.subprocess.run",
            side_effect=subprocess.TimeoutExpired([NOTIFICATION_EXECUTABLE], 10),
        ):
            timeout = invoke_termux_notification("Title", "Content")
        with mock.patch(
            "mini_harness_core.environment.termux.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            missing = invoke_termux_notification("Title", "Content")
        self.assertEqual(timeout.effect_certainty, "unknown")
        self.assertEqual(missing.effect_certainty, "not_started")

    def test_result_has_no_authority_or_raw_stream_fields(self):
        result = EnvironmentAdapterResult(
            BATTERY, "succeeded", "read_only", "no_side_effect", {}, 0,
            0, EMPTY, 0, EMPTY,
        ).to_dict()
        forbidden = {"policy", "approval", "retry", "evidence", "result",
                     "stdout", "stderr"}
        self.assertTrue(forbidden.isdisjoint(result))
        with self.assertRaises(TypeError):
            EnvironmentAdapterResult(
                BATTERY, "succeeded", "read_only", "no_side_effect", {}, 0,
                0, EMPTY, 0, EMPTY, policy="ALLOW",
            )

    def test_invocation_cannot_be_caller_constructed_or_inject_host_fields(self):
        with self.assertRaises(PermissionError):
            EnvironmentInvocation(BATTERY, {}, "a" * 32, "b" * 32)
        checkpoint = create_action_checkpoint(BATTERY, {}, "read_only")
        action = authorize_action(
            checkpoint=checkpoint, capability=BATTERY, arguments={},
            effect="read_only", policy_decision="ALLOW",
            approval_granted=True, run_id="b" * 32,
        )
        invocation = environment_invocation_from_authorized(action)
        self.assertEqual(invocation.normalized_args, {})
        self.assertFalse(hasattr(invocation, "executable"))
        self.assertFalse(hasattr(invocation, "argv"))

    def test_unknown_capability_fails_closed(self):
        with self.assertRaises(UnsupportedEnvironmentCapability):
            ENVIRONMENT_REGISTRY.spec("termux:unknown")
        with self.assertRaises(UnsupportedEnvironmentCapability):
            ENVIRONMENT_REGISTRY.normalize_arguments("termux:unknown", {})

    def test_registry_is_immutable_and_specs_have_no_authority(self):
        with self.assertRaises(AttributeError):
            ENVIRONMENT_REGISTRY.new_entry = object()
        spec = ENVIRONMENT_REGISTRY.spec(NOTIFICATION)
        self.assertIsInstance(spec, EnvironmentCapabilitySpec)
        self.assertTrue({"policy", "approval", "retry", "evidence", "result"}
                        .isdisjoint(spec.to_dict()))

    def test_registry_fingerprint_is_deterministic_and_formalized(self):
        first = ENVIRONMENT_REGISTRY.identity()
        second = ENVIRONMENT_REGISTRY.identity()
        self.assertEqual(first, second)
        self.assertEqual([item["logical_name"] for item in first["capabilities"]],
                         sorted((BATTERY, NOTIFICATION)))
        stable = {"schema_version": first["schema_version"],
                  "capabilities": first["capabilities"]}
        self.assertEqual(first["fingerprint"],
                         sha256_identity(canonical_json_bytes(stable)))
        legacy = {"schema_version": 1, "capabilities": [{
            "logical_capability": BATTERY, "effect": "read_only",
            "zone": "external", "adapter": "termux_api",
            "adapter_version": 1,
        }]}
        self.assertNotEqual(first["fingerprint"],
                            sha256_identity(canonical_json_bytes(legacy)))

    def test_model_catalog_hides_execution_mechanics(self):
        catalog = json.dumps(ENVIRONMENT_REGISTRY.model_catalog())
        for forbidden in ("/data/data/com.termux", "executable", "argv",
                          "adapter_id", "adapter_version"):
            self.assertNotIn(forbidden, catalog)

    def test_agent_has_one_generic_environment_handler(self):
        source = Path("mini_harness_core/agent.py").read_text(encoding="utf-8")
        self.assertIn("_handle_environment_decision", source)
        self.assertNotIn("_handle_termux_decision", source)
        self.assertNotIn("termux:battery_status", source)
        self.assertNotIn("termux:notification", source)
        self.assertNotIn("invoke_termux_", source)

    def test_input_schema_identity_is_canonical(self):
        left = {"type": "object", "properties": {"x": {"type": "string"}}}
        right = {"properties": {"x": {"type": "string"}}, "type": "object"}
        self.assertEqual(input_schema_identity(left), input_schema_identity(right))


if __name__ == "__main__":
    unittest.main()
