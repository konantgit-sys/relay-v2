#!/usr/bin/env python3
"""Content Router V2 — 5 parallel TCP writers to Route Engine.
   Phase 2: Bloom+Redis hybrid dedup (279x faster)."""

import asyncio
import uvloop
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
import json, time, os, sys, hashlib, math, socket
from collections import defaultdict, deque

# ─── Bloom Filter (pure Python, zero false negatives, 1% FP rate) ──────────
class BloomFilter:
    """Time-windowed Bloom filter with auto-cleanup every DEDUP_WINDOW seconds."""
    
    def __init__(self, capacity=5000, error_rate=0.01):
        self.bit_size = int(-capacity * math.log(error_rate) / (math.log(2) ** 2))
        self.hash_count = max(1, int(self.bit_size / capacity * math.log(2)))
        self.bit_size = max(self.bit_size, self.hash_count * 2)
        self._reset()
    
    def _reset(self):
        self.bits = bytearray(self.bit_size // 8 + 1)
    
    def _hashes(self, item: str):
        """N independent hashes via MD5 + SHA256."""
        h1 = hashlib.md5(item.encode()).digest()
        h2 = hashlib.sha256(item.encode()).digest()
        i1 = int.from_bytes(h1, 'big')
        i2 = int.from_bytes(h2, 'big')
        return [(i1 + i * i2) % self.bit_size for i in range(self.hash_count)]
    
    def add(self, item: str):
        for bit in self._hashes(item):
            byte_idx = bit >> 3
            mask = 1 << (bit & 7)
            self.bits[byte_idx] |= mask
    
    def check(self, item: str) -> bool:
        """True = maybe seen (could be FP). False = definitely NOT seen."""
        for bit in self._hashes(item):
            byte_idx = bit >> 3
            mask = 1 << (bit & 7)
            if not (self.bits[byte_idx] & mask):
                return False
        return True

# ─── Time-windowed in-memory dedup (zero FPs, O(1)) ───────────────────────
class FastDedup:
    """In-memory dedup with TTL. Thread-safe (asyncio)."""
    
    def __init__(self, window=5, max_events=10000):
        self.window = window
        self.max_events = max_events
        self.queue = deque()
        self.seen = set()
    
    def check_and_add(self, event_id: str) -> bool:
        """True = duplicate. False = new event (added)."""
        now = time.time()
        
        # Clean expired (amortized O(1))
        while self.queue and now - self.queue[0][1] > self.window:
            old_id, _ = self.queue.popleft()
            self.seen.discard(old_id)
        
        # Overflow guard
        if len(self.seen) >= self.max_events:
            # Emergency flush — clean 25% oldest
            for _ in range(self.max_events // 4):
                if not self.queue: break
                old_id, _ = self.queue.popleft()
                self.seen.discard(old_id)
        
        if event_id in self.seen:
            return True  # duplicate
        
        self.seen.add(event_id)
        self.queue.append((event_id, now))
        return False  # new event
    
    def clear(self):
        self.queue.clear()
        self.seen.clear()

# ─── Redis Circuit Breaker ─────────────────────────────────────────────────
class RedisCBC:
    """Circuit Breaker for Redis. If Redis goes down — standalone mode."""
    
    INITIAL = 0      # Connected
    TRIPPED = 1      # Failed → use Bloom only
    HALF_OPEN = 2    # Testing reconnect
    
    def __init__(self, check_interval=5, max_retries=3):
        self.state = self.INITIAL
        self.last_fail = 0.0
        self.check_interval = check_interval
        self.retries = 0
        self.max_retries = max_retries
        self.dedup_via_redis = 0
        self.dedup_via_bloom = 0
    
    async def check_redis(self, r):
        if self.state == self.INITIAL:
            try:
                await r.ping()
                return True
            except Exception:
                self.state = self.TRIPPED
                self.last_fail = time.time()
                self.retries = 1
                return False
        
        elif self.state == self.TRIPPED:
            if time.time() - self.last_fail >= self.check_interval:
                self.state = self.HALF_OPEN
            return False
        
        elif self.state == self.HALF_OPEN:
            try:
                await r.ping()
                self.state = self.INITIAL
                self.retries = 0
                return True
            except Exception:
                self.retries += 1
                if self.retries >= self.max_retries:
                    self.check_interval = min(self.check_interval * 2, 60)
                    self.retries = 0
                self.last_fail = time.time()
                self.state = self.TRIPPED
                return False
    
    def reset(self):
        self.state = self.INITIAL
        self.retries = 0
        self.check_interval = 5

# ─── Content Router ─────────────────────────────────────────────────────────
ROUTE_ENGINE_HOST = "127.0.0.1"
ROUTE_ENGINE_PORT = 9910
N_WRITERS = 1  # ⚡ 1 writer, not 5 — avoid ESTAB overflow
DEDUP_WINDOW = 5
CHANGE_THRESHOLD = 0.15

# Phase 3: Unix sockets
UNIX_SOCK_DIR = "/tmp/snin"
UNIX_CR_SOCK = f"{UNIX_SOCK_DIR}/cr.sock"
UNIX_RE_SOCK = f"{UNIX_SOCK_DIR}/re.sock.disabled"  # TCP only

async def init_redis():
    global REDIS_DEDUP
    try:
        import redis.asyncio as redis_py
        REDIS_DEDUP = redis_py.Redis(host='127.0.0.1', port=6379, db=0,
                                      socket_connect_timeout=1, socket_timeout=1,
                                      decode_responses=True)
        await REDIS_DEDUP.ping()
        print(f"[ContentRouter] Redis connected ✅")
    except Exception:
        print(f"[ContentRouter] Redis unavailable — in-memory only")

# Redis (optional — without Redis, CR runs on in-memory dedup)
REDIS_DEDUP = None
REDIS_CB = RedisCBC(check_interval=5)

class ContentRouterV2:
    def __init__(self, port: int):
        self.port = port
        self.writers = []
        self.writer_idx = 0
        self._reconnecting = False
        self.states = {}
        self.stats = {"received": 0, "deduped": 0, "forwarded": 0,
                      "changes": 0, "errors": 0, "redis_ok": 0, "redis_fail": 0}
        self.agents = {}
        
        # Phase 2: Bloom + FastDedup
        self.bloom = BloomFilter(capacity=5000, error_rate=0.01)
        self.fast_dedup = FastDedup(window=DEDUP_WINDOW, max_events=10000)
        self.last_bloom_reset = time.time()

    async def connect_route_engine(self):
        """Phase 3: Unix socket (faster), fallback TCP."""
        # Close stale writers first
        for w in self.writers:
            try:
                w.close()
            except:
                pass
        self.writers = []
        await asyncio.sleep(2)  # ⚡ allow time for FIN
        for _ in range(3):  # max 3 retries
            try:
                for i in range(N_WRITERS):
                    # Unix socket first (Phase 3)
                    try:
                        r, w = await asyncio.wait_for(
                            asyncio.open_unix_connection(UNIX_RE_SOCK), timeout=1)
                    except (FileNotFoundError, ConnectionRefusedError, asyncio.TimeoutError):
                        # TCP fallback
                        r, w = await asyncio.open_connection(ROUTE_ENGINE_HOST, ROUTE_ENGINE_PORT)
                        sock = w.get_extra_info('socket')
                        if sock:
                            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 131072)
                        print(f"[CR] TCP writer created → :{ROUTE_ENGINE_PORT}")
                    else:
                        print(f"[CR] Unix writer created → {UNIX_RE_SOCK}")
                    self.writers.append(w)
                print(f"[ContentRouter] {N_WRITERS} parallel writers → Route Engine (Unix)")
                return
            except (ConnectionRefusedError, OSError):
                await asyncio.sleep(1)
                self.writers = []
        print("[ContentRouter] Route Engine not available")

    async def _reconnect_delayed(self):
        """Reconnect with delay to avoid flood."""
        await asyncio.sleep(3)
        await self.connect_route_engine()
        self._reconnecting = False

    def _has_real_change(self, agent_id, new):
        old = self.states.get(agent_id)
        if old is None: return True
        # If message content (kind:39002) — no state/tasks → always forward
        if "state" not in new and "pending_tasks" not in new and "buffer_size" not in new:
            return True
        if old.get("state") != new.get("state"): return True
        old_tasks = set(old.get("pending_tasks", []))
        new_tasks = set(new.get("pending_tasks", []))
        if old_tasks != new_tasks: return True
        old_buf = old.get("buffer_size", 0)
        new_buf = new.get("buffer_size", 0)
        if abs(new_buf - old_buf) > max(1, old_buf * CHANGE_THRESHOLD): return True
        old_s = old.get("sentiment", 0.0)
        new_s = new.get("sentiment", 0.0)
        if abs(new_s - old_s) > CHANGE_THRESHOLD: return True
        return False

    async def _is_duplicate(self, event_id: str) -> bool:
        """Phase 2: hybrid deduplication.
        
        Fast path (in-memory, 0.5us):
          1. Check FastDedup (O(1) set lookup)
          2. If not found → add
        
        Slow path (Redis, 150us):
          3. Check Redis (for cross-instance)
          4. If Redis unavailable → CB bypasses
        
        Bloom filter — additional safety:
          - For events that could have been evicted from FastDedup
          - Add to Bloom on each new event
          - Check Bloom ONLY if FastDedup missed
        """
        # Step 1: In-memory (279x faster than Redis, zero FPs)
        if self.fast_dedup.check_and_add(event_id):
            self.stats["deduped"] += 1
            return True
        
        # Step 2: Bloom filter (safety for FastDedup overflow)
        if self.bloom.check(event_id):
            # Bloom says maybe — but FastDedup already said "no"
            # So it is either FP or event from old window
            # Check via Redis (it is source of truth)
            if REDIS_DEDUP and await REDIS_CB.check_redis(REDIS_DEDUP):
                dedup_key = f"dedup:{event_id}"
                if await REDIS_DEDUP.get(dedup_key):
                    self.stats["deduped"] += 1
                    self.stats["redis_ok"] += 1
                    return True
        
        # Step 3: This is a NEW event
        self.bloom.add(event_id)
        
        # Step 4: Also persist in Redis (cross-instance)
        if REDIS_DEDUP and await REDIS_CB.check_redis(REDIS_DEDUP):
            try:
                dedup_key = f"dedup:{event_id}"
                await REDIS_DEDUP.setex(dedup_key, DEDUP_WINDOW, "1")
                self.stats["redis_ok"] += 1
            except Exception:
                self.stats["redis_fail"] += 1
                REDIS_CB.state = REDIS_CB.TRIPPED
                REDIS_CB.last_fail = time.time()
        
        return False

    async def process(self, event):
        self.stats["received"] += 1
        
        content = event.get("content", "{}")
        if isinstance(content, str):
            try: content = json.loads(content)
            except: content = {}
        agent_id = content.get("from", "?") if isinstance(content, dict) else "?"
        seq = content.get("seq", 0) if isinstance(content, dict) else 0
        payload = content if isinstance(content, dict) else {}
        
        event_id = event.get("id", "")
        if not event_id:
            event_id = hashlib.sha256((agent_id + str(seq)).encode()).hexdigest()[:32]
        
        # Phase 2: hybrid dedup
        if await self._is_duplicate(event_id):
            return
        
        if not REDIS_DEDUP or not await REDIS_CB.check_redis(REDIS_DEDUP):
            # Fallback: in-memory Bloom filter only
            self.stats["redis_fail"] += 1
        elif not event_id:
            self.stats["deduped"] += 1
            return
        
        # Forward everything that passed dedup
        self.stats["changes"] += 1
        await self._forward_roundrobin(event)

    async def _forward_roundrobin(self, event):
        if not self.writers:
            if not getattr(self, '_reconnecting', False):
                self._reconnecting = True
                await self.connect_route_engine()
                self._reconnecting = False
                if not self.writers:
                    return
            else:
                return  # reconnect already in progress
        idx = self.writer_idx % len(self.writers)
        self.writer_idx += 1
        w = self.writers[idx]
        try:
            w.write((json.dumps(event) + "\n").encode())
            await asyncio.wait_for(w.drain(), timeout=0.5)
            self.stats["forwarded"] += 1
            print(f"[CR] ➡️ fwd kind={event.get('kind',0)} id={event.get('id','?')[:16]} to RE")
        except Exception as e:
            self.stats["errors"] += 1
            print(f"[CR] ⚠️ forward error: {type(e).__name__}: {e}")
            try:
                self.writers.remove(w)
            except ValueError:
                pass
            # ═══ Phase 7: close writer to avoid CLOSE_WAIT ═══
            try:
                w.close()
            except:
                pass
            if not getattr(self, '_reconnecting', False):
                self._reconnecting = True
                asyncio.ensure_future(self._reconnect_delayed())

    async def drain_all(self):
        while True:
            await asyncio.sleep(0.02)
            for w in self.writers[:]:
                try: await w.drain()
                except: pass

    async def handle_event(self, reader, writer):
        """Read events from TCP client. 30s timeout on idle."""
        while True:
            try:
                line = await asyncio.wait_for(
                    reader.readline(), timeout=30
                )
                if not line: break
                line = line.decode().strip()
                if not line: continue
                await self.process(json.loads(line))
            except asyncio.TimeoutError:
                # 30s without data — close inactive connection
                break
            except (json.JSONDecodeError, ConnectionResetError, BrokenPipeError):
                break
            except: break
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=2)
        except:
            pass

    async def clean_stale(self):
        while True:
            await asyncio.sleep(10)
            now = time.time()
            stale = [aid for aid, last in self.agents.items() if now - last > 30]
            for aid in stale:
                del self.agents[aid]
                if aid in self.states: del self.states[aid]
            
            # Background Redis CB recovery (even without events)
            if REDIS_DEDUP and REDIS_CB.state in (REDIS_CB.TRIPPED, REDIS_CB.HALF_OPEN):
                if now - REDIS_CB.last_fail >= REDIS_CB.check_interval:
                    await REDIS_CB.check_redis(REDIS_DEDUP)

    async def print_stats(self):
        while True:
            await asyncio.sleep(10)
            s = self.stats
            r_cb = f"Redis={['INIT','TRIP','HALF'][REDIS_CB.state]}"
            # Background Redis recovery in print_stats
            if REDIS_DEDUP and REDIS_CB.state != REDIS_CB.INITIAL:
                await REDIS_CB.check_redis(REDIS_DEDUP)
            b_age = f"Bloom={int(time.time()-self.last_bloom_reset)}s"
            print(f"[ContentRouter] Agents:{len(self.agents)} "
                  f"recv:{s['received']} dedup:{s['deduped']} "
                  f"chg:{s['changes']} fwd:{s['forwarded']} err:{s['errors']} "
                  f"{r_cb} {b_age}")
            for k in self.stats:
                if k not in ("redis_ok", "redis_fail"):
                    self.stats[k] = 0

    async def run(self):
        await init_redis()
        await self.connect_route_engine()
        
        # Phase 3: Unix socket (for SR)
        os.makedirs(UNIX_SOCK_DIR, exist_ok=True)
        try:
            os.unlink(UNIX_CR_SOCK)
        except FileNotFoundError:
            pass
        unix_server = await asyncio.start_unix_server(
            self.handle_event, UNIX_CR_SOCK)
        print(f"[ContentRouter] Unix socket {UNIX_CR_SOCK}")
        
        server = await asyncio.start_server(self.handle_event, "127.0.0.1", self.port)
        print(f"[ContentRouter] Phase 2 — Bloom+Redis hybrid dedup")
        print(f"[ContentRouter] Phase 3 — Unix sockets")
        print(f"[ContentRouter] Listening on TCP {self.port}")
        print(f"[ContentRouter] Writers: {N_WRITERS}")
        async with server, unix_server:
            await asyncio.gather(
                server.serve_forever(),
                unix_server.serve_forever(),
                self.drain_all(),
                self.clean_stale(),
                self.print_stats(),
            )

if __name__ == "__main__":
    router = ContentRouterV2(int(sys.argv[1]) if len(sys.argv) > 1 else 9920)
    asyncio.run(router.run())
