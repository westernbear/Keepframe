# Keepframe 코어 재설계 (v2) — 설계 초안

- 날짜: 2026-10-13
- 상태: 초안. 2026-10-13 사용자 결정: 정본 포맷은 **Motion Score JSON**, 세밀한 표현 방법을 추가(§4.5), 이행은 **P0 포맷부터**.
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

### 4.5 세밀한 표현

원칙: **세밀함은 의미층을 대체하지 않고 그 아래에 겹친다.** 프리셋 클립 하나로 끝나는 곳은 짧게, 측정이 더 말하는 곳은 같은 항목 안에서 더 깊이 내려간다. 의미 편집(길이·방향 변경)을 해도 측정된 미세 움직임은 살아남는다.

**1. 상세도 사다리.** 모션 항목 하나는 네 수준 중 하나로 쓰고, 측정 궤적 대비 오차 `err`(px, °, 불투명도)를 항상 단다.

| 수준 | 표기 | 예 |
|---|---|---|
| 클립 | 프리셋 + 파라미터 | `{"clip": "slide_in", "from": "down", "by": 80, "t": [18, 30]}` |
| 곡선 | 클립 + 임의 이징: cubic-bezier 4값, 다구간, `spring{stiffness, damping, mass}`, `steps(n)`, `hold` | `"ease": {"spring": [180, 12, 1]}` |
| 키 | 속성별 키프레임. 시간 베지어 손잡이(AE 속도·영향), 공간 접선(곡선 경로), 차원 분리, 소수 프레임 시각 | `{"keys": {"pos": [[18, [540, 900], {"out": [0.33, 1]}, {"to": [0, -40]}], [30, [540, 820]]]}}` |
| 샘플 | 모델로 설명 안 되는 나머지의 프레임별 곡선. npz 사이드카 참조만 문서에 | `{"samples": "raw/title_y.npz#res", "err": 0.2}` |

**2. 합성 규칙: 클립 ⊕ 잔차.** 최종값 = 기준값 ⊕ 클립 기여 ⊕ 잔차(u). 위치·각도·기울임은 더하고, 스케일·불투명도는 곱한다. 잔차는 클립 로컬 시간 u ∈ [0, 1]로 저장하므로, 클립 길이를 12→20프레임으로 바꾸면 측정된 오버슈트·흔들림이 같은 비율로 늘어나 따라간다. 잔차를 버리고 싶으면 `"residual": "drop"` 한 줄이다.

**3. 표현 가능한 속성.** 모든 항목은 AE·Lottie·CSS 대응이 정해져 있어야만 들어간다. 대응이 없는 항목은 Lottie·CSS 방출기에서 키로 굽고(bake) 그 사실을 경고로 남긴다.

| 영역 | 항목 | AE | Lottie | HTML |
|---|---|---|---|---|
| 변환 | anchor, pos(공간 베지어 경로), scale, rot, skew+축, rx/ry/rz + 원근 | Transform, 3D layer | `ks`, `ti/to` | transform, GSAP MotionPath |
| 외형 | 채움 색·그라디언트(≤8 stop, stop도 애니메이트 → 한 줄 안 여러 색 구역), 테두리 색·두께, 블러(가우시안·방향), 모서리 반경 | Fill/Stroke, Gradient Ramp, Fast Box Blur | `fl/gf/st`, 효과 | CSS gradient, filter |
| 마스크 | 사각·타원·패스 마스크(페더·확장 애니메이트), 트랙 매트(알파·루마). `reveal`은 사각 마스크 약칭 | Mask, Track Matte | `masksProperties`, `tt` | clip-path, mask |
| 셰이프 | rect, ellipse, path, trim paths(그리기 효과) | Shape Layer, Trim Paths | `sh/rc/el/tm` | SVG, stroke-dashoffset |
| 텍스트 애니메이터 | 단위(글리프·단어·줄), 범위 start/end/offset(애니메이트), 모양(사각·램프·삼각·둥근), 순서, 단위별 속성(불투명도·위치·스케일·회전·채움·자간·블러) | Text Animator + Range Selector | `t.a` | span별 tween |
| 계층 | `parent`, group(프리컴프·널) | Parent, Precomp | `parent`, `ty:0` | 중첩 div |
| 시간 | time remap(영상·그룹), loop, speed | Time Remap | `tm` | video currentTime |
| 수정자 | 닫힌 집합 `wiggle(freq, amp, seed)`, `loop(cycle·pingpong)`, `oscillate`, `spring_follow`. 정의식이 문서화돼 있어 모든 렌더러가 같은 값을 낸다 | Expression 또는 키 | 키로 굽기 | GSAP 또는 키 |
| 합성 | 블렌드(normal, multiply, screen, add, overlay) | Blend mode | `bm` | mix-blend-mode |

**4. 시간 관계를 구조로.** 시각은 절대 프레임(소수 허용) 또는 비트 기준(`"b2+3"`). 비트를 옮기면 묶인 항목이 같이 움직인다. 스태거는 복사된 지연값이 아니라 관계(`{"stagger": {"over": ["l1", "l2", "l3"], "step": 3, "order": "ltr"}}`)로 쓴다.

**5. 정밀도와 출처.** 값마다 `src`(measured, fitted, vlm, manual, default)와 `conf`. 지원하지 않는 효과(파티클, 비affine 왜곡)는 `"unsupported": "particles"`로 표시하고 픽셀 대체(영상 스프라이트)를 둔다. 비슷하게 꾸미지 않는다(원 스펙 §9).

**6. LLM 열람 수준.** 같은 JSON에서 결정적으로 세 가지 보기를 만든다: `semantic`(클립만, 잔차는 "+잔차 1.8px"로 요약), `keys`(키까지), `full`(샘플 참조 포함). 기본은 `semantic`. 숫자는 위치 0.1 px, 각도 0.1°, 색은 hex로 반올림해 보여 준다. 저장본은 반올림하지 않는다.

### 4.6 SaaS 프로모에 필요한 표현

조사(Envato SaaS 템플릿, Screen Studio식 줌, Lottie·AE 문서, AniMINT·V2S·SeeAction·Canis 등)와 레퍼런스 4개(`reference-clips.png`)를 대조한 결과다. 레퍼런스에는 이미 커서가 마이크를 클릭하는 장면, 검색창 타이핑, 토글, 노드 사이 연결선, 기울어진 폰·대시보드 목업, 블롭 변형, 로고 엔드카드가 있다. 지금 Score는 이것들을 레이어와 키로만 담고, **두 가지는 아예 담지 못한다**: 시간에 따라 바뀌는 문자열(타이핑, 단어 교체, 단어→아이콘), 여러 장면(ig2는 f58 컷이 있는데 한 장면으로 분석됨).

대부분은 클립처럼 §4.5 프리미티브로 펼쳐지는 약칭 구성이고 `err`와 잔차를 그대로 가진다. 새 렌더 프리미티브가 필요한 것은 넷뿐이다: 시퀀스, 텍스트 키, 배경 블러, 오디오.

| 우선 | 구성 | Score 스케치 | AE / Lottie / HTML | 측정 |
|---|---|---|---|---|
| 1 | **시퀀스 + 전환** (컷·푸시·와이프·줌스루·모프·매치컷) | `"sequence": [{"scene": "s2", "t": [58, 120], "in": {"kind": "push", "dir": "left", "dur": 8, "carrier": "s1.card"}}]` | 마스터 컴프 + 장면 프리컴프 / 프리컴프 in·out / 장면별 `data-start` | `shots.py` 컷 + 경계 주변 흐름장으로 전환 분류 |
| 2 | **시간에 따른 텍스트** (타이핑+캐럿, 단어 교체, 단어→아이콘) | `"text": {"keys": [[10, "Y"], [24, "You just"]]}, "typing": {"cps": 18, "caret": {"blink": 15}}, "swap": {"t": 39, "to": "icon_mic"}` | Source Text 홀드 키 / Lottie 텍스트 문서 키 / `textContent` | 프레임별 OCR 문자열 차분: 접두 증가 = 타이핑, 같은 박스 새 문자열 = 교체 |
| 3 | **커서 + 상호작용 이벤트** (클릭 리플, 버튼 눌림) | `{"type": "cursor", "style": "arrow", "events": [{"t": 36, "do": "click", "on": "cta", "ripple": {"r": 28}, "press": 0.96}]}` | 이미지 + 경로 + 타원 리플 / 굽기 / div + MotionPath | 커서 글리프 템플릿 매칭, 정지 + 리플·대상 변화 = 클릭 |
| 4 | **가상 카메라 포커스** (UI 영역으로 줌·팬, 고정 HUD) | `"camera": {"motion": [{"clip": "focus", "target": "pricing_card", "zoom": 1.8, "t": [60, 78]}]}, "hud": ["logo"]` | 널 부모 또는 3D 카메라 / 널 부모 / 래퍼 transform | 추적된 레이어 전체에 공통 닮음변환 적합 |
| 5 | **UI 위젯 상태** (토글·버튼·입력·드롭다운·토스트) | `{"type": "ui", "widget": "toggle", "states": [{"t": 40, "value": "on", "by": "cursor.click1"}]}` | 프리컴프 키 / 굽기 / CSS | VLM이 고정 목록에서 위젯 이름, CV가 상태 변화 프레임 |
| 6 | **디바이스·화면 컨테이너 + 스크롤** | `{"type": "device", "kind": "phone", "screen": {"content": "ui_shot", "scroll": [{"t": [30, 70], "dy": -640}]}}` | 화면 프리컴프 + 코너핀 / 프리컴프 / perspective + overflow | 기존 평면 추적으로 사각형, 사각형 안 위상상관으로 스크롤 |
| 7 | **스토리 구간 + 슬롯** (훅→문제→해결→기능→증거→CTA, 교체 가능한 로고·제품 UI, 9:16 안전 영역) | `"story": [{"part": "hook", "t": [0, 45]}]`, `"slots": {"logo": "logo"}` | 컴프 마커 / Lottie 마커·slots / data 속성 | OCR 문자열·역할·샷만 보는 LLM 패스, 숫자 없음 |
| 8 | **카운터**, 다음으로 **데이터 차트** | `"counter": {"from": 0, "to": 10000, "t": [30, 60], "ease": "out_expo", "fmt": {"sep": ",", "suffix": "+"}}` | 슬라이더 + Source Text / 프레임별 텍스트 키 / tween + textContent | 프레임별 OCR 숫자 파싱 + 이징 적합 |

그다음: 텍스트·요소에 붙는 주석과 연결선(`{"annot": "underline", "on": "title#w3"}`), 레이아웃 관계(아이콘이 텍스트 뒤에 붙음, 버튼이 라벨에 맞춤 → 텍스트 교체 시 재배치), 배경 블러 유리 카드(`backdrop_blur`), 블롭 필드 배경, 오디오·비트 연결. 파티클은 `unsupported` + 영상 스프라이트 유지, 대신 `"fx": "sparkles"` 라벨만 둔다.

**외형 요약 (시각 대조에서 발견).** 이미지 레이어는 Score에 에셋 경로뿐이라 "분홍 링"인지 텍스트만으로 알 수 없다. 측정 가능한 요약을 단다: `"look": {"colors": ["#f0466e", "#ffd166"], "shape": "ring", "aspect": 0.74}`(주색 클러스터, 모멘트·구멍 수로 모양 분류). VLM 캡션은 선택적 보충이다.

**측정 범위와 표현 범위는 다르다.** 포맷은 분석기가 아직 재지 못하는 것(마스크 패스, trim paths, wiggle)도 표현한다. 그래서 편집 에이전트와 내보내기가 먼저 쓸 수 있다. 각 항목의 측정기는 P2·P4·P5에서 붙인다. 스키마에는 실제로 쓰는 코드가 생길 때 넣는다(P0는 현 IR을 무손실로 싣는 부분 집합).

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
- **시각 대조 (사용자 지시, 2026-10-13):** 중간 표현을 점검할 때는 렌더 프레임을 직접 읽고, 눈으로 본 것(위치, 크기, 색, z 순서, 등장 순서, 글리프 진행)이 Score 값과 맞는지 항목별로 대조한다. 실제 클립은 원본 프레임과 Score 렌더를 나란히 본다. `keepframe score --scene … --sheet sheet.png`가 프레임 번호와 100 px 눈금을 넣은 6프레임 시트를 만든다. 수치 게이트가 통과해도 시트 대조를 생략하지 않는다(Stage B의 V1–V5는 게이트가 못 잡고 시트에서만 보였다).

## 8. 이행 단계

각 단계는 단독으로 병합 가능하고 테스트가 녹색이어야 한다.

| 단계 | 내용 | 완료 게이트 |
|---|---|---|
| P0 포맷 ✅ | `keepframe/ir/score.py`: Score 스키마(버전 검사, 모르는 필드 거부), `to_score`/`from_score`, 읽기용 직렬화, `keepframe score [--sheet]`, 골든 Score. `bake`는 클립이 생기는 P2로 | 합성 20장면 + 레퍼런스 4종 + 희귀 필드 장면이 scene→score→scene 모델 동일(입력이 같으니 렌더도 같음, 레퍼런스 장면은 픽셀 동일 확인), 시트 시각 대조 |
| P1 평가기 | numpy·HTML·AE·Lottie가 baked 모델에서 방출. 중복 로직 삭제. 이후 새 렌더 프리미티브(시퀀스, 텍스트 키, 배경 블러, 오디오)는 평가기 한 곳 + 방출기 매핑으로만 추가 | 백엔드 간 프레임 동등성 테스트, 기존 게이트 유지 |
| P2 모션 추상화 | 클립 적합 + 잔차, `bake`, 비트, 스타일 토큰, 역할, 외형 요약 `look`, 스토리·슬롯 (Stage C 흡수). §4.6의 시퀀스·텍스트 키·카운터 | 합성 왕복 클립 종류 정확도, 렌더 해시 불변 |
| P3 편집·검증 | 패치 연산, 1급 그룹, Score 술어 + 외형 검사, 수리 ≤ 3 | 프롬프트 평가 ≥ 현재, 편집 후 검사 유지 |
| P4 분석 구조 | 타입 가설, 합성에 의한 선택, 측정 함수 단일화, Crello 벤치. §4.6의 커서·카메라 포커스·위젯 상태·디바이스 측정기 | Crello 지표 기준선 확보, 실제 클립 게이트 ≥ Stage B |
| P5 텍스트 디렌더 | 텍스트 모듈 통합(text·titles·textstyle·reading·plane) | 타이틀 온전·색 ΔE·폰트 top-3 ≥ Stage B |

P0–P3는 분석기를 건드리지 않으므로 지금 결과를 그대로 새 포맷에 싣고 먼저 에이전트 쪽 이득을 본다. P4–P5가 분석 품질 작업이다.

## 9. 위험과 열린 질문

- **포맷 표기(§4):** JSON으로 결정(2026-10-13).
- **테스트 환경:** 이 클라우드 컨테이너에서는 Chromium 렌더가 필요한 기존 테스트(`test_cli.py::test_synth_compose_verify_cli` 등)가 변경 전 코드에서도 실패한다. 설치된 Playwright가 기대하는 브라우저 빌드와 `/opt/pw-browsers`의 빌드가 다르다. 브라우저 게이트는 `home`에서 돌린다.
- **프리셋 어휘 크기:** 너무 작으면 잔차 키가 늘고, 너무 크면 LLM 이해가 떨어진다. Crello·실제 클립에서 클립 커버리지(잔차 없는 구간 비율)로 정한다.
- **λ 보정:** 개발 분할에서 한 번, 기록과 함께.
- **네트워크:** 이 클라우드 환경은 huggingface.co, github.io, youtube.com, elements.envato.com 접속이 정책으로 막혀 있다. Crello 다운로드와 레퍼런스 영상 수집은 허용 도메인 추가 또는 `home` 머신에서.
- **GPU:** 합성에 의한 적합은 CPU에서 느릴 수 있다. 지금처럼 torch refine은 선택, 기본 경로는 CPU.
- **AE 라이브 확인 4·5**(그라디언트·영상·스타일 텍스트, 기울임 부호·회전 순서)는 여전히 사용자 단계.
