"""
SNIN Relay V2 — Auto-Fanout + Smart Mesh Write (Phase 3.5)
Fans out agent events to external relays.

- **Broadcast mode:** Events without #p tags → all alive relays (mass fanout)
- **Smart mode:** Events with #p tags → route to mentioned agents' relays
- **Mass fanout:** 520+ relays with batch processing, progress tracking, dead relay marking
"""

import asyncio
import aiohttp
import json
import logging
import os
import sqlite3
import time
from collections import deque
from typing import Callable, Optional

logger = logging.getLogger('fanout')

FANOUT_CONCURRENCY = 100      # concurrent WS connections
FANOUT_QUEUE_MAX = 1000
BATCH_SIZE = 50               # relays per batch (for progress logging)
TIMEOUT_PER_RELAY = 8         # seconds per relay WS connect+send

# DB for marking dead relays
DB_PATH = os.path.join(os.path.dirname(__file__), 'relay_v2.db')

# Agent pubkeys (unprefixed hex, for #p tag matching)
AGENT_PUBHEX = [
    "c460dc4698a7cef2be8d1b61e91a64067a7233f4ed81a94f1a14e340f05628bb",  # aiantology
    "86a1f42cf649830a1dd61dd4f5faf90a5c46384f407cf1a734187191014f4378",  # analyst
    # Seed relay pubkeys (example — replace with real relay pubkeys)
    "2047bfadceedeb9f15195c706d56a59ebe419212ffd8164aa367bf696f51fa69",  # aporia
    "ba66fbbf3eabd6330f0307e701bf7413716cb73280076a7aa6516a4bd3d6a843",  # archivist
    "c460dc4698a7cef2be8d1b61e91a64067a7233f4ed81a94f1a14e340f05628bb",  # cryptontology
    "a36a56b32054467ac6815b3ba6d84818c59c9dc97d174899b005d1f73ec118bf",  # cryter
    "f44e3a8683ac627b13e15abe9731859f30694dd4b4d730cb6c4318546c385c7a",  # director
    "67fb50e1139c62ad45f9e519eea7a19cbba4538f489d26b5646b451c5e65f12e",  # executor
    "6dcf915162d77891d06028de2ee10ce10e767d1acab412adaf3c2e2affd98e1c",  # forecaster
    "733080edaaed6b056fa7fbff73e5d43914c31f2845af25bff91f1969a2d52d9c",  # marketing
    "f8b54d33551f131540816bd77e580d62d889ade8240aa4e3afb35bee7fb6b716",  # randd
    "bd8979c65f3290f6790bf3a611fd5a0058bf42ef97b5ea281109312c71979835",  # security
    "24446e7c5b42c88fac01c83bcb2a8953ec9665e8835cc39af4303003841f2f68",  # strategist
    "8836071e3f9858d260cbe4247c5889f6fba9f9cb854eff88778c4a0dbb761169",  # support
]
# Cache for agent relay preferences: {pubkey_unprefixed: [relay_urls]}
_agent_relay_cache = {}
_cache_ts = 0


def _strip_prefix(pubkey: str) -> str:
    """Remove 02/03 prefix from compressed pubkey."""
    if pubkey.startswith(("02", "03")) and len(pubkey) == 66:
        return pubkey[2:]
    return pubkey


def _normalize_pubkey(pubkey: str) -> str:
    """Normalize to 64-char unprefixed hex."""
    return _strip_prefix(pubkey).lower()


def get_agent_relays(db, pubkey_unprefixed: str) -> list[str]:
    """Get preferred relays for an agent from source tags, kind:10002, or agent_relays table."""
    global _agent_relay_cache, _cache_ts
    
    # Cache bust every 60s
    now = time.time()
    if now - _cache_ts > 60:
        _agent_relay_cache.clear()
        _cache_ts = now
    
    if pubkey_unprefixed in _agent_relay_cache:
        return _agent_relay_cache[pubkey_unprefixed]
    
    def _db_conn(db):
        """Get sqlite3 connection from RelayDB object or raw connection."""
        return db._db if hasattr(db, '_db') else db
    
    def unprefixed_to_prefixed(hex64: str) -> list[str]:
        """Try 02 and 03 prefix."""
        return [prefix + hex64 for prefix in ("02", "03")]
    
    collected = set()
    
    try:
        # Method 1: source tags from stored events (mesh_fetch)
        for prefixed in unprefixed_to_prefixed(pubkey_unprefixed):
            cur = _db_conn(db).execute(
                """SELECT tags_json FROM events 
                   WHERE pubkey = ? AND kind = 1 AND tags_json LIKE '%"source"%"wss://%'
                   ORDER BY created_at DESC LIMIT 20""",
                (prefixed,)
            )
            rows = cur.fetchall()
            for row in rows:
                try:
                    tags = json.loads(row[0])
                    for tag in tags:
                        if len(tag) >= 2 and tag[0] == "source" and tag[1].startswith("wss://"):
                            collected.add(tag[1])
                except (json.JSONDecodeError, IndexError):
                    continue
        
        # Method 2: kind:10002 relay list metadata
        for prefixed in unprefixed_to_prefixed(pubkey_unprefixed):
            cur = _db_conn(db).execute(
                """SELECT tags_json FROM events 
                   WHERE pubkey = ? AND kind = 10002 
                   ORDER BY created_at DESC LIMIT 1""",
                (prefixed,)
            )
            row = cur.fetchone()
            if row:
                tags = json.loads(row[0])
                for tag in tags:
                    if len(tag) >= 2 and tag[0] == "r" and tag[1].startswith("wss://"):
                        collected.add(tag[1])
        
        # Method 3: agent_relays table (seeded preferences)
        if not collected:
            for prefixed in unprefixed_to_prefixed(pubkey_unprefixed):
                cur = _db_conn(db).execute(
                    "SELECT relays FROM agent_relays WHERE pubkey = ?",
                    (prefixed,)
                )
                row = cur.fetchone()
                if row:
                    for r in json.loads(row[0]):
                        collected.add(r)
        
        result = list(collected)
        _agent_relay_cache[pubkey_unprefixed] = result
        logger.debug(f"Agent relays for {pubkey_unprefixed[:12]}: {len(result)} relays")
        return result
    except Exception as e:
        logger.debug(f"Error fetching agent relays for {pubkey_unprefixed[:12]}: {e}")
        return []


def get_target_relays(event: dict, db, alive_relays: list[str]) -> list[str]:
    """
    Smart routing: determine which relays an event should be fanned out to.
    
    If event has #p tags mentioning known agents → route to those agents' relays.
    Otherwise → send to all alive relays (broadcast).
    """
    tags = event.get("tags", [])
    p_tags = [t[1] for t in tags if len(t) >= 2 and t[0] == "p"]
    
    if not p_tags:
        return alive_relays  # broadcast
    
    # Find which mentioned pubkeys are our agents
    target_relays = set()
    for pkey in p_tags:
        normalized = _normalize_pubkey(pkey)
        if normalized in AGENT_PUBHEX:
            agent_relays = get_agent_relays(db, normalized)
            for r in agent_relays:
                if r in alive_relays:
                    target_relays.add(r)
    
    if target_relays:
        return list(target_relays)
    
    # Mentioned agents have no preferred relays → broadcast
    return alive_relays


class Fanout:
    """Auto-Fanout engine with Smart Mesh Write + Mass Fanout (520+ relays)."""

    def __init__(self, get_alive_relays_fn: Callable[[], list[str]], db=None):
        self._get_alive = get_alive_relays_fn
        self._db = db
        self._queue: deque = deque(maxlen=FANOUT_QUEUE_MAX)
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._stats = {
            "enqueued": 0,
            "published": 0,
            "smart_routed": 0,
            "broadcast": 0,
            "failed_relay_conns": 0,
            "total_relays_hit": 0,
            "last_event_at": 0,
            "saved_relay_calls": 0,
            "dead_marked": 0,
        }

    def enqueue(self, event: dict):
        """Add event to fanout queue."""
        self._queue.append(event)
        self._stats["enqueued"] += 1
        self._stats["last_event_at"] = int(time.time())
        logger.debug(f"Fanout queued: {event.get('id','')[:12]}... (queue={len(self._queue)})")

    def start(self):
        """Start background fanout worker."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._worker())
        logger.info("Fanout worker started")

    async def stop(self):
        """Stop background worker."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Fanout worker stopped")

    def get_stats(self) -> dict:
        s = dict(self._stats)
        s["queue_size"] = len(self._queue)
        s["running"] = self._running
        return s

    async def _worker(self):
        """Background loop: process queued events."""
        while self._running:
            try:
                if not self._queue:
                    await asyncio.sleep(1)
                    continue

                event = self._queue.popleft()
                await self._publish(event)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Fanout worker error: {e}")
                await asyncio.sleep(1)

    async def _publish(self, event: dict):
        """Mass publish event to all alive relays with batch processing."""
        alive = self._get_alive()
        if not alive:
            logger.warning("No alive relays to fanout to")
            return

        # Smart routing: determine target relays
        target_relays = get_target_relays(event, self._db, alive)
        
        is_smart = len(target_relays) < len(alive)
        if is_smart:
            self._stats["smart_routed"] += 1
            saved = len(alive) - len(target_relays)
            self._stats["saved_relay_calls"] += saved
        else:
            self._stats["broadcast"] += 1

        if not target_relays:
            logger.debug(f"Fanout {event.get('id','')[:12]}: no target relays")
            return

        total = len(target_relays)
        msg = json.dumps(["EVENT", event])
        sent = 0
        failed = 0
        dead_relays = []
        
        # Shared session with connection pool
        conn = aiohttp.TCPConnector(limit=FANOUT_CONCURRENCY, limit_per_host=2)
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_PER_RELAY)
        
        eid = event.get('id', '')[:12]
        route_type = "🔀 smart" if is_smart else "📡 mass"
        logger.info(f"Fanout {eid} [{route_type}]: publishing to {total} relays")
        
        async with aiohttp.ClientSession(connector=conn, timeout=timeout) as session:
            for i in range(0, total, BATCH_SIZE):
                batch = target_relays[i:i + BATCH_SIZE]
                tasks = [self._publish_one(session, url, msg) for url in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                for url, result in zip(batch, results):
                    if result is True:
                        sent += 1
                    else:
                        failed += 1
                        if isinstance(result, str) and result:
                            dead_relays.append(url)
                
                # Progress log every batch
                batch_num = i // BATCH_SIZE + 1
                total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
                pct = (i + len(batch)) * 100 // total
                bar = "▓" * (pct // 10) + "░" * (10 - pct // 10)
                logger.info(
                    f"  [{bar}] {pct}% — batch {batch_num}/{total_batches}: "
                    f"{sent} OK / {failed} failed"
                )
                
                await asyncio.sleep(0.05)  # slight pause between batches
        
        # Mark dead relays in DB
        if dead_relays:
            self._mark_dead(dead_relays)
            self._stats["dead_marked"] += len(dead_relays)

        self._stats["published"] += 1
        self._stats["failed_relay_conns"] += failed
        self._stats["total_relays_hit"] += sent

        success_rate = f"{sent/total*100:.1f}%" if total else "0%"
        logger.info(
            f"✅ Fanout {eid} [{route_type}]: {sent}/{total} → {success_rate} "
            f"(dead marked: {len(dead_relays)})"
        )

    async def _publish_one(self, session: aiohttp.ClientSession, url: str, msg: str):
        """Send to one relay via WebSocket. Returns True/False/error_string."""
        try:
            async with session.ws_connect(url, heartbeat=30, max_msg_size=0) as ws:
                await ws.send_str(msg)
                try:
                    resp = await asyncio.wait_for(ws.receive(), timeout=5)
                    if resp.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(resp.data)
                        return data[0] == "OK"
                except (asyncio.TimeoutError, Exception):
                    pass
                return True  # connected = accepted
        except Exception as e:
            err = str(e)[:80]
            if "404" in err or "403" in err or "rejected" in err.lower():
                return err  # permanent failure → mark dead
            return False

    def _mark_dead(self, dead_relays: list[str]):
        """Mark relays as dead in mass_pulse DB."""
        try:
            conn = sqlite3.connect(DB_PATH)
            for url in dead_relays:
                conn.execute(
                    "UPDATE all_relays SET fail_count = fail_count + 1 WHERE url = ?",
                    (url,)
                )
            conn.commit()
            conn.close()
            logger.info(f"Marked {len(dead_relays)} relays as failed (+1 fail_count)")
        except Exception as e:
            logger.warning(f"Failed to mark dead relays: {e}")


CRYTER_PUBKEY = "028ae7965af1b61347bb9900b91cfa9487e4da2400bdb063521ad0850706ff5f96"
