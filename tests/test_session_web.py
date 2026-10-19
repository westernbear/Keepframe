import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from keepframe.session import web
from keepframe.session.agent import SessionAgent
from keepframe.session.llm import AssistantReply
from keepframe.session.tools import SessionContext, run_tool

DDG = """<table><tr><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Ffonts.example%2Fpretendard&amp;rut=x"
class="result-link">Pretendard <b>font</b></a></td></tr><tr><td class="result-snippet">A Korean <b>sans</b> family.</td></tr>
<tr><td><a href="https://ae.example/guide" class="result-link">AE guide</a></td></tr>
<tr><td class="result-snippet">Expressions.</td></tr></table>"""
BING = """<?xml version="1.0"?><rss><channel><item><title>Bing hit</title><link>https://bing.example/a</link>
<description>From RSS.</description></item></channel></rss>"""
PAGE = b"""<html><head><title>Kinetic type</title><style>p{}</style></head><body><script>alert(1)</script>
<h2>Track in</h2><p>Letters slide <a href="/docs">see docs</a> in.</p><p>""" + b"x" * 300 + b"</p></body></html>"


def test_search_parses_duckduckgo_and_falls_back_to_bing(monkeypatch):
    monkeypatch.setattr(web, "_get", lambda url, guard: ("", "text/html", DDG))
    assert web.search("pretendard ae guide", 5) == [
        {"title": "Pretendard font", "url": "https://fonts.example/pretendard", "snippet": "A Korean sans family."},
        {"title": "AE guide", "url": "https://ae.example/guide", "snippet": "Expressions."},
    ]

    def blocked_then_rss(url, guard):
        if "duckduckgo" in url:
            raise web.WebError("web_unreachable", "HTTPError")
        return "", "application/rss+xml", BING
    monkeypatch.setattr(web, "_get", blocked_then_rss)
    assert web.search("ae", 1) == []
    assert web.search("bing", 1) == [{"title": "Bing hit", "url": "https://bing.example/a", "snippet": "From RSS."}]
    assert run_tool("web_search", None, {"query": " "})["message"].startswith("web_bad_args")


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://169.254.169.254/latest", "file:///etc/passwd", "ftp://x.org/"])
def test_fetch_refuses_non_public_urls(url):
    result = run_tool("web_fetch", None, {"url": url})
    assert not result["ok"] and result["message"].startswith("web_blocked")


def test_fetch_extracts_text_pages_and_caps(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/hop":
                self.send_response(302)
                self.send_header("Location", "/page")
                self.end_headers()
                return
            body = b"\x89PNG" if self.path == "/png" else PAGE
            self.send_response(200)
            self.send_header("Content-Type", "image/png" if self.path == "/png" else "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(web, "_public", lambda host: True)
    try:
        page = run_tool("web_fetch", None, {"url": base + "/hop", "max_length": 100})["payload"]
        assert page["url"] == base + "/page" and page["title"] == "Kinetic type"
        assert page["content"].startswith(f"## Track in\n\nLetters slide [see docs]({base}/docs) in.")
        assert "alert" not in page["content"] and page["next_start"] == 100
        rest = web.fetch(base + "/page", 20_000, page["next_start"])
        assert rest["next_start"] is None and rest["content"].endswith("x" * 50) and len(page["content"] + rest["content"]) > 300
        assert run_tool("web_fetch", None, {"url": base + "/png"})["message"].startswith("web_unsupported_type")
        monkeypatch.setattr(web, "MAX_BYTES", 10)
        assert run_tool("web_fetch", None, {"url": base + "/page"})["message"].startswith("web_too_large")
    finally:
        server.shutdown()


def test_agent_round_trips_web_search(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "search", lambda query, n: [{"title": "t", "url": "https://a.example", "snippet": "s"}])
    monkeypatch.setattr("keepframe.session.agent.scene_brief", lambda *a, **k: "", raising=False)

    class Stub:
        replies = [AssistantReply(tool_calls=[{"id": "1", "name": "web_search", "arguments": {"query": "q"}}]),
                   AssistantReply(content="done")]

        def complete(self, messages, tools):
            assert any(t["function"]["name"] == "web_fetch" for t in tools)
            return self.replies.pop(0)

    turn = SessionAgent(Stub()).turn(SessionContext(root=tmp_path, scene_id="s1"), "find", [])
    assert turn.reply == "done" and turn.results[0]["payload"]["results"][0]["url"] == "https://a.example"
