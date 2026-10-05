import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DESIGNS = ROOT / ".stitch" / "designs"
STATIC = ROOT / "keepframe" / "web" / "static"
FORBIDDEN = (
    "cdn.tailwindcss.com",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "lh3.googleusercontent.com",
    "material-symbols",
)


def static_src(*names):
    return "\n".join((STATIC / name).read_text(encoding="utf-8") for name in names)


def test_analyze_stitch_source_exists():
    html = (DESIGNS / "analyze.html").read_text(encoding="utf-8")
    assert "<html" in html.lower()
    assert (DESIGNS / "analyze.png").stat().st_size > 1000


def test_landing_stitch_source_exists():
    html = (DESIGNS / "landing.html").read_text(encoding="utf-8")
    assert "<html" in html.lower()
    assert "유지할 것과 바꿀 것을 지정하세요" in html
    assert (DESIGNS / "landing.png").stat().st_size > 1000


def test_ported_pages_are_offline():
    for name in ("landing.html", "library.html", "ingest.html", "analyze.html", "review.html"):
        text = (STATIC / name).read_text(encoding="utf-8")
        for bad in FORBIDDEN:
            assert bad not in text, f"{name} still loads {bad}"
        assert "/static/css/app.css" in text
        assert "/static/js/i18n.js" in text
        assert "/static/js/api.js" in text


def test_analyze_uses_primary_navigation_without_duplicate_icon_rail():
    html = (STATIC / "analyze.html").read_text(encoding="utf-8")
    assert 'class="header__nav maker-nav"' in html
    assert "sidebar--icon" not in html
    assert "main--icon" not in html


def test_analyze_steps_follow_job_stage():
    src = static_src("analyze.html", "js/analyze.js", "js/api.js")
    assert 'data-step="shots"' in src
    assert "updateSteps" in src
    assert "/api/jobs/" in src
    assert "li--active" not in src


def test_pages_include_logo_and_favicon():
    assert (STATIC / "logo.png").stat().st_size > 100
    assert (STATIC / "icon.png").stat().st_size > 100
    for name in ("landing.html", "library.html", "ingest.html", "analyze.html", "review.html"):
        text = (STATIC / name).read_text(encoding="utf-8")
        assert 'src="/static/logo.png"' in text
        assert 'href="/static/icon.png"' in text


def test_ingest_sends_selected_range_to_analyze():
    ingest = static_src("ingest.html", "js/ingest.js")
    analyze = static_src("analyze.html", "js/analyze.js")
    assert "selectedWindow" in ingest
    assert "mode: win.mode" in ingest
    assert "payload.start" in analyze
    assert "payload.end" in analyze


def test_ingest_reestimates_edited_boundaries_and_requires_short_scene_ack():
    ingest = static_src("ingest.html", "js/ingest.js", "js/analyze.js")
    assert "data-scene-boundary-list" in ingest
    assert "payload.scenes = scenes" in ingest
    assert "acknowledge_short_scenes = Boolean(estimateSnapshot.acknowledge_short_scenes)" in ingest


def test_ingest_gpu_status_is_not_hardcoded():
    text = static_src("ingest.html", "js/ingest.js", "js/api.js")
    assert "data-gpu" in text
    assert "/api/status" in text
    assert "<span>ON</span>" not in text


def test_i18n_has_ko_and_en_keys():
    src = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
    assert "keepframe.lang" in src
    for key in ("ingest.start", "review.keepSave", "review.approve", "review.openAgent", "review.editRun", "error.liveaction", "library.empty", "landing.headline"):
        assert src.count(f'"{key}"') >= 2


def test_font_candidate_copy_is_bilingual():
    src = static_src("js/i18n.js")
    ko, en = src.split("en: {", 1)
    assert '"review.fontCandidates": "폰트 후보"' in ko
    assert '"review.fontCandidates": "Font candidates"' in en
    assert '"review.applyFont": "폰트 적용"' in ko
    assert '"review.applyFont": "Apply font"' in en


def test_review_font_picker_preserves_font_and_corrects_selected_text(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable")
    source = static_src("js/review/inspector.js")
    source = source[source.index("export function attachInspector"):].replace("export function", "function", 1)
    script = tmp_path / "font-picker.mjs"
    script.write_text('''import assert from 'node:assert/strict';
import vm from 'node:vm';
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.events = {}; this.dataset = {}; this.disabled = false; this.value = ''; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.append(child); }
  replaceChildren(...children) { this.children = [...children]; }
  addEventListener(name, fn) { this.events[name] = fn; }
  setAttribute() {}
  querySelectorAll() { return []; }
}
const ids = new Map();
const document = {createElement: tag => new Element(tag), getElementById: id => {
  if (!ids.has(id)) ids.set(id, new Element('div'));
  return ids.get(id);
}};
const font = {family_guess:'DejaVu Serif', weight:700, size_px:40, candidates:['DejaVu Sans', 'DejaVu Serif', 'Liberation Sans']};
const item = {id:'e1', kind:'text', canonical:{text:'Launch faster', font}, visible:[0, 10]};
const calls = [];
const ws = {dom:{objectDetail:new Element('section'), elementList:new Element('div'), timelineTracks:new Element('div'), formsPanel:new Element('fieldset')},
  state:{scene:{elements:[item]}, project:{versions:[{id:'v1'}]}}, versionId:'v1', frame:0,
  drawOverlays() {}, runCorrect:async (op, args) => calls.push({op,args})};
const context = vm.createContext({document, T:key=>key, ws});
vm.runInContext(''' + json.dumps(source) + ''' + '\\nattachInspector(ws);', context);
function find(tag, root = document.getElementById('font-candidates')) {
  if (root.tagName === tag) return root;
  for (const child of root.children) { const found = find(tag, child); if (found) return found; }
}
ws.selectElement('e1');
const select = find('select'), button = find('button');
assert.ok(select && button, 'selected text needs a font picker and apply button');
assert.equal(select.value, 'DejaVu Serif');
assert.deepEqual(select.children.map(option => option.value), font.candidates);
assert.equal(find('label').textContent, 'review.fontCandidates');
assert.equal(find('label').htmlFor, select.id);
assert.equal(button.textContent, 'review.applyFont');
assert.equal(calls.length, 0, 'font choice requires explicit apply');
select.value = 'Liberation Sans';
await button.events.click();
assert.equal(calls.length, 1);
assert.equal(calls[0].op, 'text');
assert.equal(calls[0].args.element_id, 'e1');
assert.deepEqual(JSON.parse(JSON.stringify(calls[0].args.font)), {...font, family_guess:'Liberation Sans'});
assert.equal(font.family_guess, 'DejaVu Serif', 'must not mutate loaded scene');
item.canonical.font = {...font, family_guess:'Inter'};
ws.selectElement('e1');
assert.equal(find('select').value, 'Inter', 'show the current family even after manual font edits');
item.canonical.font = {...font, candidates:[]};
ws.selectElement('e1');
assert.equal(find('select'), undefined);
item.kind = 'sprite'; item.canonical.font = font;
ws.selectElement('e1');
assert.equal(find('select'), undefined);
item.kind = 'text'; delete item.canonical.font;
ws.selectElement('e1');
assert.equal(find('select'), undefined);
''')
    result = subprocess.run([node, str(script)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_i18n_markup_keys_exist_in_both_langs():
    import re
    src = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
    ko = src.split("en: {", 1)[0]
    en = src.split("en: {", 1)[1]
    used = set()
    for html in STATIC.glob("*.html"):
        used.update(re.findall(r'data-i18n="([^"]+)"', html.read_text(encoding="utf-8")))
    missing = [k for k in sorted(used) if f'"{k}"' not in ko or f'"{k}"' not in en]
    assert missing == []


def test_api_prefetches_review_frames():
    src = (STATIC / "js" / "playback.js").read_text(encoding="utf-8")
    assert "function createPreviewCache" in src
    assert "function createFrameTransport" in src
    assert "img.decode" in src
    assert "PREFETCH_AHEAD" in src
    assert "PREVIEW_CACHE_LIMIT" in src


def test_css_tokens_match_design():
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    assert "#090909" in css and "#0099ff" in css and "#ffffff" in css


def test_hidden_wins_over_display():
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    assert "[hidden] { display: none !important; }" in css


def test_review_empty_project_has_library_exit():
    src = static_src("review.html", "js/review.js")
    assert 'id="review-missing"' in src
    assert "is-empty" in src
    assert 'href="/library"' in src
    assert "review.backLibrary" in src


def test_landing_and_library_link_to_demo():
    landing = static_src("landing.html")
    library = static_src("library.html", "js/library.js")
    assert 'href="/demo"' in landing
    assert 'href="/demo"' in library
    assert "isApprovedStatus" in library
    assert "nav.demo" in landing
    assert "p.scene" in library
    assert "/agent?project=" in library
    assert "isReviewStatus" in library


def test_library_filters_are_wired():
    src = static_src("library.html", "js/library.js")
    assert "data-search" in src
    assert 'data-filter="review"' in src
    assert "visibleProjects" in src
    assert 'library.filter.export' not in src

def test_review_empty_copy_uses_the_exact_korean_guidance():
    source = static_src("review.html", "js/i18n.js")
    assert 'data-i18n="review.empty">인식된 객체가 없습니다. 다른 프레임이나 분석 버전을 확인하세요.' in source
    assert '"review.empty": "인식된 객체가 없습니다. 다른 프레임이나 분석 버전을 확인하세요."' in source

def test_all_linked_runtime_assets_exist_locally():
    import re
    for page in STATIC.glob("*.html"):
        text = page.read_text(encoding="utf-8")
        for asset in re.findall(r"(?:src|href)=\"(/static/[^\"?#]+)", text):
            assert (STATIC / asset.removeprefix("/static/")).is_file(), f"{page.name} links missing {asset}"


def test_agent_sends_attachment_on_confirm():
    js = (STATIC / "js" / "agent.js").read_text()
    assert "attachment: pendingAttachment" in js
    assert "attachment: attachmentMeta()" in js


def test_agent_keep_and_correction_use_browser_confirmation_apis():
    js = static_src("js/agent.js")
    assert 'payloadOf(turn.results, "keep_change")' in js
    assert 'payloadOf(turn.results, "correction")' in js
    assert "postKeep(" in js
    assert "postCorrect(" in js
    assert "fetchReviewJob(" in js


def test_agent_keep_preview_guards_target_array_before_matching():
    js = static_src("js/agent.js")
    keep = js[js.index('const keepChange = payloadOf(turn.results, "keep_change");'):
              js.index('const correction = payloadOf(turn.results, "correction");')]
    assert "Array.isArray(keepChange.targets)" in keep
    assert keep.index("Array.isArray(keepChange.targets)") < keep.index("keepChange.targets.some(")


def test_agent_keep_and_correction_copy_is_bilingual():
    ko, en = static_src("js/i18n.js").split("en: {", 1)
    for key in ("agent.keepPresetPreview", "agent.keepOnPreview", "agent.keepOffPreview",
                "agent.keepExamplesPreview", "agent.keepChangeNote", "agent.keepPreviewChanged",
                "agent.correctionPreview", "agent.applied"):
        assert f'"{key}":' in ko
        assert f'"{key}":' in en
    assert '"agent.keepChangeNote": "에이전트 keep 변경"' in ko
    assert '"agent.keepChangeNote": "Agent keep change"' in en


@pytest.mark.parametrize("scenario", [
    "keep_preset", "keep_targets", "keep_on", "keep_error", "keep_mismatch",
    "keep_targets_string", "keep_targets_missing", "keep_targets_null",
    "correct_done", "correct_submit_error", "correct_job_error", "correct_poll_error",
])
def test_agent_pending_mutations_execute_only_on_click(tmp_path, scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable")
    js = static_src("js/agent.js")
    pending = js[js.index("function payloadOf("):js.index("\nfunction confirmEditBody(")]
    script = tmp_path / "agent-pending.mjs"
    script.write_text('''import assert from 'node:assert/strict';
import vm from 'node:vm';
const scenario = ''' + json.dumps(scenario) + ''';
const buttons = [], requests = [], banners = [], refreshed = [], summaries = [], polls = [];
class Element {
  constructor() { this.disabled = false; this.listeners = {}; }
  addEventListener(event, handler) { this.listeners[event] = handler; }
  click() { return this.listeners.click(); }
}
const constraints = [
  {pred:"type(e1,'translation')", keep:true},
  {pred:'left(e1,e2)', keep:true},
  {pred:'right(e10,e2)', keep:false},
  {pred:"type(e2,'translation')", keep:false},
];
const snapshots = [
  {status:'running', op:'text'}, {status:'running', op:'text'},
  scenario === 'correct_job_error'
    ? {status:'error', error:'correction failed'}
    : {status:'done', version:'v2'},
];
const context = vm.createContext({
  projectId:'p1', sceneId:'s1', versionId:'v1', pendingIntent:null,
  state:{scene:{constraints}}, CORRECTION_POLL_INTERVAL_MS:800,
  document:{createElement:() => new Element()},
  logEl:{appendChild:el => buttons.push(el), scrollTop:0, scrollHeight:1},
  T:key => key, Tf:(key, vars) => key + JSON.stringify(vars),
  setBanner:(message, failure=false) => banners.push({message, failure}),
  appendAgent:message => summaries.push(message),
  refreshAfterEdit:async version => {
    assert.equal(requests.length, 1, 'refresh follows exactly one confirmed request');
    if (scenario.startsWith('correct')) assert.equal(snapshots.length, 0, 'refresh waits for done');
    refreshed.push(version);
  },
  postKeep:async (...args) => {
    requests.push({kind:'keep', args});
    if (scenario === 'keep_error') throw new Error('keep save failed');
    return {version:{id:'v2'}};
  },
  postCorrect:async (...args) => {
    requests.push({kind:'correct', args});
    if (scenario === 'correct_submit_error') throw new Error('correction submit failed');
    return {job:{status:'running', op:'text'}};
  },
  fetchReviewJob:async (...args) => {
    polls.push(args);
    if (scenario === 'correct_poll_error') throw new Error('poll failed');
    assert.ok(snapshots.length, 'polling stops at a terminal status');
    return snapshots.shift();
  },
  setTimeout:fn => { fn(); return 1; },
});
vm.runInContext(''' + json.dumps(pending) + ''', context);
const isCorrect = scenario.startsWith('correct');
const payload = isCorrect
  ? {correction:{op:'text', args:{element_id:'e1', text:'Hello', version:'untrusted'}}}
  : {keep_change:scenario === 'keep_preset'
      ? {preset:'all'}
      : {targets:['e1', 'left(e1,e2)', 'e1'], on:scenario === 'keep_on',
         matched:scenario === 'keep_mismatch' ? 4 : 3,
         examples:constraints.slice(0, 3).map(c => c.pred)}};
const malformed = ['keep_targets_string', 'keep_targets_missing', 'keep_targets_null'].includes(scenario);
if (scenario === 'keep_targets_string') payload.keep_change.targets = 'e1';
if (scenario === 'keep_targets_missing') delete payload.keep_change.targets;
if (scenario === 'keep_targets_null') payload.keep_change.targets = null;
context.paintPending({results:[{ok:true, payload}], needs_confirm:true, needs_choice:false});
assert.equal(requests.length, 0, 'painting a preview must never execute it');
assert.equal(polls.length, 0, 'painting must never poll correction jobs');
if (!malformed) assert.ok(summaries.length, 'show a translated preview');
if (!isCorrect && scenario !== 'keep_preset' && !malformed) {
  assert.ok(summaries[0].includes('"n":' + payload.keep_change.matched), 'show the matched count');
  for (const example of payload.keep_change.examples) {
    assert.ok(summaries[0].includes(example), 'show example predicate ' + example);
  }
}
if (scenario === 'keep_mismatch' || malformed) {
  assert.equal(buttons.length, 0, 'a mismatched or malformed preview cannot be confirmed');
  assert.equal(banners.at(-1).message, 'agent.keepPreviewChanged');
  assert.ok(banners.at(-1).failure);
} else {
  assert.equal(buttons.length, 1);
  // Snapshot matching predicates and the displayed version before confirmation.
  constraints.push({pred:'bottom(e1,e3)', keep:true});
  context.versionId = 'v99';
  await Promise.all([buttons[0].click(), buttons[0].click()]);
  assert.equal(requests.length, 1, 'double click must not submit twice');
  const args = requests[0].args;
  assert.deepEqual(args.slice(0, 2), ['p1', 's1']);
  if (isCorrect) {
    assert.equal(requests[0].kind, 'correct');
    assert.equal(args[2], 'text');
    assert.equal(args[3].element_id, 'e1');
    assert.equal(args[3].text, 'Hello');
    assert.equal(args[3].version, 'v1', 'use the preview version, overriding model args');
  } else {
    assert.equal(args[3], 'agent.keepChangeNote', 'agent-confirmed keep changes need their own version note');
    if (scenario === 'keep_preset') {
      assert.equal(args[4], 'all');
      assert.equal(args[2].length, 0);
    } else {
      const keep = scenario === 'keep_on';
      assert.deepEqual(JSON.parse(JSON.stringify(args[2])), [
        {pred:"type(e1,'translation')", keep},
        {pred:'left(e1,e2)', keep},
        {pred:'right(e10,e2)', keep},
      ], 'submit exact predicates using equality or substring matching');
    }
  }
  if (scenario.endsWith('error')) {
    assert.equal(refreshed.length, 0);
    assert.ok(banners.at(-1).failure, 'show request, job and polling errors in the banner');
    assert.equal(buttons[0].disabled, false, 'allow retry after failure');
  } else {
    assert.deepEqual(refreshed, ['v2']);
    assert.equal(banners.at(-1).failure, false);
    assert.equal(buttons[0].disabled, true, 'do not repeat an applied change');
    await buttons[0].click();
    assert.equal(requests.length, 1);
  }
}
''')
    result = subprocess.run([node, str(script)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_runtime_assets_share_updated_cache_stamp():
    stamps = set()
    for path in [*STATIC.glob("*.html"), *STATIC.rglob("*.js")]:
        stamps.update(re.findall(r"\?v=([a-zA-Z0-9]+)", path.read_text(encoding="utf-8")))
    assert stamps == {"20261005b"}


def test_agent_confirm_needs_choice_keeps_intent_and_can_resubmit(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable")
    js = (STATIC / "js" / "agent.js").read_text()
    confirm = js[js.index("function confirmEditBody("):js.index("\nasync function refreshAfterEdit(")]
    script = tmp_path / "agent-confirm.mjs"
    script.write_text('''import assert from 'node:assert/strict';
import vm from 'node:vm';
for (const conflict of ['keep_violation', 'timing_overflow', 'font_missing']) {
  for (const intentMode of ['missing', 'null', 'updated']) {
    const initialIntent = {targets:[{element:'e1', property:'color', value:'#ffffff'}]};
    const updatedIntent = {targets:[{element:'e1', property:'color', value:'#222222'}]};
    const plan = {conflicts:[{id:conflict, choices:['release_keep']}]};
    const response = {status:'needs_choice', plan};
    if (intentMode !== 'missing') response.intent = intentMode === 'updated' ? updatedIntent : null;
    const expectedIntent = intentMode === 'updated' ? updatedIntent : initialIntent;
    const requests = [], choices = [], banners = [], refreshed = [];
    const context = vm.createContext({
      projectId:'p1', sceneId:'s1', versionId:'v1', selectedId:'e1',
      pendingIntent:initialIntent, pendingPrompt:'change color',
      pendingAttachment:'attachment-data', pendingAttachmentFile:{},
      attachInput:{value:'attachment.png'}, document:{getElementById:() => ({textContent:''})},
      EDIT_APPLIED:'applied', T:key => key,
      postEdit:async body => {
        requests.push(body);
        return requests.length === 1 ? response : {status:'done', version:{id:'v2'}};
      },
      appendChoices:plan => choices.push(plan), editChoices:() => ({[conflict]:'release_keep'}),
      setBanner:(message, failure) => banners.push({message, failure}),
      appendVerify:() => {}, appendAgent:() => {}, refreshAfterEdit:async id => refreshed.push(id),
    });
    vm.runInContext(''' + json.dumps(confirm) + ''', context);
    await context.runConfirm(false);
    assert.equal(choices.length, 1, 'confirm needs_choice must append choices');
    assert.equal(choices[0], plan);
    assert.equal(context.pendingIntent, expectedIntent, 'pending intent must survive needs_choice');
    assert.equal(context.pendingAttachment, 'attachment-data');
    assert.equal(banners.some(banner => banner.failure || banner.message), false, 'no failure banner');
    assert.equal(requests[0].confirm, true);
    assert.equal(Object.keys(requests[0].choices).length, 0);
    await context.runConfirmWithChoices();
    assert.equal(requests.length, 2);
    assert.equal(requests[1].intent, expectedIntent);
    assert.equal(requests[1].choices[conflict], 'release_keep');
    assert.equal(requests[1].prompt, 'change color');
    assert.equal(requests[1].attachment, 'attachment-data');
    assert.equal(context.pendingIntent, null);
    assert.equal(context.pendingAttachment, null);
    assert.deepEqual(refreshed, ['v2']);
  }
}
''')
    result = subprocess.run([node, str(script)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
