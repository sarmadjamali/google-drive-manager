# Google Drive Storage Manager

A fully **local**, **privacy-first** tool that frees up space on a nearly-full Google
Drive by moving older files to a second Google account — downloading each file to a
local backup, uploading it to the secondary account, and only then (with your
confirmation) deleting it from the primary.

It talks **only to Google's official Drive API over OAuth**. No third-party servers,
uploaders, sync services, or telemetry ever touch your data — everything runs on your
own machine.

---

## Why

Google's free 15 GB is shared across Drive, Gmail, and Photos. When Drive fills up,
you either pay for more storage or manually shuffle files between accounts. This tool
automates the shuffle **safely**: local backup first, verified upload second, and
deletion only after confirmation.

---

## Features

- 🖥️ **Web dashboard** — a 3-column UI (sources · file browser · selected files) with
  live progress, or a scriptable **CLI**.
- 🔒 **Local-only & official API** — OAuth direct to Google; your files never pass
  through anyone else's server.
- 💾 **Local backup first** — every migrated file is written to disk *before* upload or
  delete, so you always have a copy.
- ✅ **Confirm before delete** — files leave the primary account only after the upload
  to the secondary is verified, and only when you say so.
- 🧭 **Folder-aware** — scan all of My Drive or pick specific folders; the destination
  folder hierarchy can be preserved.
- 🚫 **No duplicates** — skips uploading a file that already exists (same name + size)
  in the destination.
- 📝 **Keep list** — mark files you want to keep in the primary so they're never
  suggested again.
- 🔁 **Resilient** — automatic retries on transient network/SSL errors.

---

## Requirements

- Python 3.9+
- Two Google accounts (a primary that fills up, and a secondary for overflow)
- A free Google Cloud project with the Drive API enabled (see setup below)

---

## Quick start

```bash
# 1. Clone
git clone https://github.com/<your-username>/<your-repo>.git
cd <your-repo>

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create your config from the template
copy config.example.py config.py        # Windows
# cp config.example.py config.py         # Mac/Linux
#   → edit config.py and set PRIMARY_ACCOUNT / SECONDARY_ACCOUNT

# 4. Add your Google OAuth credentials (see "Google Cloud setup" below)
#   → place the downloaded credentials.json in this folder

# 5. Run the web dashboard
python server.py
#   → open http://localhost:5000  (first run opens a browser sign-in for each account)
```

Prefer the terminal?

```bash
python main.py --status      # authenticate + show storage for both accounts
python main.py --dry-run     # simulate a migration without changing anything
python main.py               # interactive migration (pick folders)
python main.py --schedule    # run on a timer (SCHEDULE_INTERVAL_HOURS)
python main.py --force       # migrate regardless of the storage threshold
```

Full step-by-step instructions, including Task Scheduler / cron automation, are in
**[SETUP.md](SETUP.md)**.

---

## Google Cloud setup (one time)

1. Go to [console.cloud.google.com](https://console.cloud.google.com/) → **New Project**.
2. **APIs & Services → Library** → enable **Google Drive API**.
3. **APIs & Services → OAuth consent screen**:
   - User type **External**, fill in the app name and your email.
   - Keep the app in **Testing** status.
   - Under **Test users**, add **both** of your Google account emails.
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Application type **Desktop app**.
   - Download the JSON and rename it to **`credentials.json`** in the project folder.

On first run you'll see a browser consent screen for each account. Because the app
isn't Google-verified (it's your personal tool), click **Advanced → Go to app
(unsafe)** — that's expected for a self-hosted, unpublished app.

---

## How it works (the safety flow)

For each candidate file, in order, inside one try/except:

```
download from primary  →  save local backup  →  upload to secondary
   →  verify it exists in secondary  →  confirm  →  delete from primary
```

The delete step never runs before the upload is verified, and (by default) never runs
without your explicit confirmation. If anything fails mid-way, the local backup still
exists and nothing is deleted.

---

## Configuration reference

All behavior lives in `config.py` (copied from `config.example.py`):

| Setting | Default | Meaning |
|---|---|---|
| `STORAGE_THRESHOLD_PERCENT` | 80 | Auto-migrate when primary usage exceeds this %. |
| `MIN_AGE_DAYS` | 30 | Only files older than this are candidates. |
| `MIN_FILE_SIZE_BYTES` | 0 | Minimum file size to consider (0 = any). |
| `DESTINATION_FOLDER_NAME` | `Migrated_from_Primary` | Folder used in the secondary account. |
| `ALLOWED_MIME_TYPES` | `[]` | Restrict to certain types (empty = all). |
| `MAX_FILES_PER_RUN` / `MAX_MB_PER_RUN` | 50 / 500 | Per-run safety caps. |
| `LOCAL_BACKUP_DIR` | `GDrive_Backup` | Where local copies are saved before migrating. |
| `DELETE_AFTER_MIGRATE` | True | Remove from primary after a verified upload. |
| `CONFIRM_BEFORE_DELETE` | True | Require confirmation before any delete. |
| `SKIP_DUPLICATES` | True | Skip upload if the file already exists in destination. |
| `PRESERVE_FOLDER_STRUCTURE` | True | Recreate the Drive folder tree in the destination. |
| `DRY_RUN` | False | Print what would happen, change nothing. |

---

## A note on Google Photos

This tool **cannot migrate Google Photos**. Google classifies the Photos Library API's
browse/list access as a *restricted scope* that requires full app verification plus a
paid third-party security assessment — not feasible for a personal tool. (Drive's old
`spaces=photos` sync was also discontinued by Google in 2019.)

**To move photos between accounts, use Google's own built-in
[Partner Sharing](https://support.google.com/photos/answer/7378858):** share your
library from the primary account to the secondary, enable auto-save on the secondary,
then delete from the primary once the photos appear there. It's first-party, automatic,
and needs no code.

---

## Troubleshooting

- **`403 insufficientFilePermissions`** — the account doesn't own the file (it was
  shared in by someone else). Google blocks downloading files you don't own, so no tool
  can migrate them. These are filtered out automatically. To migrate one, "Make a copy"
  in Drive first (the copy is owned by you).
- **`ssl.SSLError: WRONG_VERSION_NUMBER`** — not a code bug. A proxy, VPN, or antivirus
  HTTPS scanner is intercepting the connection to googleapis.com. Clear `HTTP_PROXY` /
  `HTTPS_PROXY`, disconnect the VPN, or disable AV TLS scanning.
- **`403 insufficientScopes` / "granted scopes do not give access"** — your saved token
  predates a scope change. Run `python reset_auth.py`, then re-run and re-authorize.
- **Google Workspace files** (Docs/Sheets/Slides) can't be downloaded as binary and are
  skipped by design.

---

## Security & privacy

- `credentials.json`, `tokens/`, `migration.log`, `kept_files.json`, and your local
  backup folder are **git-ignored** and must never be committed or shared.
- Tokens grant full access to your Drive — treat them like passwords. Revoke anytime at
  [myaccount.google.com/permissions](https://myaccount.google.com/permissions).
- Nothing is sent anywhere except directly to Google's API from your machine.

---

## License

Released under the MIT License — see [LICENSE](LICENSE).

> **Disclaimer:** this tool deletes files from your primary account (after backup and
> confirmation). Review the safety settings, test with `DRY_RUN = True` first, and use
> at your own risk. The authors are not responsible for data loss.
