# Clawffice🦞

🌐 Language: [中文](./README_cn.md) | [English](./README.md) | **한국어** | [日本語](./README.ja.md)

![Clawffice🦞 Cover](docs/screenshots/readme-cover-2.jpg)

**픽셀 아트 스타일의 AI 오피스 대시보드**입니다. AI agent가 지금 어떤 상태인지, 누가 무엇을 하고 있는지, 어제 어떤 작업을 했는지 한눈에 볼 수 있습니다.

멀티 agent 협업, 한국어/영어/중국어/일본어 UI, AI 배경 생성, 데스크톱 펫 모드를 지원합니다. [OpenClaw](https://github.com/openclaw/openclaw)와 함께 쓰면 가장 자연스럽지만, 독립적인 상태 대시보드로도 사용할 수 있습니다.

> Clawffice는 기존 Star Office UI 프로젝트를 참고해 만든 독립 프로젝트입니다.

---

## 빠른 시작

### 1. Docker로 실행

```bash
git clone https://github.com/solanian/clawffice.git
cd clawffice
cp .env.example .env
docker compose up --build
```

브라우저에서 `http://127.0.0.1:19000`을 엽니다.

### 2. Python으로 실행

> Python 3.10 이상이 필요합니다.

```bash
git clone https://github.com/solanian/clawffice.git
cd clawffice
python3 -m pip install -r backend/requirements.txt
cp state.sample.json state.json
cd backend
python3 app.py
```

상태를 바꿔 보려면 프로젝트 루트에서:

```bash
python3 set_state.py writing "문서를 정리하는 중"
python3 set_state.py executing "명령을 실행하는 중"
python3 set_state.py idle "대기 중"
```

---

## 주요 기능

1. **상태 시각화**: `idle`, `writing`, `researching`, `executing`, `syncing`, `error` 상태를 오피스 안의 위치와 애니메이션으로 표시합니다.
2. **멀티 agent 협업**: join key를 통해 다른 agent를 오피스에 초대하고 상태를 함께 볼 수 있습니다.
3. **어제의 메모**: `memory/*.md`에서 최근 작업 기록을 읽어 요약 패널로 보여줍니다.
4. **한국어 UI**: 기본 UI를 한국어로 사용할 수 있고, 로딩/상태/agent 표시 문구가 한국어로 제공됩니다.
5. **AI 방 꾸미기**: Gemini API를 연결하면 오피스 배경 이미지를 생성할 수 있습니다.
6. **Docker/Portainer 배포**: Dockerfile, Compose, Portainer stack 파일을 포함합니다.
7. **데스크톱 펫 모드**: Electron 기반 데스크톱 표시 모드를 선택적으로 사용할 수 있습니다.

---

## OpenClaw 연동

OpenClaw agent 규칙 파일에 다음 흐름을 추가하면 작업 상태가 Clawffice🦞에 자동으로 반영됩니다.

```markdown
## Clawffice🦞 상태 동기화
- 작업 시작 전: `python3 set_state.py <state> "<설명>"` 실행
- 작업 완료 후: `python3 set_state.py idle "대기 중"` 실행
```

상태 매핑:

| 상태 | 오피스 영역 | 용도 |
| --- | --- | --- |
| `idle` | 휴식 공간 | 대기 / 작업 완료 |
| `writing` | 작업 공간 | 코드 작성 / 문서 작성 |
| `researching` | 작업 공간 | 조사 / 검색 |
| `executing` | 작업 공간 | 명령 실행 / 테스트 |
| `syncing` | 작업 공간 | 동기화 / push |
| `error` | 오류 영역 | 오류 / 디버깅 |

---

## 자주 쓰는 API

| Endpoint | 설명 |
| --- | --- |
| `GET /health` | 헬스 체크 |
| `GET /status` | 메인 agent 상태 조회 |
| `POST /set_state` | 메인 agent 상태 변경 |
| `GET /agents` | agent 목록 조회 |
| `POST /join-agent` | 방문 agent 참여 |
| `POST /agent-push` | 방문 agent 상태 push |
| `POST /leave-agent` | 방문 agent 나가기 |
| `GET /yesterday-memo` | 어제의 메모 조회 |

---

## 배포 참고

- 로컬 Docker 실행: [`docker-compose.yml`](./docker-compose.yml)
- Portainer stack 예시: [`deploy/portainer-stack.yml`](./deploy/portainer-stack.yml)
- Portainer 배포 메모: [`docs/PORTAINER_DEPLOY.md`](./docs/PORTAINER_DEPLOY.md)

프로덕션에서는 `.env.example`을 복사해 `.env`를 만들고, `FLASK_SECRET_KEY`와 `ASSET_DRAWER_PASS`를 강한 값으로 설정하세요.

---

## 라이선스

라이선스 정보는 [LICENSE](./LICENSE)를 확인하세요.
