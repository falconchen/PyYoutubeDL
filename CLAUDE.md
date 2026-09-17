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
export FLASK_APP=app.py && export FLASK_DEBUG=1 && flask run --host=0.0.0.0 --port=5100

# Run tests
python -m pytest tests/test_timezone.py tests/test_video_info.py -v

# Flask CLI: fetch YouTube cookies from the configured YTC API
flask get-cookie

# Export browser cookies for yt-dlp
yt-dlp -vU --cookies-from-browser firefox --cookies firefox-cookie.txt
```

## Architecture

The system has three persistent processes that communicate through the filesystem:

**Multi-user and anonymous access:** accounts, Google identity links, per-user OAuth tokens and resource ownership live in SQLite (`user_store.py`, `USER_DB_PATH`). Anonymous visitors get three tasks per client IP and local calendar day by default; IPs are HMAC-hashed for quota accounting, while a separate signed-session random ID owns tasks and media. Anonymous media expires after 24 hours through `anonymous_cleanup.py`. The first account becomes an active admin and adopts only unowned legacy data; logging in transfers only the current anonymous session. All task/media routes filter by the current user or anonymous owner. Google sign-in and "bind Google" share one flow at `/oauth/start?intent=login|bind`; binding requests the YouTube and Drive scopes that playlist monitoring and future Drive uploads need. Users create personal access tokens (`dlpat_…`, stored as SHA-256 hashes in `access_tokens`, optional expiry, last-used time) on `/profile`; `Authorization: Bearer` is honored only under `/api/` (an invalid token gets 401 rather than falling back to anonymous), and token-authenticated requests cannot reach the session-only profile routes. `/api/downloader_log` returns the full log to admins and only lines mentioning the user's own tasks to other users (`scope: all|own`); the global `EXTENSION_LOG_TOKEN` header is deprecated but still accepted.

**1. Flask web app (`app.py`)** — Serves the UI as a single page with two modes: a downloader for submitting URLs and a media library for playing downloaded files. `/`, `/player` and `/audio-player` all render `templates/index.html`; the media library fetches its list from `/api/media_list` and plays through zwplayer (`static/zwplayer/`). On form submission, it writes task files (`.txt` containing the URL) into `URLS_DIR` (`./urls/`). Provides a REST API (`/api/add_task`, `/api/video_info`, `/api/task_info`, `/api/task_action`, `/api/media_list`, `/api/get-cookie`). It is started directly with the project's Python environment.

**2. Downloader (`downloader.py`)** — Uses `watchdog` to monitor `URLS_DIR` for new `.txt` files. On detection, renames the file to `.downloading`, invokes `yt-dlp` as a subprocess (reading output line-by-line in real time), moves finished files to `FILES_DIR`, then renames the task file to `.ok` or `.fail`. Downloads go through a per-task temp dir in `TMP_DIR`. Video vs. audio mode is determined by the task file's first character (`v` = video, `a` = audio).

**3. WebDAV uploader (`webdav_uploader.py`)** — Uses `watchdog` to monitor `FILES_DIR` for new media files. Determines category by extension (`.mp4`/`.mkv`/`.webm`/`.mov` → Video; `.mp3` → Audio). Uploads to separate WebDAV backends with date-based directory structure (`/YYYYMMDD/filename`). Supports retry with configurable count and delay. Cleans up local files after upload per `DELETE_AFTER_UPLOAD` config. Also cleans expired local files on startup (`FILES_EXPIRE_DAYS`) and prunes old date directories on WebDAV (`*_WEBDAV_KEEP_COUNT`).

**Task file lifecycle:** `.txt` (queued) → `.downloading` (in progress) → `.ok` / `.fail` (completed). Pausing moves `.txt` / `.downloading` to `.paused`, and resuming renames it back to `.txt` so yt-dlp continues from the `.part` files in the task's temp dir.

**Task actions (`/api/task_action`):** pause, resume, restart (anything but completed; discards the temp dir and re-downloads under the same ID) and delete (unfinished tasks always lose their temp files; completed tasks delete their media only when `delete_files` is set, and only files the same owner owns). The web app handles non-downloading tasks itself. For `.downloading` it atomically writes `<task_id>.control` containing the action; the downloader terminates the yt-dlp process tree with psutil and then rewrites the state. yt-dlp stays in the downloader's process group because Supervisor stops it with `stopasgroup`, and only `Popen` may reap it — if psutil reaps the direct child, `Popen.wait()` gets ECHILD and reports exit code 0, which would treat a paused download as finished. Controls left while the downloader was down are applied at startup, before `RESUME_INTERRUPTED_DOWNLOADS`.

When `RESUME_INTERRUPTED_DOWNLOADS` is `true`, downloader startup queues existing `.downloading` tasks with their original task IDs so yt-dlp can reuse partial files. The default is `false` to avoid automatically running stale tasks.

**Alternative downloader (`downloader-opt.py`):** A variant that uses the `yt_dlp` Python library directly instead of subprocess. Not used by default; kept as an alternative implementation.

**Configuration (`config_util.py`):** Loads `config.json` with a `DEFAULT_CONFIG` fallback. Although the configuration files keep the `.json` suffix, runtime configuration uses the JSON5 parser and supports the JSONC subset (`//`, `/* ... */`, and trailing commas). Other API, log, and result JSON remains strict JSON. Path-based config keys (`URLS_DIR`, `TMP_DIR`, `FILES_DIR`, `LOG_DIR`) are resolved relative to the script directory.

**yt-dlp config:** `yt-dlp.conf` for video, `yta-dlp.conf` for audio. If a `.local.conf` variant exists (e.g., `yt-dlp.local.conf`), it takes precedence — these are gitignored for machine-specific overrides.

**Notifications (`bark_util.py`):** Singleton `BarkNotificator` wrapper for push notifications on download/upload completion or failure.

**Cookie management:** YouTube auth cookies can be fetched from an HTTP API (configured via the `YTC` config section) through a Flask CLI command (`flask get-cookie`) or API endpoint (`/api/get-cookie`). The `update_cookie.sh` cron script wraps `flask get-cookie`.

## Testing

Tests are stored in `tests/` and use `unittest` with Flask's test client. `tests/test_video_info.py` tests the `/api/video_info` endpoint — note that valid video tests hit YouTube directly and need network access. Run individually with `python -m pytest tests/test_video_info.py -v`.
