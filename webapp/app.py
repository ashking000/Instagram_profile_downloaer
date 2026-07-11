"""
Flask web app for downloading Instagram profiles.

Provides a browser-style UI (see static/) plus a small JSON API that wraps the
DownloaderSession from downloader.py. Each browser session gets its own
Instaloader instance so logins don't leak between users.

Run it:
    python webapp/app.py
Then open http://127.0.0.1:5000 in your browser.

SECURITY NOTE: this server has no authentication of its own and accepts
Instagram credentials. Run it only on your local machine (127.0.0.1) and never
expose it to the public internet.
"""

import io
import os
import re
import secrets
import zipfile
from typing import Dict

from flask import (
    Flask,
    jsonify,
    request,
    send_file,
    send_from_directory,
    session,
)

from downloader import DownloaderSession, list_media, media_type

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_ROOT = os.path.join(BASE_DIR, "downloads")
SESSION_DIR = os.path.join(BASE_DIR, "sessions")
BROWSER_ROOT = os.path.join(BASE_DIR, "browser_profiles")
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = Flask(__name__, static_folder=None)
app.secret_key = secrets.token_hex(32)

# In-memory map of browser-session-id -> DownloaderSession.
_sessions: Dict[str, DownloaderSession] = {}

# Only allow safe Instagram-username characters (defense against path traversal).
USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,64}$")


def get_session() -> DownloaderSession:
    """Return (creating if needed) the DownloaderSession for this browser."""
    sid = session.get("sid")
    if not sid or sid not in _sessions:
        sid = secrets.token_hex(16)
        session["sid"] = sid
        _sessions[sid] = DownloaderSession(
            DOWNLOAD_ROOT, SESSION_DIR,
            browser_dir=os.path.join(BROWSER_ROOT, sid),
        )
    return _sessions[sid]


def safe_username(name: str) -> str:
    name = (name or "").strip().lstrip("@")
    if not USERNAME_RE.match(name):
        raise ValueError("Invalid username.")
    return name


def profile_dir(username: str) -> str:
    return os.path.join(DOWNLOAD_ROOT, username)


# --------------------------------------------------------------------------
# Static frontend
# --------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/static/<path:filename>")
def static_files(filename: str):
    return send_from_directory(STATIC_DIR, filename)


# --------------------------------------------------------------------------
# Auth API
# --------------------------------------------------------------------------
@app.route("/api/status")
def api_status():
    ses = get_session()
    return jsonify({"logged_in": ses.is_logged_in, "username": ses.username})


@app.route("/api/login", methods=["POST"])
def api_login():
    ses = get_session()
    data = request.get_json(silent=True) or {}
    user = (data.get("username") or "").strip().lstrip("@")
    password = data.get("password") or ""
    if not user or not password:
        return jsonify({"status": "error", "message": "Username and password required."}), 400
    try:
        result = ses.login(user, password)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 401
    except Exception as exc:  # network / instaloader errors
        return jsonify({"status": "error", "message": f"Login failed: {exc}"}), 500
    return jsonify({"status": result, "username": ses.username})


@app.route("/api/2fa", methods=["POST"])
def api_2fa():
    ses = get_session()
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"status": "error", "message": "2FA code required."}), 400
    try:
        ses.complete_2fa(code)
    except Exception as exc:
        return jsonify({"status": "error", "message": f"2FA failed: {exc}"}), 401
    return jsonify({"status": "ok", "username": ses.username})


@app.route("/api/import-browser", methods=["POST"])
def api_import_browser():
    ses = get_session()
    data = request.get_json(silent=True) or {}
    browser = (data.get("browser") or "").strip()
    if not browser:
        return jsonify({"status": "error", "message": "Choose a browser."}), 400
    try:
        user = ses.import_browser_session(browser)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"status": "error", "message": f"Import failed: {exc}"}), 500
    return jsonify({"status": "ok", "username": user})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    ses = get_session()
    ses.logout()
    return jsonify({"status": "ok"})


# --------------------------------------------------------------------------
# Download API
# --------------------------------------------------------------------------
@app.route("/api/download", methods=["POST"])
def api_download():
    ses = get_session()
    data = request.get_json(silent=True) or {}
    try:
        username = safe_username(data.get("profile"))
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    engine = "browser" if data.get("engine") == "browser" else "instaloader"
    mobile = bool(data.get("mobile"))
    job = ses.start_download(username, engine=engine, mobile=mobile)
    return jsonify({"status": "started", "job": job.as_dict()})


@app.route("/api/browser/login", methods=["POST"])
def api_browser_login():
    ses = get_session()
    data = request.get_json(silent=True) or {}
    mobile = bool(data.get("mobile"))
    ses.open_browser_login(mobile=mobile)
    return jsonify({"status": "started", "state": ses.browser_login_state()})


@app.route("/api/browser/status")
def api_browser_status():
    ses = get_session()
    return jsonify(ses.browser_login_state())


@app.route("/api/download/<job_id>")
def api_download_status(job_id: str):
    ses = get_session()
    job = ses.get_job(job_id)
    if not job:
        return jsonify({"status": "error", "message": "Unknown job."}), 404
    return jsonify(job.as_dict())


# --------------------------------------------------------------------------
# Media listing + serving
# --------------------------------------------------------------------------
@app.route("/api/media/<username>")
def api_media(username: str):
    try:
        username = safe_username(username)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    items = list_media(profile_dir(username))
    for it in items:
        it["url"] = f"/api/file/{username}/{it['name']}"
    return jsonify({"profile": username, "count": len(items), "items": items})


@app.route("/api/file/<username>/<path:filename>")
def api_file(username: str, filename: str):
    try:
        username = safe_username(username)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    directory = profile_dir(username)
    # send_from_directory guards against path traversal in `filename`.
    as_attachment = request.args.get("download") == "1"
    return send_from_directory(directory, filename, as_attachment=as_attachment)


# --------------------------------------------------------------------------
# ZIP batch download
# --------------------------------------------------------------------------
@app.route("/api/zip/<username>", methods=["POST"])
def api_zip(username: str):
    try:
        username = safe_username(username)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    directory = profile_dir(username)
    if not os.path.isdir(directory):
        return jsonify({"status": "error", "message": "No downloaded media."}), 404

    data = request.get_json(silent=True) or {}
    requested = data.get("files")  # None => all media

    available = {it["name"] for it in list_media(directory)}
    if requested:
        names = [n for n in requested if n in available]
    else:
        names = sorted(available)
    if not names:
        return jsonify({"status": "error", "message": "No matching files."}), 400

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                zf.write(path, arcname=name)
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{username}_media.zip",
    )


if __name__ == "__main__":
    os.makedirs(DOWNLOAD_ROOT, exist_ok=True)
    os.makedirs(SESSION_DIR, exist_ok=True)
    os.makedirs(BROWSER_ROOT, exist_ok=True)
    print("Instagram downloader web app running at http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
