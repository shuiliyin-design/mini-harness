#!/usr/bin/env python3
"""Compatibility entrypoint for the Bridge-to-Harness worker."""

from cli.bridge_harness_worker import main


if __name__ == "__main__":
    raise SystemExit(main())
