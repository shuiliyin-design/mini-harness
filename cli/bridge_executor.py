#!/usr/bin/env python3
"""Command-line Bridge Protocol v1 executor."""

import argparse

from mini_harness_core.bridge.executor import EXECUTED, execute_bridge_task


def _parser():
    parser = argparse.ArgumentParser(description="Execute an owned Bridge v1 attempt")
    parser.add_argument("--root", required=True, help="Bridge root directory")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--consumer", required=True)
    parser.add_argument("--claim-nonce", required=True)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        result = execute_bridge_task(
            args.root, args.task_id, args.consumer, args.claim_nonce,
        )
    except (OSError, ValueError) as error:
        print("execution status: REJECTED")
        print("error: " + str(error))
        return 2
    print("task_id: " + result.task_id)
    print("claim_nonce: " + result.claim_nonce)
    print("task_type: " + (result.task_type or "-"))
    print("execution status: " + result.status)
    print("result path: " + result.result_path)
    print("protocol state: " + result.protocol_state)
    return 0 if result.status == EXECUTED else 3


if __name__ == "__main__":
    raise SystemExit(main())
