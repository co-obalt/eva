"""Deterministic recipes — Tier 0 (doc 07 §2). Zero model calls.

Covers the commands a personal agent hears every day:
open/close/focus apps · resources · windows · folders · files search ·
WhatsApp (incl. archived + contact) · URLs · terminal · organize · reminders.
Roman Urdu and English both hit the same fast path.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from ..apps.registry import find_app, match_web_app
from ..core.state import Condition, Predicate
from ..core.task import Step, Task

OPEN_WORDS = {"open", "kholo", "khol", "khulo", "start", "launch", "chalao", "run", "launch"}
CLOSE_WORDS = {"close", "band", "bnd", "quit", "khatam"}
CREATE_WORDS = {"create", "make", "banao", "bana", "new"}
FOLDER_WORDS = {"folder", "directory", "dir", "folder.", "folders"}
STOPWORDS = {"the", "a", "an", "in", "on", "at", "my", "me", "please", "plz", "pe", "par",
             "ka", "ki", "ke", "ko", "se", "to", "for", "and", "aur", "now", "ab", "do",
             "karo", "kro", "kar", "mera", "meri", "mere", "mujhe", "is", "this", "that"}
CHAT_NOISE = {"whatsapp", "whats", "wa", "web", "chat", "chats", "archived", "archive",
            "kholo", "open", "ka", "ki", "ke", "ko", "se", "in", "my", "me", "the",
            "a", "an", "with", "contact", "profile"}

# Any "<X> profile" phrase is extracted generically — profile names are never
# hardcoded; the query is resolved against the machine's real Chrome profiles
# (perception/l3_cdp.match_profile reads Chrome's Local State).
_PROFILE_RE = re.compile(r"\b(?:in|into|on|from|mein|me)\s+([\w][\w .\-&']{0,40}?)\s+profile\b",
                         re.IGNORECASE)
_PROFILE_ALT_RE = re.compile(r"\b(?:in|into|on|from|mein)\s+profile\s+([\w.\-]{1,30})",
                             re.IGNORECASE)
_SELF_PROFILE = {"my", "mera", "meri", "apna", "apni", "current", "same", "is", "this"}


def _clean(text: str) -> str:
    return re.sub(r"[?!,;]+", " ", text.casefold()).strip()


def _tokens(text: str) -> list[str]:
    return _clean(text).split()


def _quoted(text: str) -> str | None:
    m = re.search(r"['\"](.+?)['\"]", text)
    return m.group(1).strip() if m else None


def _filler(tokens: list[str], extra: set[str] | None = None) -> list[str]:
    drop = STOPWORDS | (extra or set())
    return [t for t in tokens if t not in drop]


def _home() -> Path:
    return Path.home()


def _parent_dir(text: str) -> Path:
    low = text.casefold()
    if "downloads" in low or "download mein" in low:
        return _home() / "Downloads"
    if "documents" in low or "docs" in low:
        return _home() / "Documents"
    return _home() / "Desktop"


# ------------------------------------------------------------------ recipes
def match_recipe(text: str, settings) -> Task | None:
    toks = _tokens(text)
    tset = set(toks)
    low = _clean(text)
    # Any web app mentioned by alias resolves through the registry knowledge
    # table (data/webapps.json can add new ones) — no app name lives in code.
    webapp = match_web_app(toks)

    # -- resources / status -----------------------------------------------------
    if ({"cpu", "ram", "memory", "disk", "resources", "performance"} & tset
            or ("status" in tset and ({"system", "usage", "haal", "kya", "health", "resources"} & tset))
            or ("kya" in tset and "haal" in tset) or "system status" in low):
        return _task(text, "report system resources", [
            Step(n=1, intent="query CPU/RAM/disk (L1)", tool="os.get_resources", args={}),
        ], [Predicate(path="reads.os.get_resources", op="==", value=True)])

    # -- list windows -----------------------------------------------------------
    if ("windows" in tset or "window" in tset) and ({"list", "dikhao", "show", "what", "which", "konsi"} & tset):
        return _task(text, "list open windows", [
            Step(n=1, intent="enumerate visible windows (L1)", tool="os.list_windows", args={}),
        ], [Predicate(path="reads.os.list_windows", op="==", value=True)])

    # -- screenshot (explicit user request -> legitimate L6 with recorded reason)
    if {"screenshot", "ss", "scrnshot"} & tset and not ({"read", "parho", "check"} & tset):
        return _task(text, "capture a screenshot as evidence", [
            Step(n=1, intent="capture screen (user requested pixels)", tool="os.capture_evidence",
                 args={"reason": "user explicitly requested a screenshot",
                       "exhausted_levels": "L1,L2,L3,L4"}),
        ], [Predicate(path="reads.os.capture_evidence", op="==", value=True)])

    # -- reminders ----------------------------------------------------------------
    if ("remind" in tset or "reminder" in tset or ("yaad" in tset and "dila" in tset)
            or ("yaad" in tset and "dilana" in tset)):
        minutes = _parse_minutes(low)
        if minutes:
            what = _reminder_text(text)
            return _task(text, f"remind in {minutes} minutes: {what}", [
                Step(n=1, intent=f"schedule reminder in {minutes} min", tool="scheduler.add_reminder",
                     args={"minutes": minutes, "text": what}),
            ], [Predicate(path="reads.scheduler.add_reminder", op="==", value=True)])

    # -- organize downloads --------------------------------------------------------
    if ({"organize", "organise", "sort", "arrange", "saaf"} & tset) and ("downloads" in tset or "download" in tset):
        target = str(_home() / "Downloads")
        return _task(text, "organize Downloads by file category", [
            Step(n=1, intent="organize Downloads into category folders",
                 tool="files.organize", args={"path": target, "dry_run": False}),
        ], [Predicate(path="reads.files.organize", op="==", value=True)])

    # -- terminal: run <command> ----------------------------------------------------
    m = re.search(r"\b(?:run|chalao|chala)\s+(.+)", text, re.IGNORECASE)
    if m and not webapp:
        cmd = m.group(1).strip().strip("'\"")
        if cmd:
            return _task(text, f"run terminal command: {cmd}", [
                Step(n=1, intent=f"run {cmd!r} (allowlisted)", tool="terminal.run",
                     args={"command": cmd}),
            ], [Predicate(path="terminal.last_exit_code", op="==", value=0)])

    # -- web apps (browser) ---------------------------------------------------------
    if webapp and not (CREATE_WORDS & tset):
        task = _webapp_recipe(text, toks, webapp)
        if task is not None:
            return task

    # -- URLs --------------------------------------------------------------------------
    for t in toks:
        if re.fullmatch(r"(https?://)?[\w\-]+\.(com|org|net|io|dev|ai|pk|app|co|me)(/[\w\-./?=&%]*)?", t):
            url = t if t.startswith("http") else f"https://{t}"
            return _task(text, f"open {url}", [
                Step(n=1, intent=f"open {url} in Chrome", tool="chrome.open_profile",
                     args={"url": url}),
            ], [Predicate(path="chrome.has_tab", op="==", value=True, args={"match": f"*{url.split('//')[1].split('/')[0]}*"})])

    # -- open / close / focus -------------------------------------------------------------
    app_token = _extract_app(toks)
    if app_token:
        open_hit = OPEN_WORDS & tset
        close_hit = CLOSE_WORDS & tset
        focus_hit = {"focus", "switch", "jao", "jump", "pe"} & tset
        if close_hit:
            app_name = find_app(app_token).name
            return _task(text, f"close {app_name}", [
                Step(n=1, intent=f"terminate {app_name}", tool="apps.close", args={"name": app_name}),
            ], [Predicate(path="apps.running", op="==", value=False, args={"name": app_name})])
        if focus_hit and not open_hit:
            app_name = find_app(app_token).name
            return _task(text, f"focus {app_name}", [
                Step(n=1, intent=f"focus {app_name} window", tool="os.focus_window", args={"name": app_name}),
            ], [Predicate(path="system.foreground", op="contains", value=app_name)])
        if open_hit or not close_hit:
            app_name = find_app(app_token).name
            return _task(text, f"open {app_name}", [
                Step(n=1, intent=f"launch {app_name} (registry)", tool="apps.launch", args={"name": app_name}),
            ], [Predicate(path="apps.running", op="==", value=True, args={"name": app_name})])

    # -- create folder -----------------------------------------------------------------------
    if (CREATE_WORDS & tset) and (FOLDER_WORDS & tset):
        name = _quoted(text) or _folder_name(toks)
        if name:
            parent = _parent_dir(text) if any(k in tset for k in ("downloads", "documents", "desktop")) else _home() / "Desktop"
            path = str(parent / name)
            return _task(text, f"create folder {name}", [
                Step(n=1, intent=f"create {path}", tool="files.mkdir", args={"path": path}),
            ], [Predicate(path="files.is_dir", op="==", value=True, args={"path": path})])

    # -- search files --------------------------------------------------------------------------
    if ({"search", "find", "dhoondo", "dhundo", "look"} & tset) and \
            ({"files", "file", "documents", "document", "downloads"} & tset):
        needle = _quoted(text) or _search_needle(toks)
        if needle:
            return _task(text, f"search files matching {needle!r}", [
                Step(n=1, intent=f"search home for {needle!r}", tool="files.search",
                     args={"root": str(_home()), "pattern": f"*{needle}*"}),
            ], [Predicate(path="reads.files.search", op="==", value=True)])

    return None


def _task(text: str, goal: str, steps: list[Step], completion: list[Predicate],
          risk=None) -> Task:
    from ..core.task import RiskLevel
    t = Task(user_request=text, goal=goal, completion=Condition(all_of=completion),
             plan=steps, planned_by="recipe")
    if risk:
        t.risk_level = risk
    return t


def _webapp_recipe(text: str, toks: list[str], app: dict) -> Task | None:
    url = str(app.get("url") or "")
    if not url:
        return None
    title = str(app.get("title") or app["name"].title())
    host = urlsplit(url).netloc or str(app["name"])
    tset = set(toks)
    profile, contact_text = _profile_query(text)
    contact = _quoted(text) or _contact_name(contact_text.split())
    archived = bool({"archived", "archive", "archive."} & tset)
    intent = f"open {title} in {profile!r} profile" if profile else f"open {title}"
    steps: list[Step] = [
        Step(n=1, intent=intent, tool="chrome.open_profile",
             args={"profile": profile, "url": url}),
    ]
    preds = [Predicate(path="chrome.has_tab", op="==", value=True, args={"match": f"*{host}*"})]
    # "chat" capability = in-page chat flow implemented by the wa.* DOM tools;
    # if the live page does not match, those tools report UNKNOWN -> BLOCKED.
    chat = "chat" in (app.get("capabilities") or [])
    if chat:
        preds.append(Predicate(path="wa.login", op="==", value=True))
    n = 2
    if chat and contact and archived:
        steps.append(Step(n=n, intent="open Archived folder", tool="wa.open_archived", args={}))
        preds.append(Predicate(path="wa.view", op="==", value="archived"))
        n += 1
    if chat and contact:
        steps.append(Step(n=n, intent=f"search {contact!r}", tool="wa.search_contact",
                          args={"q": contact}))
        preds.append(Predicate(path="wa.search_query", op="==", value=contact))
        n += 1
        steps.append(Step(n=n, intent=f"open chat with {contact!r}", tool="wa.open_chat",
                          args={"q": contact}))
        preds.append(Predicate(path="wa.selected_chat", op="==", value=contact))
    goal = f"{title} open"
    if contact:
        goal += f" with {contact!r} visible"
    if archived and contact:
        goal += " (archived view)"
    return _task(text, goal, steps, preds)


# ------------------------------------------------------------------ extraction helpers
_VERB_WORDS = OPEN_WORDS | CLOSE_WORDS | CREATE_WORDS | {"focus", "switch", "jump", "jao",
                                                          "pe", "par", "please", "plz"}


def _extract_app(toks: list[str]) -> str | None:
    for t in toks:
        if len(t) < 3 or t in STOPWORDS or t in _VERB_WORDS:
            continue
        app = find_app(t)
        if app and t not in ("desktop", "documents", "downloads", "whatsapp", "chat", "user"):
            return t
    for i in range(len(toks) - 1):
        pair = f"{toks[i]} {toks[i + 1]}"
        if pair in ("vs code", "file explorer", "media player"):
            return pair
    return None


def _folder_name(toks: list[str]) -> str | None:
    drop = STOPWORDS | CREATE_WORDS | FOLDER_WORDS | {"desktop", "downloads", "documents",
                                                      "download", "document"}
    rest = [t for t in toks if t not in drop and len(t) > 1]
    if not rest:
        return None
    return " ".join(rest[-2:]).title()


def _search_needle(toks: list[str]) -> str | None:
    drop = STOPWORDS | {"search", "find", "dhoondo", "dhundo", "look", "files", "file",
                        "documents", "document", "downloads", "for"}
    rest = [t for t in toks if t not in drop and len(t) > 1]
    return rest[-1] if rest else None


def _profile_query(text: str) -> tuple[str, str]:
    """Extract a "<X> profile" phrase generically (works for any profile name).

    Returns (profile_query, text_without_the_phrase).  "my profile" means the
    current profile -> empty query.
    """
    clean = _clean(text)
    m = _PROFILE_RE.search(clean) or _PROFILE_ALT_RE.search(clean)
    if not m:
        return "", clean
    q = m.group(1).strip()
    if q.casefold() in _SELF_PROFILE:
        q = ""
    rest = (clean[:m.start()] + " " + clean[m.end():]).strip()
    return q, rest


def _contact_name(toks: list[str]) -> str | None:
    rest = [t for t in toks if t not in CHAT_NOISE and len(t) > 1]
    if not rest:
        return None
    return " ".join(rest[-2:]).title()


def _parse_minutes(low: str) -> int | None:
    m = re.search(r"(\d+)\s*(minutes?|mins?|min|m)\b", low)
    if m:
        return max(1, int(m.group(1)))
    m = re.search(r"(\d+)\s*(hours?|hrs?|h)\b", low)
    if m:
        return max(1, int(m.group(1)) * 60)
    return None


def _reminder_text(text: str) -> str:
    quoted = _quoted(text)
    if quoted:
        return quoted
    m = re.search(r"(?:to|ke|ka|kar|that)\s+(.+)$", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    low = _clean(text)
    low = re.sub(r"remind(er)?\s*(me)?", "", low)
    low = re.sub(r"\d+\s*(minutes?|mins?|hours?|hrs?|min|m|h)", "", low)
    low = re.sub(r"\b(baad|later|in|me|mujhe|yaad|dila(na|o|ay)?)\b", "", low)
    return re.sub(r"\s+", " ", low).strip() or "reminder"


# ------------------------------------------------------------------ direct replies
_GREETINGS = {"hi", "hello", "salam", "salaam", "hey", "aoa", "assalam", "morning", "evening",
              "yo", "adaab"}


def direct_reply(text: str) -> str | None:
    toks = _tokens(text)
    if not toks:
        return ""
    tset = set(toks)
    if tset <= _GREETINGS | {"eva", "uff", "bhai"}:
        return "Yes, I'm here. Give me a task — I plan, execute, verify, and only say 'done' when it's verified."
    if {"help", "commands", "madad"} & tset:
        return ("I run verified tasks on this machine. Try:\n"
                "  open chrome · cpu usage · open whatsapp in <profile-name> profile, <contact> ka archived chat kholo\n"
                "  create folder Reports on desktop · organize downloads · run git status\n"
                "  remind me in 20 minutes to check the oven\n"
                "Task control: /tasks /state /debug /skills /memory /perf /explain /exit")
    if {"thanks", "thank", "shukriya", "shukria", "jazakallah"} & tset:
        return "Anytime. Task me whenever you're ready."
    if "who" in tset and "you" in tset:
        return ("I'm EVA — a stateful, verification-first agent for this machine. "
                "I know the world before I act, I prove effects before I claim success, "
                "and I stop and ask instead of guessing.")
    return None
