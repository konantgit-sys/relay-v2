# Nostr SSE Client

**Nostr over SSE — no WebSocket required.**

[![PyPI version](https://img.shields.io/badge/pypi-1.0.0-blue)](https://pypi.org/project/nostr-sse-client/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![NIPs](https://img.shields.io/badge/NIPs-01%2C%2004%2C%2042-orange)](https://github.com/nostr-protocol/nips)

A Python Nostr client that speaks to relays over **HTTP SSE** (Server-Sent Events) instead of WebSocket. Works through any proxy, ingress, load balancer, or CDN that blocks WebSocket upgrades — including Cloudflare, Nginx, and AWS ALB.

## Why?

Nostr is designed around WebSocket, but many real-world deployments cannot use WebSocket:

- **Cloudflare Tunnel** — blocks WebSocket upgrade on free tier
- **Corporate proxies** — strip Upgrade headers
- **AWS ALB / NLB** — require sticky sessions for WSS
- **Mobile apps** — SSE is simpler than WebSocket reconnection logic
- **Serverless** — SSE is compatible with Lambda/Cloud Functions

SSE is just HTTP POST + streaming response. It goes through anything.

## Protocol

Instead of `["REQ", sub_id, filters]` over WebSocket, send:

```
POST /nostr  {"method":"REQ","params":["s1",{"kinds":[1],"limit":10}]}
→ SSE stream: data: ["EVENT","s1",{...}]
               data: ["EOSE","s1"]
```

Instead of `["EVENT", {...}]`:

```
POST /nostr  {"method":"EVENT","params":[{...event...}]}
→ {"status":"ok"}
```

Zero protocol changes on the relay side — SSE handler parses standard Nostr messages and streams them back.

## Features

| NIP | Feature | Status |
|-----|---------|--------|
| NIP-01 | Event signing, REQ/EVENT/CLOSE | ✅ |
| NIP-19 | bech32 keys (nsec, npub) | ✅ |
| NIP-04 | Encrypted DMs (AES-256-CBC + ECDH) | ✅ |
| NIP-42 | AUTH authentication | ✅ |
| — | Server-Sent Events (SSE) transport | ✅ |
| — | Auto-reconnect + live follow mode | ✅ |
| — | Relay info (NIP-11) | ✅ |
| — | Dashboard + IPFS stats | ✅ |

## Installation

```bash
pip install nostr-sse-client
```

## Quick Start

### Generate a key

```bash
nostr-sse --gen-key
```

### Subscribe to events

```bash
nostr-sse --relay https://relay-snin.v2.site \
  --subscribe '{"kinds":[1],"limit":10}'
```

### Publish a note

```bash
nostr-sse --relay https://relay-snin.v2.site \
  --nsec nsec1abc... \
  --publish "Hello, Nostr!"
```

### Follow live stream

```bash
nostr-sse --relay https://relay-snin.v2.site \
  --follow --subscribe '{"kinds":[1]}'
```

### Authenticate (NIP-42)

```bash
nostr-sse --relay https://relay-snin.v2.site \
  --nsec nsec1abc... --auth
```

### Send encrypted DM (NIP-04)

```bash
nostr-sse --relay https://relay-snin.v2.site \
  --nsec nsec1abc... \
  --dm <recipient_pubkey_hex> "Secret message"
```

### Read inbox

```bash
nostr-sse --relay https://relay-snin.v2.site \
  --nsec nsec1abc... --inbox
```

## Python API

```python
from nostr_sse_client import NostrSSEClient, generate_keypair

# Generate keypair
kp = generate_keypair()

# Create client
client = NostrSSEClient(
    relay_url="https://relay-snin.v2.site",
    private_key=nsec_to_private_key(kp["nsec"])
)

# Publish
result = client.sign_and_publish("Hello Nostr via SSE!")

# Subscribe (generator)
for event, eose in client.subscribe({"kinds": [1], "limit": 5}):
    if eose:
        break
    print(f"From: {event.pubkey[:20]}...")
    print(f"Content: {event.content[:100]}")

# DM
client.send_dm(recipient_pubkey_hex, "Secret message")
events = client.fetch_inbox(limit=10)

# AUTH
client.authenticate()
```

## Architecture

```
┌─────────────┐     POST /nostr (JSON)     ┌──────────────┐
│  Nostr SSE  │ ──────────────────────────> │  Nostr Relay │
│   Client    │   SSE stream (events)       │  (SSE Mode)  │
│             │ <────────────────────────── │              │
└─────────────┘                             └──────────────┘
    │                                             │
    │ No WebSocket Upgrade                        │ HTTP only
    ▼                                             ▼
HTTP/1.1 anywhere                    Works behind Cloudflare,
                                    Nginx, AWS ALB, any proxy
```

## Running a compatible relay

Any Nostr relay with an SSE endpoint at `POST /nostr` works. The SNIN Relay V2 at `relay-snin.v2.site` supports SSE natively alongside WebSocket.

To add SSE to your own relay, expose a `POST /nostr` route:
- REQ → returns `text/event-stream` with Nostr events
- EVENT → accepts and stores events
- AUTH → NIP-42 authentication

## License

MIT

## Author

[SNIN Network](https://github.com/snin) — Sovereign Network Infrastructure
