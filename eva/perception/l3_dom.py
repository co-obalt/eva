"""L3 DOM — generic, site-agnostic browser hand (doc 04 L3/L4).

This module knows HTML *concepts* (roles, labels, inputs) and nothing about any
particular website. Elements are addressed by ROLE + accessible name or by CSS
selector — never by pixels. `find` tags matches with a handle attribute; the
other verbs act on handles, so no coordinates ever enter the picture.

Every call degrades honestly: unavailable CDP or a missing element returns
``{"ok": False, "error": ...}`` (callers turn that into a failed step, never a
fake success). Nothing here mutates the world except the explicit act verbs.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from .l3_cdp import CdpManager

HANDLE_ATTR = "data-eva-h"

# Generic HTML role knowledge (not app knowledge): role -> CSS selector set.
_ROLE_SELECTORS_JS = r"""
const ROLE_SEL = {
  button: 'button,[role="button"],input[type="submit"],input[type="button"],input[type="reset"]',
  link: 'a[href],[role="link"]',
  textbox: 'input[type="text"],input[type="search"],input[type="email"],input[type="url"],input[type="tel"],input[type="password"],input:not([type]),textarea,[role="textbox"],[contenteditable="true"]',
  searchbox: 'input[type="search"],[role="searchbox"],input[type="text"],textarea,[contenteditable="true"]',
  checkbox: 'input[type="checkbox"],[role="checkbox"]',
  radio: 'input[type="radio"],[role="radio"]',
  combobox: 'select,[role="combobox"],[aria-haspopup="listbox"]',
  option: 'option,[role="option"],[role="menuitem"]',
  menuitem: '[role="menuitem"],menuitem',
  tab: '[role="tab"]',
  listitem: 'li,[role="listitem"],[role="option"]',
  cell: 'td,th,[role="cell"],[role="gridcell"]',
  row: 'tr,[role="row"]',
  heading: 'h1,h2,h3,h4,h5,h6,[role="heading"]',
  img: 'img,[role="img"]',
  dialog: 'dialog,[role="dialog"],[role="alertdialog"]',
  switch: '[role="switch"]',
  slider: 'input[type="range"],[role="slider"]',
  file: 'input[type="file"]',
};
"""

_HELPERS_JS = r"""
const T = (el) => (el ? ((el.innerText || el.textContent || '') + '').replace(/\s+/g, ' ').trim() : '');
const VIS = (el) => {
  try {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  } catch (e) { return false; }
};
const ROLE_OF = (el) => {
  const explicit = el.getAttribute && el.getAttribute('role');
  if (explicit) return explicit.toLowerCase();
  const tag = (el.tagName || '').toLowerCase();
  const type = (el.getAttribute && el.getAttribute('type') || '').toLowerCase();
  if (tag === 'a') return 'link';
  if (tag === 'button') return 'button';
  if (tag === 'select') return 'combobox';
  if (tag === 'textarea') return 'textbox';
  if (tag === 'img') return 'img';
  if (tag === 'input') return type === 'submit' || type === 'button' ? 'button' : (type || 'textbox');
  if (/^h[1-6]$/.test(tag)) return 'heading';
  return tag;
};
const LABEL_OF = (el) => {
  const bits = [
    el.getAttribute && el.getAttribute('aria-label'),
    el.getAttribute && el.getAttribute('title'),
    el.getAttribute && el.getAttribute('placeholder'),
    el.getAttribute && el.getAttribute('name'),
    el.getAttribute && el.getAttribute('alt'),
    el.getAttribute && el.getAttribute('value'),
    T(el),
  ];
  return bits.filter(Boolean).map((b) => (b + '').trim()).join(' | ').slice(0, 160);
};
const LOOKUP = (h) => document.querySelector('[' + '__ATTR__' + '="' + h + '"]');
const WALL = () => ((window.__eva_elements = window.__eva_elements || {})[__PH__] = __PH__);
"""

_FIND_JS = r"""
(() => {
  __ROLES__
  __HELPERS__
  try {
    const query = __QUERY__;
    const by = __BY__;
    const limit = __LIMIT__;
    const tag = 'e' + Date.now().toString(36) + Math.floor(Math.random() * 1e6).toString(36);
    let cands = [];
    if (by === 'selector') {
      cands = Array.from(document.querySelectorAll(query));
    } else if (by === 'role') {
      const sel = ROLE_SEL[(query || '').toLowerCase()] || '[role="' + query + '"]';
      cands = Array.from(document.querySelectorAll(sel));
    } else if (by === 'aria') {
      cands = Array.from(document.querySelectorAll('[aria-label]'))
        .filter((el) => (el.getAttribute('aria-label') || '').toLowerCase().includes((query || '').toLowerCase()));
    } else {
      const sel = 'a,button,input,textarea,select,label,li,td,th,span,div,p,h1,h2,h3,[role],[contenteditable="true"]';
      const needle = (query || '').toLowerCase();
      cands = Array.from(document.querySelectorAll(sel)).filter((el) => {
        if (!needle) return false;
        const lab = LABEL_OF(el).toLowerCase();
        if (!lab.includes(needle)) return false;
        if (T(el).length > 240) return false;
        return true;
      });
      // most specific first: short labels and leaf-ish nodes win
      cands.sort((a, b) => (T(a).length - T(b).length) || (a.children.length - b.children.length));
    }
    const out = [];
    let i = 0;
    for (const el of cands) {
      if (!VIS(el)) continue;
      const h = tag + '-' + (++i);
      el.setAttribute('__ATTR__', h);
      out.push({
        handle: h, tag: (el.tagName || '').toLowerCase(), role: ROLE_OF(el),
        label: LABEL_OF(el), text: T(el).slice(0, 120),
        value: el.value !== undefined ? String(el.value).slice(0, 120) : null,
        href: el.getAttribute ? (el.getAttribute('href') || null) : null,
      });
      if (out.length >= limit) break;
    }
    return {ok: true, matches: out, count: out.length, scanned: cands.length,
            url: location.href, title: document.title};
  } catch (e) { return {ok: false, error: String(e)}; }
})()
"""

_STATE_JS = r"""
(() => {
  __ROLES__
  __HELPERS__
  try {
    const el = LOOKUP(__HANDLE__);
    if (!el) return {ok: true, exists: false};
    return {ok: true, exists: true, tag: (el.tagName || '').toLowerCase(), role: ROLE_OF(el),
            label: LABEL_OF(el), text: T(el).slice(0, 400),
            value: el.value !== undefined ? String(el.value) : null,
            checked: el.checked === undefined ? null : !!el.checked,
            visible: VIS(el), enabled: el.disabled === undefined ? true : !el.disabled,
            url: location.href, title: document.title};
  } catch (e) { return {ok: false, error: String(e)}; }
})()
"""

_CLICK_JS = r"""
(() => {
  __ROLES__
  __HELPERS__
  try {
    const el = LOOKUP(__HANDLE__);
    if (!el) return {ok: false, error: 'handle not found (page changed?)'};
    el.scrollIntoView({block: 'center', inline: 'center'});
    try { el.focus({preventScroll: true}); } catch (e) {}
    const before = {url: location.href, text: T(document.body).slice(0, 200)};
    let method = 'click';
    if (typeof el.click === 'function') {
      el.click();
    } else {
      method = 'dispatched';
      for (const type of ['pointerdown', 'mousedown', 'mouseup', 'click']) {
        el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
      }
    }
    return {ok: true, tag: (el.tagName || '').toLowerCase(), role: ROLE_OF(el),
            label: LABEL_OF(el), method: method, before: before};
  } catch (e) { return {ok: false, error: String(e)}; }
})()
"""

_TYPE_JS = r"""
(() => {
  __ROLES__
  __HELPERS__
  try {
    const el = LOOKUP(__HANDLE__);
    if (!el) return {ok: false, error: 'handle not found (page changed?)'};
    const text = __TEXT__;
    el.scrollIntoView({block: 'center', inline: 'center'});
    try { el.focus({preventScroll: true}); } catch (e) {}
    const tag = (el.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea') {
      const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const desc = Object.getOwnPropertyDescriptor(proto, 'value');
      if (desc && desc.set) desc.set.call(el, text); else el.value = text;
      el.dispatchEvent(new Event('input', {bubbles: true}));
      el.dispatchEvent(new Event('change', {bubbles: true}));
    } else {
      el.textContent = text;
      el.dispatchEvent(new InputEvent('input', {bubbles: true, data: text, inputType: 'insertText'}));
    }
    if (__SUBMIT__) {
      for (const type of ['keydown', 'keypress', 'keyup']) {
        el.dispatchEvent(new KeyboardEvent(type, {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true}));
      }
    }
    return {ok: true, tag: tag, typed: text.length,
            value: el.value !== undefined ? String(el.value).slice(0, 200) : T(el).slice(0, 200)};
  } catch (e) { return {ok: false, error: String(e)}; }
})()
"""

_READ_JS = r"""
(() => {
  __ROLES__
  __HELPERS__
  try {
    const limit = __MAX__;
    const handle = __HANDLE__;
    if (handle) {
      const el = LOOKUP(handle);
      if (!el) return {ok: false, error: 'handle not found (page changed?)'};
      return {ok: true, text: T(el).slice(0, limit), url: location.href, title: document.title};
    }
    const body = (document.body ? (document.body.innerText || document.body.textContent || '') : '');
    return {ok: true, text: (body + '').replace(/\n{3,}/g, '\n\n').trim().slice(0, limit),
            url: location.href, title: document.title};
  } catch (e) { return {ok: false, error: String(e)}; }
})()
"""

_TEXT_JS = r"""
(() => {
  const body = (document.body ? (document.body.innerText || document.body.textContent || '') : '');
  return {ok: true, text: (body + '').replace(/\n{3,}/g, '\n\n').trim().slice(0, __MAX__),
          url: location.href, title: document.title};
})()
"""

_RESOLVE_FILE_INPUT_JS = r"""
(() => {
  __HELPERS__
  const handle = __HANDLE__;
  const selector = __SELECTOR__;
  let el = null;
  if (handle) el = LOOKUP(handle);
  else if (selector) el = document.querySelector(selector);
  if (!el) return null;
  const isFile = (n) => n && n.tagName === 'INPUT' && (n.getAttribute('type') || '').toLowerCase() === 'file';
  if (isFile(el)) return el;
  if (el.querySelector) {
    const inner = el.querySelector('input[type="file"]');
    if (inner) return inner;
  }
  const scope = (el.closest && (el.closest('label, form, [role="dialog"], div') || document)) || document;
  const near = scope.querySelector ? scope.querySelector('input[type="file"]') : null;
  return near || null;
})()
"""


def _js(template: str, **subs: str) -> str:
    out = (template
           .replace("__ROLES__", _ROLE_SELECTORS_JS)
           .replace("__HELPERS__", _HELPERS_JS.replace("__ATTR__", HANDLE_ATTR).replace("__PH__", "'x'")))
    for key, value in subs.items():
        out = out.replace(f"__{key}__", value)
    return out


def _unavailable(error: str) -> dict[str, Any]:
    return {"ok": False, "error": error}


# ---------------------------------------------------------------- observations
async def find(cdp: CdpManager, query: str, *, by: str = "text",
               url_contains: str | None = None, limit: int = 10) -> dict[str, Any]:
    """Locate elements by text / role / aria-label / CSS selector -> handles."""
    js = _js(_FIND_JS, QUERY=json.dumps(query or ""), BY=json.dumps(by or "text"),
             LIMIT=str(max(1, min(int(limit), 25))))
    return await cdp.eval_js(js, url_contains=url_contains)


async def state(cdp: CdpManager, handle: str, *, url_contains: str | None = None) -> dict[str, Any]:
    js = _js(_STATE_JS, HANDLE=json.dumps(handle))
    return await cdp.eval_js(js, url_contains=url_contains)


async def read(cdp: CdpManager, *, handle: str | None = None, max_chars: int = 4000,
               url_contains: str | None = None) -> dict[str, Any]:
    js = _js(_READ_JS, HANDLE=json.dumps(handle or ""), MAX=str(max(200, min(int(max_chars), 40000))))
    return await cdp.eval_js(js, url_contains=url_contains)


async def page_text(cdp: CdpManager, *, max_chars: int = 8000,
                    url_contains: str | None = None) -> dict[str, Any]:
    js = _js(_TEXT_JS, MAX=str(max(200, min(int(max_chars), 60000))))
    return await cdp.eval_js(js, url_contains=url_contains)


async def tabs(cdp: CdpManager) -> list[dict[str, Any]] | None:
    infos = await cdp.tabs()
    if infos is None:
        return None
    out: list[dict[str, Any]] = []
    for t in infos:
        url = t.get("url") or ""
        if url.startswith("chrome://") or "devtools" in url:
            continue
        out.append({"target_id": t.get("targetId"), "url": url, "title": t.get("title")})
    return out


# ---------------------------------------------------------------- act verbs
async def _resolve_handle(cdp: CdpManager, handle: str | None, query: str | None, by: str,
                          url_contains: str | None) -> tuple[str | None, dict[str, Any] | None]:
    """Handle given? use it. Otherwise find the first match (structural targeting)."""
    if handle:
        return handle, None
    if not query:
        return None, _unavailable("no handle and no query supplied")
    found = await find(cdp, query, by=by, url_contains=url_contains, limit=3)
    if not found.get("ok"):
        return None, found
    matches = found.get("matches") or []
    if not matches:
        return None, _unavailable(f"no element matched {query!r} (by={by})")
    return matches[0]["handle"], None


async def click(cdp: CdpManager, *, handle: str | None = None, query: str | None = None,
                by: str = "text", url_contains: str | None = None) -> dict[str, Any]:
    target, err = await _resolve_handle(cdp, handle, query, by, url_contains)
    if err is not None:
        return err
    js = _js(_CLICK_JS, HANDLE=json.dumps(target))
    res = await cdp.eval_js(js, url_contains=url_contains)
    if res.get("ok"):
        res["handle"] = target
        res["target"] = query or handle
    return res


async def type_into(cdp: CdpManager, *, text: str, handle: str | None = None,
                    query: str | None = None, by: str = "text", submit: bool = False,
                    url_contains: str | None = None) -> dict[str, Any]:
    target, err = await _resolve_handle(cdp, handle, query, by, url_contains)
    if err is not None:
        return err
    js = _js(_TYPE_JS, HANDLE=json.dumps(target), TEXT=json.dumps(text),
             SUBMIT="true" if submit else "false")
    res = await cdp.eval_js(js, url_contains=url_contains)
    if res.get("ok"):
        res["handle"] = target
        res["target"] = query or handle
        res["submitted"] = bool(submit)
    return res


async def upload(cdp: CdpManager, *, path: str, handle: str | None = None,
                 query: str | None = None, by: str = "selector",
                 url_contains: str | None = None) -> dict[str, Any]:
    """Attach a local file to the page's file input (DOM.setFileInputFiles)."""
    session = await cdp.ensure()
    if session is None:
        return _unavailable("CDP unavailable (no Chrome with --remote-debugging-port)")
    target, err = await _resolve_handle(cdp, handle, query, by, url_contains)
    if err is not None:
        return err
    js = _js(_RESOLVE_FILE_INPUT_JS, HANDLE=json.dumps(handle or ""),
             SELECTOR=json.dumps(query if by == "selector" else ""))
    try:
        page = await session.pick_target(url_contains)
        if page is None:
            return _unavailable(f"no tab matching {url_contains!r}")
        sid = await session.attach(page["targetId"])
        resolved = await session.send("Runtime.evaluate", {
            "expression": js, "returnByValue": False, "userGesture": True}, session_id=sid)
        obj = ((resolved.get("result") or {}) if isinstance(resolved, dict) else {}).get("objectId")
        if not obj:
            return _unavailable("no file input found on the page")
        await session.send("DOM.setFileInputFiles", {"files": [path], "objectId": obj}, session_id=sid)
    except Exception as exc:
        return _unavailable(f"upload failed: {type(exc).__name__}: {exc}")
    return {"ok": True, "path": path, "handle": target, "target": query or handle}


async def wait_for(cdp: CdpManager, *, query: str | None = None, by: str = "text",
                   expect_text: str | None = None, timeout_s: float = 10.0,
                   url_contains: str | None = None) -> dict[str, Any]:
    """Poll until the element/text appears (until-semantics, no blind sleeps)."""
    deadline = asyncio.get_event_loop().time() + max(0.5, float(timeout_s))
    target_desc = repr(expect_text) if expect_text is not None else repr(query)
    last: dict[str, Any] = {"ok": False, "error": "not started"}
    while True:
        if expect_text is not None:
            last = await page_text(cdp, url_contains=url_contains)
            if last.get("ok") and expect_text.casefold() in str(last.get("text") or "").casefold():
                return {"ok": True, "found": True, "text": expect_text, "note": "text visible"}
        elif query:
            last = await find(cdp, query, by=by, url_contains=url_contains, limit=1)
            if last.get("ok") and (last.get("matches") or []):
                return {"ok": True, "found": True, "matches": last.get("matches"),
                        "note": f"{query!r} visible"}
        if asyncio.get_event_loop().time() >= deadline:
            if not last.get("ok"):
                return last
            return {"ok": True, "found": False,
                    "note": f"timed out after {timeout_s}s waiting for {target_desc}"}
        await asyncio.sleep(0.4)


async def new_tab(cdp: CdpManager, url: str) -> dict[str, Any]:
    session = await cdp.ensure()
    if session is None:
        return _unavailable("CDP unavailable (no Chrome with --remote-debugging-port)")
    try:
        target = await session.create_target(url)
    except Exception as exc:
        return _unavailable(f"could not open tab: {exc}")
    if not target:
        return _unavailable("could not open tab")
    return {"ok": True, "target_id": target, "url": url}


async def close_tab(cdp: CdpManager, *, target_id: str | None = None,
                    url_contains: str | None = None) -> dict[str, Any]:
    session = await cdp.ensure()
    if session is None:
        return _unavailable("CDP unavailable (no Chrome with --remote-debugging-port)")
    try:
        if not target_id:
            page = await session.pick_target(url_contains)
            if page is None:
                return _unavailable(f"no tab matching {url_contains!r}")
            target_id = page["targetId"]
            url_contains = page.get("url")
        await session.send("Target.closeTarget", {"targetId": target_id})
    except Exception as exc:
        return _unavailable(f"could not close tab: {exc}")
    return {"ok": True, "target_id": target_id, "closed": url_contains}


async def activate_tab(cdp: CdpManager, *, target_id: str | None = None,
                       url_contains: str | None = None) -> dict[str, Any]:
    session = await cdp.ensure()
    if session is None:
        return _unavailable("CDP unavailable (no Chrome with --remote-debugging-port)")
    try:
        if not target_id:
            page = await session.pick_target(url_contains)
            if page is None:
                return _unavailable(f"no tab matching {url_contains!r}")
            target_id = page["targetId"]
            url_contains = page.get("url")
        await session.send("Target.activateTarget", {"targetId": target_id})
    except Exception as exc:
        return _unavailable(f"could not activate tab: {exc}")
    return {"ok": True, "target_id": target_id, "url": url_contains}
