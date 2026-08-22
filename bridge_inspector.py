#!/usr/bin/env python3
"""Thin command-line interface for the read-only Bridge v1 inspector."""

import argparse
import json

from mini_harness_core.bridge_inspector import INVALID_HISTORY, inspect_bridge_task


def _parser():
    parser = argparse.ArgumentParser(description="Inspect Bridge Protocol v1 task state")
    parser.add_argument("task_id")
    parser.add_argument("--root", required=True, help="Bridge root directory")
    parser.add_argument("--consumer", help="Observer consumer identity")
    parser.add_argument("--claim", help="Observer claim nonce")
    parser.add_argument("--json", action="store_true", help="Print compact JSON")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    result = inspect_bridge_task(args.root, args.task_id, args.consumer, args.claim)
    value = result.to_dict()
    if args.json:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    else:
        print("task_id: " + result.task_id)
        print("state: " + result.state)
        print("latest_claim: " + (result.latest_claim_nonce or "-"))
        print("attempt: " + (str(result.latest_attempt) if result.latest_attempt else "-"))
        print("consumer: " + (result.consumer_id or "-"))
        print("reconciliation: " + (result.reconciliation or "-"))
        print("result: " + (result.result_status or "-"))
        print("validation errors:")
        if result.validation_errors:
            for error in result.validation_errors:
                print("- " + error)
        else:
            print("- none")
    return 3 if result.state == INVALID_HISTORY else 0


if __name__ == "__main__":
    raise SystemExit(main())
