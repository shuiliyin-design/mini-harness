#!/usr/bin/env python3
"""Compatibility entrypoint for the fixed Termux capability CLI."""

from cli.termux_capability import main


if __name__ == "__main__":
    raise SystemExit(main())
