"""Application launch registry (doc 12: \"what not how\" — deterministic fallback)."""
from .registry import AppInfo, KNOWN_APPS, find_app, launch_app, list_apps

__all__ = ["AppInfo", "KNOWN_APPS", "find_app", "launch_app", "list_apps"]
