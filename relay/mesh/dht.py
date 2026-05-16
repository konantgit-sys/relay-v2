"""DHTStore — Redis-backed distributed hash table for peer discovery.

In-memory fallback on Redis failure. TTL-based key expiry.
Used by mesh nodes to discover peers and share routing metadata.
"""

import json
import time
from collections import OrderedDict


class DHTStore:
    """KV store via Redis — single source of truth for mesh peers.

    Peers write DHT directly to Redis (keys dht:*),
    other nodes read from same store. In-memory fallback on Redis failure.
    """

    def __init__(self, node_id: str, redis_client=None, max_keys: int = 1000):
        self.node_id = node_id
        self._cache: OrderedDict[str, dict] = OrderedDict()
        self._max_keys = max_keys
        self._ops: int = 0
        self._redis = redis_client
        self._redis_ok = False

    def _r(self):
        """Lazy Redis connection."""
        if self._redis is None:
            try:
                import redis as redis_py
                self._redis = redis_py.Redis(
                    host='localhost', port=6379, db=0,
                    socket_connect_timeout=1, socket_timeout=1,
                    decode_responses=False,
                )
                self._redis.ping()
                self._redis_ok = True
            except Exception:
                self._redis_ok = False
        return self._redis if self._redis_ok else None

    def put(self, key: str, value, ttl: int = 86400, source: str = "local"):
        self._ops += 1
        expires = time.time() + ttl
        entry = {"value": value, "expires": expires, "source": source}

        r = self._r()
        if r:
            try:
                r.setex(f"dht:{key}", ttl, json.dumps(entry))
            except Exception:
                pass

        self._cache[key] = entry
        while len(self._cache) > self._max_keys:
            self._cache.popitem(last=False)
        return {"status": "ok", "key": key, "ttl": ttl}

    def get(self, key: str) -> dict | None:
        self._ops += 1
        r = self._r()
        if r:
            try:
                raw = r.get(f"dht:{key}")
                if raw:
                    return {"key": key, "value": json.loads(raw)["value"], "source": "redis"}
            except Exception:
                pass

        entry = self._cache.get(key)
        if entry is None:
            return None
        if entry["expires"] < time.time():
            del self._cache[key]
            return None
        return {"key": key, "value": entry["value"], "source": entry["source"]}

    def keys(self, prefix: str = "") -> list[str]:
        r = self._r()
        if r:
            try:
                pattern = f"dht:{prefix}*" if prefix else "dht:*"
                return [k.decode().replace("dht:", "") for k in r.keys(pattern)]
            except Exception:
                pass
        return [k for k in self._cache if k.startswith(prefix)]

    def stats(self) -> dict:
        r = self._r()
        redis_keys = 0
        redis_ok = False
        if r:
            try:
                redis_keys = len(r.keys("dht:*"))
                redis_ok = True
            except Exception:
                pass
        return {
            "keys": len(self._cache),
            "redis_keys": redis_keys,
            "redis_ok": redis_ok,
            "max": self._max_keys,
            "ops": self._ops,
            "expired": sum(1 for e in self._cache.values() if e["expires"] < time.time()),
        }
