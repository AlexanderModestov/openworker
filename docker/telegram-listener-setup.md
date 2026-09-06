# OpenWorker on macOS: agent in Docker + Telegram work-chat listener on the host

End-to-end guide: from an empty Mac to a running agent that mirrors your work Telegram
chats, scans them every 15 minutes, and sends you digests and alerts. Follow it top to
bottom on a fresh machine; on an existing machine, jump to the section you need.

Contents

1. [How the pieces fit together](#1-how-the-pieces-fit-together)
2. [Prerequisites](#2-prerequisites)
3. [Get the code](#3-get-the-code)
4. [Shared state directory](#4-shared-state-directory)
5. [Configure the agent (`docker/.env`)](#5-configure-the-agent-dockerenv)
6. [Build and run the agent container](#6-build-and-run-the-agent-container)
7. [Python environment for the listener](#7-python-environment-for-the-listener)
8. [One-time Telegram login](#8-one-time-telegram-login)
9. [Run the listener as a background service (launchd)](#9-run-the-listener-as-a-background-service-launchd)
10. [Managing the service: status, logs, stop, start, uninstall](#10-managing-the-service-status-logs-stop-start-uninstall)
11. [Register the digest and alert automations](#11-register-the-digest-and-alert-automations)
12. [Verify everything works](#12-verify-everything-works)
13. [Updating to a newer version](#13-updating-to-a-newer-version)
14. [Installing on another machine](#14-installing-on-another-machine)
15. [Troubleshooting](#15-troubleshooting)

---

## 1. How the pieces fit together

Two processes, one shared folder, no direct connection between them:

```
 Telegram (your personal account, read-only)          Telegram (bot account, sends to you)
              │  MTProto / Telethon                                  ▲  Bot API
              ▼                                                      │
 ┌────────────────────────────┐   telegram_history.db   ┌──────────────────────────────┐
 │ openworker-telegram-       │ ──────────────────────▶ │ openworker agent server      │
 │ listener  (host, launchd)  │   secrets.json          │ + browser UI  (Docker)       │
 └────────────────────────────┘   automation.db         └──────────────────────────────┘
                                 ~/openworker-data  ==  /data inside the container
```

- **Listener** (`openworker-telegram-listener`): logs into Telegram *as you* with Telethon,
  mirrors the chats you pick into `telegram_history.db`, and writes a heartbeat every
  minute. It is strictly read-only: it never sends, edits or deletes anything.
- **Agent server** (Docker): reads the mirror through five read-only tools
  (`get_recent_work_chat_messages`, `search_work_chat`, `get_messages_by_sender`,
  `get_my_own_messages`, `check_work_chat_health`), runs the scheduled automations, and
  delivers digests/alerts to you through a **separate Telegram bot** identity. Nothing is
  ever posted into the mirrored work chat itself.
- **Shared state directory** (`~/openworker-data`): the only bridge. Both processes read
  `secrets.json`; the listener writes `telegram_history.db` (SQLite, WAL mode) and the
  server reads it; the automation script writes `automation.db` and the server reads it.

Why the listener runs on the host and not in the container: it needs an interactive
terminal for the one-time login (phone, code, 2FA), and keeping a personal-account
session in its own process isolates it from everything else. Running it in a second
container is possible but not covered here.

## 2. Prerequisites

| What | Why | How to get it |
|---|---|---|
| Docker Desktop | runs the agent server + UI | https://www.docker.com/products/docker-desktop/ |
| Homebrew | installs Python 3.12 | https://brew.sh |
| Python 3.12 | the listener needs Python ≥ 3.10; Apple's `/usr/bin/python3` is 3.9 and its pip cannot install this project | `brew install python@3.12` |
| git | clone the repo | comes with Xcode command line tools |
| Telegram API id + hash | lets Telethon log in as you | https://my.telegram.org → "API development tools" → create an app |
| A Telegram bot token | the identity that messages you | in Telegram open **@BotFather**, send `/newbot`, copy the token |
| Your Telegram user id | allow-lists you on the bot; also your bot DM chat id | in Telegram message **@userinfobot**; it replies with your numeric id |
| A model provider key | the agent needs an LLM | e.g. an Anthropic, OpenAI or Moonshot key |

Check the two tools are present:

```bash
docker --version
python3.12 --version
```

## 3. Get the code

```bash
git clone https://github.com/AlexanderModestov/openworker.git ~/ManyChat/Projects/openworker
cd ~/ManyChat/Projects/openworker
```

Any path works; the rest of this guide uses `REPO` for it and `STATE` for the state dir.
Export them once per terminal so the commands below can be pasted as-is:

```bash
export REPO=~/ManyChat/Projects/openworker
export STATE=~/openworker-data
export COWORKER_STATE_DIR="$STATE"
```

## 4. Shared state directory

```bash
mkdir -p "$STATE"
```

It must be a **bind-mounted host folder, not a Docker named volume**. A named volume
lives inside Docker Desktop's Linux VM and the host-side listener cannot open it.

## 5. Configure the agent (`docker/.env`)

```bash
cd "$REPO"
cp docker/.env.example docker/.env
```

Edit `docker/.env` and fill in at least:

```
ANTHROPIC_API_KEY=...            # or whichever provider(s) you use
TELEGRAM_BOT_TOKEN=123456:ABC... # from @BotFather
TELEGRAM_ALLOWED_USERS=112671174 # your numeric user id from @userinfobot
```

`docker/.env` is git-ignored. Never commit it.

## 6. Build and run the agent container

Start Docker Desktop first (`open -a Docker`) and wait until the whale icon is steady.

```bash
cd "$REPO"
docker build -t openworker .

docker run -d --name openworker --restart unless-stopped \
  --env-file docker/.env \
  -p 8765:8765 -p 1420:1420 \
  -v "$STATE:/data" \
  -v "$REPO:/workspace" \
  openworker
```

UI: http://localhost:1420 . API: port 8765.

On first start the entrypoint copies `TELEGRAM_BOT_TOKEN` into `secrets.json` as the bot
profile. It only does this when no bot profile exists yet, so a token you later edit in the
GUI is never overwritten by a restart.

Container commands:

```bash
docker logs -f openworker      # follow output
docker stop openworker         # stop
docker start openworker        # start again, state preserved
docker rm -f openworker        # remove (state in $STATE survives)
```

## 7. Python environment for the listener

Use **`python3.12` explicitly**. Plain `python3` on macOS is Apple's 3.9 and fails with
`Directory cannot be installed in editable mode` (see Troubleshooting).

```bash
cd "$REPO"
python3.12 -m venv .venv-telegram
.venv-telegram/bin/pip install --upgrade pip
.venv-telegram/bin/pip install -e ".[telegram_user]"
```

`.venv-telegram/` is git-ignored.

## 8. One-time Telegram login

Run this **manually in a terminal**. It prompts on stdin and cannot run under launchd.

```bash
cd "$REPO"
export COWORKER_STATE_DIR="$STATE"
.venv-telegram/bin/openworker-telegram-listener --login
```

You will be asked for, in order:

1. `api_id` and `api_hash` from my.telegram.org.
2. Your phone number, the login code Telegram sends you, and your 2FA password if set.
3. Which chat(s) to mirror: a numbered list of your 50 most recent dialogs is printed; enter
   one index or several comma-separated (`2` or `2,5,7`).
4. Optional alert keywords (comma-separated, or blank). These are only a cheap hint; the
   alert automation judges importance itself.

Everything is saved into `$STATE/secrets.json` under the profile `telegram_user:default`.
Re-run `--login` any time to change the mirrored chats or keywords; the session is reused
so you are not asked for phone/code again unless Telegram revoked it.

## 9. Run the listener as a background service (launchd)

launchd starts the listener at login, keeps it alive, restarts it 10 seconds after a crash,
and writes its output to a log file. Every start resumes from the last mirrored message, so
restarts cost seconds and lose nothing.

Generate the plist with your paths substituted, then load it:

```bash
mkdir -p ~/Library/LaunchAgents ~/Library/Logs

cat > ~/Library/LaunchAgents/com.openworker.telegram-listener.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.openworker.telegram-listener</string>
    <key>ProgramArguments</key>
    <array>
        <string>$REPO/.venv-telegram/bin/openworker-telegram-listener</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>COWORKER_STATE_DIR</key>
        <string>$STATE</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$HOME/Library/Logs/openworker-telegram-listener.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/Library/Logs/openworker-telegram-listener.log</string>
</dict>
</plist>
EOF

plutil -lint ~/Library/LaunchAgents/com.openworker.telegram-listener.plist
launchctl load ~/Library/LaunchAgents/com.openworker.telegram-listener.plist
```

`$REPO`, `$STATE` and `$HOME` must be **absolute** paths (no `~`); the exports in section 3
already expand them. `PYTHONUNBUFFERED=1` makes log lines appear immediately.

Make sure no other copy of the listener is running in a terminal: two processes sharing one
Telegram session will fight over the connection (`pkill -f openworker-telegram-listener`
before loading if unsure).

**The first start downloads the full history of every mirrored chat.** Telegram serves
about 5,000 messages per minute, so a chat with 80,000 messages takes ~16 minutes. Progress
is visible in the log (`backfill complete for chat ...` per chat). Only after the last
chat finishes does the log print `listening on chats [...]` and the heartbeat begins.

## 10. Managing the service: status, logs, stop, start, uninstall

```bash
# Is it loaded / running?  (a PID in the first column means running)
launchctl list | grep com.openworker.telegram-listener
pgrep -fl openworker-telegram-listener

# Follow the log
tail -f ~/Library/Logs/openworker-telegram-listener.log

# STOP the service (also prevents it from starting at next login)
launchctl unload ~/Library/LaunchAgents/com.openworker.telegram-listener.plist

# START it again
launchctl load ~/Library/LaunchAgents/com.openworker.telegram-listener.plist

# RESTART (e.g. after pulling new listener code)
launchctl unload ~/Library/LaunchAgents/com.openworker.telegram-listener.plist
launchctl load   ~/Library/LaunchAgents/com.openworker.telegram-listener.plist

# UNINSTALL the service completely (mirror data and login stay in $STATE)
launchctl unload ~/Library/LaunchAgents/com.openworker.telegram-listener.plist
rm ~/Library/LaunchAgents/com.openworker.telegram-listener.plist
```

Run the listener in the foreground instead (useful for debugging; stop the launchd service
first so only one copy runs):

```bash
cd "$REPO" && COWORKER_STATE_DIR="$STATE" .venv-telegram/bin/openworker-telegram-listener
```

## 11. Register the digest and alert automations

The agent's scheduler runs tasks stored in `$STATE/automation.db`. This script creates two;
`--user-chat-id` is your numeric Telegram user id (for a private chat with the bot, the chat
id equals your user id).

```bash
cd "$REPO"
export COWORKER_STATE_DIR="$STATE"
.venv-telegram/bin/python scripts/setup_telegram_automations.py --user-chat-id 112671174
```

| Task | Schedule | What it does |
|---|---|---|
| Daily work-chat digest | `0 9 * * *` | health check, then a summary of the last 24 h sent to you via the bot |
| Work-chat alert scan | `*/15 * * * *` | reads the last 20 min; if something needs you, sends one alert with a suggested reply in your own voice |

The script is idempotent (looks tasks up by title), so re-running never duplicates them.
The server polls the database every 30 s; **no restart is needed**. Cron times use the
container's clock, which is UTC by default; add `-e TZ=Europe/Amsterdam` (or your zone) to
the `docker run` command in section 6 for local time. Later edits to schedule or
instructions go through the GUI's Scheduled view, not the script.

Finally, **send your bot one message in Telegram** (anything). Bots can only write to users
who have messaged them first.

## 12. Verify everything works

1. Listener: `tail ~/Library/Logs/openworker-telegram-listener.log` ends with
   `listening on chats [...]`.
2. Heartbeat is fresh:
   ```bash
   sqlite3 "$STATE/telegram_history.db" \
     "select datetime(value,'unixepoch','localtime') from meta where key='heartbeat_ts'"
   ```
3. In the UI (http://localhost:1420) ask the agent to run `check_work_chat_health`. Expect
   `backfill_complete: true` and `heartbeat_age_seconds` under 120.
4. In the UI's Scheduled view both automations are listed. Run "Work-chat alert scan" by
   hand once; if the chat was quiet it sends nothing (by design), otherwise the bot DMs you.
5. Ask the agent "what happened in the work chat today?" and it should answer from the
   mirror.

## 13. Updating to a newer version

```bash
cd "$REPO"
git pull                                   # or: git fetch upstream && git merge upstream/main

# Agent: rebuild the image and recreate the container (state in $STATE is untouched)
docker build -t openworker .
docker rm -f openworker
docker run -d --name openworker --restart unless-stopped --env-file docker/.env \
  -p 8765:8765 -p 1420:1420 -v "$STATE:/data" -v "$REPO:/workspace" openworker

# Listener: the venv is an editable install, so new code is picked up on restart
.venv-telegram/bin/pip install -e ".[telegram_user]"   # only if dependencies changed
launchctl unload ~/Library/LaunchAgents/com.openworker.telegram-listener.plist
launchctl load   ~/Library/LaunchAgents/com.openworker.telegram-listener.plist
```

## 14. Installing on another machine

Short version: do sections 2 to 12 in order on the new Mac. The details that differ per
machine, and what to carry over:

**Carry over (copy from the old machine):**

- `docker/.env`: it is git-ignored, so it does not come with the clone. Copy it, or refill
  the keys from section 5.
- Optionally `$STATE/secrets.json` if you want the *same* provider keys and bot token
  without retyping. It also contains the Telethon session string; see the note below.

**Do fresh on the new machine:**

- Sections 2, 3, 4, 6, 7: install Docker Desktop and Python 3.12, clone, create the state
  dir, build the image, create the venv.
- Section 8, the Telegram login. Recommended even though the session string could be
  copied: Telegram allows one auth key to be used from one client at a time, and a
  copied session still running on the old machine would fight with the new one. Log in
  fresh on the new Mac, then on the old Mac stop the service (section 10) and, in Telegram
  → Settings → Devices, terminate the old session.
- Section 9: generate the plist **on the new machine** so `$REPO`, `$STATE` and `$HOME`
  are that machine's absolute paths. Never copy the plist file itself between machines.
- Section 11 if you want the automations there too. Only one machine should run the
  listener and the automations at a time, otherwise you get duplicate alerts.

**Decommission the old machine:**

```bash
launchctl unload ~/Library/LaunchAgents/com.openworker.telegram-listener.plist
rm ~/Library/LaunchAgents/com.openworker.telegram-listener.plist
docker rm -f openworker
# keep or delete ~/openworker-data as you prefer; it holds the mirror and secrets
```

**Full-history backfill happens again** on the new machine unless you copy
`$STATE/telegram_history.db` (plus its `-wal` and `-shm` files, with the listener stopped
on both sides). For most chats it is simpler to let it re-download.

## 15. Troubleshooting

**`ERROR: File "setup.py" or "setup.cfg" not found. Directory cannot be installed in editable mode`**
The venv was created with Apple's Python 3.9, whose pip is too old and whose version is
below the project's minimum. Recreate it with `python3.12` (section 7):
`rm -rf .venv-telegram && python3.12 -m venv .venv-telegram`.

**`ValueError: Could not find the input entity for PeerUser(user_id=...)`**
An older listener saved bare chat ids and could not resolve them on a fresh start. Fixed in
the current code: the listener now resolves configured chats through your dialog list and
accepts both old bare ids and new marked ids. Pull the latest code and restart the service.
If it still appears, re-run `--login` and re-pick the chats.

**`configured chat(s) [...] not found among this account's dialogs`**
You left or were removed from a mirrored chat, or picked a chat outside your 50 most
recent dialogs. Re-run `--login` and re-pick.

**`Telegram session is no longer authorized (revoked/expired)`**
You terminated the session in Telegram's Devices list, or logged in elsewhere with the same
session string. Re-run `--login` (section 8), then restart the service.

**Log file stays empty after loading the service**
Normal for the first minutes: the listener is backfilling and prints one line per finished
chat. If the plist lacks `PYTHONUNBUFFERED=1`, output is buffered and appears late; use the
plist from section 9.

**`check_work_chat_health` reports no heartbeat although the listener is running**
The heartbeat only starts after every chat has finished its first backfill. Watch the log
for `listening on chats`. After that, a heartbeat older than ~2 minutes means the listener
is down; check `launchctl list` and the log.

**Agent reports stale or missing messages despite a fresh heartbeat**
The listener writes SQLite in WAL mode; the container reads it through Docker Desktop's
bind mount (virtiofs). Usually fine, but suspect this first. `docker restart openworker`
reopens the database.

**UI at localhost:1420 loads but stays on the connecting screen; `docker logs openworker` is full of `401 Unauthorized` and `WebSocket /ws/events 403`**
The web UI is using a token the server does not know. The server writes a fresh
per-launch token to `$STATE/sidecar-8765.token` and Vite reads that file once at startup.
Older entrypoints only waited for the file to *exist*, so a stale token left behind by
`docker rm -f` (the server only deletes it on a clean shutdown) let Vite start with
yesterday's token. Fixed in the current `docker/entrypoint.sh` (it clears the stale file
before starting the server). If you see this, rebuild the image (section 13), then
`docker restart openworker` and reload the browser tab.

**`failed to connect to the docker API ... docker.sock`**
Docker Desktop is not running. `open -a Docker`, wait, retry.

**Two listeners running at once**
Symptoms: connection resets in the log, duplicate rows are harmless but wasteful.
`pgrep -fl openworker-telegram-listener` should show exactly one PID. Kill stray
foreground copies with `pkill -f openworker-telegram-listener`; launchd restarts its own.

**Automations never fire**
Check in order: the container is up (`docker ps`), a provider key is set in `docker/.env`,
the tasks exist in the GUI's Scheduled view, you have messaged the bot at least once, and
`TELEGRAM_ALLOWED_USERS` contains your id. Run a task by hand from the GUI and read its
run log there.

**Digest arrives at the wrong hour**
The container clock is UTC. Recreate it with `-e TZ=<your zone>` or edit the cron in the GUI.
