"""Fetch Instagram post performance via Zernio's analytics API -- the same
service both projects publish through (upload_instagram.py), not raw Meta
Graph API, so this reuses the existing ZERNIO_API key rather than a new one.

Used by check_style_experiment.py to compare an experimental post's
performance against a baseline of recent normal posts on the same account.

Zernio Analytics is a paid add-on on some plans -- a 402 here means Moemen
needs to enable/upgrade it in the Zernio dashboard, not a bug in this tool.
Post-level insights can also lag ~24h on Instagram's side (Zernio returns 202
while still syncing) -- callers should tolerate that and retry later.

Usage:
    python tools/ig_fetch_analytics.py --post-id <zernio_or_external_post_id>
    python tools/ig_fetch_analytics.py --account-id <zernio_account_id> --limit 8

Prints JSON: single post -> {"post_id","views","likes","comments","shares",
"saves","impressions","reach","engagement_rate","sync_status","published_at"};
list -> {"posts":[...]}; pending sync -> {"sync_status":"pending","post_id":...}.
"""
import argparse
import os

from _common import load_env, emit, fail

ZERNIO_API = "https://zernio.com/api/v1"


def _normalize(post):
    a = post.get("analytics") or {}
    # The single-post lookup (?postId=) returns top-level "postId"/"syncStatus"; the list
    # endpoint (?accountId=) instead returns Mongo-style "_id" with "syncStatus" nested one
    # level down under platforms[0] -- verified against the real API 2026-07-12 (docs' example
    # response didn't match the list shape). Fall back through both.
    plat = (post.get("platforms") or [{}])[0]
    return {
        "post_id": post.get("postId") or post.get("_id"),
        "platform_post_url": post.get("platformPostUrl") or plat.get("platformPostUrl"),
        "published_at": post.get("publishedAt"),
        "views": a.get("views", 0),
        "likes": a.get("likes", 0),
        "comments": a.get("comments", 0),
        "shares": a.get("shares", 0),
        "saves": a.get("saves", 0),
        "impressions": a.get("impressions", 0),
        "reach": a.get("reach", 0),
        "engagement_rate": a.get("engagementRate", 0),
        "sync_status": post.get("syncStatus") or plat.get("syncStatus"),
    }


def _extract_list(data):
    if isinstance(data, list):
        return data
    for key in ("data", "posts", "results", "items"):
        if isinstance(data.get(key), list):
            return data[key]
    # Some responses may come back as a single post object even for a "list" query.
    return [data] if (data.get("postId") or data.get("_id")) else []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--post-id", default=None)
    parser.add_argument("--account-id", default=None)
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()

    load_env()
    api_key = (os.environ.get("ZERNIO_API") or os.environ.get("ZERNIO_API_KEY") or "").strip()
    if not api_key:
        fail("ZERNIO_API/ZERNIO_API_KEY not set in API.env.")
        return
    if not args.post_id and not args.account_id:
        fail("Pass --post-id (single post) or --account-id (recent list).")
        return

    params = {"platform": "instagram"}
    if args.post_id:
        params["postId"] = args.post_id
    if args.account_id:
        params.update({"accountId": args.account_id, "limit": args.limit,
                        "sortBy": "date", "order": "desc"})

    import httpx
    try:
        r = httpx.get(f"{ZERNIO_API}/analytics", params=params,
                       headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
    except Exception as e:
        fail(f"Zernio analytics request failed: {e}")
        return

    if r.status_code == 402:
        fail("Zernio Analytics add-on is required on this plan -- enable/upgrade it in the "
             "Zernio dashboard, then re-run.", code="analytics_addon_required")
        return
    if r.status_code == 202:
        emit({"sync_status": "pending", "post_id": args.post_id})
        return
    if r.status_code == 424:
        fail("Zernio analytics sync failed for this post on all platforms.", post_id=args.post_id)
        return
    try:
        r.raise_for_status()
    except Exception as e:
        fail(f"Zernio analytics failed: {e} {r.text[:200]}")
        return

    data = r.json()
    if args.post_id:
        emit(_normalize(data))
    else:
        emit({"posts": [_normalize(p) for p in _extract_list(data)]})


if __name__ == "__main__":
    main()
