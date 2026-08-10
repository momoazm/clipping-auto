"""Publish a finished Short to Instagram as a Reel -- via Zernio (zernio.com), not Meta's Graph
API directly.

Why Zernio instead of our own Meta app: posting through your OWN Facebook App requires either
(a) Advanced Access via Meta App Review + Business Verification (real registered-business
documents Moemen doesn't have), or (b) staying in Development Mode with the posting account
added as an App Tester/Admin -- and even then a System User token (Business Manager construct)
still hits the same Advanced Access wall. Zernio already completed App Review and Business
Verification under THEIR app; you authorize via a normal "Continue with Facebook" OAuth consent
screen on zernio.com, and Zernio's API takes it from there. Free tier covers this project's
volume (first 2 connected accounts, unlimited posts).

SAFETY GATE: refuses to publish without --confirm (dry-run preview otherwise). Irreversible.

AUTH/SETUP:
  * ZERNIO_API in API.env (the GitHub secret name already wired into this repo's workflow).
  * ZERNIO_INSTAGRAM_ID -- the Zernio-internal id for the connected Instagram account (NOT the
    same as a Meta IG user id; fetched once via GET /v1/accounts after connecting in Zernio's
    dashboard).

Instagram still fetches the video from a PUBLIC url (Zernio just proxies the same Graph API
container-create/poll/publish flow under the hood) -- pass --video-url (host_public.py).

Usage:
    python tools/upload_instagram.py --video-url https://... --caption "..." [--confirm]

Prints JSON: dry run -> {"status":"preview",...}; real -> {"status":"uploaded","post_id",...}.
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
        if entry.get("platform") == "instagram":
            return entry
    return {}


def _platform_detail(entry):
    details = {key: entry.get(key) for key in ("error", "code", "message", "platformError")
               if entry.get(key) not in (None, "", {})}
    if not details:
        return "provider returned no Instagram error detail"
    return json.dumps(details, ensure_ascii=True, separators=(",", ":"))[:600]


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


def create_post(payload, api_key, max_tries=5):
    """Create a Zernio post with bounded transient retries and structured errors.

    A provider response that explicitly rate-limits the connected account is not transient:
    retrying it for several minutes only creates noise and can extend the cooldown. A duplicate
    response is also safe to treat as delivered when Zernio gives us the existing post id.
    """
    import httpx
    backoff = 10
    last = None
    request_id = str(uuid.uuid4())
    for attempt in range(max_tries):
        try:
            r = httpx.post(f"{ZERNIO_API}/posts", json=payload,
                           headers={"Authorization": f"Bearer {api_key}",
                                    "x-request-id": request_id}, timeout=60)
            try:
                body = r.json()
            except Exception:
                body = {}
            details = body.get("details") if isinstance(body, dict) else {}
            details = details if isinstance(details, dict) else {}

            if r.status_code == 429:
                message = body.get("error") if isinstance(body, dict) else None
                return None, f"Zernio HTTP 429: {message or 'account temporarily rate-limited'}", {
                    "rate_limited": True,
                    "status_code": 429,
                    "rate_limited_until": details.get("rateLimitedUntil"),
                }

            # Account/plan/auth errors are permanent for this run. Retrying a 403 five times
            # only delayed the workflow and hid the actionable response body in the old code.
            if r.status_code == 409:
                message = body.get("error") if isinstance(body, dict) else None
                return None, f"Zernio HTTP 409: {message or 'duplicate content'}", {
                    "duplicate": True,
                    "status_code": 409,
                    "existing_post_id": details.get("existingPostId"),
                }
            if 400 <= r.status_code < 500:
                body = r.text[:320].strip()
                return None, f"Zernio HTTP {r.status_code}: {body or 'request rejected'}", {}
            if r.status_code >= 500:
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                retry_after = (r.headers.get("Retry-After") or "").strip()
                if attempt < max_tries - 1:
                    time.sleep(min(120, int(retry_after) if retry_after.isdigit() else backoff))
                    backoff *= 2
                    continue
            r.raise_for_status()
            return (body.get("post") or body.get("existingPost") or {}) if isinstance(body, dict) else {}, None, {}
        except Exception as exc:
            last = str(exc)
            if attempt < max_tries - 1:
                time.sleep(min(120, backoff))
                backoff *= 2
                continue
            break
    return None, f"Zernio post create failed after {max_tries} tries ({last})", {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-url", required=True, help="PUBLIC https url to the mp4 (host_public.py)")
    parser.add_argument("--caption", default="", help="Caption incl. hashtags")
    parser.add_argument("--confirm", action="store_true", help="Required to actually publish.")
    parser.add_argument("--poll-timeout", type=int, default=180)
    args = parser.parse_args()

    load_env()
    api_key = (os.environ.get("ZERNIO_API") or os.environ.get("ZERNIO_API_KEY") or "").strip()
    account_id = os.environ.get("ZERNIO_INSTAGRAM_ID", "").strip()
    if not api_key:
        fail("ZERNIO_API not set in API.env. Sign up free at zernio.com and grab it from "
             "Settings -> API Keys.")
        return
    if not account_id:
        fail("ZERNIO_INSTAGRAM_ID not set in API.env. After connecting the Instagram account "
             "in Zernio's dashboard, fetch it via GET /v1/accounts.")
        return

    payload = {
        "content": args.caption,
        "mediaItems": [{"type": "video", "url": args.video_url}],
        "platforms": [{
            "platform": "instagram",
            "accountId": account_id,
            "platformSpecificData": {"contentType": "reels", "shareToFeed": True},
        }],
        "publishNow": True,
    }

    if not args.confirm:
        emit({
            "status": "preview", "would_upload": True, "platform": "instagram",
            "via": "zernio", "account_id": account_id,
            "video_url": args.video_url, "caption": args.caption,
            "note": "DRY RUN. Re-run with --confirm to publish.",
        })
        return

    import httpx

    post, create_error, create_meta = create_post(payload, api_key)
    if create_error:
        if create_meta.get("duplicate") and create_meta.get("existing_post_id"):
            emit({"status": "already_published", "platform": "instagram", "via": "zernio",
                  "post_id": create_meta["existing_post_id"], "duplicate": True})
            return
        if create_meta.get("rate_limited"):
            fail(create_error, platform="instagram", status_code=create_meta.get("status_code"),
                 rate_limited=True, rate_limited_until=create_meta.get("rate_limited_until"))
            return
        fail(create_error)
        return

    post_id = post.get("_id")
    if not post_id:
        fail(f"Zernio post create returned no post id: {post}")
        return

    entry = _platform_entry(post)
    status = entry.get("status") or post.get("status")

    # publishNow:true is meant to include the URL immediately, but Instagram-side processing
    # (container transcode) can take up to ~2 min -- poll the same way the old direct-Graph-API
    # version did if it isn't done yet.
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
        fail(detail, post_id=post_id, platform_status=entry, poll_error=poll_error,
             retry_attempted=retry_attempted,
             ambiguous=status not in ("failed", "error", "partial"))
        return

    emit({"status": "uploaded", "platform": "instagram", "via": "zernio",
          "post_id": post_id, "media_url": entry.get("platformPostUrl")})


if __name__ == "__main__":
    main()
