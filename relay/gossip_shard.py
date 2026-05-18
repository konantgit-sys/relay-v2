#!/usr/bin/env python3
"""Gossip Shard — P2P shard for SNIN mesh network.

Each shard:
  - holds N TCP connections (agents)
  - gossip: random fan-out to 3 peers
  - anti-entropy every 60 seconds
  - Redis: DHT + last_seen
  - Forward: non-heartbeat → Content Router (:9920, deduplication)

Start a single shard:
    python3 gossip_shard.py --shard-id 0 --port 9100 --agents 100

Start 5 shards:
    for i in 0 1 2 3 4; do
        python3 gossip_shard.py --shard-id $i --port $((9100 + i)) --agents 100 &
    done
"""

import hashlib
import asyncio
import orjson as json
import time
import os
import sys
import random
import argparse

# Configuration
REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
ROUTE_ENGINE_HOST = "127.0.0.1"
ROUTE_ENGINE_PORT = 9920  # Now → Content Router (was 9910 → Route Engine)
GOSSIP_FANOUT = 3  # how many local peers to forward to
ANTI_ENTROPY_INTERVAL = 60  # seconds — peer rebalancing
HEARTBEAT_TTL = 60  # seconds — TTL in Redis
N_SHARDS = 5  # total shards

# Phase 12.1: P2P between gossip shards
GOSSIP_PEER_PORTS = [9100, 9101, 9102, 9103, 9104]  # all shards

# Phase 3: Unix sockets
UNIX_SOCK_DIR = "/tmp/snin"

# ═══ Phase 3: Consistent Hashing ═══
def gossip_shard_for(pubkey: str) -> int:
    """Select shard by pubkey via MD5 hash."""
    if not pubkey or pubkey == "?" or len(pubkey) < 8:
        return random.randint(0, N_SHARDS - 1)
    h = hashlib.md5(pubkey.encode()).hexdigest()
    return int(h[:8], 16) % N_SHARDS


# ═══ Phase 4: Gossip Batch ═══
BATCH_INTERVAL = 0.05   # 50ms — max batch delay
BATCH_SIZE = 32          # max messages per batch

class GossipBatcher:
    """Buffered gossip send.
    
    Instead of per-event writer.write + drain (event loop blocking),
    buffer messages and send in batches every BATCH_INTERVAL seconds
    when BATCH_SIZE messages accumulated.
    
    drain() is called only once per batch.
    """
    
    def __init__(self):
        self._buffers: dict = {}  # writer_id -> (writer, [messages])
        self._task = None
    
    def add(self, writer, msg: bytes):
        """Add message to buffer for this writer."""
        wid = id(writer)
        if wid not in self._buffers:
            self._buffers[wid] = (writer, [])
        self._buffers[wid][1].append(msg)
        # When BATCH_SIZE reached — immediate flush
        if len(self._buffers[wid][1]) >= BATCH_SIZE:
            if self._task and not self._task.done():
                # Signal for immediate flush
                pass
    
    async def flush(self):
        """Flush all buffers as single batch per writer."""
        for wid, (writer, msgs) in list(self._buffers.items()):
            if not msgs:
                continue
            try:
                # Batch write (no drain between messages)
                for m in msgs:
                    writer.write(m)
                await writer.drain()
                msgs.clear()
            except (ConnectionResetError, BrokenPipeError, OSError):
                self._buffers.pop(wid, None)
    
    async def flusher_loop(self):
        """Background loop: flush buffers every BATCH_INTERVAL seconds."""
        while True:
            await asyncio.sleep(BATCH_INTERVAL)
            await self.flush()
    
    def start(self, loop: asyncio.AbstractEventLoop = None):
        """Start background flusher."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.flusher_loop())
    
    async def stop(self):
        """Stop flusher + final flush."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.flush()

class GossipShard:
    """A single gossip shard."""

    def __init__(self, shard_id: int, port: int, max_agents: int):
        self.shard_id = shard_id
        self.port = port
        self.max_agents = max_agents
        self.agents: dict[str, dict] = {}  # agent_id -> {writer, pubkey, last_seen, n_peers}
        self.peer_map: dict[str, set[str]] = {}  # agent_id -> set of peer_ids
        self.route_writer = None
        self.batcher = GossipBatcher()
        # Phase 12.1: P2P between gossip shards
        self.peer_shards: dict[int, tuple] = {}  # shard_id -> (reader, writer)
        self.p2p_batcher = GossipBatcher()
        self.stats = {
            "received": 0,
            "gossiped": 0,
            "forwarded": 0,
            "p2p_sent": 0,
            "p2p_recv": 0,
            "dht_puts": 0,
            "errors": 0,
            "agents_connected": 0,
            "rejected_direct": 0,
            "upstream_connected": 0,
        }
        # Phase 12.1: P2P peer shards
        self.peer_shards: dict[int, tuple] = {}
        self.p2p_batcher = GossipBatcher()

    async def connect_redis(self):
        """Connect to Redis."""
        try:
            import redis.asyncio as aioredis
            self.redis = await aioredis.from_url(
                f"redis://{REDIS_HOST}:{REDIS_PORT}",
                max_connections=10,
                decode_responses=True,
            )
            await self.redis.ping()
            print(f"[Shard-{self.shard_id}] Redis connected")
        except Exception as e:
            print(f"[Shard-{self.shard_id}] Redis not available (DHT falls back): {e}")
            self.redis = None

    async def connect_content_router(self):
        """Connect to Content Router (dedup before RE)."""
        for attempt in range(5):
            try:
                reader, writer = await asyncio.open_connection(
                    ROUTE_ENGINE_HOST, ROUTE_ENGINE_PORT
                )
                self.route_writer = writer
                print(f"[Shard-{self.shard_id}] Connected to Content Router (:{ROUTE_ENGINE_PORT})")
                return
            except (ConnectionRefusedError, OSError):
                await asyncio.sleep(1)
        print(f"[Shard-{self.shard_id}] Content Router not available (forward disabled)")

    # ═══ Phase 12.1: P2P between gossip shards ═══
    async def connect_peer_shards(self):
        """Connect directly to all other gossip shards (P2P)."""
        for sid, sport in enumerate(GOSSIP_PEER_PORTS):
            if sid == self.shard_id:
                continue  # skip self
            for attempt in range(3):
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection("127.0.0.1", sport),
                        timeout=2
                    )
                    # Send hello-message with gossip_peer identification
                    hello = json.dumps({
                        "from": f"shard_{self.shard_id}",
                        "meta": {"origin": "gossip_peer", "from": f"shard_{self.shard_id}"},
                        "pubkey": "?" * 8,
                        "kind": 0,
                        "content": json.dumps({"type": "p2p_hello", "shard_id": self.shard_id}).decode(),
                    }) + b"\n"
                    writer.write(hello)
                    await asyncio.wait_for(writer.drain(), timeout=2)
                    
                    self.peer_shards[sid] = (reader, writer)
                    print(f"[Shard-{self.shard_id}] ✅ P2P connected to shard {sid} (:{sport})")
                    break
                except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as e:
                    if attempt == 2:
                        print(f"[Shard-{self.shard_id}] ⚠️ P2P shard {sid} not available: {e}")
                    await asyncio.sleep(1)

        print(f"[Shard-{self.shard_id}] P2P peers: {list(self.peer_shards.keys())} ({len(self.peer_shards)}/4)")

    async def _p2p_to_shards(self, msg: dict, sender_id: str):
        """Broadcast message to all P2P peer shards (except self)."""
        for sid, (reader, writer) in list(self.peer_shards.items()):
            if writer and not writer.is_closing():
                try:
                    msg["meta"]["p2p_from"] = f"shard_{self.shard_id}"
                    self.p2p_batcher.add(writer, json.dumps(msg).encode() + b"\n")
                    self.stats["p2p_sent"] += 1
                except Exception:
                    self.stats["errors"] += 1
                    # Try reconnect
                    self.peer_shards.pop(sid, None)
                    asyncio.create_task(self._reconnect_peer(sid))

    async def _reconnect_peer(self, sid: int):
        """Reconnect to peer shard."""
        await asyncio.sleep(5)
        sport = GOSSIP_PEER_PORTS[sid]
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", sport), timeout=2
            )
            hello = json.dumps({
                "from": f"shard_{self.shard_id}",
                "meta": {"origin": "gossip_peer", "from": f"shard_{self.shard_id}"},
                "pubkey": "?" * 8,
                "kind": 0,
                "content": json.dumps({"type": "p2p_hello", "shard_id": self.shard_id}).decode(),
            }) + b"\n"
            writer.write(hello)
            await asyncio.wait_for(writer.drain(), timeout=2)
            self.peer_shards[sid] = (reader, writer)
            print(f"[Shard-{self.shard_id}] ✅ P2P reconnected to shard {sid}")
        except Exception as e:
            print(f"[Shard-{self.shard_id}] ⚠️ P2P reconnect shard {sid} failed: {e}")

    def _get_peers(self, exclude: str, count: int = GOSSIP_FANOUT) -> list[str]:
        """Return N random peer_ids, excluding exclude."""
        candidates = [aid for aid in self.agents if aid != exclude]
        if len(candidates) <= count:
            return candidates
        return random.sample(candidates, count)

    async def _gossip_to_peers(self, msg: dict, sender_id: str):
        """Broadcast message to random local peers (fan-out ×3) + P2P shards."""
        # Local fan-out
        peers = self._get_peers(sender_id, GOSSIP_FANOUT)
        for peer_id in peers:
            try:
                writer = self.agents[peer_id].get("writer")
                if writer and not writer.is_closing():
                    self.batcher.add(writer, json.dumps(msg).encode() + b"\n")
                    self.stats["gossiped"] += 1
                if sender_id not in self.peer_map:
                    self.peer_map[sender_id] = set()
                self.peer_map[sender_id].add(peer_id)
            except Exception:
                self.stats["errors"] += 1
        
        # Phase 12.1: P2P to all other shards
        await self._p2p_to_shards(msg, sender_id)

    async def _forward_to_route(self, msg: dict):
        """Non-heartbeat → Content Router (with dedup)."""
        if self.route_writer and not self.route_writer.is_closing():
            try:
                self.batcher.add(self.route_writer, json.dumps(msg).encode() + b"\n")
                self.stats["forwarded"] += 1
            except Exception:
                self.stats["errors"] += 1
                self.route_writer = None
                await self.connect_content_router()

    async def _update_redis_dht(self, agent_id: str, pubkey: str, data: dict):
        """DHT write to Redis."""
        if self.redis:
            try:
                key = f"dht:{agent_id}"
                val = json.dumps({
                    "pubkey": pubkey,
                    "shard": self.shard_id,
                    "port": self.port,
                    "last_seen": time.time(),
                    "data": data,
                })
                await self.redis.setex(key, HEARTBEAT_TTL, val)
                self.stats["dht_puts"] += 1
            except Exception:
                pass

    # _update_redis_heartbeat removed — P0-B: heartbeat from Relay Mesh, not shards

    async def handle_agent(self, reader, writer):
        """Handle messages from Smart Router (single upstream after P0-C)."""
        addr = writer.get_extra_info("peername")
        agent_id = f"agent_{int(time.time()*1000) % 100000}"
        is_upstream = False
        upstream_name = ""

        try:
            # P0-C: check that Smart Router or peer shard connected
            first_line = await reader.readline()
            if not first_line:
                writer.close()
                return

            first_line = first_line.decode().strip()
            if not first_line:
                writer.close()
                return

            msg = json.loads(first_line)
            meta = msg.get("meta", {})
            if not isinstance(meta, dict):
                meta = {}
            origin = meta.get("origin", "")
            upstream_from = meta.get("from", "")

            if origin in ("smart_router", "gossip_peer"):
                is_upstream = True
                upstream_name = upstream_from
                self.stats["upstream_connected"] += 1
                
                # Phase 12.1: P2P message from another shard
                if origin == "gossip_peer" and "p2p_from" in meta:
                    self.stats["p2p_recv"] += 1
                    # P2P messages: forward to CR only (no gossip — avoid loop)
                    await self._forward_to_route(msg)
                    # Also gossip locally
                    await self._gossip_to_peers(msg, agent_name)
                    return  # do not register as agent
            else:
                # Direct agent connection — redirect to Smart Router
                reply = json.dumps({
                    "error": "direct_agent_connection_not_allowed",
                    "message": "Use Smart Router (:9932) as single entry point",
                    "redirect": "127.0.0.1:9932"
                })
                try:
                    writer.write((reply + "\n").encode())
                    await writer.drain()
                except Exception:
                    pass
                writer.close()
                self.stats["rejected_direct"] += 1
                return

            # Register agent
            agent_name = msg.get("from", agent_id)
            pubkey = msg.get("pubkey", "")
            
            # ═══ Phase 3: Consistent Hashing — check agent shard assignment ═══
            if pubkey and len(pubkey) > 8 and origin == "smart_router":
                expected_shard = gossip_shard_for(pubkey)
                if expected_shard != self.shard_id:
                    # Message arrived at wrong shard — forward to correct one
                    self.stats["ch_misroute"] += 1
                    # Forward to CR as fallback (CR deduplicates anyway)
                    await self._forward_to_route(msg)
                    print(f"[Shard-{self.shard_id}] ⚠️ CH misroute: {pubkey[:12]} → shard{expected_shard}, forwarding to CR")
                    # Do not register agent — not ours
                    return
            
            if agent_name not in self.agents:
                self.agents[agent_name] = {
                    "writer": writer,
                    "pubkey": pubkey,
                    "last_seen": time.time(),
                    "n_peers": len(self._get_peers(agent_name, 100)),
                }
                self.stats["agents_connected"] = len(self.agents)

            self.agents[agent_name]["last_seen"] = time.time()

            # Handle first message
            kind = msg.get("kind")
            content = msg.get("content", "{}")
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except (json.JSONDecodeError, TypeError):
                    content = {}
            msg_type = content.get("type", "") if isinstance(content, dict) else ""

            await self._route_message(msg, agent_name, pubkey, kind, content, msg_type)

            # Remaining messages
            while True:
                line = await reader.readline()
                if not line:
                    break
                line = line.decode().strip()
                if not line:
                    continue

                msg = json.loads(line)
                self.stats["received"] += 1
                agent_name = msg.get("from", agent_name)
                pubkey = msg.get("pubkey", pubkey)
                kind = msg.get("kind")
                content = msg.get("content", "{}")
                if isinstance(content, str):
                    try:
                        content = json.loads(content)
                    except (json.JSONDecodeError, TypeError):
                        content = {}
                msg_type = content.get("type", "") if isinstance(content, dict) else ""

                await self._route_message(msg, agent_name, pubkey, kind, content, msg_type)

        except (ConnectionResetError, BrokenPipeError, json.JSONDecodeError):
            pass
        except Exception as e:
            self.stats["errors"] += 1
        finally:
            if agent_name in self.agents:
                del self.agents[agent_name]
                self.stats["agents_connected"] = len(self.agents)
            writer.close()

    async def _route_message(self, msg, agent_name, pubkey, kind, content, msg_type):
        """Route a single message."""
        self.stats["received"] += 1

        if msg_type == "heartbeat":
            # Heartbeat → gossip peers only (P0-B: Redis write from Relay Mesh)
            await self._gossip_to_peers(msg, agent_name)

        elif msg_type == "hello":
            # Hello → DHT into Redis
            await self._update_redis_dht(agent_name, pubkey, content)
            await self._gossip_to_peers(msg, agent_name)

        elif kind in (39010, 39011):
            # DAO → gossip + forward (urgent)
            await self._gossip_to_peers(msg, agent_name)
            await self._forward_to_route(msg)

        elif kind == 39001:
            # DHT signal → Redis + gossip
            await self._update_redis_dht(agent_name, pubkey, content)
            await self._gossip_to_peers(msg, agent_name)

        else:
            # Everything else → gossip + forward to Content Router
            await self._gossip_to_peers(msg, agent_name)
            await self._forward_to_route(msg)

    async def anti_entropy(self):
        """Run anti-entropy rebalancing every ANTI_ENTROPY_INTERVAL seconds."""
        while True:
            await asyncio.sleep(ANTI_ENTROPY_INTERVAL)
            # Dead agents cleanup
            now = time.time()
            dead = [
                aid for aid, info in self.agents.items()
                if now - info["last_seen"] > HEARTBEAT_TTL
            ]
            for aid in dead:
                del self.agents[aid]
                if aid in self.peer_map:
                    del self.peer_map[aid]
                print(f"[Shard-{self.shard_id}] Agent expired: {aid}")

            self.stats["agents_connected"] = len(self.agents)

    async def print_stats(self):
        """Statistics every 10 seconds."""
        while True:
            await asyncio.sleep(10)
            s = self.stats
            print(f"[Shard-{self.shard_id}] Agents:{s['agents_connected']} "
                  f"recv:{s['received']} gossip:{s['gossiped']} "
                  f"p2p_sent:{s['p2p_sent']} p2p_recv:{s['p2p_recv']} "
                  f"fwd:{s['forwarded']} dht:{s['dht_puts']} err:{s['errors']}")
            # Reset
            for k in self.stats:
                if k != "agents_connected":
                    self.stats[k] = 0


    async def run(self):
        await self.connect_redis()
        await self.connect_content_router()
        await self.connect_peer_shards()  # Phase 12.1: P2P between shards
        
        # Start P2P batcher
        self.p2p_batcher.start()
        
        # Phase 3: Unix socket
        unix_path = f"{UNIX_SOCK_DIR}/gossip_{self.shard_id}.sock"
        os.makedirs(UNIX_SOCK_DIR, exist_ok=True)
        try:
            os.unlink(unix_path)
        except FileNotFoundError:
            pass
        unix_server = await asyncio.start_unix_server(
            self.handle_agent, unix_path)
        print(f"[Shard-{self.shard_id}] Unix socket {unix_path}")

        server = await asyncio.start_server(
            self.handle_agent,
            "127.0.0.1",
            self.port,
        )
        print(f"[Shard-{self.shard_id}] TCP 127.0.0.1:{self.port}")
        print(f"[Shard-{self.shard_id}] Max agents: {self.max_agents}")
        print(f"[Shard-{self.shard_id}] Gossip fanout: {GOSSIP_FANOUT}")

        async with server, unix_server:
            await asyncio.gather(
                server.serve_forever(),
                unix_server.serve_forever(),
                self.anti_entropy(),
                self.print_stats(),
            )


def main():
    parser = argparse.ArgumentParser(description="Gossip Shard")
    parser.add_argument("--shard-id", type=int, default=0, help="Shard ID (0-4)")
    parser.add_argument("--port", type=int, default=9100, help="TCP port")
    parser.add_argument("--agents", type=int, default=100, help="Max agents in shard")
    args = parser.parse_args()

    shard = GossipShard(
        shard_id=args.shard_id,
        port=args.port,
        max_agents=args.agents,
    )
    asyncio.run(shard.run())


if __name__ == "__main__":
    main()
