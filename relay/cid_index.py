"""
SNIN Relay — CID Index
SQLite index for event.id → IPFS CID lookup.

Features:
- Lookup CID by event.id (retrieve event from IPFS)
- Find CID list by pubkey (agent history)
- Find events by kind (type filter)
"""

import json
import logging
import os
import sqlite3
import time

logger = logging.getLogger('cid_index')

DB_PATH_DEFAULT = os.path.join(os.path.dirname(__file__), "relay_v2.db")


class CIDIndex:
    """CID Index — maps Nostr event.id to IPFS CID."""

    def __init__(self, db_path: str = DB_PATH_DEFAULT):
        self.db_path = db_path
        self._db: sqlite3.Connection | None = None
        self._init_db()

    def _init_db(self):
        self._db = sqlite3.connect(self.db_path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS cid_index (
                event_id TEXT PRIMARY KEY,
                cid TEXT NOT NULL,
                pubkey TEXT NOT NULL DEFAULT '',
                kind INTEGER NOT NULL DEFAULT -1,
                created_at INTEGER NOT NULL DEFAULT 0,
                stored_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
            )
        """)
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_cid_pubkey ON cid_index(pubkey)")
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_cid_kind ON cid_index(kind)")
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_cid_created ON cid_index(created_at)")
        self._db.commit()
        logger.debug(f"CID Index: {self._get_count()} records")

    def _get_count(self):
        return self._db.execute("SELECT COUNT(*) FROM cid_index").fetchone()[0]

    def add(self, event_id: str, cid: str, pubkey: str = "",
            kind: int = -1, created_at: int = 0) -> bool:
        """Add event_id → CID record.
        If record exists — update (UPSERT)."""
        try:
            self._db.execute("""
                INSERT INTO cid_index (event_id, cid, pubkey, kind, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    cid=excluded.cid,
                    stored_at=strftime('%s','now')
            """, (event_id, cid, pubkey, kind, created_at))
            self._db.commit()
            return True
        except Exception as e:
            logger.error(f"CID add error: {e}")
            return False

    def add_batch(self, entries: list[tuple]) -> int:
        """Batch add: [(event_id, cid, pubkey, kind, created_at), ...].
        Returns count added."""
        count = 0
        try:
            self._db.executemany("""
                INSERT OR IGNORE INTO cid_index (event_id, cid, pubkey, kind, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, entries)
            self._db.commit()
            count = self._db.total_changes
        except Exception as e:
            logger.error(f"CID batch add error: {e}")
        return count

    def get_by_event_id(self, event_id: str) -> str | None:
        """Find CID by event.id."""
        row = self._db.execute(
            "SELECT cid FROM cid_index WHERE event_id = ?", (event_id,)
        ).fetchone()
        return row["cid"] if row else None

    def get_by_pubkey(self, pubkey: str, limit: int = 20) -> list[dict]:
        """Find CID by pubkey (newest to oldest)."""
        rows = self._db.execute("""
            SELECT event_id, cid, kind, created_at
            FROM cid_index
            WHERE pubkey = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (pubkey, limit)).fetchall()
        return [dict(r) for r in rows]

    def get_by_kind(self, kind: int, limit: int = 50) -> list[dict]:
        """Find CID by kind."""
        rows = self._db.execute("""
            SELECT event_id, cid, pubkey, created_at
            FROM cid_index
            WHERE kind = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (kind, limit)).fetchall()
        return [dict(r) for r in rows]

    def get_recent(self, limit: int = 20) -> list[dict]:
        """Last N records."""
        rows = self._db.execute("""
            SELECT event_id, cid, pubkey, kind, created_at
            FROM cid_index
            ORDER BY stored_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        """Index statistics."""
        total = self._get_count()
        by_kind = {}
        rows = self._db.execute(
            "SELECT kind, COUNT(*) as cnt FROM cid_index GROUP BY kind"
        ).fetchall()
        for r in rows:
            by_kind[str(r["kind"])] = r["cnt"]

        top_pubkeys = self._db.execute("""
            SELECT pubkey, COUNT(*) as cnt
            FROM cid_index
            WHERE pubkey != ''
            GROUP BY pubkey
            ORDER BY cnt DESC
            LIMIT 10
        """).fetchall()

        return {
            "total": total,
            "by_kind": by_kind,
            "top_pubkeys": [{"pubkey": r["pubkey"][:16] + "...", "count": r["cnt"]}
                           for r in top_pubkeys],
            "db_path": self.db_path,
        }

    def delete(self, event_id: str) -> bool:
        """Delete record by event.id."""
        try:
            self._db.execute("DELETE FROM cid_index WHERE event_id = ?", (event_id,))
            self._db.commit()
            return True
        except Exception as e:
            logger.error(f"CID delete error: {e}")
            return False

    def close(self):
        if self._db:
            self._db.close()
            self._db = None
