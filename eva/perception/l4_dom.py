"""L4 — DOM / browser inspection (doc 04). WhatsApp Web adapters.

All functions degrade honestly: if CDP or the WA tab is unavailable the answer
is UNKNOWN (never a guess). Selectors carry fallbacks because WA Web's DOM is
volatile; when nothing matches, we say so instead of pretending.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from .l3_cdp import CdpManager

WA_URL_MARKER = "web.whatsapp.com"

WA_STATE_JS = r"""
(() => {
  const out = {ok:true};
  try {
    const q = (s) => document.querySelector(s);
    const all = (s) => Array.from(document.querySelectorAll(s));
    const T = (el) => (el ? (el.innerText || el.textContent || '').trim() : '');
    out.url = location.href;
    out.title = document.title;
    out.ready = document.readyState;
    const qrSel = 'canvas[aria-label*="Scan"], [data-testid="qrcode"], div[data-ref]';
    out.qr_visible = !!q(qrSel);
    const pane = q('#pane-side');
    out.chatlist_present = !!pane;
    out.logged_in = !!pane && !out.qr_visible;
    let archived = null;
    if (q('[data-testid="archived-banner"], [data-testid="archived-chats-banner"]')) archived = 'banner';
    if (!archived && pane) {
      const cands = all('#pane-side span, #pane-side div').filter(
        el => el.children.length === 0 && /^archived$/i.test(T(el)));
      for (const c of cands) {
        const r = c.getBoundingClientRect();
        if (r.top >= 0 && r.top < 280 && r.width > 0 && r.height > 0) { archived = 'header'; break; }
      }
    }
    out.view = out.qr_visible ? 'qr_login' : (archived ? 'archived' : (pane ? 'chatlist' : 'unknown));
    const search = q('[data-testid="chat-list-search"]') || all('div[contenteditable="true"]')[0] || null;
    let sq = search ? T(search) : null;
    if (sq === '') sq = null;
    out.search_query = sq;
    const title = q('[data-testid="conversation-info-header-chat-title"]') || q('#main header span[title]');
    out.selected_chat = title ? (title.getAttribute('title') || T(title)) : null;
    out.chat_count = pane ? pane.querySelectorAll('[role="listitem"], [data-testid="cell-frame-container"]').length : 0;
    return out;
  } catch (e) { return {ok:false, error: String(e)}; }
})()
"""

WA_CLICK_ARCHIVED_JS = r"""
(() => {
  const all = (s) => Array.from(document.querySelectorAll(s));
  const T = (el) => (el ? (el.innerText || el.textContent || '').trim() : '');
  const cands = all('#pane-side span, #pane-side div').filter(
    el => el.children.length === 0 && /^archived$/i.test(T(el)));
  for (const c of cands) {
    const r = c.getBoundingClientRect();
    if (r.top >= 0 && r.top < 280 && r.width > 0) {
      c.click();
      return {ok:true, clicked:true};
    }
  }
  const banner = document.querySelector('[data-testid="archived-banner"], [data-testid="archived-chats-banner"]');
  if (banner) return {ok:true, clicked:false, note:'already in archived view'};
  return {ok:false, error:'archived row not found'};
})()
"""

WA_SEARCH_JS = r"""
(() => {
  const all = (s) => Array.from(document.querySelectorAll(s));
  const el = document.querySelector('[data-testid="chat-list-search"]') || all('div[contenteditable="true"]')[0];
  if (!el) return {ok:false, error:'search box not found'};
  el.focus();
  const q = %QUERY%;
  if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
    el.value = q;
    el.dispatchEvent(new Event('input', {bubbles:true}));
  } else {
    el.textContent = q;
    el.dispatchEvent(new InputEvent('input', {bubbles:true, data:q, inputType:'insertText'}));
  }
  return {ok:true};
})()
"""

WA_OPEN_CHAT_JS = r"""
(() => {
  const T = (el) => (el ? (el.innerText || el.textContent || '').trim() : '');
  const items = Array.from(document.querySelectorAll(
    '#pane-side [role="listitem"], #pane-side [data-testid="cell-frame-container"]'));
  const target = %TARGET%.toLowerCase();
  let fuzzy = null;
  for (const it of items) {
    const tEl = it.querySelector('[title]');
    const label = (tEl ? tEl.getAttribute('title') : '') || T(it.querySelector('[data-testid="cell-frame-title"]') || it);
    const low = (label || '').toLowerCase();
    if (low === target) { it.click(); return {ok:true, matched:label}; }
    if (!fuzzy && low.includes(target)) fuzzy = {it, label};
  }
  if (fuzzy) { fuzzy.it.click(); return {ok:true, matched:fuzzy.label, fuzzy:true}; }
  return {ok:false, error:'no chat row matched ' + target, count: items.length};
})()
"""

WA_TRANSCRIPT_HAS_JS = r"""
(() => {
  const T = (el) => (el ? (el.innerText || el.textContent || '').trim() : '');
  const rows = Array.from(document.querySelectorAll('[data-testid="msg-container"], div.message-out, div.message-in'));
  const needle = %NEEDLE%.toLowerCase();
  for (let i = rows.length - 1; i >= Math.max(0, rows.length - 30); i--) {
    if (T(rows[i]).toLowerCase().includes(needle)) return {ok:true, found:true};
  }
  return {ok:true, found:false, rows: rows.length};
})()
"""

WA_TYPE_SEND_JS = r"""
(() => {
  const all = (s) => Array.from(document.querySelectorAll(s));
  const box = document.querySelector('[data-testid="conversation-compose-box-input"]')
    || all('div[contenteditable="true"]').pop();
  if (!box) return {ok:false, error:'compose box not found'};
  box.focus();
  const text = %TEXT%;
  box.textContent = text;
  box.dispatchEvent(new InputEvent('input', {bubbles:true, data:text, inputType:'insertText'}));
  return {ok:true, typed:true};
})()
"""

WA_CLICK_SEND_JS = r"""
(() => {
  const q = (s) => document.querySelector(s);
  const send = q('[data-testid="send"]') || q('button[aria-label*="Send"]')
    || q('span[data-icon="send"]');
  if (!send) return {ok:false, error:'send button not found'};
  (send.closest('button') || send).click();
  return {ok:true, clicked:true};
})()
"""


async def wa_state(cdp: CdpManager) -> dict[str, Any] | None:
    res = await cdp.eval_js(WA_STATE_JS, url_contains=WA_URL_MARKER)
    if not res.get("ok"):
        return None
    value = res.get("value")
    return value if isinstance(value, dict) else None


async def wa_click_archived(cdp: CdpManager) -> dict[str, Any]:
    return await cdp.eval_js(WA_CLICK_ARCHIVED_JS, url_contains=WA_URL_MARKER)


async def wa_search(cdp: CdpManager, query: str) -> dict[str, Any]:
    return await cdp.eval_js(WA_SEARCH_JS.replace("%QUERY%", json.dumps(query)),
                             url_contains=WA_URL_MARKER)


async def wa_open_chat(cdp: CdpManager, name: str) -> dict[str, Any]:
    return await cdp.eval_js(WA_OPEN_CHAT_JS.replace("%TARGET%", json.dumps(name)),
                             url_contains=WA_URL_MARKER)


async def wa_type_message(cdp: CdpManager, text: str) -> dict[str, Any]:
    return await cdp.eval_js(WA_TYPE_SEND_JS.replace("%TEXT%", json.dumps(text)),
                             url_contains=WA_URL_MARKER)


async def wa_click_send(cdp: CdpManager) -> dict[str, Any]:
    return await cdp.eval_js(WA_CLICK_SEND_JS, url_contains=WA_URL_MARKER)


async def wa_transcript_contains(cdp: CdpManager, text: str) -> bool | None:
    res = await cdp.eval_js(WA_TRANSCRIPT_HAS_JS.replace("%NEEDLE%", json.dumps(text)),
                            url_contains=WA_URL_MARKER)
    if not res.get("ok"):
        return None
    value = res.get("value") or {}
    return bool(value.get("found")) if isinstance(value, dict) else None


async def wa_send_message(cdp: CdpManager, recipient: str, text: str,
                          wait_s: float = 3.0) -> dict[str, Any]:
    """Full outbound flow at L4: ensure chat open -> type -> send. Verifier
    independently re-reads the transcript (doc 03 §5 rule 2)."""
    state = await wa_state(cdp)
    if state is None:
        return {"ok": False, "error": "WhatsApp Web tab not reachable via CDP"}
    if not state.get("logged_in"):
        return {"ok": False, "error": "WhatsApp not logged in (QR screen)"}
    current = (state.get("selected_chat") or "").casefold()
    if recipient.casefold() not in current:
        await wa_search(cdp, recipient)
        await asyncio.sleep(0.8)
        opened = await wa_open_chat(cdp, recipient)
        if not opened.get("ok"):
            return {"ok": False, "error": f"could not open chat with {recipient!r}: {opened.get('error')}"}
        await asyncio.sleep(0.6)
    typed = await wa_type_message(cdp, text)
    if not typed.get("ok"):
        return {"ok": False, "error": typed.get("error", "typing failed")}
    await asyncio.sleep(0.3)
    sent = await wa_click_send(cdp)
    if not sent.get("ok"):
        return {"ok": False, "error": sent.get("error", "send click failed")}
    deadline = asyncio.get_event_loop().time() + wait_s
    while asyncio.get_event_loop().time() < deadline:
        found = await wa_transcript_contains(cdp, text)
        if found:
            return {"ok": True, "note": "message visible in transcript"}
        await asyncio.sleep(0.4)
    return {"ok": True, "note": "send clicked; transcript confirmation not observed in time"}
