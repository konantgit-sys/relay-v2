"""
SNIN Agent Heartbeat Daemon (kind:19000)
Publishes agent health status to snin-relay every 10 minutes.

Custom NIP:
  kind: 19000 — Agent Heartbeat
  content: JSON {status, agent, events_count, authors, timestamp}
  tags:
    p: agent pubkey
    status: active|idle|degraded|dead
    events: event count
    name: agent name
"""

import asyncio
import json
import logging
import os
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('heartbeat')

# ── Config ──
AGENTS_REGISTRY = "/home/agent/data/agents_registry"
RELAY_WS = "ws://127.0.0.1:8198"  # local connection
RELAY_API = "http://127.0.0.1:8198"
HEARTBEAT_INTERVAL = 600  # 10 minutes

# Agent name mapping (directory -> display name)
AGENT_NAMES = {
    "analyst_ai": "analyst",
    "anton_ai": "anton",
    "aporialab": "aporia",
    "archivist_ai": "archivist",
    "axiom": "axiom",
    "cryptoantology": "cryptontology",
    "cryter": "cryter",
    "director_ai": "director",
    "executor_ai": "executor",
    "forecaster_ai": "forecaster",
    "marketing_ai": "marketing",
    "rnd_ai": "randd",
    "security_ai": "security",
    "strategist_ai": "strategist",
    "support_ai": "support",
    "unknown_1": "aiantology",
}


def get_relay_stats() -> dict:
    try:
        import urllib.request
        resp = urllib.request.urlopen(f"{RELAY_API}/api/stats", timeout=3)
        return json.loads(resp.read())
    except Exception as e:
        logger.error(f"Stats fetch error: {e}")
        return {"events": 0, "authors": 0}


async def publish_event(event_dict: dict) -> bool:
    """Publish a Nostr event to the local relay via WS."""
    try:
        import websockets
        async with websockets.connect(RELAY_WS, max_size=10*1024*1024, open_timeout=5) as ws:
            msg = json.dumps(["EVENT", event_dict])
            await ws.send(msg)
            resp = await asyncio.wait_for(ws.recv(), timeout=5)
            data = json.loads(resp)
            if data[0] == "OK" and data[2] is True:
                return True
            logger.warning(f"Rejected: {data[3] if len(data)>3 else 'unknown'}")
            return False
    except Exception as e:
        logger.error(f"WS error: {e}")
        return False


def load_valid_agents() -> list[dict]:
    """Load agents with valid nsec keys only."""
    from nostr_protocol import Keys
    
    agents = []
    for dirname in sorted(os.listdir(AGENTS_REGISTRY)):
        keys_path = os.path.join(AGENTS_REGISTRY, dirname, "keys.json")
        if not os.path.exists(keys_path):
            continue
        try:
            with open(keys_path) as f:
                keys = json.load(f)
            nsec = keys.get("nsec", "")
            if not nsec or not nsec.startswith("nsec"):
                continue
            k = Keys.parse(nsec)
            pubhex = k.public_key().to_hex()
            agents.append({
                "dir": dirname,
                "name": AGENT_NAMES.get(dirname, dirname),
                "nsec": nsec,
                "pubhex": pubhex,
            })
        except Exception as e:
            logger.debug(f"Skipping {dirname}: {e}")
    
    logger.info(f"Loaded {len(agents)} valid agents from registry")
    return agents


def create_heartbeat(agent: dict, stats: dict) -> dict:
    """Create and sign a kind:19000 heartbeat event.
    Returns the event dict ready for publish."""
    from nostr_protocol import Keys, Tag, Kind, EventBuilder
    
    k = Keys.parse(agent["nsec"])
    
    content = json.dumps({
        "status": "active",
        "agent": agent["name"],
        "events_count": stats.get("events", 0),
        "authors": stats.get("authors", 0),
        "timestamp": int(time.time()),
    })
    
    tags = [
        Tag.parse(["p", agent["pubhex"]]),
        Tag.parse(["status", "active"]),
        Tag.parse(["events", str(stats.get("events", 0))]),
        Tag.parse(["name", agent["name"]]),
        Tag.parse(["version", "v2.1.0"]),
    ]
    
    evt = EventBuilder(Kind(19000), content, tags).to_event(k)
    return json.loads(evt.as_json())


async def heartbeat_cycle(agents: list[dict]):
    """Run one heartbeat cycle — all agents ping the relay."""
    stats = get_relay_stats()
    success, failed = 0, 0
    
    for agent in agents:
        try:
            event_dict = create_heartbeat(agent, stats)
            if await publish_event(event_dict):
                success += 1
                logger.info(f"❤️ {agent['name']}: HB sent ({agent['pubhex'][:12]}...)")
            else:
                failed += 1
                logger.warning(f"💔 {agent['name']}: HB rejected")
        except Exception as e:
            failed += 1
            logger.error(f"💔 {agent['name']}: error: {e}")
        
        await asyncio.sleep(0.3)  # gentle rate limit
    
    logger.info(f"❤️ Cycle done: {success} sent, {failed} failed")


async def main():
    logger.info(f"❤️ Heartbeat Daemon v2.1.0 starting...")
    logger.info(f"   Relay: {RELAY_WS} | Interval: {HEARTBEAT_INTERVAL}s ({HEARTBEAT_INTERVAL//60} min)")
    
    agents = load_valid_agents()
    if not agents:
        logger.error("No valid agents! Exiting.")
        return
    
    logger.info(f"   Agents: {', '.join(a['name'] for a in agents)}")
    
    # First cycle immediately
    await heartbeat_cycle(agents)
    
    # Then periodic
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        await heartbeat_cycle(agents)


if __name__ == "__main__":
    asyncio.run(main())
