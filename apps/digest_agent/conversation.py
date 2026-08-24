"""Durable multi-turn Subscription Definition workflow."""

from dataclasses import dataclass
import json
import os
import unicodedata
import uuid

from mini_harness_core.agent import run_agent
from mini_harness_core.audit import AuditWriter
from mini_harness_core.providers import ProviderError
from mini_harness_core.result import ResultError, ResultStore
from mini_harness_core.security import SECRET_PATTERNS

from .adapters.provider import ProviderAdapterError
from .domain import (
    Conversation, ConversationTurn, DefinitionOutcome, DomainError,
    definition_candidate_identity, definition_outcome_identity,
    normalize_definition_envelope, utc_now, validate_definition_protocol,
)


SAFE_CONVERSATION_FAILURES = frozenset({
    "invalid_candidate", "definition_incomplete",
    "definition_recovery_required", "turn_limit_reached",
})


class ConversationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _DefinitionHarnessProvider:
    """Translate one app protocol candidate into a Harness final candidate."""

    SYSTEM_PROMPT = (
        "You execute one Subscription Definition Agent turn. The application "
        "adapter may propose NEXT_QUESTION, REJECT, or DONE. Return that proposal "
        "as a final candidate only. It cannot create a Subscription, relation, "
        "Digest, delivery, or outbox, and it grants no application authority."
    )

    def __init__(self, adapter, context):
        self.adapter = adapter
        self.context = context

    def complete(self, _messages):
        try:
            raw = self.adapter.propose(self.context)
            candidate = normalize_definition_envelope(raw)
            encoded = json.dumps(
                candidate, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            )
            if any(pattern.search(encoded) for pattern in SECRET_PATTERNS):
                raise ProviderError("definition candidate failed secret screening")
        except (ProviderAdapterError, DomainError, TypeError, ValueError) as error:
            raise ProviderError("definition provider returned no safe candidate") from error
        return {
            "type": "final_answer",
            "final_answer": encoded,
            "claimed_status": "completed",
        }


@dataclass(frozen=True, slots=True)
class ConversationExecution:
    conversation: Conversation
    turn: ConversationTurn
    outcome: DefinitionOutcome | None
    reused: bool


class DefinitionConversationWorkflow:
    """Persist user intent first, then run and project one Definition turn."""

    def __init__(self, repository, provider, audit_directory, *,
                 id_factory=None, clock=None, maximum_turns=8,
                 owner_id=None, fault_injector=None):
        if (type(maximum_turns) is not int or not 1 <= maximum_turns <= 100):
            raise ValueError("invalid conversation turn ceiling")
        self.repository = repository
        self.provider = provider
        self.audit_directory = str(audit_directory)
        self.id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self.clock = clock or utc_now
        self.maximum_turns = maximum_turns
        self.owner_id = owner_id or uuid.uuid4().hex
        self.fault_injector = fault_injector

    def _fault(self, stage, turn):
        if self.fault_injector is not None:
            self.fault_injector(stage, turn)

    @staticmethod
    def _safe_user_text(value):
        if not isinstance(value, str):
            raise ConversationError("invalid_conversation_message")
        value = value.strip()
        if (not 1 <= len(value) <= 2_000
                or any(unicodedata.category(ch) == "Cc" for ch in value)
                or any(pattern.search(value) for pattern in SECRET_PATTERNS)):
            raise ConversationError("invalid_conversation_message")
        return value

    def _new_turn(self, conversation_id, number, text, idempotency_key,
                  timestamp):
        return ConversationTurn(
            self.id_factory(), conversation_id, number, "user", text,
            idempotency_key, self.id_factory(), "reserved", None, None,
            None, timestamp, timestamp,
        )

    def start(self, user_id, text, idempotency_key):
        safe_text = self._safe_user_text(text)
        timestamp = self.clock()
        conversation_id = self.id_factory()
        conversation = Conversation(
            conversation_id, user_id, "COLLECTING", 1,
            timestamp, timestamp, 1, idempotency_key,
        )
        turn = self._new_turn(
            conversation_id, 1, safe_text, idempotency_key, timestamp,
        )
        conversation, turn, created = self.repository.reserve_conversation(
            conversation, turn,
        )
        execution = ConversationExecution(
            conversation, turn,
            self.repository.get_definition_outcome_for_turn(turn.turn_id),
            not created,
        )
        if not created and turn.safe_text != safe_text:
            raise ConversationError("idempotency_conflict")
        if not created and turn.status in {"completed", "failed", "blocked"}:
            return execution
        self._fault("after_turn_reserved", turn)
        return self._execute(execution)

    def continue_conversation(self, user_id, conversation_id, text,
                              idempotency_key):
        safe_text = self._safe_user_text(text)
        conversation = self.repository.get_conversation(conversation_id)
        if conversation is None or conversation.user_id != user_id:
            raise ConversationError("not_found")
        existing = self.repository.get_conversation_turn_by_key(
            conversation_id, idempotency_key,
        )
        if existing is not None:
            if existing.safe_text != safe_text:
                raise ConversationError("idempotency_conflict")
            return self._execute(ConversationExecution(
                self.repository.get_conversation(conversation_id), existing,
                self.repository.get_definition_outcome_for_turn(existing.turn_id),
                True,
            ))
        if conversation.status != "WAITING_FOR_ANSWER":
            raise ConversationError("conversation_not_waiting")
        timestamp = self.clock()
        turn = self._new_turn(
            conversation_id, conversation.turn_count + 1,
            safe_text, idempotency_key, timestamp,
        )
        try:
            conversation, turn, created = (
                self.repository.reserve_conversation_turn(
                    conversation_id, user_id, turn, self.maximum_turns,
                    timestamp,
                )
            )
        except ValueError as error:
            raise ConversationError("conversation_not_waiting") from error
        execution = ConversationExecution(
            conversation, turn,
            self.repository.get_definition_outcome_for_turn(turn.turn_id),
            not created,
        )
        if not created and turn.status in {"completed", "failed", "blocked"}:
            return execution
        self._fault("after_turn_reserved", turn)
        return self._execute(execution)

    def _context(self, conversation_id, current_turn_id):
        turns = self.repository.list_conversation_turns(conversation_id)
        messages = []
        for turn in turns:
            messages.append({"role": "user", "content": turn.safe_text})
            outcome = self.repository.get_definition_outcome_for_turn(
                turn.turn_id,
            )
            if outcome is not None and turn.turn_id != current_turn_id:
                payload = outcome.payload
                if outcome.outcome_type == "NEXT_QUESTION":
                    content = payload["question"]
                elif outcome.outcome_type == "REJECT":
                    content = payload["reason"]
                else:
                    content = json.dumps(
                        payload["definition"], ensure_ascii=False,
                        sort_keys=True, separators=(",", ":"),
                    )
                messages.append({"role": "agent", "content": content})
        return {
            "conversation_id": conversation_id,
            "turn_count": len(turns), "messages": messages,
        }

    def _result_store(self):
        return ResultStore(os.path.join(self.audit_directory, "results"))

    def _load_result(self, turn):
        try:
            return self._result_store().load(turn.harness_run_id)
        except ResultError:
            path = os.path.join(
                self.audit_directory, "results", turn.harness_run_id + ".json",
            )
            if os.path.exists(path):
                self.repository.fail_conversation_turn(
                    turn.turn_id, "definition_recovery_required",
                    "INCOMPLETE", self.clock(),
                )
                return False
            return None

    def _execute(self, execution):
        turn = self.repository.get_conversation_turn(execution.turn.turn_id)
        outcome = self.repository.get_definition_outcome_for_turn(turn.turn_id)
        conversation = self.repository.get_conversation(turn.conversation_id)
        if outcome is not None or turn.status in {"completed", "failed", "blocked"}:
            return ConversationExecution(conversation, turn, outcome, execution.reused)
        result = self._load_result(turn)
        if result is False:
            turn = self.repository.get_conversation_turn(turn.turn_id)
            conversation = self.repository.get_conversation(turn.conversation_id)
            return ConversationExecution(conversation, turn, None, execution.reused)
        if result is None:
            claimed = self.repository.claim_conversation_turn(
                turn.turn_id, self.owner_id, self.clock(),
            )
            if claimed is None:
                return ConversationExecution(
                    self.repository.get_conversation(turn.conversation_id),
                    self.repository.get_conversation_turn(turn.turn_id),
                    None, True,
                )
            turn = claimed
            self._fault("after_turn_claimed", turn)
            context = self._context(turn.conversation_id, turn.turn_id)
            writer = AuditWriter(
                conversation.user_id, turn.harness_run_id,
                self.audit_directory,
            )
            result = run_agent(
                json.dumps(context, ensure_ascii=False, sort_keys=True),
                _DefinitionHarnessProvider(self.provider, context),
                max_steps=1, audit_writer=writer,
                result_store=self._result_store(), return_result=True,
            )
        self._fault("after_harness_result", turn)
        return self._persist_result(turn, result, execution.reused)

    def _persist_result(self, turn, result, reused):
        if not isinstance(result, dict) or result.get("status") != "completed":
            conversation, turn = self.repository.fail_conversation_turn(
                turn.turn_id, "definition_incomplete", "INCOMPLETE",
                self.clock(),
            )
            return ConversationExecution(conversation, turn, None, reused)
        try:
            raw = json.loads(result["answer"])
            normalized, _definition = validate_definition_protocol(raw)
        except (KeyError, TypeError, json.JSONDecodeError, DomainError):
            conversation, turn = self.repository.fail_conversation_turn(
                turn.turn_id, "invalid_candidate", "INCOMPLETE",
                self.clock(),
            )
            return ConversationExecution(conversation, turn, None, reused)
        timestamp = self.clock()
        outcome = DefinitionOutcome(
            definition_outcome_identity(turn.turn_id), turn.conversation_id,
            turn.turn_id, normalized["type"], normalized,
            definition_candidate_identity(normalized), timestamp,
        )
        if normalized["type"] == "DONE":
            status, reason = "DEFINITION_ACCEPTED", None
        elif normalized["type"] == "REJECT":
            status, reason = "REJECTED", None
        elif turn.turn_number >= self.maximum_turns:
            status, reason = "INCOMPLETE", "turn_limit_reached"
        else:
            status, reason = "WAITING_FOR_ANSWER", None
        conversation, turn, outcome = self.repository.finish_conversation_turn(
            turn, outcome, status, reason, timestamp,
        )
        return ConversationExecution(conversation, turn, outcome, reused)

    def get(self, user_id, conversation_id):
        conversation = self.repository.get_conversation(conversation_id)
        if conversation is None or conversation.user_id != user_id:
            raise ConversationError("not_found")
        turns = self.repository.list_conversation_turns(conversation_id)
        if not turns:
            raise ConversationError("not_found")
        turn = turns[-1]
        return ConversationExecution(
            conversation, turn,
            self.repository.get_definition_outcome_for_turn(turn.turn_id),
            True,
        )
