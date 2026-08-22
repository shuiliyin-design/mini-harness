#!/usr/bin/env python3
"""Command-line Bridge Protocol v1 Result repairer."""

import argparse
import json

from mini_harness_core.bridge.result_repairer import repair_bridge_result


def _parser():
    parser = argparse.ArgumentParser(description="Repair an applied Bridge Result")
    parser.add_argument("--root", required=True, help="Bridge root directory")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--claim-nonce", required=True)
    parser.add_argument("--consumer", required=True)
    parser.add_argument("--result-json", required=True)
    parser.add_argument("--artifact-refs-json", default="[]")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        outcome = repair_bridge_result(
            args.root, args.task_id, args.claim_nonce, args.consumer,
            json.loads(args.result_json), json.loads(args.artifact_refs_json),
        )
    except (json.JSONDecodeError, OSError, ValueError) as error:
        print("repair status: REJECTED")
        print("error: " + str(error))
        return 2
    print("task_id: " + outcome.task_id)
    print("claim_nonce: " + outcome.claim_nonce)
    print("repair status: " + outcome.status)
    print("result path: " + outcome.result_path)
    print("ready path: " + outcome.ready_path)
    print("protocol state: " + outcome.protocol_state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
