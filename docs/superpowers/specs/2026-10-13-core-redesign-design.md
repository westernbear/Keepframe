# Keepframe 코어 재설계 (v2) — 설계 초안

- 날짜: 2026-10-13
- 상태: 초안. **§4 변환 포맷은 사용자 결정 대기.** 나머지 절은 포맷 결정과 무관하게 성립한다.
- 근거: 코드 조사(분석 파이프라인, IR·렌더·편집 결합도), `docs/qa/*`, `docs/superpowers/*`, §3의 논문·웹 조사

## 1. 지금까지 진행

| 단계 | 목표 | 상태 | 핵심 측정값 |
|---|---|---|---|
| M1+M2 (09-05) | 결정적 코어 + 2D 분석기 | 완료 | gate-m1 20/20, gate-m2 20/20 (temporal .72) |
| UI·관리자 (09-06) | 메이커 화면, 관리자 | 완료 | — |
| Core flow (10-03) | 브리프, VLM 캡션, 타입 편집, 실제 클립 | 완료 | 프롬프트 22/32 유효, mean L1 ≤ .02는 ig3만 |
| Round 2 (10-04) | 속도, 술어 축소, 글자 reveal, 3D, AE | 완료 | 2.9–5.4× 빠름, 술어 55k→3.8k, TRELLIS 3D는 한 번도 안 이김 |
| AE 확장 1·2 (10-06/07) | CEP 패널, AE 대조 검증 | 완료 | 4/4 장면 통과, 큰 동기화 79 s |
| Stage A (10-07) | 깨끗한 레이어 | 병합 | 판 ΔE 13–25→0–1.5, α 경계 오차 게이트 미달 |
| Stage B (10-10) | 큰·키네틱 텍스트를 텍스트로 | 병합, 게이트 일부 실패 | 클립 타이틀 온전 6/8, ig3 색 ΔE 11.7, 폰트 top-3 .77 (< .9) |
| Stage C | AI가 읽는 장면 기술 | **미착수** | 스키마에 motions·beats·style 없음 |

Stage B의 시각 수정 라운드(V1–V5)는 세 번 돌며 매번 한 케이스를 고치고 다른 케이스를 깼고, 사용자가 "원칙에 근거한 수정만 남긴다"(10-12)고 정했다. 이것이 재설계의 직접 계기다: 지금 구조에서는 개선이 케이스별 임계값으로만 들어간다.

## 2. 진단: 무엇이 구조적으로 막혀 있나

**분석 (`keepframe/analyze`, ~9.6k줄)**
- 결정이 여러 패스에 흩어지고 뒤 패스가 앞 패스를 되돌린다. text/shape 분류(`text.keep_text_track`) → mover 점유 → solid 실패 시 mover 전부 해제(`pipeline.py:426`) → sprites에서 mover 탈락 → `merge_titles` → `guard_titles`가 나쁜 병합을 되돌림(`titles.py:653`) → `choose_solid`가 다시 조각으로 되돌림(`solid_assets.py:349`).
- 같은 양을 여러 번 잰다. 텍스트 색 4곳(`text_props`, `full_opacity_frame`, `merge_title`, `textstyle.fill_colour`), 불투명도 6곳, 판(plate) 표현 6가지, 병합·그룹 메커니즘 9가지.
- sprites 단계 이후 데이터는 타입 없는 `props` dict(키 `t*`/`s*`/`o*`/`m*`/`title_*` + 플래그 ~30개)이고, 메시지용 `_` 키가 같은 dict에 섞인다.
- 클립·시드 이름이 주석에 박힌 임계값: `movers.py:28,32`, `textstyle.py:46,765,778`, `text.py:511`, `titles.py:178,668`. 모듈 상수 ~110개.

**렌더 (`render`, `compose`, `ae`, `analyze/composite.py`)**
- "t 프레임의 요소"를 계산하는 독립 구현이 5개(GSAP 템플릿, numpy, Lottie, AE spec, 텍스트 스타일 3벌). 기울임 판정 규칙 3가지, 동적 z 정책 4가지. Lottie는 텍스트 스타일을 조용히 버린다.
- 텍스트가 두 번 저장된다: 문자열+폰트+스타일, 그리고 그 래스터. Chromium·AE는 앞을, numpy는 뒤를 그리므로 모든 편집이 둘을 맞춰야 한다.
- `render/plan.py`는 평가 계층이 아니라 승인·에셋 고정 매니페스트다.

**에이전트·검증**
- LLM은 IR이 아니라 손실 요약(`session/brief.py`)을 본다. 브리프는 affine bbox라 기울어진 요소는 근사치다.
- `Group.reason == "text_fragments"` 문자열이 타입 구실을 하고 apply·intent·agent·brief 네 곳에서 반복된다.
- 편집 검증은 DOM bbox만 본다. reveal·opacity·글리프 마스크·색·스타일은 편집 루프에서 검사되지 않는다.
- 스키마 버전 문자열은 검사되지 않고 pydantic 기본값이 모르는 필드를 조용히 버린다.

**평가**
- 실제 클립 4개 + 합성 시드 10개로 게이트를 맞추다 보니 수정이 평가셋에 맞춰진다(V1/V5 되돌림). 외부 정답 데이터가 없다.

## 3. 연구 근거

| 결론 | 출처 |
|---|---|
| VLM은 영상을 실행 가능한 HTML/CSS/JS로 거의 100% 옮기고 외형 유사도 0.84까지 내지만, 시간 유사도는 최대 0.31. 프레임레이트를 올려도, 파인튜닝해도(0.08–0.09로 악화), 3회 자기수정(0.28)으로도 해결 안 됨 → **타이밍은 CV가 재고, 포맷에 숫자로 명시해야 한다** | Animation2Code, arXiv 2606.28593, Table 2 |
| LLM이 원시 Lottie JSON을 직접 쓰면 성공률이 낮다(중첩 구조, 불변 메타데이터 토큰). 두 연구 모두 전용 토크나이저로 우회 → **Lottie는 내보내기 대상이지 LLM이 읽고 쓰는 포맷이 아니다** | OmniLottie (CVPR 2026, arXiv 2603.02138) §1; LottieGPT (arXiv 2604.11792) |
| 전문 Lottie 100만 개의 변환 궤적을 군집하면 "fade-in + upward + scale-down" 같은 정형 모션 템플릿이 나온다 → **모션은 소수의 프리셋 + 파라미터로 대부분 기술된다** | OmniLottie §3.1 "Motion Library" |
| 1차 논리 술어 DSL로 모션을 검증하고 실패 보고를 LLM에 되먹이면 정답률 58.8%→93.6%(최대 50회) | MoVer (TOG 2025, arXiv 2502.13372) |
| 애니메이션 그래픽 = 정적 텍스처 스프라이트 + affine + 불투명도. 공개 정답 데이터 Crello Animation(299개, CDLA-Permissive-2.0)과 공식 코드 존재 | Fast Sprite Decomposition (ECCV 2024, arXiv 2408.03923); `cyberagent/crello-animation` |
| 텍스트 편집은 디렌더링 문제: 문자열·위치·크기·폰트·채움·테두리·그림자를 추정해 다시 그린다. 색 오차가 화질에 가장 크게 작용 | De-rendering Stylized Texts (ICCV 2021, arXiv 2110.01890) |
| 모션그래픽 영상 → 편집 가능한 SVG 모션 프로그램 + 프로그램 변환 API | Motion Vectorization (SIGGRAPH Asia 2023, arXiv 2309.14642) |
| 래스터 디자인을 레이어로 분해: 반복적 전경 추출(LayerD, ICCV 2025), 단일 패스 RGBA 레이어 확산 모델(Qwen-Image-Layered) | arXiv 2509.25134; arXiv 2512.15603 |
| HTML + `data-start`/`data-duration` + 일시정지된 GSAP 타임라인을 영상 포맷으로 쓰는 오픈소스, 에이전트용 스킬 동봉 (Keepframe 컴포저가 이미 이 속성과 호환) | HeyGen HyperFrames |

## 4. 핵심 결정: 영상을 AI가 이해하는 텍스트로 바꾸는 포맷

### 4.1 요구사항

| # | 요구 | 근거 |
|---|---|---|
| R1 | 프레임 없이 텍스트만으로 무엇이 언제 어떻게 움직이는지 이해 가능: 역할, 모션 어휘, 타이밍 관계, 스타일 | Stage C 목표 |
| R2 | 편집이 작은 국소 변경이고, 검증기가 정확한 오류를 돌려준다 | §2 에이전트 |
| R3 | 결정적 렌더가 원본을 재현: 프리셋으로 안 맞는 측정 모션도 잃지 않는다 | 원 스펙 §4 불변조건 1·2 |
| R4 | AE(편집 가능한 레이어), Lottie, MP4로 1:1에 가깝게 내보낸다 | M6 |
| R5 | 분석기가 직접 쓰고, 값마다 신뢰도·출처를 단다 | 원 스펙 §4 |
| R6 | 술어를 렌더 없이 문서에서 계산한다 | MoVer |
| R7 | 모든 렌더러가 같은 의미를 쓴다 | §2 렌더 |

### 4.2 후보

| 후보 | R1 이해 | R2 편집 | R3 재현 | R4 내보내기 | R6 술어 | 판정 |
|---|---|---|---|---|---|---|
| HTML/CSS/GSAP (HyperFrames) | 좋음: LLM이 가장 잘 아는 언어 | 자유 JS면 검증·변환 불가 | 좋음 | AE·Lottie로 역변환 어려움 | 렌더 후에만 | 출력 대상으로 유지 |
| Lottie JSON | 나쁨: 숫자 코드 키, 깊은 중첩 | 나쁨 (§3) | 좋음 | Lottie 그 자체, AE는 bodymovin | 가능 | 내보내기 대상 |
| SVG 모션 프로그램 | 보통 | 보통 | 텍스트 스타일·래스터 약함 | 중간 | MoVer가 SVG 위에서 동작 | 참고 |
| AE ExtendScript/JSX | 보통 | 명령형이라 나쁨 | 좋음 | AE 전용 | 어려움 | 내보내기 대상 |
| 현 IR JSON + 브리프 | 브리프는 손실 요약 | 고정 연산만 | 좋음 | 5개 독립 번역 | 렌더 기반 | 교체 대상 |
| **Motion Score (권장)** | 좋음: 레이어·역할·모션 클립·비트 | 타입 패치 연산 + 스키마 검증 | 프리셋 + 잔차 키로 무손실 | 레이어 모델이 AE/Lottie와 1:1 | 문서에서 직접 | **권장** |

### 4.3 권장안: Motion Score (`keepframe.score/2`)

한 장면 = 한 문서. 원 스펙의 L0/L1/L2를 텍스트 우선으로 다시 정의한다.

1. **레이어 모델은 AE/Lottie 의미론과 1:1.** 레이어 타입 `plate | image | video | text | shape | group | model3d`. 변환 `anchor, position, scale, rotation, rx, ry, opacity`, 마스크(`reveal`), 텍스트 애니메이터(글리프 스태거), 효과(`stroke, shadow, glow, gradient`). AE·Lottie 개념은 학습 데이터에 흔해서 LLM 이해를 돕고, 내보내기가 번역이 아니라 이름 바꾸기가 된다.
2. **모션 = 프리셋 클립 + 잔차.** 닫힌 어휘 `fade_in/out, slide_in/out(dir, by), scale_pop, mask_reveal(dir), stagger_glyphs(step, ramp), tilt3d, float, rotate, pulse, camera_push` + 이징 이름. 분석기는 측정 궤적에 클립을 맞추고 오차(`err`, px·프레임)를 기록한다. 허용 오차(원 스펙의 2 px / 1프레임)를 넘으면 그 구간은 명시 키(`keys`)로 남긴다. 그래서 문서는 항상 원본에 충실하고, 가능한 곳에서만 의미적이다.
3. **외형은 한 번만 저장.** 텍스트는 문자열·폰트·채움·효과 파라미터뿐, 래스터는 렌더 시 파생 캐시. 이미지·영상 레이어는 에셋 경로 + 텍스처 메타.
4. **의미층.** `role`(headline, cta, logo, ui_card …), `beats`(모션 시작·끝 군집), `style` 토큰(팔레트·타입 램프), `keep` 술어(MoVer식 문장). VLM은 문자열·역할·이름만 채우고 숫자는 넣지 않는다.
5. **측정 원본(L0)은 문서 밖.** 프레임별 행렬·박스는 지금처럼 npz 사이드카로 두고 문서는 경로만 가진다. LLM 컨텍스트에 들어가지 않는다.
6. **표기는 JSON + 버전 검사 + 모르는 필드 거부.** 저장·검증·LLM 열람이 같은 문서다. 따로 유지하는 브리프는 없앤다. 편집은 JSON Pointer 경로의 타입 패치 연산(`set`, `retime`, `swap_asset`, `add_layer`)으로 받고 스키마로 검증한다(현 Responses 클라이언트의 구조화 출력과 맞음).

예시 (축약):

```json
{
  "format": "keepframe.score/2",
  "canvas": {"w": 1080, "h": 1920, "fps": 30, "frames": 150},
  "style": {"palette": {"brand": "#a8001c", "ink": "#111111"},
            "type": {"display": {"family": "Urbanist", "weight": 800}}},
  "beats": [{"id": "b1", "t": 0}, {"id": "b2", "t": 18}, {"id": "b3", "t": 54}],
  "layers": [
    {"id": "bg", "type": "plate", "fill": {"linear": 90, "stops": ["#ffffff", "#ffe3ea"]}},
    {"id": "title", "type": "text", "role": "headline", "text": "A Weekend Away",
     "font": {"token": "display", "size": 96, "tracking": -2},
     "fill": "brand", "effects": [{"shadow": [0, 6, 18, "#00000044"]}],
     "at": [540, 820], "span": [18, 149], "conf": 0.91,
     "motion": [
       {"clip": "slide_in", "from": "down", "by": 80, "t": [18, 30], "ease": "out_cubic", "err": 0.6},
       {"clip": "stagger_glyphs", "step": 2, "ramp": 5, "t": [18, 44]},
       {"keys": {"y": [[120, 820, "linear"], [126, 812], [149, 812]]}}
     ]},
    {"id": "cta", "type": "group", "role": "cta", "children": ["cta_bg", "cta_label"], "span": [54, 149],
     "motion": [{"clip": "scale_pop", "t": [54, 62], "ease": "out_back", "err": 0.9}]}
  ],
  "keep": ["dir(title.slide_in) = up", "before(title, cta)", "dur(title.slide_in) = 12f"]
}
```

### 4.4 다른 후보를 정본으로 두지 않는 이유

- **HTML/GSAP:** 자유 CSS·JS는 무한 집합이라 AE·Lottie 내보내기와 술어 계산이 렌더에 의존하게 된다. 원 스펙 §7.2도 "자유 JS 생성 금지"다. HTML은 Score에서 결정적으로 생성하는 첫 번째 출력으로 남는다.
- **Lottie:** LLM 성공률이 낮고(§3), 텍스트 스타일·업로드 폰트·영상 판 표현이 약하며 역할·비트가 없다. Score → Lottie 내보내기는 유지한다.
- **자체 DSL 문법:** 더 짧지만 파서와 LLM 미숙지 비용이 든다. JSON이 길어지는 문제는 모션 클립이 대부분 흡수한다.

## 5. 분석 재설계: 합성에 의한 분석, 단일 선택 기준

원칙: **가설은 모두 Score 조각이고, 같은 평가기로 렌더해 원본과 비교하며, 채택은 하나의 비용으로 정한다.**

비용 = 재구성 오차(Lab ΔE 기반, 가시 프레임) + λ · 기술 길이(레이어 수, 키 수, 파라미터 수). 병합·분할·종류 결정(조각 vs 타이틀, 스프라이트 vs 텍스트 vs 3D 후보, 판 vs mover)은 서로 경쟁하는 가설로 같은 비용에서 비교한다. `guard_titles`(L1 + 0.005), `choose_solid`, mover 탈락 같은 사후 되돌림 패스가 사라진다. λ는 전역 하나이며 §7의 개발 분할에서 보정하고 보정 기록을 남긴다.

| 단계 | 하는 일 | 지금 코드에서 가져올 것 |
|---|---|---|
| 1 샷 | 샷 경계 | `analyze/shots.py` |
| 2 관측 | 결정 없는 순수 추출기: OCR 박스, 팔레트 영역, 모션 에너지, ECC | `text.ocr_frames`, `regions`, `movers.motion_energy`, `plane.track_plane` |
| 3 가설 | 판(정적·그라디언트·영상), 스프라이트 궤적, 텍스트 궤적, mover를 타입 있는 `Hypothesis`로 제안 | `plate.build_plate`, `tracking`, `text.track_text`, `movers.find_movers` |
| 4 적합 | 스프라이트: 정적 텍스처 + affine + 불투명도, 판 대비 매팅. 텍스트: 문자열·폰트·채움·효과·평면으로 디렌더 (render-and-compare) | `matting.texture_v2`, `refine.refine_affine`, `textstyle`, `fonts/match`, `plane` |
| 5 선택 | 픽셀을 덮는 가설 집합을 비용 최소로 탐욕 선택 | 신규 (가드·choose_solid 대체) |
| 6 추상화 | 레이어별 모션 클립 + 잔차, 비트 군집, 스타일 토큰 | `keyframes.tracks_from_raw`, `verify/matrix.extract_motions` |
| 7 의미 | 선택: VLM이 문자열·역할·이름만 | `reading.read_titles`, `captions` |
| 8 술어 | Score에서 keep 술어 | `constraints`, `verify/predicates` |

규칙:
- 단계 사이 데이터는 타입 있는 dataclass. `props` dict와 메시지 섞인 `_` 키를 없앤다. 단계 캐시는 내용 해시로.
- 양마다 측정 함수 하나: 색은 `fill_colour`, 불투명도는 `opacity_against_plate`, 판은 `PlateModel` 하나.
- 클립·시드를 근거로 든 상수 금지. 상수는 물리량(px, ΔE JND ≈ 2.3)이거나 개발 분할 보정값이다.

## 6. 렌더·편집·검증

- **평가기 하나:** `bake(score)`가 클립을 키로 펼치고 토큰을 풀어 정규화된 레이어 모델을 만들고, `evaluate(baked, t)`가 레이어별 행렬·불투명도·reveal·글리프 알파·스타일을 낸다. numpy 렌더, 검증, 술어가 이것을 쓴다. HTML 템플릿·AE·Lottie 방출기는 baked 모델만 읽고 자체 z·그라디언트·기울임 규칙을 갖지 않는다. 백엔드 간 동등성은 같은 Score에 대한 프레임 비교 테스트로 묶는다.
- **텍스트:** numpy 렌더도 파라미터에서 그린다(`fonts/raster.render_styled` 존재). 래스터는 캐시일 뿐이다.
- **편집:** 타입 패치 연산. 그룹은 1급 레이어(`group` + `children`). 조각난 타이틀은 문자열 태그 그룹이 아니라 글리프 범위를 가진 한 텍스트 레이어다.
- **검증:** Score 위의 술어 + 렌더 프레임의 외형 검사(reveal·불투명도·색). 실패는 술어 단위 메시지로 에이전트에 돌려주고 최대 3회 수리(Stage C 결정 유지).
- **에이전트가 보는 것:** Score 자체(중요도 순 생략 허용). 브리프 생성 코드는 없앤다.

## 7. 평가

- **외부 정답:** Crello Animation(검증 154 / 테스트 145, 스프라이트 텍스처 + affine + 불투명도 정답)으로 레이어 분해를 잰다. 평가셋 과적합을 끊는 핵심. 텍스트-as-텍스트 정답은 없으므로 텍스트는 합성으로.
- **합성 왕복:** 생성기가 Score를 내고 → 렌더 → 분석 → Score 비교(레이어 매칭, 클립 종류·파라미터, 문자열·폰트·색). 지금의 `ir/synth.py` 시드를 Score 생성기로 옮긴다.
- **실제 클립:** 기존 4개 + 추가 레퍼런스(수집에 네트워크 필요, §9).
- **AI 이해도:** Score만 주고 질의응답·편집 과제를 시켜 정답률을 잰다(`scripts/eval_prompts.py` 확장).
- **유지:** render L1, 편집 후 검사(`qa/edits.py`), AE 대조.

## 8. 이행 단계

각 단계는 단독으로 병합 가능하고 테스트가 녹색이어야 한다.

| 단계 | 내용 | 완료 게이트 |
|---|---|---|
| P0 포맷 | Score 스키마(버전 검사, 모르는 필드 거부), `score_from_scene`/`scene_from_score` 변환, `bake`, 골든 Score | 합성 20장면 scene→score→scene 렌더 해시 동일 |
| P1 평가기 | numpy·HTML·AE·Lottie가 baked 모델에서 방출. 중복 로직 삭제 | 백엔드 간 프레임 동등성 테스트, 기존 게이트 유지 |
| P2 모션 추상화 | 클립 적합 + 잔차, 비트, 스타일 토큰, 역할 (Stage C 흡수) | 합성 왕복 클립 종류 정확도, 렌더 해시 불변 |
| P3 편집·검증 | 패치 연산, 1급 그룹, Score 술어 + 외형 검사, 수리 ≤ 3 | 프롬프트 평가 ≥ 현재, 편집 후 검사 유지 |
| P4 분석 구조 | 타입 가설, 합성에 의한 선택, 측정 함수 단일화, Crello 벤치 | Crello 지표 기준선 확보, 실제 클립 게이트 ≥ Stage B |
| P5 텍스트 디렌더 | 텍스트 모듈 통합(text·titles·textstyle·reading·plane) | 타이틀 온전·색 ΔE·폰트 top-3 ≥ Stage B |

P0–P3는 분석기를 건드리지 않으므로 지금 결과를 그대로 새 포맷에 싣고 먼저 에이전트 쪽 이득을 본다. P4–P5가 분석 품질 작업이다.

## 9. 위험과 열린 질문

- **포맷 표기(§4):** JSON 권장. 대안은 HTML 정본 또는 YAML.
- **프리셋 어휘 크기:** 너무 작으면 잔차 키가 늘고, 너무 크면 LLM 이해가 떨어진다. Crello·실제 클립에서 클립 커버리지(잔차 없는 구간 비율)로 정한다.
- **λ 보정:** 개발 분할에서 한 번, 기록과 함께.
- **네트워크:** 이 클라우드 환경은 huggingface.co, github.io, youtube.com, elements.envato.com 접속이 정책으로 막혀 있다. Crello 다운로드와 레퍼런스 영상 수집은 허용 도메인 추가 또는 `home` 머신에서.
- **GPU:** 합성에 의한 적합은 CPU에서 느릴 수 있다. 지금처럼 torch refine은 선택, 기본 경로는 CPU.
- **AE 라이브 확인 4·5**(그라디언트·영상·스타일 텍스트, 기울임 부호·회전 순서)는 여전히 사용자 단계.
