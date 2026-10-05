#!/usr/bin/env python
"""Alias entry point: python eva.py [--run "<goal>"] [--json] [--yes]."""
from eva.ui.edex import main

if __name__ == "__main__":
    raise SystemExit(main())
