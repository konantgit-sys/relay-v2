#!/usr/bin/env python3
"""
Nostr crypto helpers: keys, signatures, event creation.
"""
import json, hashlib, time, os
import requests
from coincurve import PrivateKey, PublicKey
import bech32

def generate_private_key() -> bytes:
    """Generate random 32-byte private key."""
    return os.urandom(32)

def private_key_to_nsec(privkey: bytes) -> str:
    """Convert private key bytes to nsec bech32."""
    converted = bech32.convertbits(privkey, 8, 5)
    return bech32.bech32_encode("nsec", converted)

def nsec_to_private_key(nsec: str) -> bytes:
    """Convert nsec bech32 to private key bytes."""
    hrp, data = bech32.bech32_decode(nsec)
    if hrp != "nsec":
        raise ValueError(f"Invalid nsec prefix: {hrp}")
    converted = bech32.convertbits(data, 5, 8)
    key = bytes(converted)
    if len(key) == 33 and key[-1] == 0:
        key = key[:32]
    return key

def public_key_to_npub(pubkey: bytes) -> str:
    """Convert public key bytes to npub bech32."""
    converted = bech32.convertbits(pubkey, 8, 5)
    return bech32.bech32_encode("npub", converted)

def npub_to_public_key(npub: str) -> bytes:
    """Convert npub bech32 to public key bytes."""
    hrp, data = bech32.bech32_decode(npub)
    if hrp != "npub":
        raise ValueError(f"Invalid npub prefix: {hrp}")
    converted = bech32.convertbits(data, 5, 8)
    return bytes(converted)

def schnorr_sign(private_key: bytes, message_hash: bytes) -> bytes:
    """Sign a 32-byte hash with Schnorr signature scheme (BIP340)."""
    k = PrivateKey(private_key)
    return k.sign_schnorr(message_hash)

def schnorr_verify(public_key: bytes, message_hash: bytes, signature: bytes) -> bool:
    """Verify Schnorr signature (64 bytes)."""
    try:
        pk = PublicKey.from_xonly(public_key)
        return pk.verify_schnorr(message_hash, signature)
    except Exception:
        return False

def compute_event_id(event: dict) -> str:
    """Compute Nostr event ID per NIP-01: SHA256([0, pubkey, created_at, kind, tags, content])."""
    serialized = json.dumps([
        0,
        event.get("pubkey", ""),
        event.get("created_at", 0),
        event.get("kind", 1),
        event.get("tags", []),
        event.get("content", "")
    ], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode()).hexdigest()

def create_signed_event(
    private_key: bytes,
    content: str,
    kind: int = 1,
    tags: list = None,
    created_at: int = None
) -> dict:
    """Create and sign a Nostr event using nostr_protocol (valid Schnorr)."""
    from nostr_protocol import Keys, SecretKey, EventBuilder, Kind
    
    sk = SecretKey.from_bytes(private_key)
    keys = Keys(sk)
    
    tag_list = tags or []
    builder = EventBuilder(Kind(kind), content, tag_list)
    if created_at:
        builder = builder.custom_created_at(created_at)
    
    event = builder.to_event(keys)
    return json.loads(event.as_json())

def generate_keypair() -> dict:
    """Generate a new Nostr keypair, returns dict with nsec, npub, privkey_hex, pubkey_hex."""
    privkey = generate_private_key()
    pubkey_bytes = PrivateKey(privkey).public_key.format()  # 33 bytes with 02/03 prefix
    pubkey_xonly_hex = pubkey_bytes[1:].hex()  # 32 bytes → 64 hex, strip prefix byte
    return {
        "nsec": private_key_to_nsec(privkey),
        "npub": public_key_to_npub(pubkey_bytes[1:]),
        "privkey_hex": privkey.hex(),
        "pubkey_hex": pubkey_xonly_hex
    }
