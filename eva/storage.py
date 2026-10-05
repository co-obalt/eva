"""Single SQLite file (WAL) — data ownership per doc 02 §5, doc 06 §3, doc 08.

Tables:
  tasks/checkpoints/journal/effect_tokens  -> orchestrator (doc 03)
  llm_cache                                -> model router (doc 07)
  episodes/facts/profiles_kv/working_state -> memory (doc 06)
  skills/skill_runs                        -> procedural memory (doc 06 §5)
  scheduled_jobs/watch_rules               -> autonomy (doc 10 Phase 7)
  memories_meta                            -> confidence envelope for durable records
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, created_at REAL, updated_at REAL, status TEXT, task_json TEXT);
CREATE TABLE IF NOT EXISTS checkpoints(
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, step_index INTEGER,
  verified TEXT, evidence_refs TEXT, plan_tail_hash TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS journal(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, task_id TEXT, action_hash TEXT,
  tool TEXT, args_json TEXT, outcome TEXT, result_json TEXT);
CREATE TABLE IF NOT EXISTS effect_tokens(
  token TEXT PRIMARY KEY, ts REAL, task_id TEXT, tool TEXT, result_json TEXT);
CREATE TABLE IF NOT EXISTS llm_cache(
  key TEXT PRIMARY KEY, ts REAL, ttl REAL, value_json TEXT);
CREATE TABLE IF NOT EXISTS episodes(
  id TEXT PRIMARY KEY, ts REAL, intent_sig TEXT, request TEXT, outcome TEXT, record_json TEXT);
CREATE TABLE IF NOT EXISTS facts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, subject TEXT, relation TEXT, object TEXT,
  tags TEXT, confidence REAL, source TEXT, usage_count INTEGER DEFAULT 0, last_used REAL,
  last_verified REAL, verify_method TEXT, invalid INTEGER DEFAULT 0, content_json TEXT);
CREATE TABLE IF NOT EXISTS profiles_kv(
  key TEXT PRIMARY KEY, value_json TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS working_state(
  id TEXT PRIMARY KEY, value_json TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS skills(
  name TEXT PRIMARY KEY, version INTEGER, intent_sig TEXT, params_json TEXT, steps_json TEXT,
  success_rate REAL, runs INTEGER DEFAULT 0, last_run REAL, confidence REAL,
  state TEXT DEFAULT 'active', created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS skill_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, skill TEXT, task_id TEXT, ts REAL, outcome TEXT);
CREATE TABLE IF NOT EXISTS scheduled_jobs(
  id TEXT PRIMARY KEY, kind TEXT, spec_json TEXT, next_run_at REAL, last_run REAL,
  enabled INTEGER DEFAULT 1, created_at REAL);
CREATE TABLE IF NOT EXISTS watch_rules(
  id TEXT PRIMARY KEY, kind TEXT, spec_json TEXT, enabled INTEGER DEFAULT 1,
  created_at REAL, last_fired REAL);
CREATE TABLE IF NOT EXISTS rules(
  id TEXT PRIMARY KEY, scope TEXT, when_text TEXT, instruction TEXT,
  prefer_tools TEXT, forbid_tools TEXT, source TEXT, confidence REAL,
  active INTEGER DEFAULT 1, created_at REAL, last_used REAL, used_count INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS memories_meta(
  id TEXT PRIMARY KEY, type TEXT, source TEXT, confidence REAL, usage_count INTEGER DEFAULT 0,
  created_at REAL, last_used REAL, last_verified REAL, verify_method TEXT,
  invalid INTEGER DEFAULT 0, tags TEXT);
"""


class Database:
    """Thin sync SQLite wrapper (WAL). Fast enough for the envelope in doc 08."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        self._migrate()
        self.fts_enabled = self._init_fts()

    def _migrate(self) -> None:
        """Additive, idempotent migrations for databases created by older builds."""
        for table, column, decl in (("journal", "reason", "TEXT DEFAULT ''"),):
            try:
                cols = {r["name"] for r in self.q(f"PRAGMA table_info({table})")}
                if column not in cols:
                    self.ex(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            except Exception:
                continue

    def _init_fts(self) -> bool:
        try:
            self.conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(subject, relation, object, tags)"
            )
            self.conn.commit()
            return True
        except sqlite3.OperationalError:
            return False

    # -- primitives ---------------------------------------------------------
    def ex(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def executemany(self, sql: str, rows) -> None:
        self.conn.executemany(sql, rows)
        self.conn.commit()

    def q(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def q1(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        cur = self.conn.execute(sql, params)
        return cur.fetchone()

    # -- convenience --------------------------------------------------------
    def kv_set(self, table: str, key_col: str, key: str, value: Any, extra: dict | None = None,
               ts_col: str | None = None) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        cols = {key_col: key, "value_json": payload}
        if ts_col:
            cols[ts_col] = time.time()
        if extra:
            cols.update(extra)
        names = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        self.ex(f"INSERT OR REPLACE INTO {table} ({names}) VALUES ({marks})", tuple(cols.values()))

    def kv_get(self, table: str, key_col: str, key: str, value_col: str = "value_json") -> Any:
        row = self.q1(f"SELECT {value_col} FROM {table} WHERE {key_col}=?", (key,))
        if not row or row[0] is None:
            return None
        try:
            return json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            return None

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
