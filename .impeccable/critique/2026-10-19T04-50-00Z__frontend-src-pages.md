---
target: all SPA pages
total_score: 21
max_score: 40
na_heuristics: 
p0_count: 1
p1_count: 2
target_identity: "file:/home/user/Keepframe/frontend/src/pages"
timestamp: 2026-10-19T04-50-00Z
slug: frontend-src-pages
---
# Critique: Keepframe web app (frontend/src/pages) — 21/40

Heuristics: 1 status 3 (ingest stepper copy "1단계 중 1단계"; no agent step) · 2 real world 2 (jargon: IR, 술어 추출, sprite 최적화, 재할당 출발/도착, 백엔드 Native, 확인 토큰, raw predicate ids) · 3 control 2 (/analyze 취소 is only a link; keep presets apply without preview) · 4 consistency 2 (white pill = CTA/approve/user bubble/focus; mixed-language labels) · 5 error prevention 2 (approve one click, drops pending keep changes) · 6 recognition 2 (typed bbox coords, colour-only element identity) · 7 efficiency 2 (Ctrl+K navigation only) · 8 minimalism 3 (review header ~12 controls) · 9 recovery 1 ("서버 로그를 확인하세요", dead failed/rejected library rows, no retry) · 10 help 2 (keep conditions never explained).

Specificity: landing scrubber hero and review overlay are product-specific; operate chrome and gradient step cards are category-interchangeable (DESIGN.md is a Framer analysis). Review does not show measured values with provenance as the landing promises. Detector: source scan 0 (regex-only); browser: low-contrast /library placeholder 3.4:1 (Smooth UI Input), icon-tile-stack admin/tenants inspector, radial-halo + 9px NEW on landing (false positives: mock clip artwork).

Priority issues:
- [P0] /analyze 취소 does not cancel (pages/analyze/index.tsx); no retry after failure — harden.
- [P1] Approve: no confirm/summary, drops keepPending (review/core.ts approve), lives in global header — clarify, layout.
- [P1] No recovery for makers: server-log error copy, dead failed/rejected rows, no chat retry, server 404 JSON — harden.
- [P2] Keep conditions buried in collapsed details with raw predicate ids; presets apply instantly; inspector children shrink (search ~16px, forms pushed out of scroller) — layout, clarify.
- [P2] Jargon and mixed language across operate surfaces — clarify.

Personas: AE power user (no keyframes/ease on timeline, Ctrl+K no actions, AE overwrite yes/no, no rename); first-time marketer (jargon, collapsed keep conditions, /agent without project, stepper stops at review); mobile operator (tenants usage column clipped, ISO timestamps wrap, no requeue/release actions, clipped back link on /agent).

Minor: Hangul display tracking -0.045em too tight; mono font not bundled; user bubble looks like primary button; danger red for "no project"; landing shows 3 steps for a 4-step flow; uneven timeline ticks; disabled start button reads secondary.

Questions: why does review show fewer measurements than the landing demo? should review be a guided sequence? what would a Keepframe-native visual language look like?
