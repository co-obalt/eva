#!/usr/bin/env python
"""EVA v2 launcher.

Interactive:            python eva_cli.py
Headless one-shot:      python eva_cli.py --run "open chrome" --json
"""
from eva.ui.edex import main

if __name__ == "__main__":
    raise SystemExit(main())
