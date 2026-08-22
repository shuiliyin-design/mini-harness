#!/usr/bin/env python3
"""Compatibility entrypoint for the offline self-check utility."""

from tools.self_check import print_self_check


if __name__ == "__main__":
    raise SystemExit(print_self_check())
