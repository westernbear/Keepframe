"""web_search and web_fetch for the session agent, stdlib only.

web_search follows smolagents' WebSearchTool (Apache-2.0, huggingface/smolagents default_tools.py): DuckDuckGo lite
HTML, Bing RSS as fallback. web_fetch takes its arguments from the MCP fetch server (MIT,
modelcontextprotocol/servers src/fetch): url, max_length, start_index.
ponytail: both search endpoints are unofficial and rate-limit without notice; upgrade to provider-hosted search
(Responses {"type": "web_search"}, LiteLLM web_search_options) or a Tavily/Brave key when that bites.
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

TIMEOUT = 15
MAX_BYTES = 2 * 1024 * 1024
MAX_LENGTH = 20_000
SNIPPET = 300
_UA = {"User-Agent": "Mozilla/5.0 (compatible; Keepframe agent)"}
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml+xml")


class WebError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _public(host: str) -> bool:
    # ponytail: checks the resolved addresses, then urllib resolves again (DNS rebinding window); pin the IP if needed
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise WebError("web_unreachable", f"{host} does not resolve") from e
    return all(ipaddress.ip_address(info[4][0].split("%")[0]).is_global for info in infos)


def _guard(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise WebError("web_blocked", "only public http(s) URLs")
    if not _public(parsed.hostname):
        raise WebError("web_blocked", f"{parsed.hostname} is not a public address")
    return url


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, _guard(urllib.parse.urljoin(req.full_url, newurl)))


def _get(url: str, *, guard: bool) -> tuple[str, str, str]:
    """(final url, content type, decoded body) of a capped GET."""
    opener = urllib.request.build_opener(_GuardedRedirect) if guard else urllib.request.build_opener()
    request = urllib.request.Request(_guard(url) if guard else url, headers=_UA)
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise WebError("web_too_large", f"over {MAX_BYTES // (1024 * 1024)} MB")
            ctype = response.headers.get_content_type()
            charset = response.headers.get_content_charset() or "utf-8"
            return response.geturl(), ctype, data.decode(charset, errors="replace")
    except WebError:
        raise
    except Exception as e:  # noqa: BLE001 - network boundary: any failure is "unreachable" to the agent
        raise WebError("web_unreachable", type(e).__name__) from e


class _DuckLite(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self.current: dict[str, str] = {}
        self.field: str | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("class") == "result-link":
            href = attrs.get("href") or ""
            target = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg")
            self.current = {"title": "", "url": target[0] if target else urllib.parse.urljoin("https:", href), "snippet": ""}
            self.field = "title"
        elif tag == "td" and attrs.get("class") == "result-snippet" and self.current:
            self.field = "snippet"

    def handle_endtag(self, tag):
        if (tag == "a" and self.field == "title") or (tag == "td" and self.field == "snippet"):
            self.field = None
            if tag == "td":
                self.results.append(self.current)
                self.current = {}

    def handle_data(self, data):
        if self.field:
            self.current[self.field] = " ".join(f"{self.current[self.field]} {data}".split())


def _duckduckgo(query: str) -> list[dict[str, str]]:
    _, _, body = _get("https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode({"q": query}), guard=False)
    parser = _DuckLite()
    parser.feed(body)
    return parser.results


def _bing(query: str) -> list[dict[str, str]]:
    _, _, body = _get("https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "format": "rss"}), guard=False)
    try:
        items = ET.fromstring(body).iter("item")
    except ET.ParseError as e:
        raise WebError("web_unreachable", "Bing returned no RSS") from e
    return [{"title": i.findtext("title") or "", "url": i.findtext("link") or "", "snippet": i.findtext("description") or ""}
            for i in items]


def search(query: str, max_results: int = 5) -> list[dict[str, str]]:
    query = " ".join(str(query or "").split())
    if not query:
        raise WebError("web_bad_args", "query is empty")
    words = [w for w in query.casefold().split() if len(w) > 1] or [query.casefold()]
    errors = []
    for backend in (_duckduckgo, _bing):
        try:
            # Bing RSS answers throttled clients with unrelated pages; a hit must share a query word
            results = [r for r in backend(query) if r["url"].startswith(("http://", "https://"))
                       and any(w in f"{r['title']} {r['snippet']} {r['url']}".casefold() for w in words)]
        except WebError as e:
            errors.append(e)
            continue
        if results:
            return [{**r, "snippet": r["snippet"][:SNIPPET]} for r in results[: max(1, min(int(max_results), 10))]]
    if len(errors) == 2:
        raise errors[-1]
    return []


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "section", "article", "header", "footer", "pre", "blockquote", "table", "ul", "ol"}

    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base, self.parts, self.title, self.skip, self.in_title = base, [], "", 0, False
        self.links: list[tuple[int, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif len(tag) == 2 and tag[0] == "h" and tag[1].isdigit():
            self.parts.append("\n" + "#" * int(tag[1]) + " ")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self.links.append((len(self.parts), urllib.parse.urljoin(self.base, href) if href else ""))

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK or (len(tag) == 2 and tag[0] == "h" and tag[1].isdigit()):
            self.parts.append("\n")
        elif tag == "a" and self.links:
            start, href = self.links.pop()
            text = " ".join("".join(self.parts[start:]).split())
            if href.startswith(("http://", "https://")) and text:
                self.parts[start:] = [f"[{text}]({href})"]

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self.parts).splitlines())
        out: list[str] = []
        for line in lines:
            if line or (out and out[-1]):
                out.append(line)
        return "\n".join(out).strip()


def fetch(url: str, max_length: int = 5000, start_index: int = 0) -> dict:
    final, ctype, body = _get(str(url or "").strip(), guard=True)
    if not ctype.startswith(_TEXT_TYPES):
        raise WebError("web_unsupported_type", ctype)
    title = ""
    if ctype in ("text/html", "application/xhtml+xml"):
        parser = _Text(final)
        parser.feed(body)
        body, title = parser.text(), " ".join(parser.title.split())
    start = max(0, int(start_index))
    length = max(1, min(int(max_length), MAX_LENGTH))
    end = start + length
    return {"url": final, "title": title, "content": body[start:end], "next_start": end if end < len(body) else None}
