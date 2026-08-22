#!/usr/bin/env python3
"""Compatibility entrypoint; implementation lives in :mod:`cli.bridge_reconciler`."""

from cli.bridge_reconciler import main


if __name__ == "__main__":
    raise SystemExit(main())
