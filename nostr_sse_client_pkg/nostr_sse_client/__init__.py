"""
nostr-sse-client — Nostr client over SSE (Server-Sent Events).

No WebSocket required. Works through any HTTP proxy or ingress.
"""
from .client import NostrSSEClient, NostrEvent
from .utils import (
    generate_keypair, create_signed_event,
    nsec_to_private_key, private_key_to_nsec,
    npub_to_public_key, public_key_to_npub,
    compute_event_id, schnorr_sign, schnorr_verify,
)

__all__ = [
    "NostrSSEClient",
    "NostrEvent",
    "generate_keypair",
    "create_signed_event",
    "nsec_to_private_key",
    "private_key_to_nsec",
    "npub_to_public_key",
    "public_key_to_npub",
    "compute_event_id",
    "schnorr_sign",
    "schnorr_verify",
]
