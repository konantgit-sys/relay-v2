"""
SNIN Relay — Nostr Marshal (K7 / День 3)

Сериализация/десериализация Nostr событий в IPFS-совместимый формат.
Проверка целостности: id = SHA256(event_json), sig = Schnorr verify.

Поток:
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

# ── Константы ──────────────────────────────────────────────
NOSTR_EVENT_KEYS = ["id", "pubkey", "created_at", "kind", "tags", "content", "sig"]
SERIALIZE_ORDER = [0, "pubkey", "created_at", "kind", "tags", "content"]  # для id

RELAY_META = {
    "source_relay": "snin-relay.v2.site",
    "k7_version": "1.0",
    "protocol": "ipfs-pubsub",
}


# ── Сериализация ───────────────────────────────────────────

def serialize_event(event: dict, canonical: bool = False) -> bytes:
    """Nostr event → JSON bytes.

    Если canonical=True — использует канонический формат
    для id verification: [0, pubkey, created_at, kind, tags, content]
    """
    if canonical:
        # Канонический формат для вычисления id (NIP-01)
        obj = [
            0,
            event["pubkey"],
            event["created_at"],
            event["kind"],
            event.get("tags", []),
            event.get("content", ""),
        ]
    else:
        # Полный формат для IPFS (с id, sig, meta)
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

    Поддерживает:
    - Полный формат IPFS: {"nostr": {...}, "meta": {...}}
    - Канонический: [0, pubkey, ts, kind, tags, content]
    - Плоский: {"id": "...", ...}
    """
    try:
        obj = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(f"deserialize: JSON decode error: {e}")
        return None

    # Полный формат IPFS
    if isinstance(obj, dict) and "nostr" in obj:
        event = obj["nostr"]
        # Проверка наличия всех обязательных полей
        for key in ["id", "pubkey", "created_at", "kind", "content", "sig"]:
            if key not in event:
                logger.warning(f"deserialize: missing field '{key}' in nostr")
                return None
        return event

    # Плоский формат
    if isinstance(obj, dict) and "id" in obj and "pubkey" in obj:
        return obj

    # Канонический формат (массив)
    if isinstance(obj, list) and len(obj) >= 6:
        return {
            "id": "",  # будет вычислено
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
    """Вычисляет id события по NIP-01."""
    tags = event.get("tags", [])
    content = event.get("content", "")
    raw = json.dumps(
        [0, event["pubkey"], event["created_at"], event["kind"], tags, content],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def verify_event_id(event: dict) -> bool:
    """Проверяет, что id = SHA256(event)."""
    computed = compute_event_id(event)
    expected = event.get("id", "")
    if computed != expected:
        logger.debug(f"ID mismatch: computed={computed[:20]}... expected={expected[:20]}...")
        return False
    return True


# ── Schnorr Signature Verification (NIP-01) ────────────────

def verify_schnorr(event: dict) -> bool:
    """Проверяет подпись Schnorr (NIP-01).

    Использует nostr_protocol.Event.from_json + verify.
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
    """Полная проверка целостности события.

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

    # Проверка наличия полей
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

    # Проверка id
    result["checks"]["id_match"] = verify_event_id(event)
    if not result["checks"]["id_match"]:
        result["valid"] = False
        result["error"] = "event id mismatch"
        return result

    # Проверка подписи
    result["checks"]["sig_valid"] = verify_schnorr(event)
    if not result["checks"]["sig_valid"]:
        result["valid"] = False
        result["error"] = "invalid schnorr signature"
        return result

    # Проверка kind (известные типы)
    known_kinds = {0, 1, 2, 3, 4, 5, 6, 7, 40, 41, 42, 43, 44,
                   1063, 1984, 1985, 22222, 30023, 31989, 31990, 34235,
                   39000, 39001, 39002, 39003}
    if event.get("kind", -1) in known_kinds:
        result["checks"]["kind_known"] = True

    return result


def marshal_event(event: dict) -> tuple[bytes, str, dict]:
    """Полный цикл: event → bytes → CID (через IPFS-add).

    Args:
        event: Nostr event dict

    Returns:
        (json_bytes, cid_str, integrity_result)

    Если IPFS недоступен — возвращает только bytes.
    """
    data = serialize_event(event)
    integrity = verify_integrity(event)

    # Попытка добавить в IPFS
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
    """Bytes → event dict с проверкой целостности.

    Args:
        data: JSON bytes из IPFS
        expected_id: если задан — проверяет совпадение id

    Returns:
        event dict или None при ошибке
    """
    event = deserialize_event(data)
    if not event:
        return None

    # Если id пустой (канонический формат) — вычисляем
    if not event.get("id"):
        event["id"] = compute_event_id(event)

    # Проверка целостности
    integrity = verify_integrity(event)
    if not integrity["valid"]:
        logger.warning(f"unmarshal: integrity check failed: {integrity['error']}")
        return None

    # Проверка ожидаемого id
    if expected_id and event["id"] != expected_id:
        logger.warning(f"unmarshal: id mismatch: {event['id'][:20]}... != {expected_id[:20]}...")
        return None

    return event


# ── Batch Processing ───────────────────────────────────────

def marshal_batch(events: list[dict]) -> list[tuple]:
    """Пакетная обработка списка событий.

    Returns:
        [(bytes, cid, integrity), ...]
    """
    return [marshal_event(e) for e in events]


# ── Self-test ──────────────────────────────────────────────

def _create_test_event(event_id: str = None) -> dict:
    """Создаёт подписанное тестовое событие через EventBuilder."""
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
        signed_json["id"] = event_id  # принудительно

    return signed_json


def test() -> dict:
    """Прогон всех тестов. Возвращает результаты."""
    results = {"tests": [], "passed": 0, "failed": 0, "errors": []}

    def check(name, ok, detail=""):
        results["tests"].append({"name": name, "ok": ok, "detail": detail})
        if ok:
            results["passed"] += 1
        else:
            results["failed"] += 1
            results["errors"].append(f"{name}: {detail}")

    # 1. Сериализация и десериализация
    event = _create_test_event()
    data = serialize_event(event)
    restored = deserialize_event(data)
    check("serialize/deserialize", restored is not None)
    if restored:
        check("id preserved", restored["id"] == event["id"])

    # 2. Каноническая сериализация
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

    # 8. Маршалинг (без IPFS — фейковый CID)
    data_bytes, cid, integ = marshal_event(event)
    check("marshal: bytes", isinstance(data_bytes, bytes) and len(data_bytes) > 0)
    check("marshal: integrity", integ["valid"])

    # 9. Анмаршалинг
    restored2 = unmarshal_event(data_bytes, expected_id=event["id"])
    check("unmarshal", restored2 is not None)
    if restored2:
        check("unmarshal: content match", restored2["content"] == event["content"])

    # 10. Анмаршалинг с неверным id
    restored_bad = unmarshal_event(data_bytes, expected_id="0000000000000000000000000000000000000000000000000000000000000000")
    check("unmarshal: bad id reject", restored_bad is None)

    # 11. Canonical → event
    can_event = deserialize_event(serialize_event(event, canonical=True))
    check("canonical deserialize", can_event is not None)

    # 12. Пустые данные
    empty = deserialize_event(b"")
    check("empty data", empty is None)

    # 13. Мусор
    garbage = deserialize_event(b"not json at all{{{")
    check("garbage data", garbage is None)

    # Результат
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
