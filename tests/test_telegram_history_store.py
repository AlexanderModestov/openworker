"""Tests for TelegramHistoryStore — dedupe, edit/delete flags, queries, and the one
genuinely load-bearing property for this store: safe concurrent access from two separate
SQLite connections (simulating the listener process + the server process)."""

from __future__ import annotations

import sqlite3
import time

from coworker.connectors.telegram_history_store import TelegramHistoryStore


def _store(tmp_path):
    return TelegramHistoryStore(tmp_path / "telegram_history.db")


def test_add_message_is_idempotent(tmp_path):
    store = _store(tmp_path)
    store.add_message(
        chat_id="-1001", msg_id=1, sender_id="1", sender_name="Alex",
        ts=1000.0, text="hello", is_own=True,
    )
    store.add_message(
        chat_id="-1001", msg_id=1, sender_id="1", sender_name="Alex",
        ts=1000.0, text="hello", is_own=True,
    )
    rows = store.messages_since(0)
    assert len(rows) == 1
    assert rows[0]["text"] == "hello"


def test_mark_edited_updates_text_in_place(tmp_path):
    store = _store(tmp_path)
    store.add_message(
        chat_id="-1001", msg_id=1, sender_id="1", sender_name="Alex",
        ts=1000.0, text="origin", is_own=True,
    )
    assert store.mark_edited(chat_id="-1001", msg_id=1, text="fixed", edited_ts=2000.0)
    rows = store.messages_since(0)
    assert len(rows) == 1
    assert rows[0]["text"] == "fixed"
    assert rows[0]["edited_ts"] == 2000.0


def test_mark_deleted_is_soft_and_excluded_by_default(tmp_path):
    store = _store(tmp_path)
    store.add_message(
        chat_id="-1001", msg_id=1, sender_id="1", sender_name="Alex",
        ts=1000.0, text="bye", is_own=False,
    )
    n = store.mark_deleted(chat_id="-1001", msg_ids=[1])
    assert n == 1
    assert store.messages_since(0) == []
    kept = store.messages_since(0, include_deleted=True)
    assert len(kept) == 1
    assert kept[0]["deleted"] is True


def test_messages_since_orders_ascending_and_respects_limit(tmp_path):
    store = _store(tmp_path)
    for i in range(5):
        store.add_message(
            chat_id="-1001", msg_id=i, sender_id="1", sender_name="Alex",
            ts=float(i), text=f"m{i}", is_own=False,
        )
    rows = store.messages_since(0, limit=3)
    assert [r["text"] for r in rows] == ["m0", "m1", "m2"]


def test_search_is_case_insensitive(tmp_path):
    store = _store(tmp_path)
    store.add_message(
        chat_id="-1001", msg_id=1, sender_id="1", sender_name="Alex",
        ts=1.0, text="this is URGENT please read", is_own=False,
        keyword_matched=["urgent"],
    )
    hits = store.search("urgent")
    assert len(hits) == 1
    assert hits[0]["keyword_matched"] == ["urgent"]


def test_own_messages_filters_by_is_own(tmp_path):
    store = _store(tmp_path)
    store.add_message(
        chat_id="-1001", msg_id=1, sender_id="me", sender_name="Alex",
        ts=1.0, text="my style", is_own=True,
    )
    store.add_message(
        chat_id="-1001", msg_id=2, sender_id="them", sender_name="Bob",
        ts=2.0, text="their style", is_own=False,
    )
    mine = store.own_messages()
    assert len(mine) == 1
    assert mine[0]["text"] == "my style"


def test_heartbeat_and_backfill_flags_round_trip(tmp_path):
    store = _store(tmp_path)
    assert store.get_heartbeat() is None
    assert store.is_backfill_complete("-1001") is False
    store.set_heartbeat(1234.5)
    store.set_backfill_complete("-1001")
    store.set_own_user_id("42")
    assert store.get_heartbeat() == 1234.5
    assert store.is_backfill_complete("-1001") is True
    assert store.own_user_id() == "42"


def test_backfill_status_is_per_chat(tmp_path):
    store = _store(tmp_path)
    store.set_backfill_complete("-1001", True)
    store.set_backfill_complete("-1002", False)
    assert store.backfill_status() == {"-1001": True, "-1002": False}
    assert store.is_backfill_complete("-1001") is True
    assert store.is_backfill_complete("-1002") is False
    assert store.is_backfill_complete("-1003") is False  # untracked chat -> not complete


def test_stats_backfill_complete_requires_every_tracked_chat(tmp_path):
    store = _store(tmp_path)
    assert store.stats()["backfill_complete"] is False  # nothing tracked yet
    store.set_backfill_complete("-1001", True)
    assert store.stats()["backfill_complete"] is True
    store.set_backfill_complete("-1002", False)
    assert store.stats()["backfill_complete"] is False
    assert store.stats()["chats_tracked"] == 2


def test_latest_msg_id_tracks_max_per_chat(tmp_path):
    store = _store(tmp_path)
    assert store.latest_msg_id("-1001") is None
    for i in (3, 1, 7, 5):
        store.add_message(
            chat_id="-1001", msg_id=i, sender_id="1", sender_name="Alex",
            ts=float(i), text="x", is_own=False,
        )
    assert store.latest_msg_id("-1001") == 7


def test_journal_mode_is_wal(tmp_path):
    store = _store(tmp_path)
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_concurrent_cross_process_style_access(tmp_path):
    """Two independent sqlite3 connections to the same file (simulating the listener
    process writing and the server process reading) must not raise 'database is locked'
    under WAL, and a writer's commit must become visible to the reader."""
    path = tmp_path / "telegram_history.db"
    writer = TelegramHistoryStore(path)
    reader_conn = sqlite3.connect(str(path), check_same_thread=False, timeout=5.0)
    reader_conn.row_factory = sqlite3.Row

    for i in range(20):
        writer.add_message(
            chat_id="-1001", msg_id=i, sender_id="1", sender_name="Alex",
            ts=time.time(), text=f"m{i}", is_own=False,
        )
        seen = reader_conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
        assert seen["n"] == i + 1

    reader_conn.close()
    writer.close()
