import json

from keepframe.session.models import (
    list_models,
    openai_models_url,
    parse_gemini_models,
    parse_ollama_models,
    parse_openai_models,
)
from keepframe.session.provider import catalog_entry, default_base_url


def test_catalog_openai_compatible_defaults():
    row = catalog_entry("openai_compatible")
    assert row["default_base_url"] == "http://127.0.0.1:8000/v1"
    assert row["live_models"] is True
    assert default_base_url("openai") == "https://api.openai.com/v1"
    assert default_base_url("ollama") == "http://127.0.0.1:11434"


def test_chatgpt_models_are_static():
    models, source = list_models("chatgpt")
    assert source == "static"
    assert "gpt-5.4" in models


def test_openai_without_key_skips_network():
    models, source = list_models("openai")
    assert source == "static"
    assert models == ["gpt-4o-mini"]


def test_openai_models_url_and_parse():
    assert openai_models_url("http://127.0.0.1:8000/v1") == "http://127.0.0.1:8000/v1/models"
    assert openai_models_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000/v1/models"
    assert parse_openai_models({"data": [{"id": "b"}, {"id": "a"}, {"id": "a"}]}) == ["a", "b"]
    assert parse_ollama_models({"models": [{"name": "llama3.2:latest"}]}) == ["llama3.2:latest"]
    assert parse_gemini_models({"models": [{"name": "models/gemini-2.5-flash"}]}) == ["gemini-2.5-flash"]


def test_list_models_openai_compatible(monkeypatch):
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"data": [{"id": "qwen2.5"}]}).encode()

    def _fake(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        return _Resp()

    monkeypatch.setattr("keepframe.session.models.urllib.request.urlopen", _fake)
    models, source = list_models("openai_compatible", base_url="http://127.0.0.1:8000/v1", api_key="sk-x")
    assert source == "live"
    assert models == ["qwen2.5"]
    assert captured["url"] == "http://127.0.0.1:8000/v1/models"
    assert captured["auth"] == "Bearer sk-x"
