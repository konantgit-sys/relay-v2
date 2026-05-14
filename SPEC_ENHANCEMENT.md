# SPEC Enhancement — SNIN Relay V2

**Статус реализации:**

## ✅ NIP-XX Solana Payments (v3.2.0)
- [x] kind:30000 — payment event with Solana tx verification
- [x] kind:30001 — payment confirmation event
- [x] kind:30002 — payment receipt event
- [x] SNIN token deployed: `AZFF8K8NcA6gX19Dnv4gsnfbSD7g6rswD4PinEeBxZAN` (mainnet, supply 1,000,000)
- [x] `extract_transfer_info` — SOL (System Program) + SPL token support
- [x] Double-spend prevention (in-memory seen set)
- [x] Fee address: `2uHqUwHDJFvuWXub5oUovDznQ4KvWyMntGwcgokET6c4`
- [x] `/api/payments` endpoint
- [x] Bridge relay-snin → relay-mesh (P2P agent mesh)

## ✅ Core Relay Features
- [x] 22-24 NIPs supported
- [x] SQLite event storage (FTS5)
- [x] WebSocket + SSE transport
- [x] IPFS PubSub mesh (16 peers)
- [x] MassPulse fanout (3000+ relays)
- [x] DAO Groups + Voting
- [x] Agent Registry (79 agents)
- [x] NIP-96 Blossom file storage
- [x] Docker support

## 📋 План на v3.3.0 (следующая версия)
- [ ] Tier subscriptions (bind kind:30000 to relay write/read permissions)
- [ ] SNIN token staking for relay operators
- [ ] kind:30003 — subscription tier activation
- [ ] Auto-withdrawal of fees to relay operator wallet
