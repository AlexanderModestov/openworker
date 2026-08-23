# OpenWorker: agent in Docker + Telegram listener local (macOS)

Both processes need to see the same state directory (secrets.json, telegram_history.db,
automation.db), even though they never talk to each other directly.

## 0. Shared state directory

```bash
mkdir -p ~/openworker-data
```

Must be a bind-mounted host folder, not a Docker named volume — a named volume lives
inside Docker Desktop's internal VM and the local listener can't read it directly on macOS.

## 1. Build the agent image and configure keys

```bash
cd /path/to/openworker
docker build -t openworker .
cp docker/.env.example docker/.env
```

Edit `docker/.env` — fill in `MOONSHOT_API_KEY` (Kimi) and whichever other provider keys
you want. Leave `TELEGRAM_BOT_TOKEN`/`TELEGRAM_ALLOWED_USERS` set too if you want the
bot-based connector (separate identity from the listener, used for send_message/digests).

## 2. Run the agent, pointing /data at the shared folder

```bash
docker run --name openworker \
  --env-file docker/.env \
  -p 8765:8765 -p 1420:1420 \
  -v ~/openworker-data:/data \
  -v "$PWD:/workspace" \
  openworker
```

UI at http://localhost:1420.

## 3. Local Python env for the listener (host, no Docker)

```bash
cd /path/to/openworker
python3 -m venv .venv-telegram
.venv-telegram/bin/pip install --upgrade pip
.venv-telegram/bin/pip install -e ".[telegram_user]"
```

## 4. One-time interactive login — same state dir as the container

```bash
export COWORKER_STATE_DIR=~/openworker-data
.venv-telegram/bin/openworker-telegram-listener --login
```

Enter `api_id`/`api_hash` (from https://my.telegram.org), then Telethon prompts for
phone/code/2FA, then pick which chat(s) to mirror and optional alert keywords. Saves into
`~/openworker-data/secrets.json` — the same file the container reads.

## 5. Run it unattended

Quick option — a dedicated terminal/tmux pane:

```bash
export COWORKER_STATE_DIR=~/openworker-data
.venv-telegram/bin/openworker-telegram-listener
```

To survive logout/reboot, install as a launchd agent:

`~/Library/LaunchAgents/com.openworker.telegram-listener.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.openworker.telegram-listener</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/alexmodestov/ManyChat/Projects/openworker/.venv-telegram/bin/openworker-telegram-listener</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>COWORKER_STATE_DIR</key>
        <string>/Users/alexmodestov/openworker-data</string>
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
    <string>/Users/alexmodestov/Library/Logs/openworker-telegram-listener.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/alexmodestov/Library/Logs/openworker-telegram-listener.log</string>
</dict>
</plist>
```

Load / verify / stop:

```bash
launchctl load ~/Library/LaunchAgents/com.openworker.telegram-listener.plist
launchctl list | grep com.openworker.telegram-listener
tail -f ~/Library/Logs/openworker-telegram-listener.log

# to stop:
launchctl unload ~/Library/LaunchAgents/com.openworker.telegram-listener.plist
```

## 6. (Optional) Register digest/alert automations

```bash
export COWORKER_STATE_DIR=~/openworker-data
.venv-telegram/bin/python scripts/setup_telegram_automations.py --user-chat-id <YOUR_BOT_DM_CHAT_ID>
```

## 7. Verify

In the UI (localhost:1420), ask the agent to run `check_work_chat_health` — should show
`backfill_complete: true` and a recent heartbeat once the local listener has been running
a minute or two.

**Caveat:** the listener writes SQLite in WAL mode; the container reads it across a Docker
Desktop bind mount (virtiofs). Generally fine on modern Docker Desktop — if the agent ever
reports stale/missing messages despite a healthy heartbeat, suspect this first.
