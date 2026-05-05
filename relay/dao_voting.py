"""
SNIN Relay — DAO Voting Engine
Proposals (kind:1111) and Votes (kind:1112).

Proposal kinds:
- kind:1111 = DAO Proposal (title + description in content, tags: h=group, p=author)
- kind:1112 = Vote (content: "approve"|"reject"|"abstain", tags: e=proposal_id, h=group)

Quorum: 60% of group members.
Pass threshold: >50% of votes.
"""

import asyncio
import json
import logging
import os
import time

logger = logging.getLogger('dao_voting')

DB_PATH = os.path.join(os.path.dirname(__file__), 'relay_v2.db')

# ── Vote kinds ──
KIND_PROPOSAL = 1111
KIND_VOTE = 1112
KIND_PROPOSAL_CLOSED = 1113


class DAOVoting:
    """DAO proposal and voting engine."""

    def __init__(self, db_path: str = DB_PATH):
        self._db_path = db_path
        self._init_db()

    def _init_db(self):
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dao_proposals (
                id TEXT PRIMARY KEY,
                group_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                author TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                status TEXT DEFAULT 'open',  -- open, passed, rejected, closed
                closed_at INTEGER DEFAULT 0,
                votes_for INTEGER DEFAULT 0,
                votes_against INTEGER DEFAULT 0,
                votes_abstain INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dao_votes (
                id TEXT PRIMARY KEY,
                proposal_id TEXT NOT NULL,
                voter TEXT NOT NULL,
                vote TEXT NOT NULL,  -- approve, reject, abstain
                created_at INTEGER NOT NULL,
                weight INTEGER DEFAULT 1,
                UNIQUE(proposal_id, voter)
            )
        """)
        conn.commit()
        conn.close()

    def get_proposal(self, proposal_id: str) -> dict | None:
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        row = conn.execute(
            "SELECT * FROM dao_proposals WHERE id=?", (proposal_id,)
        ).fetchone()
        conn.close()
        if row:
            return {
                "id": row[0], "group_id": row[1], "title": row[2],
                "description": row[3], "author": row[4], "created_at": row[5],
                "status": row[6], "closed_at": row[7],
                "votes_for": row[8], "votes_against": row[9], "votes_abstain": row[10],
            }
        return None

    def list_proposals(self, group_id: str = None, status: str = None) -> list[dict]:
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        where = []
        params = []
        if group_id:
            where.append("group_id=?")
            params.append(group_id)
        if status:
            where.append("status=?")
            params.append(status)
        query = "SELECT * FROM dao_proposals"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY created_at DESC"
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [{
            "id": r[0], "group_id": r[1], "title": r[2],
            "description": r[3], "author": r[4], "created_at": r[5],
            "status": r[6], "closed_at": r[7],
            "votes_for": r[8], "votes_against": r[9], "votes_abstain": r[10],
        } for r in rows]

    def handle_proposal(self, event: dict) -> dict:
        """Process a kind:1111 proposal event from the relay."""
        ev_id = event.get("id", "")
        pubkey = event.get("pubkey", "")
        content = event.get("content", "")
        created_at = event.get("created_at", 0)
        tags = event.get("tags", [])
        
        # Extract group_id from tags
        group_id = ""
        title = ""
        description = content
        for t in tags:
            if len(t) >= 2 and t[0] == "h":
                group_id = t[1]
            elif len(t) >= 2 and t[0] == "title":
                title = t[1]
        
        if not group_id:
            return {"error": "missing h tag (group_id)"}
        
        # Check if already exists
        existing = self.get_proposal(ev_id)
        if existing:
            return {"error": "proposal already exists"}
        
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            """INSERT INTO dao_proposals (id, group_id, title, description, author, created_at, status)
               VALUES (?, ?, ?, ?, ?, ?, 'open')""",
            (ev_id, group_id, title, description, pubkey, created_at)
        )
        conn.commit()
        conn.close()
        
        logger.info(f"📋 Proposal created: {ev_id[:16]}... in {group_id} by {pubkey[:16]}...")
        return {"result": "ok", "proposal_id": ev_id}

    def handle_vote(self, event: dict) -> dict:
        """Process a kind:1112 vote event from the relay."""
        ev_id = event.get("id", "")
        pubkey = event.get("pubkey", "")
        vote = event.get("content", "").strip().lower()
        created_at = event.get("created_at", 0)
        tags = event.get("tags", [])
        
        # Validate vote value
        if vote not in ("approve", "reject", "abstain"):
            return {"error": f"invalid vote: {vote}. Must be: approve, reject, or abstain"}
        
        # Extract proposal_id from e tag
        proposal_id = ""
        for t in tags:
            if len(t) >= 2 and t[0] == "e":
                proposal_id = t[1]
                break
        
        if not proposal_id:
            return {"error": "missing e tag (proposal_id)"}
        
        # Check proposal exists and is open
        proposal = self.get_proposal(proposal_id)
        if not proposal:
            return {"error": "proposal not found"}
        if proposal["status"] != "open":
            return {"error": f"proposal is {proposal['status']}, not open"}
        
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        
        # Check if already voted
        existing = conn.execute(
            "SELECT vote FROM dao_votes WHERE proposal_id=? AND voter=?",
            (proposal_id, pubkey)
        ).fetchone()
        
        if existing:
            conn.close()
            return {"error": "already voted"}
        
        # Cast vote
        conn.execute(
            "INSERT INTO dao_votes (id, proposal_id, voter, vote, created_at) VALUES (?, ?, ?, ?, ?)",
            (ev_id, proposal_id, pubkey, vote, created_at)
        )
        
        # Update counts
        field_map = {"approve": "votes_for", "reject": "votes_against", "abstain": "votes_abstain"}
        field = field_map.get(vote, "votes_abstain")
        conn.execute(
            f"UPDATE dao_proposals SET {field} = {field} + 1 WHERE id=?",
            (proposal_id,)
        )
        conn.commit()
        conn.close()
        
        # Re-check if proposal should close
        updated = self.get_proposal(proposal_id)
        self._check_quorum(updated)
        
        logger.info(f"🗳️ Vote: {pubkey[:16]}... → {vote} on {proposal_id[:16]}...")
        return {"result": "ok", "proposal_id": proposal_id, "vote": vote}

    def _check_quorum(self, proposal: dict):
        """Check if proposal has reached quorum and close if needed."""
        group_members = {"strategy": 5, "market": 4, "dev": 4, "general": 15}
        group_id = proposal["group_id"]
        total_members = group_members.get(group_id, 0)
        
        if total_members == 0:
            return
        
        total_votes = proposal["votes_for"] + proposal["votes_against"] + proposal["votes_abstain"]
        quorum_needed = max(1, int(total_members * 0.6))
        
        if total_votes >= quorum_needed:
            # Close and decide
            passed = proposal["votes_for"] > proposal["votes_against"]
            status = "passed" if passed else "rejected"
            
            import sqlite3
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                "UPDATE dao_proposals SET status=?, closed_at=? WHERE id=?",
                (status, int(time.time()), proposal["id"])
            )
            conn.commit()
            conn.close()
            
            logger.info(f"📊 Proposal {proposal['id'][:16]}... {status} ({proposal['votes_for']}F/{proposal['votes_against']}A/{proposal['votes_abstain']}Abs)")

    def get_stats(self) -> dict:
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        proposals = conn.execute("SELECT COUNT(*) FROM dao_proposals").fetchone()[0]
        open_props = conn.execute("SELECT COUNT(*) FROM dao_proposals WHERE status='open'").fetchone()[0]
        votes = conn.execute("SELECT COUNT(*) FROM dao_votes").fetchone()[0]
        conn.close()
        return {
            "proposals": proposals,
            "open": open_props,
            "votes": votes,
            "status": "active",
        }
