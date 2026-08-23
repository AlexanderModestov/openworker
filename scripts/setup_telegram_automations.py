#!/usr/bin/env python3
"""One-time setup: creates the daily work-chat digest + alert-scan ScheduledTasks in the
existing automation.db (the same file `openworker-server` uses — no server needs to be
running for this to work, TaskStore is a standalone SQLite wrapper).

Usage:
    python scripts/setup_telegram_automations.py --user-chat-id 123456789

`--user-chat-id` is the numeric Telegram chat id of your DM with the EXISTING OpenWorker
Telegram bot (a different identity from the Telethon work-chat mirror) — where digests,
alerts, and draft replies get sent. Find it by messaging the bot once; the id shows up in
the connector's people directory / connect flow.

Idempotent: re-running looks up each task by title first, so it never creates duplicates.
Future edits to the instructions text are expected to go through the GUI's automation
editor, not this script.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from coworker.automation.models import Schedule, ScheduledTask
from coworker.automation.store import TaskStore
from coworker.secrets import state_dir

DIGEST_INSTRUCTIONS = """\
You have access to read-only tools for the user's private work Telegram chat \
(get_recent_work_chat_messages, search_work_chat, get_messages_by_sender, \
get_my_own_messages, check_work_chat_health) and the existing send_message tool.

1. Call check_work_chat_health first. If backfill_complete is false, or the heartbeat is \
more than about 30 minutes old, send a short warning via send_message to \
target "telegram:{chat_id}" saying the work-chat listener looks unhealthy, and stop.
2. Call get_recent_work_chat_messages with since_minutes=1440 (the last 24 hours).
3. Write a concise digest of what happened in the work chat in the last 24 hours: group by \
topic/thread where sensible, call out anything that looks like it needs the user's \
attention (questions addressed to them, decisions pending, deadlines mentioned), and skip \
routine chatter. If there is genuinely nothing notable, say so briefly rather than padding.
4. Send the digest via send_message to target "telegram:{chat_id}".
"""

ALERT_INSTRUCTIONS = """\
You have access to read-only tools for the user's private work Telegram chat \
(get_recent_work_chat_messages, search_work_chat, get_messages_by_sender, \
get_my_own_messages, check_work_chat_health) and the existing send_message tool.

1. Call get_recent_work_chat_messages with since_minutes=20 (a bit over the 15-minute run \
interval, so nothing is missed if a run is briefly delayed).
2. If there are no new messages, do nothing further — do not call send_message just to say \
nothing happened.
3. For each new message, judge for yourself (using your own reading, not just the \
keyword_matched field on a message — that field is only a cheap hint) whether it is \
genuinely important enough to interrupt the user right now: a direct question to them, an \
urgent request, a deadline, an escalation, a decision only they can make. Routine \
conversation is NOT an alert.
4. For each message you judge important:
   a. Call get_my_own_messages to see samples of how the user writes (tone, punctuation, \
typical length, greetings/sign-offs), so any draft you propose sounds like them, not like a \
generic assistant.
   b. Compose a short alert: quote or summarize the message, say why it seems important, and \
include a suggested reply drafted in the user's own voice under a clearly-labeled \
"Suggested reply:" section. Make clear it is a suggestion, not sent — there is no tool to \
send into the work chat itself; the user copies it in themselves, or asks you to redraft it.
   c. Send the alert via send_message to target "telegram:{chat_id}".
5. Send at most one message per run even if multiple items qualify — combine them into one \
alert rather than several separate sends.
"""


def _ensure(store: TaskStore, *, title: str, cron: str, instructions: str, workspace: Path) -> None:
    existing = next((t for t in store.list() if t.title == title), None)
    if existing:
        print(f"'{title}' already exists (id={existing.id}) — skipping")
        return
    workspace.mkdir(parents=True, exist_ok=True)
    task = ScheduledTask(
        title=title,
        instructions=instructions,
        schedule=Schedule(kind="cron", cron=cron),
        workspace=str(workspace),
        agent="cowork",
        notify_on_completion=False,
    )
    store.save(task)
    print(f"created '{title}' (id={task.id}, cron='{cron}')")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="setup_telegram_automations")
    parser.add_argument(
        "--user-chat-id",
        required=True,
        help="Numeric Telegram chat id of your DM with the existing OpenWorker bot.",
    )
    args = parser.parse_args(argv)

    store = TaskStore(state_dir() / "automation.db")
    workspace = state_dir() / "automations" / "telegram"
    _ensure(
        store,
        title="Daily work-chat digest",
        cron="0 9 * * *",
        instructions=DIGEST_INSTRUCTIONS.format(chat_id=args.user_chat_id),
        workspace=workspace,
    )
    _ensure(
        store,
        title="Work-chat alert scan",
        cron="*/15 * * * *",
        instructions=ALERT_INSTRUCTIONS.format(chat_id=args.user_chat_id),
        workspace=workspace,
    )
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
