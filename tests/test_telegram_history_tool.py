"""Tests for the read-only work-chat tools (telegram_history_tool.py) against a real
TelegramHistoryStore (SQLite, tmp_path) — no Telethon/network involved."""

from __future__ import annotations

import time

from coworker.connectors.telegram_history_store import TelegramHistoryStore
from coworker.connectors.telegram_history_tool import make_telegram_history_tools


def _tools(tmp_path):
    store = TelegramHistoryStore(tmp_path / "telegram_history.db")
    fns = {fn.__name__: fn for fn in make_telegram_history_tools(store)}
    return store, fns


def test_all_tools_are_read_only_and_ungated(tmp_path):
    _, fns = _tools(tmp_path)
    assert set(fns) == {
        "get_recent_work_chat_messages",
        "search_work_chat",
        "get_messages_by_sender",
        "get_my_own_messages",
        "check_work_chat_health",
    }
    for fn in fns.values():
        meta = fn.__aisuite_tool_metadata__
        assert meta.risk_level == "low"
        assert meta.requires_approval is False
        assert fn.__coworker_schema__["function"]["name"] == fn.__name__


def test_get_recent_work_chat_messages_filters_by_window(tmp_path):
    store, fns = _tools(tmp_path)
    now = time.time()
    store.add_message(
        chat_id="-1001", msg_id=1, sender_id="1", sender_name="Alex",
        ts=now - 3600 * 3, text="old", is_own=False,
    )
    store.add_message(
        chat_id="-1001", msg_id=2, sender_id="1", sender_name="Alex",
        ts=now - 60, text="recent", is_own=False,
    )
    result = fns["get_recent_work_chat_messages"](since_minutes=60)
    texts = [m["text"] for m in result["messages"]]
    assert texts == ["recent"]


def test_timestamps_are_human_readable_strings(tmp_path):
    store, fns = _tools(tmp_path)
    store.add_message(
        chat_id="-1001", msg_id=1, sender_id="1", sender_name="Alex",
        ts=time.time(), text="hi", is_own=False,
    )
    result = fns["get_recent_work_chat_messages"](since_minutes=60)
    ts = result["messages"][0]["ts"]
    assert isinstance(ts, str)
    assert "T" in ts  # ISO format


def test_search_work_chat(tmp_path):
    store, fns = _tools(tmp_path)
    store.add_message(
        chat_id="-1001", msg_id=1, sender_id="1", sender_name="Alex",
        ts=time.time(), text="this is urgent", is_own=False, keyword_matched=["urgent"],
    )
    store.add_message(
        chat_id="-1001", msg_id=2, sender_id="1", sender_name="Alex",
        ts=time.time(), text="just chatting", is_own=False,
    )
    result = fns["search_work_chat"](keyword="urgent")
    assert len(result["messages"]) == 1
    assert result["messages"][0]["text"] == "this is urgent"


def test_get_my_own_messages_for_style_samples(tmp_path):
    store, fns = _tools(tmp_path)
    store.add_message(
        chat_id="-1001", msg_id=1, sender_id="me", sender_name="Alex",
        ts=time.time(), text="hey team, sounds good!", is_own=True,
    )
    store.add_message(
        chat_id="-1001", msg_id=2, sender_id="them", sender_name="Bob",
        ts=time.time(), text="not mine", is_own=False,
    )
    result = fns["get_my_own_messages"]()
    assert len(result["messages"]) == 1
    assert result["messages"][0]["text"] == "hey team, sounds good!"


def test_check_work_chat_health_reports_heartbeat_age(tmp_path):
    store, fns = _tools(tmp_path)
    store.set_heartbeat(time.time() - 120)
    store.set_backfill_complete("-1001")
    result = fns["check_work_chat_health"]()
    assert result["backfill_complete"] is True
    assert result["heartbeat_age_seconds"] >= 120
    assert result["message_count"] == 0


def test_check_work_chat_health_before_any_heartbeat(tmp_path):
    _, fns = _tools(tmp_path)
    result = fns["check_work_chat_health"]()
    assert result["heartbeat_at"] is None
    assert result["heartbeat_age_seconds"] is None
    assert result["backfill_complete"] is False
