# CLAUDE.md — Google Drive Storage Manager

Project context and working guidelines for Claude Code (and future me).
Read this before changing anything in this folder.

---

## What this project is

A fully automated, **local** Google Drive storage manager. When the primary
Google account gets close to full, it moves older files to a secondary account
to free space — downloading each file through this machine, saving a local
backup, uploading it to the secondary account, and (only after confirmation)
deleting it from the primary.

- **Primary account:**   the account that fills up   (set in `config.py`)
- **Secondary account:** the overflow / backup target (set in `config.py`)
- Runs locally on Windows at `G:\work\google drive manager`.
- Two ways to run: a **web dashboard** (`server.py`) or **CLI** (`main.py`).

## HARD CONSTRAINT — read first, never violate

> **Only Google's official Drive API may be used. NO third-party apps,
> services, or SaaS of any kind — the data is sensitive.**

Everything happens on this machine and talks directly to Google's API over
OAuth. Do not add any external uploader, sync service, cloud function, paste
service, telemetry, analytics, or "helper" API. If a feature seems to need a
third-party service, stop and find a way to do it with the Google API alone or
on-device. This constraint overrides convenience, every time.

## Safety rules (these protect the user's data)

1. **Never delete before a confirmed upload.** A file is removed from primary
   only after it is verified present in secondary (just uploaded, or already
   there). The delete step lives inside the per-file `try`, after upload.
2. **Local backup first.** Every migrated file is written to `LOCAL_BACKUP_DIR`
   *before* upload/delete. This is the user's safety net — keep it.
3. **Confirm before delete.** `CONFIRM_BEFORE_DELETE = True`. In the web UI the
   backend emits a `confirm_delete` event and only `/api/confirm-delete`
   performs the deletion. In CLI it prompts `[Y/n]`. Never auto-delete silently.
4. **No duplicates.** Before uploading, check the destination folder for a file
   with the same name + size (`_find_existing_in_dest`); skip the upload if
   found. `SKIP_DUPLICATES = True`.
5. **Owned files only.** The candidate scanner uses `'me' in owners` so files
   shared in by other accounts are never attempted (they can't be downloaded or
   deleted anyway — see Known issues).

## File map

| File              | Role |
|-------------------|------|
| `config.py`       | All settings. Start here to change behavior. |
| `auth.py`         | OAuth2 browser flow; stores tokens in `tokens/`. |
| `storage.py`      | Storage info, folder/Photos listing, candidate selection. |
| `migrate.py`      | Core move logic: download → local backup → upload → confirm-delete. |
| `server.py`       | Flask web dashboard + SSE progress stream + REST API. |
| `dashboard.html`  | 3-column UI: Sources \| File browser \| Selected files. |
| `main.py`         | CLI entry point (interactive + `--schedule`). |
| `reset_auth.py`   | Deletes saved tokens so the next run re-authorizes with current scopes. |
| `tokens/`         | Local OAuth tokens (one JSON per account). Never commit/share. |
| `credentials.json`| Google Cloud OAuth client secret. Never commit/share. |
| `migration.log`   | Run log (UTF-8). Delete it to clear the dashboard log panel. |

## OAuth scopes (auth.py `SCOPES`)

```
https://www.googleapis.com/auth/drive                    # read/write/delete files
https://www.googleapis.com/auth/drive.metadata.readonly  # metadata
https://www.googleapis.com/auth/drive.photos.readonly    # Google Photos space
```

**If you change `SCOPES`, existing tokens become invalid for the new scope.**
Run `python reset_auth.py` then `python server.py` to re-authorize both
accounts in the browser. Symptom of a stale token: `HttpError 403
insufficientScopes` / "granted scopes do not give access to all requested
spaces".

## Config reference (config.py)

| Setting | Meaning |
|---------|---------|
| `STORAGE_THRESHOLD_PERCENT` | Auto-migrate when primary usage exceeds this %. |
| `MIN_AGE_DAYS` | Only files older than this are candidates. |
| `MIN_FILE_SIZE_BYTES` | Minimum file size to consider (0 = any). |
| `DESTINATION_FOLDER_NAME` | Folder created/used in the secondary account. |
| `ALLOWED_MIME_TYPES` | Restrict to certain types (empty = all). |
| `MAX_FILES_PER_RUN` / `MAX_MB_PER_RUN` | Per-run safety caps. |
| `LOCAL_BACKUP_DIR` | Where local copies are saved before migrating. |
| `DELETE_AFTER_MIGRATE` | Remove from primary after successful upload. |
| `CONFIRM_BEFORE_DELETE` | Require user confirmation before any delete. |
| `SKIP_DUPLICATES` | Skip upload if the file already exists in destination. |
| `DRY_RUN` | Print what would happen, change nothing. |

## How to run

```bash
# Web dashboard (preferred)
python server.py            # opens http://localhost:5000; browser sign-in on first run

# Re-authorize (after changing scopes, or auth errors)
python reset_auth.py
python server.py

# CLI
python main.py              # interactive folder picker
python main.py --schedule   # run on a timer (SCHEDULE_INTERVAL_HOURS)
```

## Network resilience

All Google API calls pass `num_retries=API_RETRIES` (=5) so transient
socket/SSL errors auto-retry instead of failing the request.

## Known issues & expected behavior

- **`403 insufficientFilePermissions`** — the account does not own the file
  (it was shared in by someone else). Google blocks downloading files you don't
  own, so **no tool can migrate them.** These are now filtered out at scan time
  (`'me' in owners`) and, if one slips through, reported as a clean
  `SKIPPED (not owned)` — not a failure. To migrate such a file the user must
  first "Make a copy" in Drive (the copy is owned by them).
- **`ssl.SSLError: WRONG_VERSION_NUMBER`** — NOT a code bug. Python's HTTPS
  request to googleapis.com is being intercepted/broken by a **proxy, VPN, or
  antivirus HTTPS scanning**, or a flaky connection. Diagnose:
  `echo %HTTP_PROXY% %HTTPS_PROXY%` (clear if set), disconnect VPN, or disable
  AV TLS scanning. Quick reachability test:
  `python -c "import httplib2;print(httplib2.Http().request('https://www.googleapis.com/discovery/v1/apis','GET')[0].status)"`
  (expect `200`).
- **Google Workspace files** (Docs/Sheets/Slides) can't be downloaded as binary
  and are skipped by design.
- **Google Photos** uses `spaces="photos"`. If the Photos scope is missing the
  code degrades gracefully (skips Photos, keeps Drive) instead of crashing.

## Conventions for changes here

- Keep the hard constraint and all five safety rules intact in any refactor.
- Prefer editing `config.py` over hard-coding behavior.
- Log actions to `migration.log`; emit SSE events for anything the dashboard
  should show live.
- Never log or print full OAuth tokens or `credentials.json` contents.
- Keep `tokens/` and `credentials.json` out of any commit or shared output.
