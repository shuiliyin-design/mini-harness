#!/usr/bin/env python3
"""Command-line Bridge Protocol v1 reconciler."""

import argparse

from mini_harness_core.bridge.reconciler import RECONCILED, reconcile_bridge_claim


def _parser():
    parser = argparse.ArgumentParser(description="Reconcile a Bridge v1 claim")
    parser.add_argument("--root", required=True, help="Bridge root directory")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--claim-nonce", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--checked-by", required=True)
    parser.add_argument("--method", required=True)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        outcome = reconcile_bridge_claim(
            args.root, args.task_id, args.claim_nonce, args.result,
            args.checked_by, args.method,
        )
    except (OSError, ValueError) as error:
        print("status: REJECTED")
        print("error: " + str(error))
        return 2
    print("task_id: " + outcome.task_id)
    print("claim_nonce: " + outcome.claim_nonce)
    print("result: " + outcome.result)
    print("status: " + outcome.status)
    return 0 if outcome.status == RECONCILED else 3


if __name__ == "__main__":
    raise SystemExit(main())
