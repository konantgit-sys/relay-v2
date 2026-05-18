#!/usr/bin/env python3
"""
WebSocket Server for Relay Mesh.
Replacement for flask-sock (simple-websocket) — full websockets server with RFC 6455 ping/pong.

Port: 9908
Receives event batches from Route Engine, responds with {"ok": true}.
Built-in keepalive: ping_interval=15 seconds (automatic via websockets).
"""

import asyncio
import json
import logging
import signal
import sys
import time
import websockets

WS_HOST = "127.0.0.1"
WS_PORT = 9908
PING_INTERVAL = 15
PING_TIMEOUT = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("ws-server")


async def handle_ingest(websocket):
    """Handle incoming messages from Route Engine."""
    peer = websocket.remote_address
    logger.info(f"[IngestWS] Route Engine connected from {peer}")

    try:
        async for raw in websocket:
            if isinstance(raw, bytes):
                raw = raw.decode()
            if not raw or raw.strip() == "":
                continue

            # Keepalive ping (application-level, redundant)
            try:
                data = json.loads(raw)
                if isinstance(data, dict) and "ping" in data:
                    await websocket.send(json.dumps({"pong": time.time()}))
                    continue
            except json.JSONDecodeError:
                pass

            data = json.loads(raw)
            # Event batch from RE
            if isinstance(data, dict) and "events" in data:
                events = data["events"]
                if isinstance(events, list):
                    logger.info(f"[IngestWS] Batch {len(events)} events from RE")

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning(f"[IngestWS] Connection error: {e}")
    finally:
        logger.info(f"[IngestWS] Route Engine disconnected")


async def main():
    loop = asyncio.get_event_loop()
    stop = loop.create_future()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set_result, None)
        except NotImplementedError:
            # Windows fallback
            signal.signal(sig, lambda s, f: stop.set_result(None))

    async with websockets.serve(
        handle_ingest,
        WS_HOST,
        WS_PORT,
        ping_interval=PING_INTERVAL,   # RFC 6455 ping every 15 seconds
        ping_timeout=PING_TIMEOUT,     # wait for pong 10 seconds
        max_size=50_000_000,           # 50MB — for large batches
        compression=None,              # no compression (faster)
    ):
        logger.info(f"🚀 WebSocket Server running on ws://{WS_HOST}:{WS_PORT}")
        await stop

    logger.info("WebSocket Server stopped")


if __name__ == "__main__":
    asyncio.run(main())
