#!/usr/bin/env python3
"""Compatibility entrypoint; implementation lives in :mod:`cli.bridge_executor`."""

from cli.bridge_executor import main


if __name__ == "__main__":
    raise SystemExit(main())
