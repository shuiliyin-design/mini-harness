#!/usr/bin/env python3
"""Command-line Bridge Protocol v1 claimer."""

import argparse

from mini_harness_core.bridge.claimer import CLAIMED, claim_bridge_task


def _parser():
    parser = argparse.ArgumentParser(description="Create a Bridge v1 claim attempt")
    parser.add_argument("--root", required=True, help="Bridge root directory")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--consumer", required=True)
    parser.add_argument("--claim-nonce", required=True)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        result = claim_bridge_task(
            args.root, args.task_id, args.consumer, args.claim_nonce,
        )
    except (OSError, ValueError) as error:
        print("status: REJECTED")
        print("error: " + str(error))
        return 2
    print("task_id: " + result.task_id)
    print("claim_nonce: " + result.claim_nonce)
    print("attempt_number: " + (
        str(result.attempt_number) if result.attempt_number is not None else "-"
    ))
    print("previous_claim_nonce: " + (result.previous_claim_nonce or "-"))
    print("status: " + result.status)
    return 0 if result.status == CLAIMED else 3


if __name__ == "__main__":
    raise SystemExit(main())
