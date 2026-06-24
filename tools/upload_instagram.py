"""Publish a finished Short to Instagram as a Reel.

Supports BOTH Instagram publishing APIs (auto-selected by base URL):
  * "Instagram API with Instagram Login" (DEFAULT, graph.instagram.com): the newer flow. Token is
    an Instagram USER token from the app's "API setup with Instagram business login -> Generate
    access tokens"; permission `instagram_business_content_publish`. IG_USER_ID optional (uses "me").
  * "Instagram API with Facebook Login" (graph.facebook.com): set IG_API_BASE to
    "https://graph.facebook.com/v21.0"; needs a Page-linked IG Business account + IG_USER_ID.

SAFETY GATE: refuses to publish without --confirm (dry-run preview otherwise). Irreversible.

AUTH/SETUP:
  * an Instagram Professional (Business/Creator) account,
  * a Meta app with the `instagram_business_content_publish` permission (App Review for OTHER users;
    your own account works in dev mode),
  * IG_ACCESS_TOKEN (long-lived) in API.env; IG_USER_ID optional on the Instagram-Login API.
  * Optional override: IG_API_BASE (defaults to https://graph.instagram.com/v21.0).

Instagram fetches the video from a PUBLIC url, so pass --video-url (use host_public.py to get one).
Reels must be 9:16, 5-90s, H.264/HEVC, audio baked in (IG music library isn't available via API) —
our pipeline already complies.

Flow: POST /{ig-id}/media (media_type=REELS, video_url, caption) -> poll the container's
status_code until FINISHED -> POST /{ig-id}/media_publish (creation_id).

Usage:
    python tools/upload_instagram.py --video-url https://... --caption "..." [--confirm]

Prints JSON: dry run -> {"status":"preview",...}; real -> {"status":"uploaded","media_id",...}.
"""
import argparse
import os
import time

from _common import load_env, emit, fail

def resolve_base(node_id):
    """Pick the Graph host. An explicit IG_API_BASE wins; otherwise infer from the id shape:
    a numeric IG_USER_ID means the Facebook-login flow (graph.facebook.com); anything else
    (e.g. node 'me') means the newer Instagram-login flow (graph.instagram.com). This lets the
    cloud runner publish without the workflow having to set IG_API_BASE."""
    base = os.environ.get("IG_API_BASE", "").strip().rstrip("/")
    if base:
        return base
    return "https://graph.facebook.com/v21.0" if node_id.isdigit() else "https://graph.instagram.com/v21.0"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-url", required=True, help="PUBLIC https url to the mp4 (host_public.py)")
    parser.add_argument("--caption", default="", help="Caption incl. hashtags")
    parser.add_argument("--confirm", action="store_true", help="Required to actually publish.")
    parser.add_argument("--poll-timeout", type=int, default=180)
    args = parser.parse_args()

    load_env()
    token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    # IG_USER_ID is optional on the Instagram-Login API ("me" works); required on the FB-login API.
    node = os.environ.get("IG_USER_ID", "").strip() or "me"
    graph = resolve_base(node)
    if not token:
        fail("IG_ACCESS_TOKEN not set in API.env. Get one from your app's "
             "'API setup with Instagram business login -> Generate access tokens', then add the "
             "instagram_business_content_publish permission (App Review for other users).")
        return

    if not args.confirm:
        emit({
            "status": "preview", "would_upload": True, "platform": "instagram",
            "api_base": graph, "node": node,
            "video_url": args.video_url, "caption": args.caption,
            "note": "DRY RUN. Re-run with --confirm to publish.",
        })
        return

    import httpx

    # 1) create the Reels container.
    try:
        r = httpx.post(f"{graph}/{node}/media",
                       data={"media_type": "REELS", "video_url": args.video_url,
                             "caption": args.caption, "access_token": token},
                       timeout=60)
        r.raise_for_status()
        creation_id = r.json().get("id")
        if not creation_id:
            fail(f"Instagram container create returned no id: {r.json()}")
            return
    except Exception as e:
        fail(f"Instagram container create failed: {e}")
        return

    # 2) poll until the container finished transcoding.
    status, deadline = None, time.time() + args.poll_timeout
    while time.time() < deadline:
        try:
            s = httpx.get(f"{graph}/{creation_id}",
                          params={"fields": "status_code", "access_token": token}, timeout=30)
            s.raise_for_status()
            status = s.json().get("status_code")
            if status in ("FINISHED", "ERROR", "EXPIRED"):
                break
        except Exception:
            pass
        time.sleep(5)
    if status != "FINISHED":
        fail(f"Instagram container not ready (status={status}).", creation_id=creation_id)
        return

    # 3) publish.
    try:
        p = httpx.post(f"{graph}/{node}/media_publish",
                       data={"creation_id": creation_id, "access_token": token}, timeout=60)
        p.raise_for_status()
        media_id = p.json().get("id")
    except Exception as e:
        fail(f"Instagram publish failed: {e}", creation_id=creation_id)
        return

    emit({"status": "uploaded", "platform": "instagram",
          "media_id": media_id, "creation_id": creation_id})


if __name__ == "__main__":
    main()
