# Google Drive Multi-Account Storage Manager — Setup Guide

## What this does
- Monitors your **primary** Google account storage
- When it exceeds the threshold (default 80%), automatically moves older files to your **secondary** account
- Uses **only Google's official API** — no third-party services touch your data
- Runs locally on your machine

---

## Step 1 — Create a Google Cloud Project & Download credentials.json

1. Go to https://console.cloud.google.com/
2. Click **"New Project"** → name it anything (e.g. `drive-manager`)
3. In the left menu → **APIs & Services → Library**
4. Search for **"Google Drive API"** → click it → click **Enable**
5. Go to **APIs & Services → OAuth consent screen**
   - User type: **External**
   - Fill in App name (e.g. `Drive Manager`), your email
   - Click **Save and Continue** through all steps
   - On the **Test users** page → add BOTH your Google account emails
6. Go to **APIs & Services → Credentials**
   - Click **+ Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Name: anything
   - Click **Create**
7. Click the **download icon** (⬇) next to the new credential
8. **Rename the downloaded file to `credentials.json`**
9. **Place `credentials.json` in the same folder as `main.py`**

---

## Step 2 — Install Python dependencies

```bash
cd gdrive-manager
pip install -r requirements.txt
```

---

## Step 3 — Configure your accounts

Open `config.py` and fill in:

```python
PRIMARY_ACCOUNT   = "your-primary@gmail.com"
SECONDARY_ACCOUNT = "your-secondary@gmail.com"
```

Or run the interactive setup:
```bash
python main.py --setup
```

---

## Step 4 — First run (authenticate)

```bash
python main.py --status
```

This will:
1. Open a browser window → sign in with your **primary** account → click Allow
2. Open another browser window → sign in with your **secondary** account → click Allow
3. Show you the storage usage of both accounts

Tokens are saved in `tokens/` folder — you only sign in once.

---

## Step 5 — Test with a dry run

```bash
python main.py --dry-run
```

This shows exactly what files WOULD be migrated, without moving anything.

---

## Step 6 — Run for real

```bash
python main.py
```

Or run on a schedule (every 6 hours by default, configurable in config.py):

```bash
python main.py --schedule
```

---

## Running automatically on Windows (Task Scheduler)

1. Open **Task Scheduler** → Create Basic Task
2. Trigger: Daily / every 6 hours
3. Action: Start a program
   - Program: `python`
   - Arguments: `C:\path\to\gdrive-manager\main.py`
   - Start in: `C:\path\to\gdrive-manager`

## Running automatically on Mac/Linux (cron)

```bash
crontab -e
# Add this line to run every 6 hours:
0 */6 * * * cd /path/to/gdrive-manager && python main.py >> migration.log 2>&1
```

---

## Key config options (config.py)

| Option | Default | Description |
|--------|---------|-------------|
| `STORAGE_THRESHOLD_PERCENT` | 80 | Migrate when primary is X% full |
| `MIN_AGE_DAYS` | 30 | Only migrate files older than N days |
| `MAX_FILES_PER_RUN` | 50 | Safety cap on files per run |
| `MAX_MB_PER_RUN` | 500 | Safety cap on data per run |
| `DELETE_AFTER_MIGRATE` | True | Delete from primary after upload |
| `DRY_RUN` | False | Simulate only, no real changes |
| `SCHEDULE_INTERVAL_HOURS` | 6 | Interval for --schedule mode |

---

## Security notes
- `credentials.json` and `tokens/` contain sensitive data — keep them on your machine only
- Never upload them to GitHub or share them
- Tokens can be revoked anytime at https://myaccount.google.com/permissions
