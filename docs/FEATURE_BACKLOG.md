# clawffice Feature Backlog

나중에 생각나는 기능 아이디어를 쌓아두고, 작업하기 좋은 시점에 하나씩 꺼내 구현하기 위한 목록입니다.

## 사용 규칙

- 새 아이디어는 `Ideas`에 짧게 추가한다.
- 구현하기로 정하면 `Planned`로 옮기고 필요한 범위를 한두 줄로 적는다.
- 작업을 시작하면 `In Progress`, 끝나면 `Done`으로 옮긴다.
- 당장 애매한 아이디어도 버리지 말고 적어둔다. 나중에 더 좋은 형태로 다듬으면 된다.
- 사용자에게 백로그를 보여줄 때는 번호가 있는 리스트로 보여줘서 번호로 소통할 수 있게 한다.

## Ideas

- 읽지 않은 메시지 배지 UX 개선: 느낌표/노란 점 외에 마지막 메시지 미리보기나 unread count 표시.
- 전송 실패 표시와 재전송: 안읽음/읽음 외에 전송 실패 상태를 표시하고 실패한 메시지를 재전송할 수 있게 하기.
- agent별 대화 목록 화면: 현재 연결된 agent와 최근 대화 상태를 한 화면에서 빠르게 확인.
- 기능 아이디어를 UI에서 직접 추가/완료 처리하는 작은 backlog 패널.
- agent 상태 히스토리: 최근 상태 변화와 작업 시간을 간단한 timeline으로 표시.
- 알림 설정: unread, error, 장시간 작업 중 상태에 대한 브라우저/소리/무음 옵션.
- 모바일 레이아웃 정리: 작은 화면에서 agents 탭, 대화, 오피스 전환을 더 빠르게 접근.
- 운영 점검 패널: backend health, OpenClaw 연결 상태, agent-push 상태를 관리자용으로 표시.
- chat message queue: 대화 전송 중에도 추가 메시지를 입력하면 queue에 넣고, queue 목록 확인/메시지 수정/보내기 취소를 지원.
- 읽음 처리 타이밍 개선: OpenClaw가 text 생성을 시작하는 시점에 해당 메시지를 안 읽음에서 읽음으로 전환.
- chat UI 음성 대화: ASR/TTS를 붙여 음성 입력과 음성 응답 재생으로 대화 가능하게 하기.
- chat UI OpenClaw command 실행: 대화 화면에서 OpenClaw command들을 직접 실행할 수 있는 command palette/실행 UI 추가.
- office tab unread count: office tab에 안 읽은 메시지 수를 알림 아이콘/배지로 표시.
- office tab 음성 대화 진입: office tab에서 바로 clawffice manager 역할 agent와 음성 대화 모드로 진입 가능하게 하기.
- office UI 확장성 재설계: agent 수가 늘어나도 배치, 탐색, 알림, 상호작용이 유지되도록 공간/레이어/목록 구조 개선.
- 픽셀 폰트 한국어 적용: 한국어 UI에도 픽셀 폰트가 자연스럽게 적용되도록 폰트/fallback/가독성 조정.
- 프로필 이미지 생성 기능: agent 프로필 이미지를 UI에서 생성하거나 변형해 선택할 수 있게 하기.
- agent 간 통신 기능 설계: agent들이 서로 메시지를 주고받는 방식, 권한, 로그, UI 노출 구조 설계.

## Planned

-

## In Progress

-

## Done

- chat UI 전송 UX 개선: 대화 전송 시 전체 대화 UI를 새로고침하지 않고 기존 흐름에 연속적으로 append/stream 표시.
