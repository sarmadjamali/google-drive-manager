#!/usr/bin/env python3
"""
Google Drive Multi-Account Storage Manager
==========================================
Monitors your primary Google account's storage and automatically
migrates older files to a secondary account when usage exceeds a threshold.

Usage:
  python main.py              → run once (check + migrate if needed)
  python main.py --status     → show storage status only, no migration
  python main.py --dry-run    → simulate migration without making changes
  python main.py --schedule   → run every N hours (set in config.py)
  python main.py --force      → migrate regardless of threshold
  python main.py --setup      → interactive first-time setup
"""

import sys
import argparse
import datetime

import config
from auth import get_drive_service
from storage import get_storage_info, print_storage_summary, interactive_folder_picker, find_folder_id
from migrate import run_migration


BANNER = """
╔══════════════════════════════════════════════════════╗
║     Google Drive Multi-Account Storage Manager       ║
║     Using official Google Drive API only             ║
╚══════════════════════════════════════════════════════╝
"""


def check_config():
    """Validate configuration before running."""
    errors = []
    if not config.PRIMARY_ACCOUNT:
        errors.append("PRIMARY_ACCOUNT is not set in config.py")
    if not config.SECONDARY_ACCOUNT:
        errors.append("SECONDARY_ACCOUNT is not set in config.py")
    if errors:
        print("\n❌  Configuration errors:")
        for e in errors:
            print(f"    • {e}")
        print("\n    Please edit config.py and fill in the missing values.")
        sys.exit(1)


def run_once(force: bool = False, dry_run_override: bool = False):
    """Single run: check storage, migrate if threshold exceeded."""
    print(BANNER)
    check_config()

    if dry_run_override:
        config.DRY_RUN = True

    # Authenticate both accounts
    print("🔐  Authenticating accounts...")
    print(f"    Primary   : {config.PRIMARY_ACCOUNT}")
    primary_svc = get_drive_service(config.PRIMARY_ACCOUNT)

    print(f"    Secondary : {config.SECONDARY_ACCOUNT}")
    secondary_svc = get_drive_service(config.SECONDARY_ACCOUNT)

    # Check storage
    print("\n📊  Fetching storage info...")
    primary_info   = get_storage_info(primary_svc)
    secondary_info = get_storage_info(secondary_svc)

    print_storage_summary(config.PRIMARY_ACCOUNT,   primary_info)
    print_storage_summary(config.SECONDARY_ACCOUNT, secondary_info)

    # Decide whether to migrate
    threshold_hit = primary_info["percent_used"] >= config.STORAGE_THRESHOLD_PERCENT

    if not force and not threshold_hit:
        print(
            f"\n✅  Primary account is at {primary_info['percent_used']}% "
            f"(threshold: {config.STORAGE_THRESHOLD_PERCENT}%). "
            "No migration needed."
        )
        return

    if force:
        print(f"\n⚡  --force flag set — migrating regardless of threshold.")
    else:
        print(
            f"\n⚠️   Primary account at {primary_info['percent_used']}% "
            f"(threshold: {config.STORAGE_THRESHOLD_PERCENT}%). "
            "Starting migration..."
        )

    # ── Folder selection ──────────────────────────────────────────────
    # If SOURCE_FOLDERS set in config, resolve their IDs automatically.
    # Otherwise ask the user to pick interactively.
    source_folder_ids = []
    if config.SOURCE_FOLDERS:
        print("\n📂  Resolving configured source folders...")
        for name in config.SOURCE_FOLDERS:
            fid = find_folder_id(primary_svc, name)
            if fid:
                source_folder_ids.append(fid)
                print(f"  ✅  Found: {name}")
            else:
                print(f"  ⚠️   Not found: {name} (skipping)")
    else:
        source_folder_ids = interactive_folder_picker(primary_svc)

    summary = run_migration(primary_svc, secondary_svc, source_folder_ids)

    # Print summary
    print("\n" + "─" * 54)
    print(f"  Run completed at : {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Files migrated   : {summary['files_migrated']}")
    print(f"  Files failed     : {summary['files_failed']}")
    print(f"  Data moved       : {summary['bytes_moved'] / 1e6:.1f} MB")
    if summary.get("skipped_dry_run"):
        print(f"  Dry-run skipped  : {summary['skipped_dry_run']} files")
    print(f"  Log saved to     : {config.LOG_FILE}")
    print("─" * 54)


def run_status():
    """Print storage status for both accounts and exit."""
    print(BANNER)
    check_config()

    primary_svc   = get_drive_service(config.PRIMARY_ACCOUNT)
    secondary_svc = get_drive_service(config.SECONDARY_ACCOUNT)

    primary_info   = get_storage_info(primary_svc)
    secondary_info = get_storage_info(secondary_svc)

    print("\n📊  Storage Status\n")
    print_storage_summary(config.PRIMARY_ACCOUNT,   primary_info)
    print_storage_summary(config.SECONDARY_ACCOUNT, secondary_info)

    combined_used  = primary_info["used_gb"]  + secondary_info["used_gb"]
    combined_total = primary_info["total_gb"] + secondary_info["total_gb"]
    print(f"\n  Combined: {combined_used:.2f} GB used of {combined_total:.2f} GB total")


def run_scheduled():
    """Run on a schedule (every N hours as set in config)."""
    import schedule
    import time

    print(BANNER)
    print(f"⏰  Scheduled mode: running every {config.SCHEDULE_INTERVAL_HOURS} hour(s).")
    print("    Press Ctrl+C to stop.\n")

    def job():
        print(f"\n{'─'*54}")
        print(f"  Scheduled run at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'─'*54}")
        run_once()

    # Run immediately on start, then schedule
    job()
    schedule.every(config.SCHEDULE_INTERVAL_HOURS).hours.do(job)

    while True:
        schedule.run_pending()
        time.sleep(60)


def interactive_setup():
    """Guide the user through first-time configuration."""
    print(BANNER)
    print("🛠️   First-time Setup\n")
    print("  This will update config.py with your account emails.")
    print("  Your credentials.json must already be in this folder.\n")

    primary = input(f"  Primary Google account email [{config.PRIMARY_ACCOUNT}]: ").strip()
    if primary:
        _update_config("PRIMARY_ACCOUNT", primary)

    secondary = input("  Secondary Google account email: ").strip()
    if secondary:
        _update_config("SECONDARY_ACCOUNT", secondary)

    threshold = input(f"  Storage threshold % to trigger migration [{config.STORAGE_THRESHOLD_PERCENT}]: ").strip()
    if threshold.isdigit():
        _update_config("STORAGE_THRESHOLD_PERCENT", int(threshold))

    print("\n  ✅  config.py updated.")
    print("  Run 'python main.py --status' to authenticate and see your storage.\n")


def _update_config(key: str, value):
    """Simple in-place replacement of a config value."""
    from pathlib import Path
    cfg_path = Path("config.py")
    text = cfg_path.read_text()
    import re
    if isinstance(value, str):
        replacement = f'{key} = "{value}"'
    else:
        replacement = f"{key} = {value}"
    text = re.sub(rf"^{key}\s*=.*$", replacement, text, flags=re.MULTILINE)
    cfg_path.write_text(text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Google Drive Multi-Account Storage Manager"
    )
    parser.add_argument("--status",   action="store_true", help="Show storage status only")
    parser.add_argument("--dry-run",  action="store_true", help="Simulate — no changes made")
    parser.add_argument("--schedule", action="store_true", help="Run on a schedule")
    parser.add_argument("--force",    action="store_true", help="Migrate regardless of threshold")
    parser.add_argument("--setup",    action="store_true", help="Interactive first-time setup")
    args = parser.parse_args()

    if args.setup:
        interactive_setup()
    elif args.status:
        run_status()
    elif args.schedule:
        run_scheduled()
    else:
        run_once(force=args.force, dry_run_override=args.dry_run)
