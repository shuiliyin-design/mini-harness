#!/usr/bin/env python3
"""Run one Bridge Harness Adapter step with the offline FakeProvider."""

import argparse
import os

from mini_harness_core.agent import run_agent
from mini_harness_core.bridge_harness_worker import run_bridge_harness_worker_once
from mini_harness_core.providers import FakeProvider


def _parser():
    parser = argparse.ArgumentParser(description="Run one Bridge-to-Harness step")
    parser.add_argument("--root", required=True, help="Bridge root directory")
    parser.add_argument("--consumer", required=True)
    parser.add_argument("--audit-directory", default=os.path.join(os.getcwd(), ".audit"))
    parser.add_argument("--session-directory")
    parser.add_argument("--max-steps", type=int, default=5)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        result = run_bridge_harness_worker_once(
            args.root, args.consumer, FakeProvider(),
            audit_directory=args.audit_directory,
            harness_runner=run_agent,
            session_directory=args.session_directory,
            max_steps=args.max_steps,
        )
    except (OSError, ValueError) as error:
        print("action: REJECTED")
        print("reason: " + str(error))
        return 2
    print("task_id: " + (result.task_id or "-"))
    print("initial_state: " + result.initial_state)
    print("action: " + result.action)
    print("final_state: " + result.final_state)
    print("claim_nonce: " + (result.claim_nonce or "-"))
    print("harness_run_id: " + (result.harness_run_id or "-"))
    print("harness_result_status: " + (result.harness_result_status or "-"))
    print("reason: " + (result.reason or "-"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
