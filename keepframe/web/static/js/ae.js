import { api } from "/static/js/api.js?v=20261006b";
import { T, Tf } from "/static/js/i18n.js?v=20261006b";

export function initAECard({projectId, getSceneId, getVersionId}) {
  const el = (id) => document.getElementById(`ae-${id}`);
  let snapshot = {devices: [], jobs: [], last_synced: {}, progress: {}};
  let pairing = null, pairMessage = "", busy = false, disconnectId = null;
  let handJobId = null, overwriteConfirmed = false;
  let stateTimer, deviceTimer, countdownTimer;
  let refreshing = false, refreshAgain = false;
  let devicesPainted = "", jobsPainted = "", hadDevices = false;

  function error(err) {
    el("error").textContent = err.message || T("ae.failedRequest");
    el("error").hidden = false;
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

  function button(key, click) {
    const value = node("button", T(key), "btn btn--inline");
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
    clearTimeout(deviceTimer);
    clearTimeout(countdownTimer);
    el("code").value = "";
    el("countdown").textContent = "";
    el("code-field").hidden = true;
    pairMessage = message;
    el("pair-message").textContent = message ? T(message) : "";
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
    if (pairing && devices.some((device) => !pairing.before.has(device.id))) clearCode("ae.connected");
  }

  async function pollDevices() {
    clearTimeout(deviceTimer);
    if (!pairing || document.hidden) return;
    try {
      const data = await api("/api/ae/devices");
      acceptDevices(data.devices);
      paint();
      if (!pairing) await refresh();
    } catch (err) { error(err); }
    if (pairing && !document.hidden) deviceTimer = setTimeout(pollDevices, 2000);
  }

  function paint() {
    const devices = [...snapshot.devices].sort((a, b) => (b.last_seen ?? 0) - (a.last_seen ?? 0) || b.created - a.created);
    const connected = devices.find((device) => device.connected);
    const status = connected ? Tf("ae.connectedDevice", {
      version: connected.ae_version, project: connected.project_name || T("ae.noProject"),
    }) : T("ae.notConnected");
    if (el("status").textContent !== status) el("status").textContent = status;
    el("status").classList.toggle("render-card__status--active", Boolean(connected));
    el("connect").disabled = busy;
    el("send").disabled = busy || !connected || !projectId || !getSceneId() || !getVersionId();
    if (Boolean(devices.length) !== hadDevices) el("install").open = !devices.length;
    hadDevices = Boolean(devices.length);
    el("pair-message").textContent = pairMessage ? T(pairMessage) : "";
    if (el("copy-status").textContent) el("copy-status").textContent = T("ae.copied");
    const synced = devices[0] && snapshot.last_synced[devices[0].id];
    el("synced").textContent = synced ? Tf("ae.hasVersion", {version: synced}) : T("ae.nothingSynced");

    const rows = devices.map((device) => ({device, text: Tf("ae.device", {
      version: device.ae_version, os: device.os, project: device.project_name || T("ae.noProject"),
      status: T(device.connected ? "ae.connected" : "ae.notConnected"), seen: relative(device.last_seen),
    })}));
    const deviceSignature = JSON.stringify([rows.map(({device, text}) => [device.id, text]), disconnectId, busy]);
    if (deviceSignature !== devicesPainted) {
      devicesPainted = deviceSignature;
      el("devices").replaceChildren(...rows.map(({device, text}) => {
        const row = node("li", "", "ae-card__row");
        row.dataset.device = device.id;
        const dot = node("span", "", "ae-card__dot");
        dot.classList.toggle("ae-card__dot--connected", device.connected);
        dot.setAttribute("aria-hidden", "true");
        row.append(dot, node("span", text, "ae-card__device"));
        if (disconnectId === device.id) {
          row.append(node("span", T("ae.disconnectConfirm")),
            button("ae.yes", () => action(async () => {
              await api(`/api/ae/devices/${encodeURIComponent(device.id)}`, {method: "DELETE"});
              disconnectId = null;
            })), button("ae.no", () => { disconnectId = null; paint(); focusDevice(device.id); }));
        } else row.append(button("ae.disconnect", () => { disconnectId = device.id; paint(); focusDevice(device.id); }));
        return row;
      }));
    }

    const newestSync = snapshot.jobs.find((job) => job.kind === "sync");
    const handJob = newestSync?.state === "done" && newestSync.result?.applied === false ? newestSync : null;
    if (handJobId !== handJob?.id) overwriteConfirmed = false;
    handJobId = handJob?.id;
    el("hand-edits").hidden = !handJob;
    if (handJob) {
      const ids = Array.isArray(handJob.result.hand_edited) ? handJob.result.hand_edited : [];
      el("hand-message").textContent = Tf("ae.handEdited", {n: ids.length, ids: ids.join(", ")});
    }
    el("overwrite-confirm").hidden = !overwriteConfirmed;
    el("overwrite").hidden = overwriteConfirmed;
    el("overwrite").disabled = el("overwrite-yes").disabled = busy || !connected;
    el("overwrite-no").disabled = busy;

    const jobs = snapshot.jobs.slice(0, 5);
    const jobSignature = JSON.stringify(jobs.map((job) => [job, relative(job.finished ?? job.started ?? job.created), snapshot.progress[job.id]])) + T("ae.sync");
    if (jobSignature === jobsPainted) return;
    jobsPainted = jobSignature;
    const openWarnings = new Set([...el("jobs").querySelectorAll("details[open]")].map((details) => details.dataset.job));
    el("jobs").replaceChildren(...jobs.map((job) => {
      const row = node("li");
      row.append(node("p", Tf("ae.job", {kind: T(`ae.${job.kind}`), state: T(`ae.${job.state}`),
        version: job.version, time: relative(job.finished ?? job.started ?? job.created)})));
      if (job.error) row.append(node("p", job.error, "ae-card__error"));
      else if (job.result?.applied === true) row.append(node("p", Tf("ae.summary", {
        created: job.result.created ?? 0, updated: job.result.updated ?? 0, deleted: job.result.deleted ?? 0,
      })));
      const progress = snapshot.progress[job.id];
      if (progress) row.append(node("p", Tf("ae.progress", progress)));
      const warnings = Array.isArray(job.result?.warnings) ? job.result.warnings : [];
      if (warnings.length) {
        const details = node("details");
        details.dataset.job = job.id;
        details.open = openWarnings.has(job.id);
        details.append(node("summary", Tf("ae.warnings", {n: warnings.length})));
        const list = node("ul");
        list.append(...warnings.map((warning) => node("li", warning)));
        details.append(list);
        row.append(details);
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
      } catch (err) { error(err); }
    } while (refreshAgain && !document.hidden);
    refreshing = false;
    if (!document.hidden) stateTimer = setTimeout(refresh, snapshot.jobs.some((job) => ["queued", "running"].includes(job.state)) ? 2000 : 15000);
  }

  async function action(work) {
    if (busy) return;
    busy = true;
    el("error").hidden = true;
    el("copy-status").textContent = "";
    paint();
    try { await work(); } catch (err) { error(err); }
    finally { busy = false; paint(); await refresh(); }
  }

  async function send(force = false) {
    await api("/api/ae/send", {method: "POST", body: JSON.stringify({
      project: projectId, scene: getSceneId(), version: getVersionId(), ...(force ? {force: true} : {}),
    })});
  }

  async function copy(value) {
    try {
      await navigator.clipboard.writeText(value);
      el("copy-status").textContent = T("ae.copied");
    } catch (err) { error(err); }
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
  el("overwrite").addEventListener("click", () => { overwriteConfirmed = true; paint(); el("overwrite-yes").focus(); });
  el("overwrite-no").addEventListener("click", () => { overwriteConfirmed = false; paint(); el("overwrite").focus(); });
  el("overwrite-yes").addEventListener("click", () => {
    if (handJobId && overwriteConfirmed) action(() => send(true));
  });
  el("install-copy").addEventListener("click", () => copy(el("install-command").value));
  el("code-copy").addEventListener("click", () => {
    tickCode();
    if (pairing) copy(pairing.code);
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
