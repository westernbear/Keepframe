const STRINGS = {
  ko: {
    "app.name": "Ref Studio",
    "nav.library": "Library",
    "nav.new": "새 레퍼런스",
    "nav.analyze": "분석",
    "nav.review": "검수",
    "lang.toggle": "EN",
    "ingest.start": "분석 시작",
    "ingest.range": "구간 지정",
    "ingest.full": "전체 영상",
    "ingest.confirm": "실행 전 확인",
    "library.search": "프로젝트 검색",
    "analyze.running": "분석 진행 상태",
    "review.keepSave": "유지 조건 저장",
    "review.reassign": "재할당",
    "review.mask": "마스크",
    "review.bbox": "박스 프롬프트",
    "review.text": "텍스트",
    "error.liveaction": "실사 푸티지는 열리지 않습니다.",
    "empty.elements": "요소 없음",
  },
  en: {
    "app.name": "Ref Studio",
    "nav.library": "Library",
    "nav.new": "New reference",
    "nav.analyze": "Analyze",
    "nav.review": "Review",
    "lang.toggle": "KO",
    "ingest.start": "Start analysis",
    "ingest.range": "Range",
    "ingest.full": "Full video",
    "ingest.confirm": "Pre-flight check",
    "library.search": "Search projects",
    "analyze.running": "Analysis progress",
    "review.keepSave": "Save keep constraints",
    "review.reassign": "Reassign",
    "review.mask": "Mask",
    "review.bbox": "Box prompt",
    "review.text": "Text",
    "error.liveaction": "Live-action footage cannot be opened.",
    "empty.elements": "No elements",
  },
};

function currentLang() {
  return localStorage.getItem("refstudio.lang") || "ko";
}

function T(key) {
  const lang = currentLang();
  return (STRINGS[lang] && STRINGS[lang][key]) || STRINGS.ko[key] || key;
}

function applyI18n(root) {
  const el = root || document;
  el.querySelectorAll("[data-i18n]").forEach((node) => {
    const key = node.getAttribute("data-i18n");
    const val = T(key);
    if (node.tagName === "INPUT" && node.hasAttribute("placeholder")) {
      node.placeholder = val;
    } else {
      node.textContent = val;
    }
  });
}

function toggleLang() {
  const next = currentLang() === "ko" ? "en" : "ko";
  localStorage.setItem("refstudio.lang", next);
  applyI18n();
  document.querySelectorAll("[data-i18n='lang.toggle']").forEach((btn) => {
    btn.textContent = T("lang.toggle");
  });
}

document.addEventListener("DOMContentLoaded", () => {
  applyI18n();
  document.querySelectorAll("[data-lang-toggle]").forEach((btn) => {
    btn.addEventListener("click", toggleLang);
  });
});

export { T, applyI18n, toggleLang };
