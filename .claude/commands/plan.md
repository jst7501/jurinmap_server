---
name: plan
description: design-planner 서브에이전트로 UI 재설계 스펙을 작성. 인자로 대상 뷰 경로/라우트와 목적을 받음.
argument-hint: <target-view-or-route> -- <purpose>
---

아래 대상을 `design-planner` 서브에이전트로 재설계 스펙 작성해줘.

**대상 & 목적:** $ARGUMENTS

절차:
1. `design-planner` subagent를 Task tool로 호출
2. 프롬프트에 대상 파일 경로/라우트 + 사용자가 준 목적(한 문장) + 현재 문제의식을 포함
3. Planner가 `scratch/plans/<YYYYMMDD_HHMM>_<topic>.md`에 스펙 저장
4. 저장 경로와 스펙 요지(목적·섹션 구조·작업 큐 항목 수)를 사용자에게 보고
5. 사용자가 스펙 승인하면 작업 큐 1번부터 Generator(나)가 구현 시작
6. 각 작업 단위 완료 후 `/evaluate`로 검증, 통과 시 다음 단위

**주의:**
- Planner는 코드를 짜지 않고 스펙만 만듦. 바로 구현하지 말 것.
- 스펙이 부실하면 (섹션 수 2개 이하, 제거 리스트 없음, Generator 작업 큐 3개 미만) 재작성 요청.
- 사용자가 "바로 구현 가" 하지 않는 한 작업 큐 실행은 대기.
