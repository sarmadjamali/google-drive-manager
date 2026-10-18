"""
OAuth2 authentication for Google Drive API.
Opens a browser window for each account — no third-party services involved.
Tokens are stored locally in the tokens/ directory.
"""

import os
import json
from pathlib import Path
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import config

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/drive.photos.readonly",
]


def _token_path(account_email: str) -> Path:
    """Return the path to the token file for a given account."""
    token_dir = Path(config.TOKEN_DIR)
    token_dir.mkdir(exist_ok=True)
    safe_name = account_email.replace("@", "_at_").replace(".", "_")
    return token_dir / f"token_{safe_name}.json"


def get_credentials(account_email: str) -> Credentials:
    """
    Load or create OAuth2 credentials for an account.
    On first run, opens a browser window so the user can log in.
    On subsequent runs, uses the saved token (refreshed automatically).
    """
    creds = None
    token_file = _token_path(account_email)

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print(f"  Refreshing token for {account_email}...")
            creds.refresh(Request())
        else:
            if not Path(config.CREDENTIALS_FILE).exists():
                raise FileNotFoundError(
                    f"\n❌  '{config.CREDENTIALS_FILE}' not found.\n"
                    "    Please follow SETUP.md Step 1 to download it from Google Cloud Console."
                )
            print(f"\n{'='*60}")
            print(f"  Authorize account: {account_email}")
            print(f"{'='*60}")
            print("  1. Copy the URL below and paste it in a browser.")
            print("  2. Sign in as the CORRECT Google account (see above).")
            print("  3. Grant access — then come back here.\n")
            flow = InstalledAppFlow.from_client_secrets_file(
                config.CREDENTIALS_FILE, SCOPES
            )
            creds = flow.run_local_server(
                port=8085,
                open_browser=False,
                authorization_prompt_message="  Auth URL: {url}\n",
            )

        # Save token for next run
        token_file.write_text(creds.to_json())
        print(f"  ✅ Token saved → {token_file}")

    return creds


def get_drive_service(account_email: str):
    """Return an authenticated Google Drive API service for the given account."""
    creds = get_credentials(account_email)
    return build("drive", "v3", credentials=creds)
