"""MemoryBackend (doc 12 §3): SQLite FTS5 is the DEFAULT.

Mem0 stays an optional future backend behind this interface — it must first
beat the doc-08 budget with measured numbers (Phase 4 decision gate).
Retrieval is tags + FTS5 + recency; embeddings are explicitly deferred.
"""
from __future__ import annotations

import re
import time
from typing import Any, Protocol

from ..storage import Database

_TOKEN = re.compile(r"[\w\u0600-\u06FF]+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    return [t.casefold() for t in _TOKEN.findall(text or "")]


class MemoryBackend(Protocol):
    def search_episodes(self, query: str, limit: int = 5) -> list[dict]: ...
    def search_facts(self, query: str, limit: int = 10) -> list[dict]: ...


class SqliteFtsBackend:
    """FTS5 when available, LIKE fallback otherwise. Zero extra RAM."""

    def __init__(self, db: Database):
        self.db = db

    # -- episodes --------------------------------------------------------------
    def search_episodes(self, query: str, limit: int = 5) -> list[dict]:
        rows = self.db.q("SELECT * FROM episodes ORDER BY ts DESC LIMIT 300")
        q_tokens = set(tokenize(query))
        scored: list[tuple[float, dict]] = []
        now = time.time()
        for r in rows:
            text = f"{r['request']} {r['intent_sig']}"
            overlap = len(q_tokens & set(tokenize(text))) / max(1, len(q_tokens))
            recency = max(0.0, 1.0 - (now - (r["ts"] or 0)) / (30 * 86400))
            score = overlap * 0.8 + recency * 0.2
            if overlap > 0:
                scored.append((score, dict(r)))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [s[1] for s in scored[:limit]]

    # -- facts -------------------------------------------------------------------
    def search_facts(self, query: str, limit: int = 10) -> list[dict]:
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        rows = self.db.q("SELECT * FROM facts WHERE invalid=0 ORDER BY ts DESC LIMIT 500")
        qset = set(q_tokens)
        scored: list[tuple[float, dict]] = []
        for r in rows:
            hay = f"{r['subject']} {r['relation']} {r['object']} {r['tags'] or ''}"
            overlap = len(qset & set(tokenize(hay))) / max(1, len(qset))
            if overlap > 0:
                scored.append((overlap * 0.7 + float(r["confidence"] or 0) * 0.3, dict(r)))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [s[1] for s in scored[:limit]]

    def upsert_fact_index(self, rowid: int, subject: str, relation: str, obj: str, tags: str) -> None:
        if not self.db.fts_enabled:
            return
        try:
            self.db.ex("DELETE FROM facts_fts WHERE rowid=?", (rowid,))
            self.db.ex("INSERT INTO facts_fts(rowid, subject, relation, object, tags) VALUES (?,?,?,?,?)",
                       (rowid, subject, relation, obj, tags))
        except Exception:
            pass
