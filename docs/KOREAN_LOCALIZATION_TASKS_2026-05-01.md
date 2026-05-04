# clawffice Korean Localization Checklist

Date: 2026-05-01

## Completed

- [x] Changed the default UI language to `ko`.
- [x] Forced this deployment to keep `ko` in `localStorage`.
- [x] Hid the language selector so Chinese is not selectable or visible in the UI.
- [x] Added Korean strings to the main UI translation pack.
- [x] Updated the default font to the Korean ArkPixel font.
- [x] Translated initial loading, memo, status controls, guest panel, drawer, asset tools, Gemini settings, and mobile pan status copy.
- [x] Translated visitor list states, guest bubbles, demo visitor names, alerts, and welcome bubbles.
- [x] Translated `/join` and `/invite` pages.
- [x] Updated public invitation examples to `http://100.75.230.136:19000`.
- [x] Translated backend user-facing API response messages that can surface in the UI.
- [x] Translated agent-push fallback status and log messages.
- [x] Renamed the UI-only agent display from `스타 오피스 도우미` to `스타 오피스 관리자`.
- [x] Later removed the UI-only manager agent and connected the existing OpenClaw `Star` agent instead.

## Verification

- [x] `frontend/join.html` has no remaining Han-script Chinese text.
- [x] `frontend/invite.html` has no remaining Han-script Chinese text.
- [x] Backend `msg` and `RuntimeError` strings exposed through JSON responses are Korean or neutral English identifiers.
- [x] Verify the deployed Docker container responds at `http://100.75.230.136:19000/health`.
- [x] Verify Portainer stack registration on endpoint `Oracle Server` after deployment.
