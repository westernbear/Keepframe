import { fetchAdminOrRedirect } from "/static/js/api.js?v=20260921u";

const MODEL_RELOAD_DEBOUNCE_MS = 400;
const AZURE_SCOPE = "https://cognitiveservices.azure.com/.default";
const LLM_PATH = "/admin/llm";

const select = document.getElementById("provider");
const model = document.getElementById("model");
const modelOptions = document.getElementById("modelOptions");
const auth = document.getElementById("auth");
const authField = document.getElementById("authField");
const apiKey = document.getElementById("apiKey");
const apiKeyField = document.getElementById("apiKeyField");
const oauthFields = document.getElementById("oauthFields");
const oauthAdvanced = document.getElementById("oauthAdvanced");
const chatgptOAuth = document.getElementById("chatgptOAuth");
const chatgptConnect = document.getElementById("chatgptConnect");
const chatgptDisconnect = document.getElementById("chatgptDisconnect");
const chatgptStatus = document.getElementById("chatgptStatus");
const clientId = document.getElementById("clientId");
const clientSecret = document.getElementById("clientSecret");
const tenantId = document.getElementById("tenantId");
const tokenUrl = document.getElementById("tokenUrl");
const scope = document.getElementById("scope");
const baseUrl = document.getElementById("baseUrl");
const baseUrlField = document.getElementById("baseUrlField");
const extra = document.getElementById("extra");
const statusEl = document.getElementById("status");
let current = null;
let catalog = [];
let modelsTimer = 0;

function specOf(id) {
  return catalog.find((p) => p.id === id) || {};
}

function fillCatalog(items) {
  catalog = items || [];
  const prev = select.value;
  select.innerHTML = "";
  catalog.forEach((p) => {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.label;
    select.appendChild(opt);
  });
  if (prev && catalog.some((p) => p.id === prev)) select.value = prev;
}

function showFields() {
  const p = select.value;
  const spec = specOf(p);
  const modes = spec.auth_modes || ["api_key"];
  [...auth.options].forEach((opt) => { opt.hidden = !modes.includes(opt.value); });
  if (!modes.includes(auth.value)) auth.value = modes[0];
  const isChatgpt = p === "chatgpt";
  const isAzureOauth = p === "azure" && auth.value === "oauth";
  const isOauth = auth.value === "oauth";
  authField.hidden = isChatgpt || modes.length === 1;
  apiKeyField.hidden = isChatgpt || Boolean(spec.hide_api_key) || isOauth;
  oauthFields.hidden = !isChatgpt && !isAzureOauth;
  chatgptOAuth.hidden = !isChatgpt;
  oauthAdvanced.hidden = p !== "azure";
  oauthAdvanced.open = isAzureOauth;
  baseUrlField.hidden = Boolean(spec.hide_base_url);
}

function setChatgptStatus(s) {
  const connected = Boolean(s && s.chatgpt_connected);
  chatgptDisconnect.hidden = !connected;
  chatgptConnect.disabled = connected;
  chatgptStatus.hidden = !connected;
  chatgptStatus.textContent = connected ? "ChatGPT 연결됨" : "";
}

function applyProviderDefaults() {
  const spec = specOf(select.value);
  auth.value = (spec.auth_modes || ["api_key"])[0];
  baseUrl.value = spec.default_base_url || "";
  model.value = spec.default_model || "";
  const shouldFillAzureScope = select.value === "azure" && auth.value === "oauth" && !scope.value;
  if (shouldFillAzureScope) scope.value = AZURE_SCOPE;
  showFields();
  loadModels(false);
}

function fill(s) {
  current = s;
  select.value = s.provider || "openai";
  model.value = s.model || "";
  const spec = specOf(select.value);
  const modes = spec.auth_modes || ["api_key"];
  const mode = s.auth && modes.includes(s.auth) ? s.auth : modes[0];
  auth.value = mode;
  setChatgptStatus(s);
  apiKey.value = s.api_key || "";
  clientId.value = s.client_id || "";
  clientSecret.value = s.client_secret || "";
  tenantId.value = s.tenant_id || "";
  tokenUrl.value = s.token_url || "";
  scope.value = s.scope || "";
  baseUrl.value = s.base_url || spec.default_base_url || "";
  extra.value = s.extra && Object.keys(s.extra).length ? JSON.stringify(s.extra, null, 2) : "";
  showFields();
  loadModels(false);
}

function note(msg, isErr = false) {
  statusEl.hidden = !msg;
  statusEl.textContent = msg || "";
  statusEl.classList.toggle("status-note--err", isErr);
}

function setModelOptions(ids) {
  modelOptions.innerHTML = "";
  (ids || []).forEach((id) => {
    const opt = document.createElement("option");
    opt.value = id;
    modelOptions.appendChild(opt);
  });
}

async function loadModels(announce) {
  setModelOptions([]);
  try {
    const data = await fetchAdminOrRedirect("/admin/api/llm/models", {
      method: "POST",
      body: JSON.stringify({
        provider: select.value,
        base_url: baseUrl.value.trim(),
        api_key: apiKey.value.trim(),
      }),
    });
    const ids = data.models || [];
    setModelOptions(ids);
    if (!model.value && ids.length) model.value = ids[0];
    if (announce) note(ids.length ? `모델 ${ids.length}개를 불러왔습니다.` : "모델 목록이 비어 있습니다.");
  } catch (err) {
    if (announce) note(err.message || "모델 목록을 불러오지 못했습니다.", true);
  }
}

function scheduleModels() {
  window.clearTimeout(modelsTimer);
  modelsTimer = window.setTimeout(() => loadModels(false), MODEL_RELOAD_DEBOUNCE_MS);
}

function parseExtraJson(text) {
  const trimmed = text.trim();
  if (!trimmed) return { ok: true, value: {} };
  try {
    return { ok: true, value: JSON.parse(trimmed) };
  } catch {
    return { ok: false, value: null };
  }
}

function readFormSettings() {
  const isOauth = auth.value === "oauth";
  return {
    provider: select.value,
    model: model.value.trim(),
    auth: auth.value,
    api_key: isOauth ? "" : apiKey.value.trim(),
    client_id: isOauth ? clientId.value.trim() : "",
    client_secret: isOauth ? clientSecret.value.trim() : "",
    tenant_id: isOauth ? tenantId.value.trim() : "",
    token_url: isOauth ? tokenUrl.value.trim() : "",
    scope: isOauth ? scope.value.trim() : "",
    base_url: baseUrl.value.trim(),
  };
}

function announceOAuthReturn(flag) {
  if (flag === "ok") {
    note("ChatGPT 연결이 완료되었습니다.");
    history.replaceState({}, "", LLM_PATH);
    return;
  }
  if (flag === "err") {
    note("ChatGPT 연결에 실패했습니다.", true);
    history.replaceState({}, "", LLM_PATH);
  }
}

auth.addEventListener("change", () => {
  showFields();
  const shouldFillAzureScope = auth.value === "oauth" && select.value === "azure" && !scope.value;
  if (shouldFillAzureScope) scope.value = AZURE_SCOPE;
});
select.addEventListener("change", () => applyProviderDefaults());
baseUrl.addEventListener("change", scheduleModels);
apiKey.addEventListener("change", scheduleModels);
document.getElementById("reloadModels").addEventListener("click", () => loadModels(true));

chatgptConnect.addEventListener("click", async () => {
  note("");
  try {
    const data = await fetchAdminOrRedirect("/admin/api/llm/oauth/start?provider=chatgpt");
    if (data.url) location.href = data.url;
    else note("연결 URL을 받지 못했습니다.", true);
  } catch (err) {
    note(err.message || "연결 시작 실패", true);
  }
});

chatgptDisconnect.addEventListener("click", async () => {
  try {
    const data = await fetchAdminOrRedirect("/admin/api/llm/oauth/disconnect", { method: "POST" });
    if (data.catalog) fillCatalog(data.catalog);
    fill(data.settings);
    note("ChatGPT 연결을 해제했습니다.");
  } catch (err) {
    note(err.message || "연결 해제 실패", true);
  }
});

document.getElementById("save").addEventListener("click", async () => {
  const parsed = parseExtraJson(extra.value);
  if (!parsed.ok) {
    note("추가 파라미터가 올바른 JSON이 아닙니다.", true);
    return;
  }
  const body = { settings: { ...readFormSettings(), extra: parsed.value } };
  try {
    const data = await fetchAdminOrRedirect("/admin/api/llm", { method: "POST", body: JSON.stringify(body) });
    if (data.catalog) fillCatalog(data.catalog);
    fill(data.settings);
    note("저장했습니다.");
  } catch (err) {
    note(err.message || "저장 실패", true);
  }
});

document.getElementById("reset").addEventListener("click", () => {
  if (current) fill(current);
  else note("");
});

const oauthFlag = new URLSearchParams(location.search).get("oauth");
fetchAdminOrRedirect("/admin/api/llm").then((data) => {
  fillCatalog(data.catalog);
  fill(data.settings);
  announceOAuthReturn(oauthFlag);
}).catch((err) => {
  if (err.status !== 401) note(err.message || "불러오기 실패", true);
});
