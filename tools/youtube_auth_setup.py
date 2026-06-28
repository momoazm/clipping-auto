"""One-time OAuth consent for YouTube uploads: an OAuth client -> token.json.

The OAuth client (a Desktop "installed" app, with the YouTube Data API v3 enabled) can come
from EITHER:
  - the local clipping/.env: set YOUTUBE_CLIENT_ID + YOUTUBE_CLIENT_SECRET (preferred), or
  - a clipping/credentials.json file downloaded from Google Cloud Console.

Run once, standalone; opens a browser for consent. This project keeps its OWN token.json
inside clipping/ (separate from the Gmail OAuth used by the newsletter project).

Scopes requested:
  - youtube.upload       (publish videos)
  - youtube.readonly     (resolve your channel name; list uploads, stats, comments)
  - yt-analytics.readonly (retention, CTR, watch time, subs — for the weekly-roundup skill)

Prereq for analytics: enable the **YouTube Analytics API** in the same Cloud project.

Usage:
    python tools/youtube_auth_setup.py

Prints JSON: {"status","token_path","source","channel_title","channel_id"}
"""
import os

from _common import REPO_ROOT, load_env, emit, fail

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


def client_config_from_env():
    """Build an InstalledAppFlow client config from the .env, or None if id/secret aren't set."""
    cid = os.environ.get("YOUTUBE_CLIENT_ID", "").strip()
    secret = os.environ.get("YOUTUBE_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        return None
    return {
        "installed": {
            "client_id": cid,
            "client_secret": secret,
            "project_id": os.environ.get("YOUTUBE_PROJECT_ID", "").strip(),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": ["http://localhost"],
        }
    }


def main():
    load_env()  # shared root API.env
    # Local project .env (gitignored) takes precedence — this is where the YouTube client lives.
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env", override=True)

    token_path = os.environ.get("YT_TOKEN_PATH", "token.json")

    from google_auth_oauthlib.flow import InstalledAppFlow

    config = client_config_from_env()
    if config:
        flow = InstalledAppFlow.from_client_config(config, SCOPES)
        source = "env"
    else:
        creds_path = os.environ.get("YT_CREDENTIALS_PATH", "credentials.json")
        if not os.path.isfile(creds_path):
            fail(
                "No OAuth client found. Either set YOUTUBE_CLIENT_ID + YOUTUBE_CLIENT_SECRET in "
                f"clipping/.env, or download a Desktop OAuth client to {creds_path}. In Google Cloud "
                "Console: enable the YouTube Data API v3 and create an OAuth Client ID (type 'Desktop app')."
            )
            return
        flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
        source = "file"

    creds = flow.run_local_server(port=0)

    with open(token_path, "w", encoding="utf-8") as f:
        f.write(creds.to_json())

    channel_title, channel_id = None, None
    try:
        from googleapiclient.discovery import build

        yt = build("youtube", "v3", credentials=creds)
        resp = yt.channels().list(part="snippet", mine=True).execute()
        items = resp.get("items", [])
        if items:
            channel_title = items[0]["snippet"]["title"]
            channel_id = items[0]["id"]
    except Exception:
        pass  # token is still valid for uploading even if the lookup failed

    emit({"status": "ok", "token_path": token_path, "source": source,
          "channel_title": channel_title, "channel_id": channel_id})


if __name__ == "__main__":
    main()
