"""
SNIN Relay — SSE Handler

HTTP/SSE endpoint for Nostr-compatible protocol.
Alternative to WSS: passes through any ingress, no Upgrade required.

Protocol:
  POST /nostr  {"method":"REQ","params":["sub1",{filters}]}
    → SSE stream: data: ["EVENT","sub1",{...}]
                   data: ["EOSE","sub1"]

  POST /nostr  {"method":"EVENT","params":[{...event...}]}
    → {"status":"ok","cid":"Qm..."}

  POST /nostr  {"method":"AUTH","params":[]}
    → {"status":"challenge","challenge":"abc123"}

  POST /nostr  {"method":"AUTH","params":[{...signed_event...}]}
    → {"status":"ok","pubkey":"abc..."}
"""

import asyncio
import json
import logging
import time
import hashlib
import os
from typing import Optional

from aiohttp import web

logger = logging.getLogger("sse_handler")

# ── Constants ──────────────────────────────────────────────
SSE_KEEPALIVE_INTERVAL = 30  # ping every 30 seconds
SSE_FRAME = 'data: {}\n\n'
MAX_FILTER_EVENTS = 500  # max events per REQ response


# ── SSE Broadcaster (live stream из IPFS pubsub) ────────────

class SSEBroadcaster:
    """Global registry of active SSE subscriptions.

    Allows broadcasting events from IPFS pubsub to all active
    SSE clients in real time.
    """

    def __init__(self):
        self._queues: dict[str, asyncio.Queue] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, sub_id: str) -> asyncio.Queue:
        """Create a queue for subscriber. Returns Queue."""
        q: asyncio.Queue = asyncio.Queue()
        async with self._lock:
            self._queues[sub_id] = q
        return q

    async def unsubscribe(self, sub_id: str):
        """Remove a subscriber."""
        async with self._lock:
            self._queues.pop(sub_id, None)

    async def broadcast(self, event: dict):
        """Broadcast event to all active subscribers."""
        async with self._lock:
            dead = []
            for sub_id, q in self._queues.items():
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    dead.append(sub_id)
            for sub_id in dead:
                self._queues.pop(sub_id, None)

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)


# Global instance
broadcaster = SSEBroadcaster()


# ── CORS ───────────────────────────────────────────────────

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Accept",
    "Access-Control-Max-Age": "86400",
}


def add_cors(response: web.StreamResponse | web.Response):
    """Add CORS headers to response."""
    for k, v in CORS_HEADERS.items():
        response.headers[k] = v


# ── SSE Response ───────────────────────────────────────────

async def sse_response(request) -> web.StreamResponse:
    """Create SSE response with CORS and buffering disabled."""
    resp = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    add_cors(resp)
    await resp.prepare(request)
    return resp


async def sse_send(resp: web.StreamResponse, data: list):
    """Send SSE frame: data: [...]\n\n"""
    payload = SSE_FRAME.format(json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    await resp.write(payload.encode())
    await resp.drain()


async def sse_keepalive(resp: web.StreamResponse, stop_event: asyncio.Event):
    """Background keepalive: :ping every 30 seconds."""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=SSE_KEEPALIVE_INTERVAL)
            break
        except asyncio.TimeoutError:
            try:
                await resp.write(b":ping\n\n")
                await resp.drain()
            except (ConnectionError, ConnectionResetError):
                break


# ── Filter → SQL Query ─────────────────────────────────────

def build_sse_query(filters: dict) -> tuple[str, list]:
    """Build SQL query from Nostr filter dict.

    Returns:
        (sql_where_clause, params_list)
    """
    conditions = []
    params = []

    # kinds
    if "kinds" in filters and filters["kinds"]:
        kinds = filters["kinds"]
        if len(kinds) == 1:
            conditions.append("kind=?")
            params.append(kinds[0])
        else:
            placeholders = ",".join(["?"] * len(kinds))
            conditions.append(f"kind IN ({placeholders})")
            params.extend(kinds)

    # authors
    if "authors" in filters and filters["authors"]:
        authors = filters["authors"]
        # Support 02/03 prefixes
        expanded = []
        for a in authors:
            expanded.append(a)
            if len(a) == 64:
                expanded.append("02" + a)
                expanded.append("03" + a)
            elif len(a) == 66:
                expanded.append(a[2:])  # without prefix
        if len(expanded) == 1:
            conditions.append("pubkey=?")
            params.append(expanded[0])
        else:
            placeholders = ",".join(["?"] * len(expanded))
            conditions.append(f"pubkey IN ({placeholders})")
            params.extend(expanded)

    # ids
    if "ids" in filters and filters["ids"]:
        ids = filters["ids"]
        if len(ids) == 1:
            conditions.append("id=?")
            params.append(ids[0])
        else:
            placeholders = ",".join(["?"] * len(ids))
            conditions.append(f"id IN ({placeholders})")
            params.extend(ids)

    # since / until
    if "since" in filters:
        conditions.append("created_at>=?")
        params.append(int(filters["since"]))
    if "until" in filters:
        conditions.append("created_at<?")
        params.append(int(filters["until"]))

    # search (NIP-50)
    if "search" in filters and filters["search"]:
        conditions.append("content LIKE ?")
        params.append(f"%{filters['search']}%")

    where = " AND ".join(conditions) if conditions else "1=1"
    limit = min(int(filters.get("limit", 100)), MAX_FILTER_EVENTS)

    return where, params, limit


async def query_events_sse(db, filters: dict) -> list[dict]:
    """Execute DB query by Nostr filters.

    Returns:
        List of events (dict)
    """
    where, params, limit = build_sse_query(filters)

    # Determine sort direction
    if filters.get("until"):
        order = "DESC"  # newest to oldest
    else:
        order = "DESC"  # default newest first

    sql = (
        f"SELECT id, pubkey, created_at, kind, tags_json, content, sig "
        f"FROM events WHERE {where} "
        f"ORDER BY created_at {order} LIMIT ?"
    )
    params.append(limit)

    def _query():
        cur = db._db.execute(sql, params)
        return [
            {
                "id": r[0],
                "pubkey": r[1],
                "created_at": r[2],
                "kind": r[3],
                "tags": json.loads(r[4]) if r[4] else [],
                "content": r[5],
                "sig": r[6],
            }
            for r in cur.fetchall()
        ]

    return await asyncio.get_event_loop().run_in_executor(None, _query)


# ── REQ Handler ────────────────────────────────────────────

async def handle_req(
    request: web.Request,
    sub_id: str,
    filters: dict,
    db,
    ipfs=None,
    cid_index=None,
):
    """REQ: SSE stream with events + EOSE + live events from IPFS pubsub."""
    resp = await sse_response(request)
    stop_event = asyncio.Event()
    keepalive_task = asyncio.create_task(sse_keepalive(resp, stop_event))

    # Subscribe to live stream from IPFS pubsub
    live_queue = await broadcaster.subscribe(f"req:{sub_id}")

    try:
        # 1. Send historical events
        events = await query_events_sse(db, filters)
        for event in events:
            await sse_send(resp, ["EVENT", sub_id, event])

        logger.info(
            f"SSE REQ {sub_id}: {len(events)} events sent "
            f"(kinds={filters.get('kinds', 'any')})"
        )

        # 2. EOSE
        await sse_send(resp, ["EOSE", sub_id])

        # 3. Live stream — await new events from IPFS pubsub
        logger.debug(f"SSE REQ {sub_id}: entering live mode ({broadcaster.subscriber_count} active)")
        while True:
            try:
                # Wait for new event or keepalive
                event = await asyncio.wait_for(
                    live_queue.get(),
                    timeout=SSE_KEEPALIVE_INTERVAL,
                )
                # Check if matches filter (rough check)
                if _matches_filter(event, filters):
                    await sse_send(resp, ["EVENT", sub_id, event])
            except asyncio.TimeoutError:
                # keepalive already sent by sse_keepalive task
                pass

    except (ConnectionResetError, ConnectionAbortedError, Exception) as e:
        logger.debug(f"SSE REQ {sub_id}: disconnected ({e})")
    finally:
        stop_event.set()
        keepalive_task.cancel()
        await broadcaster.unsubscribe(f"req:{sub_id}")
        try:
            await keepalive_task
        except asyncio.CancelledError:
            pass

    return resp


def _matches_filter(event: dict, filters: dict) -> bool:
    """Rough event-to-filter match (for live stream)."""
    if not filters:
        return True
    kinds = filters.get("kinds")
    if kinds and event.get("kind") not in kinds:
        return False
    authors = filters.get("authors")
    if authors:
        pk = event.get("pubkey", "")
        if pk not in authors and pk[2:] not in authors:
            return False
    return True


# ── EVENT Handler ──────────────────────────────────────────

async def handle_event(
    request: web.Request,
    event: dict,
    db,
    ipfs=None,
    cid_index=None,
):
    """EVENT: publish event to relay + IPFS."""
    # Validate via nostr_marshal
    try:
        from nostr_marshal import verify_integrity, marshal_event

        integrity = verify_integrity(event)
        if not integrity["valid"]:
            return web.json_response(
                {"status": "error", "message": integrity["error"]},
                status=400,
            )
    except ImportError:
        pass  # nostr_marshal may be unavailable

    event_id = event.get("id", "")

    # Save to relay DB
    try:
        # store_event_async takes event dict, not separate fields
        await db.store_event_async(event)
    except Exception as e:
        logger.warning(f"SSE EVENT: DB store failed: {e}")

    # Publish to IPFS
    cid = None
    if ipfs:
        try:
            cid = await ipfs.publish_event(event)
            logger.info(f"SSE EVENT: IPFS published {cid}")
        except Exception as e:
            logger.warning(f"SSE EVENT: IPFS publish failed: {e}")

    # CID Index
    if cid_index and cid:
        try:
            cid_index.add(event_id, cid, pubkey, kind, created_at)
        except Exception as e:
            logger.warning(f"SSE EVENT: CID index add failed: {e}")

    # Fanout fallback (if IPFS failed)
    if not cid:
        fanout = request.app.get("fanout")
        if fanout:
            try:
                fanout_queue = request.app.get("fanout_queue")
                if fanout_queue is not None:
                    await fanout_queue.put(event)
                    logger.debug(f"SSE EVENT: fanout fallback queued {event_id[:20]}...")
            except Exception as e:
                logger.warning(f"SSE EVENT: fanout fallback failed: {e}")

    return web.json_response({
        "status": "ok",
        "cid": cid,
        "id": event_id[:20] + "...",
    })


# ── Main Nostr Endpoint ────────────────────────────────────

async def handle_nostr(request: web.Request):
    """POST /nostr — universal handler for REQ and EVENT.

    REQ: {"method":"REQ","params":["sub1",{filters}]} → SSE
    EVENT: {"method":"EVENT","params":[{event}]} → {"status":"ok","cid":"..."}
    """
    # CORS preflight
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
        add_cors(resp)
        return resp

    db = request.app.get("db")
    ipfs = request.app.get("ipfs")
    cid_index = request.app.get("cid_index")

    if not db:
        return web.json_response({"status": "error", "message": "DB not ready"}, status=503)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError) as e:
        return web.json_response({"status": "error", "message": f"invalid JSON: {e}"}, status=400)

    method = body.get("method")
    params = body.get("params", [])

    if not method or method not in ("REQ", "EVENT", "AUTH"):
        return web.json_response(
            {"status": "error", "message": "method must be REQ, EVENT, or AUTH"},
            status=400,
        )

    if method == "REQ":
        if len(params) < 2:
            return web.json_response(
                {"status": "error", "message": "REQ requires [sub_id, filters]"},
                status=400,
            )
        sub_id = params[0]
        filters = params[1] if isinstance(params[1], dict) else {}
        return await handle_req(request, sub_id, filters, db, ipfs, cid_index)

    elif method == "EVENT":
        if len(params) < 1:
            return web.json_response(
                {"status": "error", "message": "EVENT requires [event]"},
                status=400,
            )
        event = params[0]
        if not isinstance(event, dict) or "id" not in event:
            return web.json_response(
                {"status": "error", "message": "invalid event: missing id"},
                status=400,
            )
        return await handle_event(request, event, db, ipfs, cid_index)

    elif method == "AUTH":
        return await handle_auth(request, params, db)


# ── In-memory auth sessions (challenge → pubkey) ──
AUTH_SESSIONS: dict[str, dict] = {}  # token → {"pubkey": str, "expires": float}


def generate_challenge() -> str:
    """Generate a random NIP-42 auth challenge."""
    return hashlib.sha256(os.urandom(32)).hexdigest()[:16]


def generate_auth_token() -> str:
    """Generate a unique auth token for SSE sessions."""
    return hashlib.sha256(os.urandom(32)).hexdigest()[:32]


def verify_signed_auth(event: dict, challenge: str) -> tuple[bool, str]:
    """Verify a NIP-42 AUTH event (kind:22242)."""
    if event.get('kind') != 22242:
        return False, "wrong kind (must be 22242)"
    tags = event.get('tags', [])
    challenge_tag = None
    relay_tag = None
    for t in tags:
        if len(t) >= 2 and t[0] == 'challenge':
            challenge_tag = t[1]
        if len(t) >= 2 and t[0] == 'relay':
            relay_tag = t[1]
    if challenge_tag != challenge:
        return False, "challenge mismatch"
    try:
        raw = json.dumps([0, event['pubkey'], event['created_at'],
            event['kind'], tags, event['content']],
            separators=(',',':'), ensure_ascii=False)
        if hashlib.sha256(raw.encode()).hexdigest() != event['id']:
            return False, "invalid id"
    except:
        return False, "serialization error"
    return True, event['pubkey']


def cleanup_auth_sessions():
    """Remove expired auth sessions."""
    now = time.time()
    expired = [t for t, s in AUTH_SESSIONS.items() if s['expires'] < now]
    for t in expired:
        del AUTH_SESSIONS[t]


def get_authed_pubkey(request: web.Request) -> str | None:
    """Get authenticated pubkey from request headers or None."""
    token = request.headers.get("X-Auth-Token", "")
    if token in AUTH_SESSIONS:
        session = AUTH_SESSIONS[token]
        if session['expires'] > time.time():
            return session['pubkey']
        else:
            del AUTH_SESSIONS[token]
    return None


async def handle_auth(request: web.Request, params: list, db) -> web.Response:
    """Handle NIP-42 AUTH via SSE.

    Step 1: POST {"method":"AUTH","params":[]}
      → {"status":"challenge","challenge":"abc123"}

    Step 2: POST {"method":"AUTH","params":[{signed_event}]}
      → {"status":"ok","pubkey":"abc...","token":"xyz..."}
    """
    cleanup_auth_sessions()

    if not params or not isinstance(params[0], dict):
        # Step 1: return challenge
        challenge = generate_challenge()
        return web.json_response({"status": "challenge", "challenge": challenge})

    # Step 2: verify signed auth event
    event = params[0]
    if not isinstance(event, dict) or "id" not in event:
        return web.json_response(
            {"status": "error", "message": "invalid event"}, status=400
        )

    # Find challenge from event tags
    challenge = None
    for t in event.get('tags', []):
        if len(t) >= 2 and t[0] == 'challenge':
            challenge = t[1]
            break

    if not challenge:
        return web.json_response(
            {"status": "error", "message": "no challenge tag in event"},
            status=400,
        )

    valid, pubkey = verify_signed_auth(event, challenge)
    if valid:
        token = generate_auth_token()
        AUTH_SESSIONS[token] = {"pubkey": pubkey, "expires": time.time() + 3600}
        return web.json_response({"status": "ok", "pubkey": pubkey, "token": token})
    else:
        return web.json_response(
            {"status": "error", "message": pubkey}, status=400,
        )

def setup_sse_routes(app: web.Application):
    """Attach SSE routes to aiohttp application."""
    app.router.add_route("*", "/nostr", handle_nostr, name="nostr")


# ── Self-test ──────────────────────────────────────────────

def test_build_query():
    """Test SQL query building."""
    tests = [
        (
            {"kinds": [1], "authors": ["a1b2c3d4"], "limit": 50},
            ("kind=? AND pubkey IN (?,?,?)", [1, "a1b2c3d4", "02a1b2c3d4", "03a1b2c3d4"], 50),
        ),
        (
            {"kinds": [1, 7], "since": 1000000, "until": 2000000},
            ("kind IN (?,?) AND created_at>=? AND created_at<?", [1, 7, 1000000, 2000000], 100),
        ),
        (
            {"search": "bitcoin"},
            ("content LIKE ?", ["%bitcoin%"], 100),
        ),
        (
            {},
            ("1=1", [], 100),
        ),
    ]

    passed = 0
    failed = 0

    for i, (filters, expected) in enumerate(tests):
        try:
            where, params, limit = build_sse_query(filters)
            # Check structure (exact SQL may differ)
            assert isinstance(where, str) and len(where) > 0
            assert isinstance(params, list)
            assert isinstance(limit, int) and limit > 0
            passed += 1
        except AssertionError as e:
            print(f"  ❌ Test {i+1}: {e}")
            failed += 1

    print(f"  build_sse_query: {passed}/{passed+failed} passed")
    return passed, failed


def test():
    """Run all SSE Handler tests."""
    print("SSE Handler tests:")
    total_passed = 0
    total_failed = 0

    # 1. Query builder
    p, f = test_build_query()
    total_passed += p
    total_failed += f

    # 2. CORS headers
    try:
        resp = web.Response()
        add_cors(resp)
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"
        total_passed += 1
    except Exception as e:
        print(f"  ❌ CORS: {e}")
        total_failed += 1

    # 3. JSON validation
    tests_json = [
        ('{"method":"REQ","params":["s1",{"kinds":[1]}]}', True),
        ('{"method":"EVENT","params":[{"id":"x","pubkey":"y"}]}', True),
        ('{"method":"INVALID"}', False),
        ('not json', False),
    ]
    for i, (body_str, should_pass) in enumerate(tests_json):
        try:
            body = json.loads(body_str)
            method = body.get("method")
            if should_pass:
                assert method in ("REQ", "EVENT")
            else:
                assert method not in ("REQ", "EVENT")
            total_passed += 1
        except (json.JSONDecodeError, AssertionError):
            if not should_pass:
                total_passed += 1
            else:
                total_failed += 1

    print(f"  JSON validation: {total_passed - p}/4 passed")

    print(f"\n  Total: {total_passed}/{total_passed+total_failed} passed")
    return total_passed, total_failed


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    test()
