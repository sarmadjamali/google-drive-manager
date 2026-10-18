"""
Storage inspection and file listing utilities.
Uses only the official Google Drive API — no third-party services.

Special sentinel IDs used by server.py / dashboard:
  "__PHOTOS__"  -> scan the entire Google Photos space (all photos/videos)
"""

import json
import logging
from pathlib import Path
from typing import Optional
import config

logger = logging.getLogger("gdrive_manager")

try:
    from googleapiclient.errors import HttpError
except Exception:  # pragma: no cover
    HttpError = Exception

# Sentinel used to represent "scan Google Photos" without a real folder ID
PHOTOS_SENTINEL = "__PHOTOS__"


def _is_scope_error(err) -> bool:
    """
    True when the error is Google's 'insufficient scopes for spaces=photos'.
    Happens when the saved OAuth token predates the drive.photos.readonly scope.
    """
    msg = str(err).lower()
    if "insufficientscopes" in msg or "scopes do not give access" in msg:
        return True
    # 401 always means auth/scope problem; 403 only when it's a scope message
    if isinstance(err, HttpError):
        status = getattr(getattr(err, "resp", None), "status", None)
        if status == 401:
            return True
        if status == 403:
            human = (getattr(err, "reason", "") or "").lower()
            return "scope" in human or "scope" in msg
    return False


# Number of automatic retries for transient network/SSL errors on every
# Google API call. httplib2 retries socket.error / ssl.SSLError when > 0.
API_RETRIES = 5


def get_storage_info(service) -> dict:
    about = service.about().get(fields="storageQuota").execute(num_retries=API_RETRIES)
    quota   = about.get("storageQuota", {})
    total   = int(quota.get("limit", 0))
    used    = int(quota.get("usage", 0))
    in_drive = int(quota.get("usageInDrive", 0))       # Drive files (excl. trash)
    in_trash = int(quota.get("usageInDriveTrash", 0))  # trashed Drive files
    free    = total - used
    pct     = round((used / total * 100), 2) if total > 0 else 0
    # "other" ≈ Gmail + Google Photos (both count but aren't split by the API)
    other   = used - in_drive - in_trash
    return {
        "total":          total,
        "used":           used,
        "free":           free,
        "percent_used":   pct,
        "total_gb":       round(total    / 1e9, 2),
        "used_gb":        round(used     / 1e9, 2),
        "free_gb":        round(free     / 1e9, 2),
        "drive_gb":       round(in_drive / 1e9, 2),
        "trash_gb":       round(in_trash / 1e9, 2),
        "other_gb":       round(other    / 1e9, 2),  # Gmail + Photos combined
    }


def _fmt(bytes_val: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if bytes_val < 1024:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} TB"


def print_storage_summary(email: str, info: dict) -> None:
    bar_len   = 30
    filled    = int(bar_len * info["percent_used"] / 100)
    bar_color = "\U0001f7e5" if info["percent_used"] >= 80 else ("\U0001f7e8" if info["percent_used"] >= 60 else "\U0001f7e9")
    bar       = bar_color * filled + "\u2b1c" * (bar_len - filled)
    print(f"\n  \U0001f4e7  {email}")
    print(f"  {bar}  {info['percent_used']}%")
    print(f"  Used: {info['used_gb']} GB / {info['total_gb']} GB  |  Free: {info['free_gb']} GB")


def list_all_folders(service) -> list:
    """Return all top-level folders in the user's My Drive."""
    folders = []
    page_token = None
    while True:
        resp = service.files().list(
            q="mimeType = 'application/vnd.google-apps.folder' and trashed = false and 'root' in parents",
            pageSize=100,
            fields="nextPageToken, files(id, name)",
            orderBy="name asc",
            pageToken=page_token,
        ).execute(num_retries=API_RETRIES)
        folders.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return folders


def get_photos_summary(service) -> dict:
    """
    Quick count + total size of photos/videos in Google Photos.
    Uses spaces=photos via Drive API — no third-party service.
    """
    total_bytes = 0
    count = 0
    page_token = None
    try:
        while True:
            resp = service.files().list(
                spaces="photos",
                pageSize=100,
                fields="nextPageToken, files(id, size)",
                pageToken=page_token,
            ).execute(num_retries=API_RETRIES)
            for f in resp.get("files", []):
                total_bytes += int(f.get("size", 0))
                count += 1
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except Exception as err:
        if _is_scope_error(err):
            # Token lacks drive.photos.readonly — report as unavailable, don't crash.
            return {"count": 0, "total_bytes": 0, "scope_error": True}
        raise
    return {"count": count, "total_bytes": total_bytes}


def _expand_folder_tree(service, folder_ids: list) -> list:
    """
    Given a list of folder IDs, return them plus every descendant folder ID
    (BFS). Each level is fetched in batches of 30 to keep the query short.
    Returned list may contain duplicates — the caller deduplicates via a set
    or by querying each folder separately.
    """
    expanded: set = set(folder_ids)
    frontier: list = list(folder_ids)
    while frontier:
        batch, frontier = frontier[:30], frontier[30:]
        parents_q = " or ".join(f"'{fid}' in parents" for fid in batch)
        q = (f"mimeType = 'application/vnd.google-apps.folder' "
             f"and trashed = false and ({parents_q})")
        page_token = None
        while True:
            resp = service.files().list(
                q=q,
                fields="nextPageToken, files(id)",
                pageSize=1000,
                pageToken=page_token,
            ).execute(num_retries=API_RETRIES)
            new_ids = [f["id"] for f in resp.get("files", []) if f["id"] not in expanded]
            expanded.update(new_ids)
            frontier.extend(new_ids)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    return list(expanded)


def list_migration_candidates(
    service,
    source_folder_ids: Optional[list] = None,
) -> list:
    """
    List files that are candidates for migration.
    source_folder_ids may include PHOTOS_SENTINEL to scan Google Photos.
    Empty / None = scan entire My Drive only.
    """
    import datetime

    cutoff = (
        datetime.datetime.utcnow()
        - datetime.timedelta(days=config.MIN_AGE_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    all_files = []
    ids = source_folder_ids or []
    scan_photos = PHOTOS_SENTINEL in ids
    real_ids    = [i for i in ids if i != PHOTOS_SENTINEL]

    # When nothing specific is selected → scan all My Drive AND Google Photos.
    # Photos are often the largest space consumer and are otherwise invisible.
    if not real_ids and not scan_photos:
        scan_photos = True

    # ── My Drive folders ──────────────────────────────────────────────────────
    # real_ids empty → [None] (no parent filter) = full My Drive scan.
    # When specific folders are selected, expand them recursively so files nested
    # inside subfolders are also returned (Drive API only matches direct children
    # with the '{id}' in parents filter).
    if real_ids:
        drive_targets = _expand_folder_tree(service, real_ids)
        logger.info(f"Folder selection: {len(real_ids)} chosen → "
                    f"{len(drive_targets)} total (including subfolders)")
    else:
        drive_targets = [None]

    for folder_id in drive_targets:
        q_parts = [
            "trashed = false",
            "mimeType != 'application/vnd.google-apps.folder'",
            f"createdTime < '{cutoff}'",
            "'me' in owners",   # only files you OWN can be downloaded + deleted
        ]
        if folder_id:
            q_parts.append(f"'{folder_id}' in parents")
        if config.ALLOWED_MIME_TYPES:
            mime_conditions = " or ".join(
                f"mimeType contains '{m}'" for m in config.ALLOWED_MIME_TYPES
            )
            q_parts.append(f"({mime_conditions})")
        if config.MIN_FILE_SIZE_BYTES > 0:
            q_parts.append(f"size > {config.MIN_FILE_SIZE_BYTES}")

        query = " and ".join(q_parts)
        page_token = None
        while True:
            resp = service.files().list(
                q=query,
                spaces="drive",
                pageSize=100,
                fields="nextPageToken, files(id, name, size, mimeType, createdTime, modifiedTime, parents)",
                orderBy="createdTime asc",
                pageToken=page_token,
            ).execute(num_retries=API_RETRIES)
            all_files.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    # ── Google Photos space ───────────────────────────────────────────────────
    if scan_photos:
        q_parts = [f"createdTime < '{cutoff}'"]
        if config.MIN_FILE_SIZE_BYTES > 0:
            q_parts.append(f"size > {config.MIN_FILE_SIZE_BYTES}")
        if config.ALLOWED_MIME_TYPES:
            mime_conditions = " or ".join(
                f"mimeType contains '{m}'" for m in config.ALLOWED_MIME_TYPES
            )
            q_parts.append(f"({mime_conditions})")

        query = " and ".join(q_parts)
        page_token = None
        photos_count_before = len(all_files)
        try:
            while True:
                resp = service.files().list(
                    q=query,
                    spaces="photos",
                    pageSize=100,
                    fields="nextPageToken, files(id, name, size, mimeType, createdTime, modifiedTime, parents)",
                    pageToken=page_token,
                ).execute(num_retries=API_RETRIES)
                photos = resp.get("files", [])
                for f in photos:
                    f["_source"] = "photos"  # tag so path resolver shows correct location
                all_files.extend(photos)
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
            photos_found = len(all_files) - photos_count_before
            logger.info(f"Google Photos scan: {photos_found} photos older than {config.MIN_AGE_DAYS} days")
        except Exception as err:
            if _is_scope_error(err):
                logger.warning(
                    "Google Photos scan skipped — token is missing the "
                    "drive.photos.readonly scope. "
                    "Run:  python reset_auth.py  then  python server.py  "
                    "to re-authorise and unlock Photos scanning."
                )
            else:
                raise

    logger.info(f"Candidate scan: {len(all_files)} raw files from Drive API "
                f"(drive={'all' if not real_ids else real_ids}, photos={scan_photos})")

    # ── Drop Google-native formats (can never be downloaded as binary) ────────
    # mimeType contains / not-contains is unreliable in the Drive query
    # language for non-name fields, so filter in Python instead.
    pre_workspace = len(all_files)
    all_files = [
        f for f in all_files
        if not f.get("mimeType", "").startswith("application/vnd.google-apps.")
    ]
    logger.info(f"After workspace filter: {len(all_files)} "
                f"(dropped {pre_workspace - len(all_files)} workspace files)")

    # ── Filter out files the user chose to keep in primary ───────────────────
    kept_ids: set = set()
    kept_path = Path(getattr(config, "KEPT_FILES_FILE", "kept_files.json"))
    if kept_path.exists():
        try:
            kept_ids = set(json.loads(kept_path.read_text(encoding="utf-8")).keys())
        except Exception:
            logger.warning("Could not read kept_files.json — no kept-file filtering applied")

    # ── Deduplicate ───────────────────────────────────────────────────────────
    seen = set()
    files = []
    for f in all_files:
        if f["id"] not in seen and f["id"] not in kept_ids:
            seen.add(f["id"])
            files.append(f)
    logger.info(f"After kept filter ({len(kept_ids)} kept): {len(files)} candidates")

    files.sort(key=lambda f: f.get("createdTime", ""))

    if config.MAX_FILES_PER_RUN > 0:
        files = files[: config.MAX_FILES_PER_RUN]

    if config.MAX_MB_PER_RUN > 0:
        cap   = config.MAX_MB_PER_RUN * 1024 * 1024
        total = 0
        kept  = []
        for f in files:
            sz = int(f.get("size", 0))
            if total + sz > cap:
                break
            kept.append(f)
            total += sz
        files = kept

    return files


def interactive_folder_picker(service) -> list:
    print("\n\U0001f4c2  Fetching your Google Drive folders...\n")
    folders = list_all_folders(service)
    items = [{"id": PHOTOS_SENTINEL, "name": "Google Photos (all photos & videos)"}] + folders

    print("  \u250c" + "\u2500"*44 + "\u2510")
    print("  \u2502   Your Google Drive Sources               \u2502")
    print("  \u251c" + "\u2500"*6 + "\u252c" + "\u2500"*37 + "\u2524")
    for i, f in enumerate(items, 1):
        print(f"  \u2502  {str(i).rjust(2)}  \u2502  {f['name']:<33}  \u2502")
    print("  \u2514" + "\u2500"*6 + "\u2534" + "\u2500"*37 + "\u2518")

    print("\n  Enter numbers to migrate (comma-separated).")
    print("  Example: 1,3,5   or   all   or   skip\n")

    while True:
        choice = input("  Your choice: ").strip().lower()
        if choice == "skip":
            return []
        if choice == "all":
            return [f["id"] for f in items]
        try:
            indices = [int(x.strip()) for x in choice.split(",")]
            selected = [items[i - 1] for i in indices if 1 <= i <= len(items)]
            if selected:
                print(f"\n  \u2705  Selected: {', '.join(f['name'] for f in selected)}\n")
                return [f["id"] for f in selected]
        except (ValueError, IndexError):
            pass
        print("  \u274c  Invalid input.")


def find_folder_id(service, folder_name: str) -> Optional[str]:
    resp = service.files().list(
        q=(
            f"name = '{folder_name}' "
            "and mimeType = 'application/vnd.google-apps.folder' "
            "and trashed = false"
        ),
        fields="files(id, name)",
        pageSize=1,
    ).execute(num_retries=API_RETRIES)
    items = resp.get("files", [])
    return items[0]["id"] if items else None


def get_or_create_folder(service, folder_name: str) -> str:
    fid = find_folder_id(service, folder_name)
    if fid:
        return fid
    meta = {
        "name":     folder_name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    folder = service.files().create(body=meta, fields="id").execute(num_retries=API_RETRIES)
    print(f"  \U0001f4c1 Created folder '{folder_name}' in destination account.")
    return folder["id"]
