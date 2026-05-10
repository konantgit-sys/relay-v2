"""
SNIN Relay — Mass PulseSync
Scans ALL known Nostr relays for liveness (1791+ relay).
Stores alive/dead in SQLite. Runs in background.
"""

import asyncio
import aiohttp
import json
import logging
import time
import os
import sqlite3

logger = logging.getLogger('mass_pulse')

RELAY_LIST_PATH = os.path.join(os.path.dirname(__file__), 'all_known_relays.txt')
DB_PATH = os.path.join(os.path.dirname(__file__), 'relay_v2.db')

BATCH_SIZE = 100       # concurrent checks
TIMEOUT = 4            # seconds per relay
SCAN_INTERVAL = 600   # rescan every hour
PULSE_KEEP_MIN = 120   # keep alive status min 2 hours


class MassPulse:
    """Scan thousands of relays for liveness."""

    def __init__(self, db_path: str = DB_PATH):
        self._relays: list[str] = self._load_relays()
        self._alive: set[str] = set()
        self._dead: set[str] = set()
        self._db_path = db_path
        self._task: asyncio.Task | None = None
        self._last_scan = 0
        self._scanning = False
        self._progress = {"total": 0, "checked": 0, "alive": 0, "dead": 0}

    def _load_relays(self) -> list[str]:
        """Load relay list from file."""
        path = RELAY_LIST_PATH
        if not os.path.exists(path):
            # Fallback: try to find it in repo
            path = os.path.join(os.path.dirname(__file__), 'all_known_relays.txt')
        if not os.path.exists(path):
            logger.warning(f"Relay list not found at {path}")
            return []
        try:
            with open(path) as f:
                relays = [
                    line.strip() for line in f
                    if line.strip().startswith('wss://') and len(line.strip()) < 200
                ]
            relays = list(set(relays))  # dedup
            logger.info(f"Loaded {len(relays)} relays from {path}")
            return relays
        except Exception as e:
            logger.error(f"Failed to load relays: {e}")
            return []

    def _get_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS all_relays (
                url TEXT PRIMARY KEY,
                status TEXT DEFAULT 'unknown',
                checked_at INTEGER DEFAULT 0,
                alive_since INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        return conn

    def _save_relay(self, url: str, status: str):
        now = int(time.time())
        try:
            conn = self._get_db()
            existing = conn.execute(
                "SELECT status, alive_since, fail_count FROM all_relays WHERE url=?", (url,)
            ).fetchone()
            if existing:
                old_status, old_alive_since, old_fail_count = existing
                if status == 'alive':
                    fail_count = 0
                    alive_since = old_alive_since if old_status == 'alive' else now
                else:
                    fail_count = (old_fail_count or 0) + 1
                    alive_since = old_alive_since or 0
                conn.execute(
                    """UPDATE all_relays SET status=?, checked_at=?, alive_since=?, fail_count=?
                       WHERE url=?""",
                    (status, now, alive_since, fail_count, url)
                )
            else:
                alive_since = now if status == 'alive' else 0
                conn.execute(
                    "INSERT INTO all_relays (url, status, checked_at, alive_since, fail_count) VALUES (?,?,?,?,?)",
                    (url, status, now, alive_since, 0 if status == 'alive' else 1)
                )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"DB save error for {url}: {e}")

    async def _check_relay(self, session: aiohttp.ClientSession, url: str) -> bool:
        """Check one relay via NIP-11 (GET) + WSS fallback."""
        try:
            # Method 1: NIP-11 info endpoint
            http_url = url.replace('wss://', 'https://').replace('ws://', 'http://')
            async with session.get(http_url, timeout=TIMEOUT, 
                                   headers={"Accept": "application/nostr+json"}) as resp:
                if resp.status == 200:
                    try:
                        data = await resp.json(content_type=None)
                        if isinstance(data, dict) and (data.get('name') or data.get('software') or data.get('description')):
                            return True  # valid NIP-11 relay
                    except (json.JSONDecodeError, ValueError):
                        pass

            # Method 2: WSS WebSocket connection
            try:
                ws_timeout = aiohttp.ClientTimeout(total=8)
                async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=1), timeout=ws_timeout) as ws_session:
                    async with ws_session.ws_connect(url, timeout=8, max_msg_size=0) as ws:
                        await ws.send_str('["REQ","test",{"limit":1}]')
                        try:
                            msg = await asyncio.wait_for(ws.receive(), timeout=3)
                            return msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY)
                        except asyncio.TimeoutError:
                            return True  # connected but no msg = still alive
                        except Exception:
                            return True  # connected = alive
            except Exception:
                return False

        except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
            return False

    async def scan_once(self, progress_callback=None):
        """One full pass over all relays."""
        self._scanning = True
        self._progress = {"total": len(self._relays), "checked": 0, "alive": 0, "dead": 0}
        
        alive_batch = set()
        dead_batch = set()
        
        # Batch processing
        connector = aiohttp.TCPConnector(limit=BATCH_SIZE, limit_per_host=2)
        timeout_obj = aiohttp.ClientTimeout(total=TIMEOUT + 2)
        
        async with aiohttp.ClientSession(connector=connector, timeout=timeout_obj) as session:
            for i in range(0, len(self._relays), BATCH_SIZE):
                batch = self._relays[i:i + BATCH_SIZE]
                tasks = [self._check_relay(session, url) for url in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                for url, result in zip(batch, results):
                    is_alive = result is True
                    status = 'alive' if is_alive else 'dead'
                    self._save_relay(url, status)
                    if is_alive:
                        alive_batch.add(url)
                    else:
                        dead_batch.add(url)
                
                self._progress["checked"] += len(batch)
                self._progress["alive"] = len(alive_batch)
                self._progress["dead"] = len(dead_batch)
                
                if progress_callback:
                    progress_callback(self._progress)
                
                # Small delay between batches to not overwhelm
                await asyncio.sleep(0.1)
                
                # Log progress every 500
                if self._progress["checked"] % 500 == 0 or self._progress["checked"] == len(self._relays):
                    pct = self._progress["checked"] / max(len(self._relays), 1) * 100
                    logger.info(
                        f"Scan: {self._progress['checked']}/{self._progress['total']} "
                        f"({pct:.0f}%) — {self._progress['alive']} alive, {self._progress['dead']} dead"
                    )

        self._alive = alive_batch
        self._dead = dead_batch
        self._last_scan = int(time.time())
        self._scanning = False
        
        logger.info(
            f"✅ Scan complete: {len(alive_batch)} alive / {len(dead_batch)} dead "
            f"/ {len(self._relays)} total"
        )
        return list(alive_batch), list(dead_batch)

    def get_alive(self) -> list[str]:
        """Return currently known alive relays (from latest scan + DB)."""
        if self._alive:
            return list(self._alive)
        # Fallback: get all alive from DB (any timestamp)
        conn = self._get_db()
        rows = conn.execute(
            "SELECT url FROM all_relays WHERE status='alive'"
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]

    def get_stats(self) -> dict:
        """Get scan statistics."""
        conn = self._get_db()
        total = conn.execute("SELECT COUNT(*) FROM all_relays").fetchone()[0]
        alive = conn.execute("SELECT COUNT(*) FROM all_relays WHERE status='alive'").fetchone()[0]
        dead = conn.execute("SELECT COUNT(*) FROM all_relays WHERE status='dead'").fetchone()[0]
        unknown = conn.execute("SELECT COUNT(*) FROM all_relays WHERE status='unknown'").fetchone()[0]
        conn.close()
        return {
            "total": total,
            "alive": alive,
            "dead": dead,
            "unknown": unknown,
            "last_scan": self._last_scan,
            "scanning": self._scanning,
            "progress": self._progress,
        }

    async def _scan_loop(self):
        """Background scanning loop."""
        while True:
            logger.info("🚀 Starting mass pulse scan...")
            await self.scan_once()
            logger.info(f"⏳ Next scan in {SCAN_INTERVAL}s ({SCAN_INTERVAL/60:.0f} min)")
            await asyncio.sleep(SCAN_INTERVAL)

    def start_background(self):
        """Start background scanning."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._scan_loop())
            logger.info("MassPulse background scanner started")
        return self._task

    def stop(self):
        """Stop background scanning."""
        if self._task:
            self._task.cancel()
            self._task = None
