"""Upload a finished Short to YouTube (the only irreversible step).

Pipeline role: publishes the rendered clip via the YouTube Data API v3 resumable
upload. `<60 s` + `9:16` + `#Shorts` in the title/description = Shorts treatment.

SAFETY GATE: this tool refuses to publish unless `--confirm` is passed. Without it
it performs a DRY RUN -- resolving the destination channel and echoing exactly what
WOULD be posted (title, privacy, file) so the agent can show the user the gate.
Only call it with --confirm after the user explicitly approves. (~1600 quota units
per upload; the default 10k/day budget allows ~6 uploads/day.)

Usage:
    # preview / gate (no upload):
    python tools/upload_youtube.py --video .tmp/short_01.mp4 --title "..." --privacy public
    # actually publish:
    python tools/upload_youtube.py --video .tmp/short_01.mp4 --title "..." \
        --description "..." --tags shorts,clip --privacy public --confirm

Prints JSON: dry run -> {"status":"preview",...}; real -> {"status":"uploaded","video_id","url",...}
"""
import argparse
import os

from _common import load_env, emit, fail

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def load_credentials(token_path):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return creds


def channel_title(youtube):
    try:
        resp = youtube.channels().list(part="snippet", mine=True).execute()
        items = resp.get("items", [])
        return items[0]["snippet"]["title"] if items else None
    except Exception:
        return None


def ensure_shorts_tag(text):
    return text if "#shorts" in (text or "").lower() else f"{text} #Shorts".strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--description", default="")
    parser.add_argument("--tags", default="shorts", help="Comma-separated")
    parser.add_argument("--privacy", default="public", choices=["public", "unlisted", "private"])
    parser.add_argument("--category", default="22", help="YouTube categoryId (22 = People & Blogs)")
    parser.add_argument("--confirm", action="store_true",
                        help="Required to actually publish. Omit for a dry-run gate preview.")
    args = parser.parse_args()

    load_env()
    token_path = os.environ.get("YT_TOKEN_PATH", "token.json")

    if not os.path.isfile(args.video):
        fail(f"Video not found: {args.video}")
        return
    if not os.path.isfile(token_path):
        fail(f"{token_path} not found. Run: python tools/youtube_auth_setup.py")
        return

    try:
        creds = load_credentials(token_path)
    except Exception as e:
        fail(f"Could not load/refresh YouTube credentials: {e}. "
             "If the token was revoked, re-run: python tools/youtube_auth_setup.py")
        return

    from googleapiclient.discovery import build

    youtube = build("youtube", "v3", credentials=creds)
    title = ensure_shorts_tag(args.title)[:100]
    description = ensure_shorts_tag(args.description)
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    if "shorts" not in [t.lower() for t in tags]:
        tags.append("shorts")
    chan = channel_title(youtube)

    if not args.confirm:
        emit({
            "status": "preview",
            "would_upload": True,
            "channel_title": chan,
            "title": title,
            "privacy": args.privacy,
            "tags": tags,
            "video": args.video,
            "size_bytes": os.path.getsize(args.video),
            "note": "DRY RUN. Re-run with --confirm to publish after user approval.",
        })
        return

    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": args.category,
        },
        "status": {
            "privacyStatus": args.privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(args.video, mimetype="video/mp4", resumable=True, chunksize=4 * 1024 * 1024)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    try:
        response = None
        while response is None:
            _status, response = request.next_chunk()
    except Exception as e:
        fail(f"YouTube upload failed: {e}", channel_title=chan)
        return

    video_id = response.get("id")
    emit({
        "status": "uploaded",
        "video_id": video_id,
        "url": f"https://youtube.com/shorts/{video_id}",
        "privacy": args.privacy,
        "channel_title": chan,
        "title": title,
    })


if __name__ == "__main__":
    main()
