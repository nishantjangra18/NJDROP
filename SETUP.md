# NJDROP — Windows Setup Guide

## Project structure

```text
Video Downloader/
│
├── index.html
├── style.css
├── script.js
│
└── backend/
    ├── main.py
    ├── requirements.txt
    └── downloads/        (created automatically, holds temp files)
```

The FastAPI backend serves the frontend itself (via `StaticFiles`), so there's
only **one server to run** and no CORS configuration needed.

---

## 1. Install Python

Make sure Python 3.10+ is installed and on PATH:

```powershell
python --version
```

If not installed, get it from https://www.python.org/downloads/ (check "Add
python.exe to PATH" during install).

---

## 2. Install FFmpeg (required for merging audio/video streams)

1. Download a Windows build from https://www.gyan.dev/ffmpeg/builds/ (get the
   "release essentials" zip).
2. Extract it somewhere permanent, e.g. `C:\ffmpeg`.
3. Add the `bin` folder to your system PATH:
   - Search "Environment Variables" in the Start menu → **Edit the system
     environment variables** → **Environment Variables**.
   - Under **System variables**, select `Path` → **Edit** → **New** → add
     `C:\ffmpeg\bin`.
   - Click OK on all dialogs.
4. Open a **new** terminal window and verify:

```powershell
ffmpeg -version
```

yt-dlp automatically detects and uses FFmpeg from PATH — no extra config
needed in the code.

---

## 3. Set up the backend

Open a terminal in the project's `backend` folder:

```powershell
cd "d:\Nishant Jangra\Coding\Projects\Video Downloader\backend"
```

Create and activate a virtual environment (recommended):

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

> If you get an "execution policy" error activating the venv, run:
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` then retry.

Install dependencies (this installs FastAPI, Uvicorn, and yt-dlp):

```powershell
pip install -r requirements.txt
```

---

## 4. Start the backend server

Still inside `backend/`, with the venv active:

```powershell
uvicorn main:app --reload
```

You should see it listening on `http://127.0.0.1:8000`.

---

## 5. Open the app

Just visit **http://127.0.0.1:8000** in your browser. FastAPI serves
`index.html`, `style.css`, and `script.js` directly — no separate frontend
server, no CORS setup, nothing else to configure.

The API itself lives at `POST http://127.0.0.1:8000/api/download`.

---

## Notes

- Downloaded files are written temporarily to `backend/downloads/` and are
  **automatically deleted** right after being sent to the browser (via
  FastAPI's `BackgroundTasks`).
- Videos longer than 60 minutes are rejected as a safety cap — adjust
  `MAX_DURATION_SECONDS` in `main.py` if you need a different limit.
- This tool is intended only for downloading content you own or are
  authorized to download. It does not attempt to bypass any platform
  protections and only processes a single URL you explicitly submit.
- If `pip install -r requirements.txt` fails on `yt-dlp`, you can always
  upgrade it independently later with `pip install -U yt-dlp` — YouTube and
  Instagram frequently change their internals, and newer yt-dlp releases fix
  extraction breakage quickly.

---

## Fixing "Sign in to confirm you're not a bot" errors

YouTube periodically bot-checks requests that don't look like a real signed-in
browser session — this can happen on any IP, cloud-hosted or local, and shows
up as a 422/503 error from `/api/download`. The reliable fix is supplying
cookies from your own logged-in YouTube session so yt-dlp authenticates like
a real user.

### 1. Export cookies.txt from Chrome/Edge

1. Install the **"Get cookies.txt LOCALLY"** extension from the Chrome Web
   Store (works in Edge too) — search for it by that exact name. Avoid
   extensions that upload your cookies anywhere; this one exports locally
   only.
2. Log into **youtube.com** in that browser if you aren't already.
3. Click the extension icon while on a youtube.com tab → **Export** → save
   the file as `cookies.txt`.

### 2. Use it locally

Place the exported file at:

```
backend/cookies.txt
```

(This path is already git-ignored — it will never be committed.) The backend
picks it up automatically on the next request; no restart needed.

### 3. Use it on Render

Cookies expire and shouldn't be committed to your repo, so upload them as a
**Secret File**:

1. Render dashboard → your service → **Environment** tab → **Secret Files**.
2. Add a new secret file with path `/etc/secrets/cookies.txt` and paste the
   contents of your exported `cookies.txt`.
3. Add an environment variable `COOKIES_FILE_PATH` = `/etc/secrets/cookies.txt`.
4. Redeploy (or it picks it up on the next request if the service is already
   running with that env var set).

**Cookies expire** — typically after a few weeks to a couple months depending
on your Google account activity. If bot-check errors come back, just repeat
the export and re-upload the secret file.
