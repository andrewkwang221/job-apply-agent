"""Tests for local vs cloud Ollama request headers and URLs."""
from unittest.mock import patch

import config
from utils.ollama_client import request_headers, tags_url, unreachable_hint


def test_local_headers_have_no_authorization():
    with patch("utils.ollama_client.config.OLLAMA_MODE", "local"):
        headers = request_headers()
    assert "Authorization" not in headers
    assert headers["Content-Type"] == "application/json"


def test_cloud_headers_include_bearer_key():
    with patch("utils.ollama_client.config.OLLAMA_MODE", "cloud"), \
         patch("utils.ollama_client.config.OLLAMA_API_KEY", "ollama-key-test"):
        headers = request_headers()
    assert headers["Authorization"] == "Bearer ollama-key-test"


def test_cloud_headers_omit_authorization_when_key_missing():
    with patch("utils.ollama_client.config.OLLAMA_MODE", "cloud"), \
         patch("utils.ollama_client.config.OLLAMA_API_KEY", ""):
        headers = request_headers()
    assert "Authorization" not in headers


def test_tags_url_follows_chat_endpoint():
    with patch("utils.ollama_client.config.OLLAMA_URL", "https://ollama.com/api/chat"):
        assert tags_url() == "https://ollama.com/api/tags"


def test_unreachable_hint_mentions_api_key_in_cloud_mode():
    with patch("utils.ollama_client.config.OLLAMA_MODE", "cloud"), \
         patch("utils.ollama_client.config.OLLAMA_URL", "https://ollama.com/api/chat"):
        hint = unreachable_hint("401")
    assert "OLLAMA_API_KEY" in hint
    assert "401" in hint


def test_config_mode_is_local_or_cloud():
    assert config.OLLAMA_MODE in ("local", "cloud")
