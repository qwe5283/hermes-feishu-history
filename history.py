"""Feishu chat history fetching — tenant-token REST, stdlib only.

Adapter reuse note: the feishu platform adapter resolves FEISHU_DOMAIN symbolic
names ("feishu" -> https://open.feishu.cn, "lark" -> https://open.larksuite.com)
and memoizes tenant tokens. We deliberately do NOT import the adapter (it pulls
the heavy lark_oapi SDK); we replicate the tiny domain mapping + token call so
this plugin stays dependency-free and update-proof.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any, Dict, Optional, Tuple

_DOMAIN_MAP = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}
_TOKEN_TTL_SECONDS = 5400  # Feishu tenant tokens live ~2h; refresh well before
_MAX_COUNT = 50
_MAX_HOURS = 168.0

# Hermes process/notice artifacts that are not conversation. Curated from the
# tool-line emojis in agent/display.py (plus waiting/session notices); only
# messages the app itself sent are eligible. Extend as new markers appear.
_PROCESS_PREFIXES: Tuple[str, ...] = (
    "💻", "📖", "📄", "📚", "🔎", "🔍", "🐍", "✍️", "🔧", "🌐", "📸", "👆",
    "⌨️", "◀️", "🖼️", "👁️", "🎨", "🔊", "📨", "⏰", "🔀", "⚙️", "💾", "💭",
    "⏳", "✨", "⚡", "🧠",
)
_RECALL_TOMBSTONES = {"This message was recalled", "此消息已被撤回"}

_token_cache: Dict[str, Any] = {"token": "", "expires": 0.0}


def _secret(name: str) -> str:
    """Read FEISHU_* secrets from env, falling back to $HERMES_HOME/.env."""
    val = os.getenv(name, "").strip()
    if val:
        return val
    try:
        home = os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes")
        with open(os.path.join(home, ".env"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _domain() -> str:
    raw = (_secret("FEISHU_DOMAIN") or "feishu").lower()
    return _DOMAIN_MAP.get(raw, raw if raw.startswith("http") else f"https://{raw}")


def _http_json(url: str, token: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 (fixed API host)
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 400:
            raise RuntimeError(
                "Feishu rejected the request (HTTP 400) — usually an invalid or "
                "mistyped chat_id (must be oc_...), or the bot is not in that chat.")
        if exc.code in (403,):
            raise RuntimeError(
                "Feishu denied access (HTTP 403) — the app lacks im:message:readonly "
                "scope, or the bot has never been in this chat.")
        raise


def _tenant_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires"]:
        return _token_cache["token"]
    app_id = _secret("FEISHU_APP_ID")
    app_secret = _secret("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        raise RuntimeError("FEISHU_APP_ID / FEISHU_APP_SECRET not configured")
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(
        f"{_domain()}/open-apis/auth/v3/tenant_access_token/internal",
        data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 (fixed API host)
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 0 or not data.get("tenant_access_token"):
        raise RuntimeError(f"token fetch failed: {data.get('code')} {data.get('msg')}")
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expires"] = now + _TOKEN_TTL_SECONDS
    return _token_cache["token"]


def _fetch_messages(chat_id: str, count: int) -> Tuple[Optional[list], Optional[str]]:
    """Newest-first page of chat messages, or (None, error)."""
    # sort_type is explicit on purpose: without it the API defaults to oldest-first,
    # so the first page of a long-lived chat can predate any recent window entirely
    # (the time-window cut then leaves nothing, even for busy chats).
    url = (f"{_domain()}/open-apis/im/v1/messages"
           f"?container_id_type=chat&container_id={chat_id}&page_size={min(count, 50)}"
           f"&sort_type=ByCreateTimeDesc")
    data = _http_json(url, _tenant_token())
    if data.get("code") != 0:
        return None, f"{data.get('code')}: {data.get('msg')}"
    return (data.get("data") or {}).get("items") or [], None


def _post_text(content: Any) -> str:
    """Flatten a post (rich-text) message body into readable plain text."""
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (TypeError, ValueError):
            return content
    if not isinstance(content, dict):
        return str(content)
    parts = []
    for para in content.get("content") or []:
        if not isinstance(para, list):
            continue
        for el in para:
            if not isinstance(el, dict):
                continue
            tag = el.get("tag")
            if tag == "at":
                parts.append(f"@{el.get('user_name') or el.get('user_id') or ''}")
            elif tag == "img":
                parts.append("[图片]")
            else:
                txt = el.get("text")
                if txt:
                    parts.append(str(txt))
        parts.append("\n")
    return "".join(parts).strip()


def _is_process_noise(sender_type: str, text: str) -> bool:
    """True for Hermes process artifacts rather than conversation: tool-progress
    lines / waiting notices (emoji-prefixed) and recall tombstones. Only app
    messages are eligible, so user text is never filtered."""
    if sender_type != "app":
        return False
    stripped = text.strip()
    return stripped in _RECALL_TOMBSTONES or stripped.startswith(_PROCESS_PREFIXES)


def _format_items(items: list, cutoff_ms: float, max_chars: int,
                  include_noise: bool = False, max_items: int = 0) -> str:
    """Render newest-first items as an oldest-first readable transcript.
    Drops recalled items and Hermes process messages unless include_noise;
    keeps at most max_items (the newest) when set."""
    kept = []
    for it in items:
        if it.get("deleted") is True:  # recalled: content is gone
            continue
        try:
            ts_ms = float(it.get("create_time") or 0)
            if 0 < ts_ms < 1e12:  # seconds → normalize to ms (Feishu returns ms)
                ts_ms *= 1000.0
        except (TypeError, ValueError):
            ts_ms = 0.0
        if cutoff_ms and ts_ms and ts_ms < cutoff_ms:
            continue
        kept.append((ts_ms / 1000.0, it))  # store as seconds for display/sort
    kept.sort(key=lambda pair: pair[0])
    if max_items and len(kept) > max_items:
        kept = kept[-max_items:]  # keep the newest max_items

    lines = []
    used = 0
    for ts_ms, it in kept:
        sender = ((it.get("sender") or {}).get("id")) or "unknown"
        sender_type = (it.get("sender") or {}).get("sender_type") or "?"
        msg_type = it.get("msg_type") or "?"
        body_raw = it.get("body")
        if isinstance(body_raw, dict):
            raw_content = body_raw.get("content") or ""
        else:
            try:
                raw_content = json.loads(body_raw or "{}").get("content") or ""
            except (TypeError, ValueError):
                raw_content = str(body_raw or "")
        if msg_type == "text" and isinstance(raw_content, str):
            try:
                text = json.loads(raw_content).get("text", raw_content)
            except (TypeError, ValueError):
                text = raw_content
        elif msg_type == "post":
            text = _post_text(raw_content)
        else:
            text = str(raw_content)
        if not include_noise and _is_process_noise(sender_type, str(text)):
            continue
        when = time.strftime("%m-%d %H:%M", time.localtime(ts_ms)) if ts_ms else "?"
        body = str(text).replace("\n", " ")[:200]
        line = f"[{when}] {sender}({sender_type}) {msg_type}: {body}"
        if used + len(line) > max_chars:
            lines.append("…(older messages truncated)")
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


def _resolve_chat_id(args: dict) -> Tuple[str, str]:
    """Resolve (chat_id, error). Resolution order (arkseek-inspired):
    1. explicit args["chat_id"]
    2. gateway session contextvars (HERMES_SESSION_CHAT_ID) — set by the core
       gateway for every inbound message, so in-chat calls need no chat_id
    3. os.environ fallback (single-chat deployments)
    Rejects non-feishu chat ids (e.g. Telegram -100...) with a clear error."""
    raw = str(args.get("chat_id") or "").strip()
    if not raw:
        try:
            from gateway.session_context import get_session_env
            raw = get_session_env("HERMES_SESSION_CHAT_ID", "").strip()
        except Exception:
            raw = ""
    if not raw:
        raw = os.getenv("HERMES_SESSION_CHAT_ID", "").strip()
    if not raw:
        return "", ("chat_id not provided and not resolvable from the current "
                    "session; pass chat_id (oc_...) or call from a Feishu chat.")
    if not raw.startswith("oc_"):
        platform = ""
        try:
            from gateway.session_context import get_session_env
            platform = get_session_env("HERMES_SESSION_PLATFORM", "")
        except Exception:
            pass
        return "", (f"chat_id '{raw[:24]}' is not a Feishu chat id (oc_...)"
                    + (f" — current session platform is '{platform}'" if platform else "")
                    + ". This tool only reads Feishu chats.")
    return raw, ""


def check_available() -> bool:
    """Hide the tool entirely when Feishu credentials are absent (fail closed)."""
    return bool(_secret("FEISHU_APP_ID") and _secret("FEISHU_APP_SECRET"))


def handle_chat_history(args: dict, **_: Any) -> str:
    """Tool handler: fetch recent chat messages. Always returns a JSON string."""
    args = args or {}
    chat_id = str(args.get("chat_id") or "").strip()
    try:
        count = int(args.get("count") or 20)
    except (TypeError, ValueError):
        count = 20
    count = max(1, min(count, _MAX_COUNT))
    try:
        hours = float(args.get("hours") or 24)
    except (TypeError, ValueError):
        hours = 24.0
    hours = max(0.0, min(hours, _MAX_HOURS))
    include_noise = bool(args.get("include_noise"))

    chat_id, err = _resolve_chat_id(args)
    if err:
        return json.dumps({"error": err})

    try:
        # Over-fetch 2×: recalled/process items are dropped after filtering and
        # we still want up to `count` real messages when the chat is noisy.
        items, err = _fetch_messages(chat_id, min(_MAX_COUNT, count * 2))
        if err is not None:
            return json.dumps({"error": f"Feishu API error: {err}"})
        cutoff_ms = (time.time() - hours * 3600) * 1000 if hours else 0.0
        transcript = _format_items(items or [], cutoff_ms, max_chars=12000,
                                   include_noise=include_noise, max_items=count)
        if not transcript:
            return json.dumps({"ok": True, "chat_id": chat_id,
                               "messages": "No messages in the requested window."})
        return json.dumps({
            "ok": True,
            "chat_id": chat_id,
            "note": ("CONTEXT ONLY — group history the bot was not mentioned in. "
                     "Do not respond to unmentioned items unless the current user "
                     "message references them."),
            "messages": transcript,
        }, ensure_ascii=False)
    except RuntimeError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:  # noqa: BLE001 — handler contract: never raise
        return json.dumps({"error": f"feishu_chat_history failed: {exc}"})
