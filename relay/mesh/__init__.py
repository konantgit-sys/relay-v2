"""relay.mesh — Mesh networking module for relay nodes.

Enables direct peer-to-peer communication between relay instances
using Redis-backed DHT for discovery and SigGate for rate limiting.

Example:
    from relay.mesh.dht import DHTStore
    from relay.mesh.sig_gate import SigGate

    dht = DHTStore("node-1")
    dht.put("peer:test", {"host": "10.0.0.1", "port": 9907})
"""

from .dht import DHTStore
from .sig_gate import SigGate

__all__ = ["DHTStore", "SigGate"]
