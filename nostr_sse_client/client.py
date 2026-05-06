#!/usr/bin/env python3
"""
Nostr SSE Client — подключается к relay через HTTP SSE вместо WebSocket.

Использование:
  python3 nostr_sse_client.py --relay https://relay-snin.v2.site --gen-key
  python3 nostr_sse_client.py --relay https://relay-snin.v2.site --subscribe '{"kinds":[1],"limit":5}'
  python3 nostr_sse_client.py --relay https://relay-snin.v2.site --publish "Hello from SSE client!"

Без WebSocket Upgrade — только SSE (POST /nostr).
"""

import json, hashlib, time, sys, os, argparse, threading
from typing import Optional, Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin

import requests
from coincurve import PrivateKey, PublicKey
import bech32

# ──────────────────────────────────────────────
# Nostr crypto (Schnorr signatures, keys)
# ──────────────────────────────────────────────

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
    # Bech32 padding may add extra zero byte — trim to 32 bytes
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
    sig = k.sign_schnorr(message_hash)
    return sig

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
    import time
    
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
    pubkey = PrivateKey(privkey).public_key.format()[2:]  # 32 bytes x-only
    return {
        "nsec": private_key_to_nsec(privkey),
        "npub": public_key_to_npub(pubkey),
        "privkey_hex": privkey.hex(),
        "pubkey_hex": pubkey.hex()
    }

# ──────────────────────────────────────────────
# Nostr SSE Client
# ──────────────────────────────────────────────

@dataclass
class NostrEvent:
    id: str
    pubkey: str
    created_at: int
    kind: int
    tags: list
    content: str
    sig: str
    
    @classmethod
    def from_dict(cls, d: dict):
        return cls(
            id=d.get("id", ""),
            pubkey=d.get("pubkey", ""),
            created_at=d.get("created_at", 0),
            kind=d.get("kind", 1),
            tags=d.get("tags", []),
            content=d.get("content", ""),
            sig=d.get("sig", "")
        )
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "pubkey": self.pubkey,
            "created_at": self.created_at,
            "kind": self.kind,
            "tags": self.tags,
            "content": self.content,
            "sig": self.sig
        }


class NostrSSEClient:
    """
    Nostr client using SSE (POST /nostr) instead of WebSocket.
    
    Connects to any relay that supports the SSE Nostr endpoint.
    """
    
    def __init__(self, relay_url: str, private_key: Optional[bytes] = None):
        self.relay_url = relay_url.rstrip("/")
        self.nostr_endpoint = urljoin(self.relay_url, "/nostr")
        self.private_key = private_key
        self.pubkey_hex = None
        self._subscriptions = {}  # sub_id -> callback
        self._auth_token: Optional[str] = None  # NIP-42 auth token
        self._auth_pubkey: Optional[str] = None  # authenticated pubkey
        
        if private_key:
            self.pubkey_hex = PrivateKey(private_key).public_key.format().hex()[2:]
    
    @property
    def is_authenticated(self) -> bool:
        """Check if client has valid NIP-42 auth token."""
        return self._auth_token is not None
    
    def _headers(self) -> dict:
        """Get request headers, including auth token if available."""
        h = {"Content-Type": "application/json"}
        if self._auth_token:
            h["X-Auth-Token"] = self._auth_token
        return h
    
    @property
    def npub(self) -> Optional[str]:
        if self.pubkey_hex:
            return public_key_to_npub(bytes.fromhex(self.pubkey_hex))
        return None
    
    # ── REQ: Subscribe ──
    
    def subscribe(self, filters: dict, callback: Callable = None,
                  sub_id: str = None, follow: bool = False,
                  max_retries: int = 10, retry_delay: float = 3.0):
        """
        Open SSE subscription. Returns events via callback or yields them.
        
        Args:
            filters: Nostr filter dict (kinds, limit, since, etc.)
            callback: function(sub_id, event, eose=False, notice=None, error=None)
                      called for each event/message
            sub_id: optional subscription ID (auto-generated if None)
            follow: keep streaming after EOSE, reconnect on disconnect
            max_retries: max reconnect attempts (default 10)
            retry_delay: seconds between reconnects (default 3.0)
        
        Returns:
            sub_id (if callback) or generator of (event, eose_flag) tuples
        """
        if sub_id is None:
            sub_id = f"s{int(time.time())}_{len(self._subscriptions)}"
        
        if callback:
            self._subscriptions[sub_id] = callback
            threading.Thread(
                target=self._run_subscription,
                args=(sub_id, filters, follow, max_retries, retry_delay),
                daemon=True
            ).start()
            return sub_id
        else:
            return self._stream_events(sub_id, filters, follow, max_retries, retry_delay)
    
    def _run_subscription(self, sub_id: str, filters: dict, follow: bool = False,
                          max_retries: int = 10, retry_delay: float = 3.0):
        """
        Run SSE subscription in a thread, calling callback for each event.
        Retries on both initial connection failure and mid-stream disconnect.
        In follow mode, after EOSE, adds 'since' to filters to avoid replay.
        """
        retries = 0
        current_filters = dict(filters)
        
        while retries < max_retries:
            try:
                payload = json.dumps({"method": "REQ", "params": [sub_id, current_filters]})
                response = requests.post(
                    self.nostr_endpoint,
                    data=payload,
                    headers=self._headers(),
                    stream=True,
                    timeout=(10, None)
                )
                
                if response.status_code != 200:
                    if sub_id in self._subscriptions:
                        self._subscriptions[sub_id](sub_id, None, error=f"HTTP {response.status_code}")
                    retries += 1
                    if follow and retries < max_retries:
                        time.sleep(retry_delay)
                        continue
                    break
                
                retries = 0
                
                if sub_id in self._subscriptions:
                    self._subscriptions[sub_id](sub_id, None)
                
                buffer = ""
                eose_time = 0
                
                for chunk in response.iter_content(chunk_size=1, decode_unicode=True):
                    if not chunk:
                        continue
                    buffer += chunk
                    
                    if buffer.endswith("\n\n"):
                        for line in buffer.strip().split("\n"):
                            if line.startswith("data: "):
                                data_str = line[6:]
                                if not data_str.startswith('['):
                                    continue
                                try:
                                    data = json.loads(data_str)
                                    msg_type = data[0] if data else ""
                                    
                                    if msg_type == "EVENT" and len(data) >= 3 and data[1] == sub_id:
                                        event = NostrEvent.from_dict(data[2])
                                        if sub_id in self._subscriptions:
                                            self._subscriptions[sub_id](sub_id, event)
                                    
                                    elif msg_type == "EOSE" and len(data) >= 2 and data[1] == sub_id:
                                        eose_time = int(time.time())
                                        if sub_id in self._subscriptions:
                                            self._subscriptions[sub_id](sub_id, None, eose=True)
                                    
                                    elif msg_type == "NOTICE" and len(data) >= 2:
                                        if sub_id in self._subscriptions:
                                            self._subscriptions[sub_id](sub_id, None, notice=data[1])
                                    
                                except json.JSONDecodeError:
                                    pass
                        buffer = ""
                
                if follow:
                    if eose_time:
                        # Use 'since' after EOSE to avoid replays
                        current_filters = {k: v for k, v in filters.items() if k != 'limit'}
                        current_filters['since'] = eose_time
                    # Always reconnect in follow mode
                    time.sleep(retry_delay)
                    continue
                break
            
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                retries += 1
                if sub_id in self._subscriptions:
                    self._subscriptions[sub_id](sub_id, None, error=f"connection error: {e}")
                if follow and retries < max_retries:
                    time.sleep(retry_delay * min(retries, 5))
                    continue
                break
                
            except Exception as e:
                retries += 1
                if sub_id in self._subscriptions:
                    self._subscriptions[sub_id](sub_id, None, error=str(e))
                if follow and retries < max_retries:
                    time.sleep(retry_delay * min(retries, 5))
                    continue
                break
        
        if sub_id in self._subscriptions:
            del self._subscriptions[sub_id]
    
    def _stream_events(self, sub_id: str, filters: dict, follow: bool = False,
                        max_retries: int = 10, retry_delay: float = 3.0):
        """Generator that yields (NostrEvent, eose_flag) tuples from SSE stream.
        Retries on both initial connection failure and mid-stream disconnect.
        In follow mode, after EOSE, automatically adds 'since' to avoid replay."""
        retries = 0
        current_filters = dict(filters)  # mutable copy
        
        while retries < max_retries:
            try:
                payload = json.dumps({"method": "REQ", "params": [sub_id, current_filters]})
                response = requests.post(
                    self.nostr_endpoint,
                    data=payload,
                    headers=self._headers(),
                    stream=True,
                    timeout=(10, None)
                )
                
                if response.status_code != 200:
                    retries += 1
                    if follow and retries < max_retries:
                        time.sleep(retry_delay)
                        continue
                    return
                
                retries = 0
                buffer = ""
                eose_time = 0
                
                for chunk in response.iter_content(chunk_size=1, decode_unicode=True):
                    if not chunk:
                        continue
                    buffer += chunk
                    
                    if buffer.endswith("\n\n"):
                        for line in buffer.strip().split("\n"):
                            if line.startswith("data: "):
                                data_str = line[6:]
                                if not data_str.startswith('['):
                                    continue
                                try:
                                    data = json.loads(data_str)
                                    msg_type = data[0] if data else ""
                                    
                                    if msg_type == "EVENT" and len(data) >= 3 and data[1] == sub_id:
                                        yield (NostrEvent.from_dict(data[2]), False)
                                    
                                    elif msg_type == "EOSE" and len(data) >= 2 and data[1] == sub_id:
                                        eose_time = int(time.time())
                                        yield (None, True)
                                    
                                except json.JSONDecodeError:
                                    pass
                        buffer = ""
                
                if follow:
                    if eose_time:
                        # Use 'since' after EOSE to avoid replay
                        current_filters = {k: v for k, v in filters.items() if k != 'limit'}
                        current_filters['since'] = eose_time
                    # Always reconnect in follow mode (EOSE or not)
                    time.sleep(retry_delay)
                    continue
                return
            
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                retries += 1
                if follow and retries < max_retries:
                    time.sleep(retry_delay * min(retries, 5))
                    continue
                return
            
            except Exception:
                retries += 1
                if follow and retries < max_retries:
                    time.sleep(retry_delay * min(retries, 5))
                    continue
                return
    
    # ── Close subscription ──
    
    def close_subscription(self, sub_id: str):
        """Close an active SSE subscription."""
        try:
            payload = json.dumps({"method": "CLOSE", "params": [sub_id]})
            requests.post(
                self.nostr_endpoint,
                data=payload,
                headers=self._headers(),
                timeout=5
            )
        except Exception:
            pass
        if sub_id in self._subscriptions:
            del self._subscriptions[sub_id]
    
    # ── Publish event ──
    
    def publish(self, event: dict) -> dict:
        """
        Publish a Nostr event to the relay.
        
        Args:
            event: signed Nostr event dict (with id and sig)
        
        Returns:
            response dict from relay
        """
        payload = json.dumps({"method": "EVENT", "params": [event]})
        try:
            resp = requests.post(
                self.nostr_endpoint,
                data=payload,
                headers=self._headers(),
                timeout=30
            )
            if resp.status_code == 200:
                try:
                    return resp.json()
                except:
                    return {"status": "ok" if resp.status_code == 200 else "error"}
            return {"status": "error", "message": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    def sign_and_publish(self, content: str, kind: int = 1, tags: list = None) -> dict:
        """Create, sign, and publish a Nostr event."""
        if not self.private_key:
            return {"status": "error", "message": "No private key configured"}
        event = create_signed_event(self.private_key, content, kind, tags)
        result = self.publish(event)
        result["event_id"] = event["id"]
        return result
    
    # ── NIP-42 AUTH ──
    
    def get_auth_challenge(self) -> str | None:
        """Step 1: Request an auth challenge from relay."""
        try:
            payload = json.dumps({"method": "AUTH", "params": []})
            resp = requests.post(
                self.nostr_endpoint,
                data=payload,
                headers=self._headers(),
                timeout=15
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "challenge":
                    return data.get("challenge")
            return None
        except Exception:
            return None
    
    def sign_auth_event(self, challenge: str) -> dict | None:
        """
        Step 1.5: Create and sign a NIP-42 auth event (kind:22242).
        
        Returns signed event dict or None if no private key.
        """
        if not self.private_key:
            return None
        from nostr_protocol import Tag
        tags = [
            Tag.parse(["challenge", challenge]),
            Tag.parse(["relay", self.relay_url])
        ]
        return create_signed_event(self.private_key, "", kind=22242, tags=tags)
    
    def submit_auth(self, signed_event: dict) -> dict:
        """
        Step 2: Submit signed auth event to relay.
        
        Returns response dict with status, pubkey, and token on success.
        """
        payload = json.dumps({"method": "AUTH", "params": [signed_event]})
        try:
            resp = requests.post(
                self.nostr_endpoint,
                data=payload,
                headers=self._headers(),
                timeout=15
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "ok" and data.get("token"):
                    self._auth_token = data["token"]
                    self._auth_pubkey = data.get("pubkey")
                return data
            return {"status": "error", "message": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    def authenticate(self, nsec: str = None) -> dict:
        """
        Full NIP-42 AUTH flow: challenge → sign → submit.
        
        Args:
            nsec: optional nsec string (uses self.private_key if None)
        
        Returns:
            result dict with status, pubkey, token on success
        """
        if nsec:
            self.private_key = nsec_to_private_key(nsec)
            if self.private_key:
                self.pubkey_hex = PrivateKey(self.private_key).public_key.format().hex()[2:]
        
        if not self.private_key:
            return {"status": "error", "message": "No private key available"}
        
        # Step 1: get challenge
        challenge = self.get_auth_challenge()
        if not challenge:
            return {"status": "error", "message": "Failed to get auth challenge"}
        
        # Step 1.5: sign auth event
        signed = self.sign_auth_event(challenge)
        if not signed:
            return {"status": "error", "message": "Failed to sign auth event"}
        
        # Step 2: submit
        result = self.submit_auth(signed)
        return result

    # ── NIP-04 DM ──
    
    def encrypt_dm(self, recipient_pubkey_hex: str, message: str) -> str | None:
        """Encrypt a message for a recipient using NIP-04 (AES-256-CBC + ECDH)."""
        if not self.private_key:
            return None
        from nostr_protocol import nip04_encrypt, Keys, SecretKey, PublicKey

        sk = SecretKey.from_bytes(self.private_key)
        keys = Keys(sk)
        # Convert compressed pubkey (66 hex) to x-only (64 hex) if needed
        pk_hex = recipient_pubkey_hex
        if len(pk_hex) == 66:  # compressed key, strip the prefix byte
            pk_hex = pk_hex[2:]
        rpk = PublicKey.from_hex(pk_hex)
        return nip04_encrypt(keys.secret_key(), rpk, message)
    
    def decrypt_dm(self, encrypted_content: str, sender_pubkey_hex: str) -> str | None:
        """Decrypt a NIP-04 DM from a sender."""
        if not self.private_key:
            return None
        from nostr_protocol import nip04_decrypt, Keys, SecretKey, PublicKey

        sk = SecretKey.from_bytes(self.private_key)
        keys = Keys(sk)
        # Convert compressed pubkey (66 hex) to x-only (64 hex) if needed
        pk_hex = sender_pubkey_hex
        if len(pk_hex) == 66:  # compressed key, strip the prefix byte
            pk_hex = pk_hex[2:]
        spk = PublicKey.from_hex(pk_hex)
        return nip04_decrypt(keys.secret_key(), spk, encrypted_content)
    
    def send_dm(self, recipient_pubkey_hex: str, message: str) -> dict:
        """Encrypt and publish a NIP-04 DM (kind:4)."""
        if not self.private_key:
            return {"status": "error", "message": "No private key configured"}
        
        encrypted = self.encrypt_dm(recipient_pubkey_hex, message)
        if not encrypted:
            return {"status": "error", "message": "Encryption failed"}
        
        from nostr_protocol import Tag
        tags = [Tag.parse(["p", recipient_pubkey_hex])]
        event = create_signed_event(self.private_key, encrypted, kind=4, tags=tags)
        result = self.publish(event)
        result["event_id"] = event["id"]
        return result
    
    def fetch_inbox(self, limit: int = 20) -> list:
        """Fetch received DM events (kind:4 with our pubkey in #p tag)."""
        if not self.pubkey_hex:
            return []
        
        filters = {"kinds": [4], "#p": [self.pubkey_hex], "limit": limit}
        events = []
        for event, eose in self.subscribe(filters):
            if eose:
                break
            events.append(event)
            if len(events) >= limit:
                break
        return events


# ──────────────────────────────────────────────
# CLI Interface
# ──────────────────────────────────────────────

def print_event(event: NostrEvent, show_content: bool = True):
    """Pretty-print a Nostr event."""
    created = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(event.created_at))
    print(f"  ┌─ Kind:{event.kind}  ── {created}")
    print(f"  │ ID:   {event.id[:20]}...")
    print(f"  │ From: {event.pubkey[:20]}...")
    if show_content:
        content = event.content[:120].replace("\n", "\\n")
        print(f"  └─ {content}")
    print()


def cmd_gen_key():
    """Generate a new Nostr keypair."""
    kp = generate_keypair()
    print(f"nsec:       {kp['nsec']}")
    print(f"npub:       {kp['npub']}")
    print(f"privkey:    {kp['privkey_hex']}")
    print(f"pubkey:     {kp['pubkey_hex']}")
    print()
    print("Save your nsec securely! It cannot be recovered.")


def cmd_auth(relay_url: str, nsec: str = None):
    """Perform NIP-42 AUTH authentication."""
    if not nsec:
        print("Error: --auth requires --nsec <key>", file=sys.stderr)
        sys.exit(1)
    
    privkey = nsec_to_private_key(nsec)
    client = NostrSSEClient(relay_url, private_key=privkey)
    
    print(f"Authenticating with relay: {relay_url}")
    if client.npub:
        print(f"  As: {client.npub}")
    
    result = client.authenticate()
    if result.get("status") == "ok":
        print(f"  ✅ AUTH success!")
        print(f"  Pubkey: {result.get('pubkey', '?')[:20]}...")
        print(f"  Token:  {client._auth_token[:20]}...")
    else:
        print(f"  ❌ AUTH failed: {result.get('message', 'unknown error')}")
        sys.exit(1)


def cmd_send_dm(relay_url: str, nsec: str, recipient_pubkey: str, message: str):
    """Send an encrypted NIP-04 DM."""
    if not nsec:
        print("Error: --dm requires --nsec <key>", file=sys.stderr)
        sys.exit(1)

    privkey = nsec_to_private_key(nsec)
    client = NostrSSEClient(relay_url, private_key=privkey)

    if client.npub:
        print(f"From: {client.npub}")
    print(f"To:   {recipient_pubkey[:20]}...")
    print(f"Msg:  {message[:60]}{'...' if len(message) > 60 else ''}")
    print()

    result = client.send_dm(recipient_pubkey, message)
    if result.get("status") == "ok":
        print(f"  ✅ DM sent! event_id: {result.get('event_id', '?')[:20]}...")
    else:
        print(f"  ❌ Failed: {result.get('message', 'unknown error')}")
        sys.exit(1)


def cmd_inbox(relay_url: str, nsec: str, limit: int = 10):
    """Show received DMs."""
    if not nsec:
        print("Error: --inbox requires --nsec <key>", file=sys.stderr)
        sys.exit(1)

    privkey = nsec_to_private_key(nsec)
    client = NostrSSEClient(relay_url, private_key=privkey)

    if client.npub:
        print(f"Inbox for: {client.npub}")
    print(f"Relay: {relay_url}")
    print()

    events = client.fetch_inbox(limit)
    if not events:
        print("  📭 No DMs found")
        return

    print(f"  📨 {len(events)} DM(s)\n")
    for i, ev in enumerate(events, 1):
        decrypted = client.decrypt_dm(ev.content, ev.pubkey)
        created = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ev.created_at))
        print(f"  [{i}] From: {ev.pubkey[:20]}...")
        print(f"       At:  {created}")
        if decrypted:
            print(f"       📝: {decrypted[:200]}")
        else:
            print(f"       🔒 (cannot decrypt — wrong key)")
        print()


def cmd_subscribe(relay_url: str, filters: dict, nsec: str = None,
                   count: int = None, follow: bool = False):
    """Subscribe and print events."""
    privkey = None
    if nsec:
        privkey = nsec_to_private_key(nsec)
    
    client = NostrSSEClient(relay_url, private_key=privkey)
    
    if client.npub:
        print(f"Connected as: {client.npub[:20]}...")
    print(f"Relay: {relay_url}")
    print(f"Filters: {json.dumps(filters, ensure_ascii=False)}")
    if follow:
        print(f"Mode: LIVE (follow enabled, auto-reconnect on)")
    else:
        print(f"Listening... (Ctrl+C to stop)")
    print()
    
    received = 0
    try:
        for event, eose_flag in client.subscribe(filters, follow=follow):
            if eose_flag:
                if not follow:
                    print(f"  [EOSE] — end of stored events ({received} received)")
                    break
                else:
                    print(f"  [EOSE] — stored events done, waiting for live...")
                    continue
            
            print_event(event)
            received += 1
            if not follow and count and received >= count:
                break
    except KeyboardInterrupt:
        print("\n  Stopped.")


def cmd_publish(relay_url: str, content: str, nsec: str, kind: int = 1):
    """Publish an event to the relay."""
    if not nsec:
        print("Error: --nsec required for publishing", file=sys.stderr)
        return
    
    privkey = nsec_to_private_key(nsec)
    client = NostrSSEClient(relay_url, private_key=privkey)
    
    print(f"Relay: {relay_url}")
    print(f"As:    {client.npub[:20]}...")
    print(f"Kind:  {kind}")
    print(f"Content: {content[:100]}")
    print()
    
    result = client.sign_and_publish(content, kind)
    if result.get("status") == "ok":
        eid = result.get("event_id", "?")[:20]
        print(f"✅ Published! event_id: {eid}...")
    else:
        print(f"❌ Error: {result.get('message', 'unknown')}")


def cmd_info(relay_url: str):
    """Get relay info (NIP-11)."""
    try:
        resp = requests.get(
            relay_url,
            headers={"Accept": "application/nostr+json"},
            timeout=10
        )
        if resp.status_code == 200:
            info = resp.json()
            print(f"Name:    {info.get('name', '?')}")
            print(f"Version: {info.get('version', '?')}")
            print(f"NIPs:    {info.get('supported_nips', [])}")
            print(f"Events:  {info.get('event_count', '?')}")
            print(f"Authors: {info.get('authors_count', '?')}")
            print(f"Contact: {info.get('contact', '?')}")
        else:
            print(f"HTTP {resp.status_code}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)


def cmd_ipfs(relay_url: str):
    """Get IPFS stats from relay."""
    try:
        resp = requests.get(
            urljoin(relay_url, "/api/ipfs"),
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            print(f"IPFS Peers:      {data.get('peers', '?')}")
            print(f"Published:       {data.get('published', 0)}")
            print(f"Topic:           {data.get('topic', '?')}")
            print(f"CID Index:       {data.get('cid_index', {}).get('total', 0)} records")
            kinds = data.get('cid_index', {}).get('by_kind', {})
            for k, v in sorted(kinds.items(), key=lambda x: -x[1]):
                print(f"  kind:{k} → {v}")
        else:
            print(f"HTTP {resp.status_code}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)


def cmd_dashboard(relay_url: str):
    """Fetch and show relay dashboard."""
    try:
        resp = requests.get(
            relay_url,
            headers={"Accept": "text/html"},
            timeout=10
        )
        if resp.status_code == 200:
            html = resp.text
            # Extract cards
            import re
            cards = re.findall(r'<div class="card[^"]*"[^>]*><div class="value">([^<]+)</div><div class="label">([^<]+)</div></div>', html)
            if cards:
                print("Relay Status:")
                for val, label in cards:
                    print(f"  {label}: {val}")
            # Extract IPFS section
            ipfs_section = re.search(r'<h2>🪐 IPFS Pubsub \(K7\)</h2>(.*?)</div>', html, re.DOTALL)
            if ipfs_section:
                rows = re.findall(r'<span class="label">([^<]+)</span><span class="value"[^>]*>([^<]+)</span>', ipfs_section.group(1))
                if rows:
                    print("\nIPFS Pubsub:")
                    for label, val in rows:
                        print(f"  {label}: {val}")
        else:
            print(f"HTTP {resp.status_code}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Nostr SSE Client — подключение к relay через HTTP SSE",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python3 nostr_sse_client.py --relay https://relay-snin.v2.site --gen-key
  python3 nostr_sse_client.py --relay https://relay-snin.v2.site --subscribe '{"kinds":[1],"limit":5}'
  python3 nostr_sse_client.py --relay https://relay-snin.v2.site --nsec nsec1... --publish "Hello Nostr!"
  python3 nostr_sse_client.py --relay https://relay-snin.v2.site --info
  python3 nostr_sse_client.py --relay https://relay-snin.v2.site --dashboard
        """
    )
    parser.add_argument("--relay", "-r", default="https://relay-snin.v2.site",
                        help="Relay URL (default: https://relay-snin.v2.site)")
    parser.add_argument("--nsec", help="Private key in nsec format")
    parser.add_argument("--gen-key", action="store_true", help="Generate new Nostr keypair")
    parser.add_argument("--subscribe", "-s", help="Subscribe with JSON filter")
    parser.add_argument("--publish", "-p", help="Publish a text event")
    parser.add_argument("--kind", type=int, default=1, help="Event kind for publish (default: 1)")
    parser.add_argument("--count", "-n", type=int, help="Number of events to receive")
    parser.add_argument("--info", action="store_true", help="Get relay NIP-11 info")
    parser.add_argument("--dashboard", action="store_true", help="Show relay dashboard")
    parser.add_argument("--ipfs", action="store_true", help="Show IPFS stats")
    parser.add_argument("--follow", "-f", action="store_true",
                        help="Follow SSE stream continuously (no EOSE stop)")
    parser.add_argument("--auth", action="store_true",
                        help="Authenticate via NIP-42 AUTH (requires --nsec)")
    parser.add_argument("--dm", nargs=2, metavar=("PUBKEY", "MESSAGE"),
                        help="Send encrypted DM to a pubkey (requires --nsec)")
    parser.add_argument("--inbox", action="store_true",
                        help="Show received DMs (kind:4, requires --nsec)")
    parser.add_argument("--dm-limit", type=int, default=10,
                        help="Max DMs to show in --inbox (default: 10)")
    
    args = parser.parse_args()
    
    if args.gen_key:
        cmd_gen_key()
    elif args.info:
        cmd_info(args.relay)
    elif args.dashboard:
        cmd_dashboard(args.relay)
    elif args.ipfs:
        cmd_ipfs(args.relay)
    elif args.auth:
        cmd_auth(args.relay, args.nsec)
    elif args.dm:
        cmd_send_dm(args.relay, args.nsec, args.dm[0], args.dm[1])
    elif args.inbox:
        cmd_inbox(args.relay, args.nsec, args.dm_limit)
    elif args.subscribe:
        try:
            filters = json.loads(args.subscribe)
        except json.JSONDecodeError as e:
            print(f"Invalid JSON filter: {e}", file=sys.stderr)
            sys.exit(1)
        cmd_subscribe(args.relay, filters, args.nsec, args.count)
    elif args.publish:
        cmd_publish(args.relay, args.publish, args.nsec, args.kind)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
