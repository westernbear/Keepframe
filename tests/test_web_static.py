import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESIGNS = ROOT / ".stitch" / "designs"
STATIC = ROOT / "keepframe" / "web" / "static"
SRC = ROOT / "frontend" / "src"
FORBIDDEN = (
    "cdn.tailwindcss.com",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "lh3.googleusercontent.com",
    "material-symbols",
)


def spa_src(*names):
    """SPA sources under frontend/src, joined."""
    return "\n".join((SRC / name).read_text(encoding="utf-8") for name in names)


def bundle():
    """The committed SPA entry (app/index.html) and the /static/ paths of the assets it links."""
    html = (STATIC / "app" / "index.html").read_text(encoding="utf-8")
    return html, re.findall(r"(?:src|href)=\"/static/([^\"?#]+)", html)


I18N = SRC / "i18n"


def strings(lang):
    """The SPA's dictionary (frontend/src/i18n/<lang>.json)."""
    return json.loads((I18N / f"{lang}.json").read_text(encoding="utf-8"))


def test_spa_dictionaries_have_the_same_keys():
    assert len(strings("ko")) > 400
    assert set(strings("ko")) == set(strings("en"))


def test_analyze_stitch_source_exists():
    html = (DESIGNS / "analyze.html").read_text(encoding="utf-8")
    assert "<html" in html.lower()
    assert (DESIGNS / "analyze.png").stat().st_size > 1000


def test_landing_stitch_source_exists():
    html = (DESIGNS / "landing.html").read_text(encoding="utf-8")
    assert "<html" in html.lower()
    assert "유지할 것과 바꿀 것을 지정하세요" in html
    assert (DESIGNS / "landing.png").stat().st_size > 1000


def test_spa_bundle_is_offline():
    html, assets = bundle()
    assert any(a.endswith(".js") for a in assets) and any(a.endswith(".css") for a in assets)
    for name, text in [("app/index.html", html), *((a, (STATIC / a).read_text(encoding="utf-8"))
                                                    for a in assets if a.endswith((".js", ".css")))]:
        for bad in FORBIDDEN:
            assert bad not in text, f"{name} still loads {bad}"


def test_pages_include_logo_and_favicon():
    assert (STATIC / "logo.png").stat().st_size > 100
    assert (STATIC / "icon.png").stat().st_size > 100
    assert 'href="/static/icon.png"' in bundle()[0]
    assert 'src="/static/logo.png"' in spa_src("components/app/app-header.tsx")


def test_ingest_gpu_status_is_not_hardcoded():
    page = spa_src("pages/ingest/index.tsx")
    assert "data-gpu" in page
    assert "fetchStatus()" in page
    assert "'/api/status'" in spa_src("lib/api.ts")


def test_i18n_has_ko_and_en_keys():
    assert "keepframe.lang" in (ROOT / "frontend/src/i18n/index.ts").read_text(encoding="utf-8")
    ko, en = strings("ko"), strings("en")
    for key in ("ingest.start", "review.keepSave", "review.approve", "review.openAgent", "review.editRun", "error.liveaction", "library.empty", "landing.headline"):
        assert ko[key] and en[key]


def test_font_candidate_copy_is_bilingual():
    ko, en = strings("ko"), strings("en")
    assert ko["review.fontCandidates"] == "폰트 후보" and en["review.fontCandidates"] == "Font candidates"
    assert ko["review.applyFont"] == "폰트 적용" and en["review.applyFont"] == "Apply font"


def test_i18n_keys_used_by_the_spa_exist_in_both_langs():
    # literal T('key') / Tf('key') calls; template keys are pinned by their own tests
    ko, en = strings("ko"), strings("en")
    used = set()
    for path in SRC.rglob("*.ts*"):
        if ".test." not in path.name:
            used.update(re.findall(r"""\bTf?\(\s*['"]([^'"]+)['"]""", path.read_text(encoding="utf-8")))
    assert len(used) > 200
    assert [k for k in sorted(used) if k not in ko or k not in en] == []


def test_api_prefetches_review_frames():
    src = spa_src("lib/playback.ts")
    assert "function createPreviewCache" in src
    assert "function createFrameTransport" in src
    assert "img.decode" in src
    assert "PREFETCH_AHEAD" in src
    assert "PREVIEW_CACHE_LIMIT" in src


def test_css_tokens_match_design():
    css = spa_src("styles.css")
    assert "#090909" in css and "#0099ff" in css and "#ffffff" in css


def test_hidden_wins_over_display():
    assert "[hidden] { display: none !important; }" in spa_src("styles.css")


def test_review_empty_project_has_library_exit():
    src = spa_src("pages/review/index.tsx")
    assert 'id="review-missing"' in src
    assert "is-empty" in src
    assert 'href="/library"' in src
    assert "review.backLibrary" in src


def test_landing_and_library_link_to_demo():
    # the library's per-status hrefs are in frontend/src/lib/library.test.ts
    landing = spa_src("pages/landing/index.tsx")
    library = spa_src("pages/library/index.tsx")
    assert 'href="/demo"' in landing and "nav.demo" in landing
    assert 'href="/demo"' in library
    assert "library.filter.export" not in library


def test_review_empty_copy_uses_the_exact_korean_guidance():
    assert "T('review.empty')" in spa_src("pages/review/inspector.tsx")
    assert strings("ko")["review.empty"] == "인식된 객체가 없습니다. 다른 프레임이나 분석 버전을 확인하세요."


def test_all_linked_runtime_assets_exist_locally():
    html, assets = bundle()
    for asset in assets:
        assert (STATIC / asset).is_file(), f"app/index.html links missing /static/{asset}"
        if asset.endswith(".css"):
            for ref in re.findall(r"url\(/static/([^)?#]+)", (STATIC / asset).read_text(encoding="utf-8")):
                assert (STATIC / ref).is_file(), f"{asset} links missing /static/{ref}"


def test_agent_keep_and_correction_copy_is_bilingual():
    ko, en = strings("ko"), strings("en")
    for key in ("agent.keepPresetPreview", "agent.keepOnPreview", "agent.keepOffPreview",
                "agent.keepExamplesPreview", "agent.keepChangeNote", "agent.keepPreviewChanged",
                "agent.correctionPreview", "agent.correctionInvalid", "agent.correctionElement",
                "agent.correctionText", "agent.correctionFont", "agent.correctionFontFamily",
                "agent.correctionFontWeight", "agent.correctionFontSize", "agent.correctionFrames",
                "agent.correctionFrame", "agent.correctionBbox", "agent.correctionMask",
                "agent.correctionMaskHidden", "agent.applied"):
        assert key in ko
        assert key in en
    assert ko["agent.keepChangeNote"] == "에이전트 keep 변경"
    assert en["agent.keepChangeNote"] == "Agent keep change"


def test_agent_previews_render_only_text():
    # model-supplied preview values reach the page as React text, never as markup (field picking is in Vitest)
    for name in ("pages/agent/index.tsx", "pages/agent/chat.tsx", "pages/agent/ui.tsx",
                 "lib/agent-corrections.ts", "lib/agent-chat.ts"):
        src = spa_src(name)
        assert "dangerouslySetInnerHTML" not in src and "innerHTML" not in src, name


def test_ae_card_static_contract():
    page = spa_src("pages/agent/index.tsx")
    assert page.index('className="compare-panes') < page.index('className="agent-transport') < page.index("<RenderCard") < page.index("<AeCard")
    card = spa_src("pages/agent/ae-card.tsx")
    assert 'id="ae-card" aria-labelledby="ae-card-title"' in card
    assert '<a href="/ae/keepframe.zxp" download>' in card
    command = r'& "C:\Program Files\Common Files\Adobe\Adobe Desktop Common\RemoteComponents\UPI\UnifiedPluginInstallerAgent\UnifiedPluginInstallerAgent.exe" /install "$env:USERPROFILE\Downloads\keepframe.zxp"'
    assert f"const INSTALL_COMMAND = '{command}'".replace("\\", "\\\\") in card
    assert 'aria-describedby="ae-install-path-hint"' in card
    assert "T('ae.installPathHint')" in card
    assert "data-ai-private" in card[card.index('id="ae-pairing"'):card.index('id="ae-devices"')]
    assert 'id="ae-status"' in card and 'id="ae-jobs"' in card
    assert page.count("<AeCard ") == 1
    refresh = page[page.index("async function refreshAfterEdit("):page.index("const ctx = ")]
    assert "aeRefresh.current()" in refresh
    ae = spa_src("pages/agent/ae-core.ts", "lib/ae.ts")
    assert set(re.findall(r"/api/ae/[a-z_/-]+", ae)) == {
        "/api/ae/codes", "/api/ae/devices", "/api/ae/devices/", "/api/ae/send", "/api/ae/state",
        "/api/ae/verify", "/api/ae/verify-image",
    }
    assert "visibilitychange" in ae
    for src in (ae, card):
        assert "console." not in src and "innerHTML" not in src and "window.confirm" not in src
    ko, en = strings("ko"), strings("en")
    used = set(re.findall(r"""['"`](ae\.[A-Za-z0-9_.]+)""", card + ae)) - {"ae.count."}
    # Dynamic job keys must also have both translations.
    used |= {f"ae.{value}" for value in (
        "sync", "render_frames", "render_final", "package", "queued", "running", "done", "failed", "superseded",
    )}
    used |= {f"ae.count.{value}" for value in ("created", "updated", "deleted")}
    used |= {f"ae.verify{value}" for value in (
        "", "Rendering", "Verifying", "Passed", "Differs", "Over", "Failed", "Interrupted",
        "Limits", "Frame", "AE", "KF", "Diff", "ImageAlt", "Masked",
    )}
    assert len(used) > 40
    for key in used:
        assert key in ko and key in en, key


def test_agent_and_ae_removed_copy_stays_unused():
    for key in ("ae.warnings", "ae.connected", "agent.toolCall", "agent.job"):
        assert key not in strings("ko") and key not in strings("en")


def test_solid_generation_copy_is_bilingual():
    ko, en = strings("ko"), strings("en")
    assert ko["review.generate3d"] == "3D 생성" and en["review.generate3d"] == "Generate 3D"
    assert ko["review.generate3dPrompt"] and en["review.generate3dPrompt"]


def test_final_ae_upgrade_and_live_check_docs_match_slice_one():
    readme = (ROOT / "README.md").read_text()
    guide = (ROOT / "docs/qa/ae-extension/live-check.md").read_text()
    assert "Scripts/ScriptUI Panels/keepframe_panel.jsx" in readme
    assert "KEEPFRAME_AE_RELAY_*" in readme and "no longer used" in readme
    assert "Docker images do not include a built ZXP" in readme
    assert "scripts/build_zxp.sh" in readme and "keepframe/ae/static/keepframe.zxp" in readme
    assert "source checkout" in readme
    assert "results are not filled in yet" not in guide
    assert "Results are recorded in [README.md](README.md)" in guide
    assert "```powershell" in guide and '$env:USERPROFILE\\Downloads\\keepframe.zxp' in guide
    assert "does not display a separate `interrupted` detail" not in guide
    assert "current web summary can show layer IDs" not in guide
