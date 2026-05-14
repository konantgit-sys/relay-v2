# SNIN Relay V2

**Nostr relay with native Solana payments — for AI agents, DAOs, and decentralized networks.**

[![Nostr](https://img.shields.io/badge/protocol-Nostr-8B5CFE)](https://github.com/nostr-protocol/nostr)
[![Solana](https://img.shields.io/badge/settlement-Solana-9945FF)](https://solana.com)
[![Python](https://img.shields.io/badge/python-3.10+-blue)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![dePIN](https://img.shields.io/badge/sector-dePIN-22C55E)](https://en.wikipedia.org/wiki/Decentralized_physical_infrastructure_network)
[![AI Agents](https://img.shields.io/badge/use--case-AI_Agents-FF6B6B)](https://github.com/topics/ai-agents)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)](https://github.com/konantgit-sys/relay-v2/pulls)

---

**Live:** `wss://relay-snin.v2.site` | **Dashboard:** `https://relay-dash.v2.site` | **Payments API:** `https://relay-snin.v2.site/api/payments`

---

## ✨ Key Features

### 🔷 Solana Payments (NIP-XX)
- **kind:30000** — payment events with Solana tx verification
- **kind:30001** — payment confirmations
- **kind:30002** — payment receipts
- Native SOL + SPL token support (SNIN token: `AZFF8K8NcA6gX19Dnv4gsnfbSD7g6rswD4PinEeBxZAN`)
- Double-spend prevention with in-memory seen set
- Fee collection: configurable per-event fee in SNIN tokens
- REST API at `/api/payments` for transaction history

### 🌐 Nostr Protocol (24 NIPs)
01, 04, 09, 11, 12, 13, 20, 26, 29, 33, 40, 42, 45, 50, 56, 57, 71, 86, 89, 94, 96, +3 custom

### 🧩 Architecture
```
Solana L1 (settlement, 400ms finality)
    ↑
SNIN Relay V2 (NIP-XX: kind:30000-30002)
    ↑
P2P Agent Mesh (pub/sub for AI agents)
    ↑
ESP32 / IoT devices (kind:8010-8017)
```

### 🔗 Integrations
- **Bridge → P2P Agent Mesh** — forward events to `relay-mesh.v2.site`
- **IPFS PubSub** — P2P event propagation (16 peers, CID index)
- **MassPulse** — live scan of 5000+ Nostr relays
- **SSE-Nostr Bridge** — HTTP POST `/nostr` for non-WebSocket clients
- **DAO Voting** — NIP-29 groups with proposal/vote engine
- **Blossom** — NIP-96 decentralized file storage

## Quick Start

```bash
git clone https://github.com/konantgit-sys/relay-v2.git
cd relay-v2
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your Solana wallet
python3 relay_server_v2.py
```

## Configuration

See `relay.yaml` for all options. Key Solana settings:

```yaml
solana:
  rpc_url: "https://api.mainnet-beta.solana.com"
  fee_address: "2uHqUwHDJFvuWXub5oUovDznQ4KvWyMntGwcgokET6c4"
  mint_address: "AZFF8K8NcA6gX19Dnv4gsnfbSD7g6rswD4PinEeBxZAN"
  dev_mode: false
```

## SNIN Token

- **Mint:** `AZFF8K8NcA6gX19Dnv4gsnfbSD7g6rswD4PinEeBxZAN`
- **Decimals:** 9
- **Network:** Solana mainnet
- **Supply:** 1,000,000 SNIN
- **Explorer:** [Solscan](https://solscan.io/token/AZFF8K8NcA6gX19Dnv4gsnfbSD7g6rswD4PinEeBxZAN)

## Documentation
- [SPECIFICATION.md](SPECIFICATION.md) — Full technical specification
- [ARCHITECTURE.md](ARCHITECTURE.md) — System architecture
- [CHANGELOG.md](CHANGELOG.md) — Version history
- [DEPLOY.md](DEPLOY.md) — Deployment guide
- [SECURITY.md](SECURITY.md) — Threat model

## License
MIT
