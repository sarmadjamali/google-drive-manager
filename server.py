"""
Local Flask web server for Google Drive Storage Manager dashboard.
Run:  python server.py
Then open:  http://localhost:5000  in your browser.
All data stays on your machine — no cloud service involved.
"""

import json
import logging
import threading
import queue
import datetime
import traceback
from pathlib import Path

from flask import Flask, jsonify, request, Response, send_from_directory
from flask_cors import CORS
from googleapiclient.errors import HttpError as DriveHttpError

import config
from auth import get_drive_service, get_credentials
from storage import (
    get_storage_info,
    list_all_folders,
    list_migration_candidates,
    get_photos_summary,
    _expand_folder_tree,
    PHOTOS_SENTINEL,
)
from migrate import run_migration, _setup_logging

_setup_logging()
logger = logging.getLogger("gdrive_manager")


# ── Kept-files helpers ────────────────────────────────────────────────────────

def _kept_files_path():
    return Path(getattr(config, "KEPT_FILES_FILE", "kept_files.json"))

def _load_kept_files() -> dict:
    p = _kept_files_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_kept_files(data: dict):
    _kept_files_path().write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )

# ── App setup ────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static")
_kept_files_lock = threading.Lock()  # serialises concurrent read-modify-write on kept_files.json
CORS(app)  # allow the artifact/browser to call localhost

# ── Globals for migration state ───────────────────────────────────────────────
_migration_running   = False
_migration_queue: "queue.Queue[dict]" = queue.Queue()
_pending_deletes: "dict[str, dict]"   = {}   # file_id → {file_id, name, service}
def _get_services():
    """
    Create fresh Drive service instances per call.
    httplib2.Http is NOT thread-safe — sharing one instance across concurrent
    Flask threads causes ERR_CONNECTION_RESET crashes. Always build fresh.
    """
    primary_svc   = get_drive_service(config.PRIMARY_ACCOUNT)
    secondary_svc = get_drive_service(config.SECONDARY_ACCOUNT)
    return primary_svc, secondary_svc


# ── Request / error logging ───────────────────────────────────────────────────

@app.after_request
def _log_request(response):
    # Only log non-200 responses and non-SSE endpoints to keep the log clean
    if response.status_code >= 400 or request.path not in ("/api/progress",):
        if response.status_code >= 400:
            logger.warning(f"{request.method} {request.path} → {response.status_code}")
    return response


def _log_exc(endpoint: str, exc: Exception):
    """Log an exception with full traceback to migration.log."""
    logger.error(f"{endpoint} error: {exc}", exc_info=True)


# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    """Storage info for both accounts."""
    try:
        primary_svc, secondary_svc = _get_services()
        primary_info   = get_storage_info(primary_svc)
        secondary_info = get_storage_info(secondary_svc)
        return jsonify({
            "primary":   {**primary_info,   "email": config.PRIMARY_ACCOUNT},
            "secondary": {**secondary_info, "email": config.SECONDARY_ACCOUNT},
            "threshold": config.STORAGE_THRESHOLD_PERCENT,
        })
    except Exception as e:
        _log_exc("/api/status", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/folders")
def api_folders():
    """List all top-level folders in primary account, plus Google Photos entry."""
    try:
        primary_svc, _ = _get_services()
        folders = list_all_folders(primary_svc)

        # Prepend a special Google Photos entry
        try:
            photos = get_photos_summary(primary_svc)
            photos_entry = {
                "id":          PHOTOS_SENTINEL,
                "name":        "Google Photos",
                "type":        "photos",
                "count":       photos["count"],
                "total_bytes": photos["total_bytes"],
            }
        except Exception:
            photos_entry = {
                "id":    PHOTOS_SENTINEL,
                "name":  "Google Photos",
                "type":  "photos",
                "count": 0,
                "total_bytes": 0,
            }

        return jsonify({"folders": [photos_entry] + folders})
    except Exception as e:
        _log_exc("/api/folders", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/folder-size", methods=["POST"])
def api_folder_size():
    """
    Count all owned files (any type, any age) in a folder tree recursively.
    Returns {count, total_bytes} — used for the folder-list size badges.
    """
    try:
        folder_id = (request.json or {}).get("folder_id")
        if not folder_id:
            return jsonify({"error": "folder_id required"}), 400
        primary_svc, _ = _get_services()
        all_folder_ids = _expand_folder_tree(primary_svc, [folder_id])
        total_bytes = 0
        count = 0
        for fid in all_folder_ids:
            q = (f"'{fid}' in parents and trashed = false "
                 f"and mimeType != 'application/vnd.google-apps.folder' "
                 f"and 'me' in owners")
            page_token = None
            while True:
                resp = primary_svc.files().list(
                    q=q, fields="nextPageToken, files(size)",
                    pageSize=1000, pageToken=page_token,
                ).execute(num_retries=5)
                for f in resp.get("files", []):
                    total_bytes += int(f.get("size") or 0)
                    count += 1
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        return jsonify({"count": count, "total_bytes": total_bytes})
    except Exception as e:
        _log_exc("/api/folder-size", e)
        return jsonify({"error": str(e)}), 500


def _resolve_path(service, file_meta: dict, folder_cache: dict) -> str:
    """
    Walk the parents chain to build a human-readable path like:
      My Drive / Photos / Vacation 2023
    Uses folder_cache to avoid redundant API calls within one request.
    Files from Google Photos space have no parents — return "Google Photos" for those.
    """
    # Photos-space files are tagged by storage.py with _source="photos"
    if file_meta.get("_source") == "photos":
        return "Google Photos"

    parts = []
    parent_ids = file_meta.get("parents", [])
    current_id = parent_ids[0] if parent_ids else None

    while current_id:
        node = None
        if current_id in folder_cache:
            node = folder_cache[current_id]
        else:
            try:
                node = service.files().get(
                    fileId=current_id,
                    fields="id, name, parents"
                ).execute()
                folder_cache[current_id] = node
            except Exception:
                break   # can't resolve further — stop here

        if node is None:
            break

        name = node.get("name", "")
        # "My Drive" is the root — Drive API returns it as a node with no parents
        if name == "My Drive" or not node.get("parents"):
            parts.append("My Drive")
            break
        parts.append(name)
        p = node.get("parents", [])
        current_id = p[0] if p else None

    parts.reverse()
    return " / ".join(parts) if parts else "My Drive"


@app.route("/api/candidates", methods=["POST"])
def api_candidates():
    """
    List migration candidates from selected folder IDs.
    Body: {"folder_ids": ["id1", "id2", ...]}
    Returns each file with an added 'location' field (full Drive path).
    """
    try:
        primary_svc, _ = _get_services()
        folder_ids = (request.json or {}).get("folder_ids", [])
        files = list_migration_candidates(primary_svc, folder_ids)

        # Resolve full Drive path for every candidate
        folder_cache: dict = {}
        for f in files:
            f["location"] = _resolve_path(primary_svc, f, folder_cache)

        logger.info(f"Candidates returned to dashboard: {len(files)} files")
        return jsonify({"files": files, "count": len(files)})
    except Exception as e:
        _log_exc("/api/candidates", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/migrate", methods=["POST"])
def api_migrate():
    """
    Start migration in a background thread.
    Body: {"folder_ids": ["id1", ...]}
    """
    global _migration_running, _migration_queue, _pending_deletes

    if _migration_running:
        return jsonify({"error": "Migration already running"}), 409

    folder_ids = (request.json or {}).get("folder_ids", [])

    # Clear queue and pending deletes
    while not _migration_queue.empty():
        _migration_queue.get_nowait()
    _pending_deletes.clear()

    def progress_cb(event: dict):
        if event.get("type") == "confirm_delete":
            _pending_deletes[event["file_id"]] = {
                "file_id": event["file_id"],
                "name":    event["file"],
            }
        _migration_queue.put(event)

    def run_thread():
        global _migration_running
        _migration_running = True
        try:
            primary_svc, secondary_svc = _get_services()
            run_migration(
                primary_svc,
                secondary_svc,
                source_folder_ids=folder_ids,
                progress_callback=progress_cb,
            )
        except Exception as e:
            _log_exc("run_migration", e)
            _migration_queue.put({"type": "error", "error": str(e)})
        finally:
            _migration_running = False
            _migration_queue.put({"type": "stream_end"})

    threading.Thread(target=run_thread, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/progress")
def api_progress():
    """
    Server-Sent Events stream of migration progress.
    The dashboard JS listens here for live updates.
    """
    def generate():
        while True:
            try:
                event = _migration_queue.get(timeout=30)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("stream_end", "done"):
                    break
            except queue.Empty:
                yield "data: {\"type\": \"heartbeat\"}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/confirm-delete", methods=["POST"])
def api_confirm_delete():
    """
    Confirm or skip deletion of a file from the primary account.
    Body: {"file_id": "...", "confirm": true/false}
    """
    file_id = request.json.get("file_id")
    confirm = request.json.get("confirm", False)

    if file_id not in _pending_deletes:
        return jsonify({"error": "File not pending delete"}), 404

    info = _pending_deletes.pop(file_id)

    if confirm:
        try:
            # Create a fresh service so this request doesn't share the httplib2.Http
            # instance that the migration thread may be using concurrently.
            delete_svc = get_drive_service(config.PRIMARY_ACCOUNT)
            delete_svc.files().delete(fileId=file_id).execute(num_retries=5)
            logger.info(f"DELETED from primary: {info['name']}")
            _migration_queue.put({"type": "deleted", "file": info["name"]})
            return jsonify({"deleted": True})
        except DriveHttpError as e:
            if getattr(e, "status_code", None) == 404:
                # File already gone from primary — treat as success.
                logger.info(f"ALREADY GONE from primary (skipping delete): {info['name']}")
                _migration_queue.put({"type": "deleted", "file": info["name"]})
                return jsonify({"deleted": True, "note": "already removed"})
            logger.error(f"DELETE FAILED: {info['name']} - {e}")
            return jsonify({"error": str(e)}), 500
        except Exception as e:
            logger.error(f"DELETE FAILED: {info['name']} - {e}")
            return jsonify({"error": str(e)}), 500
    else:
        # Remember this choice — don't suggest the file for migration again.
        # Lock prevents parallel requests from racing on read-modify-write.
        with _kept_files_lock:
            kept = _load_kept_files()
            kept[file_id] = {
                "name":    info["name"],
                "kept_at": datetime.datetime.now().isoformat(),
            }
            _save_kept_files(kept)
        logger.info(f"KEPT in primary (will not suggest again): {info['name']}")
        _migration_queue.put({"type": "delete_skipped", "file": info["name"]})
        return jsonify({"deleted": False})


@app.route("/api/kept-files")
def api_kept_files():
    """List all files the user chose to keep in primary."""
    return jsonify({"files": _load_kept_files()})


@app.route("/api/kept-files/remove", methods=["POST"])
def api_remove_kept_file():
    """Remove one entry from the kept list so it will be suggested again."""
    file_id = request.json.get("file_id")
    if not file_id:
        return jsonify({"error": "file_id required"}), 400
    with _kept_files_lock:
        kept = _load_kept_files()
        removed = kept.pop(file_id, None)
        if removed:
            _save_kept_files(kept)
    if removed:
        name = removed.get("name", file_id)
        logger.info(f"REMOVED from kept list: {name}")
        return jsonify({"removed": True, "name": name})
    return jsonify({"removed": False}), 404


@app.route("/api/kept-files/clear", methods=["POST"])
def api_clear_kept_files():
    """Remove all entries from the kept list."""
    with _kept_files_lock:
        kept = _load_kept_files()
        count = len(kept)
        _save_kept_files({})
    logger.info(f"CLEARED kept list ({count} entries removed)")
    return jsonify({"cleared": count})


@app.route("/api/config")
def api_config():
    """Return safe subset of config for the dashboard."""
    return jsonify({
        "primary_account":        config.PRIMARY_ACCOUNT,
        "secondary_account":      config.SECONDARY_ACCOUNT,
        "threshold":              config.STORAGE_THRESHOLD_PERCENT,
        "local_backup_dir":       getattr(config, "LOCAL_BACKUP_DIR", ""),
        "confirm_before_delete":  getattr(config, "CONFIRM_BEFORE_DELETE", False),
        "delete_after_migrate":   config.DELETE_AFTER_MIGRATE,
        "dry_run":                config.DRY_RUN,
        "destination_folder":     config.DESTINATION_FOLDER_NAME,
        "min_age_days":           config.MIN_AGE_DAYS,
        "max_files_per_run":      config.MAX_FILES_PER_RUN,
        "max_mb_per_run":         config.MAX_MB_PER_RUN,
    })


@app.route("/api/log")
def api_log():
    """Return last N lines of the migration log."""
    log_path = Path(config.LOG_FILE)
    if not log_path.exists():
        return jsonify({"lines": []})
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return jsonify({"lines": lines[-200:]})


# ── Static file serving (dashboard HTML) ─────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(".", "dashboard.html")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = getattr(config, "DASHBOARD_PORT", 5000)
    print("""
╔══════════════════════════════════════════════════════╗
║   Google Drive Manager  —  Web Dashboard             ║
╚══════════════════════════════════════════════════════╝
""")

    # Pre-authorise both accounts before starting Flask.
    # If a saved token exists it is reused silently; if not, the auth URL is
    # printed here so the user can copy/paste it into the correct browser tab.
    for account in [config.PRIMARY_ACCOUNT, config.SECONDARY_ACCOUNT]:
        print(f"  Checking auth for: {account}")
        try:
            get_credentials(account)
            print(f"  ✅ Token OK: {account}\n")
        except Exception as exc:
            print(f"  ❌ Auth failed for {account}: {exc}\n")

    print(f"  Dashboard URL:  http://localhost:{port}")
    print("  Press Ctrl+C to stop the server.\n")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
