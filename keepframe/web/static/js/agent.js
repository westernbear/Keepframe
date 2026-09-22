import {
  fetchReviewState,
  postAgent,
  postEdit,
  reviewAssetUrl,
} from "/static/js/api.js?v=20260921v";
import { T } from "/static/js/i18n.js?v=20260921v";
import {
  createPreviewCache,
  createFrameTransport,
} from "/static/js/playback.js?v=20260921v";

const KEEP_PASS_RATE = 0.95;
const CONFIDENCE_PERCENT = 100;
const DEFAULT_SCENE_ID = "s1";
const MODEL_LABEL = "LLM 도구";
const EDIT_APPLIED = "적용 완료.";

const TOOL_PROMPTS = {
  analyze: "이 장면을 다시 분석해줘",
  correct: "보정할 대상을 지정해서 고쳐줘",
  set_keep: "유지할 조건을 지정해줘",
  edit: "문구를 바꿔줘",
  render: "장면을 렌더해줘",
  verify: "장면을 검증해줘",
  export: "MP4와 프로젝트를 내보내줘",
  report: "재구성 리포트를 요약해줘",
};

const params = new URLSearchParams(location.search);
const projectId = params.get("project");
const sceneId = params.get("scene") || DEFAULT_SCENE_ID;
let versionId = params.get("v") || null;
let state = null;
let frame = 0;
let selectedId = null;
let pendingIntent = null;
let pendingPrompt = "";

const logEl = document.getElementById("agent-log");
const inputEl = document.getElementById("agent-input");
const sendBtn = document.getElementById("agent-send");
const bannerEl = document.getElementById("agent-banner");
const emptyEl = document.getElementById("agent-empty");
const orig = document.getElementById("agent-orig");
const recon = document.getElementById("agent-recon");
const frameNum = document.getElementById("agent-frame");
const frameTotal = document.getElementById("agent-total");
const elementsList = document.getElementById("agent-elements-list");
const countEl = document.getElementById("agent-count");
const playBtn = document.getElementById("agent-play");
const playIcon = document.getElementById("agent-play-icon");
const pauseIcon = document.getElementById("agent-pause-icon");
const sceneBadge = document.getElementById("agent-scene");
const modelEl = document.getElementById("agent-model");

function setBanner(msg, isError = false) {
  bannerEl.hidden = !msg;
  bannerEl.textContent = msg || "";
  bannerEl.classList.toggle("agent-banner--error", isError);
}

function hideEmpty() {
  emptyEl.hidden = true;
}

function applyReadyFrame(f) {
  orig.src = previews.src("orig", f);
  recon.src = previews.src("recon", f);
  previews.prefetch(f + 1, state.scene.frames);
}

function showNextPlaybackFrame(next) {
  frameNum.textContent = String(frame);
  applyReadyFrame(next);
}

function syncPlayButton(playing) {
  playBtn.classList.toggle("is-playing", playing);
  playIcon.hidden = playing;
  pauseIcon.hidden = !playing;
}

function loadFrame() {
  if (!state) return;
  const target = frame;
  previews.prefetch(target, state.scene.frames);
  previews.wait(target).then((ok) => {
    if (!ok || frame !== target) return;
    applyReadyFrame(target);
  });
}

function clampFrame(f) {
  return Math.max(0, Math.min(state.scene.frames - 1, f));
}

function setFrame(f) {
  if (!state) return;
  frame = clampFrame(f);
  frameNum.textContent = String(frame);
  loadFrame();
}

function setPlaying(on) {
  transport.setPlaying(on);
}

function renderElements() {
  const els = state.scene.elements || [];
  countEl.textContent = String(els.length);
  elementsList.innerHTML = "";
  els.forEach((el) => {
    const row = document.createElement("div");
    row.className = "element-row" + (el.id === selectedId ? " element-row--selected" : "");
    row.dataset.id = el.id;
    const tex = el.canonical && el.canonical.texture;
    const thumb = tex ? `<img class="element-thumb" alt="" src="${reviewAssetUrl(tex, projectId, sceneId)}"/>` : `<span class="element-thumb"></span>`;
    const conf = el.confidence != null ? Math.round(el.confidence * CONFIDENCE_PERCENT) + "%" : "";
    row.innerHTML = `${thumb}<span class="element-row__id">${el.id}</span><span class="element-row__kind">${el.kind}</span><span class="element-row__conf">${conf}</span>`;
    row.addEventListener("click", () => {
      selectedId = el.id;
      elementsList.querySelectorAll(".element-row").forEach((r) => r.classList.toggle("element-row--selected", r.dataset.id === el.id));
    });
    elementsList.appendChild(row);
  });
}

function applySceneChrome() {
  sceneBadge.textContent = `${sceneId} · ${state.scene.frames}f`;
  frameTotal.textContent = String(state.scene.frames);
  modelEl.textContent = MODEL_LABEL;
}

async function loadState() {
  state = await fetchReviewState(projectId, sceneId, versionId);
  versionId = state.version.id;
  applySceneChrome();
  renderElements();
  setFrame(0);
}

function appendUser(text) {
  hideEmpty();
  const wrap = document.createElement("div");
  wrap.className = "msg msg--user";
  const label = document.createElement("div");
  label.className = "msg__label";
  label.textContent = T("agent.you");
  const body = document.createElement("div");
  body.className = "msg__body";
  body.textContent = text;
  wrap.append(label, body);
  logEl.appendChild(wrap);
  logEl.scrollTop = logEl.scrollHeight;
}

function appendToolCall(name, args, result) {
  const el = document.createElement("div");
  el.className = "toolcall";
  const head = document.createElement("div");
  head.className = "toolcall__head";
  const nameEl = document.createElement("span");
  nameEl.className = "toolcall__name";
  nameEl.textContent = `TOOL ${name}`;
  const status = document.createElement("span");
  status.className = "toolcall__status" + (result.ok ? " toolcall__status--ok" : " toolcall__status--fail");
  status.textContent = result.ok ? "OK" : "FAIL";
  head.append(nameEl, status);
  const argsEl = document.createElement("div");
  argsEl.className = "toolcall__args";
  argsEl.textContent = JSON.stringify(args || {}, null, 0);
  const msgEl = document.createElement("div");
  msgEl.className = "toolcall__msg";
  msgEl.textContent = result.message || "";
  el.append(head, argsEl, msgEl);
  logEl.appendChild(el);
  logEl.scrollTop = logEl.scrollHeight;
}

function isKeepPassed(verify) {
  return verify.passed !== false && verify.keep_pass_rate >= KEEP_PASS_RATE;
}

function appendVerify(verify) {
  if (!verify) return;
  const keepPassed = isKeepPassed(verify);
  const chip = document.createElement("span");
  chip.className = "verify-chip " + (keepPassed ? "verify-chip--pass" : "verify-chip--fail");
  chip.textContent = `${keepPassed ? "PASS" : "FAIL"} · keep ${Math.round((verify.keep_pass_rate || 0) * CONFIDENCE_PERCENT)}% · err ${(verify.layer_max_err_px ?? 0).toFixed(2)}px`;
  logEl.appendChild(chip);
  logEl.scrollTop = logEl.scrollHeight;
}

function appendAgent(text) {
  hideEmpty();
  const wrap = document.createElement("div");
  wrap.className = "msg msg--agent";
  const label = document.createElement("div");
  label.className = "msg__label";
  label.textContent = T("agent.assistant");
  const body = document.createElement("div");
  body.className = "msg__body";
  body.textContent = text;
  wrap.append(label, body);
  logEl.appendChild(wrap);
  logEl.scrollTop = logEl.scrollHeight;
}

function appendChoices(plan) {
  const conflicts = (plan && plan.conflicts) || [];
  if (!conflicts.length) return;
  const wrap = document.createElement("div");
  wrap.className = "agent-choice";
  conflicts.forEach((c) => {
    const title = document.createElement("div");
    title.className = "mono agent-choice__reason";
    title.textContent = c.reason || c.element;
    wrap.appendChild(title);
    (c.choices || []).forEach((ch, i) => {
      const label = document.createElement("label");
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = `agent-${c.id}`;
      radio.value = ch;
      if (i === 0) radio.checked = true;
      const span = document.createElement("span");
      span.textContent = T(`review.editChoice.${ch}`);
      label.append(radio, span);
      wrap.appendChild(label);
    });
  });
  const confirm = document.createElement("button");
  confirm.className = "btn btn--primary";
  confirm.type = "button";
  confirm.textContent = T("agent.choose");
  confirm.addEventListener("click", () => runConfirmWithChoices());
  wrap.appendChild(confirm);
  logEl.appendChild(wrap);
  logEl.scrollTop = logEl.scrollHeight;
}

function editChoices() {
  const out = {};
  logEl.querySelectorAll(".agent-choice input[type=radio]:checked").forEach((el) => {
    out[el.name.replace(/^agent-/, "")] = el.value;
  });
  return out;
}

function payloadOf(results, key) {
  const hit = results.find((r) => r.payload && r.payload[key]);
  return hit && hit.payload ? hit.payload[key] : null;
}

function appendConfirmButton() {
  const confirm = document.createElement("button");
  confirm.className = "btn btn--primary";
  confirm.type = "button";
  confirm.textContent = T("agent.confirm");
  confirm.addEventListener("click", () => runConfirm(false));
  logEl.appendChild(confirm);
  logEl.scrollTop = logEl.scrollHeight;
}

function paintPending(turn) {
  pendingIntent = payloadOf(turn.results, "intent");
  const plan = payloadOf(turn.results, "plan");
  const needsChoice = Boolean(turn.needs_choice);
  const needsConfirm = Boolean(turn.needs_confirm);
  if (needsChoice) appendChoices(plan);
  if (needsConfirm) appendConfirmButton();
}

function confirmEditBody(withChoices) {
  return {
    project: projectId,
    scene: sceneId,
    v: versionId,
    prompt: pendingPrompt,
    element: selectedId,
    intent: pendingIntent,
    confirm: true,
    choices: withChoices ? editChoices() : {},
  };
}

async function applyConfirmedEdit(res) {
  pendingIntent = null;
  const editDone = res.status === "done" && res.version;
  if (editDone) {
    appendVerify(res.verify);
    appendAgent(res.summary || EDIT_APPLIED);
    await refreshAfterEdit(res.version.id);
    return true;
  }
  setBanner(res.error || T("agent.failed"), true);
  return false;
}

async function runConfirm(withChoices) {
  setBanner("");
  try {
    const res = await postEdit(confirmEditBody(withChoices));
    await applyConfirmedEdit(res);
  } catch (err) {
    setBanner(err.message || T("agent.failed"), true);
  }
}

async function runConfirmWithChoices() {
  return runConfirm(true);
}

async function refreshAfterEdit(version) {
  versionId = version;
  await loadState();
}

function paintToolCalls(turn) {
  (turn.tool_calls || []).forEach((tc, i) => {
    appendToolCall(tc.name, tc.arguments, (turn.results && turn.results[i]) || { ok: false, message: "" });
  });
}

function verifyFromTurn(turn) {
  return (turn.results || []).map((r) => r.payload && r.payload.verify).find(Boolean);
}

function paintTurn(turn) {
  paintToolCalls(turn);
  if (turn.reply) appendAgent(turn.reply);
  appendVerify(verifyFromTurn(turn));
  const isPending = turn.status === "pending";
  const isError = turn.status === "error";
  if (isPending) paintPending(turn);
  if (isError) setBanner(turn.reply || T("agent.failed"), true);
}

async function send(message) {
  const text = (message || inputEl.value || "").trim();
  if (!text || !projectId) return;
  inputEl.value = "";
  pendingPrompt = text;
  appendUser(text);
  setBanner(T("agent.thinking"));
  sendBtn.disabled = true;
  try {
    const turn = await postAgent({ project: projectId, scene: sceneId, v: versionId, message: text });
    setBanner("");
    paintTurn(turn);
  } catch (err) {
    setBanner(err.message || T("agent.failed"), true);
  } finally {
    sendBtn.disabled = false;
  }
}

function fillToolPrompt(tool) {
  inputEl.value = TOOL_PROMPTS[tool] || "";
  inputEl.focus();
}

function showMissingProject() {
  emptyEl.textContent = T("agent.needProject");
  emptyEl.hidden = false;
}

const previews = createPreviewCache({
  project: projectId,
  scene: sceneId,
  version: () => versionId,
});

const transport = createFrameTransport({
  getFrame: () => frame,
  setFrameIndex: (next) => { frame = next; },
  getFps: () => state.scene.fps,
  getFrameCount: () => state.scene.frames,
  prefetch: (from, total) => previews.prefetch(from, total),
  isReady: (index) => previews.isReady(index),
  wait: (index) => previews.wait(index),
  showFrame: showNextPlaybackFrame,
  canPlay: () => Boolean(state),
  onPlayingChange: syncPlayButton,
});

document.getElementById("agent-back").href = `/review?project=${encodeURIComponent(projectId || "")}&scene=${encodeURIComponent(sceneId)}`;

sendBtn.addEventListener("click", () => send());
inputEl.addEventListener("keydown", (e) => {
  const isSendChord = e.key === "Enter" && (e.ctrlKey || e.metaKey);
  if (isSendChord) send();
});
document.querySelectorAll("#agent-tools .tool").forEach((btn) => {
  btn.addEventListener("click", () => fillToolPrompt(btn.dataset.tool));
});
playBtn.addEventListener("click", () => setPlaying(!transport.isPlaying()));
document.getElementById("agent-prev").addEventListener("click", () => { setPlaying(false); setFrame(frame - 1); });
document.getElementById("agent-next").addEventListener("click", () => { setPlaying(false); setFrame(frame + 1); });
window.addEventListener("keepframe:lang", () => {
  if (state) renderElements();
});

if (!projectId) showMissingProject();
else {
  loadState().catch((err) => {
    setBanner(err.message || T("agent.failed"), true);
  });
}
