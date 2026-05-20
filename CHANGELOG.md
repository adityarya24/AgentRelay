# Changelog

All notable changes to AgentRelay are documented here.

## [2.1.0] — 2026-05-20

### Added
- Release deployment plumbing (GitHub Actions)
- Public Telegram lab onboarding flow
- CI hardening: prevent start command tests from blocking pipeline

### Fixed
- Code review findings from clawpatch audit

## [2.0.0] — 2026-05-13

### Added
- Telegram group supervision mode with bot-to-bot transport
- Route policy engine for governed message flow
- Identity registry with stable IDs, trust levels, and secret refs
- Room-scoped thread model with auth hardening
- Genericized agent names for public release

### Changed
- Full production hardening pass (error handling, edge cases, persistence)

## [1.0.0] — 2026-04-25

### Added
- WebSocket relay server with room-based multi-agent communication
- MCP-first architecture with ClientPool and `from_agent` params
- Governance engine: rate limits, cooldowns, thread caps, inactivity timeouts
- Telegram bot command auto-registration
- SQLite WAL persistent storage
- Docker support (Dockerfile + docker-compose)
- CLI interface (`agentrelay start`, `agentrelay status`)
- Comprehensive test suite (7 phased test files)
- MIT license
