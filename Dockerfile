# OpenWorker agent server + browser UI in one container.
# The desktop (Tauri) shell is a native window and isn't included here.

FROM python:3.11-slim

# Node 20 (required by surfaces/gui) on top of the Python base.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY coworker/ coworker/
COPY packaging/ packaging/
RUN pip install --no-cache-dir -e ".[messaging]"

COPY surfaces/gui/package.json surfaces/gui/package-lock.json surfaces/gui/
RUN npm install --prefix surfaces/gui

COPY surfaces/gui/ surfaces/gui/
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Server state (secrets, conversation history) and the agent's default workspace —
# mount host dirs onto these for persistence.
ENV COWORKER_STATE_DIR=/data
VOLUME ["/data", "/workspace"]

EXPOSE 8765 1420

ENTRYPOINT ["/entrypoint.sh"]
