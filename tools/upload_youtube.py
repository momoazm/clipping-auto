"""Publish a finished Short to YouTube -- via Zernio (zernio.com), not direct OAuth.

Why Zernio instead of the YouTube Data API OAuth flow: avoids babysitting a refresh
token (YT_TOKEN_JSON expires -- needs re-running youtube_auth_setup.py locally and
hand-updating the GitHub secret every time it goes stale). Zernio already has this
channel connected on their side; same Bearer API key as Instagram, just a different
ZERNIO_YOUTUBE_ID accountId.

SAFETY GATE: refuses to publish without --confirm (dry-run preview otherwise). Irreversible.

AUTH/SETUP:
  * ZERNIO_API in API.env (the GitHub secret name already wired into this repo's workflow,
    shared with Instagram).
  * ZERNIO_YOUTUBE_ID -- the Zernio-internal id for the connected YouTube channel (fetch
    via GET /v1/accounts after connecting it in Zernio's dashboard).

YouTube (like Instagram) needs a PUBLIC url to the video, not a local path -- pass
--video-url (host_public.py). Shorts vs. regular video is auto-detected by YouTube from
duration + aspect ratio; no separate flag needed. Zernio has no `tags` field for YouTube,
so --tags is folded into the description as hashtags instead.

Usage:
    python tools/upload_youtube.py --video-url https://... --title "..." \\
        [--description "..."] [--tags a,b,c] [--privacy public|unlisted|private] [--confirm]

Prints JSON: dry run -> {"status":"preview",...}; real -> {"status":"uploaded","post_id","url",...}.
"""
import argparse
import os
import time
import uuid

from _common import load_env, emit, fail

ZERNIO_API = "https://zernio.com/api/v1"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-url", required=True, help="PUBLIC https url to the mp4 (host_public.py)")
    parser.add_argument("--title", required=True)
    parser.add_argument("--description", default="")
    parser.add_argument("--tags", default="", help="Comma-separated; folded into the description as hashtags")
    parser.add_argument("--privacy", default="public", choices=["public", "unlisted", "private"])
    parser.add_argument("--confirm", action="store_true", help="Required to actually publish.")
    parser.add_argument("--poll-timeout", type=int, default=180)
    args = parser.parse_args()

    load_env()
    api_key = os.environ.get("ZERNIO_API", "").strip()
    account_id = os.environ.get("ZERNIO_YOUTUBE_ID", "").strip()
    if not api_key:
        fail("ZERNIO_API not set in API.env. Sign up free at zernio.com and grab it from "
             "Settings -> API Keys.")
        return
    if not account_id:
        fail("ZERNIO_YOUTUBE_ID not set in API.env. After connecting the YouTube channel "
             "in Zernio's dashboard, fetch it via GET /v1/accounts.")
        return

    title = args.title[:100]
    tags = [t.strip() for t in args.tags.split(",") if t.strip() and t.strip().lower() != "shorts"]
    hashtags = " ".join(f"#{t}" for t in tags)
    description = (args.description + ("\n\n" + hashtags if hashtags else "")).strip()[:5000]

    payload = {
        "content": description,
        "mediaItems": [{"type": "video", "url": args.video_url}],
        "platforms": [{
            "platform": "youtube",
            "accountId": account_id,
            "platformSpecificData": {"title": title, "visibility": args.privacy},
        }],
        "publishNow": True,
    }

    if not args.confirm:
        emit({
            "status": "preview", "would_upload": True, "platform": "youtube",
            "via": "zernio", "account_id": account_id,
            "video_url": args.video_url, "title": title, "description": description,
            "privacy": args.privacy,
            "note": "DRY RUN. Re-run with --confirm to publish.",
        })
        return

    import httpx

    headers = {"Authorization": f"Bearer {api_key}", "x-request-id": str(uuid.uuid4())}
    try:
        r = httpx.post(f"{ZERNIO_API}/posts", json=payload, headers=headers, timeout=60)
        r.raise_for_status()
        post = r.json().get("post", {})
    except Exception as e:
        body = getattr(getattr(e, "response", None), "text", "")
        fail(f"Zernio post create failed: {e} {body}".strip())
        return

    post_id = post.get("_id")
    if not post_id:
        fail(f"Zernio post create returned no post id: {post}")
        return

    def platform_entry(p):
        for entry in p.get("platforms", []):
            if entry.get("platform") == "youtube":
                return entry
        return {}

    entry = platform_entry(post)
    status = entry.get("status") or post.get("status")

    # publishNow:true still needs YouTube-side processing (transcode) to finish -- poll
    # the same way upload_instagram.py does.
    deadline = time.time() + args.poll_timeout
    while status not in ("published", "failed", "error") and time.time() < deadline:
        time.sleep(5)
        try:
            s = httpx.get(f"{ZERNIO_API}/posts/{post_id}",
                          headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
            s.raise_for_status()
            post = s.json().get("post", post)
            entry = platform_entry(post)
            status = entry.get("status") or post.get("status")
        except Exception:
            pass

    if status not in ("published",):
        fail(f"Zernio publish did not complete (status={status}).",
             post_id=post_id, platform_status=entry)
        return

    emit({"status": "uploaded", "platform": "youtube", "via": "zernio",
          "post_id": post_id, "url": entry.get("platformPostUrl")})


if __name__ == "__main__":
    main()
