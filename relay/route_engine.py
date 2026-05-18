#!/usr/bin/env python3
"""Route Engine — Router between P2P bridge and relay-mesh.

Classifies events by kind:
  - kind:39000 with type=heartbeat → bypass to file (not in relay)
  - kind:39001 (DHT) → batch → POST /api/ingest/batch
  - kind:39010+ (DAO) → immediate → POST /api/ingest
  - kind:39000/39002/39003 (mesh) → batch → POST /api/ingest/batch

Start:
    python3 route_engine.py

Receives Nostr events via TCP (localhost:9910) in JSON format.
Each event — one JSON line with newline terminator.
"""

import asyncio
import uvloop
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
import orjson as json
import time
import os
import sys
from collections import defaultdict

import httpx
import websockets

RELAY_MESH = "http://localhost:9907"
RELAY_MESH_WS = "ws://localhost:9908"
HEARTBEAT_LOG = "./heartbeat.log"
BATCH_WINDOW = 0.1  # seconds — batch accumulation (10 flushes/sec)
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 9910

# Phase 3: Unix sockets for internal communication
UNIX_SOCK_DIR = "/tmp/snin"
UNIX_SOCK_PATH = f"{UNIX_SOCK_DIR}/re.sock"


class RouteEngine:
    """Classifier + batcher + bypass."""

    def __init__(self):
        self.batches = defaultdict(list)  # type -> list of events
        self.last_flush = time.time()
        self._http = None  # httpx.AsyncClient (lazy init)
        self._ws = None    # websocket connection (lazy init)
        self._ws_mode = False  # True when WS is active
        self.stats = {
            "received": 0,
            "heartbeat_bypassed": 0,
            "dao_immediate": 0,
            "batched": 0,
            "dht_redis": 0,
            "errors": 0,
            "flushes": 0,
            "ws_flushes": 0,
        }

    async def _get_http(self):
        """Lazy init httpx client (single for entire lifetime)."""
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(5.0, connect=2.0),
                limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
            )
        return self._http

    async def _ws_connect(self):
        """Connect WS to relay-mesh."""
        try:
            self._ws = await websockets.connect(
                RELAY_MESH_WS,
                max_size=10_000_000,  # 10MB — for large batches
                ping_interval=20,      # RFC 6455 keepalive (ws_server supports it)
                ping_timeout=10,
            )
            self._ws_mode = True
            print(f"[RouteEngine] ✅ WS connected to relay-mesh")
            return True
        except Exception as e:
            self._ws_mode = False
            self._ws = None
            print(f"[RouteEngine] ⚠️ WS failed: {e}, fallback to HTTP")
            return False

    async def _get_http(self):
        """Lazy init httpx client (single for entire lifetime)."""
        if self._http is None or self._http.is_closed:
            import httpx
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(5.0, connect=2.0),
                limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
            )
        return self._http

    def classify(self, event: dict) -> str:
        """Return route type: heartbeat / dht / dao / mesh / unknown."""
        kind = event.get("kind")
        if kind == 39000:
            try:
                content = json.loads(event.get("content", "{}"))
                if content.get("type") == "heartbeat":
                    return "heartbeat"
                if content.get("type") == "hello":
                    return "heartbeat"  # hello = one-time heartbeat
            except (json.JSONDecodeError, TypeError):
                pass
            return "mesh"
        if kind == 39001:
            return "dht"
        if kind in (39010, 39011, 39012, 39013):
            return "dao"
        if kind in (39020, 39021):
            return "nft"
        if kind == 30000:
            return "solana"
        if kind in (39002, 39003):
            return "mesh"
        return "unknown"

    async def add(self, event: dict):
        """Add event to queue. Classify and route."""
        rtype = self.classify(event)
        self.stats["received"] += 1

        if rtype == "heartbeat":
            self._bypass_heartbeat(event)
            self.stats["heartbeat_bypassed"] += 1
            return

        if rtype == "dao":
            await self._send_immediate(event)
            self.stats["dao_immediate"] += 1
            return

        # Everything else — into batch
        self.batches[rtype].append(event)
        self.stats["batched"] += 1

    def _bypass_heartbeat(self, event: dict):
        """Write heartbeat to file, not relay."""
        try:
            content = json.loads(event.get("content", "{}"))
            line = json.dumps({
                "ts": time.time(),
                "pubkey": event.get("pubkey", "?"),
                "kind": event.get("kind"),
                "from": content.get("from", "?"),
                "counter": content.get("counter", 0),
                "uptime": content.get("uptime", 0),
                "id": event.get("id", "")[:16],
            }) + "\n"
            with open(HEARTBEAT_LOG, "a") as f:
                f.write(line)
        except Exception as e:
            self.stats["errors"] += 1

    async def _send_immediate(self, event: dict):
        """DAO — immediate relay-mesh (async HTTP)."""
        try:
            http = await self._get_http()
            r = await http.post(
                f"{RELAY_MESH}/api/ingest",
                json=event,
            )
            if r.status_code != 200:
                self.stats["errors"] += 1
        except Exception:
            self.stats["errors"] += 1

    async def _flush_batch(self):
        """Flush accumulated batches — via WS if available, else HTTP."""
        now = time.time()
        total_events = sum(len(v) for v in self.batches.values())
        if total_events == 0:
            # Keepalive — if idle >20s, send ping
            if time.time() - self.last_flush > 20:
                if self._ws_mode and self._ws is not None:
                    try:
                        await self._ws.send(json.dumps({"ping": time.time()}))
                        self.last_flush = time.time()
                    except Exception:
                        self._ws_mode = False
                        self._ws = None
                        print(f"[RouteEngine] ⚠️ WS lost, fallback to HTTP")
            return

        # Collect all events into single array
        all_events = []
        for rtype, events in list(self.batches.items()):
            if events:
                all_events.extend(events)
                self.batches[rtype] = []

        if not all_events:
            return

        # WS: send everything as one message
        if self._ws_mode and self._ws is not None:
            try:
                await self._ws.send(json.dumps({"events": all_events}))
                self.stats["ws_flushes"] += 1
                self.stats["flushes"] += 1
                self.last_flush = now
                return
            except Exception:
                self._ws_mode = False
                self._ws = None
                print(f"[RouteEngine] ⚠️ WS lost, fallback to HTTP")
                # Return events to batch for retry via HTTP
                for ev in all_events:
                    self.batches["recovery"].append(ev)
                return

        # HTTP fallback: batch by type
        for rtype, events in list(self.batches.items()):
            if not events:
                continue
            try:
                http = await self._get_http()
                r = await http.post(
                    f"{RELAY_MESH}/api/ingest/batch",
                    json={"kind": events[0].get("kind"), "events": events, "type": rtype},
                )
                if r.status_code == 200:
                    self.batches[rtype] = []
                    self.stats["flushes"] += 1
                else:
                    self.stats["errors"] += 1
            except Exception:
                self.stats["errors"] += 1

        # Recovery: resend events that returned after WS failure
        recovery = self.batches.pop("recovery", [])
        if recovery:
            try:
                http = await self._get_http()
                r = await http.post(
                    f"{RELAY_MESH}/api/ingest/batch",
                    json={"kind": 39002, "events": recovery, "type": "recovery"},
                )
                if r.status_code == 200:
                    self.stats["flushes"] += 1
                else:
                    self.stats["errors"] += 1
            except Exception:
                self.stats["errors"] += 1

        self.last_flush = now

    async def tick(self):
        """Tick — check and flush batch every BATCH_WINDOW."""
        while True:
            await asyncio.sleep(BATCH_WINDOW)
            await self._flush_batch()

    def stats_report(self) -> dict:
        return dict(self.stats)


class RouteEngineServer:
    """TCP server receiving Nostr events line by line."""

    def __init__(self, engine: RouteEngine):
        self.engine = engine

    async def handle_client(self, reader, writer):
        while True:
            try:
                line = await reader.readline()
                if not line:
                    break
                line = line.decode().strip()
                if not line:
                    continue
                event = json.loads(line)
                await self.engine.add(event)
            except (json.JSONDecodeError, ConnectionResetError, BrokenPipeError):
                self.engine.stats["errors"] += 1
                break
            except Exception:
                self.engine.stats["errors"] += 1
                break
        writer.close()

    async def run(self):
        # Phase 3: Unix socket (for CR)
        os.makedirs(UNIX_SOCK_DIR, exist_ok=True)
        try:
            os.unlink(UNIX_SOCK_PATH)
        except FileNotFoundError:
            pass
        unix_server = await asyncio.start_unix_server(
            self.handle_client, UNIX_SOCK_PATH)
        print(f"[RouteEngine] Unix socket {UNIX_SOCK_PATH}")

        server = await asyncio.start_server(
            self.handle_client,
            LISTEN_HOST,
            LISTEN_PORT,
        )
        addr = server.sockets[0].getsockname()
        print(f"[RouteEngine] TCP {addr[0]}:{addr[1]}")
        print(f"[RouteEngine] Relay: {RELAY_MESH}")
        print(f"[RouteEngine] Heartbeat log: {HEARTBEAT_LOG}")
        print(f"[RouteEngine] Batch window: {BATCH_WINDOW}s")

        async with server, unix_server:
            await asyncio.gather(
                server.serve_forever(),
                unix_server.serve_forever(),
            )


async def keep_ws_alive(engine: RouteEngine):
    """Maintain WS connection to relay-mesh. Reconnect on failure."""
    while True:
        if not engine._ws_mode or engine._ws is None:
            await engine._ws_connect()
        else:
            try:
                # Ping disabled — Flask Sock does not support ping/pong
                await asyncio.sleep(0.1)
            except Exception:
                engine._ws_mode = False
                engine._ws = None
                print(f"[RouteEngine] WS ping failed, reconnecting...")
        await asyncio.sleep(5)


async def main():
    engine = RouteEngine()
    server = RouteEngineServer(engine)

    # Print statistics every 10 seconds
    async def print_stats():
        while True:
            await asyncio.sleep(10)
            s = engine.stats_report()
            print(f"[RouteEngine] Stats: recv={s['received']} hb_bypass={s['heartbeat_bypassed']} "
                  f"dao={s['dao_immediate']} batch={s['batched']} flush={s['flushes']} "
                  f"ws={s['ws_flushes']} err={s['errors']}")
            # Reset counters
            for k in s:
                engine.stats[k] = 0

    await asyncio.gather(
        server.run(),
        engine.tick(),
        print_stats(),
        keep_ws_alive(engine),
    )


if __name__ == "__main__":
    asyncio.run(main())
