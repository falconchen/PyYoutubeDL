# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Shell commands must be compatible with Linux, FreeBSD, and macOS.

## Commands

```bash
# Install dependencies (virtual env expected at ./venv)
pip install -r requirements.txt

# Start all services (app, downloader, webdav_uploader) via the launcher
python start.py

# Or use the shell runner (activates venv, upgrades yt-dlp, stops old processes, starts all)
./runner.sh

# Stop all running services
python stop.py

# Run Flask dev server with auto-reload
export FLASK_APP=app.py && export FLASK_DEBUG=1 && flask run --host=0.0.0.0

# Run tests
python -m pytest test_timezone.py test_video_info.py -v

# Flask CLI: fetch YouTube cookies from the configured YTC API
flask get-cookie

# Export browser cookies for yt-dlp
yt-dlp -vU --cookies-from-browser firefox --cookies firefox-cookie.txt
```

## Architecture

The system has three persistent processes that communicate through the filesystem:

**1. Flask web app (`app.py`)** — Serves the UI (index page for submitting URLs, player page for watching downloaded videos). On form submission, it writes task files (`.txt` containing the URL) into `URLS_DIR` (`./urls/`). Provides a REST API (`/api/add_task`, `/api/video_info`, `/api/task_info`, `/api/get-cookie`). Deployed via Passenger WSGI (`passenger_wsgi.py`) on shared hosting with `devil` CLI, or directly via Flask's dev server.

**2. Downloader (`downloader.py`)** — Uses `watchdog` to monitor `URLS_DIR` for new `.txt` files. On detection, renames the file to `.downloading`, invokes `yt-dlp` as a subprocess (reading output line-by-line in real time), moves finished files to `FILES_DIR`, then renames the task file to `.ok` or `.fail`. Downloads go through a per-task temp dir in `TMP_DIR`. Video vs. audio mode is determined by the task file's first character (`v` = video, `a` = audio).

**3. WebDAV uploader (`webdav_uploader.py`)** — Uses `watchdog` to monitor `FILES_DIR` for new media files. Determines category by extension (`.mp4`/`.mkv`/`.webm`/`.mov` → Video; `.mp3` → Audio). Uploads to separate WebDAV backends with date-based directory structure (`/YYYYMMDD/filename`). Supports retry with configurable count and delay. Cleans up local files after upload per `DELETE_AFTER_UPLOAD` config. Also cleans expired local files on startup (`FILES_EXPIRE_DAYS`) and prunes old date directories on WebDAV (`*_WEBDAV_KEEP_COUNT`).

**Task file lifecycle:** `.txt` (queued) → `.downloading` (in progress) → `.ok` / `.fail` (completed).

**Alternative downloader (`downloader-opt.py`):** A variant that uses the `yt_dlp` Python library directly instead of subprocess. Not used by default; kept as an alternative implementation.

**Configuration (`config_util.py`):** Loads `config.json` with a `DEFAULT_CONFIG` fallback. Path-based config keys (`URLS_DIR`, `TMP_DIR`, `FILES_DIR`, `LOG_DIR`) are resolved relative to the script directory.

**yt-dlp config:** `yt-dlp.conf` for video, `yta-dlp.conf` for audio. If a `.local.conf` variant exists (e.g., `yt-dlp.local.conf`), it takes precedence — these are gitignored for machine-specific overrides.

**Notifications (`bark_util.py`):** Singleton `BarkNotificator` wrapper for push notifications on download/upload completion or failure.

**Cookie management:** YouTube auth cookies can be fetched from an HTTP API (configured via the `YTC` config section) through a Flask CLI command (`flask get-cookie`) or API endpoint (`/api/get-cookie`). The `update_cookie.sh` cron script wraps `flask get-cookie`.

## Testing

Tests use `unittest` with Flask's test client. `test_video_info.py` tests the `/api/video_info` endpoint — note that valid video tests hit YouTube directly and need network access. Run individually with `python -m pytest test_video_info.py -v`.
