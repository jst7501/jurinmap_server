---
name: council
description: 4명의 비평가(design-evaluator + critic-newbie/creative/analyst)를 병렬로 소환해 UI를 평가하고, design-council-synthesizer가 합의·충돌·단독 주장을 정리한 종합 리포트를 받음. 단일 평가자로 잡히지 않는 고수준 이슈(주린이 이해도·창의성·의사결정 가치)를 다층 관점으로 확인.
argument-hint: <widget-path | route-url>
---

**대상:** $ARGUMENTS

아래 4명의 비평가를 **병렬로 소환**하고 그 결과를 종합자에게 넘겨 토론 기록 형식의 리포트를 받아라.

## 절차

### 0. 사전 체크
- `curl -s http://localhost:5173 -o /dev/null -w "%{http_code}"`로 dev 서버 확인
- 200 아니면 사용자에게 `cd dashboard && npm run dev` 요청 후 중단 (메인이 띄우는 게 정책)

### 1. Round 1: 4명 병렬 평가

**한 메시지 안에 4개 Agent 호출** (병렬 필수 — 순차는 시간 낭비). 각 호출에 다음 포함:

- 대상 경로/URL: `$ARGUMENTS`
- 최근 diff 요약 (있으면 `git diff HEAD~1 -- <대상파일>` 짧게)
- 리포트를 **반환 텍스트로 전달**하고, 선택적으로 `scratch/eval/<critic>_<slug>.md`에 Write

호출할 비평가:
1. `design-evaluator` — 4축 크래프트 점수
2. `design-critic-newbie` — 주린이 이해도·행동 유도
3. `design-critic-creative` — 기시감·대안 제안
4. `design-critic-analyst` — 의사결정 가치·한국 시장 컨텍스트

### 2. Round 2: 종합

4개 리포트 수집 후 `design-council-synthesizer`를 단일 호출. 프롬프트에 4개 리포트 본문을 구획 구분해서 전달:

```
<evaluator-report>
{design-evaluator 리포트 전문}
</evaluator-report>

<newbie-report>
{design-critic-newbie 리포트 전문}
</newbie-report>

<creative-report>
{design-critic-creative 리포트 전문}
</creative-report>

<analyst-report>
{design-critic-analyst 리포트 전문}
</analyst-report>
```

+ 대상 경로·목적 설명.

### 3. 사용자 보고

synthesizer 리포트를 **그대로** 사용자에게 전달. 추가로:
- 합의 이슈가 있다면 "바로 수정 착수할까요?" 제안
- 충돌 포인트가 있다면 사용자 결정 요청 (양측 주장 요약해 질문)
- synthesizer가 "Planner 호출 필요"로 판정하면 `/plan <대상> -- <재설계 목적>` 제안

## 주의

- 메인(너)은 **평가에 개입 금지**. Generator 역할로만 남아라.
- 비평가 리포트를 임의로 해석·요약·재작성 금지. synthesizer가 그 역할.
- Round 1은 **반드시 병렬** — 한 메시지에 4개 Agent 호출을 담아라.
- 점수가 FAIL로 나왔다고 합리화·재평가 요청 금지. FAIL은 FAIL로 보고.
- synthesizer도 Claude다. synthesizer의 권고가 명백히 편향되어 보이면 사용자에게 flag.

## 언제 /council vs /evaluate 쓰나?

- `/evaluate` (단일 평가자) — 크래프트 수정 후 회귀 체크, 빠른 확인, 2~5회 루프
- `/council` (4명 카운슬) — 뷰 전체 재검토, 방향성 의심될 때, 주린이/창의성/실무성 중 어느 축이 약한지 확인 필요할 때. 비싸므로 큰 마일스톤에만.

`/council` 실행은 4배 토큰·시간이 드니, 일반 루프는 `/evaluate`로 유지하고 카운슬은 선택적으로 써라.
