const STRINGS = {
  ko: {
    "app.name": "Keepframe",
    "nav.library": "라이브러리",
    "nav.new": "새 레퍼런스",
    "nav.analyze": "분석",
    "nav.review": "검수",
    "lang.toggle": "EN",
    "common.cancel": "취소",
    "ingest.start": "분석 시작",
    "ingest.range": "구간 지정",
    "ingest.full": "전체 영상",
    "ingest.confirm": "실행 전 확인",
    "ingest.title": "레퍼런스 영상",
    "ingest.desc": "평면 2D 모션그래픽 또는 UI 화면 녹화. 실사 푸티지는 받지 않습니다.",
    "ingest.pickFile": "영상 파일 선택",
    "ingest.startFrame": "시작",
    "ingest.endFrame": "끝",
    "ingest.eta": "예상 시간",
    "ingest.gpu": "GPU 사용 허용",
    "ingest.scenes": "장면",
    "ingest.confirmGo": "확인하고 계속",
    "ingest.uploading": "업로드 중…",
    "ingest.uploadFailed": "업로드 실패",
    "ingest.aboutMin": "약 {m}분 (추정)",
    "ingest.perScene": " · 장면당 {s}초 (추정)",
    "ingest.rangeSummary": "선택 {sec}초 · {frames}프레임 · 장면 {n}개",
    "ingest.fullSummary": "전체 {sec}초 · {frames}프레임 · 장면 {n}개",
    "ingest.fullConfirm": "전체 영상 분석 예상 {m}분, 장면 {n}개. 계속하시겠습니까?",
    "library.search": "프로젝트 검색",
    "library.empty": "프로젝트가 없습니다. 새 프로젝트에서 레퍼런스를 올리세요.",
    "library.filter.recent": "최근",
    "library.filter.review": "검수 필요",
    "library.filter.export": "내보내기 준비",
    "library.confidence": "신뢰도 {c}",
    "library.loadFailed": "목록을 불러오지 못했습니다.",
    "status.uploaded": "업로드됨",
    "status.analyzing": "분석 중",
    "status.review": "검수 필요",
    "status.error": "실패",
    "status.rejected": "거부됨",
    "analyze.running": "분석 진행 상태",
    "analyze.selectedRange": "선택된 구간",
    "analyze.etaLeft": "예상 남은 시간 약 {m}분",
    "analyze.failed": "분석 실패",
    "analyze.noProject": "프로젝트 ID가 없습니다.",
    "analyze.pollFailed": "상태 조회 실패",
    "analyze.startFailed": "분석 시작 실패",
    "analyze.step.shots": "구간·샷 분할",
    "analyze.step.bg": "배경",
    "analyze.step.text": "텍스트 인식",
    "analyze.step.textHint": "장면당 수 분 허용",
    "analyze.step.regions": "영역 추출",
    "analyze.step.sprites": "sprite 최적화",
    "analyze.step.keyframes": "키프레임",
    "analyze.step.predicates": "술어 추출",
    "analyze.step.report": "리포트",
    "review.keepSave": "유지 조건 저장",
    "review.reassign": "재할당",
    "review.mask": "마스크",
    "review.bbox": "박스 프롬프트",
    "review.text": "텍스트",
    "review.orig": "원본",
    "review.recon": "재구성",
    "review.elements": "요소 {n}",
    "review.empty": "인식된 요소가 없습니다. 원본 화면에서 박스를 그려 첫 요소를 지정하세요.",
    "review.keep": "유지 조건",
    "review.runReassign": "재할당 실행",
    "review.runMask": "마스크 적용",
    "review.runBbox": "박스 적용",
    "review.runText": "텍스트 저장",
    "review.running": "보정 실행 중: {op}",
    "review.retry": "{err}. 값을 확인하고 다시 실행하세요.",
    "review.error": "오류",
    "review.needProject": "project 쿼리가 필요합니다.",
    "review.loadFailed": "상태 로드 실패",
    "review.loading": "장면 불러오는 중",
    "review.loadingFrames": "프레임 불러오는 중",
    "review.loadingList": "요소 목록 그리는 중",
    "review.loadingRecon": "재구성 그리는 중",
    "error.liveaction": "실사 푸티지는 열리지 않습니다.",
    "empty.elements": "요소 없음",
    "admin.login": "관리자 로그인",
    "admin.enter": "들어가기",
    "admin.tenants": "테넌트",
    "admin.queue": "작업 큐",
    "admin.quarantine": "검역",
    "admin.audit": "감사 로그",
    "admin.policy": "재시도 상한 4회 · 에셋 생성 2회 · 관리자가 올릴 수 없음.",
    "admin.ops": "운영",
    "admin.email": "이메일",
    "admin.password": "비밀번호",
    "admin.back": "제작 도구로 돌아가기",
    "admin.localOff": "로컬판에는 이 화면이 없습니다.",
    "admin.loginFailed": "로그인 실패",
    "admin.manage": "관리",
  },
  en: {
    "app.name": "Keepframe",
    "nav.library": "Library",
    "nav.new": "New reference",
    "nav.analyze": "Analyze",
    "nav.review": "Review",
    "lang.toggle": "KO",
    "common.cancel": "Cancel",
    "ingest.start": "Start analysis",
    "ingest.range": "Range",
    "ingest.full": "Full video",
    "ingest.confirm": "Pre-flight check",
    "ingest.title": "Reference video",
    "ingest.desc": "Flat 2D motion graphics or a UI screen recording. Live-action is rejected.",
    "ingest.pickFile": "Choose a video file",
    "ingest.startFrame": "Start",
    "ingest.endFrame": "End",
    "ingest.eta": "Estimated time",
    "ingest.gpu": "GPU allowed",
    "ingest.scenes": "Scenes",
    "ingest.confirmGo": "Confirm and continue",
    "ingest.uploading": "Uploading…",
    "ingest.uploadFailed": "Upload failed",
    "ingest.aboutMin": "About {m} min (estimate)",
    "ingest.perScene": " · {s}s per scene (estimate)",
    "ingest.rangeSummary": "Selected {sec}s · {frames} frames · {n} scenes",
    "ingest.fullSummary": "Full {sec}s · {frames} frames · {n} scenes",
    "ingest.fullConfirm": "Full-video analysis is about {m} min, {n} scenes. Continue?",
    "library.search": "Search projects",
    "library.empty": "No projects. Upload a reference from New.",
    "library.filter.recent": "Recent",
    "library.filter.review": "Needs review",
    "library.filter.export": "Ready to export",
    "library.confidence": "Confidence {c}",
    "library.loadFailed": "Could not load the list.",
    "status.uploaded": "Uploaded",
    "status.analyzing": "Analyzing",
    "status.review": "Needs review",
    "status.error": "Failed",
    "status.rejected": "Rejected",
    "analyze.running": "Analysis progress",
    "analyze.selectedRange": "Selected range",
    "analyze.etaLeft": "About {m} min left",
    "analyze.failed": "Analysis failed",
    "analyze.noProject": "Missing project ID.",
    "analyze.pollFailed": "Could not read status",
    "analyze.startFailed": "Could not start analysis",
    "analyze.step.shots": "Shots",
    "analyze.step.bg": "Background",
    "analyze.step.text": "Text",
    "analyze.step.textHint": "A few minutes per scene is allowed",
    "analyze.step.regions": "Regions",
    "analyze.step.sprites": "Sprite optimize",
    "analyze.step.keyframes": "Keyframes",
    "analyze.step.predicates": "Predicates",
    "analyze.step.report": "Report",
    "review.keepSave": "Save keep constraints",
    "review.reassign": "Reassign",
    "review.mask": "Mask",
    "review.bbox": "Box prompt",
    "review.text": "Text",
    "review.orig": "Original",
    "review.recon": "Reconstructed",
    "review.elements": "Elements {n}",
    "review.empty": "No elements. Draw a box on the original pane to add the first one.",
    "review.keep": "Keep constraints",
    "review.runReassign": "Run reassign",
    "review.runMask": "Apply mask",
    "review.runBbox": "Apply box",
    "review.runText": "Save text",
    "review.running": "Correction running: {op}",
    "review.retry": "{err}. Check the values and run again.",
    "review.error": "Error",
    "review.needProject": "project query is required.",
    "review.loadFailed": "Could not load state",
    "review.loading": "Loading scene",
    "review.loadingFrames": "Loading frames",
    "review.loadingList": "Drawing elements",
    "review.loadingRecon": "Drawing reconstruction",
    "error.liveaction": "Live-action footage cannot be opened.",
    "empty.elements": "No elements",
    "admin.login": "Admin login",
    "admin.enter": "Enter",
    "admin.tenants": "Tenants",
    "admin.queue": "Job queue",
    "admin.quarantine": "Quarantine",
    "admin.audit": "Audit log",
    "admin.policy": "Retry cap 4 · asset gen 2 · admins cannot raise limits.",
    "admin.ops": "Ops",
    "admin.email": "Email",
    "admin.password": "Password",
    "admin.back": "Back to maker",
    "admin.localOff": "This screen is not on the local edition.",
    "admin.loginFailed": "Login failed",
    "admin.manage": "Admin",
  },
};

function currentLang() {
  return localStorage.getItem("keepframe.lang") || "ko";
}

function T(key) {
  const lang = currentLang();
  return (STRINGS[lang] && STRINGS[lang][key]) || STRINGS.ko[key] || key;
}

function Tf(key, vars) {
  return T(key).replace(/\{(\w+)\}/g, (_, name) => (vars && vars[name] != null ? String(vars[name]) : ""));
}

function setTranslated(node, val) {
  if ((node.tagName === "INPUT" || node.tagName === "TEXTAREA") && node.hasAttribute("placeholder")) {
    node.placeholder = val;
    return;
  }
  const texts = [...node.childNodes].filter((n) => n.nodeType === Node.TEXT_NODE && n.textContent.trim());
  if (texts.length && node.children.length) {
    texts[0].textContent = val;
    return;
  }
  node.textContent = val;
}

function applyI18n(root) {
  const el = root || document;
  document.documentElement.lang = currentLang();
  el.querySelectorAll("[data-i18n]").forEach((node) => {
    setTranslated(node, T(node.getAttribute("data-i18n")));
  });
}

function toggleLang() {
  const next = currentLang() === "ko" ? "en" : "ko";
  localStorage.setItem("keepframe.lang", next);
  applyI18n();
  window.dispatchEvent(new CustomEvent("keepframe:lang", { detail: { lang: next } }));
}

function boot() {
  applyI18n();
  document.querySelectorAll("[data-lang-toggle]").forEach((btn) => {
    btn.addEventListener("click", toggleLang);
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}

export { T, Tf, applyI18n, toggleLang, currentLang };
