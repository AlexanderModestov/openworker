"""Read-only agent tools over the Telegram work-chat mirror (`TelegramHistoryStore`).

These are the ONLY tools that read `telegram_history.db` — they never write to it (writing
is the standalone `telegram_listener` process's job) and they never touch the Telegram API
directly. Distinct from `send_message`/`tools.py`: that's the existing Bot-API connector
(a different Telegram identity, DMing the user); this is a mirror of a private work chat
read via the user's own personal account. All five tools are read-only and ungated —
`risk_level="low"`, `requires_approval=False`, matching `make_web_search_tool`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

import aisuite as ai

from .telegram_history_store import TelegramHistoryStore


def _meta(name: str) -> ai.ToolMetadata:
    return ai.ToolMetadata(
        name=name,
        category="connector",
        risk_level="low",
        capabilities=["messaging"],
        requires_approval=False,
    )


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def _attach(fn: Callable[..., Any], schema: dict[str, Any]) -> Callable[..., Any]:
    name = schema["function"]["name"]
    fn.__name__ = name
    fn.__doc__ = schema["function"]["description"]
    fn.__coworker_schema__ = schema
    fn.__aisuite_tool_metadata__ = _meta(name)
    return fn


def _human_ts(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _humanize(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        r = dict(r)
        r["ts"] = _human_ts(r.get("ts"))
        if r.get("edited_ts") is not None:
            r["edited_ts"] = _human_ts(r["edited_ts"])
        out.append(r)
    return out


def make_telegram_history_tools(store: TelegramHistoryStore) -> list[Callable[..., Any]]:
    """Build the read-only work-chat tools bound to a `TelegramHistoryStore`."""

    def get_recent_work_chat_messages(since_minutes: int = 60, limit: int = 200) -> dict:
        since_minutes = since_minutes if isinstance(since_minutes, int) else 60
        cutoff = datetime.now().timestamp() - max(1, since_minutes) * 60
        rows = store.messages_since(cutoff, limit=max(1, min(limit, 2000)))
        return {"messages": _humanize(rows)}

    def search_work_chat(keyword: str, limit: int = 50) -> dict:
        rows = store.search(keyword, limit=max(1, min(limit, 500)))
        return {"messages": _humanize(rows)}

    def get_messages_by_sender(sender_id: str, limit: int = 50) -> dict:
        rows = store.messages_by_sender(sender_id, limit=max(1, min(limit, 500)))
        return {"messages": _humanize(rows)}

    def get_my_own_messages(limit: int = 100) -> dict:
        rows = store.own_messages(limit=max(1, min(limit, 500)))
        return {"messages": _humanize(rows)}

    def check_work_chat_health() -> dict:
        stats = store.stats()
        heartbeat_ts = stats.get("heartbeat_ts")
        age = (
            datetime.now().timestamp() - heartbeat_ts if heartbeat_ts is not None else None
        )
        return {
            "message_count": stats["message_count"],
            "oldest_message_at": _human_ts(stats.get("oldest_ts")),
            "newest_message_at": _human_ts(stats.get("newest_ts")),
            "backfill_complete": stats["backfill_complete"],
            "heartbeat_at": _human_ts(heartbeat_ts),
            "heartbeat_age_seconds": age,
        }

    return [
        _attach(
            get_recent_work_chat_messages,
            _schema(
                "get_recent_work_chat_messages",
                "Messages from the user's private work Telegram chat (read-only mirror via "
                "their personal account) from the last `since_minutes` minutes. Use this for "
                "digests and to check what's new since the last check.",
                {
                    "since_minutes": {
                        "type": "integer",
                        "description": "How far back to look, in minutes. Default 60.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max messages to return. Default 200.",
                    },
                },
                [],
            ),
        ),
        _attach(
            search_work_chat,
            _schema(
                "search_work_chat",
                "Keyword search over the full retained history of the user's private work "
                "Telegram chat.",
                {
                    "keyword": {"type": "string", "description": "Substring to search for."},
                    "limit": {"type": "integer", "description": "Max results. Default 50."},
                },
                ["keyword"],
            ),
        ),
        _attach(
            get_messages_by_sender,
            _schema(
                "get_messages_by_sender",
                "Past messages from one specific sender in the work chat, most recent first.",
                {
                    "sender_id": {
                        "type": "string",
                        "description": "The Telegram user id of the sender.",
                    },
                    "limit": {"type": "integer", "description": "Max results. Default 50."},
                },
                ["sender_id"],
            ),
        ),
        _attach(
            get_my_own_messages,
            _schema(
                "get_my_own_messages",
                "The user's OWN past messages in the work chat. Use these as style samples "
                "before drafting a suggested reply, so the draft sounds like the user's own "
                "voice (tone, punctuation, typical length, greetings) rather than generic.",
                {"limit": {"type": "integer", "description": "Max results. Default 100."}},
                [],
            ),
        ),
        _attach(
            check_work_chat_health,
            _schema(
                "check_work_chat_health",
                "Health of the work-chat listener: message count, backfill status, and how "
                "long ago the listener last confirmed it's alive (heartbeat). Use this before "
                "a digest or alert scan, and to answer 'is the work chat still being read'.",
                {},
                [],
            ),
        ),
    ]
