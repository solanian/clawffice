# Oracle Server Handoff - clawffice / OpenClaw

Date: 2026-05-01
Remote host: `opc@oracle-server`
Remote workspace: `/home/opc/workspace/clawffice`

## Summary

clawffice and the `clawffice` OpenClaw agent were migrated from the local Mac workspace to the Oracle Linux server.

The remote clawffice backend is running, the remote OpenClaw agent is registered in that UI, and the agent is configured to use Korean as the default user-facing language.

## What Was Moved

The local workspace was synchronized to:

```bash
/home/opc/workspace/clawffice
```

The rsync excluded local-only runtime artifacts such as `.venv/`, `node_modules/`, `__pycache__/`, `.DS_Store`, and `office-agent-state.json`.

Key code/config changes included:

- `AGENTS.md`: Korean default reply rules and clawffice status sync rules.
- `IDENTITY.md`: identity set to `Star`.
- `office-agent-push.py`: supports `OFFICE_JOIN_KEY`, `OFFICE_AGENT_NAME`, and `OFFICE_URL`.
- `frontend/office-agent-push.py`: same environment-variable support as the root push script.

## Remote Runtime

Updated 2026-05-01: clawffice now runs from Docker through Portainer, not from a host Python venv.

Portainer server:

```text
https://100.75.202.58:9443
```

Target endpoint:

```text
Oracle Server / tcp://100.75.230.136:9001
```

Published URL:

```text
http://100.75.230.136:19000
```

Portainer stack:

```text
clawffice
```

The old workspace `.venv` was removed from the workspace and moved to:

```text
/tmp/clawffice-venv-20260501-removed
```

## Running Processes

Docker containers managed by the Portainer stack:

```text
clawffice
clawffice-agent-push
```

## OpenClaw

Remote OpenClaw:

```bash
OpenClaw 2026.4.29 (a448042)
Binary: /home/opc/.npm-global/bin/openclaw
```

The remote agent was created as:

```bash
openclaw agents add clawffice \
  --workspace /home/opc/workspace/clawffice \
  --non-interactive

openclaw agents set-identity \
  --agent clawffice \
  --workspace /home/opc/workspace/clawffice \
  --from-identity
```

Current agent facts:

- Agent id: `clawffice`
- Workspace: `/home/opc/workspace/clawffice`
- Identity name: `Star`
- Identity emoji: unset/default
- Model observed: `openai-codex/gpt-5.5`

Verification run returned Korean:

```text
저는 `/home/opc/workspace/clawffice` 작업공간을 사용하고 있습니다.
```

## Gateway Status

OpenClaw gateway initially failed because plugin runtime dependency setup had not completed cleanly. Running:

```bash
openclaw doctor --non-interactive --repair --yes
```

restarted the systemd user service.

Final observed gateway status:

```text
Runtime: running
Connectivity probe: ok
Capability: connected-no-operator-scope
Listening: 127.0.0.1:18789
```

Gateway service:

```bash
~/.config/systemd/user/openclaw-gateway.service
```

Gateway log:

```bash
/tmp/openclaw/openclaw-2026-05-01.log
```

## clawffice Registration

The remote UI `/agents` endpoint shows:

- Main agent: `Star`
- Remote OpenClaw agent: `Star`
- Auth status: `approved`
- State: `idle`
- Area: `breakroom`
- Source: `remote-openclaw`

Current state file:

```bash
/home/opc/workspace/clawffice/state.json
```

To set state manually:

```bash
cd ~/workspace/clawffice
python3 set_state.py writing "작업 중"
python3 set_state.py idle "대기 중"
```

## Local Mac Cleanup

The local clawffice backend and local `office-agent-push.py` process that had been started during setup were stopped with Ctrl-C.

Local port `19000` no longer had a listening process after cleanup.

## Operational Checks

Check remote backend:

```bash
curl -s http://100.75.230.136:19000/health
```

Check remote agents:

```bash
curl -s http://100.75.230.136:19000/agents
```

Check remote OpenClaw agent:

```bash
ssh opc@oracle-server 'export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"; openclaw agents list --json'
```

Check gateway:

```bash
ssh opc@oracle-server 'export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"; openclaw gateway status'
```

## Follow-Up

The backend and push process are now Docker services under Portainer. Before exposing it beyond the VPN/LAN, keep strong values configured for:

- `FLASK_SECRET_KEY` or `CLAWFFICE_SECRET`
- `ASSET_DRAWER_PASS`
