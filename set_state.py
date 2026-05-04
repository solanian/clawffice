#!/usr/bin/env python3
"""clawffice 상태를 업데이트합니다.

OpenClaw에서 상태를 자동 동기화하려면 agent의 SOUL.md 또는 AGENTS.md에 규칙을 추가하세요:
  작업 시작 전: `python3 set_state.py writing "작업 내용"`
  작업 완료 후: `python3 set_state.py idle "대기 중"`
Office UI는 이 스크립트가 쓰는 state.json을 읽습니다.
"""

import json
import os
import sys
from datetime import datetime

STATE_FILE = (
    os.environ.get("CLAWFFICE_STATE_FILE")
    or os.environ.get("STAR_OFFICE_STATE_FILE")
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
)

VALID_STATES = [
    "idle",
    "writing",
    "receiving",
    "replying",
    "researching",
    "executing",
    "syncing",
    "error"
]

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "state": "idle",
        "detail": "대기 중...",
        "progress": 0,
        "updated_at": datetime.now().isoformat()
    }

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python3 set_state.py <state> [detail]")
        print(f"상태 옵션: {', '.join(VALID_STATES)}")
        print("\n예시:")
        print("  python3 set_state.py idle")
        print("  python3 set_state.py researching \"Godot MCP를 조사하는 중...\"")
        print("  python3 set_state.py writing \"핫딜 리포트 템플릿을 작성하는 중...\"")
        sys.exit(1)
    
    state_name = sys.argv[1]
    detail = sys.argv[2] if len(sys.argv) > 2 else ""
    
    if state_name not in VALID_STATES:
        print(f"유효하지 않은 상태: {state_name}")
        print(f"사용 가능한 상태: {', '.join(VALID_STATES)}")
        sys.exit(1)
    
    state = load_state()
    state["state"] = state_name
    state["detail"] = detail
    state["updated_at"] = datetime.now().isoformat()
    
    save_state(state)
    print(f"상태가 업데이트되었습니다: {state_name} - {detail}")
