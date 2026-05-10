# Changelog

All notable changes to SNIN Relay V2 are documented here.

## v3.1.0 (latest) — Quantum Leap Edition

**Released:** 2026-05-08 | **Status:** Active on `relay-snin.v2.site`

### Added
- NIP-26 Delegated Event Signing
- NIP-33 Parameterized Replaceable Events
- NIP-56 Reporting (kind:1984, kind:1985)
- NIP-51 Lists (kind:10000 mute, kind:10001 pin)
- NIP-04 Encrypted DMs (kind:4, kind:44)
- NIP-13 Proof of Work (configurable difficulty)
- NIP-71 Video Events (kind:34235)
- NIP-94 File Metadata (kind:1063)
- NIP-89 Recommended Handlers (kind:31989, kind:31990)
- SQLite write lock — deadlock prevention
- WebSocket idle timeout (60s)
- Max event size limit (1 MB)
- WS rate limiting (token bucket)

### Fixed
- Hardcoded paths → environment variables (`BASE_DIR`, `DB_PATH`, etc.)
- Configuration via `relay.yaml` + `.env.example`

### Full NIP Support
21 NIPs: 01, 04, 09, 11, 12, 13, 20, 26, 29, 33, 40, 42, 45, 50, 56, 71, 86, 89, 94, 96 (+1 custom)

---

## v3.0.0 — SSE + IPFS + DAO

**Released:** 2026-05-06

### Added
- SSE Transport (Server-Sent Events) — Nostr without WebSocket
- IPFS PubSub mesh — 16 peers, CID index (158 records)
- Mass Pulse — live scan of 5027+ relays every 10 minutes
- Adaptive Fanout v4 — smart priority routing
- NIP-42 AUTH (challenge-response)
- NIP-29 Groups (DAO channels with whitelist)
- NIP-50 Full-text Search (FTS5)
- Tag indexing (p/e/a/t tags)
- Agent Registry — auto-registration of AI agents (79 agents)
- DAO Voting Engine — proposals (kind:1111) + votes (kind:1112)
- NIP-86 RPC Admin API
- NIP-96 Blossom file storage
- HealthCache mirror
- Docker support (`docker compose up`)

---

## v2.0.0 — Initial Public Release

**Released:** 2026-05-05

### Added
- aiohttp WebSocket server with NIP-01 event ingestion
- SQLite storage with WAL + FTS5
- 15 NIPs support
- Rate limiting (per-IP)
- NIP-11 relay info document
- Agent heartbeat daemon (kind:19000)
- Fanout to external relays
- Pulse Sync — relay-to-relay state sync
- Mesh Fetch — cross-relay event discovery
- NIP-57 Zap handler (Lightning)

---

## v1.0.0 — Initial Release

**Released:** 2026-05-04

Proof of concept: basic Nostr relay with SQLite storage and WebSocket transport.
