"""Shared Ollama HTTP helpers for local (`localhost:11434`) or cloud (`ollama.com`)."""

from __future__ import annotations

import requests

import config


def request_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if config.OLLAMA_MODE == "cloud":
        key = (config.OLLAMA_API_KEY or "").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
    return headers


def tags_url() -> str:
    base = config.OLLAMA_URL or ""
    if "/api/" in base:
        return base.split("/api/", 1)[0] + "/api/tags"
    if config.OLLAMA_MODE == "cloud":
        return "https://ollama.com/api/tags"
    return "http://localhost:11434/api/tags"


def is_reachable(timeout: float = 3.0) -> tuple[bool, str]:
    """True when the configured Ollama host answers. No model load."""
    if config.OLLAMA_MODE == "cloud" and not (config.OLLAMA_API_KEY or "").strip():
        return False, "cloud mode requires OLLAMA_API_KEY in .env"
    try:
        response = requests.get(tags_url(), headers=request_headers(), timeout=timeout)
        response.raise_for_status()
        return True, ""
    except requests.RequestException as exc:
        return False, str(exc)


def unreachable_hint(error: str = "") -> str:
    extra = f" {error}".rstrip()
    if config.OLLAMA_MODE == "cloud":
        return (
            f"Ollama Cloud is not reachable at {config.OLLAMA_URL}. "
            f"Set OLLAMA_API_KEY in .env (https://ollama.com/settings/keys).{extra}"
        )
    return (
        f"Ollama is not running at {config.OLLAMA_URL}. "
        f"Start it with `ollama serve` (or the Ollama tray app), then retry.{extra}"
    )
