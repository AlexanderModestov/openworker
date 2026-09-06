#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_DIR="${WORKSPACE_DIR:-/workspace}"
mkdir -p "$WORKSPACE_DIR"

# Seed the Telegram bot token from the env var into the secret store as a ${VAR}
# reference (resolved at read time) — but only if no profile exists yet, so a
# persisted/GUI-edited /data/secrets.json is never overwritten on restart.
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
  python3 - <<'PY'
from coworker.secrets import SecretStore

store = SecretStore()
if store.get("telegram:default") is None:
    store.put("telegram:default", {"bot_token": "${TELEGRAM_BOT_TOKEN}", "enabled": True})
PY
fi

# Vite reads the server's per-launch token file once, at startup, so it must
# exist before `npm run dev` starts — and it must be THIS launch's token. The server
# only unlinks the file on a clean shutdown, so after `docker rm -f`/a crash a stale
# one survives in /data; the wait below would pass on it immediately and Vite would
# bake the old token in, leaving every UI request 401. Clear it before starting.
token_file="${COWORKER_STATE_DIR:-/data}/sidecar-8765.token"
rm -f "$token_file"

openworker-server --cwd "$WORKSPACE_DIR" --host 0.0.0.0 --port 8765 &

for _ in $(seq 1 50); do
  [ -f "$token_file" ] && break
  sleep 0.2
done

exec npm run dev --prefix surfaces/gui -- --host 0.0.0.0
