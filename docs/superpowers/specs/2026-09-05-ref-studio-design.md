# Ref Studio 설계 스펙 (v1)

- 날짜: 2026-09-05
- 상태: 사용자 승인 (2026-09-05). 에이전트 통솔 범위 A안 반영. 구현 계획은 M1+M2를 한 계획으로 작성. 관리자 패널 §13은 2026-09-06 추가(기존 v1에는 없었음).
- 근거 자료: `docs/research/2026-09-05-ref-studio-tech-research.md` (논문 전문 조사), `사업.txt`

## 1. 목표와 비목표

### 목표
1. 레퍼런스 영상(평면 2D 모션그래픽, UI 화면 녹화)을 **측정 기반 중간 표현(IR)** 으로 변환한다.
2. 사용자가 IR에서 "유지"와 "변경"을 지정하면, 에이전트가 프롬프트·첨부(이미지, URL, SVG, glTF)를 반영해 **유지 조건을 형식 검증하면서** 새 장면을 만든다.
3. 결과를 결정적으로 렌더(MP4)하고, 다시 편집할 수 있는 프로젝트(IR + HTML 컴포지션)로 저장한다.

### v1 결정 사항 (2026-09-05 확정)
| 항목 | 결정 |
| --- | --- |
| 레퍼런스 범위 | 평면 2D 모션그래픽 + UI 화면 녹화. 실사 푸티지는 비지원(명시적으로 거부) |
| 구간 | "구간 지정"(기본) 과 "전체 영상"(샷 분할 후 장면별 분석) 두 모드 |
| IR·렌더 | 자체 JSON IR + HTML/GSAP 컴포지션(HyperFrames 호환 속성). Adobe MCP(After Effects·Premiere) 익스포트는 M6, Lottie는 그 뒤 |
| 분석 지연 | 장면당 수 분, GPU 사용 허용. 실행 전 예상 시간·비용 표시 |
| 3D | 2D 장면 속 3D 에셋(이미지→3D→glTF→Three.js). 카메라 연출 복원은 비목표 |
| 코드베이스 | greenfield |

### 비목표
- 실사 영상 레이어 분해, 비affine 변형(글자 왜곡 등), 물리 시뮬레이션, 원본 제작 파일 복원.
- 완전 자동. 보정이 필요한 부분은 숨기지 않고 사용자에게 넘긴다.

## 2. 용어
- **장면(scene)**: 샷 경계로 나뉜 연속 구간. 분석 단위.
- **요소(element)**: 장면 안에서 독립적으로 움직이는 것. kind ∈ {text, sprite, ui, 3d, group}.
- **L0/L1/L2**: 측정층 / 키프레임층 / 의미·검증층. §4 참조.
- **keep 술어**: 재생성 후에도 참이어야 하는 L2 술어. 사용자가 "유지"로 지정한 것.
- **motion bundle**: 요소의 L1 트랙 묶음을 외형과 분리해 다른 요소에 적용할 수 있는 단위.

## 3. 아키텍처

```
[Ingest] → [Analyzer worker (GPU)] → [IR store] → [Review UI] → [Edit agent] → [Composer] → [Renderer] → [Verifier] → [Export]
                                         ↑_____________________________ repair loop (≤4) ______________________|
```

| 구성 요소 | 책임 | 의존 |
| --- | --- | --- |
| Ingest | 업로드, 구간/전체 모드, 샷 분할(전체 모드), 비용 예측 | ffmpeg, 샷 경계 검출 |
| Analyzer worker | §5 파이프라인 실행, L0·L1·L2 생성, 신뢰도 리포트 | GPU, OCR·SAM2·CoTracker3·sprite 최적화 |
| IR store | project/scene JSON + 텍스처·glTF 블롭, 버전 이력, diff | 파일 시스템(로컬) / 오브젝트 스토리지(클라우드) |
| Review UI | 분석 결과 표시, 보정 4연산, keep/변경 지정 | IR store |
| Session agent | §7.1. 사용자와 대화하며 아래 구성 요소를 **도구로 호출**. 측정·렌더·검증을 직접 수행하지 않음 | LLM, 도구 레지스트리 |
| Edit agent | §7. 의도 해석 → 계획 → 에셋 → 컴포즈 | LLM/VLM, 에셋 모델 |
| Composer | IR → HTML+GSAP 컴포지션 (타입 API만 사용) | 없음(순수 변환) |
| Renderer | 헤드리스 Chrome 프레임 시킹 + FFmpeg, 프레임 해시 | Chromium, ffmpeg |
| Verifier | 스키마·keep 술어·레이어별 프레임 비교·외형/시간 유사도 | Renderer, CoTracker3, DreamSim |
| Export | MP4, 프로젝트 zip, (M6) Adobe MCP, (M6+) Lottie | Composer |

배포: 로컬판은 전부 한 프로세스(CLI + 로컬 웹 UI). 클라우드판은 Analyzer worker와 Renderer만 큐 뒤로 분리한다. 코드 경로는 동일하고 실행기만 다르다.

## 4. IR 스키마

파일: `project.json` + `scenes/<id>/scene.json` + `assets/`(텍스처 PNG, glTF, 원시 배열 npz).

```json
{
  "schema": "refstudio.project/1",
  "source": {"file": "ref.mp4", "fps": 30, "size": [1920, 1080], "mode": "full|range", "range": [0, 120]},
  "scenes": [{"id": "s1", "frames": [0, 119], "transition_out": {"type": "cut|fade|slide|unknown", "frames": 6}}],
  "links": [{"from": "s1/e3", "to": "s2/e1", "reason": "same logo"}],
  "versions": [{"id": "v1", "parent": null, "note": "initial analysis", "auto": true}]
}
```

```json
{
  "schema": "refstudio.scene/1",
  "background": {"kind": "color|image", "value": "#101418", "confidence": 0.98},
  "elements": [{
    "id": "e3", "kind": "text", "role": "primary|secondary|text|background",
    "canonical": {"text": "Launch faster", "font": {"family_guess": "Inter", "weight": 700, "size_px": 96}, "texture": "assets/e3.png"},
    "raw": "assets/e3_tracks.npz",
    "tracks": {
      "x": [{"t": 0, "v": -400, "ease": [0.2, 0.0, 0.0, 1.0]}, {"t": 18, "v": 220}],
      "opacity": [{"t": 0, "v": 0}, {"t": 12, "v": 1}]
    },
    "z": [{"t": 0, "v": 3}],
    "fit_error": {"max_px": 1.4, "max_frames": 0},
    "confidence": 0.91, "provenance": "auto|manual"
  }],
  "groups": [{"id": "g1", "members": ["e3", "e4"], "reason": "word group"}],
  "constraints": [
    {"pred": "type(m_e3,'translation')", "keep": true},
    {"pred": "dir(m_e3,[1,0])", "keep": true},
    {"pred": "before(m_e3,m_e4)", "keep": true},
    {"pred": "dur(m_e3,18)", "keep": false}
  ],
  "ui": {"components": [{"id": "c1", "type": "card", "bbox": [..], "children": [..], "states": ["idle", "hover"]}]}
}
```

불변 조건
1. `raw`(프레임별 affine·opacity·z, 텍스트 박스)는 삭제하지 않는다. L1은 L0에서 파생되고 `fit_error`로 오차 상한을 기록한다.
2. `tracks`의 키프레임은 cubic-bezier 이징으로 표현한다. 표현 불가면 키프레임을 더 쪼갠다(오차 상한 2px 또는 1프레임).
3. `constraints`는 Analyzer가 자동 추출하고, 사용자가 keep을 켜고 끈다. keep=true 술어는 Verifier의 필수 검사 항목이다.
4. 버전은 append-only. 편집은 새 버전을 만들고 parent를 가리킨다.
5. kind=ui 요소는 캡처 픽셀을 `canonical.texture`에 보존하되, 출력 컴포지션에서는 `ui.components`로 재생성한 것만 쓴다.

## 5. 분석 파이프라인 (Analyzer)

| 단계 | 방법 | 출력 | 실패 신호 |
| --- | --- | --- | --- |
| 5.1 구간·샷 분할 | 전체 모드: 히스토그램+SSIM 기반 샷 경계, 사용자 조정 가능 | scenes[] | 장면 길이 < 6프레임 → 병합 제안 |
| 5.2 배경 | LAB 최빈색 클러스터. 사용자 배경 이미지 지정 가능 | background | 최빈색 비율 < 30% → 배경 불확실 표시 |
| 5.3 텍스트 | 이미지 텍스트 스포터(고정) + 경량 추적. 사용자가 문구 시퀀스를 주면 edit-distance DTW로 정합. Hi-SAM 스트로크 마스크로 텍스트 sprite 생성. 폰트 추정은 후보 3개 | text 요소 (canonical.text, box/frame) | 인식 신뢰도 < 0.7 → 검수 강조 |
| 5.4 영역 추출 | 평면 MG: Canny + trapped-ball. 텍스처: 사용자 bbox 1프레임 → SAM2 전파 | 후보 영역/프레임 | 영역 수 급변 → 분할 실패 표시 |
| 5.5 sprite 최적화 | canonical RGBA 텍스처 + 프레임별 affine·opacity·z. differentiable compositing으로 프레임 간 매핑(1:1/split/merge/appear/disappear) 후 텍스처 prior 최적화. GPU, 장면당 수 분 | sprite 요소 raw | 재구성 L1 > 0.02 → 경고 |
| 5.6 키프레임 축약 | 동적 계획법으로 키프레임 선택, 구간별 cubic-bezier 이징 피팅, 오차 상한 2px/1프레임 | tracks, fit_error | 키프레임 수 > 프레임 수/3 → "복잡 모션" 표시 |
| 5.7 UI 파싱 | 요소 검출(아이콘·텍스트·박스) + 상태 변화 프레임 검출 + 컴포넌트 스키마(nav/card/button/list/table/chart) | ui.components, states | 컴포넌트 미분류 → generic box |
| 5.8 의미 | VLM: 요소 캡션, 역할, 그룹 제안. 타이밍 판단에는 쓰지 않는다 | role, groups | 없음(제안만) |
| 5.9 술어 추출 | tracks에서 type/dir/mag/dur, 요소 쌍의 before/while/after, 공간 관계 | constraints | 없음 |
| 5.10 리포트 | 재구성 오차, 요소별 신뢰도, 예상 보정 지점 | report.json | — |

VLM은 5.8에만 쓴다. 타이밍·위치는 전부 측정값이다.

## 6. 검수·보정 (Review UI)
보정 연산은 네 가지뿐이다. 각 연산은 IR을 새 버전으로 만들고 영향 범위만 재계산한다.
1. 객체 ID 재할당(프레임 구간 지정)
2. 영역 경계 지정(한 프레임에 마스크 그리기 → 5.4부터 재실행)
3. bbox 프롬프트 추가/수정(→ SAM2 전파 재실행)
4. 텍스트 내용·폰트 수정

keep/변경 지정: 요소 단위(외형/모션 각각), 술어 단위 토글. "문구·이미지만 바꾸고 나머지 유지"는 프리셋 하나로 제공한다.

검수 화면(2026-09-06 정정): 로컬 사이트 네 장 — 라이브러리, 새 프로젝트(인제스트), 분석 진행, 검수. 서버는 표준 라이브러리 `http.server` 한 프로세스. 검수 첫 화면은 원본·재구성 나란히 재생 + 타임라인이며, 타임라인 아래에 프레임별 재구성 오차 띠를 그린다. 요소마다 자동/수동 배지와 신뢰도를 항상 표시하고 미지원 항목을 숨기지 않는다. 한국어 기본, 영어 토글. 노트북(1280px)에서 편해야 하고 키보드로 스크럽·오차 구간 이동이 가능해야 한다. 마크업 원본은 Stitch(`.stitch/designs/`). 시각 규칙은 저장소 루트의 `DESIGN.md`가 갖는다. 관리자 화면은 §13, 로컬판에는 없다.

## 7. 에이전트

### 7.1 세션 에이전트 (통솔 범위 결정: A안, 2026-09-05)
사용자와의 대화 창구는 세션 에이전트 하나다. 세션 에이전트는 아래 도구만 호출할 수 있고, 각 도구 내부는 결정적이다.

| 도구 | 하는 일 | 결정적 여부 |
| --- | --- | --- |
| `analyze(project, mode, range)` | §5 파이프라인 실행 | 결정적 (같은 입력·파라미터 → 같은 IR) |
| `correct(scene, op, args)` | §6 보정 4연산 중 하나 | 결정적 |
| `set_keep(scene, targets, on)` | keep 술어·요소 토글 | 결정적 |
| `edit(scene, prompt, attachments)` | §7.2 편집 에이전트 실행 | 비결정적 (LLM), 검증으로 게이트 |
| `render(version)` | §8 렌더, 프레임 해시 | 결정적 |
| `verify(version)` | §7.2 Verifier | 결정적 |
| `export(version, target)` | MP4·프로젝트·(M6) Adobe MCP | 결정적 |
| `report(version)` | 신뢰도·오차·보정 지점 요약 | 결정적 |

정책은 코드로 강제한다. 세션 에이전트는 다음을 스스로 바꿀 수 없다: 재시도 상한(§9), 비용 확인 게이트(전체 영상 모드), keep 술어 검사 생략, 미지원 효과의 "비슷하게 처리". 세션 에이전트가 하는 일은 사용자 지시를 도구 호출 순서로 바꾸고, 결과를 설명하고, 충돌 선택지를 전달하는 것뿐이다. 분석 파라미터 변경은 사용자가 명시적으로 요청한 경우에만 인자로 넘긴다.

### 7.2 편집 에이전트

```
prompt + attachments + IR(v_n) + keep set
  → Interpreter  : 편집 의도 JSON {targets:[{element, property, value|asset_request}], keep:[pred...]} + 해석 확인 문장
  → Planner      : 변경 대상별 작업 목록, 에셋 요청, 충돌 후보(예: 문구 길이 초과)
  → Asset agents : text | vector(SVG) | raster(RGBA) | 3d(glTF) | ui(components→HTML)  (각 도구 1개)
  → Composer     : L1 트랙 복사, 변경 대상만 수정, 타입 API로 HTML+GSAP 출력
  → Renderer     : MP4 + 프레임 시퀀스
  → Verifier     : (1) 스키마 (2) keep 술어 전 프레임 (3) 레이어별 프레임 비교 (4) 외형·시간 유사도
  → 실패 시 리포트를 Composer에 되먹임 (최대 4회, 실패 프레임 이미지 포함)
  → 충돌은 선택지로 사용자에게 (자동 처리 금지)
```

- Interpreter는 실행 전에 해석을 한 문장으로 보여주고 확인을 받는다.
- Composer가 쓰는 애니메이션 API는 IR tracks와 1:1인 소수의 함수(set, tween, sequence, group)로 제한한다. 자유 JS 생성 금지.
- Asset agents 기본 모델: vector=OmniSVG, raster=LayerDiffuse, 3d=이미지→3D(Hunyuan3D 또는 TRELLIS), ui=OmniParser 스키마→브랜드 토큰 HTML. 모델은 어댑터 뒤에 두어 교체 가능.
- 3D 에셋은 glTF로 저장하고 Composer가 Three.js 레이어로 배치한다. 회전·스케일·위치는 일반 요소와 같은 tracks를 쓴다.
- 충돌 규칙 예: 문구가 원래 상자를 넘치면 {글자 축소, 줄바꿈, 상자 확대} 중 선택. 선택 전에는 실패로 표시한다.

## 8. 렌더·익스포트
- 컴포지션: 요소 = `<div data-start data-duration data-track-index>`(HyperFrames 호환), 애니메이션 = GSAP 타임라인, 3D = Three.js 캔버스 레이어, 텍스트 = 실제 `<span>`(캡처 이미지 아님).
- 결정성: 프레임 시킹 렌더. 동일 IR → 동일 프레임 해시. 해시는 회귀 테스트와 재렌더 생략 판단에 쓴다.
- 출력: MP4(H.264), 프로젝트 zip(IR + assets + composition).
- M6: Adobe MCP로 After Effects 컴포지션 생성(요소→레이어, tracks→키프레임, bezier 이징→AE easing), Premiere는 렌더 MP4 + 마커. Lottie는 그 뒤.

## 9. 실패 정책·비용
- 미지원 효과(비affine 변형, 파티클, 실사)는 "미지원"으로 표시하고 원본 픽셀 참조만 남긴다. 비슷하게 꾸미지 않는다.
- 신뢰도 < 0.7 요소는 검수 UI에서 강조. keep 술어 실패는 렌더 결과에 프레임 번호와 함께 표시.
- 재시도 상한: Verifier 되먹임 4회, 에셋 생성 2회. 초과 시 실패로 보고.
- 전체 영상 모드는 장면 수·예상 분석 시간·예상 비용을 실행 전에 보여주고 확인을 받는다.

## 10. 평가·테스트
- 시험 세트: 타이포그래피 10, 제품 카드 10, UI 모션 10, 전체 영상 5. 사용 권리 확인된 것만.
- 지표와 M 게이트 임계값

| 지표 | 계산 | M2 게이트 | M3 게이트 |
| --- | --- | --- | --- |
| 재구성 L1 | 원본 vs IR 렌더 프레임 평균 L1 | ≤ 0.02 (30개 중 24개 이상) | — |
| 트래킹 오류 | 수동 정답 대비 잘못된 매핑 수 | 클립당 ≤ 5 | — |
| 외형/시간 유사도 | DreamSim+DTW / CoTracker3 tracklet 상관 | 시간 ≥ 0.7 | 시간 ≥ 0.7 유지 |
| keep 통과율 | keep 술어 중 통과 비율 | — | ≥ 0.95 |
| 수동 보정 수 | 채택까지 보정 연산 횟수 | 중앙값 ≤ 3 | 중앙값 ≤ 3 |
| 채택 시간 | 업로드→승인 | 기록만 | 기록만 |

- 단위 테스트: 모듈별 입력/출력 픽스처. golden IR(합성 장면: Crello Animation 샘플에서 정답 sprite·affine 보유) 대비 sprite 오차 회귀.
- 렌더 결정성: 같은 IR 두 번 렌더 → 프레임 해시 동일.
- Verifier 테스트: 알려진 위반(방향 반전, 순서 뒤바뀜, 위치 오프셋)을 주입해 검출되는지 확인.

## 11. 마일스톤과 완료 기준
| M | 내용 | 완료 기준 |
| --- | --- | --- |
| M1 | IR 스키마, Composer, Renderer, Verifier(스키마·술어·프레임 비교) | 합성 장면 20개: IR→렌더→IR 술어 재추출 일치, 해시 결정성 |
| M2 | Analyzer(평면 2D MG, 구간 모드), Review UI 보정 4연산 | §10 M2 게이트 |
| M3 | Edit agent(문구·이미지·색 교체) + 되먹임 루프 + 충돌 선택지 | §10 M3 게이트, 대표 데모 흐름 완성 |
| M4 | 전체 영상 모드(샷 분할, 전환, 장면 간 링크) + UI 녹화 경로(5.7, ui asset agent) | 전체 영상 5개 분할 정확도 ≥ 0.9, UI 10개 keep 통과율 ≥ 0.9 |
| M5 | 3D 에셋 에이전트 + Three.js 레이어 | 3D 에셋 포함 장면 5개 렌더·검증 통과 |
| M6 | Adobe MCP 익스포트(AE·Premiere), 이후 Lottie | AE에서 열어 키프레임 일치 확인 |

## 12. 보류한 결정(기본값 명시)
- 폰트 매칭 정확도: v1은 후보 3개 제시, 사용자가 선택. 자동 확정 안 함.
- 에셋 모델 호스팅: v1 로컬 GPU 또는 외부 API 둘 다 어댑터로. 기본은 외부 API.
- 학습형 분석 모델(video→keyframes): 교정된 IR이 300건 이상 쌓이면 검토.
- 실사 푸티지: 비지원. 요청 시 Generative Omnimatte 계열 별도 경로 검토.

## 13. 관리자 패널 (클라우드·팀, 2026-09-06 추가)

로컬판에는 관리자 화면이 없다. 관리형 클라우드와 팀용 서비스만 이 화면을 가진다. 제작 도구(업로드·검수·편집·출력)와 운영 도구는 같은 DESIGN.md를 쓰되, 운영 화면은 큐·테넌트·권한만 다루고 장면을 편집하지 않는다.

### 역할
| 역할 | 할 수 있는 일 | 못 하는 일 |
| --- | --- | --- |
| 운영자(admin) | 테넌트·멤버·할당량·작업 큐·감사 로그 | keep 술어 생략, 재시도 상한 상향, 실사 경로 켜기 |
| 빌링 | 사용량(분석 분, 렌더 분, 저장) 조회, 영수증 | 가격 확정(미정), 실패 재처리 한도 변경 |
| 검수 리드 | 테넌트 안 프로젝트 열람, 공동 승인 | 다른 테넌트 자산 |

정책은 §7.1·§9와 같다. 관리자 UI는 코드로 고정된 한도를 **표시**만 한다. 재시도 4회, 에셋 생성 2회, keep 검사 생략 금지는 화면에서 바꿀 수 없다.

### 화면
1. **테넌트 목록** — 조직, 멤버 수, 이번 주기 분석·렌더 사용량, 상태(정상 / 할당량 초과 / 정지).
2. **작업 큐** — Analyzer·Renderer 대기열. 작업 id, 테넌트, 종류(analyze/render/correct), 장면, 대기·실행·실패, GPU 점유. 실패는 재처리 횟수와 남은 한도를 함께 보여 준다.
3. **검역(quarantine)** — 실사·미지원으로 거부된 업로드. 원본은 열지 않고 거부 이유와 시각만 남긴다.
4. **감사 로그** — 누가 keep을 바꿨는지, 누가 내보냈는지, 누가 멤버를 넣었는지. append-only.
5. **로그인** — 관리자 전용 입구. 제작 도구 로그인과 경로를 분리한다.

### 비목표
- 관리자가 장면 IR을 직접 고치는 편집기.
- 가격표·프로모션 관리(가격 미정).
- 생성 모델 교체 콘솔. 모델은 어댑터 뒤이며 운영 화면에서 핫스왑하지 않는다.

## 14. 참고
근거 논문·수치는 `docs/research/2026-09-05-ref-studio-tech-research.md` §1·§2·§5 참조. 핵심: Motico(TOG 2023), Sprite Decomposition(ECCV 2024), MoVer(SIGGRAPH 2025), LogoMotion(CHI 2025), Animation2Code(2026), VimTS/GoMatching++(2025–26), CodeGen-3D(2026), Interaction2Code(ASE 2025).
