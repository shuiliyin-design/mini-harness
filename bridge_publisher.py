#!/usr/bin/env python3
"""Command-line Task v1 publisher for the filesystem Bridge."""

import argparse
import json

from mini_harness_core.bridge_publisher import publish_bridge_task


def _parser():
    parser = argparse.ArgumentParser(description="Publish a Bridge Protocol v1 task")
    parser.add_argument("--root", required=True, help="Bridge root directory")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--task-type", required=True)
    parser.add_argument("--payload-json", required=True)
    parser.add_argument("--publisher", help="Publisher identity for audit")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        payload = json.loads(args.payload_json)
        result = publish_bridge_task(
            args.root, args.task_id, args.task_type, payload, args.publisher,
        )
    except (json.JSONDecodeError, OSError, ValueError) as error:
        print("publish status: REJECTED")
        print("error: " + str(error))
        return 2
    print("task_id: " + result.task_id)
    print("task path: " + result.task_path)
    print("ready path: " + result.ready_path)
    print("publish status: " + result.status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
