"""
SNIN Relay V2 — Pulse Sync (Phase 2.4)
Pings all Nostr relays from Cryter's health monitor, tracks alive/dead status.
Integrated into relay_server_v2.py via background task.
"""

import asyncio
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger('pulse_sync')

# Cryter relay list paths (set via env for production; fallback to empty = local scan only)
CRYTER_RELAY_PATH = os.getenv("CRYTER_RELAY_PATH", "")
CRYTER_BLACKLIST_PATH = os.getenv("CRYTER_BLACKLIST_PATH", "")

# Fallback relay list if Cryter's file isn't available
FALLBACK_RELAYS = [
    "wss://relay.primal.net",
    "wss://nos.lol",
    "wss://relay.damus.io",
    "wss://offchain.pub",
    "wss://relay.nostr.wirednet.jp",
    "wss://nostr.bitcoiner.social",
    "wss://nostr.oxtr.dev",
    "wss://relay.nostr.net",
    "wss://relay.nostr.info",
    "wss://purplepag.es",
    "wss://nostr.sathoarder.com",
    "wss://relay.minibits.cash",
    "wss://nostr.mom",
    "wss://relay.nostr.nu",
    "wss://nostr-pub.semisol.dev",
    "wss://pyramid.fiatjaf.com",
    "wss://nostr.slothy.win",
    "wss://relay.ryzizub.com",
    "wss://nostr.einundzwanzig.space",
    "wss://nostr.vulpem.com",
    "wss://nostr.sovbit.host",
    "wss://relay.nostr.hu",
    "wss://relay.nostr.watch",
    "wss://nostrrelay.com",
    "wss://nostr-pub.wellorder.net",
    "wss://nostr-verified.wellorder.net",
    "wss://relay.nostr.com.au",
    "wss://relay.nostrcheck.me",
    "wss://relay.nostr.wf",
    "wss://relay.noswhere.com",
    "wss://nostr-relay.derekross.me",
]


def load_cryter_relays() -> list[str]:
    """Load relay list from Cryter's relay_health_monitor.py."""
    try:
        import importlib.util
        import sys
        
        spec = importlib.util.spec_from_file_location("relay_health_monitor", CRYTER_RELAY_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["relay_health_monitor_pulse"] = mod
        spec.loader.exec_module(mod)
        if hasattr(mod, 'ALL_RELAYS'):
            return mod.ALL_RELAYS
    except Exception as e:
        logger.debug(f"Could not load Cryter relays: {e}")
    
    # Fallback: try to parse the file directly
    try:
        with open(CRYTER_RELAY_PATH) as f:
            content = f.read()
        import re
        relays = re.findall(r'"wss://[^"]+"', content)
        if relays:
            return [r.strip('"') for r in relays]
    except Exception:
        pass
    
    return FALLBACK_RELAYS


def load_blacklist() -> dict:
    """Load Cryter's relay blacklist."""
    try:
        with open(CRYTER_BLACKLIST_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


class PulseSync:
    """Pulse sync engine — checks relay health periodically."""

    def __init__(self):
        self._relays: list[str] = load_cryter_relays()
        self._blacklist: dict = load_blacklist()
        self._results: dict[str, dict] = {}
        self._last_pulse: float = 0
        self._running = False
        self._task: Optional[asyncio.Task] = None

    @property
    def relay_count(self) -> int:
        return len(self._relays)

    @property
    def alive_count(self) -> int:
        return sum(1 for r in self._results.values() if r.get('alive'))

    @property
    def dead_count(self) -> int:
        return sum(1 for r in self._results.values() if not r.get('alive'))

    async def ping_relay(self, url: str, timeout: float = 3.0) -> dict:
        """Ping a single relay via WebSocket connect."""
        start = time.time()
        result = {
            "url": url,
            "alive": False,
            "latency_ms": 0,
            "error": None,
            "checked_at": int(start),
        }

        blacklist = load_blacklist()
        if url in blacklist:
            result["alive"] = False
            result["error"] = f"blacklisted: {blacklist[url].get('reason', 'unknown')}"
            return result

        try:
            import aiohttp
            session_timeout = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=session_timeout) as session:
                async with session.ws_connect(url, heartbeat=30):
                    latency = (time.time() - start) * 1000
                    result["alive"] = True
                    result["latency_ms"] = round(latency, 1)
        except asyncio.TimeoutError:
            result["error"] = "timeout"
        except Exception as e:
            result["error"] = str(e)[:60]

        result["checked_at"] = int(time.time())
        return result

    async def pulse(self, batch_size: int = 50):
        """Run a full pulse — ping all relays in batches."""
        logger.info(f"⚡ Pulse start: {len(self._relays)} relays (batch={batch_size})")

        results = {}
        for i in range(0, len(self._relays), batch_size):
            batch = self._relays[i:i + batch_size]
            tasks = [self.ping_relay(url) for url in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in batch_results:
                if isinstance(r, dict):
                    results[r["url"]] = r
                elif isinstance(r, Exception):
                    logger.warning(f"Ping exception: {r}")

        alive = sum(1 for r in results.values() if r.get('alive'))
        dead = sum(1 for r in results.values() if not r.get('alive'))
        self._results = results
        self._last_pulse = time.time()

        logger.info(f"⚡ Pulse complete: {alive} alive / {dead} dead / {len(results)} total")
        return {
            "total": len(results),
            "alive": alive,
            "dead": dead,
            "checked_at": int(self._last_pulse),
            "source": "cryter_health_monitor",
            "relays": results,
        }

    def get_alive(self) -> list[str]:
        """Return list of alive relay URLs."""
        return [url for url, r in self._results.items() if r.get('alive')]

    async def pulse_loop(self, interval: int = 1800):
        """Background loop: pulse every `interval` seconds."""
        self._running = True
        while self._running:
            try:
                await self.pulse()
            except Exception as e:
                logger.error(f"Pulse error: {e}")
            await asyncio.sleep(interval)

    def start_background(self, interval: int = 1800):
        """Start pulse loop as asyncio task."""
        self._task = asyncio.create_task(self.pulse_loop(interval))
        return self._task

    def stop(self):
        """Stop the background loop."""
        self._running = False
        if self._task:
            self._task.cancel()
