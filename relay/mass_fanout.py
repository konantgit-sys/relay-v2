"""
SNIN Relay — Mass Fanout Engine
Broadcasts events to ALL alive relays (520+) instead of usual 30.
Async, batches of 50 relays.
"""

import asyncio
import aiohttp
import json
import logging
import os
import time
import sqlite3

logger = logging.getLogger('mass_fanout')

DB_PATH = os.path.join(os.path.dirname(__file__), 'relay_v2.db')
BATCH_SIZE = 50       # relays per batch
TIMEOUT = 10          # timeout per relay
RATE_LIMIT = 0.05     # delay between relays within batch


class MassFanout:
    """Fanouts events to thousands of relays."""

    def __init__(self, db_path: str = DB_PATH):
        self._db_path = db_path
        self._sent_count = 0
        self._failed_count = 0
        self._cache_ttl = 300  # refresh alive relay list every 5 min
        self._alive_cache = []
        self._cache_time = 0
        self._task: asyncio.Task | None = None

    def _get_alive(self) -> list[str]:
        """Get list of alive relays from DB (with cache)."""
        now = time.time()
        if self._alive_cache and (now - self._cache_time) < self._cache_ttl:
            return self._alive_cache
        
        try:
            conn = sqlite3.connect(self._db_path)
            rows = conn.execute(
                "SELECT url FROM all_relays WHERE status='alive' ORDER BY fail_count ASC"
            ).fetchall()
            conn.close()
            self._alive_cache = [r[0] for r in rows]
            self._cache_time = now
            logger.info(f"MassFanout: loaded {len(self._alive_cache)} alive relays")
        except Exception as e:
            logger.error(f"MassFanout DB error: {e}")
            self._alive_cache = self._alive_cache or []
        
        return self._alive_cache

    async def fanout_event(self, event_json: dict) -> dict:
        """
        Publish event to all alive relays.
        Returns send statistics.
        """
        relays = self._get_alive()
        if not relays:
            return {"sent": 0, "total": 0, "failed": 0}

        msg = json.dumps(["EVENT", event_json])
        total = len(relays)
        sent = 0
        failed = 0
        fail_details = []
        
        conn = aiohttp.TCPConnector(limit=BATCH_SIZE, limit_per_host=2)
        timeout_obj = aiohttp.ClientTimeout(total=TIMEOUT)
        
        async with aiohttp.ClientSession(connector=conn, timeout=timeout_obj) as session:
            for i in range(0, total, BATCH_SIZE):
                batch = relays[i:i + BATCH_SIZE]
                tasks = [self._send_to_relay(session, url, msg) for url in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                for url, result in zip(batch, results):
                    if result is True:
                        sent += 1
                    else:
                        failed += 1
                        if len(fail_details) < 5:
                            fail_details.append(url)
                
                # Log progress
                batch_num = i // BATCH_SIZE + 1
                total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
                logger.info(
                    f"Fanout batch {batch_num}/{total_batches}: "
                    f"{sent}/{i + len(batch)} sent, {failed} failed"
                )
                
                await asyncio.sleep(0.1)  # pause between batches
        
        self._sent_count += sent
        self._failed_count += failed
        
        stats = {
            "sent": sent,
            "failed": failed,
            "total": total,
            "success_rate": f"{sent/total*100:.1f}%" if total else "0%",
            "fail_samples": fail_details[:5],
            "total_all_time_sent": self._sent_count,
        }
        
        logger.info(f"✅ Fanout: {sent}/{total} → {sent/total*100:.1f}%")
        return stats

    async def _send_to_relay(self, session: aiohttp.ClientSession, url: str, msg: str) -> bool:
        """Send event to one relay via WebSocket."""
        try:
            async with session.ws_connect(url, timeout=TIMEOUT, max_msg_size=0) as ws:
                await ws.send_str(msg)
                try:
                    resp = await asyncio.wait_for(ws.receive(), timeout=5)
                    if resp.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(resp.data)
                        return data[0] == "OK"
                except (asyncio.TimeoutError, Exception):
                    pass
                return True  # connected = probably accepted
        except Exception:
            return False

    def get_stats(self) -> dict:
        relays = self._get_alive()
        return {
            "alive_relays": len(relays),
            "total_sent": self._sent_count,
            "total_failed": self._failed_count,
            "cache_ttl": self._cache_ttl,
        }
