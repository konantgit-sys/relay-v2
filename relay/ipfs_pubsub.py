"""
SNIN Relay — IPFS Pubsub Engine
Обёртка над IPFS CLI для публикации/подписки Nostr событий.

Использует ipfs CLI (не API), т.к. pubsub в kubo 0.32.0
работает стабильно только через CLI.
"""

import asyncio
import json
import logging
import os
import time

logger = logging.getLogger('ipfs_pubsub')

IPFS_BIN = os.getenv("IPFS_BIN", "ipfs")
TOPIC = os.getenv("IPFS_TOPIC", "snin-dao")
DEFAULT_ENV = {
    "HOME": os.environ.get("HOME", "/root"),
    "IPFS_PATH": os.getenv("IPFS_PATH", os.path.expanduser("~/.ipfs")),
    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
}


class IPFSPubsub:
    """IPFS pubsub publish/subscribe для Nostr событий."""

    def __init__(self, ipfs_bin=IPFS_BIN, topic=TOPIC):
        self.ipfs = ipfs_bin
        self.topic = topic
        self._published = 0
        self._received = 0
        self._peers = 0
        self._last_peer_check = 0

    async def add_event(self, event: dict) -> str:
        """Nostr event → IPFS объект → CID.
        Возвращает CID строку."""
        obj = {
            "nostr": {
                "id": event["id"],
                "pubkey": event["pubkey"],
                "created_at": event["created_at"],
                "kind": event["kind"],
                "tags": event.get("tags", []),
                "content": event.get("content", ""),
                "sig": event["sig"]
            },
            "meta": {
                "source_relay": "snin-relay.v2.site",
                "published_at": int(time.time())
            }
        }
        data = json.dumps(obj, separators=(',', ':'), ensure_ascii=False).encode()
        
        proc = await asyncio.create_subprocess_exec(
            self.ipfs, "add", "-Q", "--pin=false",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=DEFAULT_ENV
        )
        stdout, stderr = await proc.communicate(data)
        if proc.returncode != 0:
            raise RuntimeError(f"ipfs add failed: {stderr.decode()}")
        cid = stdout.decode().strip()
        logger.debug(f"IPFS add: {cid} ({len(data)} bytes)")
        return cid

    async def get_event(self, cid: str) -> dict | None:
        """CID → IPFS объект → Nostr event."""
        proc = await asyncio.create_subprocess_exec(
            self.ipfs, "cat", cid,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=DEFAULT_ENV
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(f"IPFS cat {cid}: {stderr.decode()[:100]}")
            return None
        try:
            obj = json.loads(stdout)
            return obj.get("nostr")
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"IPFS decode: {e}")
            return None

    async def publish_cid(self, cid: str) -> bool:
        """Опубликовать CID в pubsub topic."""
        proc = await asyncio.create_subprocess_exec(
            self.ipfs, "pubsub", "pub", self.topic,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=DEFAULT_ENV
        )
        stdout, stderr = await proc.communicate(cid.encode())
        if proc.returncode != 0:
            logger.warning(f"IPFS pubsub pub: {stderr.decode()[:100]}")
            return False
        self._published += 1
        return True

    async def publish_event(self, event: dict) -> str:
        """Полный цикл: event → add → pubsub pub.
        Возвращает CID."""
        cid = await self.add_event(event)
        ok = await self.publish_cid(cid)
        if not ok:
            raise RuntimeError(f"Failed to publish CID {cid}")
        logger.info(f"IPFS published: {cid} (kind={event.get('kind')})")
        return cid

    async def subscribe_loop(self, on_event_callback, on_error=None):
        """Подписка на topic. Бесконечный цикл.
        on_event_callback(event) — вызывается для каждого события.
        """
        logger.info(f"IPFS subscribing to topic '{self.topic}'...")
        while True:
            try:
                proc = await asyncio.create_subprocess_exec(
                    self.ipfs, "pubsub", "sub", self.topic,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=DEFAULT_ENV
                )
                # Читаем CID-строки из stdout (по одной в строке)
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    cid = line.decode().strip()
                    if not cid:
                        continue
                    self._received += 1
                    logger.debug(f"IPFS received CID: {cid}")
                    try:
                        event = await self.get_event(cid)
                        if event:
                            await on_event_callback(event)
                        else:
                            # CID есть, но не содержит Nostr event
                            # (может быть от другой системы)
                            pass
                    except Exception as e:
                        logger.warning(f"Process CID {cid}: {e}")
                        if on_error:
                            await on_error(cid, e)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"IPFS sub error: {e}")
                if on_error:
                    await on_error(None, e)
                await asyncio.sleep(5)  # переподключение

    async def get_peers(self) -> int:
        """Количество пиров в swarm."""
        now = time.time()
        if now - self._last_peer_check < 60:
            return self._peers
        try:
            proc = await asyncio.create_subprocess_exec(
                self.ipfs, "swarm", "peers",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=DEFAULT_ENV
            )
            stdout, _ = await proc.communicate()
            count = len([l for l in stdout.decode().strip().split('\n') if l.strip()])
            self._peers = count
            self._last_peer_check = now
        except Exception:
            pass
        return self._peers

    def get_stats(self) -> dict:
        return {
            "published": self._published,
            "received": self._received,
            "peers": self._peers,
            "topic": self.topic,
            "ipfs_bin": self.ipfs,
        }
