"""Connectors included when a run asks for every source.

``full_run_sources.json`` is the allowlist. It was seeded from the connectors
registered at the time, minus FlexJobs and JustJoin. A connector added to
``CONNECTORS`` later stays off until it is checked in Settings.
"""
from __future__ import annotations

import json
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "full_run_sources.json"


def read_enabled() -> list[str] | None:
    """Return saved names, or None when the file is missing or unreadable."""
    if not PATH.is_file():
        return None
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    names = data.get("enabled") if isinstance(data, dict) else None
    if not isinstance(names, list):
        return None
    return [name.strip() for name in names if isinstance(name, str) and name.strip()]


def enabled_sources(known: list[str] | tuple[str, ...]) -> list[str]:
    """Names from ``known`` that the allowlist includes, in ``known`` order."""
    saved = read_enabled()
    if not saved:
        return []
    chosen = set(saved)
    return [name for name in known if name in chosen]


def save_enabled(names: list[str]) -> None:
    PATH.write_text(
        json.dumps({"enabled": list(names)}, indent=2) + "\n",
        encoding="utf-8",
    )
