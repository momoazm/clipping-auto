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
import json
import os
import time
import uuid

from _common import load_env, emit, fail

ZERNIO_API = "https://zernio.com/api/v1"


def _platform_entry(post):
    for entry in (post or {}).get("platforms", []):
        if entry.get("platform") == "youtube":
            return entry
    return {}


def _platform_detail(entry):
    details = {key: entry.get(key) for key in ("error", "code", "message", "platformError")
               if entry.get(key) not in (None, "", {})}
    if not details:
        return "provider returned no YouTube error detail"
    return json.dumps(details, ensure_ascii=True, separators=(",", ":"))[:600]


def create_post(payload, api_key, max_tries=5):
    """Create one idempotent Zernio post with bounded transient retries."""
    import httpx
    backoff, last = 10, None
    request_id = str(uuid.uuid4())
    for attempt in range(max_tries):
        try:
            response = httpx.post(f"{ZERNIO_API}/posts", json=payload,
                                  headers={"Authorization": f"Bearer {api_key}",
                                           "x-request-id": request_id}, timeout=60)
            try:
                body = response.json()
            except Exception:
                body = {}
        except Exception as exc:
            last = str(exc)
            if attempt < max_tries - 1:
                time.sleep(min(backoff, 120)); backoff *= 2
            continue

        details = body.get("details") if isinstance(body, dict) else {}
        details = details if isinstance(details, dict) else {}
        if response.status_code == 429:
            return None, f"Zernio HTTP 429: {body.get('error') or 'account temporarily rate-limited'}", {
                "rate_limited": True, "status_code": 429,
                "rate_limited_until": details.get("rateLimitedUntil"),
            }
        if response.status_code == 409:
            return None, f"Zernio HTTP 409: {body.get('error') or 'duplicate content'}", {
                "duplicate": True, "status_code": 409,
                "existing_post_id": details.get("existingPostId"),
            }
        if 400 <= response.status_code < 500:
            return None, f"Zernio HTTP {response.status_code}: {response.text[:320].strip()}", {}
        if response.status_code >= 500:
            last = f"HTTP {response.status_code}: {response.text[:200]}"
            if attempt < max_tries - 1:
                retry_after = response.headers.get("Retry-After", "")
                wait = int(retry_after) if retry_after.isdigit() else backoff
                time.sleep(min(wait, 120)); backoff *= 2
            continue
        if response.status_code >= 400:
            return None, f"Zernio post create failed HTTP {response.status_code}: {response.text[:320]}", {}
        post = body.get("post") or body.get("existingPost") if isinstance(body, dict) else None
        return post if isinstance(post, dict) else {}, None, {}
    return None, f"Zernio post create failed after {max_tries} tries ({last})", {}


def retry_post(post_id, api_key, max_tries=2):
    """Retry only failed platforms on the existing Zernio post."""
    import httpx
    backoff, last = 10, None
    url = f"{ZERNIO_API}/posts/{post_id}/retry"
    for attempt in range(max_tries):
        try:
            response = httpx.post(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=60)
            try:
                body = response.json()
            except Exception:
                body = {}
        except Exception as exc:
            last = str(exc)
            if attempt < max_tries - 1:
                time.sleep(min(backoff, 120)); backoff *= 2
            continue
        raw = response.text[:320]
        if response.status_code == 429 or response.status_code >= 500:
            last = f"HTTP {response.status_code}: {raw}"
            if attempt < max_tries - 1:
                retry_after = response.headers.get("Retry-After", "")
                wait = int(retry_after) if retry_after.isdigit() else backoff
                time.sleep(min(wait, 120)); backoff *= 2
            continue
        if response.status_code >= 400:
            return None, f"Zernio post retry failed HTTP {response.status_code}: {raw}"
        post = body.get("post") or body.get("existingPost") if isinstance(body, dict) else None
        if isinstance(post, dict):
            return post, None
        return None, f"Zernio post retry returned no post: {raw}"
    return None, f"Zernio post retry failed after {max_tries} tries ({last})"


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
    api_key = (os.environ.get("ZERNIO_API") or os.environ.get("ZERNIO_API_KEY") or "").strip()
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

    post, create_error, create_meta = create_post(payload, api_key)
    if create_error:
        if create_meta.get("rate_limited"):
            fail(create_error, platform="youtube", status_code=create_meta.get("status_code"),
                 rate_limited=True, rate_limited_until=create_meta.get("rate_limited_until"))
        fail(create_error)
        return

    post_id = post.get("_id")
    if not post_id:
        fail(f"Zernio post create returned no post id: {post}")
        return

    entry = _platform_entry(post)
    status = entry.get("status") or post.get("status")

    # publishNow:true still needs YouTube-side processing (transcode) to finish -- poll
    # the same way upload_instagram.py does.
    poll_error = None
    retry_attempted = False
    retry_error = None

    def poll_until_terminal():
        nonlocal post, entry, status, poll_error
        deadline = time.time() + args.poll_timeout
        while status not in ("published", "failed", "error", "partial") and time.time() < deadline:
            time.sleep(5)
            try:
                s = httpx.get(f"{ZERNIO_API}/posts/{post_id}",
                              headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
                s.raise_for_status()
                body = s.json()
                post = body.get("post", post) if isinstance(body, dict) else post
                entry = _platform_entry(post)
                status = entry.get("status") or post.get("status")
            except Exception as exc:
                poll_error = str(exc)

    poll_until_terminal()
    if status in ("failed", "error", "partial"):
        retry_attempted = True
        retried, retry_error = retry_post(post_id, api_key)
        if retried:
            post = retried
            entry = _platform_entry(post)
            status = entry.get("status") or post.get("status")
            poll_until_terminal()

    if status not in ("published",):
        detail = f"Zernio publish did not complete (status={status}; {_platform_detail(entry)})."
        if retry_error:
            detail += f" Retry attempt: {retry_error}"
        fail(detail, post_id=post_id, platform_status=entry,
             retry_attempted=retry_attempted, poll_error=poll_error,
             ambiguous=status not in ("failed", "error", "partial"))
        return

    emit({"status": "uploaded", "platform": "youtube", "via": "zernio",
          "post_id": post_id, "url": entry.get("platformPostUrl")})


if __name__ == "__main__":
    main()
