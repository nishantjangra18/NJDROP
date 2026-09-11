# NJDROP — Video Downloader
# Single container: FastAPI backend (yt-dlp + FFmpeg) also serves the
# static frontend (index.html/style.css/script.js/logo.svg).

FROM python:3.12-slim

# FFmpeg is required by yt-dlp to merge separate audio/video streams.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first for better layer caching.
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# yt-dlp ships frequent fixes for YouTube's extraction changes; always pull
# the latest at build time in its own layer (rather than relying on the
# requirements.txt pin alone) so a stale cached layer never silently keeps
# an old, broken version around across deploys.
RUN pip install --no-cache-dir -U yt-dlp && yt-dlp --version

# Copy the rest of the project (frontend files + backend source).
COPY . .

RUN mkdir -p backend/downloads

WORKDIR /app/backend

# Render (and most PaaS) inject $PORT at runtime; default to 8000 locally.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
