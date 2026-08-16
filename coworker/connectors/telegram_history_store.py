"""SQLite-backed store for the read-only Telegram work-chat mirror.

Written by the standalone `telegram_listener` process (a Telethon/MTProto user-session
client), read by `openworker-server` (via `telegram_history_tool.py`) to power the daily
digest, the alert-scan automation, and on-demand questions asked through the existing
Telegram Bot connector. The two processes never share a live connection — this file, in
WAL mode, is the entire integration surface between them.

WAL (not the default rollback journal) is required here specifically because this is the
one store in the project opened concurrently by two OS processes rather than just multiple
threads within one process: WAL lets the listener's writer and the server's readers proceed
without blocking each other. `busy_timeout` covers the rare moment both sides touch the
file in the same instant. The `threading.RLock` below only protects against races between
threads *within* one process (the server itself is multi-threaded) — cross-process safety
comes entirely from SQLite's own WAL locking, not from this lock.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional


class TelegramHistoryStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init()

    def _init(self) -> None:
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id         TEXT NOT NULL,
                    msg_id          INTEGER NOT NULL,
                    sender_id       TEXT,
                    sender_name     TEXT,
                    ts              REAL NOT NULL,
                    text            TEXT NOT NULL DEFAULT '',
                    is_own          INTEGER NOT NULL DEFAULT 0,
                    keyword_matched TEXT,
                    edited_ts       REAL,
                    deleted         INTEGER NOT NULL DEFAULT 0,
                    raw_json        TEXT,
                    UNIQUE(chat_id, msg_id)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
                CREATE INDEX IF NOT EXISTS idx_messages_sender_ts ON messages(sender_id, ts);
                CREATE INDEX IF NOT EXISTS idx_messages_keyword ON messages(keyword_matched)
                    WHERE keyword_matched IS NOT NULL;
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS chat_backfill (
                    chat_id  TEXT PRIMARY KEY,
                    complete INTEGER NOT NULL DEFAULT 0
                );
                """)
            self._conn.commit()

    # -- writes (the listener process) -------------------------------------------
    def add_message(
        self,
        *,
        chat_id: str,
        msg_id: int,
        sender_id: Optional[str],
        sender_name: Optional[str],
        ts: float,
        text: str,
        is_own: bool,
        keyword_matched: Optional[list[str]] = None,
        raw_json: Optional[str] = None,
    ) -> None:
        """Idempotent: `UNIQUE(chat_id, msg_id)` + INSERT OR REPLACE means a repeated
        backfill pass or an at-least-once redelivery after a reconnect never double-counts."""
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO messages
                   (chat_id, msg_id, sender_id, sender_name, ts, text, is_own,
                    keyword_matched, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    chat_id,
                    msg_id,
                    sender_id,
                    sender_name,
                    ts,
                    text,
                    1 if is_own else 0,
                    ",".join(keyword_matched) if keyword_matched else None,
                    raw_json,
                ),
            )
            self._conn.commit()

    def mark_edited(self, *, chat_id: str, msg_id: int, text: str, edited_ts: float) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE messages SET text=?, edited_ts=? WHERE chat_id=? AND msg_id=?",
                (text, edited_ts, chat_id, msg_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def mark_deleted(self, *, chat_id: str, msg_ids: list[int]) -> int:
        """Soft-delete: 'retained forever' means a deleted message keeps its row (its prior
        existence can matter for context) — only the flag is set, never a real DELETE."""
        if not msg_ids:
            return 0
        with self._lock:
            placeholders = ",".join("?" for _ in msg_ids)
            cur = self._conn.execute(
                f"UPDATE messages SET deleted=1 WHERE chat_id=? AND msg_id IN ({placeholders})",
                (chat_id, *msg_ids),
            )
            self._conn.commit()
            return cur.rowcount

    def set_heartbeat(self, ts: float) -> None:
        self._set_meta("heartbeat_ts", str(ts))

    def set_backfill_complete(self, chat_id: str, done: bool = True) -> None:
        """Per-chat, not global: the listener can mirror several chats at once, each
        completing its (potentially long) initial history pull independently."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO chat_backfill (chat_id, complete) VALUES (?, ?)",
                (chat_id, 1 if done else 0),
            )
            self._conn.commit()

    def is_backfill_complete(self, chat_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT complete FROM chat_backfill WHERE chat_id=?", (chat_id,)
            ).fetchone()
        return bool(row and row["complete"])

    def backfill_status(self) -> dict[str, bool]:
        """{chat_id: complete} for every chat the listener has ever tracked — lets
        `stats()`/the health-check tool report 'all configured chats done' without the
        store needing to know the configured set from outside."""
        with self._lock:
            rows = self._conn.execute("SELECT chat_id, complete FROM chat_backfill").fetchall()
        return {r["chat_id"]: bool(r["complete"]) for r in rows}

    def set_own_user_id(self, user_id: str) -> None:
        self._set_meta("own_user_id", user_id)

    def _set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
            )
            self._conn.commit()

    def _get_meta(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else None

    # -- reads (both the listener's own resume logic and the agent-facing tool) --
    def messages_since(
        self, ts: float, *, limit: int = 500, include_deleted: bool = False
    ) -> list[dict]:
        clause = "" if include_deleted else "AND deleted=0"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM messages WHERE ts>=? {clause} ORDER BY ts ASC LIMIT ?",
                (ts, limit),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def search(self, keyword: str, *, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE deleted=0 AND text LIKE ? "
                "ORDER BY ts DESC LIMIT ?",
                (f"%{keyword}%", limit),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def messages_by_sender(self, sender_id: str, *, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE deleted=0 AND sender_id=? "
                "ORDER BY ts DESC LIMIT ?",
                (sender_id, limit),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def own_messages(self, *, limit: int = 100) -> list[dict]:
        """Style samples: the user's own past messages, for drafting replies in their voice."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE deleted=0 AND is_own=1 "
                "ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def latest_msg_id(self, chat_id: str) -> Optional[int]:
        """Resume point for backfill (or gap-detection) after a restart."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(msg_id) AS m FROM messages WHERE chat_id=?", (chat_id,)
            ).fetchone()
        return row["m"] if row and row["m"] is not None else None

    def get_heartbeat(self) -> Optional[float]:
        v = self._get_meta("heartbeat_ts")
        return float(v) if v is not None else None

    def own_user_id(self) -> Optional[str]:
        return self._get_meta("own_user_id")

    def stats(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, MIN(ts) AS oldest, MAX(ts) AS newest "
                "FROM messages WHERE deleted=0"
            ).fetchone()
        backfill = self.backfill_status()
        return {
            "message_count": row["n"] or 0,
            "oldest_ts": row["oldest"],
            "newest_ts": row["newest"],
            # True only once EVERY chat the listener has ever tracked has finished its
            # initial history pull — false (not unknown) before any chat is configured.
            "backfill_complete": bool(backfill) and all(backfill.values()),
            "chats_tracked": len(backfill),
            "heartbeat_ts": self.get_heartbeat(),
        }

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["is_own"] = bool(d["is_own"])
        d["deleted"] = bool(d["deleted"])
        d["keyword_matched"] = d["keyword_matched"].split(",") if d["keyword_matched"] else []
        d.pop("raw_json", None)
        return d

    def close(self) -> None:
        with self._lock:
            self._conn.close()
