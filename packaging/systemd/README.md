# Headless systemd services (Linux)

Two independent `systemd --user` units: the OpenWorker server itself, and the standalone
Telethon work-chat listener (`coworker/connectors/telegram_listener.py`). They share no
live connection — only a SQLite file (`telegram_history.db`, WAL mode) and the
`SecretStore` (`~/.config/coworker/secrets.json`) — so there is deliberately no
`After=`/`Requires=` between them: either can restart or crash-loop without disturbing the
other.

## 1. One-time setup (before enabling anything)

```bash
cd ~/Documents/openworker
.venv/bin/pip install -e ".[telegram_user]"   # installs telethon
```

### Telegram work-chat listener login (interactive — run in a real terminal, not via systemd)

```bash
.venv/bin/openworker-telegram-listener --login
```

This asks for your `api_id`/`api_hash` (get them once from <https://my.telegram.org>), then
Telethon itself prompts for your phone number, login code, and (if enabled) your 2FA
password. You'll then pick which chat(s) to mirror from a numbered list of your dialogs —
one index, or several comma-separated (e.g. `2,5,7`) if you want to monitor more than one
work chat — and optionally set keyword/mention triggers shared across all of them for
alerts. Everything gets saved into the existing `SecretStore` (`telegram_user:default`
profile) — nothing is printed or logged elsewhere.

**Do not skip this step or enable the systemd unit before it succeeds** — the unit has no
TTY to answer a phone/2FA prompt, and will just crash-loop with a clear "run --login first"
error in its logs.

### Register the digest + alert-scan automations

Find the numeric chat id of your DM with the *existing* OpenWorker Telegram bot (a
different identity — the one you already message OpenWorker through), then:

```bash
.venv/bin/python scripts/setup_telegram_automations.py --user-chat-id <YOUR_CHAT_ID>
```

Safe to re-run — it skips tasks that already exist by title. Future wording tweaks to the
digest/alert instructions are best done through the GUI's automation editor once one run has
been created.

## 2. Install the units

```bash
mkdir -p ~/.config/systemd/user
cp packaging/systemd/openworker-server.service ~/.config/systemd/user/
cp packaging/systemd/openworker-telegram-listener.service ~/.config/systemd/user/
systemctl --user daemon-reload
```

Both unit files assume this repo lives at `~/Documents/openworker` (i.e. `.venv` at
`~/Documents/openworker/.venv`) — edit the `ExecStart=` path first if yours differs.

## 3. Enable + start

```bash
systemctl --user enable --now openworker-server.service
systemctl --user enable --now openworker-telegram-listener.service
# so both units survive a reboot without you being logged in:
loginctl enable-linger "$USER"
```

## 4. Verify

```bash
systemctl --user status openworker-server.service openworker-telegram-listener.service
journalctl --user -u openworker-telegram-listener -f
```

The listener's first live run does a full history backfill of the target chat before it
starts tailing new messages — expect a burst of log lines, then quiet until new messages
arrive. `check_work_chat_health` (a tool available to the agent) reports backfill status and
how long ago the listener last confirmed it's alive.
