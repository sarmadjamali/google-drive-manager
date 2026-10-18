"""
One-time auth reset.
Deletes the saved OAuth token files so the next run of server.py
re-opens the browser and re-authorizes WITH the new
drive.photos.readonly scope (needed to scan Google Photos).

Run once:   python reset_auth.py
Then:       python server.py   (browser will open for both accounts)

No third-party services involved — this only removes local token files.
"""

from pathlib import Path
import config

token_dir = Path(config.TOKEN_DIR)

if not token_dir.exists():
    print(f"No token directory found at {token_dir} — nothing to reset.")
else:
    removed = []
    for f in token_dir.glob("token_*.json"):
        try:
            f.unlink()
            removed.append(f.name)
        except Exception as e:
            print(f"  Could not delete {f.name}: {e}")

    if removed:
        print("Deleted token files:")
        for name in removed:
            print(f"   - {name}")
        print("\nDone. Now run:  python server.py")
        print("Your browser will open to sign in again and grant Google Photos access.")
    else:
        print("No token_*.json files found — already clean.")
        print("Run:  python server.py")
