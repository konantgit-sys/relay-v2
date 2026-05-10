# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 3.1.x (latest) | ✅ |
| < 3.1 | ❌ |

## Reporting a Vulnerability

We take security seriously. If you discover a vulnerability in SNIN Relay V2:

1. **Do NOT** open a public GitHub issue.
2. Send details to **konant.git@gmail.com** with subject `[SECURITY] SNIN Relay`.
3. Include: description, steps to reproduce, impact, suggested fix (if any).

You will receive a response within 48 hours. We will coordinate a fix and disclosure timeline.

## Scope

- Relay event validation and storage
- NIP-42 authentication
- IPFS PubSub integration
- Rate limiting and DoS protection
- Blossom file upload handling

## Out of scope

- Nostr protocol vulnerabilities (report to nostr-protocol/nips)
- Third-party dependencies (report to respective maintainers)

## Responsible Disclosure

We ask researchers to allow 90 days between initial report and public disclosure for critical vulnerabilities.
