import json

from keepframe.session.llm import LiteLLMClient, NullClient, OpenAICompatibleClient, make_llm
from keepframe.session.provider import ProviderConfig


def test_null_client_replies_without_tools():
    reply = NullClient().complete([{"role": "user", "content": "hi"}], [])
    assert reply.tool_calls == []
    assert "KEEPFRAME_LLM_API_KEY" in reply.content


def test_openai_client_parses_tool_calls(monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {"name": "edit", "arguments": '{"prompt": "문구를 Hello로"}'},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp())
    client = OpenAICompatibleClient(api_key="x", model="m")
    reply = client.complete([{"role": "user", "content": "hi"}], [])
    assert reply.content == ""
    assert reply.tool_calls[0]["name"] == "edit"
    assert reply.tool_calls[0]["arguments"] == {"prompt": "문구를 Hello로"}


def test_openai_client_posts_tools(monkeypatch):
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")

    def _fake(req, timeout=None):
        captured["data"] = req.data
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _fake)
    client = OpenAICompatibleClient(base_url="http://x", api_key="k", model="m")
    client.complete([{"role": "user", "content": "hi"}], [{"type": "function"}])
    body = json.loads(captured["data"])
    assert body["model"] == "m"
    assert body["tools"] == [{"type": "function"}]


def test_provider_config_model_and_credentials(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("KEEPFRAME_LLM_API_KEY", raising=False)
    assert ProviderConfig(provider="anthropic", model="claude-3-5-sonnet").litellm_model == "anthropic/claude-3-5-sonnet"
    assert ProviderConfig(provider="openai", model="gpt-4o").credentials_present() is False
    assert ProviderConfig(provider="openai", model="gpt-4o", api_key="k").credentials_present() is True
    assert ProviderConfig(provider="ollama", model="llama3").credentials_present() is True
    assert ProviderConfig(provider="azure", model="gpt-4o", extra={"tenant_id": "x"}).credentials_present() is True


def test_litellm_client_calls_and_parses(monkeypatch):
    class _Fn:
        name = "edit"
        arguments = '{"prompt": "문구를 Hello로"}'

    class _TC:
        id = "call_1"
        function = _Fn()

    class _Msg:
        content = ""
        tool_calls = [_TC()]

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    captured = {}

    def _fake(**kwargs):
        captured["kwargs"] = kwargs
        return _Resp()

    monkeypatch.setattr("litellm.completion", _fake)
    client = LiteLLMClient(ProviderConfig(provider="openai", model="gpt-4o-mini", api_key="k"))
    reply = client.complete([{"role": "user", "content": "hi"}], [{"type": "function"}])
    assert reply.tool_calls[0]["name"] == "edit"
    assert reply.tool_calls[0]["arguments"] == {"prompt": "문구를 Hello로"}
    assert captured["kwargs"]["model"] == "openai/gpt-4o-mini"
    assert captured["kwargs"]["api_key"] == "k"


def test_make_llm_falls_back_to_null_without_credentials(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("KEEPFRAME_LLM_API_KEY", raising=False)
    assert isinstance(make_llm(ProviderConfig(provider="openai", model="gpt-4o")), NullClient)
