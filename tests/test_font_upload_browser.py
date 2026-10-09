"""The agent page's Fonts disclosure (Task 11): closed by default, lists the project's uploads, uploads a font and
reports refusals in Korean and English."""
import pytest

from tests.test_agent_layout_browser import seed_agent
from tests.test_font_upload import _ttf

pytestmark = pytest.mark.browser


@pytest.fixture
def agent(tmp_path):
    from playwright.sync_api import sync_playwright
    server, _ = seed_agent(tmp_path)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_default_timeout(10000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/agent?project=p1&scene=s1")
            yield page, tmp_path / "p1"
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


def _files(name: str, data: bytes, mime="application/octet-stream") -> dict:
    return {"name": name, "mimeType": mime, "buffer": data}


def test_font_upload_ui_lists_and_reports_errors(agent):
    from playwright.sync_api import expect
    page, root = agent
    toggle, body = page.locator("#agent-fonts-toggle"), page.locator("#agent-fonts-body")
    status, rows = page.locator("#agent-fonts-status"), page.locator(".font-row")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(toggle).to_have_text("폰트")
    expect(body).to_be_hidden()
    toggle.click()
    expect(body).to_be_visible()
    expect(page.locator("#agent-fonts-empty")).to_have_text("아직 올린 폰트가 없습니다")
    expect(page.locator("#agent-fonts-note")).to_have_text("다음 분석이나 문구 수정부터 쓰입니다")
    expect(page.locator("#agent-fonts-input")).to_have_attribute("accept", ".ttf,.otf,.woff2")

    page.locator("#agent-fonts-input").set_input_files(_files("brand.ttf", _ttf()))
    expect(status).to_have_text("Brand Wide 폰트를 추가했습니다")
    expect(rows).to_have_count(1)
    expect(rows.first.locator(".font-row__family")).to_have_text("Brand Wide")
    expect(rows.first.locator(".font-row__meta")).to_have_text("Regular · 400 · TTF")
    expect(toggle).to_have_text("폰트 1개")
    expect(page.locator("#agent-fonts-empty")).to_be_hidden()
    assert (root / "fonts" / "index.json").is_file()

    page.locator("#agent-fonts-input").set_input_files(_files("broken.ttf", b"\0\1\0\0" + bytes(range(256)) * 4))
    expect(status).to_have_text("폰트 파일이 손상됐거나 읽을 수 없습니다")
    expect(status).to_have_class("agent-fonts__status agent-fonts__status--error")
    page.locator("#agent-fonts-input").set_input_files(_files("notes.txt", b"hello", "text/plain"))
    expect(status).to_have_text("TTF, OTF, WOFF2 파일만 올릴 수 있습니다")
    expect(rows).to_have_count(1)

    page.locator("[data-lang-toggle]").click()
    expect(status).to_have_text("Only TTF, OTF and WOFF2 files can be uploaded")
    expect(toggle).to_have_text("Fonts · 1")
    expect(page.locator("#agent-fonts-note")).to_have_text("Used from the next analysis or text edit")
    page.locator("#agent-fonts-input").set_input_files(_files("copy.ttf", _ttf()))
    expect(status).to_have_text("Brand Wide is already uploaded")
    expect(status).to_have_class("agent-fonts__status")
    expect(rows).to_have_count(1)

    page.reload()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(rows).to_have_count(1)
    expect(toggle).to_have_text("Fonts · 1")
    expect(status).to_be_hidden()


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844)])
def test_fonts_disclosure_fits_the_viewport(agent, width, height):
    from playwright.sync_api import expect
    page, _ = agent
    page.set_viewport_size({"width": width, "height": height})
    page.locator("#agent-fonts-toggle").click()
    upload = page.locator("#agent-fonts-upload")
    expect(upload).to_be_visible()
    upload.scroll_into_view_if_needed()
    box = upload.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= width and box["height"] >= 32
    assert page.evaluate("document.documentElement.scrollWidth") <= width


def test_render_card_names_a_substituted_uploaded_font(agent):
    """R47: a preview plan drawn with a stand-in for a missing uploaded font says so on the render card (ko/en)."""
    from playwright.sync_api import expect
    page, _ = agent
    plan = {"id": "plan1", "backend": "native", "mode": "preview", "version_id": "v1",
            "artifact_contract": {"outputs": ["mp4"]}}
    payload = {"plan": plan, "status": "done", "state": {"status": "approved", "revision": 1}, "artifacts": [],
               "warnings": [{"kind": "font_substituted", "element": "t1", "family": "Ghost Brand", "used": "Inter"},
                            {"kind": "font_file_missing", "element": "t2", "family": "Brand Wide",
                             "file": "assets/font-0123456789abcdef.ttf"}]}
    page.route("**/api/render-plans?*", lambda route: route.fulfill(json={"plans": [payload]}))
    page.reload()
    items = page.locator("#render-warnings li")
    expect(items).to_have_text(["올린 폰트가 없어 다른 폰트로 그렸습니다: Ghost Brand → Inter",
                                "장면에 폰트 파일이 없어 프로젝트에 올린 폰트를 썼습니다: Brand Wide"])
    page.locator("[data-lang-toggle]").click()
    expect(items).to_have_text(["Uploaded font Ghost Brand is missing; drawn with Inter",
                                "The Brand Wide font file is not in the scene; used the project's uploaded font"])
    payload["warnings"] = []
    page.reload()
    expect(page.locator("#render-warnings")).to_be_hidden()
