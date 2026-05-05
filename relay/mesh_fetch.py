"""
SNIN Agent Mesh — Read Mesh
Fetches events from external relays for whitelisted SNIN agents.
Extends PulseSync with event ingestion.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger('mesh')

# ── Agent pubkeys (Nostr 32-byte hex, no 02/03 prefix) ──
# Derived from whitelist in relay_server_v2.py
AGENT_PUBKEYS = [
    "147d6c45f5d12d5710c7d1571e6ff12df515bbe40d7640c957a2d3b932162790",  # aiantology
    "a3dfff689567d8402771146c69e653829f245777de78f90e8becc5f198d245a4",  # analyst
    "64f0bf504202aee3810952108b3ddad9e4e3285b73d6dda7a1855c98dc96ab5a",  # anton
    "e7c578c86f0a3a535d334a1f7b85220871168eda420855c4f02cc1d405354498",  # aporia
    "98185a965fde2a8520debe3c498118815612daa2e609f95c74636c12fdc7a712",  # archivist
    "a0542326be9b89ad9aec6d37290855ed50261e0bb23484c3887f621a17ea0b8b",  # cryptontology
    "8ae7965af1b61347bb9900b91cfa9487e4da2400bdb063521ad0850706ff5f96",  # cryter
    "058b28ffa66d3fa23a589bcbb00a15e57dc736eaf073ae918b1ec820537ac26a",  # director
    "92ed16c86de8b918beb14b7de963c982e65e9686c6d60fd2b8c73e13e7d19aa0",  # executor
    "930971ce14f14c027d633335ed535c565c0464e6c2f4d8b05ff8361f37d92f69",  # forecaster
    "9266d881c0fccc02a5beef86c0e7f2894ed754cc36054492a1f99c27b1e0a7be",  # marketing
    "056f236b2a1b4b99d1bd796e9c6899cf04861752c1490dcb350ef43619be121b",  # randd
    "9002b66c4d71dbb7f69bd7bee0338afb3ce38ad304c4eb2e043a4e02a4b058f9",  # security
    "ba0034618ff724f70003a5f5b6c0cb4ed97f079acf3bf147c6eb4fd058531953",  # strategist
    "b2264d6d559e6bc5642564b8126dfd513b9e0bac063a2020ea810d1fd87461b8",  # support
]


class MeshFetcher:
    """Fetches agent events from external relays into local DB."""

    def __init__(self, db, pulse_sync=None):
        self.db = db
        self.pulse = pulse_sync
        self._last_fetch = {}  # relay_url -> timestamp
        self._stats = {
            "total_fetched": 0,
            "total_stored": 0,
            "total_failed": 0,
            "cycles": 0,
            "last_cycle": 0,
            "stored_by_agent": {},
        }
        self._running = False

    def get_alive_relays(self) -> list[str]:
        """Get alive relays from pulse sync or fallback."""
        if self.pulse:
            return self.pulse.get_alive()
        return []

    async def fetch_from_relay(self, url: str, since: int, timeout: float = 8.0) -> list[dict]:
        """Connect to an external relay via WS and fetch events from our agents.
        Returns list of event dicts."""
        import aiohttp

        ws_url = url.replace("http://", "wss://").replace("ws://", "wss://")
        # Strip / if present
        ws_url = ws_url.rstrip("/")

        sub_id = f"mesh_{int(time.time())}"
        req = json.dumps([
            "REQ", sub_id,
            {
                "kinds": [1],
                "authors": AGENT_PUBKEYS,
                "since": since,
                "limit": 100,
            }
        ])

        events = []
        try:
            session_timeout = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=session_timeout) as session:
                async with session.ws_connect(ws_url, heartbeat=30) as ws:
                    await ws.send_str(req)
                    while True:
                        try:
                            resp = await asyncio.wait_for(ws.receive(), timeout=3.0)
                            if resp.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(resp.data)
                                if data[0] == "EVENT" and data[1] == sub_id:
                                    events.append(data[2])
                                elif data[0] == "EOSE" and data[1] == sub_id:
                                    break
                            elif resp.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                        except asyncio.TimeoutError:
                            break
        except Exception as e:
            logger.debug(f"Mesh fetch from {url}: {e}")
            return []

        return events

    def store_event(self, event: dict, source_relay: str) -> bool:
        """Store a fetched event in the local relay DB (idempotent)."""
        event_id = event.get("id", "")
        if not event_id:
            return False

        pubkey = event.get("pubkey", "")
        kind = event.get("kind", 1)
        content = event.get("content", "")
        sig = event.get("sig", "")
        created_at = event.get("created_at", 0)
        tags = event.get("tags", [])
        now = int(time.time())

        try:
            # Add source relay info as a tag if not already present
            has_source = any(len(t) > 0 and t[0] == "source" for t in tags)
            if not has_source:
                tags = list(tags) + [["source", source_relay]]
            
            has_original = any(len(t) > 0 and t[0] == "original" for t in tags)
            if not has_original:
                tags = list(tags) + [["original", str(created_at)]]
                
            tags_json = json.dumps(tags)

            # INSERT OR IGNORE — dedup by event_id
            self.db._db.execute(
                """INSERT OR IGNORE INTO events
                   (id, pubkey, created_at, kind, tags_json, content, sig, received_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, pubkey, created_at, kind, tags_json, content, sig, now)
            )
            self.db._db.commit()

            if self.db._db.total_changes > 0:
                # Update agent stats
                agent_short = pubkey[:12]
                self._stats["stored_by_agent"][agent_short] = \
                    self._stats["stored_by_agent"].get(agent_short, 0) + 1
                return True
            return False  # duplicate
        except Exception as e:
            logger.error(f"Mesh store error: {e}")
            return False

    async def fetch_cycle(self, max_concurrent: int = 5):
        """Run one fetch cycle against all alive relays."""
        relays = self.get_alive_relays()
        if not relays:
            logger.warning("Mesh: no alive relays to fetch from")
            return

        since = int(time.time()) - 3600 * 6  # last 6 hours by default
        # Use last fetch time per relay if available
        relay_targets = []
        for url in relays:
            last = self._last_fetch.get(url, 0)
            relay_since = max(since, last)
            relay_targets.append((url, relay_since))

        logger.info(f"🌐 Mesh: fetching from {len(relays)} relays...")

        # Process in batches to avoid overwhelming
        sem = asyncio.Semaphore(max_concurrent)

        async def fetch_one(url, since_ts):
            async with sem:
                return url, await self.fetch_from_relay(url, since_ts)

        tasks = [fetch_one(url, ts) for url, ts in relay_targets]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        total_fetched = 0
        total_stored = 0
        total_failed = 0

        for result in results:
            if isinstance(result, Exception):
                total_failed += 1
                continue
            url, events = result
            self._last_fetch[url] = int(time.time())
            total_fetched += len(events)
            for evt in events:
                if self.store_event(evt, url):
                    total_stored += 1

        self._stats["total_fetched"] += total_fetched
        self._stats["total_stored"] += total_stored
        self._stats["total_failed"] += total_failed
        self._stats["cycles"] += 1
        self._stats["last_cycle"] = int(time.time())

        agent_details = ", ".join(
            f"{k[:8]}={v}" for k, v in
            sorted(self._stats["stored_by_agent"].items(), key=lambda x: -x[1])[:5]
        )

        logger.info(
            f"🌐 Mesh: {total_fetched} fetched, {total_stored} new / "
            f"{total_failed} failed | agents: {agent_details}"
        )

    async def mesh_loop(self, interval: int = 600):
        """Background loop: fetch every `interval` seconds."""
        self._running = True
        logger.info(f"🌐 Mesh loop started (interval={interval}s)")
        while self._running:
            try:
                await self.fetch_cycle()
            except Exception as e:
                logger.error(f"Mesh error: {e}")
            await asyncio.sleep(interval)

    def start_background(self, interval: int = 600):
        """Start mesh loop as asyncio task."""
        self._task = asyncio.create_task(self.mesh_loop(interval))
        return self._task

    def stop(self):
        self._running = False
        if hasattr(self, '_task'):
            self._task.cancel()

    def get_stats(self) -> dict:
        """Return mesh stats for API."""
        return {
            "running": self._running,
            "cycles": self._stats["cycles"],
            "total_fetched": self._stats["total_fetched"],
            "total_stored": self._stats["total_stored"],
            "total_failed": self._stats["total_failed"],
            "last_cycle": self._stats["last_cycle"],
            "alive_relays": len(self.get_alive_relays()),
            "stored_by_agent": dict(sorted(
                self._stats["stored_by_agent"].items(),
                key=lambda x: -x[1]
            )[:10]),
        }
