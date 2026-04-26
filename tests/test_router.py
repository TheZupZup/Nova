import httpx
import ollama
import pytest
from unittest.mock import patch
from config import MODELS
from core.router import route, FALLBACK_MODEL


def _mock_response(content):
    return {"message": {"content": content}}


class TestRoute:
    def test_code_category_returns_code_model(self):
        with patch("core.router.client.chat", return_value=_mock_response("code")):
            assert route("write a python script") == MODELS["code"]

    def test_unknown_category_returns_fallback(self):
        with patch("core.router.client.chat", return_value=_mock_response("banana")):
            assert route("some input") == FALLBACK_MODEL

    def test_connection_error_returns_fallback(self):
        with patch("core.router.client.chat", side_effect=ConnectionError("unreachable")):
            assert route("some input") == FALLBACK_MODEL

    def test_ollama_response_error_returns_fallback(self):
        with patch("core.router.client.chat", side_effect=ollama.ResponseError("bad")):
            assert route("some input") == FALLBACK_MODEL

    def test_httpx_error_returns_fallback(self):
        with patch("core.router.client.chat", side_effect=httpx.HTTPError("timeout")):
            assert route("some input") == FALLBACK_MODEL
