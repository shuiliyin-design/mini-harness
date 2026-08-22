#!/usr/bin/env python3
"""Command-line deterministic Bridge v1 worker."""

import argparse

from mini_harness_core.bridge.worker import run_bridge_worker_once


def _parser():
    parser = argparse.ArgumentParser(description="Run one Bridge worker step")
    parser.add_argument("--root", required=True, help="Bridge root directory")
    parser.add_argument("--consumer", required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        result = run_bridge_worker_once(args.root, args.consumer, args.dry_run)
    except (OSError, ValueError) as error:
        print("action: REJECTED")
        print("reason: " + str(error))
        return 2
    if args.dry_run:
        print("next candidate: " + (result.task_id or "-"))
        print("state: " + result.initial_state)
        print("would_action: " + result.action)
    else:
        print("consumer_id: " + result.consumer_id)
        print("task_id: " + (result.task_id or "-"))
        print("initial_state: " + result.initial_state)
        print("action: " + result.action)
        print("final_state: " + result.final_state)
        print("claim_nonce: " + (result.claim_nonce or "-"))
        print("reason: " + (result.reason or "-"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
