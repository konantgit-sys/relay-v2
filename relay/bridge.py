#!/usr/bin/env python3
"""bridge.py — Мост relay-snin → relay-mesh.

Не изменяет relay-snin. Read-only к SQLite.
Форвардит новые события (kind:8010, kind:1, kind:39000...)
в relay-mesh через POST /api/ingest.
"""
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import asyncio

# ─── Config ───────────────────────────────────────────────────────────────
DB_PATH = Path(os.getenv("BRIDGE_DB_PATH", "/home/agent/data/sites/relay/relay_v2.db"))
RELAY_MESH_URL = os.getenv("BRIDGE_MESH_URL", "http://localhost:9907")
POLL_INTERVAL = float(os.getenv("BRIDGE_POLL", "3.0"))  # seconds

# Какие kinds форвардить
FORWARD_KINDS = {1, 11, 8010, 8011, 8012, 8013, 8014, 8015, 8016, 8017, 
                 19000, 39000, 39001, 39002, 39003}

# state — последний форварднутый event.id
STATE_FILE = Path(__file__).parent / ".bridge_state.json"

# ─── Logger ───────────────────────────────────────────────────────────────
import logging
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [BRIDGE] %(message)s")
log = logging.getLogger("bridge")


def load_state() -> str:
    """Загрузить last_id из файла состояния."""
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            return data.get("last_id", "")
        except Exception:
            return ""
    return ""


def save_state(last_id: str):
    """Сохранить last_id."""
    STATE_FILE.write_text(json.dumps({
        "last_id": last_id,
        "updated_at": time.time(),
    }))


def query_new_events(db_path: Path, last_id: str, limit: int = 50) -> list[dict]:
    """Выбрать новые события из SQLite (id > last_id)."""
    if not db_path.exists():
        return []
    
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    
    if last_id:
        cur.execute("""
            SELECT id, pubkey, created_at, kind, content, sig, received_at
            FROM events
            WHERE rowid > (SELECT rowid FROM events WHERE id = ?)
              AND kind IN ({})
            ORDER BY rowid ASC
            LIMIT ?
        """.format(",".join(str(k) for k in FORWARD_KINDS)),
                     (last_id, limit))
    else:
        cur.execute("""
            SELECT id, pubkey, created_at, kind, content, sig, received_at
            FROM events
            WHERE kind IN ({})
            ORDER BY rowid ASC
            LIMIT ?
        """.format(",".join(str(k) for k in FORWARD_KINDS)),
                     (limit,))
    
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


async def forward_event(session: aiohttp.ClientSession, event: dict) -> bool:
    """Форварднуть одно событие в relay-mesh /api/ingest."""
    payload = {
        "kind": event["kind"],
        "pubkey": event["pubkey"],
        "content": event["content"],
        "created_at": event["created_at"],
        "id": event["id"],
        "sig": event["sig"],
    }
    
    try:
        async with session.post(
            f"{RELAY_MESH_URL}/api/ingest",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.ok:
                return True
            else:
                body = await resp.text()
                log.warning(f"forward error {resp.status}: {body[:100]}")
                return False
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning(f"forward exception: {e}")
        return False


async def bridge_loop():
    """Основной цикл: polling SQLite → forward в relay-mesh."""
    log.info(f"Bridge started. DB: {DB_PATH}, Mesh: {RELAY_MESH_URL}")
    log.info(f"Forward kinds: {sorted(FORWARD_KINDS)}")
    
    last_id = load_state()
    log.info(f"Resumed from last_id: {last_id[:24] if last_id else '(none)'}")
    
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                events = query_new_events(DB_PATH, last_id)
                
                for event in events:
                    ok = await forward_event(session, event)
                    if ok:
                        last_id = event["id"]
                        kind = event["kind"]
                        log.debug(f"forwarded kind:{kind} {event['id'][:16]}...")
                    else:
                        # Если не получилось — пробуем позже
                        break
                
                if events:
                    save_state(last_id)
                    log.info(f"Forwarded {len(events)} events, last_id: {last_id[:16]}...")
                
            except Exception as e:
                log.error(f"bridge error: {e}")
            
            await asyncio.sleep(POLL_INTERVAL)


def main():
    asyncio.run(bridge_loop())


if __name__ == "__main__":
    main()
