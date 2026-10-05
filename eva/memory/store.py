"""MemoryStore — the memory service every layer uses (doc 06).

Retrieval never dumps memory into prompts: `retrieve()` returns a bounded,
confidence-tagged slice (~800 tokens) for the context builder.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from pydantic import BaseModel, Field

from ..core.task import Task, TaskStatus
from ..storage import Database
from .backend import SqliteFtsBackend, tokenize

STOPWORDS = {
    "open", "kholo", "khol", "kro", "karo", "kar", "do", "plz", "please", "the", "a", "an",
    "in", "into", "on", "my", "me", "mere", "meri", "mujhe", "ka", "ki", "ke", "ko", "se",
    "hai", "he", "ho", "koi", "aur", "and", "to", "for", "of", "of", "what", "whats",
    "what's", "kya", "hai?", "batao", "bata", "de", "dena", "chalo", "now", "ab",
}


def intent_signature(text: str) -> str:
    tokens = [t for t in tokenize(text) if t not in STOPWORDS and len(t) > 1]
    return " ".join(sorted(set(tokens)))


class Skill(BaseModel):
    name: str
    intent_sig: str = ""
    params: list[str] = Field(default_factory=list)
    steps: list[dict[str, Any]] = Field(default_factory=list)   # [{tool, args}]
    success_rate: float = 1.0
    runs: int = 0
    last_run: float | None = None
    confidence: float = 0.9
    state: str = "active"            # active | quarantined
    version: int = 1
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class Rule(BaseModel):
    """A durable user instruction ("aage se X ke liye Y") — memory, never code.

    `forbid_tools`/`prefer_tools` are what make a rule *enforceable* rather than
    advisory: a forbidden tool proposed for a matching task is rejected before
    execution (POLICY_REJECTED), and preferred tools are surfaced as hints.
    """
    id: str = Field(default_factory=lambda: f"rule_{uuid.uuid4().hex[:8]}")
    scope: str = "global"              # global | app:<name> | site:<host> | free tag
    when_text: str = ""                # trigger description ("websites", "software")
    instruction: str = ""              # what the user said, verbatim-ish
    prefer_tools: list[str] = Field(default_factory=list)
    forbid_tools: list[str] = Field(default_factory=list)
    source: str = "user_statement"
    confidence: float = 0.95
    active: bool = True
    created_at: float = Field(default_factory=time.time)
    last_used: float | None = None
    used_count: int = 0

    def tokens(self) -> set[str]:
        text = self.when_text or self.instruction
        return {t for t in intent_signature(text).split() if len(t) > 2}

    def relevance(self, text: str) -> float:
        """0..1 — how strongly this rule speaks about `text`.

        The *trigger* drives matching when one is given ("when: websites" makes the
        rule apply to anything mentioning websites); otherwise the instruction does.
        No words at all means the rule is unconditional (it applies to everything)."""
        want = {t for t in intent_signature(text).split() if len(t) > 2}
        mine = self.tokens()
        if not mine:
            return 1.0
        if not want:
            return 0.0
        return len(want & mine) / max(1, len(mine))


class MemoryStore:
    def __init__(self, db: Database, bus=None):
        self.db = db
        self.bus = bus
        self.backend = SqliteFtsBackend(db)
        self._working: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------- working
    def put_working(self, task_id: str, key: str, value: Any) -> None:
        self._working.setdefault(task_id, {})[key] = value

    def get_working(self, task_id: str) -> dict[str, Any]:
        return dict(self._working.get(task_id, {}))

    def clear_working(self, task_id: str) -> None:
        self._working.pop(task_id, None)
        self.db.ex("DELETE FROM working_state WHERE id=?", (task_id,))

    # ------------------------------------------------------------- episodes
    def record_episode(self, task: Task) -> str:
        ep_id = f"ep_{uuid.uuid4().hex[:10]}"
        record = {
            "request": task.user_request,
            "goal": task.goal,
            "outcome": task.status.value,
            "tools": [s.tool for s in task.plan],
            "steps": len(task.plan),
            "failures": [{"step": f.step, "kind": f.kind, "detail": f.detail[:200]}
                         for f in task.failures][:5],
            "lessons": self._lessons(task),
            "message": task.result_message[:300],
            "planned_by": task.planned_by,
        }
        sig = intent_signature(task.user_request)
        self.db.ex(
            "INSERT OR REPLACE INTO episodes(id, ts, intent_sig, request, outcome, record_json) "
            "VALUES (?,?,?,?,?,?)",
            (ep_id, time.time(), sig, task.user_request, task.status.value,
             json.dumps(record, default=str)),
        )
        self._meta_upsert(ep_id, "episodic", "verified_task" if task.status is TaskStatus.FINISHED
                          else "inference", confidence=0.9 if task.status is TaskStatus.FINISHED else 0.6,
                          verify_method="task_outcome", tags=sig)
        return ep_id

    def _lessons(self, task: Task) -> list[str]:
        out: list[str] = []
        for f in task.failures:
            if f.kind == "blocked":
                out.append(f"blocked: {f.detail[:120]}")
            elif f.kind == "verification_failed":
                out.append(f"expected {f.expected[:80]} but observed {f.observed[:80]}")
        for s in task.plan:
            if s.attempts > 1 and s.status.value == "VERIFIED":
                out.append(f"{s.tool} needed {s.attempts} attempts")
        return out[:5]

    def recent_episodes(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.db.q("SELECT * FROM episodes ORDER BY ts DESC LIMIT ?", (limit,))
        return [self._episode_row(r) for r in rows]

    def find_similar_episodes(self, sig: str, limit: int = 5) -> list[dict[str, Any]]:
        rows = self.db.q("SELECT * FROM episodes ORDER BY ts DESC LIMIT 200")
        want = set(sig.split())
        scored: list[tuple[float, dict]] = []
        now = time.time()
        for r in rows:
            have = set((r["intent_sig"] or "").split())
            if not want or not have:
                continue
            overlap = len(want & have) / max(1, len(want))
            recency = max(0.0, 1.0 - (now - (r["ts"] or 0)) / (30 * 86400))
            if overlap >= 0.5:
                scored.append((overlap * 0.8 + recency * 0.2, self._episode_row(r)))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [s[1] for s in scored[:limit]]

    def _episode_row(self, row) -> dict[str, Any]:
        try:
            record = json.loads(row["record_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            record = {}
        record.update({"id": row["id"], "ts": row["ts"], "intent_sig": row["intent_sig"]})
        return record

    # ------------------------------------------------------------- facts
    def upsert_fact(self, subject: str, relation: str, obj: str, *, tags: str = "",
                    source: str = "verified_task", confidence: float | None = None,
                    verify_method: str = "") -> int:
        row = self.db.q1(
            "SELECT id, confidence, usage_count FROM facts WHERE subject=? AND relation=?",
            (subject, relation))
        now = time.time()
        base = confidence if confidence is not None else (
            0.95 if source == "user_statement" else 0.90 if source == "verified_task" else 0.6)
        if row:
            new_conf = min(0.99, max(float(row["confidence"]), base) + 0.02)
            self.db.ex(
                "UPDATE facts SET object=?, tags=?, confidence=?, source=?, usage_count=usage_count+1,"
                " last_used=?, last_verified=?, verify_method=?, invalid=0 WHERE id=?",
                (obj, tags, new_conf, source, now, now, verify_method, row["id"]))
            self.backend.upsert_fact_index(int(row["id"]), subject, relation, obj, tags)
            return int(row["id"])
        cur = self.db.ex(
            "INSERT INTO facts(ts, subject, relation, object, tags, confidence, source,"
            " usage_count, last_used, last_verified, verify_method, invalid, content_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?)",
            (now, subject, relation, obj, tags, base, source, 1, now, now, verify_method, "{}"))
        rowid = int(cur.lastrowid)
        self.backend.upsert_fact_index(rowid, subject, relation, obj, tags)
        self._meta_upsert(f"fact_{rowid}", "semantic", source, confidence=base,
                          verify_method=verify_method or "discovery_pass", tags=tags)
        return rowid

    def invalidate_fact(self, subject: str, relation: str) -> None:
        self.db.ex("UPDATE facts SET invalid=1, confidence=0.1 WHERE subject=? AND relation=?",
                   (subject, relation))
        if self.bus is not None:
            self.bus.emit("MEMORY_INVALIDATED", subject=subject, relation=relation)

    def facts(self, query: str = "", limit: int = 30) -> list[dict[str, Any]]:
        if query:
            rows = self.backend.search_facts(query, limit)
        else:
            rows = [dict(r) for r in self.db.q(
                "SELECT * FROM facts WHERE invalid=0 ORDER BY ts DESC LIMIT ?", (limit,))]
        return rows

    # ------------------------------------------------------------- profile
    def profile_set(self, key: str, value: Any) -> None:
        self.db.ex("INSERT OR REPLACE INTO profiles_kv(key, value_json, ts) VALUES (?,?,?)",
                   (key, json.dumps(value, default=str), time.time()))
        self._meta_upsert(f"profile:{key}", "profile", "user_statement", confidence=0.95,
                          verify_method="user_statement")

    def profile_get(self, key: str, default: Any = None) -> Any:
        val = self.db.kv_get("profiles_kv", "key", key)
        return default if val is None else val

    def profile_all(self) -> dict[str, Any]:
        rows = self.db.q("SELECT key, value_json FROM profiles_kv ORDER BY ts DESC LIMIT 40")
        out: dict[str, Any] = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value_json"])
            except (TypeError, json.JSONDecodeError):
                continue
        return out

    # ------------------------------------------------------------- rules
    def upsert_rule(self, rule: Rule) -> str:
        """Remember a user instruction. Same scope+instruction updates in place."""
        prior = self.db.q1(
            "SELECT id, created_at, used_count FROM rules WHERE instruction=? AND scope=?",
            (rule.instruction, rule.scope))
        if prior:
            rule.id = prior["id"]
            rule.created_at = float(prior["created_at"] or rule.created_at)
            rule.used_count = int(prior["used_count"] or 0)
        self.db.ex(
            "INSERT OR REPLACE INTO rules(id, scope, when_text, instruction, prefer_tools,"
            " forbid_tools, source, confidence, active, created_at, last_used, used_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (rule.id, rule.scope, rule.when_text, rule.instruction,
             json.dumps(rule.prefer_tools), json.dumps(rule.forbid_tools), rule.source,
             float(rule.confidence), 1 if rule.active else 0, rule.created_at,
             rule.last_used, rule.used_count))
        self._meta_upsert(f"rule:{rule.id}", "rule", rule.source, confidence=rule.confidence,
                          verify_method="user_statement", tags=rule.when_text or "global")
        if self.bus is not None:
            self.bus.emit("RULE_REMEMBERED", rule=rule.id, instruction=rule.instruction[:120],
                          scope=rule.scope)
        return rule.id

    def _rule_row(self, row) -> Rule:
        def _loads(raw) -> list[str]:
            try:
                return [str(x) for x in json.loads(raw or "[]")]
            except (TypeError, json.JSONDecodeError):
                return []
        return Rule(
            id=row["id"], scope=row["scope"] or "global", when_text=row["when_text"] or "",
            instruction=row["instruction"] or "", prefer_tools=_loads(row["prefer_tools"]),
            forbid_tools=_loads(row["forbid_tools"]), source=row["source"] or "user_statement",
            confidence=float(row["confidence"] or 0.0), active=bool(row["active"]),
            created_at=float(row["created_at"] or 0.0), last_used=row["last_used"],
            used_count=int(row["used_count"] or 0))

    def get_rule(self, rule_id: str) -> Rule | None:
        row = self.db.q1("SELECT * FROM rules WHERE id=?", (rule_id,))
        return self._rule_row(row) if row else None

    def list_rules(self, active_only: bool = True) -> list[Rule]:
        sql = "SELECT * FROM rules"
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY created_at DESC"
        return [self._rule_row(r) for r in self.db.q(sql)]

    def has_rule(self, instruction: str) -> bool:
        want = " ".join(str(instruction).casefold().split())
        if not want:
            return False
        for rule in self.list_rules():
            if " ".join(rule.instruction.casefold().split()) == want:
                return True
        return False

    def forget_rule(self, selector: str) -> bool:
        """Forget by id or by instruction text — rules must be as easy to drop as to add."""
        row = self.db.q1("SELECT id FROM rules WHERE id=?", (selector,))
        if row is None:
            want = " ".join(str(selector).casefold().split())
            for rule in self.list_rules(active_only=False):
                if want and want in " ".join(rule.instruction.casefold().split()):
                    row = {"id": rule.id}
                    break
        if row is None:
            return False
        self.db.ex("UPDATE rules SET active=0, confidence=0.1 WHERE id=?", (row["id"],))
        self._meta_upsert(f"rule:{row['id']}", "rule", "user_statement", confidence=0.1,
                          verify_method="retracted", tags="forgotten")
        if self.bus is not None:
            self.bus.emit("RULE_FORGOTTEN", rule=row["id"])
        return True

    def applicable_rules(self, text: str, *, scope: str | None = None,
                         threshold: float = 0.34) -> list[Rule]:
        """Rules that speak to this request (scope filter + trigger relevance)."""
        out: list[Rule] = []
        for rule in self.list_rules():
            if scope is not None and rule.scope not in ("global", scope):
                continue
            if rule.relevance(text) >= threshold:
                out.append(rule)
        out.sort(key=lambda r: (-r.relevance(text), -r.confidence))
        return out[:8]

    def record_rule_use(self, rule_id: str) -> None:
        self.db.ex("UPDATE rules SET used_count=used_count+1, last_used=? WHERE id=?",
                   (time.time(), rule_id))

    # ------------------------------------------------------------- skills
    def upsert_skill(self, skill: Skill) -> None:
        existing = self.get_skill(skill.name)
        if existing:
            skill.version = existing.version + 1
            skill.created_at = existing.created_at
            skill.runs = existing.runs
            skill.success_rate = existing.success_rate
        skill.updated_at = time.time()
        self.db.ex(
            "INSERT OR REPLACE INTO skills(name, version, intent_sig, params_json, steps_json,"
            " success_rate, runs, last_run, confidence, state, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (skill.name, skill.version, skill.intent_sig, json.dumps(skill.params),
             json.dumps(skill.steps, default=str), skill.success_rate, skill.runs,
             skill.last_run, skill.confidence, skill.state, skill.created_at, skill.updated_at))
        self._meta_upsert(f"skill:{skill.name}", "procedural", "verified_task",
                          confidence=skill.confidence, verify_method="skill_runs", tags=skill.intent_sig)
        if self.bus is not None:
            self.bus.emit("SKILL_DISTILLED", skill=skill.name, version=skill.version)

    def get_skill(self, name: str) -> Skill | None:
        row = self.db.q1("SELECT * FROM skills WHERE name=?", (name,))
        return self._skill_row(row) if row else None

    def list_skills(self) -> list[Skill]:
        rows = self.db.q("SELECT * FROM skills ORDER BY updated_at DESC")
        return [self._skill_row(r) for r in rows]

    def _skill_row(self, row) -> Skill:
        return Skill(
            name=row["name"], version=row["version"], intent_sig=row["intent_sig"] or "",
            params=json.loads(row["params_json"] or "[]"),
            steps=json.loads(row["steps_json"] or "[]"),
            success_rate=float(row["success_rate"] or 0), runs=int(row["runs"] or 0),
            last_run=row["last_run"], confidence=float(row["confidence"] or 0),
            state=row["state"] or "active", created_at=row["created_at"] or time.time(),
            updated_at=row["updated_at"] or time.time())

    def match_skill(self, text: str, threshold: float = 0.66) -> tuple[Skill, float] | None:
        want = set(intent_signature(text).split())
        if not want:
            return None
        best: tuple[Skill, float] | None = None
        for skill in self.list_skills():
            if skill.state != "active":
                continue
            have = set((skill.intent_sig or "").split())
            if not have:
                continue
            overlap = len(want & have) / max(1, len(want))
            if overlap >= threshold and (best is None or overlap > best[1]):
                best = (skill, overlap)
        return best

    def record_skill_run(self, name: str, outcome: str, task_id: str | None = None) -> None:
        skill = self.get_skill(name)
        if skill is None:
            return
        self.db.ex("INSERT INTO skill_runs(skill, task_id, ts, outcome) VALUES (?,?,?,?)",
                   (name, task_id, time.time(), outcome))
        ok = outcome == "FINISHED"
        alpha = 0.3
        skill.success_rate = round((1 - alpha) * skill.success_rate + alpha * (1.0 if ok else 0.0), 3)
        skill.runs += 1
        skill.last_run = time.time()
        if skill.runs >= 3 and skill.success_rate < 0.5:
            skill.state = "quarantined"
            skill.confidence = min(skill.confidence, 0.3)
            if self.bus is not None:
                self.bus.emit("SKILL_QUARANTINED", skill=name, success_rate=skill.success_rate)
        self.db.ex(
            "UPDATE skills SET success_rate=?, runs=?, last_run=?, state=?, confidence=?,"
            " updated_at=? WHERE name=?",
            (skill.success_rate, skill.runs, skill.last_run, skill.state, skill.confidence,
             time.time(), name))

    # ------------------------------------------------------------- retrieval
    def retrieve(self, task: Task, budget_tokens: int = 800) -> dict[str, Any]:
        sig = intent_signature(task.user_request)
        skills = []
        match = self.match_skill(task.user_request)
        if match and match[0].confidence >= 0.5:
            s, score = match
            skills = [{"name": s.name, "score": round(score, 2), "success_rate": s.success_rate,
                       "steps": [st.get("tool") for st in s.steps]}]
        episodes = [{"request": e.get("request", "")[:120], "outcome": e.get("outcome", ""),
                     "lessons": e.get("lessons", [])[:3]}
                    for e in self.find_similar_episodes(sig, 3)]
        facts = [{"s": f["subject"], "r": f["relation"], "o": f["object"],
                  "conf": round(float(f["confidence"]), 2)}
                 for f in self.backend.search_facts(task.user_request, 10)]
        profile = {k: v for k, v in list(self.profile_all().items())[:10]}
        rules = [{"id": r.id, "instruction": r.instruction[:160], "when": r.when_text,
                  "prefer": r.prefer_tools, "forbid": r.forbid_tools}
                 for r in self.applicable_rules(task.user_request)]

        slice_ = {"rules": rules, "skills": skills, "episodes": episodes, "facts": facts,
                  "profile": profile}

        def _tokens() -> int:
            return len(json.dumps(slice_, default=str)) // 4

        # trim in priority order (doc 07 §3 rule 6): rules stay longest — they are
        # durable user intent, not incidental context.
        while _tokens() > budget_tokens and slice_["profile"]:
            slice_["profile"].popitem()
        while _tokens() > budget_tokens and slice_["episodes"]:
            slice_["episodes"].pop()
        while _tokens() > budget_tokens and slice_["facts"]:
            slice_["facts"].pop()
        if _tokens() > budget_tokens and slice_["skills"]:
            slice_["skills"] = []
        while _tokens() > budget_tokens and slice_["rules"]:
            slice_["rules"].pop()
        return slice_

    # ------------------------------------------------------------- meta
    def _meta_upsert(self, id_: str, type_: str, source: str, confidence: float,
                     verify_method: str, tags: str = "") -> None:
        now = time.time()
        prior = self.db.q1("SELECT usage_count, created_at FROM memories_meta WHERE id=?", (id_,))
        usage = int(prior["usage_count"]) if prior else 0
        created = float(prior["created_at"]) if prior else now
        self.db.ex(
            "INSERT OR REPLACE INTO memories_meta(id, type, source, confidence, usage_count,"
            " created_at, last_used, last_verified, verify_method, invalid, tags)"
            " VALUES (?,?,?,?,?,?,?,?,?,0,?)",
            (id_, type_, source, confidence, usage, created, now, now, verify_method, tags))
