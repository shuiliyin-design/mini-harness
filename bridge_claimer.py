#!/usr/bin/env python3
"""Compatibility entrypoint; implementation lives in :mod:`cli.bridge_claimer`."""

from cli.bridge_claimer import main


if __name__ == "__main__":
    raise SystemExit(main())
