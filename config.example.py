"""
Configuration for Google Drive Multi-Account Storage Manager.

SETUP: copy this file to `config.py` and fill in your own values.
    Windows:  copy config.example.py config.py
    Mac/Linux: cp config.example.py config.py

`config.py` is git-ignored so your personal accounts and paths never get committed.
"""

# ─── Account Emails ───────────────────────────────────────────────────────────
# Used as labels for your local token files and to pick which account is which.
# The actual sign-in happens via Google's OAuth browser flow.
PRIMARY_ACCOUNT   = "your-primary@gmail.com"     # the account that fills up
SECONDARY_ACCOUNT = "your-secondary@gmail.com"   # the overflow / backup account

# ─── Migration Trigger ────────────────────────────────────────────────────────
# When primary account storage usage exceeds this % → auto-migrate files
STORAGE_THRESHOLD_PERCENT = 80

# ─── What to Migrate ──────────────────────────────────────────────────────────
MIN_AGE_DAYS = 30            # only files older than this are candidates
MIN_FILE_SIZE_BYTES = 0      # only migrate files larger than this (0 = no minimum)

# Folders to migrate FROM. Empty [] = pick interactively / scan all of My Drive.
# Examples: ["Photos", "Videos", "WhatsApp"]
SOURCE_FOLDERS = []

# Folder name to create/use in the secondary account
DESTINATION_FOLDER_NAME = "Migrated_from_Primary"

# File types to migrate (MIME substrings). Empty = all. e.g. ["image/", "video/"]
ALLOWED_MIME_TYPES = []

# ─── Local Backup ─────────────────────────────────────────────────────────────
# Save a local copy of every file BEFORE migrating it. Use an absolute path.
# A relative folder (kept next to the code, git-ignored) is the simple default.
LOCAL_BACKUP_DIR = "GDrive_Backup"

# ─── Delete Behaviour ─────────────────────────────────────────────────────────
DELETE_AFTER_MIGRATE  = True   # delete from primary AFTER upload is confirmed
CONFIRM_BEFORE_DELETE = True   # ask before deleting each file (recommended)
SKIP_DUPLICATES       = True   # skip upload if same name+size already in destination

# ─── Safety ───────────────────────────────────────────────────────────────────
MAX_FILES_PER_RUN = 50    # max files per run (0 = unlimited)
MAX_MB_PER_RUN    = 500   # max total MB per run (0 = unlimited)
DRY_RUN           = False  # True = only print what WOULD happen, change nothing

# ─── Schedule (for auto-run mode: python main.py --schedule) ──────────────────
SCHEDULE_INTERVAL_HOURS = 6

# ─── Web Dashboard ────────────────────────────────────────────────────────────
DASHBOARD_PORT = 5000

# ─── Folder Structure ─────────────────────────────────────────────────────────
# True = recreate the Drive folder hierarchy in the destination + local backup.
# False = flat (all files in one folder).
PRESERVE_FOLDER_STRUCTURE = True

# ─── Paths (defaults are fine for most users) ─────────────────────────────────
TOKEN_DIR        = "tokens"
LOG_FILE         = "migration.log"
CREDENTIALS_FILE = "credentials.json"
KEPT_FILES_FILE  = "kept_files.json"
