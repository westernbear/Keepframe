# Keepframe Round 2 — 남은 gap 수리 + 속도·술어·글자 리빌·3D + After Effects 해제 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. **Implementer = Codex** (`run-codex.sh` = `codex exec -m gpt-6.1-sol -c model_reasoning_effort=xhigh -s workspace-write -c sandbox_workspace_write.network_access=true -C <worktree>`; 샌드박스가 .git을 읽기 전용으로 두므로 컨트롤러가 전체 스위트를 샌드박스 밖에서 돌리고 커밋). 리뷰 = Claude 서브에이전트(과제 sonnet, 재리뷰 haiku, 최종 opus). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 1라운드에서 남긴 gap을 모두 닫고, 실제 클립의 남은 한계(느림, 술어 폭발, 글자 리빌 조각, 입체 객체 조각)를 고치며, After Effects 출력이 새 IR 기능과 실제 클립을 다루게 한다.

**Architecture:** IR에 트랙 3개(`reveal`, `rx`, `ry`)와 요소 플래그 1개(`pending_asset`)를 더한다. 분석은 (1) 점진적으로 드러나는 OCR 트랙을 리빌 트랙이 있는 텍스트 하나로 접고, (2) 조각난 입체 객체를 하나의 요소로 모아 회전을 측정한 뒤, 에셋 API(이미지→3D, Hugging Face Space 어댑터)로 GLB를 만든다. 생성기가 없으면 정적 sprite + "3D 후보" 표시로 남기고, 생성되더라도 원본 대비 국소 오차가 가장 낮은 표현(3D·정지 이미지·기존 조각)만 채택한다. 네이티브 컴포저·Lottie·AE가 새 트랙을 각자 표현하고, 표현 못 하면 호환성 이슈로 드러낸다(조용히 틀리지 않음).

**Tech Stack:** Python 3.12, pydantic 2, numpy/OpenCV, Pillow, RapidOCR, Playwright Chromium, GSAP + Three.js(벤더 포함), ExtendScript 패널(AE), gradio_client(어댑터 스크립트 전용, 코어 의존성 아님), pytest.

**Spec:** `docs/superpowers/specs/2026-09-05-keepframe-design.md` (§5.3 텍스트, §5.4 영역, §5.5 sprite, §5.9 술어, §6 보정, §7.2 편집, §9 실패 정책, §11 M5 3D·M6 AE). 지난 라운드: `docs/superpowers/plans/2026-10-03-keepframe-core-flow.md`, 결과 `docs/qa/core-flow/README.md`.

---

## Context

1라운드(2026-10-03~04, master `4d3ce07`)는 핵심 흐름을 실제 클립 4개로 검증했다. 남은 것:

| # | 구분 | 내용 | 출처 |
|---|---|---|---|
| G1 | gap | 에이전트가 `correct`(문구·폰트)와 `set_keep`으로 확인 없이 버전을 만든다 | Task 23 park |
| G2 | gap | CLI·에이전트 보정 작업 경로는 잘못된 폰트 이름을 거부하지 않고 sans-serif로 바꾼다 | 최종 재리뷰 Minor |
| G3 | gap | 움직이는 요소 3개 중 1개를 망가뜨려도 시간 유사도 0.81로 통과 | Task 22 park |
| G4 | gap | 프롬프트 평가의 "ok"는 타입이 맞는지만 본다(맞는 요소인지 아님). gate-m1/m3 미기록, 폰트 후보 UI 미검증 | 최종 리뷰 Minor |
| G5 | gap | 그라데이션 장면에서 텍스트 획·불투명도가 판(plate) 평균색 기준 | final fix 한계 |
| G6 | gap | 판 장면에서 GPU refine을 건너뜀 | final fix 한계 |
| G7 | gap | 채팅 첨부가 PNG/JPEG만(SVG·GLB 보류) | Task 6 ruling |
| L1 | 한계 | 150프레임당 9–31분(CPU). envato1: OCR 13.5분(검출이 90%+), 영역 8분, sprite ECC 6.6분 | 실측 |
| L2 | 한계 | 술어 폭발(ig1 55k): 모든 모션 쌍·요소 쌍 조합 | `analyze/constraints.py` |
| L3 | 한계 | 글자별 리빌이 제목을 조각냄("You just","ust","st") → ig3 프롬프트 3/8 | 실측 |
| L4 | 한계 | 입체로 보이는 객체(ig2 지구본)가 평면 조각들로 재구성 | 실측 |
| L5 | 한계 | AE가 이미지 배경(실제 클립 3/4)과 3D 요소를 거부 | `after_effects/compatibility.py:287,457` |

실측 근거(2026-10-04, 이 머신 4코어 CPU): `extract_regions` 20프레임 25.7초 중 `_region`의 전체 프레임 마스크 `np.nonzero`가 19.4초. RapidOCR 1080p 프레임당 2–3초 중 검출 1.9–3.0초, 절반 해상도 검출 0.77초.

### 사용자 결정(2026-10-04)
- 입체 객체 → **3D 에셋 경로**(이미지→3D). 백엔드 = **Hugging Face Space 어댑터**. 생성기에 닿지 못하면 **정적 sprite 하나 + "3D 후보" 표시**.
- 3D 충실도 가드 → **오차가 가장 낮은 표현을 채택**: 고체마다 원본 대비 국소 오차를 조각(현행)·정지 이미지·생성 3D 셋 다 재고, 3D는 최선 대안 + 0.005 이하일 때만, 아니면 정지 이미지, 정지 이미지가 조각보다 나쁘면 조각 유지. 항상 "3D 후보" 표시 + 세 값 기록. (생성기는 단일 크롭으로 뒷면·질감을 추측하므로 원본과 다를 수 있다는 점을 반영)
- 글자 리빌 → **텍스트 요소 하나 + 리빌 트랙**.
- 속도 → **2배 이상, 충실도 손실 없음**.
- After Effects → **동결 해제**. 사용자가 AE를 보유 — 브리지(패널) 설치 후 실기 점검 가능.

---

## Global Constraints

- 코어 런타임 의존성(`pyproject.toml` `dependencies`) 추가 금지. HF 어댑터는 `uv run --with gradio_client`로만 실행(코어 미포함). refine 테스트용 CPU torch는 기존 선택 extra 범위.
- 3D 생성은 기존 에셋 API(`KEEPFRAME_ASSET_API_URL`, `keepframe/assets/client.py`)로만. 어댑터는 127.0.0.1에만 바인드. HF 토큰·OAuth 토큰·계정 id는 출력·로그·보고서·커밋 어디에도 남기지 않는다.
- AE 변경은 닫힌 연산 카탈로그 방식 유지: 새 이펙트·레이어 종류는 `operations.py` 카탈로그, 패널 JSX 스키마, 검증을 함께 바꾸고 테스트로 고정. 표현 못 하는 의미는 호환성 이슈로 노출.
- 새 IR 트랙을 표현 못 하는 출력(Lottie·AE)은 조용히 버리지 않는다: 이슈/경고로 드러낸다.
- 실패를 숨기지 않는다: 선택 기능(캡션·3D 생성·refine) 실패는 `report.json` `messages`에 남기고 분석은 계속.
- 재시도 상한(edit 검증 4회, 에셋 생성 2회) 유지. 충돌은 `needs_choice`.
- UI 문구 ko+en(`i18n.js`). 정적 자산 캐시버스터는 모든 페이지가 같은 값 하나.
- 평가 데이터는 메인 체크아웃 `/home/singlerr/ref_stdio/eval`(gitignore; 워크트리에는 없음 — 절대경로로 지정). `eval/`, `.superpowers/`, `.impeccable/` 커밋 금지.
- 테스트: `.venv/bin/python -m pytest -p no:cacheprovider -q -m 'not browser and not gpu and not ocr'` (browser 표시 테스트는 해당 Task의 집중 실행에서 따로 돌린다)
- 커밋 메시지 끝: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- 머지 후 메인 체크아웃에서 `graphify update .`

### 분석 변경 채택 기준(Task 6–10, 13, 15)
분리된 detached 체크아웃(`PYTHONPATH=<checkout>`)에서 `gate-m2-real --render-check` + 합성 `gate-m2 --n 20`. 채택 = 합성 게이트 `passed` 유지 **그리고** 실제 클립 4개 중 3개 이상에서 `mean_l1` 악화 ≤ +0.005 **그리고** 해당 Task의 목표 지표 개선. 속도 Task는 추가로 클립별 텍스트 트랙 문구 95% 이상 기준선과 일치. 미달이면 `git revert` 후 README에 기록. 측정 후 `<out>/<clip>/scenes/*/stages`는 지워 디스크를 지킨다.

## Review Focus

1. 리빌이 있는 제목을 훨씬 긴 문구로 바꾸면 마지막 프레임에 새 문구 전체가 보여야 한다(리빌은 비율이라 잘리지 않음) → Task 12 `test_reveal_survives_longer_copy`.
2. 3D 생성기가 HTML 200, 시간 초과, 50MB 초과를 돌려줘도 분석은 정적 sprite + 메시지로 끝나야 한다 → Task 16 `test_generation_failures_fall_back`.
3. 그라데이션으로 칠해진 단단한 카드가 움직이는 장면은 3D 후보가 되면 안 된다 → Task 15 `test_rigid_gradient_card_is_not_solid`.
4. LLM이 `set_keep(targets=["e1"])`처럼 수천 개 술어에 걸리는 요청을 하면, 미리보기에 개수가 보이고 확인 시 정확히 그것만 바뀌어야 한다 → Task 2 `test_set_keep_preview_counts_and_confirm_applies_exactly`.
5. 영역 추출 최적화가 수동 마스크(override, label ≥ 1000)와 텍스트 제외 마스크가 있을 때도 결과를 바꾸면 안 된다 → Task 6 `test_crop_extraction_matches_reference_with_overrides`.

---

## Task 0: 작업 공간

- [ ] `git worktree add .worktrees/round2 -b feature/round2` (base `4d3ce07`), venv: `python3.12 -m venv .venv && .venv/bin/pip install -e '.[ocr,llm,dev]' && .venv/bin/python -m playwright install chromium`
- [ ] 계획 사본: `docs/superpowers/plans/2026-10-04-keepframe-round2.md`, SDD 워크스페이스·원장 생성, `run-codex.sh` 재작성(위 명령)
- [ ] 기준 테스트 1546 passed 확인 → Commit `docs: add round-2 plan`

## Phase A — 측정 기준

### Task 1: 끝단 충실도 지표(render_l1) + 프롬프트 정답 대조 + 기준선

**Files:** Modify `keepframe/gates.py`, `keepframe/cli.py`, `scripts/eval_prompts.py` · Create `docs/qa/round2/README.md` · Test `tests/test_render_fidelity.py`, `tests/test_eval_gold.py`

**Interfaces:** Produces `render_fidelity(root: Path, scene_id: str, clip: Path, sample_every: int = 10, max_frames: int = 150) -> dict` (`render_l1`, `frames`); `m2_gate_real(..., render_check: bool = False)` 행에 `render_l1`; CLI `gate-m2-real --render-check`; `scripts/eval_prompts.py --gold FILE` 행에 `correct`; `gold_match(scene: Scene, targets: list[dict], gold: dict) -> bool`.

- [ ] 실패 테스트: (a) 합성 장면을 렌더한 영상 자체를 clip으로 주면 `render_l1 < 0.01`, 요소 x를 20px 민 장면이면 더 큼. (b) `gold_match`: gold `{"property":"text","text":"A Weekend Away"}`는 대상 요소 `canonical.text`에 casefold 포함일 때만 참; gold `{"property":"color","at":[50,30,1.2]}`는 대상 요소 `element_bbox(el, round(1.2*fps))`가 (50%,30%) 점을 포함할 때만 참; `{"property":"background"}`는 element 없음과 property 일치.
- [ ] 구현: `render_fidelity` = `compose` → `render(html, scene, tmp, frames=idx)` → 원본을 cv2로 순차 읽어 같은 프레임과 비교(크기 다르면 렌더를 원본 크기로 resize), `mean(|a−b|)/255`. eval 스크립트: `--gold`가 있으면 각 행 `correct = typed_ok and gold_match(...)`, 합계 `correct` 출력.
- [ ] 정답 파일 `eval/gold/<clip>.json`(8개 프롬프트, 내용·위치 기준이라 재분석에도 유효)은 **컨트롤러가** 현재 브리프·스크린샷을 보고 작성(커밋 안 함).
- [ ] 기준선(master 코드): `gate-m2-real --clips $EVAL/clips --out $EVAL/out/r2-baseline --max-frames 150 --render-check`, `gate-m1/m2/m3`, 프롬프트 평가(+gold) → `docs/qa/round2/README.md` 표(요소, kinds, mean_l1, render_l1, low_conf, 술어 수, 단계별 초, 프롬프트 ok/correct).
- [ ] Commit `feat(gates): end-to-end render fidelity and gold-checked prompt eval`

## Phase B — gap 수리

### Task 2: `correct`·`set_keep`도 브라우저 확인 후 실행 (G1)

**Files:** Modify `keepframe/session/tools.py` (`_correct`, `_set_keep`, 스키마 설명), `keepframe/session/agent.py` (SYSTEM), `keepframe/web/static/js/agent.js` (`paintPending`), `keepframe/web/static/js/api.js`(필요 시), `i18n.js` · Test `tests/test_session_tools.py`, `tests/test_web_static.py`

**Interfaces:** `correct` → `needs_confirm=True`, payload `correction={"op","args"}`(작업 제출 안 함). `set_keep` → `needs_confirm=True`, payload `keep_change={"preset"} | {"targets","on","matched"}`(버전 안 만듦). 확인 클릭 → 기존 `/api/correct`, `/api/keep`(same-origin 검사 이미 있음).

- [ ] 실패 테스트: `run_tool("set_keep", ctx, {"preset":"all"})` → needs_confirm, 버전 수 불변. `{"targets":["e1"],"on":false}` → `matched`가 실제 일치 개수(Review Focus 4), 버전 불변. `correct` → needs_confirm, `ctx.submit_job` 미호출(스파이), op 오류는 `ok=False`. 정적 계약: `agent.js`가 `keep_change`/`correction`을 처리하고 `postKeep`/`postCorrect`를 호출.
- [ ] 구현: 두 도구를 `_pending(..., confirm=True, ...)`로. `agent.js` `paintPending`에 분기 2개(확인 버튼 → 기존 API → `refreshAfterEdit`; correct는 202 작업이라 기존 작업 폴링 후 새로고침). SYSTEM: "correct·set_keep도 미리보기만 한다".
- [ ] 브라우저 확인(`/browse`)은 Task 22 E2E에서. Commit `fix(agent): corrections and keep changes need browser confirmation`

### Task 3: 폰트 이름 검증 일원화 (G2)

**Files:** Modify `keepframe/ir/schema.py`, `keepframe/edit/intent.py`, `keepframe/web/server.py`(`/api/correct` 인라인 정규식, 에이전트 보정 작업 `FontGuess(**font)` 부근), `keepframe/cli.py`(correct) · Test `tests/test_font_validation.py`

**Interfaces:** `FONT_FAMILY_RE`, `validate_font_family(value: object) -> str`(잘못되면 `ValueError`) in `ir/schema.py`. 저장 데이터 로딩은 지금처럼 관대(`FontGuess` 검증기 유지).

- [ ] 실패 테스트: CLI `correct --op text --args '{"element_id":..,"font":{"family_guess":"x;color:red"}}'` → 종료코드 2, 새 버전 없음. 에이전트 보정 작업에 같은 값 → 작업 실패 + 메시지, 버전 없음. `/api/correct` 400 유지. `Target(property="font", value="Noto_Sans")` 거부 유지.
- [ ] Commit `fix(fonts): reject invalid font families on every input path`

### Task 4: 시간 유사도 요소별 하한 (G3)

**Files:** Modify `keepframe/verify/similarity.py`, `keepframe/verify/verifier.py`, `keepframe/edit/agent.py` · Test `tests/test_temporal_floor.py`

**Interfaces:** `temporal_by_id_detail(ref, out, min_share: float = 0.10) -> tuple[float, tuple[str, float] | None]`; `VerifyReport.temporal_worst: float | None`, `temporal_worst_element: str | None`; `edit.agent.TEMPORAL_ELEMENT_MIN = 0.5`.

```python
def temporal_by_id_detail(ref, out, min_share=0.10):
    weights = {eid: _evidence_weight(t) for eid, t in ref.items()}
    total = sum(weights.values())
    worst = None
    for eid, w in weights.items():
        if not total or w == 0 or w / total < min_share:
            continue
        s = tracklet_correlation(ref[eid], out[eid]) if eid in out else None
        s = 0.0 if s is None else s
        if worst is None or s < worst[1]:
            worst = (eid, s)
    return temporal_similarity_by_id(ref, out), worst
```
- [ ] 실패 테스트: 움직임 3개 중 하나를 통째로 밀거나 삭제 → 평균 0.81이어도 `_passed` False, 오류 문구에 그 요소 id. 움직임 비중 < 10% 조각만 망가지면 통과. 문구만 바꾼 편집 통과.
- [ ] Commit `fix(verify): fail edits that damage one significant motion`

### Task 5: 채팅에서 SVG·GLB 첨부 (G7)

**Files:** Create `keepframe/edit/svgraster.py` · Modify `keepframe/edit/apply.py`(`_DATA_URL`, texture·model 분기), `keepframe/edit/intent.py`(model value "attachment"), `keepframe/web/static/agent.html`(accept), `agent.js`, `i18n.js` · Test `tests/test_chat_attachments.py`(SVG 렌더 테스트는 `browser` 표시)

**Interfaces:** `rasterize_svg(svg: bytes, width: int, height: int) -> bytes`(PNG). 흐름: `sanitize_svg`(기존) → Chromium(JS 끔, 모든 네트워크 요청 abort)으로 상자 크기 ×2 렌더 → 기존 contain-fit 경로.

- [ ] 실패 테스트: SVG data URL 첨부 → 텍스처 PNG(알파 있음), 원래 상자 안에 맞음. `<script>`/외부 `href` 포함 SVG → 제거된 뒤 렌더(네트워크 요청 0). `data:model/gltf-binary;base64,…` 첨부 + model target → `canonical.model` 설정, kind "3d". 잘못된 GLB → 실패 메시지.
- [ ] Commit `feat(agent): accept SVG and GLB attachments in chat`

## Phase C — 속도 (2배 이상, 무손실)

### Task 6: 영역 추출을 성분 bbox 안에서 계산(정확히 같은 결과)

**Files:** Modify `keepframe/analyze/regions.py` · Test `tests/test_regions_crop.py`

```python
def _region_crop(frame_idx, frame, sub, x0, y0, label) -> Region:
    ys, xs = np.nonzero(sub)
    window = frame[y0:y0 + sub.shape[0], x0:x0 + sub.shape[1]]
    return Region(frame=frame_idx, label=label, color=tuple(float(v) for v in window[sub].mean(0)),
                  bbox=(x0 + int(xs.min()), y0 + int(ys.min()), x0 + int(xs.max()) + 1, y0 + int(ys.max()) + 1),
                  area=int(sub.sum()), centroid=(x0 + float(xs.mean()), y0 + float(ys.mean())),
                  mask=sub[ys.min():ys.max() + 1, xs.min():xs.max() + 1].copy())

# extract_regions 루프
        n, comp, stats, _ = cv2.connectedComponentsWithStats((labels == lab).astype(np.uint8), connectivity=8)
        for c in range(1, n):
            x, y, w, h, area = (int(v) for v in stats[c])
            if area >= min_area:
                out.append(_region_crop(frame_idx, frame, comp[y:y + h, x:x + w] == c, x, y, int(lab)))
```
- [ ] 실패 테스트(Review Focus 5): 무작위 팔레트 프레임 + override 마스크 2개(label 1000+) + exclude 마스크에서 새 함수 결과가 기존 구현(테스트 안에 그대로 복사한 참조 함수)과 모든 필드 동일.
- [ ] 측정: envato1 20프레임 영역 단계 ≥5배 빨라짐(README 기록). Commit `perf(regions): label components inside their bounding boxes`

### Task 7: OCR 검출 해상도 상한

**Files:** Modify `keepframe/analyze/text.py`(`RapidOcr`), `keepframe/analyze/pipeline.py`(`AnalyzeOptions.ocr_max_side: int = 1280`), `keepframe/cli.py`(`--ocr-max-side`) · Test `tests/test_ocr_cap.py`

**Interfaces:** `RapidOcr(max_side: int | None = 1280)` — 검출만 `max_side`로 줄이고(설치된 rapidocr_onnxruntime가 지원하는 `det_limit_side_len`/`det_limit_type="max"` 경로; 없으면 프레임을 축소해 검출 후 박스를 원래 좌표로 되돌리고 인식은 원본 크롭), 인식은 원본 해상도 크롭.
- [ ] 테스트: 가짜 엔진으로 상자 좌표가 원래 해상도로 복원되는지, `max_side=None`이면 기존 동작.
- [ ] 측정(Task 8과 함께 한 번): OCR 단계 ≥2배, 텍스트 트랙 문구 95% 이상 일치. Commit `perf(text): cap OCR detection resolution`

### Task 8: ECC를 축소 크롭에서

**Files:** Modify `keepframe/analyze/sprites.py`(`_refine_ecc`, `_refine_ecc_xy`) · Test `tests/test_ecc_downscale.py`

- [ ] ECC 입력 크롭·템플릿을 최대 변 384px로 축소(배율 s), 결과 affine의 이동 성분을 1/s로 되돌림. 384 이하면 그대로.
- [ ] 실패 테스트: 알려진 affine(이동 7.3px, 회전 4°, 배율 1.1)로 그린 640px sprite → 위치 오차 ≤ 0.5px, 회전 ≤ 0.5°.
- [ ] 측정(Task 6–8 합산, 채택 기준 + 전체 분석 시간 4개 중 3개 이상에서 ≥2배): `r2-speed`. Commit `perf(sprites): fit ECC on downscaled crops`

## Phase D — 남은 분석 gap

### Task 9: 판 장면의 획·불투명도를 국소 판 색으로 (G5)

**Files:** Modify `keepframe/analyze/text.py`(`stroke_mask`, `text_props`), `keepframe/analyze/sprites.py`(`estimate_opacity`, `sprite_props`), `keepframe/analyze/pipeline.py`(`_stage_sprites`에 plate 전달; `_stage_background`의 `bg` 평균색 주석 갱신) · Test `tests/test_plate_local_colour.py`

**Interfaces:** `text_props(..., plate: np.ndarray | None = None)`, `estimate_opacity(..., plate=None)`, `sprite_props(..., plate=None)`. plate가 있으면 획 마스크 = `foreground_mask_plate(crop, plate_crop)`, 불투명도 투영의 배경 = 픽셀별 판 색.
- [ ] 실패 테스트: 가로 그라데이션 판 위 불투명도 0.5 흰 글자 → 추정 0.5±0.1(평균색 방식은 벗어남), 획 마스크 IoU ≥ 0.9.
- [ ] 측정은 Task 10과 합산. Commit `fix(analyze): measure text and sprites against the local plate colour`

### Task 10: 판 배경으로 refine (G6)

**Files:** Modify `keepframe/analyze/refine.py`(`refine_affine(..., plate=None)`, `_upload_consts`), `keepframe/analyze/pipeline.py`(건너뛰기 분기 삭제) · Test `tests/test_refine_plate.py`(`pytest.importorskip("torch")`)

**Interfaces:** plate가 있으면 `consts["bg"]` = refine 해상도(h,w)로 줄인 판 `(1,3,h,w)` 텐서; 없으면 지금의 색 텐서.
- [ ] 실패 테스트(CPU torch를 venv에 설치해서 실행: `.venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu`): 그라데이션 판 + 정사각형 sprite의 초기 x를 3px 틀리게 → refine 후 오차 < 1px. 색 배경 회귀 테스트 유지.
- [ ] 측정(Task 9·10 합산): `r2-plate` 채택 기준, 그라데이션 클립 중 1개 이상 mean_l1 개선. CPU refine이 너무 느리면 ig2 하나만 `--refine` 켜고 시간·mean_l1 기록. Commit `feat(refine): composite sprites over the background plate`

### Task 11: 술어를 중요 요소 쌍으로 + 순서 이행 축약 (L2)

**Files:** Create `keepframe/ir/importance.py` · Modify `keepframe/analyze/constraints.py`, `keepframe/session/brief.py`(같은 순위 함수 사용) · Test `tests/test_constraints_scale.py`

**Interfaces:** `rank_elements(scene) -> list[Element]`(텍스트 먼저, 그다음 canonical 넓이 × 보이는 길이 — 브리프의 기존 기준을 옮김), `MAX_SALIENT = 24`, `extract_constraints(scene, max_salient=MAX_SALIENT)`.

```python
    salient = {e.id for e in rank_elements(scene)[:max_salient]}
    # 단항(type/dir/mag/dur)은 모든 모션
    key = [m for m in motions if m.element in salient]
    for m1 in key:                       # before: 바로 다음 시작 모션만(나머지는 이행으로 따라옴)
        later = [m2 for m2 in key if m2.element != m1.element and m2.start >= m1.end]
        if later:
            first = min(m2.start for m2 in later)
            preds += [f"before({m1.id},{m2.id})" for m2 in later if m2.start == first]
    for m1, m2 in combinations(key, 2):  # while: 중요 모션끼리만
        if m1.element != m2.element and min(m1.end, m2.end) - max(m1.start, m2.start) >= 1:
            preds.append(f"while({m1.id},{m2.id})")
    # 공간 관계: 마지막 프레임에 보이는 중요 요소 쌍만
```
- [ ] 실패 테스트: 조각 sprite 100개 + 텍스트 3개 합성 장면 → 술어 수가 단항 + O(24²) 이하. 중요 요소 둘의 순서를 바꾸면 keep 검증 실패(검출력 유지). `gate-m1` 통과(IR→렌더→재추출 일치). keep 프리셋 의미 불변.
- [ ] 실제: ig1 55k → ≤ 5k(README). Commit `perf(constraints): relate salient elements and reduce order chains`

## Phase E — 글자 리빌 (L3)

### Task 12: IR `reveal` 트랙과 모든 출력의 표현

**Files:** Modify `keepframe/ir/schema.py`(`PROPS`+`"reveal"`, `DEFAULTS["reveal"]=1.0`), `keepframe/ir/tracks.py`, `keepframe/analyze/keyframes.py`(`PROPS[:raw.shape[1]]`만 순회 — 8열 raw 하위호환), `keepframe/compose/template.html`(`apply()`에 `clip-path: inset(0 ${(1-st.reveal)*100}% 0 0)`, 1이면 비움; `DEFAULTS`에 reveal), `keepframe/analyze/composite.py`(텍스처 오른쪽 `(1−v)` 알파 0), `keepframe/verify/matrix.py`(모션 종류 `reveal`: mag=Δreveal, dur), `keepframe/render/lottie.py`(사각 마스크 정점 키프레임), `keepframe/after_effects/compatibility.py`(Task 19 전까지 `reveal` 이슈), `keepframe/session/brief.py`("reveals left→right 0.20s–0.90s") · Test `tests/test_reveal_track.py`(렌더 테스트 `browser`)

**Interfaces:** reveal = 왼쪽부터 보이는 폭 비율 0..1(클리핑, 레이아웃·bbox 불변 → 레이어 프로브 그대로). raw 9번째 열(index 8) = reveal.
- [ ] 실패 테스트: 0→1(10프레임) 리빌 텍스트 렌더의 5번째 프레임은 왼쪽 절반만 보임(Chromium·composite 둘 다), 마지막 프레임 전체. `test_reveal_survives_longer_copy`(Review Focus 1): 문구를 3배 길게 바꿔도 마지막 프레임 전체 보임. Lottie JSON에 마스크. AE 호환성에 reveal 이슈. 8열 raw 장면 로드·키프레임 정상. content_only 프리셋이 `type(m,'reveal')`·`dur` 유지.
- [ ] Commit `feat(ir): reveal track rendered by composer, composite and Lottie`

### Task 13: 점진 OCR 트랙을 리빌 텍스트 하나로

**Files:** Modify `keepframe/analyze/text.py`(`track_text` 연관 완화, `merge_reveals`, `text_props` reveal 열, `reveal_exclusion_boxes`), `keepframe/analyze/pipeline.py`(`_stage_text`, `_stage_regions` 제외 마스크) · Test `tests/test_text_reveal.py`

**Interfaces:** `merge_reveals(tracks: list[TextTrack], max_gap: int = 3) -> list[TextTrack]`, `reveal_exclusion_boxes(tracks) -> dict[int, list[tuple[int,int,int,int]]]`.
- 연관 완화: `geo = max(iou, inter/min(area), 1 − dist/max_dist)`, 문구는 `_sim ≥ 0.5` 또는 공백 제거·casefold 후 한쪽이 다른 쪽의 접두사.
- 병합: 앞 트랙 문구가 뒤 트랙 문구의 진접두사 + 왼쪽 끝 차이 ≤ 0.25×높이 + 기준선(y1) 차이 ≤ 0.3×높이 + 앞 트랙 끝이 뒤 트랙 시작 ±max_gap → 뒤 트랙이 흡수(연쇄 반복). 병합 트랙 문구 = 가장 넓은 프레임의 문구.
- reveal 열 = `(box.x1 − full.x0) / (full.x1 − full.x0)` 클립 [0,1](full = 가장 넓은 상자). 리빌 구간 프레임의 영역 제외 마스크는 full 상자 → 반쯤 드러난 글자가 sprite 조각이 되지 않음.
- `# ponytail: left-to-right reveals only; right/centre reveals stay separate tracks`
- [ ] 실패 테스트: 가짜 OCR이 "Y"→"You"→"You ju"→"You just"(왼쪽 고정, 넓어지는 상자)를 돌려주는 합성 영상 → text 요소 1개, reveal 키 0.1 근처→1.0, 상자 안 sprite 0개. 서로 다른 두 줄 제목은 합쳐지지 않음.
- [ ] 측정 `r2-reveal`: 채택 기준 + ig3 텍스트 요소·조각 감소, ig3 프롬프트 correct ≥ 6/8 목표. Commit `feat(text): fold letter-by-letter reveals into one text element`

## Phase F — 입체 객체 → 3D (L4)

### Task 14: IR `rx`/`ry` + `pending_asset` + Three.js 회전

**Files:** Modify `keepframe/ir/schema.py`(`PROPS`+`"rx","ry"`(도, 기본 0), `Element.pending_asset: Optional[Literal["3d"]] = None`), `keepframe/compose/template.html`(model 캔버스별 객체 보관, `apply()`에서 kind 3d면 `object.rotation.set(rx·π/180, ry·π/180, 0)`), `keepframe/compose/composer.py`, `keepframe/analyze/composite.py`(3d 요소는 `canonical.texture` 정적 그림), `keepframe/verify/matrix.py`(모션 `spin`: mag=Δ도), `keepframe/render/lottie.py`(3d → 정적 텍스처 이미지 레이어 + 경고), `keepframe/session/brief.py`("spins 360° …", "(3D 후보)"), 검수 인스펙터 배지(`review/inspector.js`, i18n `review.pending3d` "3D 후보"/"3D candidate") · Test `tests/test_ir_spin.py`(렌더 `browser`, 기존 GLB 픽스처 사용)

**Interfaces:** rx/ry는 kind "3d"에만 적용(sprite에는 무시 — 브리프·문서에 명시). Three.js 규약: ry 증가 = 앞면이 +x로, rx 증가 = 앞면이 화면 아래로.
- [ ] 실패 테스트: ry 0→90 GLB 장면 렌더 프레임 차이 발생, ry 없음과 다름. sprite에 ry가 있어도 렌더 불변. 8·9열 raw 하위호환. AE 호환성: 3d 이슈 유지(Task 20 전).
- [ ] Commit `feat(ir): 3D rotation tracks and pending 3D asset flag`

### Task 15: 조각난 입체 객체 통합 + 회전 측정

**Files:** Create `keepframe/analyze/solids.py` · Modify `keepframe/analyze/pipeline.py`(tracking 뒤 `solids` 단계, `_stage_sprites`가 고체 props 생성, `_elements_from_props`가 `pending_asset="3d"`), `keepframe/ir/synth.py`(`make_spinning_sphere_video`) · Test `tests/test_solids.py`

**Interfaces:** `Solid(frames: dict[int, tuple[bbox, mask]], members: list[int])`, `find_solids(frames, fg, obj_tracks, text_masks, min_life=12, min_members=3, change_thr=0.15) -> list[Solid]`, `estimate_spin(frames, masks, boxes) -> tuple[np.ndarray, np.ndarray]`.
- 덩어리 = 텍스트 제외 전경을 5px 팽창한 연결 성분, 프레임 간 IoU ≥ 0.5로 추적. 후보 = 수명 ≥ max(12, 장면의 30%) **그리고** (내부 객체 트랙 ≥ 3개이고 그 수명 중앙값 < 덩어리 수명의 50% **또는** 연속 프레임 64×64 회색 크롭의 평균 (1−NCC) ≥ 0.15이면서 bbox 크기 변화 < 10%). 객체 트랙은 프레임 절반 이상에서 중심이 덩어리 마스크 안이면 구성원.
- 회전: 
```python
def estimate_spin(frames, masks, boxes):
    """Yaw/pitch (deg) of a roughly convex solid from interior flow: u = ω·z on a sphere (orthographic).
    ponytail: sphere-like assumption; a learned pose tracker if real objects disagree."""
    n = len(frames); rx = np.zeros(n); ry = np.zeros(n)
    for f in range(1, n):
        rx[f], ry[f] = rx[f - 1], ry[f - 1]
        if masks[f - 1] is None or masks[f] is None:
            continue
        g0, g1 = (cv2.cvtColor(frames[i], cv2.COLOR_RGB2GRAY) for i in (f - 1, f))
        pts = cv2.goodFeaturesToTrack(g0, 200, 0.01, 4, mask=masks[f - 1].astype(np.uint8))
        if pts is None:
            continue
        nxt, st, _ = cv2.calcOpticalFlowPyrLK(g0, g1, pts, None)
        p, d = pts.reshape(-1, 2)[st.ravel() == 1], (nxt - pts).reshape(-1, 2)[st.ravel() == 1]
        x0, y0, x1, y1 = boxes[f - 1]; cx, cy, R = (x0 + x1) / 2, (y0 + y1) / 2, max(1.0, (x1 - x0 + y1 - y0) / 4)
        d = d - (_centroid(masks[f]) - _centroid(masks[f - 1]))
        z = np.sqrt(np.maximum(R * R - (p[:, 0] - cx) ** 2 - (p[:, 1] - cy) ** 2, 0))
        ok = z >= 0.3 * R
        if ok.sum() >= 5:
            ry[f] += np.degrees(np.median(d[ok, 0] / z[ok]))
            rx[f] += np.degrees(np.median(d[ok, 1] / z[ok]))
    return rx, ry
```
- 통합 요소: 구성원 트랙 제거, 하나의 sprite(`pending_asset="3d"`), canonical = 마스크 넓이 최대 프레임의 RGBA 크롭, x/y/sx/sy = 덩어리 bbox, raw 열 9·10 = rx/ry. 보고서 `messages`에 "3D 후보 N개". 구성원 트랙의 props는 버리지 않고 고체 props의 `fragments`에 보관(Task 16의 오차 비교·되돌림용).
- [ ] 실패 테스트: `make_spinning_sphere_video`(정사영 줄무늬 구, 6°/프레임 + 평행이동) → 고체 1개, ry 기울기 6±1.5°/프레임, rx ≈ 0. `test_rigid_gradient_card_is_not_solid`(Review Focus 3). 움직이는 단색 사각형 → 고체 아님. 텍스트 블록 → 고체 아님.
- [ ] 측정 `r2-solids`: 클립별 고체 수와 크롭을 README에 붙여 컨트롤러가 오탐 확인(ig1·ig3에 오탐 0 목표), ig2 요소 수 감소. 생성기 없는 정적 대체라 ig2 mean_l1 악화는 허용(사용자 결정) — 대신 Task 17 뒤 render_l1로 평가. Commit `feat(analyze): merge fragmented solids and measure their spin`

### Task 16: 참조 크롭 → 3D 생성(분석·편집), 실패 시 정적 대체

**Files:** Create `keepframe/analyze/solid_assets.py` · Modify `keepframe/analyze/pipeline.py`(`AnalyzeOptions.generate_3d: bool = True`, `_finish`), `keepframe/edit/intent.py`·`apply.py`·`agent.py`(model value `"reference"` → 요소 참조 크롭을 `input_image`로), `review/inspector.js`("3D 생성" 버튼 → 기존 편집 확인 경로), `i18n.js` · Test `tests/test_solid_assets.py`(기존 가짜 에셋 서버 패턴, `tests/test_edit.py` 참고)

**Interfaces:** `generate_solid_assets(scene: Scene, sd: Path, client) -> list[str]`(메시지). 성공: GLB 검증(`validate_glb`) → `assets/{eid}.model1.glb`, kind "3d", `pending_asset=None`, texture 유지(대체용). `KEEPFRAME_ASSET_API_URL` 없으면 "3D 후보 N개: 생성기 미설정". `solid_errors(frames, plate_or_bg, solid_props, glb_path | None, sample_every=5) -> dict[str, float | None]`(`fragments`, `still`, `model`), `choose_solid(errors) -> Literal["model","still","fragments"]`.
- **충실도 가드(사용자 결정)**: 고체 bbox 안, 보이는 프레임 5개마다 원본과 L1. 조각·정지 이미지 = 기존 `composite_scene`로 그 요소들만 그림; 3D = 그 요소 하나와 배경만 있는 미니 장면을 `compose`→`render`(Chromium, Three.js) 후 bbox 크롭. 규칙:
```python
def choose_solid(e):
    best_alt = min(v for k, v in e.items() if k != "model" and v is not None)
    if e.get("model") is not None and e["model"] <= best_alt + 0.005:
        return "model"
    return "still" if e["still"] <= e["fragments"] + 0.005 else "fragments"
```
  "fragments"면 보관한 구성원 props로 요소를 되돌리고(지금과 같은 결과) 가장 큰 구성원에 `pending_asset="3d"` 표시. 세 값은 `report.json` `solids[]`와 메시지에 기록.
- 결정성: 생성된 GLB는 버전 자산으로 저장되어 재렌더·재분석(`rerun`)에서 재사용, 다시 생성은 사용자가 "3D 생성"을 누를 때만. 생성 호출은 에셋 생성 상한 2회 안에서.
- [ ] `test_generation_failures_fall_back`(Review Focus 2): HTML 200, 타임아웃, 크기 초과, 잘못된 GLB → 분석 완료, 요소는 정적 sprite + `pending_asset="3d"`, 메시지. 편집 "3D 모델로 바꿔줘"(value reference) → 생성 요청에 참조 PNG 포함.
- [ ] `test_guard_picks_lowest_error`: 가짜 오차 표로 세 갈래(model/still/fragments) 모두, 경계값(+0.005) 포함. 원본과 다른 색 GLB(가짜 서버) → `still` 또는 `fragments`로 되돌아감(브라우저 표시 테스트). `rerun` 시 GLB 재생성 없음(가짜 서버 호출 0).
- [ ] Commit `feat(3d): generate solids from their reference crop via the asset API`

### Task 17: Hugging Face Space 이미지→3D 어댑터 + 실제 프로브

**Files:** Create `scripts/asset_adapter_hf.py`, `docs/qa/round2/3d-adapter.md` · Test `tests/test_asset_adapter_hf.py`(가짜 gradio 클라이언트 주입, 네트워크 없음)

**Interfaces:** `uv run --with gradio_client scripts/asset_adapter_hf.py --port 8790 [--space ID] [--probe]`. 127.0.0.1 전용 `ThreadingHTTPServer`, `keepframe.asset.request/1`의 `task=generate, kind=3d, input_image` → Space 호출 → `model/gltf-binary`; 그 외 501. 토큰: `HF_TOKEN` 또는 `huggingface_hub.get_token()`(출력 금지). Space 후보(차례로 프로브): `stabilityai/stable-fast-3d`, `tencent/Hunyuan3D-2`, `trellis-community/TRELLIS`. `--probe`는 `view_api()` 엔드포인트 이름과 작은 PNG 1회 생성 성공/실패만 출력.
- [ ] 이 머신에 HF 로그인이 없으면 컨트롤러가 사용자에게 `! huggingface-cli login`을 요청(AskUserQuestion).
- 공개 Space는 대기열·속도 제한이 있고 같은 입력에도 결과가 달라질 수 있다: 어댑터 타임아웃 300초, Space가 seed 입력을 받으면 고정값(0) 전달, 실패는 `asset_api_unavailable`로 돌려줘 분석은 대체 경로로 간다.
- [ ] 실측: 어댑터를 띄우고 `KEEPFRAME_ASSET_API_URL=http://127.0.0.1:8790` 로 ig2 재분석 → 가드가 고른 표현과 세 오차 값, 지구본 GLB 렌더 스크린샷, render_l1을 기준선과 비교(README). 3D가 뽑히지 않아도 결과 그대로 기록(실패가 아님). Commit `feat(scripts): Hugging Face Space adapter for image-to-3D`

## Phase G — After Effects (동결 해제)

### Task 18: 이미지 배경 → 맨 아래 푸티지 레이어

**Files:** Modify `keepframe/after_effects/compatibility.py`(L457 이슈 제거), `mapping.py`(배경 푸티지 레이어; `_safe_asset_reference` 재사용), `planning.py`·`verification.py`(필요 범위) · Test `tests/test_ae_mapping.py`, `tests/test_ae_compatibility.py`
- [ ] 판 배경 장면 → 대체안 없이 매핑, 맨 아래 footage 레이어(`assets/background.png`, comp 크기, 전 구간). 경로 탈출 시도 거부 유지. Commit `feat(ae): map image backgrounds as the bottom footage layer`

### Task 19: reveal → Linear Wipe

**Files:** Modify `keepframe/after_effects/operations.py`(`_EFFECT_PROPERTIES["ADBE Linear Wipe"] = {"ADBE Linear Wipe-0001" 완료%, "-0002" 각도, "-0003" 페더}`), `assets/keepframe_panel.jsx`(이펙트 스키마·카탈로그 프로브), `mapping.py`(reveal 트랙 → 이펙트 + 완료 키프레임 `(1−reveal)×100`, 각도 270°), `compatibility.py`(reveal 이슈 제거), `verification.py` · Test `tests/test_ae_reveal.py`
- [ ] 단위: 키프레임 값·시간·이징 대응, 카탈로그에 없는 AE면 호환성 이슈. 각도 부호는 Task 21 실기에서 확인(스크린샷). Commit `feat(ae): map reveal tracks to Linear Wipe`

### Task 20: 3D → AE 3D 모델 레이어(가능할 때), 아니면 대체안

**Files:** Modify `operations.py`(`AddLayerOperation` layer_type `"model"`, 3D 변환 속성 `ADBE Rotate X/Y`), `models.py`(capability `model_layers`), `assets/keepframe_panel.jsx`(AE 버전 ≥ 24.1이면 `model_layers` 보고, GLB `importFile`, comp 렌더러 Advanced 3D), `mapping.py`(kind 3d → model 레이어 + rx/ry 키프레임; capability 없으면 정적 텍스처 푸티지 대체안 제안), `compatibility.py`(3d 이슈를 capability 기준으로) · Test `tests/test_ae_model_layer.py`
- [ ] 단위: capability 있으면 model 레이어 + 회전 키프레임, 없으면 이슈 + 대체안(사용자 승인 필요 — 기존 대체안 흐름). Commit `feat(ae): place 3D elements as AE model layers`

### Task 21: AE 실기 점검(사용자 AE 머신)

**Files:** Create `scripts/ae_live_check.py`, `docs/qa/round2/ae-live-check.md`
- [ ] 스크립트: 워크스페이스에 점검용 프로젝트 3개 준비(합성 리빌 텍스트, ig2 판 배경, 합성 회전 구 + GLB), 각 프로젝트의 AE 내보내기 URL과 확인할 프레임 목록 출력.
- [ ] 문서: 서버 접근(같은 LAN이면 `--host 0.0.0.0` + 관리자 로그인, 아니면 `ssh -L 8765:127.0.0.1:8765` 권장), Windows에서 `pip install 'keepframe[ae]'` → `keepframe ae-install` → AE 환경설정(스크립트 파일·네트워크 허용) → `Window > Keepframe Panel` → `keepframe ae-connect`.
- [ ] 컨트롤러가 이 단계에서 사용자와 진행(AskUserQuestion으로 준비 확인). 기존 AE 검증(키프레임 일치·프레임 비교) 결과와 스크린샷을 README에 기록. 문제는 수정 Task로 되돌림.

## Phase H — 마무리

### Task 22: 최종 측정 + 브라우저 E2E + 문서

- [ ] `gate-m2-real --render-check`(`r2-final`), 합성 `gate-m1/m2/m3`, 프롬프트 평가(+gold) → 기준선 대비 표.
- [ ] gstack `/browse`: ig3 제목 리빌 편집("제목을 'Fall Drop'으로") → 확인 → 렌더 리빌 유지; 검수에서 폰트 후보 선택 적용; 에이전트 `set_keep`/`correct` 미리보기 → 확인; ig2 지구본(3D 또는 3D 후보 배지). 스크린샷 `docs/qa/round2/`.
- [ ] README(새 트랙·3D 어댑터·AE 지원 범위), 스펙 상태 주석(§5.3, §5.9, §11 M5·M6), `docs/qa/round2/README.md` 알려진 한계.
- [ ] `graphify update .`(머지 후 메인) · 브랜치 전체 리뷰 → finishing-a-development-branch.

---

## Verification (전체)

1. 단위/통합: 위 테스트 명령 전부 통과 + 각 Task의 `browser` 표시 테스트 집중 실행 통과.
2. 합성 게이트: `gate-m1`, `gate-m2 --n 20`, `gate-m3` passed.
3. 실제 클립 기준선 대비: 전체 분석 시간 ≥2배(4개 중 3개 이상), mean_l1 비악화(채택 기준), render_l1 기록, ig1 술어 ≤ 5k, ig3 제목 조각 해소, 프롬프트 correct 합계 개선.
4. 3D: 어댑터로 ig2 지구본 GLB 생성·렌더, 가드가 고른 표현과 조각·정지·3D 국소 오차 기록(3D가 더 나쁘면 채택되지 않는 것이 정상 동작). Space 불가 시 대체 경로와 원인 기록.
5. AE 실기: 판 배경·리빌·3D 장면이 사용자 AE에서 열리고 키프레임 일치(Task 21).
