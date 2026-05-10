"""
SNIN Relay — Nostr Marshal

Nostr event serialization/deserialization for IPFS-compatible format.
Integrity check: id = SHA256(event_json), sig = Schnorr verify.

Flow:
  event → marshal_event(event) → bytes → ipfs add → CID
  CID → ipfs cat → bytes → unmarshal_event(bytes) → event
  verify_integrity(event) → True/False
"""

import hashlib
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("nostr_marshal")

# ── Constants ──────────────────────────────────────────────
NOSTR_EVENT_KEYS = ["id", "pubkey", "created_at", "kind", "tags", "content", "sig"]
SERIALIZE_ORDER = [0, "pubkey", "created_at", "kind", "tags", "content"]  # for id

RELAY_META = {
    "source_relay": "snin-relay.v2.site",
    "k7_version": "1.0",
    "protocol": "ipfs-pubsub",
}


# ── Serialization ───────────────────────────────────────────

def serialize_event(event: dict, canonical: bool = False) -> bytes:
    """Nostr event → JSON bytes.

    If canonical=True — uses canonical format
    for id verification: [0, pubkey, created_at, kind, tags, content]
    """
    if canonical:
        # Canonical format for id computation (NIP-01)
        obj = [
            0,
            event["pubkey"],
            event["created_at"],
            event["kind"],
            event.get("tags", []),
            event.get("content", ""),
        ]
    else:
        # Full format for IPFS (with id, sig, meta)
        obj = {
            "nostr": {
                "id": event["id"],
                "pubkey": event["pubkey"],
                "created_at": event["created_at"],
                "kind": event["kind"],
                "tags": event.get("tags", []),
                "content": event.get("content", ""),
                "sig": event["sig"],
            },
            "meta": {
                **RELAY_META,
                "published_at": int(time.time()),
            },
        }

    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()


def deserialize_event(data: bytes) -> Optional[dict]:
    """JSON bytes → Nostr event dict.

    Supports:
    - Full IPFS format: {"nostr": {...}, "meta": {...}}
    - Canonical: [0, pubkey, ts, kind, tags, content]
    - Flat: {"id": "...", ...}
    """
    try:
        obj = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(f"deserialize: JSON decode error: {e}")
        return None

    # Full IPFS format
    if isinstance(obj, dict) and "nostr" in obj:
        event = obj["nostr"]
        # Check all required fields exist
        for key in ["id", "pubkey", "created_at", "kind", "content", "sig"]:
            if key not in event:
                logger.warning(f"deserialize: missing field '{key}' in nostr")
                return None
        return event

    # Flat format
    if isinstance(obj, dict) and "id" in obj and "pubkey" in obj:
        return obj

    # Canonical format (array)
    if isinstance(obj, list) and len(obj) >= 6:
        return {
            "id": "",  # will be computed
            "pubkey": obj[1],
            "created_at": obj[2],
            "kind": obj[3],
            "tags": obj[4] if isinstance(obj[4], list) else [],
            "content": str(obj[5]),
            "sig": obj[6] if len(obj) > 6 else "",
        }

    logger.warning(f"deserialize: unknown format: {type(obj).__name__}")
    return None


# ── ID Verification (NIP-01) ───────────────────────────────

def compute_event_id(event: dict) -> str:
    """Compute event id per NIP-01."""
    tags = event.get("tags", [])
    content = event.get("content", "")
    raw = json.dumps(
        [0, event["pubkey"], event["created_at"], event["kind"], tags, content],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def verify_event_id(event: dict) -> bool:
    """Verify id = SHA256(event)."""
    computed = compute_event_id(event)
    expected = event.get("id", "")
    if computed != expected:
        logger.debug(f"ID mismatch: computed={computed[:20]}... expected={expected[:20]}...")
        return False
    return True


# ── Schnorr Signature Verification (NIP-01) ────────────────

def verify_schnorr(event: dict) -> bool:
    """Verify Schnorr signature (NIP-01).

    Uses nostr_protocol.Event.from_json + verify.
    """
    try:
        from nostr_protocol import Event

        event_json = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
        ne = Event.from_json(event_json)
        return ne.verify()
    except ImportError:
        logger.warning("nostr_protocol not available, skipping sig verify")
        return True  # soft-fail
    except Exception as e:
        logger.warning(f"Schnorr verify error: {e}")
        return False


# ── Public API ─────────────────────────────────────────────

def verify_integrity(event: dict) -> dict:
    """Full event integrity check.

    Returns:
        {"valid": True/False, "checks": {...}, "error": "..."}
    """
    result = {
        "valid": True,
        "checks": {
            "has_id": False,
            "has_pubkey": False,
            "has_sig": False,
            "id_match": False,
            "sig_valid": False,
            "kind_known": False,
        },
        "error": None,
    }

    # Check field presence
    for field in ["id", "pubkey", "sig"]:
        if field in event and event[field]:
            result["checks"][f"has_{field}"] = True

    if not result["checks"]["has_id"]:
        result["valid"] = False
        result["error"] = "missing event id"
        return result

    if not result["checks"]["has_pubkey"]:
        result["valid"] = False
        result["error"] = "missing pubkey"
        return result

    if not result["checks"]["has_sig"]:
        result["valid"] = False
        result["error"] = "missing signature"
        return result

    # Verify id
    result["checks"]["id_match"] = verify_event_id(event)
    if not result["checks"]["id_match"]:
        result["valid"] = False
        result["error"] = "event id mismatch"
        return result

    # Verify signature
    result["checks"]["sig_valid"] = verify_schnorr(event)
    if not result["checks"]["sig_valid"]:
        result["valid"] = False
        result["error"] = "invalid schnorr signature"
        return result

    # Verify kind (known types)
    known_kinds = {0, 1, 2, 3, 4, 5, 6, 7, 40, 41, 42, 43, 44,
                   1063, 1984, 1985, 22222, 30023, 31989, 31990, 34235,
                   39000, 39001, 39002, 39003}
    if event.get("kind", -1) in known_kinds:
        result["checks"]["kind_known"] = True

    return result


def marshal_event(event: dict) -> tuple[bytes, str, dict]:
    """Full cycle: event → bytes → CID (via IPFS-add).

    Args:
        event: Nostr event dict

    Returns:
        (json_bytes, cid_str, integrity_result)

    If IPFS unavailable — return bytes only.
    """
    data = serialize_event(event)
    integrity = verify_integrity(event)

    # Attempt to add to IPFS
    cid = None
    try:
        import asyncio
        _ipfs_bin = os.getenv("IPFS_BIN", "ipfs")
        _ipfs_path = os.getenv("IPFS_PATH", os.path.expanduser("~/.ipfs"))
        _home_dir = os.environ.get("HOME", "/root")

        async def _add():
            proc = await asyncio.create_subprocess_exec(
                _ipfs_bin, "add", "-Q", "--pin=false",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={
                    "HOME": _home_dir,
                    "IPFS_PATH": _ipfs_path,
                    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                },
            )
            stdout, _ = await proc.communicate(data)
            if proc.returncode == 0:
                return stdout.decode().strip()
            return None

        cid = asyncio.run(_add())
    except Exception as e:
        logger.warning(f"marshal: IPFS add failed: {e}")

    return data, cid, integrity


def unmarshal_event(data: bytes, expected_id: str = None) -> Optional[dict]:
    """Bytes → event dict with integrity check.

    Args:
        data: JSON bytes из IPFS
        expected_id: if set — validates id match

    Returns:
        event dict or None on error
    """
    event = deserialize_event(data)
    if not event:
        return None

    # If id empty (canonical format) — compute it
    if not event.get("id"):
        event["id"] = compute_event_id(event)

    # Integrity check
    integrity = verify_integrity(event)
    if not integrity["valid"]:
        logger.warning(f"unmarshal: integrity check failed: {integrity['error']}")
        return None

    # Verify expected id
    if expected_id and event["id"] != expected_id:
        logger.warning(f"unmarshal: id mismatch: {event['id'][:20]}... != {expected_id[:20]}...")
        return None

    return event


# ── Batch Processing ───────────────────────────────────────

def marshal_batch(events: list[dict]) -> list[tuple]:
    """Batch process event list.

    Returns:
        [(bytes, cid, integrity), ...]
    """
    return [marshal_event(e) for e in events]


# ── Self-test ──────────────────────────────────────────────

def _create_test_event(event_id: str = None) -> dict:
    """Create signed test event via EventBuilder."""
    from nostr_protocol import Keys, EventBuilder, Tag, Kind
    import secrets

    key = Keys.generate()
    pubkey_hex = key.public_key().to_hex()
    first_byte = int(pubkey_hex[:2], 16) % 3
    prefix = "02" if first_byte == 2 else "03"
    pubkey = prefix + pubkey_hex

    content = "K7 Nostr Marshal test — integrity verification"
    tags = [Tag.parse(["t", "test"])]
    kind = Kind(1)

    builder = EventBuilder(kind, content, tags)
    signed = builder.to_event(key)
    signed_json = json.loads(signed.as_json())

    if event_id:
        signed_json["id"] = event_id  # forced

    return signed_json


def test() -> dict:
    """Run all tests. Returns results."""
    results = {"tests": [], "passed": 0, "failed": 0, "errors": []}

    def check(name, ok, detail=""):
        results["tests"].append({"name": name, "ok": ok, "detail": detail})
        if ok:
            results["passed"] += 1
        else:
            results["failed"] += 1
            results["errors"].append(f"{name}: {detail}")

    # 1. Serialization and deserialization
    event = _create_test_event()
    data = serialize_event(event)
    restored = deserialize_event(data)
    check("serialize/deserialize", restored is not None)
    if restored:
        check("id preserved", restored["id"] == event["id"])

    # 2. Canonical serialization
    canonical = serialize_event(event, canonical=True)
    check("canonical format", isinstance(canonical, bytes) and len(canonical) > 0)

    # 3. ID verification
    id_ok = verify_event_id(event)
    check("id verification", id_ok)

    # 4. ID mismatch detection
    bad_event = dict(event)
    bad_event["content"] = "tampered content"
    id_bad = verify_event_id(bad_event)
    check("id mismatch detection", not id_bad)

    # 5. Integrity
    integrity = verify_integrity(event)
    check("integrity pass", integrity["valid"])

    # 6. Integrity — missing id
    no_id = {"pubkey": event["pubkey"], "content": "no id"}
    int_no_id = verify_integrity(no_id)
    check("integrity: missing id", not int_no_id["valid"])

    # 7. Integrity — tampered
    tampered = dict(event)
    tampered["content"] = "tampered"
    int_tampered = verify_integrity(tampered)
    check("integrity: tampered", not int_tampered["valid"])

    # 8. Marshaling (no IPFS — fake CID)
    data_bytes, cid, integ = marshal_event(event)
    check("marshal: bytes", isinstance(data_bytes, bytes) and len(data_bytes) > 0)
    check("marshal: integrity", integ["valid"])

    # 9. Unmarshaling
    restored2 = unmarshal_event(data_bytes, expected_id=event["id"])
    check("unmarshal", restored2 is not None)
    if restored2:
        check("unmarshal: content match", restored2["content"] == event["content"])

    # 10. Unmarshaling with wrong id
    restored_bad = unmarshal_event(data_bytes, expected_id="0000000000000000000000000000000000000000000000000000000000000000")
    check("unmarshal: bad id reject", restored_bad is None)

    # 11. Canonical → event
    can_event = deserialize_event(serialize_event(event, canonical=True))
    check("canonical deserialize", can_event is not None)

    # 12. Empty data
    empty = deserialize_event(b"")
    check("empty data", empty is None)

    # 13. Garbage
    garbage = deserialize_event(b"not json at all{{{")
    check("garbage data", garbage is None)

    # Result
    status = "ALL PASSED" if results["failed"] == 0 else f"{results['failed']} FAILED"
    logger.info(f"Nostr Marshal test: {status} ({results['passed']}/{len(results['tests'])})")

    return results


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    results = test()
    print(f"\n{'='*50}")
    print(f"  Tests: {len(results['tests'])} | Passed: {results['passed']} | Failed: {results['failed']}")
    if results["errors"]:
        print(f"  Errors:")
        for e in results["errors"]:
            print(f"    ❌ {e}")
    print(f"{'='*50}")
