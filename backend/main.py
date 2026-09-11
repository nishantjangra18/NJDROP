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
import os
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

# Optional: path to a Netscape-format cookies.txt (exported from a browser
# logged into YouTube/Instagram). Cloud/datacenter IPs are often bot-checked
# by YouTube; supplying cookies from a real signed-in session is the most
# reliable fix. Set COOKIES_FILE_PATH as an env var pointing at an uploaded
# secret file (e.g. on Render: Settings -> Secret Files), or drop a
# cookies.txt next to this script for local testing.
_COOKIES_SOURCE = Path(os.environ.get("COOKIES_FILE_PATH", str(BASE_DIR / "cookies.txt")))

# yt-dlp writes updated cookies back to this file when it's done (cookie
# jar autosave). Platforms like Render mount secret files read-only, which
# makes that write crash the whole request. So if the configured cookies
# file isn't writable, copy it once into a writable spot and use that copy
# instead — yt-dlp can then freely update it without touching the original.
COOKIES_FILE = _COOKIES_SOURCE
if _COOKIES_SOURCE.exists():
    if not os.access(_COOKIES_SOURCE, os.W_OK):
        writable_copy = DOWNLOADS_DIR.parent / "cookies.runtime.txt"
        try:
            writable_copy.write_bytes(_COOKIES_SOURCE.read_bytes())
            COOKIES_FILE = writable_copy
            logger.info(
                "Cookies file at %s is read-only; copied to writable %s for yt-dlp's use.",
                _COOKIES_SOURCE, writable_copy,
            )
        except OSError:
            logger.exception("Failed to copy read-only cookies file to a writable location.")
    logger.info("Cookies file ready at %s — will be used for YouTube requests.", COOKIES_FILE)
else:
    logger.warning(
        "No cookies file found at %s (set COOKIES_FILE_PATH or place backend/cookies.txt). "
        "YouTube requests from this server's IP may be bot-checked and fail without it.",
        _COOKIES_SOURCE,
    )


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

# YouTube player clients to try in order. Each uses a different auth path;
# when one gets bot-checked, the next often succeeds without cookies. This
# is retried as fully separate yt-dlp runs (not passed together) since a
# single combined attempt can fail outright instead of cleanly falling back.
YOUTUBE_CLIENT_ATTEMPTS = [
    ["tv"],
    ["ios"],
    ["android"],
    ["web_safari"],
    ["web"],
]


def _build_ydl_opts(output_template: str, player_client: list | None) -> dict:
    opts = {
        "outtmpl": output_template,
        # Prefer mp4 (broadest player compatibility), but fall back to
        # whatever best video+audio combo is actually available (webm/AV1
        # etc.) rather than failing outright — some clients (ios/android)
        # don't expose mp4-native adaptive streams for every video.
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "retries": 3,
        "socket_timeout": 30,
        # FFmpeg must be on PATH for merging separate audio/video streams.
    }

    if player_client:
        opts["extractor_args"] = {"youtube": {"player_client": player_client}}

    if COOKIES_FILE and COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)

    return opts


def _resolve_final_path(ydl: "yt_dlp.YoutubeDL", info: dict, job_id: str) -> Path:
    final_path = Path(ydl.prepare_filename(info))
    if not final_path.exists():
        # merge_output_format may have changed the extension to mp4
        candidate = final_path.with_suffix(".mp4")
        if candidate.exists():
            final_path = candidate

    if not final_path.exists():
        # Fall back to scanning the downloads dir for this job_id
        matches = list(DOWNLOADS_DIR.glob(f"{job_id}.*"))
        if matches:
            final_path = matches[0]

    return final_path


def _is_bot_check_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "sign in to confirm" in message or "not a bot" in message


def run_download(platform: str, url: str, job_id: str) -> Path:
    """
    Downloads the video with yt-dlp, merging audio+video via FFmpeg when the
    best available streams are separate. Returns the path to the final file.

    For YouTube, retries across several player clients (ios/android/
    tv_embedded/web) since YouTube's bot-check applies per-client — one
    getting blocked doesn't mean the others will be.
    """
    output_template = str(DOWNLOADS_DIR / f"{job_id}.%(ext)s")

    client_attempts = YOUTUBE_CLIENT_ATTEMPTS if platform == "youtube" else [None]

    last_exc: Exception | None = None

    for attempt_index, player_client in enumerate(client_attempts):
        ydl_opts = _build_ydl_opts(output_template, player_client)

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

                final_path = _resolve_final_path(ydl, info, job_id)
                if not final_path.exists():
                    raise HTTPException(status_code=500, detail="Processing finished but the output file was not found.")

                return final_path

        except yt_dlp.utils.DownloadError as exc:
            last_exc = exc
            message = str(exc).lower()
            is_last_attempt = attempt_index == len(client_attempts) - 1

            # These outcomes are definitive regardless of which client was
            # used — retrying with a different player client won't help, so
            # stop immediately instead of burning through the rest. Careful
            # to match on the *video* being gone, not a per-client "format
            # not available" error (that's exactly what should be retried).
            if "unsupported url" in message or "unable to extract" in message:
                raise HTTPException(status_code=400, detail="This link isn't supported or the URL is invalid.")
            if "private video" in message or "this is a private" in message:
                raise HTTPException(status_code=403, detail="This video is private and can't be accessed.")
            if "video unavailable" in message or "video is unavailable" in message or "has been removed" in message:
                raise HTTPException(status_code=404, detail="This video is unavailable or has been removed.")

            # Everything else (bot-checks, "no video formats found", other
            # per-client extraction glitches) is worth retrying on the next
            # player client before giving up.
            if not is_last_attempt:
                logger.warning(
                    "Job %s: client %s failed (%s), trying next client",
                    job_id, player_client, exc,
                )
                continue

            if _is_bot_check_error(exc):
                logger.error("yt-dlp bot-check triggered on all clients: %s", exc)
                raise HTTPException(
                    status_code=503,
                    detail="YouTube is temporarily blocking this server's requests. Please try again shortly.",
                )

            logger.error("yt-dlp download error: %s", exc)
            raise HTTPException(status_code=422, detail="This video could not be processed. It may be restricted or unavailable.")

        except HTTPException:
            raise

        except Exception as exc:  # noqa: BLE001 - surface as a clean 500 to the client
            logger.exception("Unexpected error during download")
            raise HTTPException(status_code=500, detail="An unexpected server error occurred while processing your video.")

    # Exhausted all client attempts without success or a raised HTTPException
    logger.error("All player client attempts failed: %s", last_exc)
    raise HTTPException(
        status_code=503,
        detail="YouTube is temporarily blocking this server's requests. Please try again shortly.",
    )


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
    cookies_info = {"configured": False}

    if COOKIES_FILE.exists():
        try:
            lines = COOKIES_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
            cookie_lines = [
                ln for ln in lines if ln.strip() and not ln.strip().startswith("#")
            ]
            youtube_cookie_names = sorted({
                parts[5]
                for ln in cookie_lines
                if len(parts := ln.split("\t")) >= 7 and "youtube.com" in parts[0]
            })
            cookies_info = {
                "configured": True,
                "total_lines": len(lines),
                "cookie_entries": len(cookie_lines),
                "youtube_cookie_names": youtube_cookie_names,
                # These three being present together is what actually proves
                # a logged-in session, not just consent/analytics cookies.
                "looks_logged_in": {"SID", "SSID", "HSID"}.issubset(set(youtube_cookie_names))
                or "__Secure-3PSID" in youtube_cookie_names
                or "LOGIN_INFO" in youtube_cookie_names,
            }
        except OSError as exc:
            cookies_info = {"configured": True, "read_error": str(exc)}

    pot_provider_status = "unreachable"
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:4416/ping", timeout=2) as resp:
            pot_provider_status = "ok" if resp.status == 200 else f"http_{resp.status}"
    except Exception as exc:  # noqa: BLE001 - just a diagnostic probe
        pot_provider_status = f"error: {exc}"

    return {
        "status": "ok",
        "cookies": cookies_info,
        "yt_dlp_version": yt_dlp.version.__version__,
        "pot_provider": pot_provider_status,
    }


@app.get("/api/debug-plugins")
def debug_plugins():
    """Diagnostic-only: confirms whether the bgutil PO token plugin package
    is importable and which extractor/PO-token classes yt-dlp registered."""
    result = {}
    try:
        import bgutil_ytdlp_pot_provider  # noqa: F401
        result["bgutil_package_importable"] = True
    except ImportError as exc:
        result["bgutil_package_importable"] = False
        result["import_error"] = str(exc)

    try:
        from yt_dlp.extractor.youtube.pot._provider import IEContentProvider
        registered = [cls.__name__ for cls in IEContentProvider.__subclasses__()]
        result["registered_pot_provider_classes"] = registered
    except Exception as exc:  # noqa: BLE001
        result["pot_provider_class_check_error"] = str(exc)

    return result


@app.get("/api/debug-extract")
def debug_extract(url: str):
    """
    Diagnostic-only endpoint: runs yt-dlp's extract_info (no download) for
    each player client and returns the raw result/error for each, directly
    in the JSON response. Exists purely to debug extraction failures without
    needing to read server logs. Not linked from the UI.
    """
    import io
    import contextlib

    results = {}
    for player_client in [["tv"], ["ios"], ["android"], ["web_safari"], ["web"], None]:
        label = player_client[0] if player_client else "default"
        captured = io.StringIO()
        opts = {
            "verbose": True,
            "skip_download": True,
            "socket_timeout": 20,
            "logger": logging.getLogger(f"ytdlp-debug-{label}"),
        }
        # Route yt-dlp's verbose/debug lines into our capture buffer.
        handler = logging.StreamHandler(captured)
        opts["logger"].addHandler(handler)
        opts["logger"].setLevel(logging.DEBUG)
        opts["logger"].propagate = False

        if player_client:
            opts["extractor_args"] = {"youtube": {"player_client": player_client}}
        if COOKIES_FILE.exists():
            opts["cookiefile"] = str(COOKIES_FILE)

        pot_lines = []
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                formats = info.get("formats") or []
                results[label] = {
                    "ok": True,
                    "title": info.get("title"),
                    "format_count": len(formats),
                    "sample_format_ids": [f.get("format_id") for f in formats[:5]],
                }
        except Exception as exc:  # noqa: BLE001 - diagnostic endpoint, want the raw text
            results[label] = {"ok": False, "error": str(exc)}
        finally:
            opts["logger"].removeHandler(handler)
            captured_text = captured.getvalue()
            pot_lines = [ln for ln in captured_text.splitlines() if "pot" in ln.lower()]
            results[label]["pot_debug_lines"] = pot_lines[:15]

    return {"yt_dlp_version": yt_dlp.version.__version__, "results": results}


# --------------------------------------------------------------------------
# Serve the static frontend (index.html / style.css / script.js) from the
# same FastAPI app so there's no separate server and no CORS to configure.
# Mounted last so it doesn't shadow the /api routes above.
# --------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
