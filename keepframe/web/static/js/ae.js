import { api } from "/static/js/api.js?v=20261006j";
import { T, Tf } from "/static/js/i18n.js?v=20261006j";

const JOB_HISTORY_LIMIT = 4;

async function copyText(text) {
  if (window.isSecureContext && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {}
  }
  const focused = document.activeElement;
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.readOnly = true;
  textarea.style.cssText = "position:fixed;left:-9999px;top:0";
  document.body.append(textarea);
  try {
    textarea.select();
    return document.execCommand("copy");
  } catch { return false; }
  finally {
    textarea.remove();
    focused?.focus({preventScroll: true});
  }
}

export function initAECard({projectId, getSceneId, getVersionId}) {
  const el = (id) => document.getElementById(`ae-${id}`);
  let snapshot = {devices: [], jobs: [], last_synced: {}, progress: {}};
  let pairing = null, pairMessage = "", copyMessage = "", busy = false, disconnectId = null;
  let handJobId = null, overwriteConfirmed = false;
  let stateTimer, deviceTimer, countdownTimer;
  let refreshing = false, refreshAgain = false;
  let devicesPainted = "", jobsPainted = "", verifyPainted = "", hadConnection = false;
  let stateDelay = 15000, deviceDelay = 2000, pollError = false;

  function error(err, fromPoll = false) {
    pollError = fromPoll;
    el("error").textContent = err.message || T("ae.failedRequest");
    el("error").hidden = false;
  }

  function clearError() {
    pollError = false;
    el("error").textContent = "";
    el("error").hidden = true;
  }

  function relative(time) {
    if (time == null) return T("ae.neverSeen");
    const seconds = Math.max(0, (Date.now() / 1000) - time);
    if (seconds < 60) return T("ae.justNow");
    if (seconds < 3600) return Tf("ae.minutesAgo", {n: Math.floor(seconds / 60)});
    if (seconds < 86400) return Tf("ae.hoursAgo", {n: Math.floor(seconds / 3600)});
    return Tf("ae.daysAgo", {n: Math.floor(seconds / 86400)});
  }

  function node(tag, text = "", className = "") {
    const value = document.createElement(tag);
    value.textContent = text;
    value.className = className;
    return value;
  }

  function editMessage(result) {
    const interrupted = Array.isArray(result.interrupted) ? result.interrupted : [];
    const edited = (Array.isArray(result.hand_edited) ? result.hand_edited : []).filter(id => !interrupted.includes(id));
    return [interrupted.length && Tf("ae.interrupted", {ids: interrupted.join(", ")}),
      edited.length && Tf("ae.handEdited", {n: edited.length, ids: edited.join(", ")})].filter(Boolean).join("\n");
  }

  function button(key, click, destructive = false) {
    const value = node("button", T(key), `btn btn--secondary btn--inline${destructive ? " ae-card__destructive" : ""}`);
    value.type = "button";
    value.disabled = busy;
    value.addEventListener("click", click);
    return value;
  }

  function focusDevice(id) {
    [...el("devices").children].find((row) => row.dataset.device === id)?.querySelector("button")?.focus();
  }

  function clearCode(message = "") {
    pairing = null;
    deviceDelay = 2000;
    clearTimeout(deviceTimer);
    clearTimeout(countdownTimer);
    el("code").value = "";
    el("countdown").textContent = "";
    el("code-field").hidden = true;
    pairMessage = message;
    el("pair-message").textContent = message ? T(message) : "";
    el("pair-message").hidden = !message;
  }

  function tickCode() {
    clearTimeout(countdownTimer);
    if (!pairing) return;
    const milliseconds = pairing.expires_at * 1000 - Date.now();
    const remaining = Math.ceil(milliseconds / 1000);
    if (milliseconds <= 0) return clearCode("ae.expired");
    el("code-field").hidden = document.hidden;
    el("code").value = document.hidden ? "" : pairing.code;
    el("countdown").textContent = `${Math.floor(remaining / 60).toString().padStart(2, "0")}:${(remaining % 60).toString().padStart(2, "0")}`;
    if (!document.hidden) countdownTimer = setTimeout(tickCode, Math.min(1000, milliseconds));
  }

  function acceptDevices(devices) {
    snapshot.devices = devices;
    if (pairing && devices.some((device) => !pairing.before.has(device.id))) clearCode();
  }

  async function pollDevices() {
    clearTimeout(deviceTimer);
    if (!pairing || document.hidden) return;
    try {
      const data = await api("/api/ae/devices");
      acceptDevices(data.devices);
      paint();
      deviceDelay = 2000;
      if (pollError) clearError();
      if (!pairing) await refresh();
    } catch (err) {
      deviceDelay = Math.min(deviceDelay * 2, 60000);
      error(err, true);
    }
    if (pairing && !document.hidden) deviceTimer = setTimeout(pollDevices, deviceDelay);
  }

  function jobOutcome(job) {
    if (!job) return "";
    if (job.error) return job.error;
    if (job.state !== "done") return T(`ae.${job.state}`);
    if (job.result?.applied !== true) {
      if (job.result?.applied === false) return ""; // The hand-edit prompt owns this outcome.
      return T("ae.done");
    }
    const counts = ["created", "updated", "deleted"].flatMap(key => {
      const value = job.result[key];
      const n = Array.isArray(value) ? value.length : Number(value) || 0;
      return n > 0 ? [Tf(`ae.count.${key}`, {n})] : [];
    });
    return counts.join(", ") || T("ae.noChanges");
  }

  function percent(value) {
    return (value * 100).toFixed(1);
  }

  function verifyOutcome(report) {
    if (report.state === "rendering") return T("ae.verifyRendering");
    if (report.state === "verifying") return T("ae.verifyVerifying");
    if (report.state === "failed") return report.error || T("ae.verifyFailed");
    if (report.state === "interrupted") return T("ae.verifyInterrupted");
    const worst = report.worst[0];
    let text = Tf(report.passed ? "ae.verifyPassed" : "ae.verifyDiffers", {
      version: report.version, mean: percent(report.mean), frame: worst.frame, max: percent(report.max),
    });
    if (!report.passed) {
      const n = report.frames.filter(row => row.l1 > report.thresholds.frame).length;
      text += Tf("ae.verifyOver", {n, threshold: Number(percent(report.thresholds.frame))});
    }
    return text;
  }

  function paintVerify(report) {
    const signature = JSON.stringify([getSceneId(), report, T("ae.verify")]);
    if (signature === verifyPainted) return;
    verifyPainted = signature;
    const hasReport = report?.state === "done";
    el("verify-report").hidden = !hasReport;
    el("verify-worst").replaceChildren();
    el("verify-notes").replaceChildren();
    el("verify-masked").replaceChildren();
    if (!hasReport) return;
    el("verify-limits").textContent = Tf("ae.verifyLimits", {
      mean: Number(percent(report.thresholds.mean)), frame: Number(percent(report.thresholds.frame)),
    });
    el("verify-worst").replaceChildren(...report.worst.slice(0, 3).map(row => {
      const item = node("li");
      item.append(node("p", Tf("ae.verifyFrame", {frame: row.frame, l1: percent(row.l1)})));
      const images = node("div", "", "ae-card__verify-images");
      for (const [kind, key] of [["ae", "ae.verifyAE"], ["kf", "ae.verifyKF"], ["diff", "ae.verifyDiff"]]) {
        const figure = node("figure");
        const img = node("img");
        img.loading = "lazy";
        img.alt = Tf("ae.verifyImageAlt", {kind: T(key), frame: row.frame});
        img.src = `/api/ae/verify-image?${new URLSearchParams({project: projectId, scene: getSceneId(),
          version: report.version, name: row[kind], job: report.job})}`;
        figure.append(node("figcaption", T(key)), img);
        images.append(figure);
      }
      item.append(images);
      return item;
    }));
    el("verify-notes").hidden = !report.notes.length;
    el("verify-notes").replaceChildren(...report.notes.map(note => node("li", note)));
    el("verify-masked").hidden = !report.masked.length;
    el("verify-masked").replaceChildren(...report.masked.map(row => node("li", Tf("ae.verifyMasked", {
      id: row.id, l1: percent(row.worst_l1),
    }))));
  }

  function paint() {
    const devices = [...snapshot.devices].sort((a, b) => (b.last_seen ?? 0) - (a.last_seen ?? 0) || b.created - a.created);
    const connected = devices.find((device) => device.connected);
    const isConnected = Boolean(connected);
    let status = T("ae.notConnected");
    if (isConnected) {
      const version = connected.ae_version?.split(".").slice(0, 2).join(".");
      status = Tf("ae.connectedDevice", {version: version ? ` ${version}` : ""});
      if (connected.project_name) status += ` · ${connected.project_name}`;
    }
    if (el("status").textContent !== status) el("status").textContent = status;
    el("status").classList.toggle("render-card__status--active", isConnected);
    const connectContainer = el(isConnected ? "install-connect" : "actions");
    if (el("connect").parentElement !== connectContainer) connectContainer.prepend(el("connect"));
    el("connect").disabled = busy;
    el("send").disabled = busy || !isConnected || !projectId || !getSceneId() || !getVersionId();
    el("verify").disabled = el("send").disabled;
    el("send-reason").hidden = isConnected;
    el("overwrite-reason").hidden = isConnected;
    if (isConnected !== hadConnection) el("install").open = !isConnected;
    hadConnection = isConnected;
    el("pair-message").textContent = pairMessage ? T(pairMessage) : "";
    el("pair-message").hidden = !pairMessage;
    el("copy-status").textContent = copyMessage ? T(copyMessage) : "";
    el("copy-status").hidden = !copyMessage;
    const syncDevice = connected || devices[0];
    const synced = syncDevice && snapshot.last_synced[syncDevice.id];
    const latestJob = snapshot.jobs[0];
    const syncedText = synced ? Tf("ae.hasVersion", {version: synced}) : T("ae.nothingSynced");
    const outcome = jobOutcome(latestJob);
    const report = snapshot.verify;
    const showVerify = report && (!latestJob || latestJob.id === report.job);
    let summary = outcome ? `${syncedText}. ${outcome}` : syncedText;
    if (showVerify) summary = verifyOutcome(report);
    el("synced").textContent = summary;
    const failed = showVerify ? report.state === "failed" : Boolean(latestJob?.error) || latestJob?.state === "failed";
    el("synced").classList.toggle("ae-card__error", failed);
    paintVerify(report);
    const buildsDiffer = devices.some(device => device.panel_build && device.host_build && device.panel_build !== device.host_build);
    el("build-warning").hidden = !buildsDiffer;
    el("build-warning").textContent = T("ae.buildMismatch");
    const latestProgress = latestJob && snapshot.progress[latestJob.id];
    el("latest-progress").hidden = !latestProgress;
    el("latest-progress").textContent = latestProgress ? Tf("ae.progress", latestProgress) : "";
    const warnings = Array.isArray(latestJob?.result?.warnings) ? latestJob.result.warnings : [];
    el("warnings").hidden = !warnings.length;
    el("warnings").replaceChildren(...warnings.map(warning => node("li", warning)));

    const rows = devices.map((device) => ({device, text: Tf("ae.device", {
      version: device.ae_version || T("ae.unknownBuild"), seen: relative(device.last_seen),
    }), build: Tf("ae.build", {panel: device.panel_build || T("ae.unknownBuild"),
      host: device.host_build || T("ae.unknownBuild")})}));
    const deviceSignature = JSON.stringify([rows.map(({device, text, build}) => [device.id, text, build, device.connected]), disconnectId, busy]);
    if (deviceSignature !== devicesPainted) {
      devicesPainted = deviceSignature;
      el("devices").replaceChildren(...rows.map(({device, text, build}) => {
        const row = node("li", "", "ae-card__row");
        row.dataset.device = device.id;
        const dot = node("span", "", "ae-card__dot");
        dot.classList.toggle("ae-card__dot--connected", device.connected);
        dot.setAttribute("aria-hidden", "true");
        const description = node("span", "", "ae-card__device");
        description.append(node("span", text), node("p", build));
        row.append(dot, description);
        if (disconnectId === device.id) {
          row.append(node("span", T("ae.disconnectConfirm")),
            button("ae.yes", () => action(async () => {
              await api(`/api/ae/devices/${encodeURIComponent(device.id)}`, {method: "DELETE"});
              disconnectId = null;
            }), true), button("ae.no", () => { disconnectId = null; paint(); focusDevice(device.id); }));
        } else row.append(button("ae.disconnect", () => { disconnectId = device.id; paint(); focusDevice(device.id); }, true));
        return row;
      }));
    }

    const newestSync = snapshot.jobs.find((job) => job.kind === "sync");
    const handJob = newestSync?.state === "done" && newestSync.result?.applied === false ? newestSync : null;
    if (handJobId !== handJob?.id) overwriteConfirmed = false;
    handJobId = handJob?.id;
    el("hand-edits").hidden = !handJob;
    if (handJob) {
      el("hand-message").textContent = editMessage(handJob.result);
    }
    el("overwrite-confirm").hidden = !overwriteConfirmed;
    el("overwrite").hidden = overwriteConfirmed;
    el("overwrite").disabled = el("overwrite-yes").disabled = busy || !connected;
    el("overwrite-no").disabled = busy;

    const jobs = snapshot.jobs.slice(1, JOB_HISTORY_LIMIT + 1);
    const jobSignature = JSON.stringify(jobs.map((job) => [job, relative(job.finished ?? job.started ?? job.created), snapshot.progress[job.id]])) + T("ae.sync");
    if (jobSignature === jobsPainted) return;
    jobsPainted = jobSignature;
    el("jobs").replaceChildren(...jobs.map((job) => {
      const row = node("li");
      row.append(node("p", Tf("ae.job", {kind: T(`ae.${job.kind}`), state: T(`ae.${job.state}`),
        version: job.version, time: relative(job.finished ?? job.started ?? job.created)})));
      const outcome = jobOutcome(job);
      if (outcome) row.append(node("p", outcome, job.error ? "ae-card__error" : ""));
      if (job.result?.applied === false) row.append(node("p", editMessage(job.result)));
      const progress = snapshot.progress[job.id];
      if (progress) row.append(node("p", Tf("ae.progress", progress)));
      const warnings = Array.isArray(job.result?.warnings) ? job.result.warnings : [];
      if (warnings.length) {
        const list = node("ul");
        list.append(...warnings.map(warning => node("li", warning)));
        row.append(list);
      }
      return row;
    }));
  }

  async function refresh() {
    clearTimeout(stateTimer);
    if (document.hidden) return;
    if (refreshing) { refreshAgain = true; return; }
    refreshing = true;
    do {
      refreshAgain = false;
      const scene = getSceneId();
      try {
        const data = projectId && scene
          ? await api(`/api/ae/state?${new URLSearchParams({project: projectId, scene})}`)
          : await api("/api/ae/devices");
        if (scene !== getSceneId()) { refreshAgain = true; continue; }
        snapshot = {jobs: [], last_synced: {}, progress: {}, ...data};
        acceptDevices(data.devices);
        paint();
        const hasActiveJob = snapshot.jobs.some(job => ["queued", "running"].includes(job.state));
        const isVerifying = ["rendering", "verifying"].includes(snapshot.verify?.state);
        stateDelay = hasActiveJob || isVerifying ? 2000 : 15000;
        if (pollError) clearError();
      } catch (err) {
        stateDelay = Math.min(stateDelay * 2, 60000);
        error(err, true);
      }
    } while (refreshAgain && !document.hidden);
    refreshing = false;
    if (!document.hidden) stateTimer = setTimeout(refresh, stateDelay);
  }

  async function action(work) {
    if (busy) return;
    busy = true;
    clearError();
    copyMessage = "";
    paint();
    try { await work(); } catch (err) { error(err); }
    finally { busy = false; paint(); await refresh(); }
  }

  async function send(force = false) {
    await api("/api/ae/send", {method: "POST", body: JSON.stringify({
      project: projectId, scene: getSceneId(), version: getVersionId(), ...(force ? {force: true} : {}),
    })});
  }

  async function copy(field) {
    const copied = await copyText(field.value);
    if (!copied) {
      field.focus();
      field.select();
    }
    copyMessage = copied ? "ae.copied" : "ae.copyFailed";
    el("copy-status").textContent = T(copyMessage);
    el("copy-status").hidden = false;
  }

  el("connect").addEventListener("click", () => action(async () => {
    clearCode();
    const {devices} = await api("/api/ae/devices");
    const code = await api("/api/ae/codes", {method: "POST", body: "{}"});
    pairing = {...code, before: new Set(devices.map((device) => device.id))};
    tickCode();
    if (pairing && !document.hidden) deviceTimer = setTimeout(pollDevices, 2000);
  }));
  el("send").addEventListener("click", () => action(() => send()));
  el("verify").addEventListener("click", () => action(() => api("/api/ae/verify", {
    method: "POST", body: JSON.stringify({project: projectId, scene: getSceneId(), version: getVersionId()}),
  })));
  el("overwrite").addEventListener("click", () => { overwriteConfirmed = true; paint(); el("overwrite-yes").focus(); });
  el("overwrite-no").addEventListener("click", () => { overwriteConfirmed = false; paint(); el("overwrite").focus(); });
  el("overwrite-yes").addEventListener("click", () => {
    if (handJobId && overwriteConfirmed) action(() => send(true));
  });
  el("install-copy").addEventListener("click", () => copy(el("install-command")));
  el("code-copy").addEventListener("click", () => {
    tickCode();
    if (pairing) copy(el("code"));
  });
  window.addEventListener("keepframe:lang", paint);
  document.addEventListener("visibilitychange", () => {
    clearTimeout(stateTimer);
    clearTimeout(deviceTimer);
    clearTimeout(countdownTimer);
    tickCode();
    if (!document.hidden) { refresh(); pollDevices(); }
  });
  paint();
  refresh();
  return {refresh};
}
