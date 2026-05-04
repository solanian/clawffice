# Server Handoff - Star Office UI / OpenClaw

Date: 2026-05-01

This is the repository-safe handoff summary. The full operational handoff with host-specific process IDs, local URLs, and runtime key values was delivered directly to the target server workspace and should not be committed to a public repository.

## Summary

Star Office UI and the `star-office-ui` OpenClaw agent were migrated from the local development machine to a Linux server workspace.

The server-side Star Office UI backend was started, the OpenClaw agent was registered in that UI, and the agent was configured to use Korean as the default user-facing language.

## Repository Changes

Key code/config changes included:

- `AGENTS.md`: Korean default reply rules and Star Office status sync rules.
- `IDENTITY.md`: identity set to `스타 오피스 도우미` with emoji `🧭`.
- `office-agent-push.py`: supports `OFFICE_JOIN_KEY`, `OFFICE_AGENT_NAME`, and `OFFICE_URL`.
- `frontend/office-agent-push.py`: same environment-variable support as the root push script.
- `.gitignore`: ignores local runtime artifacts such as `.openclaw/` and `office-agent-state.json`.

## Runtime Setup

The legacy Python/Flask server has been replaced by the TypeScript/SvelteKit server. Use the Docker service for deployment:

```bash
docker compose up -d --build
```

## OpenClaw

The remote OpenClaw version observed during setup was:

```text
OpenClaw 2026.4.29
```

The agent was configured as:

- Agent id: `star-office-ui`
- Identity name: `스타 오피스 도우미`
- Identity emoji: `🧭`
- Default user-facing language: Korean

Verification returned Korean and confirmed the expected server workspace.

## Star Office UI Registration

The Star Office UI `/agents` endpoint showed the registered OpenClaw agent with:

- Auth status: `approved`
- State: `idle`
- Area: `breakroom`
- Source: `remote-openclaw`

The push script was started with environment variables rather than hardcoded values:

```bash
OFFICE_JOIN_KEY=<runtime join key>
OFFICE_AGENT_NAME="스타 오피스 도우미"
OFFICE_URL=<office url>
OFFICE_LOCAL_STATE_FILE=<workspace state.json path>
python office-agent-push.py
```

Do not commit the generated `office-agent-state.json`; it contains runtime join state.

## Gateway Status

OpenClaw gateway initially needed repair after plugin runtime dependency setup. Running `openclaw doctor --non-interactive --repair --yes` restarted the service. Final status observed:

```text
Runtime: running
Connectivity probe: ok
```

## Local Cleanup

The local Star Office UI backend and local push process that had been started during setup were stopped. Local port `19000` was no longer listening after cleanup.

## Follow-Up

The server backend and push process were started as background processes. If they should survive reboot, create service manager units for:

- Star Office UI server
- Star Office UI agent push

Before exposing the UI publicly, configure strong production secrets:

- `STAR_OFFICE_SECRET`
- `ASSET_DRAWER_PASS`
