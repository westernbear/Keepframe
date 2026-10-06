import http.client
import io
import json
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import UUID

import pytest

from keepframe.session.chatgpt_client import ChatGPTClient
from tests.test_web_agent import capture_agent_client
from keepframe.session.chatgpt_oauth import refresh_chatgpt_token
from keepframe.session.llm import AssistantReply, LiteLLMClient, OpenAICompatibleClient, make_llm, vision_llm
from keepframe.session.provider import ProviderConfig, load_llm_settings, save_llm_settings


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
    assert body["tools"] == [{"type": "function", **EDIT["function"], "strict": False}]
    assert body["tool_choice"] == "auto"
    assert body["stream"] is True and body["store"] is False and body["parallel_tool_calls"] is False
    assert "include" not in body
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
def test_http_error_preserves_bounded_html_without_secrets(config, backend, caplog, monkeypatch, status):
    monkeypatch.setattr("keepframe.session.chatgpt_client.refresh_chatgpt_token", lambda token: {
        "access_token": "retry-access", "expires_in": 3600,
    })
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
def test_refresh_before_request_persists_via_hook(config, backend, monkeypatch, tmp_path, missing_access):
    config.oauth_expires_at = time.time() - 30
    if missing_access:
        config.api_key = ""
    before = config.model_dump()
    refreshed = []
    def refresh(token):
        refreshed.append(token)
        return {"access_token": "new-access-secret", "refresh_token": "rotated-refresh-secret",
                "id_token": "new-id-secret", "expires_in": 3600}
    monkeypatch.setattr("keepframe.session.chatgpt_client.refresh_chatgpt_token", refresh)
    client = ChatGPTClient(config, base_url=backend["url"], on_refresh=lambda cfg: save_llm_settings(tmp_path, cfg))
    for _ in range(2):
        assert client.complete([], []).content == "pong"
    assert refreshed == [config.refresh_token]
    assert all(r["headers"]["Authorization"] == "Bearer new-access-secret" for r in backend["requests"])
    assert config.model_dump() == before
    saved = load_llm_settings(tmp_path)
    assert (saved.api_key, saved.refresh_token, saved.id_token) == (
        "new-access-secret", "rotated-refresh-secret", "new-id-secret")
    assert saved.oauth_expires_at > time.time() + 3500


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


@pytest.mark.parametrize("factory", ["make", "vision"])
def test_workspace_refresh_persists_and_new_clients_reuse(config, backend, monkeypatch, tmp_path, factory):
    config.oauth_expires_at = time.time() - 1
    save_llm_settings(tmp_path, config)
    calls = []
    def refresh(token):
        calls.append(token)
        return {"access_token": "saved-access", "refresh_token": "saved-refresh",
                "id_token": "saved-id", "expires_in": 3600}
    monkeypatch.setattr("keepframe.session.chatgpt_client.refresh_chatgpt_token", refresh)
    for _ in range(2):
        client = make_llm(config, workspace=tmp_path) if factory == "make" else vision_llm(tmp_path)
        client.base_url = backend["url"]
        assert client.complete([], []).content == "pong"
    assert calls == [config.refresh_token]
    saved = load_llm_settings(tmp_path)
    assert (saved.api_key, saved.refresh_token, saved.id_token) == ("saved-access", "saved-refresh", "saved-id")
    assert all(r["headers"]["Authorization"] == "Bearer saved-access" for r in backend["requests"])


def test_concurrent_clients_refresh_once(config, backend, monkeypatch, tmp_path):
    config.oauth_expires_at = time.time() - 1
    save_llm_settings(tmp_path, config)
    calls, barrier = [], threading.Barrier(2)
    def refresh(token):
        calls.append(token)
        time.sleep(0.05)
        return {"access_token": "shared-access", "refresh_token": "shared-refresh", "expires_in": 3600}
    monkeypatch.setattr("keepframe.session.chatgpt_client.refresh_chatgpt_token", refresh)
    clients = [make_llm(config, workspace=tmp_path) for _ in range(2)]
    def complete(client):
        client.base_url = backend["url"]
        barrier.wait(timeout=5)
        return client.complete([], [])
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert [reply.content for reply in pool.map(complete, clients)] == ["pong", "pong"]
    assert calls == [config.refresh_token]
    assert all(r["headers"]["Authorization"] == "Bearer shared-access" for r in backend["requests"])


@pytest.mark.parametrize("admin_enabled", [False, True])
def test_server_factory_persists_refresh_and_updates_admin(config, backend, monkeypatch, tmp_path, admin_enabled):
    from keepframe.admin.memory import MemoryAdmin
    from keepframe.web.server import make_server

    config.oauth_expires_at = time.time() - 1
    admin = MemoryAdmin(workspace=tmp_path) if admin_enabled else None
    if admin is not None:
        admin.set_llm_settings(config, "test")
    else:
        save_llm_settings(tmp_path, config)
    calls = []
    def refresh(token):
        calls.append(token)
        return {"access_token": "server-access", "refresh_token": "server-refresh",
                "id_token": "server-id", "expires_in": 3600}
    monkeypatch.setattr("keepframe.session.chatgpt_client.refresh_chatgpt_token", refresh)
    server = make_server(tmp_path, port=0, admin_svc=admin)
    try:
        for _ in range(2):
            client = capture_agent_client(server, tmp_path, monkeypatch)
            client.base_url = backend["url"]
            assert client.complete([], []).content == "pong"
    finally:
        server.server_close()
    assert calls == [config.refresh_token]
    assert load_llm_settings(tmp_path).refresh_token == "server-refresh"
    if admin is not None:
        assert admin.get_llm_settings().api_key == "server-access"
        assert admin.get_llm_settings().refresh_token == "server-refresh"
        assert admin.get_llm_settings().id_token == "server-id"
        assert admin.get_llm_settings().oauth_expires_at > time.time() + 3500


def test_analyze_job_captioner_persists_refresh(config, backend, monkeypatch, tmp_path):
    from keepframe.jobs import JobSpec, run_job

    config.oauth_expires_at = time.time() - 1
    save_llm_settings(tmp_path, config)
    monkeypatch.setattr("keepframe.session.chatgpt_client.refresh_chatgpt_token", lambda token: {
        "access_token": "job-access", "refresh_token": "job-refresh", "expires_in": 3600,
    })
    def analyze(*args, captioner, **kwargs):
        captioner.base_url = backend["url"]
        assert captioner.complete([], []).content == "pong"
    monkeypatch.setattr("keepframe.analyze.pipeline.analyze", analyze)
    assert run_job(JobSpec(kind="analyze", args={
        "video": "synthetic.mp4", "start": 0, "end": 23, "out_root": str(tmp_path / "project"),
        "workspace": str(tmp_path),
    })) == {"project_id": None}
    assert load_llm_settings(tmp_path).refresh_token == "job-refresh"


@pytest.mark.parametrize("seconds, expected", [(0, 1), (59, 1), (60, 1), (61, 0)])
def test_refresh_has_sixty_second_margin(config, backend, monkeypatch, seconds, expected):
    now, calls = time.time(), []
    monkeypatch.setattr("keepframe.session.chatgpt_client.time.time", lambda: now)
    config.oauth_expires_at = now + seconds
    def refresh(token):
        calls.append(token)
        return {"access_token": "early-access", "expires_in": 3600}
    monkeypatch.setattr("keepframe.session.chatgpt_client.refresh_chatgpt_token", refresh)
    ChatGPTClient(config, base_url=backend["url"]).complete([], [])
    assert len(calls) == expected


@pytest.mark.parametrize("retry_status", [200, 401])
def test_401_refreshes_and_retries_only_once(config, backend, monkeypatch, tmp_path, retry_status):
    backend.update(status=401, body="unauthorized")
    calls = []
    def refresh(token):
        calls.append(token)
        backend["status"] = retry_status
        return {"access_token": "retry-access", "refresh_token": "retry-refresh", "expires_in": 3600}
    monkeypatch.setattr("keepframe.session.chatgpt_client.refresh_chatgpt_token", refresh)
    client = ChatGPTClient(config, base_url=backend["url"], on_refresh=lambda cfg: save_llm_settings(tmp_path, cfg))
    if retry_status == 200:
        assert client.complete([], []).content == "pong"
    else:
        with pytest.raises(RuntimeError, match="401"):
            client.complete([], [])
    assert calls == [config.refresh_token]
    assert [r["headers"]["Authorization"] for r in backend["requests"]] == [
        f"Bearer {config.api_key}", "Bearer retry-access"]
    assert load_llm_settings(tmp_path).refresh_token == "retry-refresh"


def test_401_reuses_tokens_refreshed_by_another_client(config, backend, monkeypatch, tmp_path):
    save_llm_settings(tmp_path, config)
    client = make_llm(config, workspace=tmp_path)
    client.base_url = backend["url"]
    requests = []
    def send(req, timeout):
        requests.append(req.get_header("Authorization"))
        if len(requests) == 1:
            saved = config.model_copy(update={"api_key": "other-access", "refresh_token": "other-refresh"})
            save_llm_settings(tmp_path, saved)
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", None, io.BytesIO(b"unauthorized"))
        return io.BytesIO(("data: " + json.dumps(COMPLETED) + "\n\n").encode())
    def forbidden(token):
        raise AssertionError("already refreshed")
    monkeypatch.setattr("urllib.request.urlopen", send)
    monkeypatch.setattr("keepframe.session.chatgpt_client.refresh_chatgpt_token", forbidden)
    assert client.complete([], []) == AssistantReply()
    assert requests == [f"Bearer {config.api_key}", "Bearer other-access"]


def test_all_real_tool_schemas_opt_out_of_strict(config, backend):
    from keepframe.session.tools import TOOL_SCHEMAS

    tools = TOOL_SCHEMAS
    ChatGPTClient(config, base_url=backend["url"]).complete([], tools)
    converted = backend["requests"][0]["body"]["tools"]
    assert len(converted) == len(tools)
    for original, tool in zip(tools, converted):
        assert tool["strict"] is False
        assert tool["parameters"] == original["function"]["parameters"]


def test_session_agent_receives_valid_typed_edit(config, backend, tmp_path):
    from keepframe.analyze.constraints import apply_keep_preset, extract_constraints
    from keepframe.edit.intent import Target
    from keepframe.ir.store import init_project
    from keepframe.ir.synth import make_synthetic_scene
    from keepframe.session.agent import SessionAgent
    from keepframe.session.tools import TOOL_SCHEMAS, SessionContext

    root = tmp_path / "project"
    scene = make_synthetic_scene(root / "scenes" / "s1", seed=4, with_text=True, frames=24).model_copy(update={"id": "s1"})
    scene.constraints = apply_keep_preset(extract_constraints(scene), "content_only")
    init_project(root, {"file": "synthetic.mp4", "fps": scene.fps, "size": list(scene.size),
                        "mode": "range", "range": [0, 23]}, scene)
    arguments = {"prompt": "e1 색을 #ff0000으로 바꿔줘", "targets": [
        {"element": "e1", "property": "color", "value": "#ff0000"},
    ]}
    backend["events"] = [{"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": "call_edit", "name": "edit", "arguments": json.dumps(arguments),
    }}, COMPLETED]
    client = ChatGPTClient(config, base_url=backend["url"])
    turn = SessionAgent(client).turn(SessionContext(root, "s1"), arguments["prompt"], [])
    assert turn.needs_confirm and turn.status == "pending"
    assert turn.tool_calls == [{"name": "edit", "arguments": arguments}]
    assert Target.model_validate(turn.tool_calls[0]["arguments"]["targets"][0]).element == "e1"
    sent_tools = backend["requests"][0]["body"]["tools"]
    assert [t["name"] for t in sent_tools] == [t["function"]["name"] for t in TOOL_SCHEMAS]
    assert all(t["strict"] is False for t in sent_tools)


@pytest.mark.parametrize("detail", ["low", "high", "auto"])
def test_image_detail_is_forwarded(config, backend, detail):
    ChatGPTClient(config, base_url=backend["url"]).complete([{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,cG5n", "detail": detail}},
    ]}], [])
    assert backend["requests"][0]["body"]["input"][0]["content"][0]["detail"] == detail


def test_saved_base_url_is_ignored(config, monkeypatch):
    config.base_url = "https://wrong.example/v1"
    test_timeout_and_default_endpoint(config, monkeypatch)


def test_reasoning_effort_is_configurable(config, backend):
    config.extra["reasoning_effort"] = "high"
    ChatGPTClient(config, base_url=backend["url"]).complete([], [])
    assert backend["requests"][0]["body"]["reasoning"]["effort"] == "high"


@pytest.mark.parametrize("kind", ["response.completed", "response.failed", "response.incomplete", "error"])
@pytest.mark.parametrize("payload", [[], "bad", 42, None])
def test_non_object_response_payload_is_runtime_error(config, backend, kind, payload):
    backend["events"] = [{"type": kind, "response": payload}]
    with pytest.raises(RuntimeError):
        ChatGPTClient(config, base_url=backend["url"]).complete([], [])


@pytest.mark.parametrize("where", ["open", "stream", "error_body"])
def test_incomplete_http_read_is_bounded_and_redacted(config, monkeypatch, where):
    class Stream:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def __iter__(self):
            raise http.client.IncompleteRead(config.api_key.encode(), 100)
    class Body(io.BytesIO):
        def read(self, size=-1):
            raise http.client.IncompleteRead(config.api_key.encode(), 100)
    def send(req, timeout):
        if where == "stream":
            return Stream()
        if where == "error_body":
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", None, Body())
        raise http.client.IncompleteRead(config.api_key.encode(), 100)
    monkeypatch.setattr("urllib.request.urlopen", send)
    with pytest.raises(RuntimeError) as error:
        ChatGPTClient(config).complete([], [])
    assert config.api_key not in str(error.value) and error.value.__suppress_context__


def test_http_error_body_read_is_limited(config, monkeypatch):
    reads = []
    class Body(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)
    def send(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", None, Body(b"x" * 100000))
    monkeypatch.setattr("urllib.request.urlopen", send)
    with pytest.raises(RuntimeError):
        ChatGPTClient(config).complete([], [])
    assert reads == [64 * 1024]
