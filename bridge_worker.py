#!/usr/bin/env python3
"""Compatibility entrypoint; implementation lives in :mod:`cli.bridge_worker`."""

from cli.bridge_worker import main


if __name__ == "__main__":
    raise SystemExit(main())
