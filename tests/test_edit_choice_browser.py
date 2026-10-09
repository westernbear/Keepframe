"""Task 12 on the agent page: a background colour request on a picture background offers tint / replace / cancel,
and cancel applies nothing (no version, no asset)."""
import hashlib

import cv2
import numpy as np
import pytest

from keepframe.ir.schema import Background, Canonical, Element, Keyframe, Scene, Track
from keepframe.ir.store import init_project, load_project, scene_dir
from keepframe.session.agent import SessionTurn
from keepframe.session.store import append_turn
from keepframe.session.tools import SessionContext, run_tool
from tests.test_web_server import start

pytestmark = pytest.mark.browser
PROMPT = "make the background navy"


def _seed(workspace):
    root = workspace / "p1"
    sd = root / "scenes" / "s1"
    (sd / "assets").mkdir(parents=True)
    ys, xs = np.mgrid[0:270, 0:480]
    plate = np.dstack([40 + xs * 0.3, 80 + ys * 0.4, 160 + 0 * xs]).clip(0, 255).astype(np.uint8)
    cv2.imwrite(str(sd / "assets" / "background.png"), plate[..., ::-1])
    el = Element(id="e1", kind="sprite", canonical=Canonical(width=40, height=30, color="#ffffff"), visible=(0, 5),
                 tracks={"x": Track(keys=[Keyframe(t=0, v=200)]), "y": Track(keys=[Keyframe(t=0, v=120)])})
    scene = Scene(id="s1", size=(480, 270), fps=30, frames=6, elements=[el],
                  background=Background(kind="image", value="assets/background.png", confidence=0.8))
    init_project(root, {"file": "source.mp4"}, scene)
    (sd / "stages").mkdir()
    np.save(sd / "stages" / "frames.npy", np.stack([plate] * scene.frames))
    args = {"prompt": PROMPT, "targets": [{"property": "background", "value": "#1a2a6c"}]}
    result = run_tool("edit", SessionContext(root, "s1"), args)
    assert result["ok"] and result["needs_confirm"]
    append_turn(root, "s1", PROMPT, SessionTurn(reply=result["message"], status="pending", needs_confirm=True,
                                                 tool_calls=[{"name": "edit", "arguments": args}], results=[result]))
    return root


def _assets(root):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((scene_dir(root, "s1") / "assets").iterdir())}


def test_agent_shows_three_choices_and_cancel_does_not_apply(tmp_path):
    from playwright.sync_api import expect, sync_playwright
    root = _seed(tmp_path)
    before = _assets(root)
    server = start(tmp_path)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_default_timeout(10000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/agent?project=p1&scene=s1")
            page.locator("#agent-log button.btn--primary").last.click()          # confirm the previewed edit
            choice = page.locator(".agent-choice")
            expect(choice).to_have_count(1)
            expect(choice.locator(".agent-choice__reason")).to_have_text(
                "배경이 그림·그라데이션·영상입니다. 색만 입힐까요, 단색으로 바꿀까요?")
            expect(choice.locator("label")).to_have_text(["색 입히기 (밝고 어두운 결 유지)", "단색으로 바꾸기", "취소"])
            expect(choice.locator("input[type=radio]")).to_have_count(3)
            expect(choice.locator("input[value=tint]")).to_be_checked()
            choice.locator("input[value=cancel]").check()
            choice.get_by_role("button", name="선택").click()
            expect(page.locator(".msg--agent .msg__body").last).to_have_text("취소했습니다. 바뀐 것은 없습니다.")
            expect(choice.get_by_role("button", name="선택")).to_be_disabled()
            expect(page.locator("#agent-banner")).to_be_hidden()
            assert [v.id for v in load_project(root).versions] == ["v1"] and _assets(root) == before

            page.locator("[data-lang-toggle]").click()
            page.reload()
            page.locator("#agent-log button.btn--primary").last.click()
            choice = page.locator(".agent-choice")
            expect(choice.locator(".agent-choice__reason")).to_have_text(
                "The background is a picture, gradient or video. Tint it, or replace it with a flat colour?")
            expect(choice.locator("label")).to_have_text(["Tint (keep its light and dark)", "Replace with a flat colour",
                                                          "Cancel"])
            choice.locator("input[value=cancel]").check()
            choice.get_by_role("button", name="Choose").click()
            expect(page.locator(".msg--agent .msg__body").last).to_have_text("Cancelled. Nothing was changed.")
            assert [v.id for v in load_project(root).versions] == ["v1"] and _assets(root) == before
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
