#!/usr/bin/env python3
"""
SNIN Relay Monitor Daemon — Telegram alerts for relay health.

Checks every 60 seconds: WSS, SSE, DB, memory, disk, IPFS, last event.
Sends Telegram alert on problem, recovery alert when resolved.

Config via environment:
  TG_BOT_TOKEN     — Telegram bot token (from @BotFather)
  TG_CHAT_ID       — Telegram chat ID to send alerts to
  CHECK_INTERVAL   — seconds between checks (default: 60)
  ALERT_COOLDOWN   — seconds between repeat alerts (default: 300)
  RELAY_HOST       — relay host (default: 127.0.0.1)
  RELAY_WSS_PORT   — WSS port (default: 8198)
  RELAY_SSE_URL    — SSE endpoint (default: http://127.0.0.1:8198/nostr)
"""

import asyncio
import json
import logging
import os
import socket
import sqlite3
import time
import struct
import sys
from pathlib import Path
from datetime import datetime, timezone
import urllib.request

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('relay_monitor')

# ── Config from environment ──
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))
ALERT_COOLDOWN = int(os.environ.get("ALERT_COOLDOWN", "300"))
RELAY_HOST = os.environ.get("RELAY_HOST", "127.0.0.1")
RELAY_WSS_PORT = int(os.environ.get("RELAY_WSS_PORT", "8198"))
RELAY_API = f"http://{RELAY_HOST}:{RELAY_WSS_PORT}"
RELAY_SSE_URL = f"{RELAY_API}/nostr"
DB_PATH = "/home/agent/data/sites/relay/relay_v2.db"
MEMORY_WARN_MB = int(os.environ.get("MEMORY_WARN_MB", "500"))
DISK_WARN_PCT = int(os.environ.get("DISK_WARN_PCT", "10"))
IPFS_MIN_PEERS = int(os.environ.get("IPFS_MIN_PEERS", "5"))
EVENT_STALE_MIN = int(os.environ.get("EVENT_STALE_MIN", "360"))

# ── State ──
last_alert_time = 0
last_errors = {}  # check_name -> was_error
total_checks = 0
total_alerts = 0


def tg_send(text: str) -> bool:
    """Send message to Telegram. Returns True if sent."""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return False
    try:
        import urllib.request
        data = json.dumps({
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode()
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status == 200
    except Exception as e:
        logger.error(f"Telegram send error: {e}")
        return False


def check_wss() -> dict:
    """Check WSS port TCP connectivity."""
    start = time.time()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect((RELAY_HOST, RELAY_WSS_PORT))
        sock.close()
        latency = int((time.time() - start) * 1000)
        return {"ok": True, "latency_ms": latency, "error": ""}
    except Exception as e:
        return {"ok": False, "latency_ms": 0, "error": str(e)}


def check_sse() -> dict:
    """Check SSE endpoint responds with 200."""
    start = time.time()
    try:
        payload = json.dumps({"method": "REQ", "params": ["test", {"kinds": [1], "limit": 1}]}).encode()
        req = urllib.request.Request(RELAY_SSE_URL, data=payload, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=5)
        latency = int((time.time() - start) * 1000)
        return {"ok": resp.status == 200, "latency_ms": latency, "error": f"HTTP {resp.status}" if resp.status != 200 else ""}
    except Exception as e:
        return {"ok": False, "latency_ms": 0, "error": str(e)}


def check_db() -> dict:
    """Check SQLite responds quickly."""
    start = time.time()
    try:
        conn = sqlite3.connect(DB_PATH, timeout=2)
        conn.execute("SELECT COUNT(*) FROM events")
        conn.close()
        latency = int((time.time() - start) * 1000)
        return {"ok": True, "latency_ms": latency, "error": ""}
    except Exception as e:
        return {"ok": False, "latency_ms": 0, "error": str(e)}


def check_memory() -> dict:
    """Check RSS memory usage."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    mb = kb // 1024
                    return {"ok": mb < MEMORY_WARN_MB, "usage_mb": mb, "warn_at_mb": MEMORY_WARN_MB, "error": ""}
    except:
        pass
    return {"ok": True, "usage_mb": 0, "warn_at_mb": MEMORY_WARN_MB, "error": "no /proc/self/status"}


def check_disk() -> dict:
    """Check disk space for relay data."""
    try:
        st = os.statvfs("/home/agent/data/")
        free_pct = (st.f_bavail * st.f_frsize * 100) / (st.f_blocks * st.f_frsize)
        return {"ok": free_pct > DISK_WARN_PCT, "free_pct": round(free_pct, 1), "warn_below_pct": DISK_WARN_PCT, "error": ""}
    except Exception as e:
        return {"ok": True, "free_pct": 0, "warn_below_pct": DISK_WARN_PCT, "error": str(e)}


def check_ipfs() -> dict:
    """Check IPFS swarm peer count."""
    try:
        # Проверяем доступен ли ipfs
        which = os.popen("which ipfs 2>/dev/null").read().strip()
        if not which:
            return {"ok": True, "peers": 0, "min_peers": IPFS_MIN_PEERS, "error": "ipfs not installed"}
        result = os.popen("ipfs swarm peers 2>/dev/null | wc -l").read().strip()
        peers = int(result) if result else 0
        return {"ok": peers >= IPFS_MIN_PEERS, "peers": peers, "min_peers": IPFS_MIN_PEERS, "error": ""}
    except Exception as e:
        return {"ok": True, "peers": 0, "min_peers": IPFS_MIN_PEERS, "error": str(e)}


def check_last_event() -> dict:
    """Check time since last event stored in relay."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=2)
        cur = conn.execute("SELECT MAX(created_at) FROM events")
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            last_ts = row[0]
            now = int(time.time())
            minutes_ago = (now - last_ts) // 60
            return {"ok": minutes_ago < EVENT_STALE_MIN, "minutes_ago": minutes_ago, "warn_after_min": EVENT_STALE_MIN, "error": ""}
        return {"ok": True, "minutes_ago": 0, "warn_after_min": EVENT_STALE_MIN, "error": "no events"}
    except Exception as e:
        return {"ok": True, "minutes_ago": 0, "warn_after_min": EVENT_STALE_MIN, "error": str(e)}


def run_checks() -> dict:
    """Run all checks and return results."""
    checks = {
        "WSS порт": check_wss(),
        "SSE эндпоинт": check_sse(),
        "База данных": check_db(),
        "Память (RSS)": check_memory(),
        "Диск": check_disk(),
        "IPFS пиры": check_ipfs(),
        "Последнее событие": check_last_event(),
    }
    return checks


def format_alert_message(checks: dict) -> str:
    """Format problem alert for Telegram."""
    lines = ["🔴 <b>SNIN RELAY — ПРОБЛЕМА</b>", "┌───────────────────────"]
    for name, result in checks.items():
        if not result["ok"]:
            icon = "❌"
            detail = result.get("error", "")
            if "latency_ms" in result:
                detail = f"{result.get('latency_ms', '?')}ms"
            lines.append(f"│ {icon} {name}: {detail}")
        else:
            icon = "✅"
            detail = ""
            if "latency_ms" in result and result["latency_ms"] > 0:
                detail = f" ({result['latency_ms']}ms)"
            elif "usage_mb" in result and result["usage_mb"] > 0:
                detail = f" ({result['usage_mb']} MB)"
            elif "free_pct" in result and result["free_pct"] > 0:
                detail = f" ({result['free_pct']}%)"
            elif "peers" in result and result["peers"] > 0:
                detail = f" ({result['peers']})"
            elif "minutes_ago" in result and result["minutes_ago"] > 0:
                detail = f" ({result['minutes_ago']} мин)"
            lines.append(f"│ {icon} {name}{detail}")
    lines.append("└───────────────────────")
    return "\n".join(lines)


def format_recovery_message(checks: dict, resolved: list) -> str:
    """Format recovery alert for Telegram."""
    lines = ["🟢 <b>SNIN RELAY — ВОССТАНОВЛЕНО</b>"]
    for item in resolved:
        lines.append(f"✅ {item}")
    lines.append("")
    all_ok = all(c["ok"] for c in checks.values())
    if all_ok:
        lines.append("Все проверки пройдены ✅")
    else:
        remaining = [n for n, c in checks.items() if not c["ok"]]
        lines.append(f"Ещё проблемы: {', '.join(remaining)}")
    return "\n".join(lines)


def format_status_report(checks: dict) -> str:
    """Format full status report."""
    lines = ["📊 <b>SNIN RELAY — СТАТУС</b>", "┌───────────────────────"]
    for name, result in checks.items():
        icon = "✅" if result["ok"] else "❌" if result.get("error") else "⚠️"
        detail = ""
        if "latency_ms" in result and result["latency_ms"] > 0:
            detail = f" {result['latency_ms']}ms"
        elif "usage_mb" in result and result["usage_mb"] > 0:
            detail = f" {result['usage_mb']} MB"
        elif "free_pct" in result and result["free_pct"] > 0:
            detail = f" {result['free_pct']}%"
        elif "peers" in result and result.get("peers", 0) > 0 or result.get("peers", 0) == 0:
            detail = f" {result.get('peers', '?')}"
        elif "minutes_ago" in result and result["minutes_ago"] >= 0:
            detail = f" {result['minutes_ago']} мин"
        if result.get("error"):
            detail += f" — {result['error']}"
        lines.append(f"│ {icon} {name}{detail}")
    lines.append("└───────────────────────")
    return "\n".join(lines)


def should_alert(checks: dict) -> bool:
    """Check if any check failed."""
    return any(not c["ok"] for c in checks.values())


async def monitor_cycle():
    """Run one monitoring cycle."""
    global last_alert_time, total_checks, total_alerts
    
    checks = run_checks()
    total_checks += 1
    has_problem = should_alert(checks)
    now = time.time()
    
    # Log problems
    for name, result in checks.items():
        was_error = last_errors.get(name, False)
        is_error = not result["ok"]
        
        if is_error and not was_error:
            logger.warning(f"⚠️ {name}: {result.get('error', 'unknown')}")
        elif not is_error and was_error:
            logger.info(f"✅ {name}: recovered")
        
        last_errors[name] = is_error
    
    # Determine if we need to send Telegram
    if has_problem and (now - last_alert_time) > ALERT_COOLDOWN:
        msg = format_alert_message(checks)
        if tg_send(msg):
            last_alert_time = now
            total_alerts += 1
            logger.info(f"🔴 Alert sent to Telegram")
    elif has_problem:
        pass  # In cooldown, skip
    else:
        # Check if we just recovered from previous errors
        if any(not c["ok"] for n, c in checks.items()):
            pass  # Still partially broken
        elif any(last_errors.values()):
            # We recovered! Send recovery
            resolved = [n for n, c in last_errors.items() if c and checks.get(n, {}).get("ok", True)]
            if resolved:
                msg = format_recovery_message(checks, resolved)
                if tg_send(msg):
                    logger.info(f"🟢 Recovery sent to Telegram")
                # Clear all errors
                for k in last_errors:
                    last_errors[k] = False
    
    # Log summary
    ok_count = sum(1 for c in checks.values() if c["ok"])
    total = len(checks)
    status = "✅" if ok_count == total else f"⚠️ {total - ok_count}/{total} проблем"
    logger.info(f"📊 Check #{total_checks}: {status} ({ok_count}/{total})")


async def main():
    logger.info("=" * 50)
    logger.info("🔍 SNIN Relay Monitor Daemon v1.0")
    logger.info(f"   Relay: {RELAY_HOST}:{RELAY_WSS_PORT}")
    logger.info(f"   DB: {DB_PATH}")
    logger.info(f"   Interval: {CHECK_INTERVAL}s | Cooldown: {ALERT_COOLDOWN}s")
    
    if TG_BOT_TOKEN and TG_CHAT_ID:
        logger.info(f"   Telegram: ✅ configured")
        # Test Telegram
        test_msg = "🚀 <b>SNIN Relay Monitor</b> запущен"
        if tg_send(test_msg):
            logger.info("   Telegram test: ✅ sent")
        else:
            logger.warning("   Telegram test: ❌ failed — check TG_BOT_TOKEN and TG_CHAT_ID")
    else:
        logger.warning("   ⚠️ Telegram not configured. Set TG_BOT_TOKEN and TG_CHAT_ID env vars.")
        logger.warning("   Monitoring will run without notifications.")
    
    logger.info("=" * 50)
    
    # First check immediately
    await monitor_cycle()
    
    # Then periodic
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        await monitor_cycle()


if __name__ == "__main__":
    asyncio.run(main())
