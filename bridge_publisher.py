#!/usr/bin/env python3
"""Compatibility entrypoint; implementation lives in :mod:`cli.bridge_publisher`."""

from cli.bridge_publisher import main


if __name__ == "__main__":
    raise SystemExit(main())
