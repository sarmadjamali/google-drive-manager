"""
Core migration logic: download from primary account, upload to secondary.
All transfers happen through your local machine — no third-party services.
"""

import io
import os
import logging
import datetime
from pathlib import Path
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from googleapiclient.errors import HttpError
from tqdm import tqdm

import config
from storage import list_migration_candidates, get_or_create_folder

logger = logging.getLogger("gdrive_manager")

# Automatic retries for transient network / SSL errors on Google API calls.
API_RETRIES = 5


def _setup_logging():
    log = logging.getLogger("gdrive_manager")
    if log.handlers:
        return  # already configured
    log.setLevel(logging.INFO)
    log.propagate = False  # keep Flask/Werkzeug HTTP logs out of migration.log
    fmt = logging.Formatter("%(asctime)s  %(levelname)s  %(message)s")
    fh = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)


def _download_file(primary_service, file_id: str, file_name: str) -> io.BytesIO:
    """Download a file from Google Drive into memory."""
    buffer = io.BytesIO()
    request = primary_service.files().get_media(fileId=file_id)
    downloader = MediaIoBaseDownload(buffer, request, chunksize=10 * 1024 * 1024)

    done = False
    with tqdm(total=100, desc=f"  down {file_name[:40]}", unit="%", leave=False) as pbar:
        last = 0
        while not done:
            status, done = downloader.next_chunk(num_retries=API_RETRIES)
            if status:
                prog = int(status.progress() * 100)
                pbar.update(prog - last)
                last = prog
    buffer.seek(0)
    return buffer


def _get_file_path_parts(primary_service, file_meta: dict, folder_cache: dict) -> list:
    """
    Walk the parent chain and return a list of folder names from just below
    'My Drive' down to the file's immediate parent.
    E.g. a file in My Drive/Photos/2023/ returns ["Photos", "2023"].
    Returns ["Google Photos"] for photos-space files.
    Falls back to [] (root) on any API error.
    """
    if file_meta.get("_source") == "photos":
        return ["Google Photos"]

    parts = []
    parent_ids = file_meta.get("parents", [])
    current_id = parent_ids[0] if parent_ids else None

    while current_id:
        if current_id in folder_cache:
            node = folder_cache[current_id]
        else:
            try:
                node = primary_service.files().get(
                    fileId=current_id,
                    fields="id, name, parents",
                ).execute(num_retries=API_RETRIES)
                folder_cache[current_id] = node
            except Exception:
                break

        name = node.get("name", "")
        if name == "My Drive" or not node.get("parents"):
            break  # reached root — stop without including "My Drive"
        parts.append(name)
        p = node.get("parents", [])
        current_id = p[0] if p else None

    parts.reverse()
    return parts  # e.g. ["Photos", "2023"]


def _get_or_create_nested_folder(
    secondary_service, root_folder_id: str, path_parts: list, folder_cache: dict
) -> str:
    """
    Walk (and create as needed) nested folders in the secondary account
    under root_folder_id.  Returns the ID of the deepest folder.
    """
    current_id = root_folder_id
    for part in path_parts:
        part = part.strip()   # normalise: Drive allows trailing spaces, filesystems don't
        if not part:
            continue
        cache_key = f"{current_id}/{part}"
        if cache_key in folder_cache:
            current_id = folder_cache[cache_key]
            continue

        safe_name = part.replace("\\", "\\\\").replace("'", "\\'")
        resp = secondary_service.files().list(
            q=(
                f"name = '{safe_name}' "
                f"and '{current_id}' in parents "
                "and mimeType = 'application/vnd.google-apps.folder' "
                "and trashed = false"
            ),
            fields="files(id)",
            pageSize=1,
        ).execute(num_retries=API_RETRIES)
        hits = resp.get("files", [])
        if hits:
            folder_id = hits[0]["id"]
        else:
            meta = {
                "name": part,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [current_id],
            }
            folder_id = secondary_service.files().create(
                body=meta, fields="id"
            ).execute(num_retries=API_RETRIES)["id"]

        folder_cache[cache_key] = folder_id
        current_id = folder_id

    return current_id


def _save_local_backup(buffer: io.BytesIO, file_name: str, path_parts: list = None) -> str:
    """
    Save the file buffer to LOCAL_BACKUP_DIR on disk, mirroring the Drive
    folder structure when path_parts is provided.
    Returns the path it was saved to, or empty string if backup is disabled.
    """
    backup_dir = getattr(config, "LOCAL_BACKUP_DIR", "")
    if not backup_dir:
        return ""

    dest_dir = Path(backup_dir)
    if path_parts and getattr(config, "PRESERVE_FOLDER_STRUCTURE", True):
        for part in path_parts:
            # Strip surrounding whitespace — Windows silently strips trailing spaces
            # from directory names, causing ENOENT when the path string still has them.
            safe_part = part.strip().replace("/", "_").replace("\\", "_")
            if safe_part:
                dest_dir = dest_dir / safe_part
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Avoid overwriting: add timestamp suffix if file already exists
    safe_name = file_name.replace("/", "_").replace("\\", "_")
    dest_path = dest_dir / safe_name
    if dest_path.exists():
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dest_path = dest_dir / f"{dest_path.stem}_{ts}{dest_path.suffix}"

    buffer.seek(0)
    dest_path.write_bytes(buffer.read())
    buffer.seek(0)

    return str(dest_path)


def _find_existing_in_dest(secondary_service, file_name: str, file_size: int,
                           dest_folder_id: str):
    """
    Return the ID of an existing file in the destination folder that matches
    this file's name (and size, when both are known) — so we never create a
    duplicate copy. Returns None if no match is found.
    """
    # Escape backslashes and single quotes for the Drive query language
    safe_name = file_name.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"name = '{safe_name}' "
        f"and '{dest_folder_id}' in parents "
        "and trashed = false"
    )
    try:
        resp = secondary_service.files().list(
            q=query,
            spaces="drive",
            fields="files(id, name, size)",
            pageSize=10,
        ).execute(num_retries=API_RETRIES)
    except Exception as e:
        # If the lookup fails, don't block the migration — just report no match.
        logger.warning(f"Duplicate check failed for {file_name}: {e}")
        return None

    for f in resp.get("files", []):
        existing_size = int(f.get("size", 0))
        # Treat as duplicate when the size matches, or when size is unknown
        # on either side (some items don't report a size).
        if file_size == 0 or existing_size == 0 or existing_size == file_size:
            return f["id"]
    return None


def _upload_file(
    secondary_service,
    buffer: io.BytesIO,
    file_name: str,
    mime_type: str,
    dest_folder_id: str,
) -> str:
    """Upload a file buffer to Google Drive and return the new file ID."""
    buffer.seek(0)
    meta = {"name": file_name, "parents": [dest_folder_id]}
    media = MediaIoBaseUpload(buffer, mimetype=mime_type, chunksize=10 * 1024 * 1024, resumable=True)

    request = secondary_service.files().create(
        body=meta, media_body=media, fields="id"
    )
    request.num_retries = API_RETRIES

    response = None
    with tqdm(total=100, desc=f"  up {file_name[:40]}", unit="%", leave=False) as pbar:
        last = 0
        while response is None:
            status, response = request.next_chunk(num_retries=API_RETRIES)
            if status:
                prog = int(status.progress() * 100)
                pbar.update(prog - last)
                last = prog
        pbar.update(100 - last)

    return response.get("id")


def run_migration(
    primary_service,
    secondary_service,
    source_folder_ids: list = None,
    progress_callback=None,
) -> dict:
    """
    Main migration routine.
    source_folder_ids : list of Drive folder IDs to migrate from (None = entire Drive)
    progress_callback : optional callable(event_dict) for web server progress streaming
    Returns a summary dict with counts and bytes moved.
    """
    _setup_logging()

    def emit(event: dict):
        if progress_callback:
            progress_callback(event)

    summary = {
        "timestamp":         datetime.datetime.now().isoformat(),
        "files_migrated":    0,
        "files_failed":      0,
        "files_duplicate":   0,
        "files_not_owned":   0,
        "bytes_moved":       0,
        "skipped_dry_run":   0,
        "log":               [],
    }

    # ── 1. Scan for candidates ────────────────────────────────────────────────
    emit({"type": "status", "msg": "Scanning primary account for migration candidates..."})
    print("\nScanning primary account for migration candidates...")
    candidates = list_migration_candidates(primary_service, source_folder_ids or [])

    if not candidates:
        msg = "Nothing to migrate (no files match the criteria)."
        print(f"  {msg}")
        summary["log"].append(msg)
        emit({"type": "done", "summary": summary})
        return summary

    total_size = sum(int(f.get("size", 0)) for f in candidates)
    print(f"  Found {len(candidates)} file(s)  ({total_size / 1e6:.1f} MB total)\n")
    emit({"type": "candidates", "count": len(candidates), "total_mb": round(total_size / 1e6, 1)})

    # ── 2. Dry-run short-circuit ──────────────────────────────────────────────
    if config.DRY_RUN:
        print("  DRY RUN — no changes will be made.\n")
        for f in candidates:
            sz = int(f.get("size", 0))
            print(f"    WOULD migrate: {f['name']}  ({sz / 1e6:.2f} MB)  [{f['createdTime'][:10]}]")
        summary["skipped_dry_run"] = len(candidates)
        emit({"type": "done", "summary": summary})
        return summary

    # ── 3. Ensure destination folder in secondary account ────────────────────
    dest_root_folder_id = get_or_create_folder(secondary_service, config.DESTINATION_FOLDER_NAME)

    skip_dupes              = getattr(config, "SKIP_DUPLICATES", True)
    preserve_structure      = getattr(config, "PRESERVE_FOLDER_STRUCTURE", True)
    primary_folder_cache    = {}   # fileId → node  (for path resolution in primary)
    secondary_folder_cache  = {}   # "parentId/name" → folderId  (for nested creation)

    # ── 4. Migrate each file ─────────────────────────────────────────────────
    for idx, file in enumerate(candidates, 1):
        file_id   = file["id"]
        file_name = file["name"]
        mime_type = file.get("mimeType", "application/octet-stream")
        file_size = int(file.get("size", 0))

        # Skip Google Workspace formats (can't be downloaded as binary)
        if mime_type.startswith("application/vnd.google-apps."):
            msg = f"SKIPPED (Google Workspace format): {file_name}"
            logger.info(msg)
            summary["log"].append(msg)
            emit({"type": "skip", "file": file_name, "reason": "Google Workspace format"})
            continue

        # ── Resolve folder path for structure preservation ───────────────────
        path_parts: list = []
        if preserve_structure:
            try:
                path_parts = _get_file_path_parts(primary_service, file, primary_folder_cache)
            except Exception:
                path_parts = []

        # Determine (or create) the target folder in secondary
        try:
            if preserve_structure and path_parts:
                dest_folder_id = _get_or_create_nested_folder(
                    secondary_service, dest_root_folder_id, path_parts, secondary_folder_cache
                )
            else:
                dest_folder_id = dest_root_folder_id
        except Exception:
            dest_folder_id = dest_root_folder_id  # fall back to root on error

        print(f"\n[{idx}/{len(candidates)}]  {file_name}  ({file_size / 1e6:.2f} MB)")
        emit({
            "type":  "file_start",
            "index": idx,
            "total": len(candidates),
            "name":  file_name,
            "size_mb": round(file_size / 1e6, 2),
        })

        try:
            # ── Duplicate guard: already in the destination? ─────────────────
            already_present = False
            if skip_dupes:
                existing_id = _find_existing_in_dest(
                    secondary_service, file_name, file_size, dest_folder_id
                )
                if existing_id:
                    already_present = True
                    msg = f"DUPLICATE (already in destination, not re-uploaded): {file_name}"
                    logger.info(msg)
                    summary["log"].append(msg)
                    summary["files_duplicate"] += 1
                    print(f"  Already in destination — skipping upload.")
                    emit({
                        "type":   "skip",
                        "file":   file_name,
                        "reason": "already in destination (no duplicate created)",
                    })

            if not already_present:
                # ── Download into memory ─────────────────────────────────────
                emit({"type": "progress", "step": "downloading", "file": file_name})
                buffer = _download_file(primary_service, file_id, file_name)

                # ── Save local backup FIRST (your safety net) ────────────────
                backup_path = _save_local_backup(buffer, file_name, path_parts)
                if backup_path:
                    print(f"  Saved locally: {backup_path}")
                    logger.info(f"LOCAL BACKUP: {file_name} -> {backup_path}")
                    emit({"type": "backup", "file": file_name, "path": backup_path})

                # ── Upload to secondary ──────────────────────────────────────
                emit({"type": "progress", "step": "uploading", "file": file_name})
                new_id = _upload_file(secondary_service, buffer, file_name, mime_type, dest_folder_id)
                logger.info(f"UPLOADED: {file_name} -> {new_id}")
                print(f"  Uploaded to secondary account.")
                emit({"type": "uploaded", "file": file_name, "new_id": new_id})

                summary["bytes_moved"] += file_size

            # ── Delete from primary (with optional confirmation) ──────────────
            # Only reached once the file is safely in the secondary account
            # (either just uploaded, or already present as a prior copy).
            if config.DELETE_AFTER_MIGRATE:
                do_delete = True

                confirm_enabled = getattr(config, "CONFIRM_BEFORE_DELETE", False)
                if confirm_enabled and progress_callback is None:
                    # Interactive CMD mode: ask the user
                    ans = input(
                        f"\n  Delete '{file_name}' from primary account? [Y/n]: "
                    ).strip().lower()
                    do_delete = ans in ("", "y", "yes")

                elif confirm_enabled and progress_callback is not None:
                    # Web mode: emit a confirm event; the dashboard asks you,
                    # and /api/confirm-delete performs the actual deletion.
                    emit({
                        "type":    "confirm_delete",
                        "file_id": file_id,
                        "file":    file_name,
                    })
                    do_delete = False   # never delete here in web mode

                if do_delete:
                    primary_service.files().delete(fileId=file_id).execute(num_retries=API_RETRIES)
                    logger.info(f"DELETED from primary: {file_name}")
                    print(f"  Deleted from primary account.")
                    emit({"type": "deleted", "file": file_name})

            if not already_present:
                summary["files_migrated"] += 1
                summary["log"].append(f"OK: {file_name} ({file_size / 1e6:.2f} MB)")

        except HttpError as e:
            # Files the account does not own can't be downloaded or deleted by
            # any app — Google returns "insufficientFilePermissions". That's not
            # a failure of this tool, so report it as a clean skip, not an error.
            reason_code = ""
            try:
                details = getattr(e, "error_details", None) or []
                if isinstance(details, list) and details and isinstance(details[0], dict):
                    reason_code = details[0].get("reason", "")
            except Exception:
                reason_code = ""

            human_reason = getattr(e, "reason", "") or ""
            perm_denied = (
                reason_code == "insufficientFilePermissions"
                or "insufficientFilePermissions" in str(e)
                or "does not have sufficient permissions" in human_reason.lower()
                or "does not have sufficient permissions" in str(e).lower()
            )

            if perm_denied:
                msg = f"SKIPPED (not owned by your account — cannot be migrated): {file_name}"
                logger.info(msg)
                summary["files_not_owned"] += 1
                summary["log"].append(msg)
                emit({"type": "skip", "file": file_name, "reason": "not owned by your account"})
            else:
                logger.error(f"FAILED: {file_name} - {e}")
                summary["files_failed"] += 1
                summary["log"].append(f"FAILED: {file_name} - {e}")
                emit({"type": "error", "file": file_name, "error": str(e)})

    print(
        f"\nMigration complete: "
        f"{summary['files_migrated']} moved, "
        f"{summary['files_duplicate']} already existed, "
        f"{summary['files_not_owned']} not owned (skipped), "
        f"{summary['files_failed']} failed, "
        f"{summary['bytes_moved'] / 1e6:.1f} MB transferred."
    )
    emit({"type": "done", "summary": summary})
    return summary
