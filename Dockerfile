# NJDROP — Video Downloader
# Single container: FastAPI backend (yt-dlp + FFmpeg) also serves the
# static frontend (index.html/style.css/script.js/logo.svg).

FROM python:3.12-slim

# FFmpeg for stream merging, Node.js for the PO Token provider sidecar
# (YouTube now requires a valid Proof-of-Origin token to serve real format
# URLs to server-side/datacenter requests — without this, yt-dlp gets a
# format list but every format is refused/"not available" regardless of
# player client or cookies).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl gnupg git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first for better layer caching.
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# yt-dlp ships frequent fixes for YouTube's extraction changes; always pull
# the latest at build time in its own layer (rather than relying on the
# requirements.txt pin alone) so a stale cached layer never silently keeps
# an old, broken version around across deploys. bgutil-ytdlp-pot-provider is
# the Python-side plugin that talks to the Node.js token server below.
RUN pip install --no-cache-dir -U yt-dlp bgutil-ytdlp-pot-provider && yt-dlp --version

# bgutil's PO Token generator: a small Node.js HTTP server the yt-dlp
# plugin calls over localhost:4416. No published npm CLI package for
# this — built from source per the project's own instructions.
RUN git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider /opt/bgutil-pot \
    && cd /opt/bgutil-pot/server \
    && npm ci \
    && npx tsc

# Copy the rest of the project (frontend files + backend source).
COPY . .

RUN mkdir -p backend/downloads

WORKDIR /app/backend

# Render (and most PaaS) inject $PORT at runtime; default to 8000 locally.
ENV PORT=8000
EXPOSE 8000

# Start the PO Token provider server in the background, wait briefly for
# it to come up, then start the app. yt-dlp's plugin talks to it over
# http://127.0.0.1:4416 by default. If the Node process dies, restart it
# in a loop rather than silently running without PO tokens.
CMD ["sh", "-c", "(while true; do node /opt/bgutil-pot/server/build/main.js; echo 'POT provider exited, restarting in 2s'; sleep 2; done) & sleep 2 && uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
