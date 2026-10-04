"""feishu-history plugin — on-demand group chat history backfill.

Tools:
  feishu_chat_history — fetch recent messages from a Feishu chat so the agent
  can recover context when a user references discussions the bot was never
  mentioned in (require_mention=true means unmentioned messages are dropped
  upstream and never reach the session).

v1.2.0: also registers a pre_gateway_dispatch hook that auto-injects recent
group history as channel_context on triggered messages (lurker-mode backfill,
Telegram observe-style; DMs excluded).

Read-only: uses GET /open-apis/im/v1/messages with the app's tenant token.
Credentials come from the same FEISHU_APP_ID / FEISHU_APP_SECRET env the
feishu platform adapter already uses — no duplicate configuration.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from . import history

_TOOLS = (
    (
        "feishu_chat_history",
        {
            "name": "feishu_chat_history",
            "description": (
                "Use whenever the user references earlier chat discussion you haven't seen "
                "(\"上面说的\", \"as said above\", \"刚才\", a plan/decision from group chatter) — "
                "call this BEFORE claiming you cannot see history. Fetches recent messages from "
                "a Feishu/Lark chat (group or DM) as background context, including messages the "
                "bot was NOT mentioned in (independent of require_mention / allow_bots; pulls "
                "via API up to 7 days back). Hermes process messages (tool progress, notices, "
                "recall placeholders) are filtered out by default. Messages are returned oldest-first with sender and "
                "timestamp. Treat the result as CONTEXT ONLY: do not respond to questions "
                "addressed to other people, and never reply to unmentioned items unless the "
                "current user message asks about them. chat_id defaults to the current "
                "conversation when omitted."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "string",
                        "description": "Feishu chat id (oc_...). Omit it when the user is asking about the current Feishu conversation — it auto-resolves from the active session.",
                    },
                    "count": {
                        "type": "integer",
                        "description": "How many recent messages to fetch (1-200; auto-paginates past the 50/page API cap). Default 20.",
                    },
                    "hours": {
                        "type": "number",
                        "description": "Only include messages newer than this many hours. Default 24, max 168 (7 days).",
                    },
                    "include_noise": {
                        "type": "boolean",
                        "description": "Keep Hermes process messages (tool-progress lines, waiting notices, recall placeholders) instead of filtering them. Default false.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
        history.handle_chat_history,
        "🕘",
    ),
)


def register(ctx) -> None:
    """Register tools with the plugin loader (called once at discovery)."""
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="feishu_history",
            schema=schema,
            handler=handler,
            check_fn=history.check_available,
            emoji=emoji,
        )
    # Lurker-mode backfill hook (v1.2.0). Cores without plugin-hook support
    # (or with the hook disabled) keep the manual tool — never fatal.
    register_hook = getattr(ctx, "register_hook", None)
    if register_hook is not None:
        try:
            register_hook("pre_gateway_dispatch", history.auto_inject_hook)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("plugins.feishu-history").warning(
                "pre_gateway_dispatch hook not registered: %s", exc)
