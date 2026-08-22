#!/usr/bin/env python3
"""Thin CLI for the fixed Termux environment capability registry."""

import argparse
import json

from mini_harness_core.termux_capabilities import invoke_termux_capability


def main(argv=None):
    parser = argparse.ArgumentParser(description="Invoke a fixed Termux capability")
    parser.add_argument("capability", choices=("battery_status",))
    args = parser.parse_args(argv)
    result = invoke_termux_capability(args.capability)
    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
