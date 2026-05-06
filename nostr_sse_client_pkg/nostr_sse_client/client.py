#!/usr/bin/env python3
"""
NostrSSEClient — Nostr client over SSE (Server-Sent Events).

Replaces WebSocket with HTTP POST + SSE stream.
Works behind any HTTP proxy / ingress.
"""
import json, hashlib, time, threading
from typing import Optional, Callable
from dataclasses import dataclass
from urllib.parse import urljoin

import requests

from .utils import (
    create_signed_event, nsec_to_private_key,
    public_key_to_npub, generate_keypair,
)
from coincurve import PrivateKey


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
        self._subscriptions = {}
        self._auth_token: Optional[str] = None
        self._auth_pubkey: Optional[str] = None
        
        if private_key:
            self.pubkey_hex = PrivateKey(private_key).public_key.format().hex()[2:]
    
    @property
    def is_authenticated(self) -> bool:
        return self._auth_token is not None
    
    @property
    def npub(self) -> Optional[str]:
        if self.pubkey_hex:
            return public_key_to_npub(bytes.fromhex(self.pubkey_hex))
        return None
    
    def _headers(self) -> dict:
        """Get request headers, including auth token if available."""
        h = {"Content-Type": "application/json"}
        if self._auth_token:
            h["X-Auth-Token"] = self._auth_token
        return h
    
    # ── SSE Subscribe ──
    
    def subscribe(
        self,
        filters: dict,
        callback: Optional[Callable] = None,
        sub_id: Optional[str] = None,
        follow: bool = False,
        max_retries: int = 10,
        retry_delay: float = 3.0,
    ):
        """
        Subscribe to events via SSE.
        
        Args:
            filters: Nostr subscription filters dict
            callback: if provided, runs in background thread; if None, returns generator
            sub_id: optional subscription ID (auto-generated if None)
            follow: if True, keep SSE open after EOSE for live events
            max_retries: max reconnection attempts (follow mode)
            retry_delay: seconds between reconnection attempts
        
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
        """Run SSE subscription in a thread."""
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
                eose_received = False
                
                for line in response.iter_lines():
                    if not line:
                        continue
                    decoded = line.decode("utf-8", errors="replace")
                    
                    if decoded.startswith("data: "):
                        data_str = decoded[6:]
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        
                        if not isinstance(data, list) or len(data) < 2:
                            continue
                        
                        msg_type = data[0]
                        
                        if msg_type == "EVENT" and len(data) >= 3:
                            sub = data[1]
                            event_data = data[2]
                            event = NostrEvent.from_dict(event_data)
                            if sub == sub_id and sub in self._subscriptions:
                                self._subscriptions[sub](sub, event, error=None)
                        
                        elif msg_type == "EOSE" and data[1] == sub_id:
                            eose_received = True
                            if sub_id in self._subscriptions:
                                self._subscriptions[sub_id](sub_id, None, eose=True)
                            if not follow:
                                return
                            # After EOSE in follow mode: update filter with 'since'
                            current_filters = dict(filters)
                            current_filters["since"] = int(time.time())
                
                if follow and not eose_received:
                    continue
            
            except Exception:
                retries += 1
                if follow and retries < max_retries:
                    time.sleep(retry_delay * min(retries, 5))
                    continue
                return
    
    def _stream_events(self, sub_id: str, filters: dict, follow: bool = False,
                       max_retries: int = 10, retry_delay: float = 3.0):
        """Generator that yields (event, eose_flag) tuples."""
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
                    retries += 1
                    if follow and retries < max_retries:
                        time.sleep(retry_delay * min(retries, 5))
                        continue
                    return
                
                retries = 0
                eose_received = False
                
                for line in response.iter_lines():
                    if not line:
                        continue
                    decoded = line.decode("utf-8", errors="replace")
                    
                    if decoded.startswith("data: "):
                        data_str = decoded[6:]
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        
                        if not isinstance(data, list) or len(data) < 2:
                            continue
                        
                        msg_type = data[0]
                        
                        if msg_type == "EVENT" and len(data) >= 3:
                            event_data = data[2]
                            event = NostrEvent.from_dict(event_data)
                            yield (event, False)
                        
                        elif msg_type == "EOSE":
                            yield (None, True)
                            eose_received = True
                            if not follow:
                                return
                            current_filters = dict(filters)
                            current_filters["since"] = int(time.time())
                
                if follow and not eose_received:
                    continue
            
            except Exception:
                retries += 1
                if follow and retries < max_retries:
                    time.sleep(retry_delay * min(retries, 5))
                    continue
                return
    
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
        """Publish a Nostr event to the relay."""
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
    
    def get_auth_challenge(self) -> Optional[str]:
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
    
    def sign_auth_event(self, challenge: str) -> Optional[dict]:
        """Create and sign a NIP-42 auth event (kind:22242)."""
        if not self.private_key:
            return None
        from nostr_protocol import Tag
        tags = [
            Tag.parse(["challenge", challenge]),
            Tag.parse(["relay", self.relay_url])
        ]
        return create_signed_event(self.private_key, "", kind=22242, tags=tags)
    
    def submit_auth(self, signed_event: dict) -> dict:
        """Submit signed auth event to relay. Returns response with token."""
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
        """Full NIP-42 AUTH flow: challenge → sign → submit."""
        if nsec:
            self.private_key = nsec_to_private_key(nsec)
            if self.private_key:
                self.pubkey_hex = PrivateKey(self.private_key).public_key.format().hex()[2:]
        
        if not self.private_key:
            return {"status": "error", "message": "No private key available"}
        
        challenge = self.get_auth_challenge()
        if not challenge:
            return {"status": "error", "message": "Failed to get auth challenge"}
        
        signed = self.sign_auth_event(challenge)
        if not signed:
            return {"status": "error", "message": "Failed to sign auth event"}
        
        return self.submit_auth(signed)
    
    # ── NIP-04 DM ──
    
    def encrypt_dm(self, recipient_pubkey_hex: str, message: str) -> Optional[str]:
        """Encrypt a message for a recipient using NIP-04 (AES-256-CBC + ECDH)."""
        if not self.private_key:
            return None
        from nostr_protocol import nip04_encrypt, Keys, SecretKey, PublicKey
        
        sk = SecretKey.from_bytes(self.private_key)
        keys = Keys(sk)
        rpk = PublicKey.from_hex(recipient_pubkey_hex)
        return nip04_encrypt(keys.secret_key(), rpk, message)
    
    def decrypt_dm(self, encrypted_content: str, sender_pubkey_hex: str) -> Optional[str]:
        """Decrypt a NIP-04 DM from a sender."""
        if not self.private_key:
            return None
        from nostr_protocol import nip04_decrypt, Keys, SecretKey, PublicKey
        
        sk = SecretKey.from_bytes(self.private_key)
        keys = Keys(sk)
        spk = PublicKey.from_hex(sender_pubkey_hex)
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
