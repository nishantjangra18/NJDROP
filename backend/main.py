"""
NJDROP — Video Downloader backend.

FastAPI service that wraps yt-dlp (+ FFmpeg for stream merging) to fetch a
single video from a user-supplied YouTube or Instagram Reel URL, and returns
the finished file directly as a downloadable HTTP response.

This service is intended ONLY for downloading content the requesting user
owns or is otherwise authorized to download (their own uploads, content
licensed to them, material explicitly permitted for offline use, etc.).
It performs no scraping beyond what yt-dlp needs to fetch a single URL the
user explicitly provides, and applies no bypass of platform protections.

Run with:  uvicorn main:app --reload
(see ../SETUP.md for full Windows setup instructions)
"""

import logging
import re
import uuid
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
import yt_dlp

# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("njdrop")

BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

FRONTEND_DIR = BASE_DIR.parent  # project root, holds index.html/style.css/script.js

app = FastAPI(title="NJDROP Downloader API")

URL_PATTERNS = {
    "youtube": re.compile(r"^https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", re.IGNORECASE),
    "instagram": re.compile(r"^https?://(www\.)?instagram\.com/", re.IGNORECASE),
}

MAX_DURATION_SECONDS = 60 * 60  # 1 hour safety cap


# --------------------------------------------------------------------------
# Request / response models
# --------------------------------------------------------------------------

class DownloadRequest(BaseModel):
    platform: Literal["youtube", "instagram"]
    url: str

    @field_validator("url")
    @classmethod
    def url_must_be_reasonable(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 2000:
            raise ValueError("URL is missing or too long")
        if not v.lower().startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def validate_url_for_platform(platform: str, url: str) -> None:
    pattern = URL_PATTERNS[platform]
    if not pattern.match(url):
        raise HTTPException(
            status_code=400,
            detail=f"That link doesn't look like a {platform.capitalize()} URL.",
        )


def cleanup_file(path: Path) -> None:
    """Best-effort removal of a temp file after the response has been sent."""
    try:
        if path.exists():
            path.unlink()
            logger.info("Cleaned up temp file: %s", path.name)
    except OSError as exc:
        logger.warning("Failed to clean up %s: %s", path.name, exc)


def safe_filename(name: str, fallback: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    return name or fallback


# --------------------------------------------------------------------------
# Core download logic
# --------------------------------------------------------------------------

def run_download(platform: str, url: str, job_id: str) -> Path:
    """
    Downloads the video with yt-dlp, merging audio+video via FFmpeg when the
    best available streams are separate. Returns the path to the final file.
    """
    output_template = str(DOWNLOADS_DIR / f"{job_id}.%(ext)s")

    ydl_opts = {
        "outtmpl": output_template,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "retries": 3,
        "socket_timeout": 30,
        # FFmpeg must be on PATH for merging separate audio/video streams.
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            if info is None:
                raise HTTPException(status_code=422, detail="Could not read video information from that link.")

            duration = info.get("duration") or 0
            if duration and duration > MAX_DURATION_SECONDS:
                raise HTTPException(
                    status_code=422,
                    detail="This video is too long to process (limit: 60 minutes).",
                )

            if info.get("is_live"):
                raise HTTPException(status_code=422, detail="Live streams can't be downloaded.")

            ydl.download([url])

            # Resolve the final filename yt-dlp actually wrote (post merge).
            final_path = Path(ydl.prepare_filename(info))
            if not final_path.exists():
                # merge_output_format may have changed the extension to mp4
                candidate = final_path.with_suffix(".mp4")
                if candidate.exists():
                    final_path = candidate

            if not final_path.exists():
                # Fall back to scanning the downloads dir for this job_id
                matches = list(DOWNLOADS_DIR.glob(f"{job_id}.*"))
                if not matches:
                    raise HTTPException(status_code=500, detail="Processing finished but the output file was not found.")
                final_path = matches[0]

            return final_path

    except yt_dlp.utils.DownloadError as exc:
        message = str(exc).lower()
        if "unsupported url" in message or "unable to extract" in message:
            raise HTTPException(status_code=400, detail="This link isn't supported or the URL is invalid.")
        if "private" in message:
            raise HTTPException(status_code=403, detail="This video is private and can't be accessed.")
        if "unavailable" in message or "not available" in message:
            raise HTTPException(status_code=404, detail="This video is unavailable or has been removed.")
        logger.error("yt-dlp download error: %s", exc)
        raise HTTPException(status_code=422, detail="This video could not be processed. It may be restricted or unavailable.")

    except HTTPException:
        raise

    except Exception as exc:  # noqa: BLE001 - surface as a clean 500 to the client
        logger.exception("Unexpected error during download")
        raise HTTPException(status_code=500, detail="An unexpected server error occurred while processing your video.")


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.post("/api/download")
def download_video(payload: DownloadRequest, background_tasks: BackgroundTasks):
    validate_url_for_platform(payload.platform, payload.url)

    job_id = uuid.uuid4().hex
    logger.info("Job %s: starting %s download for %s", job_id, payload.platform, payload.url)

    file_path = run_download(payload.platform, payload.url, job_id)

    # Build a friendly, safe download filename
    download_name = safe_filename(f"{payload.platform}-{job_id[:8]}{file_path.suffix}", f"{job_id}{file_path.suffix}")

    # Delete the temp file once the response has finished sending
    background_tasks.add_task(cleanup_file, file_path)

    logger.info("Job %s: sending file %s", job_id, file_path.name)

    return FileResponse(
        path=file_path,
        filename=download_name,
        media_type="application/octet-stream",
        background=background_tasks,
    )


@app.get("/api/health")
def health_check():
    return {"status": "ok"}


# --------------------------------------------------------------------------
# Serve the static frontend (index.html / style.css / script.js) from the
# same FastAPI app so there's no separate server and no CORS to configure.
# Mounted last so it doesn't shadow the /api routes above.
# --------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
