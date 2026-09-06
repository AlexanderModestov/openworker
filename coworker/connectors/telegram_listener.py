"""Standalone, read-only Telegram work-chat listener (Telethon / MTProto user session).

# INVARIANT (do not remove): this process is READ-ONLY over the Telethon user session. It
# must never call client.send_message / client.edit_message / client.delete_messages, or
# anything else that mutates the chat it reads. Outbound replies/alerts/drafts go
# exclusively through the EXISTING Bot-API `send_message` tool (coworker/connectors/tools.py,
# coworker/connectors/senders.py) — a different Telegram identity (a bot DMing the user),
# entirely separate from this personal-account mirror. If you find yourself importing
# anything from `tools.py`/`senders.py` here, or calling a Telethon send/edit/delete method,
# stop — it means this invariant is about to be violated. This file's import list is itself
# evidence of compliance: it imports nothing send-capable, from Telethon or from coworker.

Deliberately standalone — NOT wired into `coworker/connectors/gateway.py`'s `Gateway`/
`BasePlatformAdapter` framework, because a personal-account MTProto session is a
fundamentally different risk profile than the existing Bot-API connectors (full access to
a live human account, not a bot token). It talks to the rest of OpenWorker through exactly
two shared, file-backed surfaces: the existing `SecretStore` (credentials) and a new
`TelegramHistoryStore` SQLite file (`telegram_history.db`, WAL mode — safe to read
concurrently from `openworker-server`). No live connection, no IPC.

Two run modes:
  openworker-telegram-listener --login   Interactive, one-time setup (phone/code/2FA on
                                          stdin, pick the target chat from your dialogs).
                                          Must be run manually in a terminal — never from
                                          the systemd unit (no TTY for a 2FA prompt there).
  openworker-telegram-listener           The unattended run: reconnects with the saved
                                          session, backfills history once, then listens
                                          live. This is what the systemd ExecStart runs.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from typing import Any, Optional

from ..secrets import SecretStore

PROFILE = "telegram_user:default"
HEARTBEAT_INTERVAL_SECONDS = 60


def _db_path():
    from ..secrets import state_dir

    return state_dir() / "telegram_history.db"


def _match_keywords(text: str, keywords: list[str]) -> Optional[list[str]]:
    low = (text or "").lower()
    hits = [k for k in keywords if k and k.lower() in low]
    return hits or None


def _resolve_chats(dialogs, chat_ids: list[str], *, peer_id) -> list[tuple[str, Any]]:
    """Map the configured chat ids onto the account's dialogs.

    A StringSession persists only the auth key, not Telethon's entity cache, so on every
    unattended start a bare `int(chat_id)` can't be turned into an input entity (no
    access_hash) — hence resolving through `get_dialogs()`, which both warms the cache and
    gives us the entity objects. Accepts the marked id (`utils.get_peer_id`, e.g. -100…
    for channels) that `--login` now saves, and also the legacy bare `entity.id` an older
    `--login` stored. Returns [(canonical_marked_id, entity)]; the marked id is what
    `event.chat_id` reports, so backfill and live events write under the same chat_id.
    `peer_id` is injected (telethon.utils.get_peer_id) so this stays importable/testable
    without Telethon."""
    by_id: dict[str, tuple[str, Any]] = {}
    for d in dialogs:
        marked = str(peer_id(d.entity))
        by_id[marked] = (marked, d.entity)
        by_id.setdefault(str(d.entity.id), (marked, d.entity))
    resolved, missing = [], []
    for c in chat_ids:
        hit = by_id.get(str(c))
        if hit is None:
            missing.append(str(c))
        else:
            resolved.append(hit)
    if missing:
        raise SystemExit(
            f"configured chat(s) {missing} not found among this account's dialogs — "
            "run `openworker-telegram-listener --login` again and re-pick the chat(s)."
        )
    return resolved


def _prompt(prompt: str, *, validate=None, error: str = "invalid input, try again") -> str:
    """input() that re-asks on a blank/invalid answer instead of crashing with a raw
    traceback — this is a hand-typed (or occasionally mis-pasted) terminal flow, so a bad
    line should just re-prompt, not blow up `--login` with an int()/index traceback."""
    while True:
        try:
            value = input(prompt).strip()
        except EOFError:
            raise SystemExit(
                "\nno input available (stdin closed) — run --login in an interactive "
                "terminal, not piped/non-interactively."
            )
        if not value:
            print("(empty — please enter a value)")
            continue
        if validate is not None and not validate(value):
            print(error)
            continue
        return value


# -- login (interactive, one-time) -------------------------------------------------------
def _cmd_login(secrets: SecretStore) -> int:
    from telethon import TelegramClient, utils
    from telethon.sessions import StringSession

    profile = secrets.get(PROFILE) or {}
    api_id = profile.get("api_id") or _prompt(
        "Telegram api_id (from https://my.telegram.org): ",
        validate=str.isdigit,
        error="api_id must be numeric — check https://my.telegram.org and try again",
    )
    api_hash = profile.get("api_hash") or _prompt("Telegram api_hash: ")

    def _parse_indices(v: str) -> bool:
        try:
            return all(0 <= int(p.strip()) < 10_000 for p in v.split(","))
        except ValueError:
            return False

    async def _run() -> dict[str, Any]:
        client = TelegramClient(StringSession(), int(api_id), api_hash)
        await client.start()  # prompts phone/code/2FA password on stdin as needed
        me = await client.get_me()
        print(f"\nLogged in as {me.first_name} (id={me.id}).")

        print("\nYour dialogs:")
        dialogs = await client.get_dialogs(limit=50)
        for i, d in enumerate(dialogs):
            print(f"  [{i}] {d.name!r} (id={d.entity.id})")
        choice = _prompt(
            "\nPick the work chat(s) to mirror — one index, or several comma-separated "
            "(e.g. '2' or '2,5,7'): ",
            validate=lambda v: _parse_indices(v) and all(int(p) < len(dialogs) for p in v.split(",")),
            error=f"enter one or more indices between 0 and {len(dialogs) - 1}, comma-separated",
        )
        picked = [dialogs[int(p.strip())] for p in choice.split(",")]
        # Marked id (-100… for channels/supergroups), not the bare entity.id: it encodes the
        # peer type, and it's what event.chat_id reports at run time.
        chat_ids = [str(utils.get_peer_id(d.entity)) for d in picked]
        print("Selected: " + ", ".join(f"{d.name!r}" for d in picked))

        kw_raw = input(
            "Keywords/mentions to flag as alerts (shared across all selected chats), "
            "comma-separated, or blank for none (e.g. 'urgent, @you'): "
        ).strip()
        keywords = [k.strip() for k in kw_raw.split(",") if k.strip()]

        session_string = client.session.save()
        await client.disconnect()
        return {
            "api_id": str(api_id),
            "api_hash": api_hash,
            "session_string": session_string,
            "chat_ids": chat_ids,
            "keywords": keywords,
        }

    data = asyncio.run(_run())
    secrets.put(PROFILE, data)
    print(f"\nSaved to SecretStore profile '{PROFILE}'. Chats: {data['chat_ids']}")
    print(
        "You can now enable the systemd unit — see packaging/systemd/README.md. "
        "Re-run --login any time to change the monitored chats or keywords."
    )
    return 0


# -- unattended run ------------------------------------------------------------------------
def _require_config(profile: dict[str, Any]) -> None:
    missing = [
        k for k in ("api_id", "api_hash", "session_string") if not profile.get(k)
    ]
    if not profile.get("chat_ids"):
        missing.append("chat_ids")
    if missing:
        raise SystemExit(
            f"telegram_user:default is missing {missing} — run "
            "`openworker-telegram-listener --login` first (interactively, in a terminal)."
        )


async def _get_sender_name(message) -> Optional[str]:
    try:
        sender = await message.get_sender()
    except Exception:
        return None
    if sender is None:
        return None
    name = " ".join(p for p in (getattr(sender, "first_name", None), getattr(sender, "last_name", None)) if p)
    return name or getattr(sender, "username", None) or getattr(sender, "title", None)


async def _ingest_message(store, message, *, chat_id: str, keywords: list[str], own_id: int) -> None:
    text = message.message or ""
    sender_name = await _get_sender_name(message)
    store.add_message(
        chat_id=chat_id,
        msg_id=message.id,
        sender_id=str(message.sender_id) if message.sender_id is not None else None,
        sender_name=sender_name,
        ts=message.date.timestamp(),
        text=text,
        is_own=bool(message.out) or message.sender_id == own_id,
        keyword_matched=_match_keywords(text, keywords),
    )


async def _backfill(
    client, store, *, chat_id: str, entity: Any, keywords: list[str], own_id: int
) -> None:
    resume_from = store.latest_msg_id(chat_id)
    kwargs: dict[str, Any] = {"reverse": True}
    if resume_from:
        kwargs["min_id"] = resume_from
    count = 0
    async for message in client.iter_messages(entity, **kwargs):
        await _ingest_message(store, message, chat_id=chat_id, keywords=keywords, own_id=own_id)
        count += 1
    store.set_backfill_complete(chat_id, True)
    print(f"backfill complete for chat {chat_id}: {count} messages ingested (resume_from={resume_from}).")


async def _heartbeat_loop(store) -> None:
    while True:
        store.set_heartbeat(time.time())
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


async def _run(secrets: SecretStore) -> int:
    from telethon import TelegramClient, events, utils
    from telethon.sessions import StringSession

    from .telegram_history_store import TelegramHistoryStore

    profile = secrets.get(PROFILE) or {}
    _require_config(profile)
    keywords = list(profile.get("keywords") or [])

    client = TelegramClient(
        StringSession(profile["session_string"]), int(profile["api_id"]), profile["api_hash"]
    )
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit(
            "Telegram session is no longer authorized (revoked/expired) — this can't be "
            "fixed unattended. Run `openworker-telegram-listener --login` again in a terminal."
        )

    store = TelegramHistoryStore(_db_path())
    me = await client.get_me()
    store.set_own_user_id(str(me.id))
    own_id = me.id

    # get_dialogs() warms Telethon's entity cache (see _resolve_chats) — without it a fresh
    # StringSession can't resolve a chat id to an input entity.
    chats = _resolve_chats(
        await client.get_dialogs(), [str(c) for c in profile["chat_ids"]], peer_id=utils.get_peer_id
    )
    chat_ids = [chat_id for chat_id, _ in chats]

    for chat_id, entity in chats:
        if not store.is_backfill_complete(chat_id):
            await _backfill(
                client, store, chat_id=chat_id, entity=entity, keywords=keywords, own_id=own_id
            )

    chat_entities = [entity for _, entity in chats]

    @client.on(events.NewMessage(chats=chat_entities))
    async def _on_new(event) -> None:
        await _ingest_message(
            store, event.message, chat_id=str(event.chat_id), keywords=keywords, own_id=own_id
        )

    @client.on(events.MessageEdited(chats=chat_entities))
    async def _on_edit(event) -> None:
        store.mark_edited(
            chat_id=str(event.chat_id),
            msg_id=event.message.id,
            text=event.message.message or "",
            edited_ts=event.message.date.timestamp(),
        )

    @client.on(events.MessageDeleted(chats=chat_entities))
    async def _on_delete(event) -> None:
        # Telethon can't always resolve which chat a deletion happened in (best-effort for
        # some contexts) — skip rather than guess/write under the wrong chat_id.
        if event.deleted_ids and event.chat_id is not None:
            store.mark_deleted(chat_id=str(event.chat_id), msg_ids=list(event.deleted_ids))

    print(f"listening on chats {chat_ids} (own_id={own_id}, keywords={keywords}) …")
    try:
        await asyncio.gather(client.run_until_disconnected(), _heartbeat_loop(store))
    finally:
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openworker-telegram-listener")
    parser.add_argument(
        "--login",
        action="store_true",
        help="Interactive one-time setup (run manually in a terminal, never via systemd).",
    )
    args = parser.parse_args(argv)
    secrets = SecretStore()
    if args.login:
        return _cmd_login(secrets)
    return asyncio.run(_run(secrets))


if __name__ == "__main__":
    sys.exit(main())
