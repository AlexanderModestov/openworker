# Running OpenWorker in Docker

The container runs the headless agent server (`openworker-server`, port 8765)
and the browser UI (Vite dev server, port 1420). The desktop shell (Tauri) is
a native window and isn't included here.

## 1. Build the image

From the repo root:

```bash
docker build -t openworker .
```

## 2. Configure

```bash
cp docker/.env.example docker/.env
```

Edit `docker/.env`:

- **Model provider key** — set the one for whichever provider you use
  (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, ...; see
  `coworker/providers/registry.py` for the full list).
- **`TELEGRAM_BOT_TOKEN`** — from [@BotFather](https://t.me/BotFather), if you
  want the bot to listen on Telegram.
- **`TELEGRAM_ALLOWED_USERS`** — comma-separated Telegram user IDs allowed to
  message the bot. Leave empty and nobody can talk to it.

## 3. Run

```bash
docker run --name openworker \
  --env-file docker/.env \
  -p 8765:8765 -p 1420:1420 \
  -v openworker-data:/data \
  -v "$PWD:/workspace" \
  openworker
```

Open **http://localhost:1420** for the UI.

- `/data` (`openworker-data` volume) holds the agent's local state: the
  secret store, conversation history, automations. Keep this volume around
  across restarts — losing it means re-entering connector tokens.
- `/workspace` is the directory the agent works in (`--cwd`). Mount whatever
  project/folder you want it operating on; `$PWD` above just means "the
  current directory."

## Stopping / restarting

```bash
docker stop openworker
docker start openworker      # state in the volumes is preserved
docker rm -f openworker       # remove the container (volumes survive)
```

## Notes

- `TELEGRAM_BOT_TOKEN` is only used to seed `/data/secrets.json` on first
  boot. If a `telegram:default` profile already exists there (e.g. you set
  it up through the GUI, or the volume already has one from a previous run),
  the env var is ignored and the stored value wins — it's never overwritten.
- To change the bot token later, either edit it in the GUI or delete the
  `telegram:default` entry from `/data/secrets.json` and restart with the new
  `TELEGRAM_BOT_TOKEN` set.
- Logs from both processes (server + Vite) go to `docker logs openworker`.
