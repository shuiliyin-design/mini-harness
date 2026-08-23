"""Thin argparse transport over DigestApplication and bootstrap seams."""

import argparse
from contextlib import redirect_stdout
from dataclasses import asdict, is_dataclass
import io
import json
import os

from .application import ApplicationError
from .bootstrap import (
    BootstrapError, DigestAppConfig, bootstrap_application, check_readiness,
)


def _parser():
    parser = argparse.ArgumentParser(description="AI Digest local application")
    parser.add_argument("--database", default=".digest/digest.db")
    parser.add_argument("--workspace", default=".digest/workspace")
    parser.add_argument("--audit", default=".digest/audit")
    parser.add_argument("--search-provider", choices=("fake", "brave"), default="fake")
    parser.add_argument("--llm-provider", choices=("fake", "vertex"), default="fake")
    parser.add_argument("--delivery-provider", choices=("fake", "termux"), default="fake")
    parser.add_argument("--user-id", default="a" * 32)
    parser.add_argument("--json", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("subscription-create")
    create.add_argument("--request", required=True)
    commands.add_parser("subscription-list")
    get_sub = commands.add_parser("subscription-get")
    get_sub.add_argument("--subscription-id", required=True)
    update = commands.add_parser("subscription-update")
    update.add_argument("--subscription-id", required=True)
    update.add_argument("--expected-version", type=int, required=True)
    update.add_argument("--topic")
    update.add_argument("--request")
    update.add_argument("--cadence")
    update.add_argument("--language")
    update.add_argument("--max-chars", type=int)
    update.add_argument("--max-items", type=int)
    update.add_argument("--focus-topic", action="append")
    update.add_argument("--delivery-preference")
    for name in ("subscription-enable", "subscription-disable"):
        command = commands.add_parser(name)
        command.add_argument("--subscription-id", required=True)
        command.add_argument("--expected-version", type=int, required=True)

    run = commands.add_parser("run")
    run.add_argument("--subscription-id", required=True)
    run.add_argument("--idempotency-key", required=True)
    run.add_argument("--period-key")
    for name in ("run-status", "run-recover"):
        command = commands.add_parser(name)
        command.add_argument("--application-run-id", required=True)
    recovery_inspect = commands.add_parser("run-recovery-inspect")
    recovery_inspect.add_argument("--application-run-id", required=True)
    recovery_execute = commands.add_parser("run-recovery-execute")
    recovery_execute.add_argument("--application-run-id", required=True)
    recovery_execute.add_argument("--action", required=True)

    digest_list = commands.add_parser("digest-list")
    digest_list.add_argument("--subscription-id")
    digest_get = commands.add_parser("digest-get")
    digest_get.add_argument("--digest-id", required=True)
    deliver = commands.add_parser("deliver")
    deliver.add_argument("--digest-id", required=True)
    deliver.add_argument("--channel", choices=("fake", "termux_notification"), required=True)
    feedback = commands.add_parser("feedback")
    feedback.add_argument("--digest-id", required=True)
    feedback.add_argument("--type", choices=("opened", "liked", "dismissed", "saved"), required=True)
    feedback.add_argument("--event-key", required=True)
    feedback.add_argument("--item-id")
    commands.add_parser("profile")
    commands.add_parser("readiness")
    return parser


def _config(args):
    return DigestAppConfig(
        args.database, args.workspace, args.audit,
        args.search_provider, args.llm_provider, args.delivery_provider,
        args.user_id,
    )


def _plain(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _print(value, json_mode):
    projected = _plain(value)
    if json_mode:
        print(json.dumps(projected, ensure_ascii=False, sort_keys=True))
    elif isinstance(projected, dict):
        for key, item in projected.items():
            rendered = (json.dumps(item, ensure_ascii=False, sort_keys=True)
                        if isinstance(item, (dict, list, tuple)) else str(item))
            print(f"{key}: {rendered}")
    elif isinstance(projected, list):
        if not projected:
            print("items: []")
        for item in projected:
            print(json.dumps(item, ensure_ascii=False, sort_keys=True))
    else:
        print(str(projected))


def _dispatch(app, args):
    user = args.user_id
    if args.command == "subscription-create":
        return app.create_subscription(user, args.request)
    if args.command == "subscription-list":
        return app.list_subscriptions(user)
    if args.command == "subscription-get":
        return app.get_subscription(user, args.subscription_id)
    if args.command == "subscription-update":
        mapping = {
            "topic": args.topic, "natural_language_request": args.request,
            "cadence": args.cadence, "language": args.language,
            "max_chars": args.max_chars, "max_items": args.max_items,
            "focus_topics": args.focus_topic,
            "delivery_preference": args.delivery_preference,
        }
        return app.update_subscription(
            user, args.subscription_id, args.expected_version,
            **{key: value for key, value in mapping.items() if value is not None},
        )
    if args.command in {"subscription-enable", "subscription-disable"}:
        method = (app.enable_subscription if args.command.endswith("enable")
                  else app.disable_subscription)
        return method(user, args.subscription_id, args.expected_version)
    if args.command == "run":
        return app.run_subscription(
            user, args.subscription_id, args.idempotency_key, args.period_key,
        )
    if args.command == "run-status":
        return app.get_run(user, args.application_run_id)
    if args.command == "run-recover":
        return app.recover_run(user, args.application_run_id)
    if args.command == "run-recovery-inspect":
        return app.inspect_run_recovery(args.application_run_id)
    if args.command == "run-recovery-execute":
        return app.execute_run_recovery(
            args.application_run_id, args.action,
        )
    if args.command == "digest-list":
        return app.list_digests(user, args.subscription_id)
    if args.command == "digest-get":
        return app.get_digest(user, args.digest_id)
    if args.command == "deliver":
        return app.deliver_digest(user, args.digest_id, args.channel)
    if args.command == "feedback":
        return app.record_feedback(
            user, args.digest_id, args.type, args.event_key, args.item_id,
        )
    if args.command == "profile":
        return app.get_profile(user)
    raise ApplicationError("invalid_command")


def main(argv=None, *, environ=None, termux_dispatcher=None):
    args = _parser().parse_args(argv)
    environ = os.environ if environ is None else environ
    try:
        config = _config(args)
        if args.command == "readiness":
            report = check_readiness(config, environ, termux_dispatcher)
            _print(report, args.json)
            return 0 if report.status == "READY" else 2
        app = bootstrap_application(config, environ, termux_dispatcher)
        with redirect_stdout(io.StringIO()):
            result = _dispatch(app, args)
        _print(result, args.json)
        failure = getattr(result, "failure_reason", None)
        return 1 if failure is not None else 0
    except ApplicationError as error:
        print(f"ERROR code={error.code}")
        return 1
    except (BootstrapError, OSError, ValueError):
        print("ERROR code=startup_not_ready")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
