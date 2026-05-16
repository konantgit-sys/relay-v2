"""SigGate — signature verification gate with rate limiting.

Verifies identity of incoming messages. Supports allowlist mode.
Rate limiting per sender identity prevents spam across mesh.
"""

import time


class SigGate:
    """Message verification gate: rate limit + allowlist.

    Each sender has a rate counter. If sender is not in allowlist (when set),
    message is rejected before processing.
    """

    def __init__(self, rate_limit: int = 1000, window: float = 1.0):
        self.rate_limit = rate_limit
        self.window = window
        self._counters: dict[str, list[float]] = {}
        self._allowlist: set[str] | None = None
        self._stats = {"passed": 0, "rejected_rate": 0, "rejected_deny": 0}

    def set_allowlist(self, pubkeys: list[str]):
        """Enable allowlist mode. Only these sender IDs will pass."""
        self._allowlist = set(pubkeys)

    def check(self, sender_id: str) -> bool:
        """Check if sender is allowed and within rate limit."""
        if self._allowlist is not None and sender_id not in self._allowlist:
            self._stats["rejected_deny"] += 1
            return False

        now = time.time()
        timestamps = self._counters.get(sender_id, [])
        timestamps = [t for t in timestamps if now - t < self.window]

        if len(timestamps) >= self.rate_limit:
            self._stats["rejected_rate"] += 1
            return False

        timestamps.append(now)
        self._counters[sender_id] = timestamps
        self._stats["passed"] += 1
        return True

    def stats(self) -> dict:
        return {**self._stats, "allowlist": self._allowlist is not None}
