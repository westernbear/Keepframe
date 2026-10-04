import io
import json
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import UUID

import pytest

from keepframe.session.chatgpt_client import ChatGPTClient
from keepframe.session.chatgpt_oauth import refresh_chatgpt_token
from keepframe.session.llm import AssistantReply, LiteLLMClient, OpenAICompatibleClient, make_llm, vision_llm
from keepframe.session.provider import ProviderConfig, save_llm_settings


@pytest.fixture
def config():
    return ProviderConfig(
        provider="chatgpt", auth="oauth", model="gpt-6.1-sol", api_key="test-access-secret",
        refresh_token="test-refresh-secret", id_token="test-id-secret", account_id="test-account-secret",
        oauth_expires_at=time.time() + 3600,
    )


def message(text):
    return {"type": "response.output_item.done", "item": {
        "type": "message", "content": [{"type": "output_text", "text": text}],
    }}


COMPLETED = {"type": "response.completed", "response": {"status": "completed"}}
EDIT = {"type": "function", "function": {
    "name": "edit", "description": "Edit a scene", "parameters": {
        "type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"],
    },
}}


@pytest.fixture
def backend():
    state = {"events": [message("pong"), COMPLETED], "status": 200, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def do_POST(self):
            state["requests"].append({
                "path": self.path, "headers": dict(self.headers),
                "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
            })
            self.send_response(state["status"])
            self.send_header("Content-Type", "text/event-stream" if state["status"] == 200 else "text/html")
            self.end_headers()
            if state["status"] != 200:
                self.wfile.write(state["body"].encode())
                return
            self.wfile.write(b": keepalive\n\nevent: ignored\n")
            for event in state["events"]:
                raw = event if isinstance(event, str) else json.dumps(event, ensure_ascii=False)
                self.wfile.write(f"data: {raw}\n\n".encode())
                self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}/backend-api/codex"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_streamed_text_and_completed_stop(config, backend):
    backend["events"] = [
        {"type": "response.output_text.delta", "delta": "pong"}, message("pong"), COMPLETED,
        {"type": "error", "message": "must not read after completion"},
    ]
    reply = ChatGPTClient(config, base_url=backend["url"]).complete([{"role": "user", "content": "ping"}], [])
    assert reply == AssistantReply(content="pong")
    assert backend["requests"][0]["path"] == "/backend-api/codex/responses"


def test_streamed_function_calls(config, backend):
    backend["events"] = [{"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": "call_edit", "name": "edit", "arguments": '{"prompt":"문구 변경"}',
    }}, COMPLETED]
    reply = ChatGPTClient(config, base_url=backend["url"]).complete([{"role": "user", "content": "edit"}], [EDIT])
    assert reply == AssistantReply(tool_calls=[{"id": "call_edit", "name": "edit", "arguments": {"prompt": "문구 변경"}}])


def test_request_conversion_and_headers(config, backend):
    image = "data:image/png;base64,cG5n"
    messages = [
        {"role": "system", "content": "First rule"}, {"role": "system", "content": "Scene data"},
        {"role": "user", "content": "plain"},
        {"role": "user", "content": [{"type": "text", "text": "see this"}, {"type": "image_url", "image_url": {"url": image}}]},
        {"role": "assistant", "content": "Editing", "tool_calls": [{"id": "call_1", "type": "function", "function": {
            "name": "edit", "arguments": '{"prompt":"update"}',
        }}]},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call_2", "function": {"name": "edit", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": '{"done":true}'},
    ]
    ChatGPTClient(config, base_url=backend["url"]).complete(messages, [EDIT])
    request = backend["requests"][0]
    body = request["body"]
    assert body["model"] == "gpt-6.1-sol"
    assert body["instructions"] == "First rule\nScene data"
    assert body["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "plain"}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "see this"}, {"type": "input_image", "image_url": image}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Editing"}]},
        {"type": "function_call", "call_id": "call_1", "name": "edit", "arguments": '{"prompt":"update"}'},
        {"type": "function_call", "call_id": "call_2", "name": "edit", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_1", "output": '{"done":true}'},
    ]
    assert body["tools"] == [{"type": "function", **EDIT["function"]}]
    assert body["tool_choice"] == "auto"
    assert body["stream"] is True and body["store"] is False and body["parallel_tool_calls"] is False
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["reasoning"]["effort"] == "low"
    headers = {k.lower(): v for k, v in request["headers"].items()}
    assert headers["authorization"] == f"Bearer {config.api_key}"
    assert headers["chatgpt-account-id"] == config.account_id
    assert headers["openai-beta"] == "responses=experimental"
    assert headers["originator"] == "codex_cli_rs"
    assert headers["accept"] == "text/event-stream"
    assert headers["user-agent"].startswith("codex_cli_rs/")
    UUID(headers["session_id"])


def test_empty_tools_and_instructions(config, backend):
    client = ChatGPTClient(config, base_url=backend["url"])
    for _ in range(2):
        client.complete([{"role": "user", "content": "ping"}], [])
    body = backend["requests"][0]["body"]
    assert "tools" not in body and "tool_choice" not in body
    assert body["instructions"] == ""
    headers = [{k.lower(): v for k, v in request["headers"].items()} for request in backend["requests"]]
    assert headers[0]["session_id"] == headers[1]["session_id"]


@pytest.mark.parametrize("kind", ["response.failed", "error"])
def test_error_events_are_bounded_and_redacted(config, backend, caplog, kind):
    detail = " ".join([config.api_key, config.refresh_token, config.id_token, config.account_id]) + " rejected " + "x" * 250 + "TAIL"
    backend["events"] = [{"type": kind, "response": {"error": {"message": detail}}} if kind == "response.failed" else {"type": kind, "error": {"message": detail}}]
    with pytest.raises(RuntimeError) as error:
        ChatGPTClient(config, base_url=backend["url"]).complete([], [])
    assert "rejected" in str(error.value) and "TAIL" not in str(error.value)
    assert len(str(error.value).split(": ", 1)[1]) <= 200
    for secret in (config.api_key, config.refresh_token, config.id_token, config.account_id):
        assert secret not in str(error.value) + caplog.text
    assert error.value.__suppress_context__


@pytest.mark.parametrize("status", [400, 401, 403, 429])
def test_http_error_preserves_bounded_html_without_secrets(config, backend, caplog, status):
    backend.update(status=status, body="<html>" + config.api_key + " " + config.account_id + " blocked " + "x" * 250 + "TAIL</html>")
    with pytest.raises(RuntimeError) as error:
        ChatGPTClient(config, base_url=backend["url"]).complete([], [])
    text = str(error.value)
    assert str(status) in text and "<html>" in text and "blocked" in text and "TAIL" not in text
    assert len(text.split(": ", 1)[1]) <= 200
    assert config.api_key not in text + caplog.text and config.account_id not in text + caplog.text
    assert error.value.__suppress_context__


@pytest.mark.parametrize("events", [[], [message("partial")], ["[DONE]"], ["not json"], [{"type": "response.incomplete", "response": {"incomplete_details": {"reason": "limit"}}}]])
def test_broken_or_incomplete_stream_is_an_error(config, backend, events):
    backend["events"] = events
    with pytest.raises(RuntimeError):
        ChatGPTClient(config, base_url=backend["url"]).complete([], [])


@pytest.mark.parametrize("arguments", ["not json", "[]", "null"])
def test_invalid_function_arguments_do_not_execute(config, backend, arguments):
    backend["events"] = [{"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": "call_bad", "name": "edit", "arguments": arguments,
    }}, COMPLETED]
    with pytest.raises(RuntimeError):
        ChatGPTClient(config, base_url=backend["url"]).complete([], [EDIT])


@pytest.mark.parametrize("missing_access", [False, True])
def test_refresh_before_request_is_kept_in_memory(config, backend, monkeypatch, missing_access):
    config.oauth_expires_at = time.time() - 30
    if missing_access:
        config.api_key = ""
    before = config.model_dump()
    refreshed = []
    def refresh(token):
        refreshed.append(token)
        return {"access_token": "new-access-secret", "refresh_token": "rotated-refresh-secret", "expires_in": 3600}
    monkeypatch.setattr("keepframe.session.chatgpt_client.refresh_chatgpt_token", refresh)
    client = ChatGPTClient(config, base_url=backend["url"])
    for _ in range(2):
        assert client.complete([], []).content == "pong"
    assert refreshed == [config.refresh_token]
    assert all(r["headers"]["Authorization"] == "Bearer new-access-secret" for r in backend["requests"])
    assert config.model_dump() == before


def test_refresh_failure_hides_secrets(config, backend, monkeypatch, caplog):
    config.oauth_expires_at = time.time() - 30
    def refresh(token):
        raise RuntimeError(f"failed {token} {config.api_key}")
    monkeypatch.setattr("keepframe.session.chatgpt_client.refresh_chatgpt_token", refresh)
    with pytest.raises(RuntimeError) as error:
        ChatGPTClient(config, base_url=backend["url"]).complete([], [])
    assert config.api_key not in str(error.value) + caplog.text and config.refresh_token not in str(error.value) + caplog.text
    assert error.value.__suppress_context__ and backend["requests"] == []


def test_oauth_refresh_http_failure_does_not_log_response_secrets(config, monkeypatch, caplog):
    def send(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", None, io.BytesIO(config.refresh_token.encode()))
    monkeypatch.setattr("urllib.request.urlopen", send)
    with pytest.raises(RuntimeError) as error:
        refresh_chatgpt_token(config.refresh_token)
    assert config.refresh_token not in str(error.value) + caplog.text


def test_timeout_and_default_endpoint(config, monkeypatch):
    seen = {}
    def send(req, timeout):
        seen.update(url=req.full_url, timeout=timeout)
        return io.BytesIO(("data: " + json.dumps(COMPLETED) + "\n\n").encode())
    monkeypatch.setattr("urllib.request.urlopen", send)
    ChatGPTClient(config).complete([], [])
    assert seen == {"url": "https://chatgpt.com/backend-api/codex/responses", "timeout": 120.0}


def test_make_llm_routes_chatgpt_without_litellm(config, monkeypatch, tmp_path):
    def forbidden(config):
        raise AssertionError("ChatGPT must not initialize LiteLLM")
    monkeypatch.setattr("keepframe.session.llm.LiteLLMClient", forbidden)
    assert isinstance(make_llm(config), ChatGPTClient)
    save_llm_settings(tmp_path, config)
    assert isinstance(vision_llm(tmp_path), ChatGPTClient)
    assert ChatGPTClient.supports_vision is True


@pytest.mark.parametrize("provider", ["openai", "anthropic", "azure", "ollama"])
def test_other_providers_still_use_litellm(monkeypatch, provider):
    selected = []
    sentinel = object()
    def create(config):
        selected.append(config.provider)
        return sentinel
    monkeypatch.setattr("keepframe.session.llm.LiteLLMClient", create)
    assert make_llm(ProviderConfig(provider=provider, api_key="test-key")) is sentinel
    assert selected == [provider]


def test_gpt6_vision_markers():
    assert OpenAICompatibleClient(api_key="test-key", model="gpt-6-sol").supports_vision
    assert LiteLLMClient(ProviderConfig(api_key="test-key", model="gpt-6-sol")).supports_vision
