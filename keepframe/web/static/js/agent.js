import {
  nativeArtifactUrl,
  lottieArtifactUrl,
  approveRenderPlan,
  fetchAgentHistory,
  fetchRenderPlans,
  fetchRenderState,
  fetchReviewJob,
  fetchReviewState,
  postAgent,
  postCorrect,
  postEdit,
  postKeep,
  postRenderPlan,
  reviewAssetUrl,
} from "/static/js/api.js?v=20261006e";
import { T, Tf } from "/static/js/i18n.js?v=20261006e";
import { initAECard } from "/static/js/ae.js?v=20261006e";
import { readFileAsDataUrl } from "/static/js/files.js?v=20261006e";
import {
  createPreviewCache,
  createFrameTransport,
} from "/static/js/playback.js?v=20261006e";

const KEEP_PASS_RATE = 0.95;
const CONFIDENCE_PERCENT = 100;
const DEFAULT_SCENE_ID = "s1";
const MODEL_LABEL = "LLM 도구";
const EDIT_APPLIED = "적용 완료.";
const CORRECTION_POLL_INTERVAL_MS = 800;

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
let sceneId = params.get("scene") || DEFAULT_SCENE_ID;
let versionId = params.get("v") || null;
let state = null;
let frame = 0;
let selectedId = null;
let pendingIntent = null;
let pendingPrompt = "";
let pendingAttachment = null;
let pendingAttachmentFile = null;

let renderPayload = null;
let renderPlans = [];
let renderPoll = null;
let renderBusy = false;
const logEl = document.getElementById("agent-log");
const inputEl = document.getElementById("agent-input");
const attachInput = document.getElementById("agent-attach");
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
const sceneSelect = document.getElementById("agent-scene-select");
const modelEl = document.getElementById("agent-model");
const renderModeEl = document.getElementById("render-mode");
const renderBackendEl = document.getElementById("render-backend");
const renderPlanSelectorEl = document.getElementById("render-plan-selector");
const renderCreateBtn = document.getElementById("render-create");
const renderApproveBtn = document.getElementById("render-approve");
const renderStatusEl = document.getElementById("render-status");
const renderLockEl = document.getElementById("render-lock");
const renderOutputsEl = document.getElementById("render-outputs");
const renderStateEl = document.getElementById("render-state");
const renderReasonEl = document.getElementById("render-reason");
const renderArtifactsEl = document.getElementById("render-artifacts");

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
    const row = document.createElement("button");
    row.type = "button";
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
  const scenes = (state.project && state.project.scenes) || [];
  sceneSelect.replaceChildren();
  scenes.forEach((scene) => {
    const option = document.createElement("option");
    option.value = scene.id;
    option.textContent = `${scene.id} · ${scene.frames[0]}–${scene.frames[1]}${scene.transition_out ? ` · ${scene.transition_out.transition || "unknown"}` : ""}`;
    option.selected = scene.id === sceneId;
    sceneSelect.appendChild(option);
  });
  sceneSelect.hidden = scenes.length < 2;
}

async function loadState() {
  state = await fetchReviewState(projectId, sceneId, versionId);
  versionId = state.version.id;
  applySceneChrome();
  renderElements();
  setFrame(0);
}

function shortDigest(value) {
  return value ? `${value.slice(0, 10)}…` : "—";
}


function shouldPollRender(payload) {
  return Boolean(payload && payload.plan && ["queued", "running"].includes(payload.status));
}

function renderPayloadStatus(payload) {
  return (payload && (payload.status || (payload.state && payload.state.status))) || "—";
}

function upsertRenderPlan(payload) {
  if (!payload || !payload.plan || !payload.plan.id) return;
  const index = renderPlans.findIndex((item) => item.plan && item.plan.id === payload.plan.id);
  if (index < 0) renderPlans.push(payload);
  else renderPlans[index] = payload;
}

function paintPlanSelector() {
  const currentId = renderPayload && renderPayload.plan && renderPayload.plan.id;
  renderPlanSelectorEl.replaceChildren();
  const current = document.createElement("option");
  current.value = "";
  current.textContent = renderPlans.length ? T("agent.renderSelectPlan") : T("agent.renderNoPlan");
  current.selected = !currentId;
  renderPlanSelectorEl.appendChild(current);
  renderPlans.forEach((payload) => {
    const plan = payload.plan;
    const option = document.createElement("option");
    const backend = plan.backend === "lottie" ? "Lottie" : "Native";
    option.value = plan.id;
    option.textContent = `${backend} · ${plan.mode} · ${renderPayloadStatus(payload)} · ${shortDigest(plan.id)}`;
    option.selected = plan.id === currentId;
    renderPlanSelectorEl.appendChild(option);
  });
}

function renderStatusClass(status) {
  renderStatusEl.className = "render-card__status";
  if (status === "failed") renderStatusEl.classList.add("render-card__status--failed");
  else if (status && !["awaiting_approval", "approved"].includes(status)) renderStatusEl.classList.add("render-card__status--active");
}


function expectedOutputs(plan) {
  const outputs = plan && plan.artifact_contract && plan.artifact_contract.outputs;
  return Array.isArray(outputs) ? outputs : [];
}


function paintArtifacts(plan, payload) {
  renderArtifactsEl.replaceChildren();
  if (!plan) return;
  (payload && payload.artifacts || []).forEach((artifact) => {
    if (!artifact || !artifact.kind) return;
    const link = document.createElement("a");
    link.href = plan.backend === "lottie" ? lottieArtifactUrl(plan.id, projectId) : nativeArtifactUrl(plan.id, artifact.kind, projectId);
    link.textContent = artifact.label || Tf("agent.renderDownload", { kind: artifact.kind.toUpperCase() });
    link.rel = "noopener";
    renderArtifactsEl.appendChild(link);
  });
}


function updateRenderControls() {
  const plan = renderPayload && renderPayload.plan;
  const planState = renderPayload && renderPayload.state;
  const finalBlocked = renderModeEl.value === "final" && (!state || state.status !== "approved");
  renderPlanSelectorEl.disabled = renderBusy || !renderPlans.length;
  renderCreateBtn.disabled = renderBusy
    || finalBlocked
    || (renderBackendEl.value === "lottie" && renderModeEl.value !== "final");
  renderApproveBtn.disabled = renderBusy
    || !plan
    || !planState
    || planState.status !== "awaiting_approval";
}

function paintRenderCard() {
  const plan = renderPayload && renderPayload.plan;
  const planState = renderPayload && renderPayload.state;
  const status = renderPayload ? renderPayloadStatus(renderPayload) : T("agent.renderNoPlan");
  const jobError = renderPayload && renderPayload.job && renderPayload.job.error;
  const lockedProject = (plan && plan.project_id) || projectId || "—";
  const lockedScene = (plan && plan.scene_id) || sceneId;
  const lockedVersion = (plan && plan.version_id) || versionId || "—";
  renderLockEl.textContent = `${lockedProject} / ${lockedScene} / ${lockedVersion}${plan ? ` · ${plan.mode} · ${plan.backend}` : ""}`;
  renderStatusEl.textContent = status;
  renderStatusClass(status);
  renderStateEl.textContent = planState ? `${status} / r${planState.revision}` : status;
  renderReasonEl.textContent = jobError || (renderPayload && renderPayload.error) || "—";
  if (plan) {
    renderBackendEl.value = plan.backend;
    renderModeEl.value = plan.mode;
  }
  renderOutputsEl.textContent = expectedOutputs(plan).join(", ") || "—";
  paintPlanSelector();
  paintArtifacts(plan, renderPayload);
  updateRenderControls();
}

function scheduleRenderPoll() {
  if (renderPoll !== null) window.clearTimeout(renderPoll);
  renderPoll = null;
  if (!shouldPollRender(renderPayload)) return;
  renderPoll = window.setTimeout(async () => {
    renderPoll = null;
    try {
      await refreshRenderState();
      setBanner("");
    } catch (err) {
      setBanner(err.message || T("agent.failed"), true);
      scheduleRenderPoll();
    }
  }, 1500);
}


function setRenderPayload(payload) {
  renderPayload = payload;
  upsertRenderPlan(payload);
  paintRenderCard();
  scheduleRenderPoll();
}

async function refreshRenderState() {
  if (!renderPayload || !renderPayload.plan) return;
  const planId = renderPayload.plan.id;
  const payload = await fetchRenderState(projectId, planId);
  if (!renderPayload || !renderPayload.plan || renderPayload.plan.id !== planId) return;
  setRenderPayload(payload);
}

async function selectRenderPlan(plan) {
  const planId = plan && plan.plan ? plan.plan.id : plan && plan.id;
  if (!planId) return;
  setRenderBusy(true);
  try {
    setRenderPayload(await fetchRenderState(projectId, planId));
  } finally {
    setRenderBusy(false);
  }
}

async function loadRenderCard() {
  const response = await fetchRenderPlans(projectId, sceneId, versionId);
  renderPlans = response.plans || [];
  const selected = renderPlans[renderPlans.length - 1] || null;
  setRenderPayload(selected);
}

function setRenderBusy(value) {
  renderBusy = value;
  updateRenderControls();
}


function planRequest() {
  return {
    project: projectId,
    scene: sceneId,
    version: versionId,
    backend: renderBackendEl.value,
    mode: renderModeEl.value,
  };
}

async function createRenderPlan() {
  setRenderBusy(true);
  setBanner("");
  try {
    const response = await postRenderPlan(planRequest());
    setRenderPayload(response);
  } catch (err) {
    setBanner(err.message || T("agent.failed"), true);
  } finally {
    setRenderBusy(false);
  }
}


async function approveCurrentPlan() {
  if (!renderPayload) return;
  setRenderBusy(true);
  setBanner("");
  try {
    setRenderPayload(await approveRenderPlan(renderPayload.plan.id, {
      project: projectId,
      digest: renderPayload.plan.digest,
      revision: renderPayload.state.revision,
    }));
  } catch (err) {
    setBanner(err.message || T("agent.failed"), true);
  } finally {
    setRenderBusy(false);
  }
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
  const logArgs = name === "correct" && args && args.args && typeof args.args === "object" && "mask_png_base64" in args.args
    ? { ...args, args: { ...args.args, mask_png_base64: T("agent.correctionMaskHidden") } }
    : args;
  argsEl.textContent = JSON.stringify(logArgs || {}, null, 0);
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
  const total = verify.keep_total ?? (verify.keep_results || []).length;
  const failed = verify.keep_failed ?? (verify.keep_results || []).filter(r => !r.passed).length;
  const rate = total > 0 ? (total - failed) / total : 0;
  const keepPassed = total > 0 && failed === 0 && isKeepPassed({ ...verify, keep_pass_rate: rate });
  const chip = document.createElement("span");
  chip.className = "verify-chip " + (total === 0 ? "verify-chip--warn" : keepPassed ? "verify-chip--pass" : "verify-chip--fail");
  chip.textContent = total === 0
    ? T("agent.verifyNoKeep")
    : `${keepPassed ? "PASS" : "FAIL"} · keep ${Math.round(rate * CONFIDENCE_PERCENT)}% (${total}) · err ${(verify.layer_max_err_px ?? 0).toFixed(2)}px`;
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

const ALLOWED = {
  text: ["element_id", "text", "font"],
  reassign: ["from_id", "to_id", "frames"],
  mask: ["object_id", "frame", "mask_png_base64"],
  bbox: ["object_id", "frame", "bbox"],
};
const REQUIRED = {
  text: ["element_id"],
  reassign: ["from_id", "to_id", "frames"],
  mask: ALLOWED.mask,
  bbox: ALLOWED.bbox,
};
const FONT_FIELDS = ["family_guess", "weight", "size_px"];

function pick(args, allowed) {
  if (!args || typeof args !== "object" || Array.isArray(args)
      || Object.keys(args).some((key) => !allowed.includes(key))) {
    throw new Error(T("agent.correctionInvalid"));
  }
  return Object.fromEntries(allowed.filter((key) => Object.hasOwn(args, key)).map((key) => {
    const value = key === "font" ? pick(args[key], FONT_FIELDS) : args[key];
    return [key, Array.isArray(value) ? [...value] : value];
  }));
}

function correctionElement(scene, id) {
  const element = (scene.elements || []).find((el) => el.id === id);
  if (typeof id !== "string" || !element) throw new Error(T("agent.correctionInvalid"));
  return element;
}

function validateShownCorrection(op, shown, scene) {
  if (REQUIRED[op].some((key) => !Object.hasOwn(shown, key))) throw new Error(T("agent.correctionInvalid"));
  const validFrame = (value) => Number.isInteger(value) && value >= 0 && value < scene.frames;
  for (const key of ["element_id", "object_id", "from_id", "to_id"]) {
    if (Object.hasOwn(shown, key)) correctionElement(scene, shown[key]);
  }
  if (op === "text") {
    if ((!Object.hasOwn(shown, "text") && !Object.hasOwn(shown, "font"))
        || (Object.hasOwn(shown, "text") && typeof shown.text !== "string")) {
      throw new Error(T("agent.correctionInvalid"));
    }
    if (shown.font) {
      const font = shown.font;
      if (!Object.keys(font).length
          || (Object.hasOwn(font, "family_guess") && (typeof font.family_guess !== "string"
            || !/^[A-Za-z0-9 \-가-힣]{1,64}$/.test(font.family_guess.trim())))
          || (Object.hasOwn(font, "weight") && !Number.isInteger(font.weight))
          || (Object.hasOwn(font, "size_px") && (!Number.isFinite(font.size_px) || font.size_px <= 0))) {
        throw new Error(T("agent.correctionInvalid"));
      }
    }
  } else if (op === "reassign") {
    if (!Array.isArray(shown.frames) || shown.frames.length !== 2
        || !shown.frames.every(validFrame) || shown.frames[0] > shown.frames[1]) {
      throw new Error(T("agent.correctionInvalid"));
    }
  } else {
    if (!validFrame(shown.frame)) throw new Error(T("agent.correctionInvalid"));
    if (op === "mask") {
      if (typeof shown.mask_png_base64 !== "string" || !shown.mask_png_base64.startsWith("iVBORw0KGgo")) {
        throw new Error(T("agent.correctionInvalid"));
      }
    } else {
      const box = shown.bbox;
      if (!Array.isArray(box) || box.length !== 4 || !box.every(Number.isInteger)
          || box[0] < 0 || box[1] < 0 || box[2] > scene.size[0] || box[3] > scene.size[1]
          || box[0] >= box[2] || box[1] >= box[3]) {
        throw new Error(T("agent.correctionInvalid"));
      }
    }
  }
}

async function summarizeCorrectionMask(base64, scene) {
  const img = new Image();
  await new Promise((resolve, reject) => {
    img.onload = resolve;
    img.onerror = () => reject(new Error(T("agent.correctionInvalid")));
    img.src = `data:image/png;base64,${base64}`;
  });
  const width = img.naturalWidth;
  const height = img.naturalHeight;
  if (width !== scene.size[0] || height !== scene.size[1]) throw new Error(T("agent.correctionInvalid"));
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(img, 0, 0);
  const { data } = ctx.getImageData(0, 0, width, height);
  let x0 = width, y0 = height, x1 = 0, y1 = 0, pixels = 0;
  for (let offset = 0; offset < data.length; offset += 4) {
    // Opaque grayscale masks have identical canvas/OpenCV samples. The server
    // selects pixels >127; reject color/alpha masks rather than misstate them.
    if (data[offset] !== data[offset + 1] || data[offset] !== data[offset + 2] || data[offset + 3] !== 255) {
      throw new Error(T("agent.correctionInvalid"));
    }
    if (data[offset] <= 127) continue;
    const x = (offset / 4) % width;
    const y = Math.floor(offset / 4 / width);
    x0 = Math.min(x0, x); y0 = Math.min(y0, y);
    x1 = Math.max(x1, x + 1); y1 = Math.max(y1, y + 1);
    pixels += 1;
  }
  if (!pixels) throw new Error(T("agent.correctionInvalid"));
  return { bbox: [x0, y0, x1, y1], pixels };
}

function correctionPreviewLines(op, shown, scene) {
  const lines = [Tf("agent.correctionPreview", { op })];
  for (const key of ["element_id", "object_id", "from_id", "to_id"]) {
    if (!Object.hasOwn(shown, key)) continue;
    const element = correctionElement(scene, shown[key]);
    lines.push(Tf("agent.correctionElement", {
      field: key, id: element.id, current: element.canonical?.text ?? element.label ?? element.kind ?? "—",
    }));
  }
  if (Object.hasOwn(shown, "text")) {
    lines.push(Tf("agent.correctionText", {
      old: correctionElement(scene, shown.element_id).canonical?.text ?? "—", next: shown.text,
    }));
  }
  if (shown.font) {
    const old = correctionElement(scene, shown.element_id).canonical?.font || {};
    // FontGuess replaces the entire font, including defaults for omitted fields.
    const next = { family_guess: "sans-serif", weight: 400, size_px: 32, ...shown.font };
    next.family_guess = next.family_guess.trim();
    const labels = ["agent.correctionFontFamily", "agent.correctionFontWeight", "agent.correctionFontSize"];
    FONT_FIELDS.forEach((field, index) => {
      lines.push(Tf("agent.correctionFont", { field: T(labels[index]), old: old[field] ?? "—", next: next[field] }));
    });
  }
  if (shown.frames) lines.push(Tf("agent.correctionFrames", { start: shown.frames[0], end: shown.frames[1] }));
  if (Object.hasOwn(shown, "frame")) lines.push(Tf("agent.correctionFrame", { frame: shown.frame }));
  if (shown.bbox) lines.push(Tf("agent.correctionBbox", { bbox: `[${shown.bbox.join(", ")}]` }));
  return lines;
}

async function paintCorrection(correction) {
  try {
    const { op, args } = correction;
    if (!Object.hasOwn(ALLOWED, op)) throw new Error(T("agent.correctionInvalid"));
    const shown = pick(args, ALLOWED[op]);
    const scene = state.scene;
    const previewVersion = versionId;
    const previewSceneId = sceneId;
    validateShownCorrection(op, shown, scene);
    const lines = correctionPreviewLines(op, shown, scene);
    if (op === "mask") {
      const summary = await summarizeCorrectionMask(shown.mask_png_base64, scene);
      lines.push(Tf("agent.correctionMask", { bbox: `[${summary.bbox.join(", ")}]`, pixels: summary.pixels }));
    }
    appendAgent(lines.join("\n"));
    appendConfirmButton(() => confirmCorrection(op, shown, previewVersion, previewSceneId));
  } catch (err) {
    setBanner(T("agent.correctionInvalid"), true);
  }
}

function appendConfirmButton(onConfirm = () => runConfirm(false)) {
  const confirm = document.createElement("button");
  confirm.className = "btn btn--primary";
  confirm.type = "button";
  confirm.textContent = T("agent.confirm");
  confirm.addEventListener("click", async () => {
    if (confirm.disabled) return;
    confirm.disabled = true;
    confirm.disabled = await onConfirm() === true;
  });
  logEl.appendChild(confirm);
  logEl.scrollTop = logEl.scrollHeight;
}


function paintPending(turn) {
  const preparedRender = payloadOf(turn.results, "render_plan");
  if (preparedRender) {
    selectRenderPlan(preparedRender).catch((err) => {
      setBanner(err.message || T("agent.failed"), true);
    });
    return;
  }
  const keepChange = payloadOf(turn.results, "keep_change");
  if (keepChange) {
    const preset = keepChange.preset != null;
    if (!preset && !Array.isArray(keepChange.targets)) {
      setBanner(T("agent.keepPreviewChanged"), true);
      return;
    }
    const changes = preset ? [] : (state.scene.constraints || [])
      .filter((c) => keepChange.targets.some((target) => c.pred === target || c.pred.includes(target)))
      .map((c) => ({ pred: c.pred, keep: keepChange.on }));
    const preview = preset
      ? Tf("agent.keepPresetPreview", { preset: keepChange.preset })
      : Tf(keepChange.on ? "agent.keepOnPreview" : "agent.keepOffPreview", { n: keepChange.matched });
    const examples = !preset && Array.isArray(keepChange.examples) ? keepChange.examples.slice(0, 5) : [];
    appendAgent(examples.length
      ? `${preview}\n${Tf("agent.keepExamplesPreview", { examples: examples.join("; ") })}`
      : preview);
    if (!preset && changes.length !== keepChange.matched) {
      setBanner(T("agent.keepPreviewChanged"), true);
      return;
    }
    appendConfirmButton(() => confirmKeepChange(keepChange, changes));
    return;
  }
  const correction = payloadOf(turn.results, "correction");
  if (correction) {
    return paintCorrection(correction);
  }
  pendingIntent = payloadOf(turn.results, "intent");
  const plan = payloadOf(turn.results, "plan");
  const needsChoice = Boolean(turn.needs_choice);
  const needsConfirm = Boolean(turn.needs_confirm);
  if (needsChoice) appendChoices(plan);
  if (needsConfirm) appendConfirmButton();
}

async function confirmKeepChange(keepChange, changes) {
  setBanner("");
  try {
    const res = await postKeep(projectId, sceneId, changes, T("agent.keepChangeNote"), keepChange.preset);
    await refreshAfterEdit(res.version.id);
    appendAgent(T("agent.applied"));
    return true;
  } catch (err) {
    setBanner(err.message || T("agent.failed"), true);
    return false;
  }
}

async function pollCorrection(previewSceneId) {
  while (true) {
    const job = await fetchReviewJob(projectId, previewSceneId);
    if (job.status === "done" && job.version) return job;
    if (job.status === "error") throw new Error(job.error || T("review.error"));
    if (!["running", "queued"].includes(job.status)) throw new Error(T("agent.failed"));
    await new Promise((resolve) => setTimeout(resolve, CORRECTION_POLL_INTERVAL_MS));
  }
}

async function confirmCorrection(op, shown, previewVersion, previewSceneId) {
  setBanner(Tf("review.running", { op }));
  try {
    if (sceneId !== previewSceneId) throw new Error(T("agent.correctionInvalid"));
    await postCorrect(projectId, sceneId, op, { ...shown, version: previewVersion });
    const job = await pollCorrection(previewSceneId);
    await refreshAfterEdit(job.version);
    setBanner("");
    appendAgent(T("agent.applied"));
    return true;
  } catch (err) {
    setBanner(err.message || T("agent.failed"), true);
    return false;
  }
}

function confirmEditBody(withChoices) {
  return {
    project: projectId,
    scene: sceneId,
    v: versionId,
    prompt: pendingPrompt,
    attachment: pendingAttachment,
    element: selectedId,
    intent: pendingIntent,
    confirm: true,
    choices: withChoices ? editChoices() : {},
  };
}

async function applyConfirmedEdit(res) {
  if (res.status === "needs_choice") {
    pendingIntent = res.intent || pendingIntent;
    appendChoices(res.plan);
    return false;
  }
  pendingIntent = null;
  const editDone = res.status === "done" && res.version;
  if (editDone) {
    pendingAttachment = pendingAttachmentFile = null;
    attachInput.value = "";
    document.getElementById("agent-attach-name").textContent = "";
    appendVerify(res.verify);
    appendAgent(res.summary || EDIT_APPLIED);
    await refreshAfterEdit(res.version.id);
    return true;
  }
  const errorMessage = res.error === "attachment_required" ? T("agent.attachmentRequired")
    : res.error === "invalid_glb" ? T("agent.invalidGlb")
    : res.error === "unsafe_svg" || res.error === "invalid_svg" ? T("agent.invalidSvg")
    : (res.error || T("agent.failed"));
  setBanner(errorMessage, true);
  return false;
}

async function runConfirm(withChoices) {
  setBanner("");
  try {
    const res = await postEdit(confirmEditBody(withChoices));
    return await applyConfirmedEdit(res);
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
  await aeCard.refresh();
  await loadRenderCard();
}

function paintToolCalls(turn) {
  (turn.tool_calls || []).forEach((tc, i) => {
    appendToolCall(tc.name, tc.arguments, (turn.results && turn.results[i]) || { ok: false, message: "" });
  });
}

function verifyFromTurn(turn) {
  return (turn.results || []).map((r) => r.payload && r.payload.verify).find(Boolean);
}

function paintTurn(turn, actionable = true) {
  paintToolCalls(turn);
  if (turn.reply) appendAgent(turn.reply);
  appendVerify(verifyFromTurn(turn));
  const isPending = turn.status === "pending";
  const isError = turn.status === "error";
  if (isPending && actionable) paintPending(turn);
  if (isError) setBanner(turn.reply || T("agent.failed"), true);
}

async function loadHistory() {
  const history = await fetchAgentHistory(projectId, sceneId, null, 50);
  (history.turns || []).forEach((record) => {
    appendUser(record.user || "");
    if (record.actionable) pendingPrompt = record.user || "";
    paintTurn(record.turn || {}, record.actionable !== false);
  });
}

function visibleText() {
  const values = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode() && values.join(" ").length < 12000) {
    const parent = walker.currentNode.parentElement;
    if (!parent || parent.closest("[data-ai-private],script,style,[hidden]") || getComputedStyle(parent).visibility === "hidden") continue;
    const text = walker.currentNode.textContent.trim();
    if (text) values.push(text);
  }
  return values.join(" ").slice(0, 12000);
}

function visibleInputs() {
  return [...document.querySelectorAll("input,textarea,select")]
    .filter((el) => !el.closest("[data-ai-private]") && !["password", "file"].includes(el.type) && el.offsetParent !== null)
    .map((el) => ({ id: el.id || null, value: String(el.value || "").slice(0, 1000) }));
}

async function previewForAI(img, kind) {
  const response = await fetch(img.currentSrc || img.src, { cache: "no-store" });
  if (!response.ok) throw new Error(`preview ${kind} unavailable`);
  const bitmap = await createImageBitmap(await response.blob());
  const scale = Math.min(1, 768 / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.72));
  const data = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
  return { kind, mime: "image/jpeg", data };
}

async function readChatAttachment(file) {
  const data = await readFileAsDataUrl(file);
  const mime = /\.glb$/i.test(file.name) ? "model/gltf-binary"
    : /\.svg$/i.test(file.name) ? "image/svg+xml" : null;
  return mime ? data.replace(/^data:[^,]*,/, `data:${mime};base64,`) : data;
}

function attachmentMeta() {
  return pendingAttachmentFile ? { name: pendingAttachmentFile.name, type: pendingAttachmentFile.type, size: pendingAttachmentFile.size } : null;
}

async function buildUIContext() {
  const summary = {
    path: location.pathname,
    language: document.documentElement.lang,
    project: projectId,
    scene: sceneId,
    version: versionId,
    frame,
    selected_element: selectedId,
    attachment: attachmentMeta(),
    workflow: state && state.status,
    inputs: visibleInputs(),
    render: renderPayload ? { plan: renderPayload.plan, status: renderPayloadStatus(renderPayload) } : null,
    visible_text: visibleText(),
  };
  const images = await Promise.all([previewForAI(orig, "original"), previewForAI(recon, "reconstruction")]);
  return { schema: "keepframe.ui-context/1", summary, images };
}

async function send(message) {
  const text = (message || inputEl.value || "").trim();
  if (!text || !projectId) return;
  inputEl.value = "";
  pendingPrompt = text;
  appendUser(text);
  setBanner("화면 읽는 중");
  sendBtn.disabled = true;
  try {
    const uiContext = await buildUIContext();
    setBanner(T("agent.thinking"));
    const turn = await postAgent({ project: projectId, scene: sceneId, v: versionId, message: text, ui_context: uiContext });
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
sceneSelect.addEventListener("change", () => {
  location.href = `/agent?project=${encodeURIComponent(projectId)}&scene=${encodeURIComponent(sceneSelect.value)}`;
});

document.getElementById("agent-attach-btn").addEventListener("click", () => attachInput.click());
attachInput.addEventListener("change", async () => {
  const file = attachInput.files[0] || null;
  try {
    const attachment = file ? await readChatAttachment(file) : null;
    pendingAttachmentFile = file;
    pendingAttachment = attachment;
  } catch (err) {
    pendingAttachment = pendingAttachmentFile = null;
    setBanner(T("agent.failed"), true);
  }
  document.getElementById("agent-attach-name").textContent = pendingAttachmentFile ? pendingAttachmentFile.name : "";
});
sendBtn.addEventListener("click", () => send());
inputEl.addEventListener("keydown", (e) => {
  const isSendChord = e.key === "Enter" && (e.ctrlKey || e.metaKey);
  if (isSendChord) send();
});
document.querySelectorAll("#agent-tools .tool").forEach((btn) => {
  btn.addEventListener("click", () => fillToolPrompt(btn.dataset.tool));
});
renderBackendEl.addEventListener("change", () => {
  if (renderBackendEl.value === "lottie") renderModeEl.value = "final";
  updateRenderControls();
});
renderModeEl.addEventListener("change", updateRenderControls);
renderPlanSelectorEl.addEventListener("change", () => {
  const selected = renderPlans.find((payload) => payload.plan && payload.plan.id === renderPlanSelectorEl.value);
  if (!selected) {
    renderPayload = null;
    paintRenderCard();
    scheduleRenderPoll();
    return;
  }
  selectRenderPlan(selected).catch((err) => {
    setBanner(err.message || T("agent.failed"), true);
  });
});
renderCreateBtn.addEventListener("click", createRenderPlan);
renderApproveBtn.addEventListener("click", approveCurrentPlan);
playBtn.addEventListener("click", () => setPlaying(!transport.isPlaying()));
document.getElementById("agent-prev").addEventListener("click", () => { setPlaying(false); setFrame(frame - 1); });
document.getElementById("agent-next").addEventListener("click", () => { setPlaying(false); setFrame(frame + 1); });
window.addEventListener("keepframe:lang", () => {
  if (state) renderElements();
  paintRenderCard();
});

const aeCard = initAECard({projectId, getSceneId: () => sceneId, getVersionId: () => versionId});

if (!projectId) showMissingProject();
else {
  loadState()
    .then(() => aeCard.refresh())
    .then(loadHistory)
    .then(loadRenderCard)
    .catch((err) => {
      setBanner(err.message || T("agent.failed"), true);
    });
}
