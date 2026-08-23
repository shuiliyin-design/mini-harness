"""Deterministic delivery adapters; raw provider responses never cross the port."""

from mini_harness_core.environment.termux import (
    NOTIFICATION_LOGICAL_CAPABILITY, validate_termux_notification_arguments,
)

from ..domain import DeliveryOutcome, DomainError, safe_digest_preview


class FakeDeliveryAdapter:
    channel = "fake"
    MODES = frozenset({"accepted", "explicit_failure", "timeout_unknown"})

    def __init__(self, mode="accepted"):
        if mode not in self.MODES:
            raise ValueError("unknown FakeDeliveryAdapter mode")
        self.mode = mode
        self.calls = []
        self.raw_responses = []

    def dispatch(self, request):
        self.calls.append(request)
        self.raw_responses.append({
            "provider_debug": "RAW_PROVIDER_RESPONSE_DO_NOT_PERSIST",
            "attempt_id": request.attempt_id,
        })
        if self.mode == "accepted":
            return DeliveryOutcome(
                "accepted", "known_applied",
                provider_message_id=f"fake-{request.attempt_id}",
            )
        if self.mode == "explicit_failure":
            return DeliveryOutcome(
                "failed", "not_started", error_code="FAKE_REJECTED",
            )
        return DeliveryOutcome(
            "unknown", "unknown", error_code="TIMEOUT",
        )


class TermuxNotificationDeliveryAdapter:
    """Map an already-authorized Environment dispatch result to app semantics."""

    channel = "termux_notification"

    def __init__(self, authorized_dispatcher):
        if not callable(authorized_dispatcher):
            raise TypeError("authorized_dispatcher must be callable")
        self.authorized_dispatcher = authorized_dispatcher
        self.calls = []

    def dispatch(self, request):
        if request.channel != self.channel:
            raise DomainError("Termux adapter channel mismatch")
        arguments = validate_termux_notification_arguments({
            "title": request.title, "content": request.content,
        })
        self.calls.append(dict(arguments))
        result = self.authorized_dispatcher(
            NOTIFICATION_LOGICAL_CAPABILITY, arguments,
        )
        certainty = result.get("effect_certainty")
        error_code = result.get("error_code")
        if result.get("status") == "succeeded" and certainty == "known_applied":
            return DeliveryOutcome("accepted", "known_applied")
        if certainty == "not_started":
            return DeliveryOutcome(
                "failed", "not_started", error_code=error_code or "NOT_STARTED",
            )
        return DeliveryOutcome(
            "unknown", "unknown", error_code=error_code or "UNKNOWN_EFFECT",
        )
