import {
  aeArtifactUrl,
  nativeArtifactUrl,
  lottieArtifactUrl,
  approveRenderPlan,
  fetchAeStatus,
  fetchAgentHistory,
  postAePairing,
  fetchRenderPlans,
  fetchRenderState,
  fetchReviewJob,
  fetchReviewState,
  postAeControl,
  postAgent,
  postCorrect,
  postEdit,
  postKeep,
  postRenderPlan,
  reviewAssetUrl,
} from "/static/js/api.js?v=20261004a";
import { T, Tf } from "/static/js/i18n.js?v=20261004a";
import { readFileAsDataUrl } from "/static/js/files.js?v=20261004a";
import {
  createPreviewCache,
  createFrameTransport,
} from "/static/js/playback.js?v=20261004a";

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
let renderDraft = null;
let renderPlans = [];
let pairingDetails = null;
let aeStatus = null;
let renderPoll = null;
let aeStatusPoll = null;
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
const renderDirectionEl = document.getElementById("render-direction");
const renderPlanSelectorEl = document.getElementById("render-plan-selector");
const renderCreateBtn = document.getElementById("render-create");
const renderPairBtn = document.getElementById("render-pair");
const renderApproveBtn = document.getElementById("render-approve");
const renderPairingEl = document.getElementById("render-pairing");
const renderPairingCodeEl = document.getElementById("render-pairing-code");
const renderPairingExpiryEl = document.getElementById("render-pairing-expiry");
const renderPairingRelayEl = document.getElementById("render-pairing-relay");
const renderPairingCommandEl = document.getElementById("render-pairing-command");
const renderStatusEl = document.getElementById("render-status");
const renderLockEl = document.getElementById("render-lock");
const renderConnectorEl = document.getElementById("render-connector");
const renderAeEl = document.getElementById("render-ae");
const renderCapabilityEl = document.getElementById("render-capability");
const renderOutputsEl = document.getElementById("render-outputs");
const renderStateEl = document.getElementById("render-state");
const renderReasonEl = document.getElementById("render-reason");
const renderSubstitutionsEl = document.getElementById("render-substitutions");
const renderSubstitutionRowsEl = document.getElementById("render-substitution-rows");
const renderSubstitutionAckEl = document.getElementById("render-substitution-ack");
const renderCheckpointsEl = document.getElementById("render-checkpoints");
const renderIterationEl = document.getElementById("render-iteration");
const renderArtifactsEl = document.getElementById("render-artifacts");
const renderStopBtn = document.getElementById("render-stop");
const renderContinueBtn = document.getElementById("render-continue");
const renderManualBtn = document.getElementById("render-manual");
const renderSyncBtn = document.getElementById("render-sync");
const renderFinalizeBtn = document.getElementById("render-finalize");

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

function isPaused(status) {
  return typeof status === "string" && status.startsWith("paused:");
}

function shouldPollRender(payload) {
  if (!payload || !payload.plan) return false;
  if (payload.plan.backend !== "after_effects") {
    return ["queued", "running"].includes(payload.status);
  }
  return !isPaused(payload.status) && !["done", "failed", "awaiting_approval", "approved"].includes(payload.status);
}

function renderPayloadStatus(payload) {
  return (payload && (
    payload.status
    || (payload.session && payload.session.status)
    || (payload.state && payload.state.status)
  )) || "—";
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
  current.textContent = renderDraft ? "AE · draft" : (renderPlans.length ? "계획 선택" : "계획 없음");
  current.selected = !currentId;
  renderPlanSelectorEl.appendChild(current);
  renderPlans.forEach((payload) => {
    const plan = payload.plan;
    const option = document.createElement("option");
    const backend = plan.backend === "after_effects" ? "AE" : (plan.backend === "lottie" ? "Lottie" : "Native");
    option.value = plan.id;
    option.textContent = `${backend} · ${plan.mode} · ${renderPayloadStatus(payload)} · ${shortDigest(plan.id)}`;
    option.selected = plan.id === currentId;
    renderPlanSelectorEl.appendChild(option);
  });
}

function renderStatusClass(status) {
  renderStatusEl.className = "render-card__status";
  if (status === "failed" || isPaused(status)) renderStatusEl.classList.add("render-card__status--failed");
  else if (status && !["awaiting_approval", "approved"].includes(status)) renderStatusEl.classList.add("render-card__status--active");
}

function paintConnectorStatus() {
  if (!aeStatus) {
    renderConnectorEl.textContent = "미연결";
    renderAeEl.textContent = "—";
    renderCapabilityEl.textContent = "—";
    return;
  }
  renderConnectorEl.textContent = aeStatus.paired ? "연결됨" : "미연결";
  const aeLabel = aeStatus.ae_ready ? (aeStatus.ae_version || "AE") : "준비 안 됨";
  renderAeEl.textContent = `${aeLabel} · ${aeStatus.project_open ? "project open" : "project closed"}`;
  renderCapabilityEl.textContent = shortDigest(aeStatus.capability_hash);
}

function paintPairingDetails() {
  renderPairBtn.textContent = (aeStatus && aeStatus.paired) || pairingDetails ? "Re-pair" : "Pair";
  renderPairingEl.hidden = !pairingDetails;
  if (!pairingDetails) return;
  const relay = pairingDetails.relay_url;
  renderPairingCodeEl.textContent = pairingDetails.code || "—";
  renderPairingExpiryEl.textContent = pairingDetails.expires_at || "—";
  renderPairingRelayEl.textContent = relay || "—";
  renderPairingCommandEl.textContent = relay
    ? `keepframe ae-connect --url ${relay}`
    : "—";
}


function expectedOutputs(plan, draft) {
  if (draft && Array.isArray(draft.expected_outputs)) return draft.expected_outputs;
  const outputs = plan && plan.artifact_contract && plan.artifact_contract.outputs;
  return Array.isArray(outputs) ? outputs : [];
}

function substitutionText(value) {
  if (!Array.isArray(value) || !value.length) return "—";
  return value.map((item) => typeof item === "string" ? item : JSON.stringify(item)).join(", ");
}

function paintSubstitutions() {
  const draft = renderBackendEl.value === "after_effects" ? renderDraft : null;
  const plan = renderPayload && renderPayload.plan;
  const selectedPlan = renderBackendEl.value === "after_effects" ? plan : null;
  const substitutions = draft ? draft.substitutions : ((selectedPlan && selectedPlan.substitutions) || []);
  const issues = draft ? draft.compatibility_issues : [];
  renderSubstitutionRowsEl.replaceChildren();
  substitutions.forEach((substitution) => {
    const issue = issues.find((item) => item.source_element_id === substitution.source_element_id);
    const row = document.createElement("tr");
    [substitution.source_element_id, (issue && issue.reason) || substitution.reason || "—", `${substitutionText(substitution.proposed_layers)} / ${substitutionText(substitution.proposed_effects)}`]
      .forEach((text) => {
        const cell = document.createElement("td");
        cell.textContent = text;
        row.appendChild(cell);
      });
    renderSubstitutionRowsEl.appendChild(row);
  });
  renderSubstitutionsEl.hidden = substitutions.length === 0;
  renderSubstitutionAckEl.disabled = !draft;
  if (!draft) renderSubstitutionAckEl.checked = Boolean(selectedPlan && selectedPlan.substitutions_acknowledged);
}

function paintCheckpoints(session) {
  renderCheckpointsEl.replaceChildren();
  const checkpoints = (session && session.checkpoints) || [];
  if (!checkpoints.length) {
    const empty = document.createElement("span");
    empty.className = "muted";
    empty.textContent = "없음";
    renderCheckpointsEl.appendChild(empty);
    return;
  }
  checkpoints.forEach((checkpoint) => {
    const label = document.createElement("label");
    label.className = `render-checkpoint${checkpoint.passed && checkpoint.lineage_valid ? "" : " render-checkpoint--failed"}`;
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "render-checkpoint";
    input.value = String(checkpoint.index);
    input.checked = session.selected_checkpoint === checkpoint.index;
    input.disabled = renderBusy || !isPaused(session.status) || !checkpoint.passed || !checkpoint.lineage_valid;
    input.addEventListener("change", () => selectCheckpoint(checkpoint.index));
    const text = document.createElement("span");
    text.textContent = `#${checkpoint.index} ${checkpoint.provenance} · ${checkpoint.passed ? "PASS" : "FAIL"}`;
    label.append(input, text);
    renderCheckpointsEl.appendChild(label);
  });
}

function paintArtifacts(plan, session, payload) {
  renderArtifactsEl.replaceChildren();
  if (!plan) return;
  if (plan.backend !== "after_effects") {
    (payload && payload.artifacts || []).forEach((artifact) => {
      if (!artifact || !artifact.kind) return;
      const link = document.createElement("a");
      link.href = plan.backend === "lottie" ? lottieArtifactUrl(plan.id, projectId) : nativeArtifactUrl(plan.id, artifact.kind, projectId);
      link.textContent = artifact.label || `${artifact.kind.toUpperCase()} 다운로드`;
      link.rel = "noopener";
      renderArtifactsEl.appendChild(link);
    });
    return;
  }
  if (!session) return;
  Object.entries(session.final_artifact_ids || {}).forEach(([kind, artifactId]) => {
    const link = document.createElement("a");
    link.href = aeArtifactUrl(artifactId, projectId, plan.id);
    link.textContent = `${kind.toUpperCase()} 다운로드`;
    link.rel = "noopener";
    renderArtifactsEl.appendChild(link);
  });
}


function updateRenderControls() {
  const plan = renderPayload && renderPayload.plan;
  const planState = renderPayload && renderPayload.state;
  const session = renderPayload && renderPayload.session;
  const aeReady = Boolean(
    aeStatus
    && aeStatus.relay_configured
    && aeStatus.paired
    && aeStatus.ae_ready
    && aeStatus.capability_hash
  );
  const planCompatible = aeReady
    && (!plan || plan.backend !== "after_effects" || plan.capability_hash === aeStatus.capability_hash);
  const creationCapabilityHash = renderDraft
    ? renderDraft.capability_hash
    : (
      renderModeEl.value === "final"
      && plan
      && plan.backend === "after_effects"
        ? plan.capability_hash
        : null
    );
  const aeCreateReady = aeReady
    && (!creationCapabilityHash || creationCapabilityHash === aeStatus.capability_hash);
  const finalDraftBlocked = renderModeEl.value === "final" && (!state || state.status !== "approved");
  const finalPlanReady = Boolean(
    plan
    && plan.backend === "after_effects"
    && plan.mode === "final"
    && planState
    && planState.status === "approved"
    && planState.execution_id
    && session
    && isPaused(session.status)
    && session.selected_checkpoint != null
    && plan.predecessor_id
    && plan.predecessor_checkpoint === session.selected_checkpoint
  );
  renderDirectionEl.disabled = renderBusy || renderBackendEl.value !== "after_effects";
  renderPlanSelectorEl.disabled = renderBusy || (!renderPlans.length && !renderDraft);
  renderPairBtn.disabled = renderBusy || !projectId || Boolean(aeStatus && !aeStatus.relay_configured);
  renderCreateBtn.disabled = renderBusy
    || finalDraftBlocked
    || (renderBackendEl.value === "lottie" && renderModeEl.value !== "final")
    || (renderBackendEl.value === "after_effects" && !aeCreateReady)
    || (renderBackendEl.value === "after_effects" && Boolean(renderDraft) && !renderSubstitutionAckEl.checked);
  renderApproveBtn.disabled = renderBusy
    || !plan
    || !planState
    || planState.status !== "awaiting_approval"
    || (plan.backend === "after_effects" && !planCompatible)
    || ((plan.substitutions || []).length > 0 && !plan.substitutions_acknowledged);
  renderStopBtn.disabled = renderBusy || !session || !["baseline", "iterating"].includes(session.status);
  renderContinueBtn.disabled = renderBusy || !session || !isPaused(session.status);
  renderManualBtn.disabled = renderBusy || !session || !isPaused(session.status);
  renderSyncBtn.disabled = renderBusy || !session || session.status !== "manual_edit";
  renderFinalizeBtn.disabled = renderBusy || !finalPlanReady;
}

function paintRenderCard() {
  const plan = renderPayload && renderPayload.plan;
  const planState = renderPayload && renderPayload.state;
  const session = renderPayload && renderPayload.session;
  const locked = plan || renderDraft;
  const status = renderPayload
    ? renderPayloadStatus(renderPayload)
    : (renderDraft ? "awaiting_substitution_acknowledgement" : "계획 없음");
  const jobError = renderPayload && renderPayload.job && renderPayload.job.error;
  const lockedProject = (locked && locked.project_id) || projectId || "—";
  const lockedScene = (locked && locked.scene_id) || sceneId;
  const lockedVersion = (locked && locked.version_id) || versionId || "—";
  renderLockEl.textContent = `${lockedProject} / ${lockedScene} / ${lockedVersion}${plan ? ` · ${plan.mode} · ${plan.backend}` : ""}`;
  renderStatusEl.textContent = status;
  renderStatusClass(status);
  renderStateEl.textContent = planState
    ? `${session ? session.status : status} / r${session ? session.revision : planState.revision}`
    : status;
  renderIterationEl.textContent = `iteration ${session ? Math.max(0, session.checkpoints.length - 1) : "—"}`;
  renderReasonEl.textContent = (
    jobError
    || (session && session.pause_detail)
    || (session && session.reason)
    || (renderPayload && renderPayload.error)
    || "—"
  );
  if (plan) {
    renderBackendEl.value = plan.backend;
    renderModeEl.value = plan.mode;
    renderDirectionEl.value = plan.direction || "";
  } else if (renderDraft) {
    renderBackendEl.value = "after_effects";
    renderModeEl.value = renderDraft.mode;
    renderDirectionEl.value = renderDraft.direction || "";
  }
  renderOutputsEl.textContent = expectedOutputs(plan, renderBackendEl.value === "after_effects" ? renderDraft : null).join(", ") || "—";
  paintConnectorStatus();
  paintPairingDetails();
  paintPlanSelector();
  paintSubstitutions();
  paintCheckpoints(session);
  paintArtifacts(plan, session, renderPayload);
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

function scheduleAeStatusPoll() {
  if (aeStatusPoll !== null) window.clearTimeout(aeStatusPoll);
  aeStatusPoll = null;
  const pairingPending = pairingDetails
    && Number(pairingDetails.expires_at) * 1000 > Date.now();
  if (
    !(pairingPending || (aeStatus && aeStatus.paired))
    || (aeStatus && aeStatus.paired && aeStatus.ae_ready)
  ) return;
  aeStatusPoll = window.setTimeout(async () => {
    aeStatusPoll = null;
    try {
      aeStatus = await fetchAeStatus(projectId);
      paintRenderCard();
    } catch (err) {
      // The pairing code remains visible; retry until it expires.
    }
    scheduleAeStatusPoll();
  }, 1500);
}

function setRenderPayload(payload) {
  renderPayload = payload;
  renderDraft = null;
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
  try {
    aeStatus = await fetchAeStatus(projectId);
  } catch (err) {
    aeStatus = null;
  }
  const response = await fetchRenderPlans(projectId, sceneId, versionId);
  renderPlans = response.plans || [];
  let selected = null;
  for (let index = renderPlans.length - 1; index >= 0; index -= 1) {
    const candidate = renderPlans[index];
    const status = renderPayloadStatus(candidate);
    if (
      candidate.plan
      && candidate.plan.backend === "after_effects"
      && candidate.session
      && !["done", "failed"].includes(status)
    ) {
      selected = candidate;
      break;
    }
  }
  if (!selected) selected = renderPlans[renderPlans.length - 1] || null;
  if (selected) setRenderPayload(selected);
  else if (renderDraft) {
    paintRenderCard();
  } else {
    renderPayload = null;
    paintRenderCard();
  }
  scheduleAeStatusPoll();
}

function setRenderBusy(value) {
  renderBusy = value;
  updateRenderControls();
}

function selectedPreviewPayload() {
  if (!renderPayload || !renderPayload.plan) return null;
  if (renderPayload.plan.backend !== "after_effects") return null;
  return renderPayload;
}

function planRequest() {
  const backend = renderBackendEl.value;
  const mode = renderModeEl.value;
  const request = {
    project: projectId,
    scene: sceneId,
    version: versionId,
    backend,
    mode,
    direction: backend === "after_effects" ? (renderDirectionEl.value.trim() || null) : null,
  };
  if (backend === "after_effects" && mode === "final") {
    const preview = selectedPreviewPayload();
    const checkpoint = preview && preview.session && preview.session.selected_checkpoint;
    const record = preview && preview.session && preview.session.checkpoints.find((item) => item.index === checkpoint);
    if (!preview || preview.plan.mode !== "preview" || checkpoint == null || !record || !record.context_digest) {
      throw new Error("선택된 PASS 미리보기 체크포인트가 필요합니다.");
    }
    Object.assign(request, {
      predecessor_id: preview.plan.id,
      predecessor_digest: preview.plan.digest,
      predecessor_checkpoint: checkpoint,
      predecessor_checkpoint_digest: record.context_digest,
    });
  }
  if (backend === "after_effects" && renderDraft && renderSubstitutionAckEl.checked) {
    request.substitutions = renderDraft.substitutions;
    request.compatibility_issues = renderDraft.compatibility_issues;
    request.substitutions_acknowledged = true;
  }
  return request;
}

async function createRenderPlan() {
  setRenderBusy(true);
  setBanner("");
  try {
    const response = await postRenderPlan(planRequest());
    if (response.draft) {
      showRenderDraft(response.draft);
      return;
    }
    setRenderPayload(response);
  } catch (err) {
    setBanner(err.message || T("agent.failed"), true);
  } finally {
    setRenderBusy(false);
  }
}

async function pairAe() {
  setRenderBusy(true);
  setBanner("");
  try {
    pairingDetails = await postAePairing(projectId, {});
    const draft = renderDraft;
    await loadRenderCard();
    if (draft && (!renderPayload || renderPayload.plan.backend !== "after_effects")) {
      renderPayload = null;
      renderDraft = draft;
      paintRenderCard();
    }
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

async function controlAe(action, extra = {}) {
  if (!renderPayload || !renderPayload.session) return null;
  const body = {
    project: projectId,
    plan: renderPayload.plan.mode === "final"
      ? renderPayload.plan.predecessor_id
      : renderPayload.plan.id,
    revision: renderPayload.session.revision,
    ...extra,
  };
  const response = await postAeControl(renderPayload.session.id, action, body);
  setRenderPayload(response);
  return response;
}

async function runAeControl(action, extra = {}) {
  setRenderBusy(true);
  setBanner("");
  try {
    await controlAe(action, extra);
  } catch (err) {
    setBanner(err.message || T("agent.failed"), true);
  } finally {
    setRenderBusy(false);
  }
}

async function selectCheckpoint(checkpoint) {
  return runAeControl("select-checkpoint", { checkpoint });
}

async function finalizeCheckpoint() {
  const plan = renderPayload && renderPayload.plan;
  const planState = renderPayload && renderPayload.state;
  const session = renderPayload && renderPayload.session;
  if (
    !plan
    || plan.backend !== "after_effects"
    || plan.mode !== "final"
    || !planState
    || planState.status !== "approved"
    || !planState.execution_id
    || !session
    || !isPaused(session.status)
    || session.selected_checkpoint == null
    || plan.predecessor_checkpoint !== session.selected_checkpoint
    || !plan.predecessor_id
  ) return;
  const finalPlanId = plan.id;
  const finalPlanDigest = plan.digest;
  setRenderBusy(true);
  setBanner("");
  try {
    await postAeControl(session.id, "finalize", {
      project: projectId,
      plan: plan.predecessor_id,
      revision: session.revision,
      checkpoint: session.selected_checkpoint,
      final_plan_id: finalPlanId,
      execution_id: planState.execution_id,
    });
    const finalized = await fetchRenderState(projectId, finalPlanId);
    if (!finalized.plan || finalized.plan.digest !== finalPlanDigest) {
      throw new Error("최종 계획이 변경되었습니다.");
    }
    setRenderPayload(finalized);
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

function isRenderDraftShape(value) {
  return Boolean(
    value
    && (
      !value.id
      || Object.prototype.hasOwnProperty.call(value, "compatibility_issues")
      || (
        Object.prototype.hasOwnProperty.call(value, "substitutions")
        && !value.digest
      )
    )
  );
}

function showRenderDraft(draft) {
  renderPayload = null;
  renderDraft = draft;
  renderSubstitutionAckEl.checked = false;
  paintRenderCard();
}

function paintPending(turn) {
  const preparedRender = payloadOf(turn.results, "render_plan");
  if (preparedRender) {
    if (isRenderDraftShape(preparedRender)) {
      showRenderDraft(preparedRender);
      return;
    }
    selectRenderPlan(preparedRender).catch((err) => {
      setBanner(err.message || T("agent.failed"), true);
    });
    return;
  }
  const keepChange = payloadOf(turn.results, "keep_change");
  if (keepChange) {
    const preset = keepChange.preset != null;
    const changes = preset ? [] : (state.scene.constraints || [])
      .filter((c) => keepChange.targets.some((target) => c.pred === target || c.pred.includes(target)))
      .map((c) => ({ pred: c.pred, keep: keepChange.on }));
    appendAgent(preset
      ? Tf("agent.keepPresetPreview", { preset: keepChange.preset })
      : Tf(keepChange.on ? "agent.keepOnPreview" : "agent.keepOffPreview", { n: keepChange.matched }));
    if (!preset && changes.length !== keepChange.matched) {
      setBanner(T("agent.keepPreviewChanged"), true);
      return;
    }
    appendConfirmButton(() => confirmKeepChange(keepChange, changes));
    return;
  }
  const correction = payloadOf(turn.results, "correction");
  if (correction) {
    const previewVersion = versionId;
    appendAgent(Tf("agent.correctionPreview", { op: correction.op }));
    appendConfirmButton(() => confirmCorrection(correction, previewVersion));
    return;
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
    const res = await postKeep(projectId, sceneId, changes, T("review.keepSave"), keepChange.preset);
    await refreshAfterEdit(res.version.id);
    appendAgent(T("agent.applied"));
    return true;
  } catch (err) {
    setBanner(err.message || T("agent.failed"), true);
    return false;
  }
}

async function pollCorrection() {
  while (true) {
    const job = await fetchReviewJob(projectId, sceneId);
    if (job.status === "done" && job.version) return job;
    if (job.status === "error") throw new Error(job.error || T("review.error"));
    if (!["running", "queued"].includes(job.status)) throw new Error(T("agent.failed"));
    await new Promise((resolve) => setTimeout(resolve, CORRECTION_POLL_INTERVAL_MS));
  }
}

async function confirmCorrection(correction, previewVersion) {
  setBanner(Tf("review.running", { op: correction.op }));
  try {
    await postCorrect(projectId, sceneId, correction.op, { ...correction.args, version: previewVersion });
    const job = await pollCorrection();
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
  setBanner(res.error === "attachment_required" ? T("agent.attachmentRequired") : (res.error || T("agent.failed")), true);
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
  renderDraft = null;
  await loadState();
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
    const attachment = file ? await readFileAsDataUrl(file) : null;
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
  paintSubstitutions();
  updateRenderControls();
});
renderModeEl.addEventListener("change", updateRenderControls);
renderSubstitutionAckEl.addEventListener("change", updateRenderControls);
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
renderPairBtn.addEventListener("click", pairAe);
renderCreateBtn.addEventListener("click", createRenderPlan);
renderApproveBtn.addEventListener("click", approveCurrentPlan);
renderStopBtn.addEventListener("click", () => runAeControl("stop"));
renderContinueBtn.addEventListener("click", () => runAeControl("continue"));
renderManualBtn.addEventListener("click", () => runAeControl("begin-manual"));
renderSyncBtn.addEventListener("click", () => runAeControl("sync-manual"));
renderFinalizeBtn.addEventListener("click", finalizeCheckpoint);
playBtn.addEventListener("click", () => setPlaying(!transport.isPlaying()));
document.getElementById("agent-prev").addEventListener("click", () => { setPlaying(false); setFrame(frame - 1); });
document.getElementById("agent-next").addEventListener("click", () => { setPlaying(false); setFrame(frame + 1); });
window.addEventListener("keepframe:lang", () => {
  if (state) renderElements();
});

if (!projectId) showMissingProject();
else {
  loadState()
    .then(loadHistory)
    .then(loadRenderCard)
    .catch((err) => {
      setBanner(err.message || T("agent.failed"), true);
    });
}
