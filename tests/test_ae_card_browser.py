"""PYTEST_DONT_REWRITE: keep pairing credentials out of assertion output."""

import json
import re
import time
from pathlib import Path
from urllib.parse import quote

import pytest

from keepframe.ir.schema import Background, Scene
from keepframe.ir.store import init_project, scene_dir
from tests.test_ae_api import EXTENSION, INFO, json_request, poll
from tests.test_web_server import start

pytestmark = pytest.mark.browser


@pytest.fixture
def ae_page(tmp_path):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    import numpy as np

    scene = Scene(id="s1", size=(32, 18), fps=30, frames=2, background=Background(value="#ffffff"), elements=[])
    init_project(tmp_path / "p1", {"file": "source.mp4"}, scene)
    stages = scene_dir(tmp_path / "p1", "s1") / "stages"
    stages.mkdir()
    np.save(stages / "frames.npy", np.full((2, 18, 32, 3), 255, dtype=np.uint8))
    server = start(tmp_path)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"], chromium_sandbox=False)
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            page.set_default_timeout(6000)
            page.add_init_script("localStorage.removeItem('keepframe.lang')")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/agent?project=p1&scene=s1")
            yield page, server, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


def connect(page, server, info=INFO):
    from playwright.sync_api import expect

    page.locator("#ae-connect").click()
    expect(page.locator("#ae-code")).to_have_value(re.compile(r"KF-[A-Z0-9]{4}-[A-Z0-9]{4}"))
    code = page.locator("#ae-code").input_value()
    status, paired = json_request(server, "POST", "/api/ae/pair", {"code": code, "info": info}, headers=EXTENSION)
    assert status == 200
    headers = EXTENSION | {"Authorization": "Bearer " + paired["token"],
                           "X-Keepframe-Project": quote("Example.aep"), "X-Keepframe-Project-Saved": "1"}
    assert poll(server, headers)[0] == 204
    expect(page.locator("#ae-status")).to_contain_text("연결됨", timeout=6000)
    expect(page.locator("#ae-code")).to_have_value("")
    assert "KF-" not in page.locator("#ae-card").inner_text()
    return paired["device_id"], headers


@pytest.mark.parametrize("builds", [{}, {"host_build": "123-abc1234", "panel_build": "123-abc1234"},
                                    {"host_build": "123-oldsha", "panel_build": "124-newsha-dirty"}])
def test_ae_device_rows_show_panel_and_host_builds_in_both_languages(ae_page, builds):
    from playwright.sync_api import expect

    page, server, errors = ae_page
    connect(page, server, INFO | builds)
    expect(page.locator("#ae-pair-message")).to_be_hidden()
    page.locator("#ae-details summary").click()
    expect(page.locator("#ae-devices")).to_contain_text("빌드 " + builds.get("panel_build", "알 수 없음"))
    expect(page.locator("#ae-devices")).to_contain_text("AE 스크립트 " + builds.get("host_build", "알 수 없음"))
    page.locator("[data-lang-toggle]").click()
    expect(page.locator("#ae-devices")).to_contain_text("build " + builds.get("panel_build", "unknown"))
    expect(page.locator("#ae-devices")).to_contain_text("AE script " + builds.get("host_build", "unknown"))
    assert not errors


@pytest.mark.parametrize("version", [None, ""])
def test_ae_device_without_version_still_paints(ae_page, version):
    from playwright.sync_api import expect

    page, _, errors = ae_page
    device = {"id": "legacy", "connected": True, "last_seen": time.time(),
              "created": time.time(), "project_name": "Example.aep"}
    if version is not None:
        device["ae_version"] = version
    state = {"devices": [device], "jobs": [], "last_synced": {}, "progress": {}}
    page.route("**/api/ae/state?*", lambda route: route.fulfill(json=state))
    page.reload()
    expect(page.locator("#ae-status")).to_have_text("AE 연결됨 · Example.aep")
    expect(page.locator("#ae-send")).to_be_enabled()
    expect(page.locator("#ae-synced")).to_have_text("아직 보내지 않았습니다")
    expect(page.locator("#ae-error")).to_be_hidden()
    page.locator("#ae-details summary").click()
    expect(page.locator("#ae-devices")).not_to_contain_text("undefined")
    page.locator("[data-lang-toggle]").click()
    expect(page.locator("#ae-status")).to_have_text("AE connected · Example.aep")
    assert not errors


def test_latest_ae_job_failure_is_red_success_is_muted(ae_page):
    from playwright.sync_api import expect

    page, server, errors = ae_page
    device, _ = connect(page, server)
    jobs = server.ae_routes.jobs
    failed = jobs.enqueue(device, "sync", "p1", "s1", "v1")
    jobs.next(device, wait=0)
    jobs.finish(failed.id, False, error="Font missing")
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    synced = page.locator("#ae-synced")
    expect(synced).to_contain_text("Font missing")
    expect(synced).to_have_class(re.compile(r"\bae-card__error\b"))
    assert synced.evaluate("el => getComputedStyle(el).color === getComputedStyle(document.querySelector('#ae-error')).color")
    success = jobs.enqueue(device, "sync", "p1", "s1", "v1")
    jobs.next(device, wait=0)
    jobs.finish(success.id, True, {"applied": True, "updated": ["title"]})
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(synced).to_have_text("AE에 v1이 있습니다. 1개 수정")
    expect(synced).not_to_have_class(re.compile(r"\bae-card__error\b"))
    assert synced.evaluate("el => getComputedStyle(el).color === getComputedStyle(document.querySelector('#ae-send-reason')).color")
    assert not errors


def assert_button_styles(page):
    """Check rendered contrast, including opacity, against a bare browser button."""
    measurements = page.locator("#ae-card").evaluate("""card => {
      const rgb = value => value.match(/[\\d.]+/g).map(Number);
      const blend = (front, back, alpha) => back.map((v, i) => front[i] * alpha + v * (1 - alpha));
      const luminance = color => color.map(v => v / 255).map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4)
        .reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
      const cardBackground = rgb(getComputedStyle(card).backgroundColor).slice(0, 3);
      const buttons = [...card.querySelectorAll('button')];
      const bare = document.createElement('button');
      document.body.append(bare);
      const result = [];
      for (const button of buttons) {
        const original = button.disabled;
        for (const disabled of [false, true]) {
          button.disabled = bare.disabled = disabled;
          const css = getComputedStyle(button), ua = getComputedStyle(bare);
          const background = rgb(css.backgroundColor), text = rgb(css.color), opacity = Number(css.opacity);
          const surface = blend(background, cardBackground, background[3] ?? 1);
          const paintedBackground = blend(surface, cardBackground, opacity);
          const paintedText = blend(blend(text, surface, text[3] ?? 1), cardBackground, opacity);
          const a = luminance(paintedText), b = luminance(paintedBackground);
          result.push({id: button.id || button.textContent, disabled, opacity,
            styled: css.backgroundColor !== ua.backgroundColor ||
              [css.borderStyle, css.borderWidth, css.borderColor].join() !== [ua.borderStyle, ua.borderWidth, ua.borderColor].join(),
            contrast: (Math.max(a, b) + .05) / (Math.min(a, b) + .05)});
        }
        button.disabled = original;
      }
      bare.remove();
      return result;
    }""")
    assert measurements
    for value in measurements:
        assert value["styled"], value
        assert value["contrast"] >= 4.5, value
        if value["disabled"]:
            assert value["opacity"] >= .7, value


def test_ae_pair_send_hand_edits_and_overwrite(ae_page, tmp_path):
    from playwright.sync_api import expect

    page, server, errors = ae_page
    expect(page.locator("#ae-status")).to_have_text("연결 안 됨")
    expect(page.get_by_role("link", name="확장 다운로드")).to_be_visible()
    expect(page.locator("#ae-send")).to_be_disabled()
    expect(page.locator("#ae-install")).to_have_attribute("open", "")
    assert_button_styles(page)
    device, headers = connect(page, server)
    expect(page.locator("#ae-status")).to_have_text("AE 25.0 연결됨 · Example.aep")
    expect(page.locator("#ae-send")).to_be_enabled()
    expect(page.locator("#ae-install")).not_to_have_attribute("open", "")
    page.locator("#ae-send").click()
    expect(page.locator("#ae-synced")).to_contain_text("대기 중")
    expect(page.locator("#ae-jobs > li")).to_have_count(0)
    status, claimed = poll(server, headers)
    assert status == 200 and claimed["job"]["version"] == "v1"
    job = claimed["job"]
    assert job["device"] == device and job["params"] == {"force": False}
    spec, host_path, actual = host_card_result(tmp_path, hand_edits=True)
    status, _ = json_request(server, "POST", f'/api/ae/jobs/{job["id"]}/result',
                             {"ok": True, "result": actual}, headers=headers)
    assert status == 200
    expect(page.locator("#ae-hand-edits")).to_be_visible(timeout=6000)
    expect(page.locator("#ae-hand-edits")).to_contain_text("2개 레이어")
    for eid in ("kf:title", "kf:logo"):
        expect(page.locator("#ae-hand-edits")).to_contain_text(eid)
    assert_button_styles(page)
    page.locator("#ae-overwrite").click()
    expect(page.locator("#ae-overwrite-confirm")).to_be_visible()
    expect(page.locator("#ae-overwrite-yes")).to_be_focused()
    assert_button_styles(page)
    assert len(server.ae_routes.jobs.state("p1", "s1")["jobs"]) == 1
    with page.expect_request("**/api/ae/send") as sent:
        page.locator("#ae-overwrite-confirm").get_by_role("button", name="예", exact=True).click()
    assert sent.value.post_data_json == {"project": "p1", "scene": "s1", "version": "v1", "force": True}
    expect(page.locator("#ae-hand-edits")).to_be_hidden()
    status, claimed = poll(server, headers)
    assert status == 200 and claimed["job"]["params"] == {"force": True}
    job = claimed["job"]
    spec, host_path, actual = host_card_result(tmp_path / "overwrite", warning="<img src=x onerror=alert(1)>")
    assert len(actual["created"]) == 55
    status, _ = json_request(server, "POST", f'/api/ae/jobs/{job["id"]}/result',
                             {"ok": True, "result": actual}, headers=headers)
    assert status == 200
    expect(page.locator("#ae-synced")).to_have_text("AE에 v1이 있습니다. 55개 생성", timeout=6000)
    page.locator("#ae-details summary").click()
    expect(page.locator("#ae-warnings")).to_contain_text("<img src=x onerror=alert(1)>")
    assert page.locator("#ae-warnings img").count() == 0
    page.locator("[data-lang-toggle]").click()
    expect(page.locator("#ae-synced")).to_have_text("AE has v1. 55 created")
    page.locator("#ae-send").click()
    expect(page.locator("#ae-synced")).to_contain_text("Queued")
    status, claimed = poll(server, headers)
    assert status == 200
    from tests.test_ae_host_sync import sync
    actual = sync(host_path, spec)["value"]
    assert actual["created"] == actual["updated"] == actual["deleted"] == []
    assert actual["unchanged"] == 55
    status, _ = json_request(server, "POST", f'/api/ae/jobs/{claimed["job"]["id"]}/result',
                             {"ok": True, "result": actual}, headers=headers)
    assert status == 200
    expect(page.locator("#ae-synced")).to_have_text("AE has v1. No changes")
    screenshot = Path("/tmp/keepframe-task10-ae-card.png")
    page.locator("#ae-card").screenshot(path=str(screenshot))
    assert not errors


def test_ae_expiry_copy_errors_and_disconnect(ae_page):
    from playwright.sync_api import expect

    page, server, errors = ae_page
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    command = r'& "C:\Program Files\Common Files\Adobe\Adobe Desktop Common\RemoteComponents\UPI\UnifiedPluginInstallerAgent\UnifiedPluginInstallerAgent.exe" /install "$env:USERPROFILE\Downloads\keepframe.zxp"'
    expect(page.locator("#ae-install-command")).to_have_value(command)
    expect(page.locator("#ae-install-path-hint")).to_have_text("PowerShell. 다른 곳에 저장했다면 마지막 경로를 바꾸세요.")
    page.locator("#ae-install-copy").click()
    expect(page.locator("#ae-copy-status")).to_have_text("복사됨")
    assert page.evaluate("navigator.clipboard.readText()") == command
    device, _ = connect(page, server)
    # Simulate the device going offline after the last state snapshot: send's 409 must be visible.
    server.ae_routes.devices.seen(device, now=0)
    page.locator("#ae-send").click()
    expect(page.locator("#ae-error")).to_have_text("no connected After Effects")
    page.locator("#ae-details summary").click()
    page.locator("#ae-devices").get_by_role("button", name="연결 해제", exact=True).click()
    expect(page.locator("#ae-devices")).to_contain_text("이 AE의 연결을 해제할까요?")
    expect(page.locator("#ae-devices").get_by_role("button", name="예", exact=True)).to_be_focused()
    assert_button_styles(page)
    page.locator("#ae-devices").get_by_role("button", name="아니요", exact=True).click()
    assert len(server.ae_routes.devices.list()) == 1
    page.locator("#ae-devices").get_by_role("button", name="연결 해제", exact=True).click()
    page.locator("#ae-devices").get_by_role("button", name="예", exact=True).click()
    expect(page.locator("#ae-devices")).to_have_text("")
    expect(page.locator("#ae-install")).to_have_attribute("open", "")
    page.route("**/api/ae/codes", lambda route: route.fulfill(
        content_type="application/json", body=json.dumps({"code": "KF-TEST-TEST", "expires_at": time.time() + 1})))
    page.locator("#ae-connect").click()
    expect(page.locator("#ae-code")).to_have_value("KF-TEST-TEST")
    expect(page.locator("#ae-pair-message")).to_have_text("코드 만료 — AE 연결을 다시 누르세요")
    expect(page.locator("#ae-code")).to_have_value("")
    expect(page.locator("#ae-code")).to_be_hidden()
    page.locator("[data-lang-toggle]").click()
    expect(page.locator("#ae-status")).to_have_text("Not connected")
    expect(page.locator("#ae-pair-message")).to_have_text("Code expired — press Connect AE again")
    expect(page.get_by_role("link", name="Download extension")).to_be_visible()
    expect(page.locator("#ae-install-path-hint")).to_have_text("PowerShell. If you saved the file elsewhere, change the last path.")
    assert not errors


@pytest.mark.parametrize("secure,clipboard_available,copy_result", [
    (False, False, True),
    (False, True, True),
    (True, True, True),
    (True, True, False),
    (True, True, "throw"),
], ids=["http-no-clipboard", "http-with-clipboard", "clipboard-rejected", "both-fail", "both-throw"])
def test_ae_copy_fallback(ae_page, secure, clipboard_available, copy_result):
    from playwright.sync_api import expect

    page, _, errors = ae_page
    page.add_init_script("""
      const [secure, clipboardAvailable, copyResult] = __SETUP__;
      Object.defineProperty(window, 'isSecureContext', {value: secure});
      window.clipboardCalls = 0;
      Object.defineProperty(navigator, 'clipboard', {value: clipboardAvailable ? {
        writeText: async () => {
          window.clipboardCalls++;
          throw new Error('Clipboard denied');
        },
      } : undefined});
      window.copyAttempts = [];
      document.execCommand = command => {
        const field = document.activeElement;
        window.copyAttempts.push({command,
          text: field.value.slice(field.selectionStart, field.selectionEnd),
          readonly: field.readOnly, textarea: field.tagName === 'TEXTAREA',
          offscreen: field.getBoundingClientRect().right < 0});
        if (copyResult === 'throw') throw new Error('Copy denied');
        return copyResult;
      };
    """.replace("__SETUP__", json.dumps([secure, clipboard_available, copy_result])))
    page.reload()
    assert page.evaluate("window.isSecureContext") is secure
    assert page.evaluate("Boolean(navigator.clipboard)") is clipboard_available
    textarea_count = page.locator("textarea").count()
    page.locator("#ae-connect").click()
    expect(page.locator("#ae-code-field")).to_be_visible()
    copied = copy_result is True
    ko = "복사됨" if copied else "복사 실패 — 텍스트가 선택되었습니다. Ctrl+C를 누르세요"
    en = "Copied" if copied else "Copy failed — the text is selected; press Ctrl+C"
    for count, (button_id, field_id) in enumerate([
        ("ae-install-copy", "ae-install-command"), ("ae-code-copy", "ae-code"),
    ], start=1):
        button, field = page.locator(f"#{button_id}"), page.locator(f"#{field_id}")
        button.click()
        expect(page.locator("#ae-copy-status")).to_have_text(ko)
        expect(button if copied else field).to_be_focused()
        if not copied:
            assert field.evaluate("field => field.value.length > 0 && field.selectionStart === 0 && field.selectionEnd === field.value.length")
        # Compare inside the page so pairing credentials never enter assertion output.
        assert page.evaluate("""id => {
          const attempt = window.copyAttempts.at(-1);
          return attempt.command === 'copy' && attempt.text === document.getElementById(id).value
            && attempt.readonly && attempt.textarea && attempt.offscreen;
        }""", field_id)
        assert page.evaluate("window.copyAttempts.length") == count
        assert page.evaluate("window.clipboardCalls") == (count if secure and clipboard_available else 0)
        expect(page.locator("textarea")).to_have_count(textarea_count)
        expect(page.locator("#ae-error")).to_be_hidden()
        page.locator("[data-lang-toggle]").click()
        expect(page.locator("#ae-copy-status")).to_have_text(en)
        page.locator("[data-lang-toggle]").click()
        expect(page.locator("#ae-copy-status")).to_have_text(ko)
    assert not errors


def test_ae_disabled_actions_explain_connection(ae_page):
    from playwright.sync_api import expect

    page, server, errors = ae_page
    reason = "AE가 연결되지 않았습니다 — After Effects에서 Keepframe 패널을 여세요"
    expect(page.locator("#ae-send-reason")).to_have_text(reason)
    device, _ = connect(page, server)
    jobs = server.ae_routes.jobs
    jobs.enqueue(device, "sync", "p1", "s1", "v1")
    jobs.finish(jobs.next(device, wait=0).id, True, {"applied": False, "hand_edited": ["kf:title", "kf:logo"]})
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(page.locator("#ae-hand-edits")).to_be_visible()
    expect(page.locator("#ae-send-reason")).to_be_hidden()
    expect(page.locator("#ae-overwrite-reason")).to_be_hidden()
    expect(page.locator("#ae-overwrite")).to_be_enabled()
    server.ae_routes.devices.seen(device, now=0)
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(page.locator("#ae-send")).to_be_disabled()
    expect(page.locator("#ae-overwrite")).to_be_disabled()
    for action in ["send", "overwrite"]:
        expect(page.locator(f"#ae-{action}-reason")).to_be_visible()
        expect(page.locator(f"#ae-{action}-reason")).to_have_text(reason)
        expect(page.locator(f"#ae-{action}")).to_have_attribute("aria-describedby", f"ae-{action}-reason")
    assert_button_styles(page)
    page.locator("[data-lang-toggle]").click()
    for action in ["send", "overwrite"]:
        expect(page.locator(f"#ae-{action}-reason")).to_have_text(
            "AE is not connected — open the Keepframe panel in After Effects")
    assert not errors


@pytest.mark.parametrize("route_name,active", [("state", False), ("state", True), ("devices", False)])
def test_ae_poll_error_backoff_and_recovery(ae_page, route_name, active):
    from playwright.sync_api import expect

    page, _, errors = ae_page
    page.clock.install()
    page.clock.pause_at(page.evaluate("Date.now() / 1000"))
    mode = {"fail": False}
    requests = []
    pattern = "**/api/ae/state?*" if route_name == "state" else "**/api/ae/devices"
    data = {"devices": [], "jobs": [{"id": "pending", "kind": "sync", "state": "queued", "version": "v1",
                                    "created": time.time()}] if active else [], "last_synced": {}, "progress": {}}

    def respond(route):
        requests.append(page.evaluate("Date.now()"))
        route.fulfill(status=503 if mode["fail"] else 200, content_type="application/json",
                      body=json.dumps({"error": "Temporary AE poll failure"} if mode["fail"] else data))

    page.route(pattern, respond)
    # Isolate device retries from successful state polls when pairing is in progress.
    if route_name == "devices":
        page.route("**/api/ae/state?*", lambda route: route.fulfill(
            status=503 if mode["fail"] else 200, content_type="application/json",
            body=json.dumps({"error": "Temporary AE poll failure"} if mode["fail"] else data)))
    page.reload()
    expect(page.locator("#agent-total")).to_have_text("2")
    if route_name == "devices":
        page.locator("#ae-connect").click()
        expect(page.locator("#ae-code-field")).to_be_visible()
    page.wait_for_timeout(100)
    base = 2000 if active or route_name == "devices" else 15000
    mode["fail"] = True

    def advance_poll(delay, failing):
        before = len(requests)
        page.clock.fast_forward(delay - 1)
        page.wait_for_timeout(50)
        assert len(requests) == before
        page.clock.fast_forward(1)
        page.wait_for_timeout(100)
        assert len(requests) == before + 1
        expect(page.locator("#ae-error")).to_be_visible() if failing else expect(page.locator("#ae-error")).to_be_hidden()
        if not failing:
            expect(page.locator("#ae-error")).to_have_text("")

    advance_poll(base, True)
    delay = base
    for _ in range(6):
        delay = min(delay * 2, 60000)
        advance_poll(delay, True)
    mode["fail"] = False
    advance_poll(60000, False)
    advance_poll(base, False)
    assert not errors


def test_ae_polling_pauses_when_hidden_and_resumes(ae_page):
    from playwright.sync_api import expect

    page, server, errors = ae_page
    counts = {"state": 0, "devices": 0}

    def count_request(request):
        for name in counts:
            if f"/api/ae/{name}" in request.url:
                counts[name] += 1

    page.on("request", count_request)
    page.clock.install()
    page.clock.pause_at(page.evaluate("Date.now() / 1000"))
    page.reload()
    expect(page.locator("#agent-total")).to_have_text("2")
    expect(page.locator("#ae-send")).to_be_disabled()
    # Idle state polls after 15 s, never at the 2 s active interval.
    before = counts["state"]
    page.clock.fast_forward(2000)
    page.wait_for_timeout(100)
    assert counts["state"] == before
    with page.expect_request("**/api/ae/state?*"):
        page.clock.fast_forward(13000)
    page.wait_for_timeout(100)
    page.locator("#ae-connect").click()
    expect(page.locator("#ae-countdown")).to_have_text(re.compile(r"\d{2}:\d{2}"))
    with page.expect_request("**/api/ae/devices"):
        page.clock.fast_forward(2000)
    page.evaluate("Object.defineProperty(document, 'hidden', {configurable: true, value: true}); document.dispatchEvent(new Event('visibilitychange'))")
    expect(page.locator("#ae-code")).to_have_value("")
    before = dict(counts)
    page.clock.fast_forward(30000)
    page.wait_for_timeout(100)
    assert counts == before
    with page.expect_request("**/api/ae/state?*"):
        page.evaluate("Object.defineProperty(document, 'hidden', {configurable: true, value: false}); document.dispatchEvent(new Event('visibilitychange'))")
    expect(page.locator("#ae-code")).to_have_value(re.compile(r"KF-"))
    # Force a queued sync outside the five displayed jobs to keep the active interval.
    code = page.locator("#ae-code").input_value()
    status, paired = json_request(server, "POST", "/api/ae/pair", {"code": code, "info": INFO}, headers=EXTENSION)
    assert status == 200
    headers = EXTENSION | {"Authorization": "Bearer " + paired["token"]}
    assert poll(server, headers)[0] == 204
    jobs = server.ae_routes.jobs
    jobs.enqueue(paired["device_id"], "sync", "p1", "s1", "v1")
    jobs.finish(jobs.next(paired["device_id"], wait=0).id, True, {"applied": True})
    for _ in range(6):
        jobs.enqueue(paired["device_id"], "package", "p1", "s1", "v1")
        jobs.finish(jobs.next(paired["device_id"], wait=0).id, True, {})
    jobs.enqueue(paired["device_id"], "sync", "p1", "s1", "v1", now=time.time() - 10)
    with page.expect_request("**/api/ae/devices"):
        page.clock.fast_forward(2000)
    expect(page.locator("#ae-code")).to_have_value("")
    expect(page.locator("#ae-jobs > li")).to_have_count(4)
    with page.expect_request("**/api/ae/state?*"):
        page.clock.fast_forward(2000)
    assert not errors


def host_card_result(root, *, hand_edits=False, interrupted=False, warning=None):
    from tests.test_ae_host_sync import spec_for, sync, read_state, save_state, layers, prop
    from tests.test_ae_spec import element

    root.mkdir(parents=True, exist_ok=True)
    spec, _ = spec_for(root, *[element(eid, kind="group") for eid in
                             ["title", "logo", *[f"e{i}" for i in range(52)]]])
    if warning:
        spec["warnings"].append(warning)
    path = root / "ae.json"
    actual = sync(path, spec)["value"]
    if hand_edits or interrupted:
        state = read_state(path)
        for eid in ("kf:title", "kf:logo"):
            prop(layers(state)[eid], "ADBE Rotate Z")["value"] = 25
        if interrupted:
            layers(state)["kf:title"]["comment"] = "keepframe:kf:title"
        save_state(path, state)
        actual = sync(path, spec)["value"]
    return spec, path, actual


def test_final_web_distinguishes_actual_interrupted_and_hand_edited_ids(ae_page, tmp_path):
    from playwright.sync_api import expect

    page, server, errors = ae_page
    device, headers = connect(page, server)
    page.locator("[data-lang-toggle]").click()
    _, _, actual = host_card_result(tmp_path, hand_edits=True, interrupted=True)
    assert actual["interrupted"] == ["kf:title"]
    assert set(actual["hand_edited"]) == {"kf:title", "kf:logo"}
    job = server.ae_routes.jobs.enqueue(device, "sync", "p1", "s1", "v1")
    assert poll(server, headers)[1]["job"]["id"] == job.id
    assert json_request(server, "POST", f'/api/ae/jobs/{job.id}/result',
                        {"ok": True, "result": actual}, headers=headers)[0] == 200
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(page.locator("#ae-hand-message")).to_contain_text(
        "a previous sync was interrupted — overwrite to finish it: kf:title")
    expect(page.locator("#ae-hand-message")).to_contain_text("1 layers were edited by hand in AE: kf:logo")
    expect(page.locator("#ae-hand-message")).not_to_contain_text("hand in AE: kf:title")
    # Keep a distinct hand-edit prompt while the interrupted sync moves into history.
    latest = server.ae_routes.jobs.enqueue(device, "sync", "p1", "s1", "v1")
    assert poll(server, headers)[1]["job"]["id"] == latest.id
    assert json_request(server, "POST", f'/api/ae/jobs/{latest.id}/result',
                        {"ok": True, "result": {"applied": False, "hand_edited": ["kf:logo"]}}, headers=headers)[0] == 200
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(page.locator("#ae-hand-message")).to_have_text("1 layers were edited by hand in AE: kf:logo")
    page.locator("#ae-details summary").click()
    expect(page.locator("#ae-jobs")).to_contain_text(
        "a previous sync was interrupted — overwrite to finish it: kf:title")
    expect(page.locator("#ae-jobs")).to_contain_text("1 layers were edited by hand in AE: kf:logo")
    expect(page.locator("#ae-jobs")).not_to_contain_text("hand in AE: kf:title")
    page.locator("[data-lang-toggle]").click()
    expect(page.locator("#ae-jobs")).to_contain_text("이전 동기화가 중단되었습니다 — 덮어써서 완료하세요: kf:title")
    expect(page.locator("#ae-hand-message")).to_have_text("AE에서 1개 레이어를 직접 수정했습니다: kf:logo")
    assert not errors
