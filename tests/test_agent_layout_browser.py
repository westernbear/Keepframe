"""Preview priority and progressive disclosure on the real agent page."""

import time
from types import SimpleNamespace

import numpy as np
import pytest

from keepframe.ir.store import init_project, scene_dir
from keepframe.ir.synth import make_synthetic_scene
from keepframe.session.store import append_turn
from tests.test_ae_api import INFO
from tests.test_web_server import start

pytestmark = pytest.mark.browser


def seed_agent(workspace):
    root = workspace / "p1"
    scene = make_synthetic_scene(root / "scenes" / "s1", seed=7, n_elements=53,
                                 frames=24, size=(960, 540)).model_copy(update={"id": "s1"})
    init_project(root, {"file": "source.mp4"}, scene)
    stages = scene_dir(root, "s1") / "stages"
    stages.mkdir()
    from PIL import Image, ImageDraw
    source = Image.new("RGB", scene.size, "#101418")
    drawing = ImageDraw.Draw(source)
    drawing.rounded_rectangle((120, 100, 840, 440), radius=30, fill="#0099ff")
    drawing.text((180, 230), "Keepframe", fill="white", font_size=64)
    image = np.asarray(source)
    np.save(stages / "frames.npy", np.stack([image] * scene.frames))
    for ok, message in [(False, "Font missing\nChoose an installed font"), (True, "Title updated")]:
        turn = {"status": "done", "reply": message,
                "tool_calls": [{"name": "edit", "arguments": {"text": "Keepframe"}}],
                "results": [{"ok": ok, "message": message, "payload": {"verify": {
                    "passed": True, "keep_total": 5543, "keep_failed": 0, "layer_max_err_px": .03}}}]}
        append_turn(root, "s1", "Update the title", SimpleNamespace(to_json=lambda: turn))
    server = start(workspace)
    devices = server.ae_routes.devices
    code, _ = devices.create_code()
    device, _ = devices.pair(code, INFO | {"ae_version": "26.5.0", "panel_build": "124-panel",
                                         "host_build": "124-panel"})
    devices.touch(device, {"project_name": "Example.aep", "project_saved": True})
    jobs = server.ae_routes.jobs
    for i in range(5):
        job = jobs.enqueue(device, "sync", "p1", "s1", "v1", now=time.time() - 10 + i)
        jobs.next(device, wait=0)
        jobs.finish(job.id, True, {"applied": True, "created": ["e1", "e2", "e3"],
                                   "updated": ["e4", "e5"], "deleted": [],
                                   "warnings": [f"Review the font for e{n}" for n in range(30)]})
    return server, device


@pytest.fixture
def agent_page(tmp_path):
    from playwright.sync_api import sync_playwright, expect
    server, device = seed_agent(tmp_path)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_default_timeout(3000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/agent?project=p1&scene=s1")
            expect(page.locator("#agent-orig")).to_have_js_property("naturalWidth", 960)
            expect(page.locator(".toolcall")).to_have_count(2)
            yield page, server, device
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("width,height", [(1440, 900), (1920, 1080), (390, 844)])
def test_preview_size_transport_and_scroll(agent_page, width, height):
    page, _, _ = agent_page
    page.set_viewport_size({"width": width, "height": height})
    dimensions = page.locator(".agent-viewer").evaluate("""viewer => {
      const panes = viewer.querySelector('.compare-panes').getBoundingClientRect();
      const transport = viewer.querySelector('.agent-transport').getBoundingClientRect();
      return {height: viewer.clientHeight, overflow: getComputedStyle(viewer).overflowY,
        gap: transport.top - panes.bottom,
        images: [...viewer.querySelectorAll('.compare-pane__body img')].map(img => {
          const r = img.getBoundingClientRect();
          return {height:r.height, width:r.width, available:img.parentElement.clientWidth};
        })};
    }""")
    assert dimensions["overflow"] == "auto"
    assert 0 <= dimensions["gap"] <= 24
    for image in dimensions["images"]:
        assert image["height"] >= min(.4 * dimensions["height"], image["available"] * 9 / 16) - 2
        assert abs(image["width"] / image["height"] - 16 / 9) < .02
    page.locator("#ae-install summary").click()
    page.locator("#ae-details > summary").click()
    page.locator("#agent-elements-toggle").click()
    page.locator(".agent-viewer").evaluate("el => el.scrollTop = el.scrollHeight")
    assert page.locator(".agent-viewer").evaluate("el => el.scrollTop > 0")
    page.locator("#agent-elements-toggle").click()


def test_render_card_only_applicable_information(agent_page):
    from playwright.sync_api import expect
    page, _, _ = agent_page
    card = page.locator("#render-card")
    expect(card).not_to_contain_text("—")
    for selector in ["#render-status", "#render-plan-selector", "#render-outputs", "#render-reason", "#render-approve"]:
        expect(page.locator(selector)).to_be_hidden()
    expect(page.locator("#render-create")).to_be_visible()
    plan = {"id": "plan1", "backend": "native", "mode": "preview", "version_id": "v1",
            "artifact_contract": {"outputs": ["mp4"]}}
    payload = {"plan": plan, "status": "awaiting_approval", "state": {"status": "awaiting_approval", "revision": 1}}
    page.route("**/api/render-plans?*", lambda route: route.fulfill(json={"plans": [payload]}))
    page.reload()
    expect(page.locator("#render-status")).to_contain_text("v1")
    expect(page.locator("#render-plan-selector")).to_be_hidden()
    expect(page.locator("#render-outputs")).to_have_text("mp4")
    expect(page.locator("#render-approve")).to_be_visible()
    payload.update(status="failed", error="Render unavailable", state={"status": "failed", "revision": 2})
    page.reload()
    expect(page.locator("#render-reason")).to_have_text("Render unavailable")
    expect(page.locator("#render-approve")).to_be_hidden()
    page.route("**/api/render-plans?*", lambda route: route.fulfill(json={"plans": [payload, {
        **payload, "plan": {**plan, "id": "plan2"}}]}))
    page.reload()
    expect(page.locator("#render-plan-selector")).to_be_visible()
    expect(page.locator("#render-plan-selector option")).to_have_count(2)


def test_ae_summary_and_details(agent_page):
    from playwright.sync_api import expect
    page, server, device = agent_page
    expect(page.locator("#ae-status")).to_have_text("AE 26.5 연결됨 · Example.aep")
    page.locator("[data-lang-toggle]").click()
    expect(page.locator("#ae-status")).to_have_text("AE 26.5 connected · Example.aep")
    page.locator("[data-lang-toggle]").click()
    expect(page.locator("#ae-synced")).to_have_text("AE에 v1이 있습니다. 3개 생성, 2개 수정")
    visible = page.locator("#ae-card").inner_text()
    assert "Windows" not in visible and "124-panel" not in visible and "0개" not in visible
    expect(page.locator("#ae-connect")).to_be_hidden()
    page.locator("#ae-install summary").click()
    expect(page.locator("#ae-connect")).to_be_visible()
    page.locator("#ae-details > summary").click()
    expect(page.locator("#ae-devices")).to_contain_text("124-panel")
    expect(page.locator("#ae-jobs > li")).to_have_count(4)
    expect(page.locator("#ae-jobs ul, #ae-jobs p")).to_have_count(0)
    expect(page.locator("#ae-warnings-details")).not_to_have_attribute("open", "")
    expect(page.locator("#ae-warnings-summary")).to_have_text("경고 30개")
    expect(page.locator("#ae-warnings li:visible")).to_have_count(0)
    expect(page.locator("#ae-warnings li")).to_have_count(30)
    page.locator("#ae-warnings-summary").click()
    expect(page.locator("#ae-warnings li:visible")).to_have_count(30)
    assert "Windows" not in page.locator("#ae-devices").inner_text()
    server.ae_routes.devices.update_info(device, INFO | {"panel_build": "125-panel", "host_build": "124-host"})
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.locator("#ae-details > summary").click()
    expect(page.locator("#ae-build-warning")).to_have_text("AE를 완전히 종료했다가 다시 여세요")


def test_tool_call_summary_and_no_repeated_reply(agent_page, tmp_path):
    from playwright.sync_api import expect
    page, _, _ = agent_page
    applied = page.locator(".toolcall").last
    failed = page.locator(".toolcall").first
    expect(applied).to_contain_text("편집 적용됨")
    expect(page.locator(".msg--agent .msg__body")).to_have_text([
        "Font missing\nChoose an installed font", "Title updated",
    ])
    for reply in page.locator(".msg--agent .msg__body").all():
        expect(reply).to_be_visible()
    expect(failed).not_to_contain_text("Choose an installed font")
    expect(applied).not_to_contain_text("Title updated")
    expect(applied.locator(".toolcall__args")).to_be_hidden()
    applied.get_by_text("자세히", exact=True).click()
    expect(applied.locator(".toolcall__args")).to_contain_text('"text":"Keepframe"')
    expect(applied.locator(".toolcall__msg")).to_have_count(0)
    failed.get_by_text("자세히", exact=True).click()
    expect(failed.locator(".toolcall__msg")).to_have_count(0)
    expect(failed).not_to_contain_text("Font missing")
    long_error = "Missing font " * 30
    turn = {"status": "done", "reply": long_error,
            "tool_calls": [{"name": "edit", "arguments": {}}],
            "results": [{"ok": False, "message": long_error}]}
    append_turn(tmp_path / "p1", "s1", "Change font", SimpleNamespace(to_json=lambda: turn))
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload()
    expect(page.locator(".toolcall")).to_have_count(3)
    latest = page.locator(".toolcall").last
    assert latest.locator(".toolcall__head").bounding_box()["height"] <= 48
    latest.get_by_text("자세히", exact=True).click()
    expect(latest.locator(".toolcall__msg")).to_have_count(0)
    expect(page.locator(".msg--agent .msg__body").last).to_have_text(long_error)


@pytest.mark.parametrize("message,reply,has_details", [
    ("  Title updated  ", "Title updated", False),
    ("Title updated", "Title updated\nReady to preview", False),
    ("Font missing\nInstalled fonts: Inter, Arial", "Choose an installed font", True),
])
def test_tool_details_only_add_information(agent_page, message, reply, has_details):
    from playwright.sync_api import expect
    page, _, _ = agent_page
    turn = {"status": "done", "reply": reply,
            "tool_calls": [{"name": "edit", "arguments": {"text": "Keepframe"}}],
            "results": [{"ok": False, "message": message}]}
    page.route("**/api/agent?*", lambda route: route.fulfill(json={"turns": [{"user": "Edit", "turn": turn}]}))
    page.reload()
    tool = page.locator(".toolcall")
    expect(page.locator(".msg--agent .msg__body")).to_have_text(reply)
    expect(page.locator(".msg--agent .msg__body")).to_be_visible()
    expect(tool.locator(".toolcall__msg")).to_be_hidden()
    tool.get_by_text("자세히", exact=True).click()
    if has_details:
        expect(tool.locator(".toolcall__msg")).to_have_text(message)
    else:
        expect(tool.locator(".toolcall__msg")).to_have_count(0)


def test_tool_line_has_only_localized_status(agent_page):
    from playwright.sync_api import expect
    page, _, _ = agent_page
    expect(page.locator(".toolcall__head")).to_have_text(["편집 실패", "편집 적용됨"])
    page.locator("[data-lang-toggle]").click()
    expect(page.locator(".toolcall__head")).to_have_text(["Edit failed", "Edit applied"])


def test_tool_status_names_each_action(agent_page):
    from playwright.sync_api import expect
    page, _, _ = agent_page
    turn = {"status": "done", "reply": "Ready to preview",
            "tool_calls": [{"name": name, "arguments": {}} for name in ["render", "set_keep", "verify"]],
            "results": [{"ok": True}, {"ok": False}, {"ok": True}]}
    page.route("**/api/agent?*", lambda route: route.fulfill(json={"turns": [{"user": "Check", "turn": turn}]}))
    page.reload()
    expect(page.locator(".toolcall__head")).to_have_text(["렌더 완료", "유지 지정 실패", "검증 완료"])
    page.locator("[data-lang-toggle]").click()
    expect(page.locator(".toolcall__head")).to_have_text(["Render completed", "Set keep failed", "Verify completed"])


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844)])
def test_composer_groups_tools_and_attach(agent_page, width, height):
    page, _, _ = agent_page
    page.set_viewport_size({"width": width, "height": height})
    tools = page.locator("#agent-tools-toggle").bounding_box()
    attach = page.locator("#agent-attach-btn").bounding_box()
    send = page.locator("#agent-send").bounding_box()
    assert 0 <= attach["x"] - (tools["x"] + tools["width"]) <= 12
    assert send["x"] - (attach["x"] + attach["width"]) > 12


@pytest.mark.parametrize("total,failed,ko,en", [
    (5543, 0, "검사 통과 · 유지 100%", "Checks passed · keep 100%"),
    (5543, 444, "검사 실패 · 유지 92%", "Checks failed · keep 92%"),
    (0, 0, "검사할 유지 조건 없음", "No keep rules to check"),
])
def test_verify_chip_hides_diagnostics(agent_page, total, failed, ko, en):
    from playwright.sync_api import expect
    page, _, _ = agent_page
    turn = {"status": "done", "tool_calls": [], "results": [{"ok": True, "payload": {"verify": {
        "passed": failed == 0, "keep_total": total, "keep_failed": failed, "layer_max_err_px": .03}}}]}
    page.route("**/api/agent?*", lambda route: route.fulfill(json={"turns": [{"user": "Check", "turn": turn}]}))
    page.reload()
    chip = page.locator(".verify-chip").last
    expect(chip).to_have_text(ko)
    expect(chip).to_have_attribute("title", f"{ko}. 유지 조건 {total}개. 최대 오차 0.03px")
    expect(chip).to_have_attribute("aria-label", chip.get_attribute("title"))
    page.locator("[data-lang-toggle]").click()
    expect(chip).to_have_text(en)
    expect(chip).to_have_attribute("aria-label", f"{en}. {total} keep rules. Maximum error 0.03px")


def test_composer_tools_keyboard_and_prompt(agent_page):
    from playwright.sync_api import expect
    page, _, _ = agent_page
    expect(page.locator(".agent-compose__context")).to_have_text("화면과 미리보기를 함께 보냅니다")
    expect(page.locator("#agent-send")).to_have_attribute("aria-keyshortcuts", "Control+Enter")
    tools = page.get_by_role("button", name="도구", exact=True)
    expect(tools).to_have_attribute("aria-haspopup", "menu")
    expect(page.locator("#agent-tools")).to_be_hidden()
    tools.click()
    expect(tools).to_have_attribute("aria-expanded", "true")
    expect(page.get_by_role("menuitem").first).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(page.get_by_role("menuitem").nth(1)).to_be_focused()
    page.keyboard.press("Escape")
    expect(tools).to_be_focused()
    tools.click()
    page.get_by_role("menuitem", name="재해석", exact=True).click()
    expect(page.locator("#agent-input")).to_have_value("문구를 바꿔줘")
    expect(page.locator("#agent-input")).to_be_focused()
    expect(page.locator("#agent-tools")).to_be_hidden()


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844)])
def test_tools_menu_items_inside_viewport_and_escape_focus(agent_page, width, height):
    from playwright.sync_api import expect
    page, _, _ = agent_page
    page.set_viewport_size({"width": width, "height": height})
    tools = page.locator("#agent-tools-toggle")
    tools.click()
    items = page.get_by_role("menuitem")
    expect(items).to_have_count(8)
    for item in items.all():
        expect(item).to_be_visible()
        assert item.evaluate("""item => {
          const r = item.getBoundingClientRect();
          const menu = item.parentElement.getBoundingClientRect();
          const insideViewport = r.top >= 0 && r.left >= 0 && r.bottom <= innerHeight && r.right <= innerWidth;
          const insideMenu = r.top >= menu.top && r.bottom <= menu.bottom;
          const unclipped = [r.top + 2, r.bottom - 2].every(y =>
            item.contains(document.elementFromPoint(r.left + r.width / 2, y)));
          return insideViewport && insideMenu && unclipped;
        }"""), item.inner_text()
    page.keyboard.press("Escape")
    expect(page.locator("#agent-tools")).to_be_hidden()
    expect(tools).to_have_attribute("aria-expanded", "false")
    expect(tools).to_be_focused()


def test_elements_initial_count_uses_saved_language(agent_page):
    from playwright.sync_api import expect
    page, _, _ = agent_page
    page.add_init_script("localStorage.setItem('keepframe.lang', 'en')")
    page.route("**/api/state?*", lambda route: route.abort())
    page.reload()
    expect(page.locator("html")).to_have_attribute("lang", "en")
    expect(page.locator("#agent-count")).to_have_text("0 elements")
    page.locator("[data-lang-toggle]").click()
    expect(page.locator("#agent-count")).to_have_text("요소 0개")


def test_elements_disclosure_count_and_persistence(agent_page):
    from playwright.sync_api import expect
    page, _, _ = agent_page
    toggle = page.locator("#agent-elements-toggle")
    expect(toggle).to_have_text("요소 54개")
    expect(page.locator("#agent-elements-list")).to_be_hidden()
    toggle.click()
    expect(page.locator("#agent-elements-list")).to_be_visible()
    expect(page.locator(".element-row")).to_have_count(54)
    page.reload()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(page.locator("#agent-elements-list")).to_be_visible()


def test_cards_persist_and_collapsed_ae_keeps_hand_edits(agent_page):
    from playwright.sync_api import expect
    page, server, device = agent_page
    jobs = server.ae_routes.jobs
    job = jobs.enqueue(device, "sync", "p1", "s1", "v1")
    jobs.next(device, wait=0)
    jobs.finish(job.id, True, {"applied": False, "hand_edited": ["kf:title"]})
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(page.locator("#ae-hand-edits")).to_be_visible()
    for prefix in ["render", "ae"]:
        toggle = page.locator(f"#{prefix}-card-toggle")
        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(page.locator(f"#{prefix}-card-body")).to_be_hidden()
    expect(page.locator("#ae-status")).to_be_visible()
    expect(page.locator("#ae-hand-message")).to_be_visible()
    page.locator("#ae-overwrite").click()
    expect(page.locator("#ae-overwrite-confirm")).to_be_visible()
    page.route("**/api/ae/send", lambda route: route.fulfill(status=503, json={"error": "Overwrite unavailable"}))
    page.locator("#ae-overwrite-yes").click()
    expect(page.locator("#ae-error")).to_be_visible()
    expect(page.locator("#ae-error")).to_have_text("Overwrite unavailable")
    page.reload()
    for prefix in ["render", "ae"]:
        expect(page.locator(f"#{prefix}-card-toggle")).to_have_attribute("aria-expanded", "false")
    expect(page.locator("#ae-overwrite")).to_be_visible()
