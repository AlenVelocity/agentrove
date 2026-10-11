# Agentrove

Self-hosted AI coding workspace for running and orchestrating Antigravity, Claude Code, Codex, Copilot, Cursor, Grok, and OpenCode agents from one interface.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![React 19](https://img.shields.io/badge/React-19-61DAFB.svg)](https://react.dev/)
[![Discord](https://img.shields.io/badge/Discord-5865F2.svg?logo=discord&logoColor=white)](https://discord.gg/HvkJU8dcBA)

![Chat Interface](screenshots/chat-interface.png)

## What It Does

- Runs Antigravity, Claude, Codex, Copilot, Cursor, Grok, and OpenCode through ACP adapters.
- Gives each workspace its own Docker or host sandbox.
- Combines chat, code editor, terminal, file tree, diffs, secrets, and git tools in one workspace.
- Supports workspaces from empty folders, git clones, existing local folders, or GitHub repositories.
- Streams agent sessions with cancellation, permission prompts, queued follow-up messages, file mentions, slash commands, and attachments.
- Includes sub-threads, pinned chats, worktree mode, personas, custom instructions, environment variables, and installed agent skills.
- Orchestrates multi-agent workflows through the bundled MCP server: a lead chat spawns worker sub-threads on any installed agent, model, and persona — in parallel worktrees when needed — then reviews their results.
- Hosts channels: group chats where you and several agents, each with its own model and persona, discuss and work in one shared workspace.
- Provides GitHub-assisted repository browsing, pull request review, PR creation, reviewer selection, and git branch/commit/push/pull helpers.
- Ships as a Docker web app, a macOS desktop app, and a native iOS app.

## Orchestrating Agents

Agentrove chats aren't just endpoints — they can drive each other. The bundled MCP server (`mcp-server/`) exposes the whole instance as tools (`send_message`, `get_messages`, `list_models`, `list_personas`, …), and every chat's agent has those tools available. That turns any chat into an orchestrator: a lead agent on a strong model that decomposes the work, routes each task to the right agent, and reviews what comes back.

The primitives:

- **Sub-threads** — `send_message(parent_chat_id=…)` creates a worker chat grouped under the lead chat, in the same workspace and branch. Sub-threads stay flat (no nesting): the lead fans out, workers report back.
- **Per-turn model and persona** — each worker runs on any installed agent and model (`model_id`) with any persona (a custom system prompt). Typical fleet: a fast model with a read-only scout persona for codebase exploration, coding models for implementation, dedicated reviewer personas (bug hunting, structural quality) for QA. `list_models` reports each model's supported reasoning tiers (`thinking_modes`), so the lead dials effort per task.
- **Isolated worktrees** — `worktree=true` gives a worker its own git worktree, so parallel workers edit concurrently without conflicts.
- **Polling and follow-ups** — the lead polls `get_messages` until a worker's turn completes, judges the result against the actual code, and sends rework to the worker's own thread; follow-ups inherit the thread's previous model, persona, and reasoning settings.
- **Unattended turns** — orchestrated turns run in the agent's full-execution mode, so workers finish without permission prompts.

A typical loop: the lead explores with a cheap fast model, hands a precise spec to a coding model in a sub-thread, fans reviewer personas out over the diff in parallel, triages their findings, delegates the accepted fixes — and owns the final result. Models, personas, and routing rules are all user-defined, so the same machinery drives whatever fleet you run.

## Channels

Channels are group chats between you and multiple agents. Create one from the sidebar, pick a workspace (optionally a branch or a dedicated worktree), and add members — each with its own agent and model, display name, persona, thinking level, and permission mode.

- **Shared conversation** — every member sees new channel messages in batches and either posts one short reply or passes, so agents speak only when they add something: answering, disagreeing, correcting, or proposing what others missed.
- **Shared workspace** — members work in the same directory and announce which files they're about to change before editing them.
- **Live status** — the status row shows which members are thinking, typing, waiting for approval, or retrying; each reply carries a collapsed "Worked" rollup with its tool activity and duration.
- **Control** — attach files, approve permission prompts inline, and stop running members from the composer.

## Quick Start

Requirements:

- Docker
- Docker Compose

```bash
git clone https://github.com/Mng-dev-ai/agentrove.git
cd agentrove
cp .env.example .env
```

Set `SECRET_KEY` in `.env`:

```bash
openssl rand -hex 32
```

Start Agentrove:

```bash
docker compose up -d
```

Open [http://localhost:3000](http://localhost:3000).

Antigravity supports authentication through Google OAuth completed in a terminal. Its ACP tokens live in `~/.gemini/antigravity-acp/`, separately from the Antigravity CLI login.

## Desktop

Agentrove also has a desktop app built with Tauri, for macOS and Windows. It starts a bundled Python backend sidecar on an available `127.0.0.1` port and connects the frontend to it at launch.

- Download the latest Apple Silicon build from [Releases](https://github.com/Mng-dev-ai/agentrove/releases/latest).
- Build from source:

```bash
cd frontend
npm install
npm run desktop:dev
```

### Windows

Builds from source on Windows x64; there is no published installer yet.

Requirements:

- [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/) with the "Desktop development with C++" workload (Tauri links with MSVC).
- The MSVC Rust toolchain: `rustup toolchain install 1.90.0-x86_64-pc-windows-msvc`, then in the repo `rustup override set 1.90.0-x86_64-pc-windows-msvc`.
- [Git for Windows](https://git-scm.com/download/win). Claude Code needs its bash, and the host sandbox runs commands through it.
- Node 20+ to run the build script. Python, Node 22, ripgrep and the ACP adapters are downloaded into `frontend/backend-sidecar` by `desktop/run_build.mjs`, the same as on macOS.

`npm run desktop:dev` and `npm run desktop:build` work as on macOS; the build produces an NSIS installer under `src-tauri/target/release/bundle/nsis/`. Optional settings go in `%APPDATA%\com.agentrove.app\.env.desktop`.

Not yet working on Windows: the terminal view (host PTYs need a ConPTY implementation), browser-use (installed on a best-effort basis), and code signing.

## Mobile (iOS)

Agentrove also builds a native iOS app with Tauri. Since iOS can't run the local
backend sidecar, the app is a thin client: it talks to an Agentrove instance you
already host (your Docker or production deployment), reachable from the phone over
`https`/`wss`. Because the project is open source, you build and sign it yourself —
nothing is hardcoded to anyone else's server.

Requirements: macOS with Xcode (plus its iOS SDK and Simulator), the Rust iOS
targets (`rustup target add aarch64-apple-ios aarch64-apple-ios-sim`), and CocoaPods.

> If your device runs an iOS beta whose SDK isn't in a stable Xcode yet, build against the latest stable Xcode's SDK (`sudo xcode-select -s /Applications/Xcode.app`) — apps built with an older SDK still run on newer iOS.

Point the app at your instance:

```bash
cd frontend
cp .env.mobile.example .env.mobile   # then set your https/wss URLs
npm install
```

Run in the simulator:

```bash
npm run ios:dev
```

Install on your own iPhone with a free Apple ID — no paid developer account needed
(the app must be re-signed every 7 days):

1. Open `frontend/src-tauri/gen/apple/*.xcodeproj` in Xcode once, pick your Team
   under **Signing & Capabilities**, and connect your iPhone (Xcode needs to
   register the device and create the provisioning profile).
2. On the phone, enable Developer Mode (**Settings → Privacy & Security**), then
   after the first install trust the certificate under
   **Settings → General → VPN & Device Management**.

Build a standalone `.ipa` and install it on the connected iPhone. The easiest path
is the bundled helper, which builds, signs, exports, and installs in one step. It
reads your team from `APPLE_DEVELOPMENT_TEAM` (so nothing is hardcoded to anyone
else's team) and auto-detects the connected device:

```bash
export APPLE_DEVELOPMENT_TEAM=<YOUR_TEAM_ID>   # find it in Xcode → Signing & Capabilities
cd frontend
npm run ios:install
```

Or run the steps yourself. The `-c` flag injects the team into both the build
signing and the IPA export, so it stays out of the committed config. On Xcode 27,
`CARGO_PROFILE_RELEASE_DEBUG` works around a release-link failure
([tauri#16130](https://github.com/tauri-apps/tauri/issues/16130)):

```bash
CARGO_PROFILE_RELEASE_DEBUG=line-tables-only npm run ios:build -- --export-method debugging \
  -c '{"bundle":{"iOS":{"developmentTeam":"<YOUR_TEAM_ID>"}}}'
# -> src-tauri/gen/apple/build/arm64/Agentrove.ipa

xcrun devicectl list devices   # find your device id
xcrun devicectl device install app --device <DEVICE_ID> \
  src-tauri/gen/apple/build/arm64/Agentrove.ipa
```

To update the app later, re-run `npm run ios:install` — the standalone build runs on
the phone without keeping a Mac connected.

## Production

For a single-host Docker deployment:

```bash
SECRET_KEY=$(openssl rand -hex 32) \
APP_URL=https://yourdomain.com \
ALLOWED_ORIGINS=https://yourdomain.com \
docker compose -f docker-compose-production.yml up -d --build
```

## Stack

- Frontend: React 19, TypeScript, Vite, Tailwind CSS, Monaco, xterm.js
- Backend: FastAPI, SQLAlchemy, SQLite, Redis
- Runtime: ACP, Docker or host sandboxes, Tauri desktop sidecar

## Community

Join the [Discord server](https://discord.gg/HvkJU8dcBA).

## Contributing

Contributions and feedback are welcome.

## License

Apache 2.0. See [LICENSE](LICENSE).
