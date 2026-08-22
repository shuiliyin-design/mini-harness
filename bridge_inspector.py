#!/usr/bin/env python3
"""Compatibility entrypoint; implementation lives in :mod:`cli.bridge_inspector`."""

from cli.bridge_inspector import main


if __name__ == "__main__":
    raise SystemExit(main())
