# Changelog

All notable changes to SNIN Relay V2 are documented here.

## v3.2.0 — Solana Payments Edition (unreleased)

**Status:** Active on `relay-snin.v2.site` | **Mint:** `AZFF8K8NcA6gX19Dnv4gsnfbSD7g6rswD4PinEeBxZAN`

### Added
- **NIP-XX: Solana Payments** — kind:30000-30002 native SOL + SPL token payments
- `snin_payments.py` — payment verification, double-spend prevention, fee collection
- `solana_rpc.py` — Solana RPC client with extract_transfer_info (SOL + SPL support)
- `bridge.py` — event bridge relay-snin → relay-mesh (P2P agent mesh)
- `ws_gateway.py` — WebSocket gateway for external relay connections
- `relay_monitor.py` — real-time relay health monitoring dashboard
- `zap_handler.py` — NIP-57 Lightning zaps handler
- `heartbeat_daemon.py` — relay heartbeat + uptime reporting
- SNIN token deployed on mainnet: `AZFF8K8NcA6gX19Dnv4gsnfbSD7g6rswD4PinEeBxZAN`
- `/api/payments` REST endpoint for payment history
- Dev mode for signature bypass during Solana testing

### Fixed
- `extract_transfer_info` — now handles both SOL (System Program) and SPL Token transfers
- Hardcoded fee_address → configurable via `relay.yaml`
- Double-spend prevention — in-memory seen set with TTL

### Changed
- `relay_server_v2.py` — integrated Solana payment pipeline, bridge mode
- `nostr_marshal.py` — dev mode support (`SNIN_RELAY_DEV_MODE`)
- `relay.yaml` — added `solana.rpc_url`, `solana.fee_address`, `solana.mint_address`
- `sse_handler.py` — broadcast Solana payment events

---

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
- SQLite event storage (20k+ events, FTS5)
- NIP-12 generic tag search
- NIP-15 end-of-stored-events marker
- NIP-20 command results
- NIP-22 comment URL
- NIP-45 event counts with caching
- MassPulse — live relay scanning engine
- IPFS CID indexing (experimental)
