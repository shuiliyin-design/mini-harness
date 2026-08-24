"""Single-process manual worker for application-owned durable work."""

from dataclasses import dataclass

from .domain import utc_now


OUTBOX_PUBLIC_STATUSES = {
    "pending": "PENDING",
    "claimed": "CLAIMED",
    "retry_wait": "RETRYABLE",
    "completed": "SUCCEEDED",
    "failed": "FAILED",
    "blocked": "BLOCKED",
}
TERMINAL_RUN_STATUSES = frozenset({"completed", "incomplete", "failed"})


class OutboxWorkerError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class OutboxExecution:
    worker_status: str
    outbox_id: str | None
    outbox_status: str | None
    subscription_id: str | None
    application_run_id: str | None
    briefing_status: str | None
    digest_id: str | None
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class OutboxRecoveryFacts:
    outbox_id: str
    event_type: str
    outbox_status: str
    attempt_number: int
    subscription_id: str
    application_run_id: str
    briefing_status: str
    application_run_status: str
    binding_status: str
    terminal_result_available: bool
    safe_recovery_actions: tuple[str, ...]
    blocking_reason: str | None
    updated_at: str


class DurableOutboxWorker:
    """Claim one event, delegate generation, then project transport truth."""

    def __init__(self, repository, generation_workflow, *, clock=None,
                 fault_injector=None):
        self.repository = repository
        self.generation = generation_workflow
        self.clock = clock or utc_now
        self.fault_injector = fault_injector

    def _fault(self, stage, value):
        if self.fault_injector is not None:
            self.fault_injector(stage, value)

    @staticmethod
    def briefing_status(record, outbox=None):
        if record is None:
            if outbox is not None and outbox.status in {"blocked", "failed"}:
                return "BLOCKED" if outbox.status == "blocked" else "FAILED"
            return "PENDING"
        return {
            "reserved": "PENDING",
            "running": "RUNNING",
            "running_recovery": "RUNNING",
            "completed": "READY" if record.digest_id else "INCOMPLETE",
            "incomplete": "INCOMPLETE",
            "failed": "FAILED",
            "blocked": "BLOCKED",
            "recovery_required": "BLOCKED",
        }.get(record.status, "BLOCKED")

    def _canonical_resources(self, outbox):
        if outbox.event_type != "FIRST_BRIEFING_REQUESTED":
            raise OutboxWorkerError("unsupported_event")
        refs = outbox.payload_refs
        activation = self.repository.get_subscription_activation(
            refs["activation_id"],
        )
        reservation = self.repository.get_briefing_reservation(
            refs["application_run_id"],
        )
        definition = self.repository.get_subscription_definition(
            refs["definition_id"], refs["definition_version"],
        )
        subscription = self.repository.get_subscription(outbox.subscription_id)
        product = self.repository.get_product_subscription(
            outbox.subscription_id,
        )
        relation = self.repository.get_user_subscription_for_subscription(
            outbox.subscription_id,
        )
        if any(value is None for value in (
                activation, reservation, definition, subscription,
                product, relation)):
            raise OutboxWorkerError("invalid_durable_refs")
        if (activation.outbox_id != outbox.outbox_id
                or activation.subscription_id != outbox.subscription_id
                or activation.application_run_id != outbox.application_run_id
                or activation.definition_id != definition.definition_id
                or reservation.application_run_id != outbox.application_run_id
                or reservation.subscription_id != outbox.subscription_id
                or reservation.definition_id != definition.definition_id
                or reservation.definition_version
                != definition.definition_version
                or product.definition_id != definition.definition_id
                or product.definition_version != definition.definition_version
                or relation.user_subscription_id
                != activation.user_subscription_id):
            raise OutboxWorkerError("invalid_durable_refs")
        return reservation, definition, subscription, product, relation

    def _execution(self, worker_status, outbox, record=None,
                   failure_reason=None):
        return OutboxExecution(
            worker_status,
            outbox.outbox_id if outbox else None,
            OUTBOX_PUBLIC_STATUSES[outbox.status] if outbox else None,
            outbox.subscription_id if outbox else None,
            outbox.application_run_id if outbox else None,
            self.briefing_status(record, outbox) if outbox else None,
            record.digest_id if record else None,
            failure_reason,
        )

    def _finalize_terminal(self, outbox, record):
        record = self.repository.get_digest_run(record.digest_run_id)
        if record.status in TERMINAL_RUN_STATUSES:
            final = self.repository.finalize_application_outbox(
                outbox.outbox_id, outbox.version, "completed", None,
                self.clock(), self.clock(),
            )
            self._fault("after_outbox_success", final)
            return self._execution("PROCESSED", final, record)
        if record.status in {"blocked", "recovery_required"}:
            final = self.repository.finalize_application_outbox(
                outbox.outbox_id, outbox.version, "blocked",
                "recovery_required", self.clock(), self.clock(),
            )
            return self._execution(
                "RECOVERY_REQUIRED", final, record, "recovery_required",
            )
        raise OutboxWorkerError("application_run_not_terminal")

    def _process_owned(self, outbox):
        reservation, definition, subscription, product, relation = (
            self._canonical_resources(outbox)
        )
        if (product.status != "ACTIVE" or relation.status != "ACTIVE"
                or not subscription.enabled):
            final = self.repository.finalize_application_outbox(
                outbox.outbox_id, outbox.version, "blocked",
                "subscription_inactive", self.clock(), self.clock(),
            )
            return self._execution(
                "RECOVERY_REQUIRED", final, None, "subscription_inactive",
            )
        record, _created = self.generation.reserve_first_briefing(
            outbox.outbox_id, reservation, definition, subscription,
        )
        self._fault("after_run_reserved", record)
        if record.status == "reserved" and record.harness_bound_at is None:
            self.generation.execute_reserved(record)
        else:
            raise OutboxWorkerError("application_run_requires_recovery")
        current = self.repository.get_digest_run(record.digest_run_id)
        self._fault("after_execution", current)
        return self._finalize_terminal(outbox, current)

    def run_once(self, now=None):
        outbox = self.repository.claim_application_outbox(now or self.clock())
        if outbox is None:
            return OutboxExecution(
                "NO_WORK", None, None, None, None, None, None, None,
            )
        self._fault("after_claim", outbox)
        return self._process_owned(outbox)

    def drain(self, maximum, now=None):
        if type(maximum) is not int or not 1 <= maximum <= 100:
            raise OutboxWorkerError("invalid_drain_limit")
        results = []
        for _index in range(maximum):
            result = self.run_once(now)
            if result.worker_status == "NO_WORK":
                break
            results.append(result)
        return tuple(results)

    def inspect(self, outbox_id):
        outbox = self.repository.get_application_outbox(outbox_id)
        if outbox is None:
            raise OutboxWorkerError("not_found")
        record = self.repository.get_digest_run(outbox.application_run_id)
        actions = ()
        blocking = None
        binding = "unbound"
        terminal = False
        run_status = record.status if record else "not_materialized"
        if record is not None:
            binding = "bound" if record.harness_bound_at else "unbound"
            terminal = record.status in TERMINAL_RUN_STATUSES
        if outbox.status == "claimed":
            blocking = "MANUAL_RECOVERY_REQUIRED"
            if record is None:
                actions = ("release_not_started",)
            elif record.status in TERMINAL_RUN_STATUSES:
                actions = ("finalize_terminal_outcome",)
                terminal = True
            elif record.status in {"blocked", "recovery_required"}:
                actions = ("block_ambiguous_run",)
            else:
                facts = self.generation.inspect_recovery_facts(record)
                binding = facts["binding_status"]
                terminal = facts["terminal_result_available"]
                actions = tuple(facts["safe_recovery_actions"])
                if not actions:
                    actions = ("block_ambiguous_run",)
        elif outbox.status in {"pending", "retry_wait"}:
            blocking = "awaiting_claim"
        else:
            blocking = "terminal_outbox"
        return OutboxRecoveryFacts(
            outbox.outbox_id, outbox.event_type,
            OUTBOX_PUBLIC_STATUSES[outbox.status], outbox.attempt_number,
            outbox.subscription_id, outbox.application_run_id,
            self.briefing_status(record, outbox), run_status, binding,
            terminal, actions, blocking, outbox.updated_at,
        )

    def inspect_all(self):
        return tuple(self.inspect(item.outbox_id)
                     for item in self.repository.list_application_outbox())

    def recover(self, outbox_id, action):
        facts = self.inspect(outbox_id)
        if action not in facts.safe_recovery_actions:
            raise OutboxWorkerError("unsafe_recovery_action")
        outbox = self.repository.get_application_outbox(outbox_id)
        record = self.repository.get_digest_run(outbox.application_run_id)
        if action == "release_not_started":
            final = self.repository.finalize_application_outbox(
                outbox_id, outbox.version, "retry_wait", "not_started",
                self.clock(), self.clock(),
            )
            return self._execution("RETRYABLE", final)
        if action == "finalize_terminal_outcome":
            return self._finalize_terminal(outbox, record)
        if action == "resume_original_run":
            self.generation.execute_reserved(record)
        elif action == "resume_bound_run":
            self.generation.resume_bound_run(record)
        elif action == "repair_projection":
            self.generation.recover_projection(record)
        elif action == "block_ambiguous_run":
            if record is not None and record.status not in TERMINAL_RUN_STATUSES:
                record = self.repository.mark_digest_run_recovery_required(
                    record.digest_run_id, "recovery_required", self.clock(),
                )
            final = self.repository.finalize_application_outbox(
                outbox_id, outbox.version, "blocked", "recovery_required",
                self.clock(), self.clock(),
            )
            return self._execution(
                "RECOVERY_REQUIRED", final, record, "recovery_required",
            )
        else:  # pragma: no cover - guarded by the durable allowlist
            raise OutboxWorkerError("unsafe_recovery_action")
        current = self.repository.get_digest_run(outbox.application_run_id)
        self._fault("after_execution", current)
        return self._finalize_terminal(outbox, current)
