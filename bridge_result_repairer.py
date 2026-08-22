#!/usr/bin/env python3
"""Compatibility entrypoint for deterministic Bridge Result repair."""

from cli.bridge_result_repairer import main


if __name__ == "__main__":
    raise SystemExit(main())
