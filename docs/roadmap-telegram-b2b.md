# AgentRelay Telegram Bot-to-Bot Roadmap

Date: 2026-05-11
Owner: Aditya
Project stance: public self-hosted open-source first, tested on Aditya's VPS agent ecosystem.

## North Star

AgentRelay is a governed real-time communication hub for AI agents and bots.

It should let MCP agents, WebSocket agents, Telegram bots, OpenClaw, Hermes, Claude Code, and external user-provided bots communicate through one broker with identity, routing policy, loop prevention, audit logs, and admin control.

Telegram is a transport. AgentRelay owns the source of truth for identity, permissions, conversation state, safety decisions, and observability.

## Product Boundary

AgentRelay owns:

- identity registry for agents and bots
- rooms, threads, DMs, and conversation state
- allow/deny routing policy
- rate limits, cooldowns, duplicate detection, max depth, TTL, and circuit breakers
- audit logs for accepted and rejected messages
- onboarding and verification flows for external bots

AgentRelay does not own:

- BotFather account settings
- individual model/provider execution
- OpenClaw, Hermes, or Claude Code internal runtime state
- private bot tokens beyond secure local storage and runtime use

## Phase 0: Product Boundary

Goal: make the project easy to reason about before implementation grows.

Deliverables:

- `docs/vision.md`
- `docs/architecture.md`
- public terminology for identities, transports, rooms, policies, and guardrails

Acceptance checks:

- New contributors can explain what AgentRelay is and is not.
- Telegram bot-to-bot is documented as one connector, not the whole product.

## Phase 1: Core Hardening

Goal: make the existing relay safe enough to build public-facing features on top.

Deliverables:

- REST endpoints require authentication, except health/version endpoints.
- WebSocket auth no longer depends on tokens in URL query strings.
- Thread state is scoped by room, not only by thread ID.
- Invalid message types produce a structured protocol error without disconnecting the socket.
- Config validation and secure defaults are documented.

Acceptance checks:

- Full test suite passes on Python 3.11+.
- Unauthenticated users cannot read rooms, history, agents, or metrics.
- A closed thread in one room does not close the same thread ID in another room.
- Bad client messages do not drop the WebSocket connection.

## Phase 2: Identity Registry

Goal: give every local or external participant a durable identity independent of transport.

Deliverables:

- identity model: `id`, `display_name`, `kind`, `transport`, `capabilities`, `enabled`, `trust_level`
- CLI support to add, list, disable, and inspect identities
- separate metadata for local, VPS, and external bots
- secret boundary for bot tokens and relay credentials

Example identities:

- `openclaw.aarya`
- `openclaw.bheem`
- `hermes.karna`
- `claude.bheeshm`
- `external.somebot`

Acceptance checks:

- Policies refer to stable identity IDs, not raw Telegram usernames.
- External identities are disabled or sandboxed by default.

## Phase 3: Policy Engine

Goal: make communication explicitly governed rather than free-for-all.

Deliverables:

- allow/deny rules for `source -> target`
- room-scoped permissions
- pair, agent, and room-level limits
- max depth and max runtime per conversation
- dedupe keys and loop circuit breaker
- optional human approval for risky routes

Acceptance checks:

- Unknown external bots are blocked by default.
- A permitted route works only in the allowed room.
- Runaway bot ping-pong is stopped and logged.
- Admin can see why a message was accepted or rejected.

## Phase 4: Telegram Bot-to-Bot Connector

Goal: make Telegram's bot-to-bot feature a first-class AgentRelay transport.

Deliverables:

- multi-bot token registry
- bot username and identity verification
- inbound Telegram update normalization into AgentRelay messages
- outbound Telegram delivery through the correct bot identity
- BotFather readiness checklist
- verification command for bot-to-bot readiness

Acceptance checks:

- Two registered Telegram bots can exchange messages through AgentRelay.
- Policy violations are blocked before Telegram delivery.
- Accepted and rejected Telegram routes are auditable.
- BotFather setup gaps are detected and shown clearly.

## Phase 5: External Bot Onboarding

Goal: let public users connect their own bots safely.

Deliverables:

- `agentrelay telegram add-bot`
- `agentrelay telegram verify-bot`
- claim/invite flow for external bots
- revoke/remove bot flow
- sandbox policy for untrusted bots

Acceptance checks:

- An external bot can be added without access to private trusted rooms.
- Admin can promote a bot into selected rooms.
- Removing a bot revokes its routes and stops delivery.

## Phase 6: Admin UX

Goal: make setup and debugging straightforward without editing random files.

Deliverables:

- `agentrelay init`
- `agentrelay doctor`
- `agentrelay identities list`
- `agentrelay policy allow`
- `agentrelay telegram verify-all`
- profile-based startup such as `agentrelay run --profile vps`

Acceptance checks:

- A single command reports missing tokens, BotFather readiness, offline bots, and blocked policies.
- Non-expert users can follow onboarding without reading source code.

## Phase 7: VPS Integration Lab

Goal: test against Aditya's real multi-agent ecosystem before public release.

Profiles:

- `openclaw`
- `hermes`
- `claude-code`
- `external-test`

Acceptance checks:

- OpenClaw agents communicate through AgentRelay only through allowed routes.
- Hermes agents can talk to OpenClaw only when policy allows.
- Claude Code/Bheeshm can observe or participate safely.
- External test bots cannot access private rooms.
- No Aarya DM or cross-agent routing leakage occurs.
- Restart recovery preserves identity, policy, and thread state.

## Phase 8: Public Release

Goal: ship a usable public project with safe defaults.

Deliverables:

- Dockerfile or Compose setup
- quickstart guide
- Telegram bot-to-bot guide
- security model document
- sample policies
- example three-bot workflow
- GitHub Actions CI
- PyPI release checklist

Acceptance checks:

- Fresh user can install, add two bots, verify them, create a policy, and send a safe test message in under 15 minutes.
- Public docs warn clearly about token handling, BotFather requirements, and loop prevention.

## Immediate Work Order

1. Complete Phase 1 hardening.
2. Add identity registry.
3. Add policy engine.
4. Add Telegram bot-to-bot connector.
5. Add external bot onboarding.
6. Run VPS integration lab.
7. Polish and release.

Do not implement Telegram bot-to-bot before Phase 1 hardening. Every public feature depends on the relay core being safe, scoped, and observable.

