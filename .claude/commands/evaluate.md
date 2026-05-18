---
name: evaluate
description: UI 위젯/뷰를 design-evaluator 서브에이전트로 평가. 인자로 위젯 파일 경로나 라우트 URL을 받음.
argument-hint: <widget-path | route-url>
---

아래 대상을 `design-evaluator` 서브에이전트로 평가해줘.

**대상:** $ARGUMENTS

절차:
1. dev 서버가 이미 떠있는지 `curl -s http://localhost:5173 -o /dev/null -w "%{http_code}"`로 확인. 200이 아니면 사용자에게 `cd dashboard && npm run dev` 실행 요청 후 중단.
2. `design-evaluator` subagent를 Task tool로 실행. 프롬프트에 대상 경로/URL, 최근 diff 요약, 관심 포인트를 포함.
3. 서브에이전트 리포트를 그대로 사용자에게 전달하고, 실패 축이 있으면 다음 루프로 어느 축을 먼저 고칠지 내 판단을 덧붙여 제안.

**주의:**
- 메인(너)은 Generator 역할만. 평가 점수를 바꾸거나 합리화하지 말 것.
- 평가자가 FAIL 줬으면 FAIL로 사용자에게 보고. 동의 구하지 말고 다음 수정 제안.
