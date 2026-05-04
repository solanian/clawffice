# clawffice Feature Backlog

나중에 생각나는 기능 아이디어를 쌓아두고, 작업하기 좋은 시점에 하나씩 꺼내 구현하기 위한 목록입니다.

## 사용 규칙

- 새 아이디어는 `Ideas`에 짧게 추가한다.
- 구현하기로 정하면 `Planned`로 옮기고 필요한 범위를 한두 줄로 적는다.
- 작업을 시작하면 `In Progress`, 끝나면 `Done`으로 옮긴다.
- 당장 애매한 아이디어도 버리지 말고 적어둔다. 나중에 더 좋은 형태로 다듬으면 된다.
- 사용자에게 백로그를 보여줄 때는 번호가 있는 리스트로 보여줘서 번호로 소통할 수 있게 한다.

## Ideas

- agent 상태 히스토리: 최근 상태 변화와 작업 시간을 간단한 timeline으로 표시.
- 알림 설정: unread, error, 장시간 작업 중 상태에 대한 브라우저/소리/무음 옵션.
- 운영 점검 패널: backend health, OpenClaw 연결 상태, agent-push 상태를 관리자용으로 표시.
- 읽음 처리 타이밍 개선: OpenClaw가 text 생성을 시작하는 시점에 해당 메시지를 안 읽음에서 읽음으로 전환.
- chat UI 음성 대화: ASR/TTS를 붙여 음성 입력과 음성 응답 재생으로 대화 가능하게 하기.
- chat UI OpenClaw command 실행: 대화 화면에서 OpenClaw command들을 직접 실행할 수 있는 command palette/실행 UI 추가.
- office tab unread count: office tab에 안 읽은 메시지 수를 알림 아이콘/배지로 표시.
- office tab 음성 대화 진입: office tab에서 바로 clawffice manager 역할 agent와 음성 대화 모드로 진입 가능하게 하기.
- office UI 확장성 재설계: agent 수가 늘어나도 배치, 탐색, 알림, 상호작용이 유지되도록 공간/레이어/목록 구조 개선.
- 픽셀 폰트 한국어 적용: 한국어 UI에도 픽셀 폰트가 자연스럽게 적용되도록 폰트/fallback/가독성 조정.
- 프로필 이미지 생성 기능: agent 프로필 이미지를 UI에서 생성하거나 변형해 선택할 수 있게 하기.
- agent 간 통신 기능 설계: agent들이 서로 메시지를 주고받는 방식, 권한, 로그, UI 노출 구조 설계.
- chat history pagination: agent와 대화할 때 전체 대화를 한 번에 불러오지 않고 처음에는 최근 일부만 로드한 뒤, 위로 끝까지 스크롤하면 이전 메시지를 추가로 불러오게 하기.
- queue 순서 변경: queue 대기열 UI에 위/아래 화살표 버튼을 추가해 대기 메시지 순서를 바꿀 수 있게 하기.
- skill 관리: skill 목록을 관리하고 agent별로 skill을 추가/제거할 수 있게 하기.
- agent 공통 행동 원칙: 모든 agent에 공통으로 적용할 행동 원칙을 정의하고 관리할 수 있게 하기.
- 백그라운드 queue 주입: 대화 화면이 열려 있지 않거나 백그라운드 상태여도 queue에 메시지를 주입하고 처리할 수 있게 하기.
- office tab agent 정보 모달 클릭 우선순위: office tab에서 agent 정보 모달을 띄웠을 때 모달 click event가 office UI 제어보다 우선 처리되도록 수정하기.

## Planned

-

## In Progress

- chat UI 분할 화면: chat UI가 보일 때 전체 화면을 덮지 않고 메인 화면과 위아래 절반씩 나눠 쓰며, 아래 절반을 chat UI가 사용하도록 변경.

## Done

-
