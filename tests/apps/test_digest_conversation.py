from dataclasses import asdict
import os
import tempfile
import threading
import unittest

from apps.digest_agent.application import ApplicationError, DigestApplication
from apps.digest_agent.adapters.definition import FakeDefinitionAgentAdapter
from apps.digest_agent.adapters.provider import ProviderAdapterError
from apps.digest_agent.adapters.sqlite import SQLiteDigestRepository
from apps.digest_agent.conversation import DefinitionConversationWorkflow


NOW = "2026-08-24T12:00:00Z"
USER = "a" * 32


def next_question(text="还需要补充什么？"):
    return {
        "protocol_version": 1, "type": "NEXT_QUESTION", "question": text,
    }


def reject(reason="当前请求不适合资讯订阅。"):
    return {"protocol_version": 1, "type": "REJECT", "reason": reason}


def done(**changes):
    definition = {
        "topic": "AI 行业动态", "language": "zh-CN",
        "cadence": "daily", "max_chars": 600, "max_items": 5,
        "focus_topics": ["Agent", "模型发布"],
        "delivery_preference": "none",
    }
    definition.update(changes)
    return {
        "protocol_version": 1, "type": "DONE",
        "definition": definition,
    }


class IdFactory:
    def __init__(self, start=1):
        self.value = start

    def __call__(self):
        value = f"{self.value:032x}"
        self.value += 1
        return value


class DefinitionConversationTests(unittest.TestCase):
    def make(self, root, outcomes, *, maximum_turns=8,
             fault_injector=None, ids=None, owner="f" * 32,
             database=None, provider=None):
        repository = SQLiteDigestRepository(
            database or os.path.join(root, "digest.db"),
        )
        provider = provider or FakeDefinitionAgentAdapter(outcomes)
        workflow = DefinitionConversationWorkflow(
            repository, provider, os.path.join(root, "audit"),
            id_factory=ids or IdFactory(), clock=lambda: NOW,
            maximum_turns=maximum_turns, owner_id=owner,
            fault_injector=fault_injector,
        )
        app = DigestApplication(
            repository, None, None, None, None, workflow,
        )
        return app, repository, provider, workflow

    def test_fake_agent_runs_intent_driven_product_journeys(self):
        journeys = (
            (
                "ai", "帮我关注 AI Agent 行业动态", "技术进展",
                "AI Agent 行业动态", "focus_topics", "技术进展",
            ),
            (
                "flight", "帮我关注深圳往返武汉的机票优惠",
                "9 月往返，低于 800 元时提醒我",
                "深圳往返武汉的机票优惠", "constraints", "低于800元",
            ),
        )
        for name, request, answer, topic, confirmed_field, expected in journeys:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                app, repository, _provider, _workflow = self.make(root, None)
                first = app.start_subscription_conversation(
                    USER, request, f"{name}-start",
                )
                self.assertEqual(first.status, "WAITING_FOR_ANSWER")
                self.assertNotRegex(first.question, r"字|条|schema|config")
                terminal = app.continue_subscription_conversation(
                    USER, first.conversation_id, answer, f"{name}-answer",
                )
                self.assertEqual(terminal.status, "DEFINITION_ACCEPTED")
                self.assertEqual(terminal.definition["topic"], topic)
                rendered = str(terminal.definition)
                self.assertIn(expected, rendered)
                self.assertNotIn("AI 行业动态", rendered)
                self.assertEqual(
                    terminal.definition["provenance"]["max_chars"],
                    "PRODUCT_DEFAULT",
                )
                self.assertEqual(
                    terminal.definition["provenance"][confirmed_field],
                    "USER_CONFIRMED",
                )
                self.assertEqual(repository.list_subscriptions(), ())

    def test_vague_flight_asks_only_remaining_material_ambiguities(self):
        with tempfile.TemporaryDirectory() as root:
            app, repository, _provider, _workflow = self.make(root, None)
            first = app.start_subscription_conversation(
                USER, "帮我关注深圳往返武汉的机票优惠", "flight-start",
            )
            self.assertEqual(first.status, "WAITING_FOR_ANSWER")
            self.assertIn("日期", first.question)
            second = app.continue_subscription_conversation(
                USER, first.conversation_id, "9 月往返", "flight-date",
            )
            self.assertEqual(second.status, "WAITING_FOR_ANSWER")
            self.assertIn("什么价格算优惠", second.question)
            self.assertNotRegex(second.question, r"字|条|schema|config")
            accepted = app.continue_subscription_conversation(
                USER, first.conversation_id, "低于 800 元时提醒我",
                "flight-price",
            )
            self.assertEqual(
                (accepted.status, accepted.turn_count,
                 accepted.definition["topic"],
                 accepted.definition["time_window"],
                 accepted.definition["constraints"]),
                ("DEFINITION_ACCEPTED", 3, "深圳往返武汉的机票优惠",
                 "9 月", ["低于800元"]),
            )
            self.assertEqual(
                accepted.definition["provenance"]["constraints"],
                "USER_CONFIRMED",
            )
            self.assertNotIn(
                "AI 行业动态", str(accepted.definition),
            )
            self.assertEqual(repository.list_subscriptions(), ())

    def test_fake_agent_accepts_sufficient_intents_without_schema_questions(self):
        immediate = (
            (
                "explicit-flight",
                "关注深圳到武汉 9 月往返机票，低于 800 元提醒我",
                "深圳往返武汉的机票优惠", "低于800元",
            ),
            (
                "event", "关注 OpenAI 新模型发布，有新模型就提醒我",
                "OpenAI 新模型发布", "出现新模型时提醒",
            ),
        )
        for name, request, topic, trigger in immediate:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                app, repository, _provider, _workflow = self.make(root, None)
                accepted = app.start_subscription_conversation(
                    USER, request, f"{name}-start",
                )
                self.assertEqual(accepted.status, "DEFINITION_ACCEPTED")
                self.assertEqual(accepted.turn_count, 1)
                self.assertEqual(accepted.definition["topic"], topic)
                self.assertIn(trigger, str(accepted.definition))
                self.assertEqual(
                    accepted.definition["provenance"]["topic"],
                    "USER_EXPLICIT",
                )
                self.assertEqual(
                    accepted.definition["provenance"]["max_items"],
                    "PRODUCT_DEFAULT",
                )
                self.assertEqual(repository.list_subscriptions(), ())

    def test_initial_and_multiple_next_question_then_done(self):
        with tempfile.TemporaryDirectory() as root:
            app, repository, provider, _workflow = self.make(root, [
                next_question("你更关心技术进展还是行业应用？"),
                next_question("只关注重大变化，还是也关注日常案例？"),
                done(),
            ])
            first = app.start_subscription_conversation(
                USER, "帮我订阅 AI 行业动态", "start-1",
            )
            self.assertEqual(
                (first.status, first.question, first.turn_count),
                ("WAITING_FOR_ANSWER", "你更关心技术进展还是行业应用？", 1),
            )
            second = app.continue_subscription_conversation(
                USER, first.conversation_id, "技术进展", "answer-1",
            )
            self.assertEqual(
                (second.status, second.question, second.turn_count),
                ("WAITING_FOR_ANSWER", "只关注重大变化，还是也关注日常案例？", 2),
            )
            terminal = app.continue_subscription_conversation(
                USER, first.conversation_id, "只关注重大变化", "answer-2",
            )
            self.assertEqual(terminal.status, "DEFINITION_ACCEPTED")
            self.assertEqual(terminal.latest_outcome, "DONE")
            self.assertEqual(terminal.definition["max_chars"], 600)
            self.assertEqual(len(provider.calls), 3)
            self.assertEqual(len(repository.list_conversation_turns(
                first.conversation_id,
            )), 3)
            self.assertEqual(len(repository.list_definition_outcomes(
                first.conversation_id,
            )), 3)
            turns = repository.list_conversation_turns(first.conversation_id)
            outcomes = repository.list_definition_outcomes(first.conversation_id)
            outcome_by_turn = {
                outcome.turn_id: outcome.outcome_id for outcome in outcomes
            }
            self.assertEqual(
                [(turn.role, turn.safe_text, turn.outcome_id) for turn in turns],
                [
                    ("user", "帮我订阅 AI 行业动态",
                     outcome_by_turn[turns[0].turn_id]),
                    ("user", "技术进展",
                     outcome_by_turn[turns[1].turn_id]),
                    ("user", "只关注重大变化",
                     outcome_by_turn[turns[2].turn_id]),
                ],
            )

    def test_next_question_then_reject(self):
        with tempfile.TemporaryDirectory() as root:
            app, _repository, _provider, _workflow = self.make(
                root, [next_question(), reject("当前只支持资讯订阅。")],
            )
            first = app.start_subscription_conversation(
                USER, "帮我持续执行任意命令", "start",
            )
            terminal = app.continue_subscription_conversation(
                USER, first.conversation_id, "是的", "answer",
            )
            self.assertEqual(terminal.status, "REJECTED")
            self.assertEqual(terminal.rejection_reason, "当前只支持资讯订阅。")
            self.assertIsNone(terminal.definition)

    def test_immediate_done_and_immediate_reject(self):
        with tempfile.TemporaryDirectory() as root:
            app, repository, _provider, _workflow = self.make(root, [done()])
            accepted = app.start_subscription_conversation(
                USER, "订阅 AI 行业动态，600 字以内", "done",
            )
            self.assertEqual(accepted.status, "DEFINITION_ACCEPTED")
            self.assertEqual(
                accepted.definition["provenance"]["topic"],
                "SYSTEM_INFERRED",
            )
            self.assertEqual(repository.list_subscriptions(), ())
            app2, repository2, _provider2, _workflow2 = self.make(
                root, [reject()], database=os.path.join(root, "second.db"),
                ids=IdFactory(100),
            )
            rejected = app2.start_subscription_conversation(
                USER, "不要执行该请求", "reject",
            )
            self.assertEqual(rejected.status, "REJECTED")
            self.assertEqual(repository2.list_subscriptions(), ())

    def test_invalid_done_candidate_is_incomplete_not_accepted(self):
        with tempfile.TemporaryDirectory() as root:
            app, repository, provider, _workflow = self.make(
                root, [done(max_chars=99)],
            )
            result = app.start_subscription_conversation(
                USER, "订阅 AI", "invalid",
            )
            self.assertEqual(
                (result.status, result.failure_reason, result.latest_outcome),
                ("INCOMPLETE", "invalid_candidate", None),
            )
            turns = repository.list_conversation_turns(result.conversation_id)
            self.assertEqual((turns[0].status, turns[0].error_code),
                             ("failed", "invalid_candidate"))
            self.assertEqual(repository.list_definition_outcomes(
                result.conversation_id,
            ), ())
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(
                (turns[0].failure_stage, turns[0].failure_subtype),
                ("definition_validation", "INVALID_MAX_CHARS"),
            )
            self.assertEqual(
                len(repository.list_definition_attempts(turns[0].turn_id)), 1,
            )

    def test_v2_agent_cannot_ask_internal_defaults_or_forge_user_preference(self):
        internal_question = {
            "protocol_version": 2, "type": "NEXT_QUESTION",
            "question": "最多几条资讯、最多多少字？",
        }
        forged = FakeDefinitionAgentAdapter._intent_done(
            topic="OpenAI 新模型发布",
            preferences={"max_chars": (600, 1)},
        )
        for name, candidate in (
            ("internal-question", internal_question),
            ("forged-preference", forged),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                app, repository, _provider, _workflow = self.make(
                    root, [candidate],
                )
                result = app.start_subscription_conversation(
                    USER, "关注 OpenAI 新模型发布", name,
                )
                self.assertEqual(
                    (result.status, result.latest_outcome),
                    ("INCOMPLETE", None),
                )
                self.assertEqual(repository.list_definition_outcomes(
                    result.conversation_id,
                ), ())
                self.assertEqual(repository.list_subscriptions(), ())

    def test_structured_failure_retries_same_logical_turn_with_safe_ledger(self):
        class RetryProvider(FakeDefinitionAgentAdapter):
            provider_identity = "vertex"

            def __init__(self):
                super().__init__([done()])
                self.entries = 0
                self.last_attempt = None

            def describe_attempt(self, _context):
                return {
                    "provider_identity": "vertex", "model_identity": "m",
                    "api_mode": "chat-completions",
                    "request_sha256": "a" * 64,
                    "schema_identity": "b" * 64,
                    "structured_output_mechanism": "strict_tool",
                }

            def propose(self, context):
                self.entries += 1
                if self.entries == 1:
                    self.last_attempt = {
                        "http_status": 200, "json_parse_succeeded": False,
                        "failure_subtype": "JSON_PARSE",
                        "json_lexical_subtype": "EXTRA_DATA",
                    }
                    raise ProviderAdapterError(
                        "INVALID_RESPONSE", subtype="JSON_PARSE",
                    )
                self.last_attempt = {
                    "http_status": 200, "json_parse_succeeded": True,
                    "schema_validation_succeeded": True,
                }
                return super().propose(context)

        with tempfile.TemporaryDirectory() as root:
            provider = RetryProvider()
            app, repository, _unused, _workflow = self.make(
                root, [], provider=provider,
            )
            result = app.start_subscription_conversation(
                USER, "订阅 AI，600 字以内", "retry",
            )
            turn = repository.list_conversation_turns(result.conversation_id)[0]
            attempts = repository.list_definition_attempts(turn.turn_id)
            self.assertEqual(result.status, "DEFINITION_ACCEPTED")
            self.assertEqual(provider.entries, 2)
            self.assertEqual(
                [(item.attempt_number, item.status) for item in attempts],
                [(1, "failed"), (2, "succeeded")],
            )
            self.assertEqual(attempts[0].failure_stage, "definition_generation")
            rendered = repr(attempts)
            self.assertNotIn("订阅 AI", rendered)
            self.assertNotIn("raw", rendered.casefold())

    def test_protocol_variant_failure_is_distinct_and_not_retried(self):
        malformed = {
            "protocol_version": 1, "type": "NEXT_QUESTION",
            "question": "问题", "extra": "forbidden",
        }
        with tempfile.TemporaryDirectory() as root:
            app, repository, provider, _workflow = self.make(
                root, [malformed],
            )
            result = app.start_subscription_conversation(
                USER, "订阅 AI", "protocol-invalid",
            )
            turn = repository.list_conversation_turns(result.conversation_id)[0]
            attempts = repository.list_definition_attempts(turn.turn_id)
            self.assertEqual(result.status, "INCOMPLETE")
            self.assertEqual(
                (turn.failure_stage, turn.failure_subtype),
                ("protocol_validation", "PROTOCOL_VARIANT"),
            )
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(
                (len(attempts), attempts[0].status,
                 attempts[0].failure_stage),
                (1, "failed", "protocol_validation"),
            )

    def test_restart_reuses_durable_successful_definition_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            database = os.path.join(root, "digest.db")
            ids = IdFactory()
            provider = FakeDefinitionAgentAdapter([done()])

            def crash(stage, _turn):
                if stage == "after_definition_attempt":
                    raise RuntimeError("after durable definition attempt")

            app, repository, _unused, _workflow = self.make(
                root, [], ids=ids, database=database, provider=provider,
                fault_injector=crash, owner="1" * 32,
            )
            with self.assertRaisesRegex(RuntimeError, "durable definition"):
                app.start_subscription_conversation(
                    USER, "订阅 AI，600 字以内", "attempt-crash",
                )
            with repository.connect() as connection:
                conversation_id = connection.execute(
                    "SELECT conversation_id FROM conversations",
                ).fetchone()[0]
            turn = repository.list_conversation_turns(conversation_id)[0]
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(
                repository.list_definition_attempts(turn.turn_id)[0].status,
                "succeeded",
            )
            resumed, resumed_repo, _unused, _workflow = self.make(
                root, [], ids=ids, database=database, provider=provider,
                owner="2" * 32,
            )
            result = resumed.start_subscription_conversation(
                USER, "订阅 AI，600 字以内", "attempt-crash",
            )
            self.assertEqual(result.status, "DEFINITION_ACCEPTED")
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(len(resumed_repo.list_definition_outcomes(
                result.conversation_id,
            )), 1)

    def test_turn_ceiling_is_governance_incomplete_not_fake_done(self):
        with tempfile.TemporaryDirectory() as root:
            app, repository, provider, _workflow = self.make(
                root, [next_question("问题一"), next_question("问题二")],
                maximum_turns=2,
            )
            first = app.start_subscription_conversation(
                USER, "订阅 AI", "start",
            )
            terminal = app.continue_subscription_conversation(
                USER, first.conversation_id, "回答一", "answer",
            )
            self.assertEqual(
                (terminal.status, terminal.failure_reason, terminal.question),
                ("INCOMPLETE", "turn_limit_reached", None),
            )
            self.assertEqual(terminal.latest_outcome, "NEXT_QUESTION")
            self.assertEqual(len(provider.calls), 2)
            self.assertEqual(repository.list_subscriptions(), ())

    def test_duplicate_start_and_message_are_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            app, _repository, provider, _workflow = self.make(
                root, [next_question(), done()],
            )
            first = app.start_subscription_conversation(
                USER, "帮我订阅 AI", "same-start",
            )
            duplicate = app.start_subscription_conversation(
                USER, "帮我订阅 AI", "same-start",
            )
            self.assertEqual(first.conversation_id, duplicate.conversation_id)
            self.assertTrue(duplicate.reused)
            terminal = app.continue_subscription_conversation(
                USER, first.conversation_id, "600 字以内", "same-answer",
            )
            duplicate_answer = app.continue_subscription_conversation(
                USER, first.conversation_id, "600 字以内", "same-answer",
            )
            self.assertEqual(terminal.definition, duplicate_answer.definition)
            self.assertEqual(len(provider.calls), 2)
            with self.assertRaisesRegex(ApplicationError, "idempotency_conflict"):
                app.start_subscription_conversation(
                    USER, "不同内容", "same-start",
                )

    def test_done_proposal_can_be_adjusted_without_creating_subscription(self):
        with tempfile.TemporaryDirectory() as root:
            app, repository, provider, _workflow = self.make(root, [
                done(), done(max_chars=800, max_items=3,
                             focus_topics=["Agent", "开发工具"]),
            ])
            proposed = app.start_subscription_conversation(
                USER, "帮我订阅 AI", "start",
            )
            self.assertEqual(proposed.status, "DEFINITION_ACCEPTED")
            self.assertEqual(repository.list_subscriptions(), ())

            adjusted = app.adjust_subscription_conversation(
                USER, proposed.conversation_id,
                "改成 800 字、3 条，增加开发工具", "adjust",
            )
            self.assertEqual(
                (adjusted.status, adjusted.turn_count,
                 adjusted.definition["max_chars"],
                 adjusted.definition["max_items"],
                 adjusted.definition["focus_topics"]),
                ("DEFINITION_ACCEPTED", 2, 800, 3,
                 ["Agent", "开发工具"]),
            )
            replay = app.adjust_subscription_conversation(
                USER, proposed.conversation_id,
                "改成 800 字、3 条，增加开发工具", "adjust",
            )
            self.assertTrue(replay.reused)
            self.assertEqual(replay.definition, adjusted.definition)
            self.assertEqual(len(provider.calls), 2)
            self.assertEqual(repository.list_subscriptions(), ())
            self.assertEqual(len(repository.list_definition_outcomes(
                proposed.conversation_id,
            )), 2)

    def test_done_proposal_survives_application_restart(self):
        with tempfile.TemporaryDirectory() as root:
            database = os.path.join(root, "digest.db")
            ids = IdFactory()
            first_app, repository, _provider, _workflow = self.make(
                root, [done()], ids=ids, database=database,
                owner="1" * 32,
            )
            proposed = first_app.start_subscription_conversation(
                USER, "帮我订阅 AI", "start",
            )
            second_app, restarted_repository, _provider2, _workflow2 = (
                self.make(
                    root, [], ids=ids, database=database,
                    owner="2" * 32,
                )
            )
            restored = second_app.get_subscription_conversation(
                USER, proposed.conversation_id,
            )
            self.assertEqual(
                (restored.status, restored.definition,
                 restored.latest_outcome),
                ("DEFINITION_ACCEPTED", proposed.definition, "DONE"),
            )
            self.assertEqual(repository.list_subscriptions(), ())
            self.assertEqual(restarted_repository.list_subscriptions(), ())

    def test_restart_after_next_question_can_continue(self):
        with tempfile.TemporaryDirectory() as root:
            database = os.path.join(root, "digest.db")
            ids = IdFactory()
            first_app, _repo, _provider, _workflow = self.make(
                root, [next_question("每篇多少字？")], ids=ids,
                database=database, owner="1" * 32,
            )
            waiting = first_app.start_subscription_conversation(
                USER, "帮我订阅 AI", "start",
            )
            second_app, repository, _provider2, _workflow2 = self.make(
                root, [done()], ids=ids, database=database, owner="2" * 32,
            )
            restored = second_app.get_subscription_conversation(
                USER, waiting.conversation_id,
            )
            self.assertEqual(restored.question, "每篇多少字？")
            terminal = second_app.continue_subscription_conversation(
                USER, waiting.conversation_id, "600 字以内", "answer",
            )
            self.assertEqual(terminal.status, "DEFINITION_ACCEPTED")
            self.assertEqual(repository.list_subscriptions(), ())

    def test_crash_after_durable_user_turn_resumes_without_duplicate_turn(self):
        with tempfile.TemporaryDirectory() as root:
            database = os.path.join(root, "digest.db")
            ids = IdFactory()
            provider = FakeDefinitionAgentAdapter([done()])

            def crash(stage, _turn):
                if stage == "after_turn_claimed":
                    raise RuntimeError("synthetic crash before provider")

            app, repository, _provider, _workflow = self.make(
                root, [], ids=ids, database=database, provider=provider,
                fault_injector=crash, owner="1" * 32,
            )
            with self.assertRaisesRegex(RuntimeError, "before provider"):
                app.start_subscription_conversation(
                    USER, "订阅 AI，600 字以内", "crash",
                )
            with repository.connect() as connection:
                row = connection.execute(
                    "SELECT conversation_id, status FROM conversation_turns",
                ).fetchone()
            self.assertEqual((row[1], len(provider.calls)), ("running", 0))
            resumed_app, resumed_repo, _provider2, _workflow2 = self.make(
                root, [], ids=ids, database=database, provider=provider,
                owner="2" * 32,
            )
            terminal = resumed_app.start_subscription_conversation(
                USER, "订阅 AI，600 字以内", "crash",
            )
            self.assertEqual(terminal.status, "DEFINITION_ACCEPTED")
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(len(resumed_repo.list_conversation_turns(
                row[0],
            )), 1)

    def test_secret_shaped_user_input_is_rejected_before_persistence(self):
        with tempfile.TemporaryDirectory() as root:
            app, repository, provider, _workflow = self.make(root, [done()])
            with self.assertRaisesRegex(
                    ApplicationError, "invalid_conversation_message"):
                app.start_subscription_conversation(
                    USER, "api_key=sk-not-a-real-secret", "unsafe",
                )
            with repository.connect() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM conversations",
                ).fetchone()[0]
            self.assertEqual((count, len(provider.calls)), (0, 0))

    def test_secret_shaped_candidate_is_not_persisted(self):
        with tempfile.TemporaryDirectory() as root:
            app, repository, provider, _workflow = self.make(
                root, [next_question("api_key=sk-not-a-real-secret")],
            )
            result = app.start_subscription_conversation(
                USER, "帮我订阅 AI", "candidate-secret",
            )
            self.assertEqual(
                (result.status, result.failure_reason, result.latest_outcome),
                ("INCOMPLETE", "definition_incomplete", None),
            )
            self.assertEqual(repository.list_definition_outcomes(
                result.conversation_id,
            ), ())
            self.assertEqual(len(provider.calls), 1)

    def test_crash_after_harness_result_replays_without_provider_call(self):
        with tempfile.TemporaryDirectory() as root:
            database = os.path.join(root, "digest.db")
            ids = IdFactory()
            provider = FakeDefinitionAgentAdapter([done()])

            def crash(stage, _turn):
                if stage == "after_harness_result":
                    raise RuntimeError("synthetic app projection crash")

            app, repository, _provider, _workflow = self.make(
                root, [], ids=ids, database=database, provider=provider,
                fault_injector=crash, owner="1" * 32,
            )
            with self.assertRaisesRegex(RuntimeError, "projection crash"):
                app.start_subscription_conversation(
                    USER, "订阅 AI，600 字以内", "crash-result",
                )
            with repository.connect() as connection:
                conversation_id = connection.execute(
                    "SELECT conversation_id FROM conversations",
                ).fetchone()[0]
            self.assertEqual(len(provider.calls), 1)
            resumed_app, resumed_repo, _provider2, _workflow2 = self.make(
                root, [], ids=ids, database=database, provider=provider,
                owner="2" * 32,
            )
            terminal = resumed_app.start_subscription_conversation(
                USER, "订阅 AI，600 字以内", "crash-result",
            )
            self.assertEqual(terminal.status, "DEFINITION_ACCEPTED")
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(len(resumed_repo.list_definition_outcomes(
                conversation_id,
            )), 1)

    def test_public_view_is_sealed_and_done_alone_creates_no_product_truth(self):
        with tempfile.TemporaryDirectory() as root:
            app, repository, _provider, _workflow = self.make(root, [done()])
            view = app.start_subscription_conversation(
                USER, "订阅 AI，600 字以内", "sealed",
            )
            rendered = repr(asdict(view)).casefold()
            for forbidden in (
                "harness_run_id", "evidence", "artifact", "checkpoint",
                "provider", "raw_response",
            ):
                self.assertNotIn(forbidden, rendered)
            self.assertEqual(repository.list_subscriptions(), ())
            self.assertEqual(repository.list_digests(USER), ())
            with repository.connect() as connection:
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'",
                )}
                run_count = connection.execute(
                    "SELECT COUNT(*) FROM digest_runs",
                ).fetchone()[0]
            self.assertEqual(run_count, 0)
            self.assertTrue({
                "conversations", "conversation_turns", "definition_outcomes",
            }.issubset(tables))
            self.assertTrue({
                "subscription_definitions", "subscription_aggregates",
                "user_subscriptions", "briefing_reservations",
                "application_outbox", "subscription_activations",
            }.issubset(tables))
            with repository.connect() as connection:
                product_counts = [connection.execute(
                    f"SELECT COUNT(*) FROM {name}",
                ).fetchone()[0] for name in (
                    "subscription_definitions", "subscription_aggregates",
                    "user_subscriptions", "briefing_reservations",
                    "application_outbox", "subscription_activations",
                )]
            self.assertEqual(product_counts, [0] * 6)

    def test_concurrent_duplicate_logical_turn_calls_provider_once(self):
        class BlockingAdapter(FakeDefinitionAgentAdapter):
            def __init__(self):
                super().__init__([done()])
                self.entered = threading.Event()
                self.release = threading.Event()

            def propose(self, context):
                self.entered.set()
                if not self.release.wait(5):
                    raise AssertionError("provider was not released")
                return super().propose(context)

        with tempfile.TemporaryDirectory() as root:
            provider = BlockingAdapter()
            app, _repository, _provider, _workflow = self.make(
                root, [], provider=provider,
            )
            values = []

            def invoke():
                values.append(app.start_subscription_conversation(
                    USER, "订阅 AI，600 字以内", "concurrent",
                ))

            first = threading.Thread(target=invoke)
            first.start()
            self.assertTrue(provider.entered.wait(5))
            second = app.start_subscription_conversation(
                USER, "订阅 AI，600 字以内", "concurrent",
            )
            self.assertTrue(second.processing)
            provider.release.set()
            first.join(5)
            self.assertFalse(first.is_alive())
            replay = app.start_subscription_conversation(
                USER, "订阅 AI，600 字以内", "concurrent",
            )
            self.assertEqual(replay.status, "DEFINITION_ACCEPTED")
            self.assertEqual(len(provider.calls), 1)


if __name__ == "__main__":
    unittest.main()
