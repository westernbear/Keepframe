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
                "agent.correctionPreview", "agent.correctionInvalid", "agent.correctionElement",
                "agent.correctionText", "agent.correctionFont", "agent.correctionFontFamily",
                "agent.correctionFontWeight", "agent.correctionFontSize", "agent.correctionFrames",
                "agent.correctionFrame", "agent.correctionBbox", "agent.correctionMask",
                "agent.correctionMaskHidden", "agent.applied"):
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
  state:{scene:{constraints, elements:[{id:'e1', canonical:{text:'Before'}}]}}, CORRECTION_POLL_INTERVAL_MS:800,
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
  ? {correction:{op:'text', args:{element_id:'e1', text:'Hello'}}}
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
    assert.equal(args[3].version, 'v1', 'use the displayed preview version');
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


def test_agent_correction_contract_picks_fields_and_renders_only_text():
    js = static_src("js/agent.js")
    pending = js[js.index("function payloadOf("):js.index("\nfunction confirmEditBody(")]
    assert "shown = pick(args, ALLOWED[op])" in pending
    assert "{ ...shown, version: previewVersion }" in pending
    assert "...correction.args" not in pending
    assert "innerHTML" not in pending
    append = js[js.index("function appendAgent("):js.index("function appendChoices(")]
    assert "body.textContent = text" in append
    assert "innerHTML" not in append


@pytest.mark.parametrize("scenario", [
    "text", "font", "font_partial", "reassign", "bbox", "mask",
    "extra_text", "extra_font", "extra_reassign", "extra_bbox", "extra_mask", "model_version",
    "missing_text", "missing_reassign", "missing_bbox", "missing_mask", "unknown_id",
    "invalid_op", "invalid_font", "invalid_bbox", "invalid_mask", "empty_mask",
    "invalid_mask_color", "invalid_mask_alpha", "invalid_mask_size",
    "invalid_args_string", "invalid_args_null", "invalid_args_array",
    "missing_element", "missing_mask_frame", "missing_bbox_frame", "missing_reassign_frames",
])
def test_agent_correction_preview_matches_confirmed_fields(tmp_path, scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable")
    js = static_src("js/agent.js")
    pending = js[js.index("function payloadOf("):js.index("\nfunction confirmEditBody(")]
    append = js[js.index("function appendAgent("):js.index("function appendChoices(")]
    tool = js[js.index("function appendToolCall("):js.index("function appendVerify(")]
    script = tmp_path / "correction-preview.mjs"
    script.write_text('''import assert from 'node:assert/strict';
import vm from 'node:vm';
const scenario = ''' + json.dumps(scenario) + ''';
const nodes = [], requests = [], banners = [], imageSources = [];
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.dataset = {}; this.textContent = ''; }
  set innerHTML(value) { throw new Error('preview values must only be rendered as text'); }
  append(...children) { this.children.push(...children); }
  addEventListener(event, handler) { this.listeners[event] = handler; }
  click() { return this.listeners.click(); }
  getContext() {
    return {drawImage:() => {}, getImageData:() => {
      const data = new Uint8ClampedArray(4 * 3 * 4);
      for (let i = 3; i < data.length; i += 4) data[i] = 255;
      data[0] = data[1] = data[2] = 127;
      if (scenario !== 'empty_mask') for (const index of [5, 6, 9]) {
        data[index * 4] = data[index * 4 + 1] = data[index * 4 + 2] = index === 5 ? 128 : 255;
      }
      if (scenario === 'invalid_mask_color') data[0] = 255;
      if (scenario === 'invalid_mask_alpha') data[3] = 0;
      return {data};
    }};
  }
}
class Image {
  constructor() { this.naturalWidth = scenario === 'invalid_mask_size' ? 5 : 4; this.naturalHeight = 3; }
  set src(value) {
    imageSources.push(value);
    queueMicrotask(() => scenario === 'invalid_mask' ? this.onerror() : this.onload());
  }
}
const old = '<img src=x onerror=alert(1)>OLD';
const changed = '<svg onload=alert(2)>NEW';
const font = {family_guess:'Noto Sans', weight:700, size_px:48};
const scene = {frames:12, size:[4, 3], elements:[
  {id:'e1', label:old, canonical:{text:old, font:{family_guess:'Arial', weight:300, size_px:20}}},
  {id:'e2', label:'Target label', canonical:{}},
]};
const context = vm.createContext({
  projectId:'p1', sceneId:'s1', versionId:'v1', state:{scene}, pendingIntent:null,
  CORRECTION_POLL_INTERVAL_MS:800, Image, Uint8ClampedArray,
  document:{createElement:tag => new Element(tag)},
  logEl:{appendChild:node => nodes.push(node)}, hideEmpty:() => {},
  T:key => key, Tf:(key, vars) => key + JSON.stringify(vars),
  setBanner:(message, failure=false) => banners.push({message, failure}),
  postCorrect:async (...args) => { requests.push(args); return {job:{status:'running'}}; },
  fetchReviewJob:async () => ({status:'done', version:'v2'}),
  refreshAfterEdit:async () => {},
});
vm.runInContext(''' + json.dumps(append + tool + pending) + ''', context);
const mask = 'iVBORw0KGgoAAAANSUhEUgAAAAQAAAAD';
let op = scenario.includes('reassign') ? 'reassign'
  : scenario.includes('bbox') ? 'bbox' : scenario.includes('mask') ? 'mask' : 'text';
let args = op === 'reassign' ? {from_id:'e1', to_id:'e2', frames:[2, 4]}
  : op === 'bbox' ? {object_id:'e1', frame:3, bbox:[1, 1, 3, 3]}
  : op === 'mask' ? {object_id:'e1', frame:3, mask_png_base64:mask}
  : {element_id:'e1', text:changed};
if (scenario === 'font') args.font = {...font};
if (scenario === 'font_partial') { delete args.text; args.font = {weight:700}; }
if (scenario.startsWith('extra_')) args.note = 'hidden instruction';
if (scenario === 'extra_font') { delete args.note; args.font = {...font, candidates:['hidden']}; }
if (scenario === 'model_version') args.version = 'v999';
if (scenario === 'missing_text') delete args.text;
if (scenario === 'missing_reassign') delete args.to_id;
if (scenario === 'missing_bbox') delete args.bbox;
if (scenario === 'missing_mask') delete args.mask_png_base64;
if (scenario === 'missing_element') delete args.element_id;
if (scenario === 'missing_mask_frame' || scenario === 'missing_bbox_frame') delete args.frame;
if (scenario === 'missing_reassign_frames') delete args.frames;
if (scenario === 'unknown_id') args.element_id = 'absent';
if (scenario === 'invalid_op') op = 'toString';
if (scenario === 'invalid_font') args.font = null;
if (scenario === 'invalid_bbox') args.bbox = [1, 2, '<bad>', 4];
if (scenario === 'invalid_args_string') args = 'e1';
if (scenario === 'invalid_args_null') args = null;
if (scenario === 'invalid_args_array') args = [];
const invalid = scenario.startsWith('extra_') || scenario.startsWith('missing_')
  || scenario.startsWith('invalid_') || ['model_version', 'unknown_id', 'empty_mask'].includes(scenario);
const expected = JSON.parse(JSON.stringify(args));
context.appendToolCall('correct', {op, args}, {ok:true});
const textOf = node => [node.textContent, ...node.children.map(textOf)].join('\\n');
if (op === 'mask') assert.ok(!nodes.map(textOf).join('\\n').includes(mask), 'never print mask base64 in tool logs');
nodes.length = 0;
await context.paintPending({results:[{ok:true, payload:{correction:{op, args}}}], needs_confirm:true});
assert.equal(requests.length, 0, 'rendering a preview cannot submit');
const buttons = nodes.filter(node => node.tag === 'button');
if (invalid) {
  assert.equal(buttons.length, 0, 'invalid or undisclosed fields must block confirmation');
  assert.equal(banners.at(-1).message, 'agent.correctionInvalid');
  assert.equal(banners.at(-1).failure, true);
} else {
  assert.equal(buttons.length, 1);
  const preview = nodes.map(textOf).join('\\n');
  assert.ok(preview.includes('e1') && preview.includes(old), 'show ID and current scene content as literal text');
  if (op === 'text') {
    if (args.text !== undefined) assert.ok(preview.includes(changed) && preview.includes('agent.correctionText'));
    if (args.font) {
      for (const value of ['Arial', '300', '20', '700']) assert.ok(preview.includes(value));
      if (scenario === 'font_partial') for (const value of ['sans-serif', '32']) assert.ok(preview.includes(value));
    }
  } else if (op === 'reassign') {
    for (const value of ['e2', 'Target label', 'agent.correctionFrames', '2', '4']) assert.ok(preview.includes(value));
  } else {
    assert.ok(preview.includes('agent.correctionFrame') && preview.includes('3'));
    assert.ok(preview.includes(op === 'mask' ? 'agent.correctionMask' : 'agent.correctionBbox'));
    if (op === 'mask') {
      assert.ok(preview.includes('[1, 1, 3, 3]'), 'mask bbox is computed from decoded pixels');
      assert.ok(preview.includes('"pixels":3'), 'mask count follows the server threshold >127');
      assert.ok(!preview.includes(mask));
      assert.equal(imageSources.length, 1);
    }
  }
  // Changing the model payload after display cannot change the confirmed snapshot.
  args.text = 'unseen';
  if (args.font) args.font.weight = 100;
  if (args.frames) args.frames[0] = 9;
  if (args.bbox) args.bbox[0] = 9;
  args.note = 'unseen';
  context.versionId = 'v99';
  await buttons[0].click();
  assert.equal(requests.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(requests[0])), ['p1', 's1', op, {...expected, version:'v1'}]);
}
''')
    result = subprocess.run([node, str(script)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_runtime_assets_share_updated_cache_stamp():
    stamps = set()
    for path in [*STATIC.glob("*.html"), *STATIC.rglob("*.js")]:
        stamps.update(re.findall(r"\?v=([a-zA-Z0-9]+)", path.read_text(encoding="utf-8")))
    assert stamps == {"20261010c"}


def test_ae_card_static_contract():
    html = static_src("agent.html")
    assert re.search(r'</section>\s*<section class="render-card ae-card" id="ae-card" aria-labelledby="ae-card-title">', html)
    assert html.index('class="compare-panes"') < html.index('class="agent-transport"') < html.index('id="render-card"') < html.index('id="ae-card"')
    assert '<a href="/ae/keepframe.zxp" download' in html
    assert r'value="&amp; &quot;C:\Program Files\Common Files\Adobe\Adobe Desktop Common\RemoteComponents\UPI\UnifiedPluginInstallerAgent\UnifiedPluginInstallerAgent.exe&quot; /install &quot;$env:USERPROFILE\Downloads\keepframe.zxp&quot;"' in html
    assert 'aria-describedby="ae-install-path-hint"' in html
    assert 'data-i18n="ae.installPathHint"' in html
    assert 'data-ai-private' in html[html.index('id="ae-pairing"'):html.index('id="ae-devices"')]
    assert 'id="ae-status"' in html and 'id="ae-jobs"' in html
    agent = static_src("js/agent.js")
    assert 'import { initAECard } from "/static/js/ae.js?v=20261010c"' in agent
    assert agent.count("initAECard({") == 1
    refresh = agent[agent.index("async function refreshAfterEdit("):agent.index("\nfunction paintToolCalls(")]
    assert "aeCard.refresh()" in refresh
    ae = static_src("js/ae.js")
    assert "export function initAECard({projectId, getSceneId, getVersionId})" in ae
    assert set(re.findall(r"/api/ae/[a-z_/-]+", ae)) == {
        "/api/ae/codes", "/api/ae/devices", "/api/ae/devices/", "/api/ae/send", "/api/ae/state",
        "/api/ae/verify", "/api/ae/verify-image",
    }
    assert "visibilitychange" in ae
    assert "console." not in ae and "innerHTML" not in ae and "window.confirm" not in ae
    ko, en = static_src("js/i18n.js").split("en: {", 1)
    used = set(re.findall(r"ae\.[A-Za-z0-9_.]+", html + ae)) - {"ae.js", "ae.count."}
    # Dynamic job keys must also have both translations.
    used |= {f"ae.{value}" for value in (
        "sync", "render_frames", "render_final", "package", "queued", "running", "done", "failed", "superseded",
    )}
    used |= {f"ae.count.{value}" for value in ("created", "updated", "deleted")}
    used |= {f"ae.verify{value}" for value in (
        "", "Rendering", "Verifying", "Passed", "Differs", "Over", "Failed", "Interrupted",
        "Limits", "Frame", "AE", "KF", "Diff", "ImageAlt", "Masked",
    )}
    assert used
    for key in used:
        assert f'"{key}":' in ko and f'"{key}":' in en, key


def test_agent_and_ae_removed_copy_stays_unused():
    source = static_src("js/i18n.js")
    for key in ("ae.warnings", "ae.connected", "agent.toolCall", "agent.job"):
        assert f'"{key}":' not in source


def test_pending_solid_button_uses_edit_preview_and_confirmation(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable")
    inspector = static_src("js/review/inspector.js")
    inspector = inspector[inspector.index("export function attachInspector"):].replace("export function", "function", 1)
    form = static_src("js/review/edit-form.js")
    form = form[form.index("export function attachEditForm"):].replace("export function", "function", 1)
    script = tmp_path / "solid-button.mjs"
    script.write_text('''import assert from 'node:assert/strict';
import vm from 'node:vm';
class Element {
  constructor(tag='div') { this.tagName=tag; this.children=[]; this.events={}; this.dataset={}; this.files=[]; this.value=''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=[...children]; }
  addEventListener(name, fn) { this.events[name]=fn; }
  querySelectorAll() { return []; }
}
const dom = Object.fromEntries(['objectDetail','editSummary','editConflicts','editConfirm','editCancel','editFile','editPrompt','editRun'].map(k=>[k,new Element()]));
const item={id:'e1',kind:'sprite',pending_asset:'3d',canonical:{},visible:[0,11]};
const requests=[];
const ws={dom,projectId:'p1',sceneId:'s1',versionId:'v1',selectedId:'e1',
  state:{scene:{elements:[item]},project:{versions:[{id:'v1'}]}},setJobBanner(){}};
const context=vm.createContext({ws,document:{createElement:tag=>new Element(tag)},T:k=>k,
  postEdit:async body=>{requests.push(body);return {status:'needs_confirm',summary:'preview',intent:body.intent};},
  readFileAsDataUrl:async()=>null,isEditNeedsConfirm:s=>s==='needs_confirm'});
vm.runInContext(''' + json.dumps(form + '\n' + inspector + '\nattachEditForm(ws);attachInspector(ws);') + ''',context);
ws.renderObjectDetail();
const button=dom.objectDetail.children.find(el=>el.tagName==='button');
assert.ok(button,'pending solid needs a generation button');
assert.equal(button.textContent,'review.generate3d');
assert.equal(requests.length,0);
await button.events.click();
assert.equal(requests.length,1);
assert.equal(requests[0].confirm,false,'generation first previews the edit');
assert.equal(requests[0].intent.targets[0].property,'model');
assert.equal(requests[0].intent.targets[0].value,'reference');
assert.equal(requests[0].intent.targets[0].element,'e1');
assert.equal(dom.editConfirm.hidden,false);
ws.bindEdit();await dom.editConfirm.events.click();
assert.equal(requests.length,2);
assert.equal(requests[1].confirm,true,'only confirmation submits generation');
assert.equal(requests[1].intent.targets[0].value,'reference');
ws.versionId='v0';ws.renderObjectDetail();
assert.equal(dom.objectDetail.children.find(el=>el.tagName==='button').disabled,true,'historical review is read only');
item.pending_asset=null;ws.renderObjectDetail();
assert.equal(dom.objectDetail.children.some(el=>el.tagName==='button'),false);
''')
    result = subprocess.run([node, str(script)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_solid_generation_copy_is_bilingual():
    ko, en = static_src("js/i18n.js").split("en: {", 1)
    assert '"review.generate3d": "3D 생성"' in ko
    assert '"review.generate3d": "Generate 3D"' in en
    for key in ("review.generate3d", "review.generate3dPrompt"):
        assert f'"{key}":' in ko and f'"{key}":' in en


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
      appendVerify:() => {}, appendAgent:() => {}, confirmedLine:res => res.summary || 'applied',
      refreshAfterEdit:async id => refreshed.push(id),
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
