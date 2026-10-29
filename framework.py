#!/usr/bin/env python3
"""Compatibility entry point. Prefer: python -m mcperf.cfn <create|scale|delete|status>."""

from mcperf.cfn.cli import main

if __name__ == "__main__":
    main()
