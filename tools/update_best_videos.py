"""Track the MOST SUCCESSFUL video per platform workflow (YouTube + Instagram).

Pulls post analytics for both Zernio accounts (same publish path the daily
pipeline uses), picks each platform's best-performing video, and stores it --
with a metrics-backed explanation of WHY it won -- in
`state/best_videos.json`. run_daily.py calls this at the end of every real run
so the store stays fresh automatically; it can also be run standalone.

Success ranking: views first, then engagement_rate, then recency (newer wins a
tie). The "why" is computed from the winner's own numbers vs the account's
medians -- retention ratio, engagement percentile, and which hook patterns the
winning title actually uses (specific number, curiosity gap, transformation
tease) -- so the rationale is always grounded in data, not vibes.

Account ids resolve in order: $ZERNIO_YOUTUBE_ID / $ZERNIO_INSTAGRAM_ID env
vars when they look like valid ids (24-hex), otherwise auto-discovered via
GET /v1/accounts. This heals a stale/placeholder env value instead of failing.

Usage:
    python tools/update_best_videos.py [--limit 100] [--out state/best_videos.json]

Prints JSON: {"updated_at","platforms":{...}} on success; {"error": ...} + exit 1.
"""
import argparse
import datetime
import json
import os
import re

from _common import load_env, emit, fail

ZERNIO_API = "https://zernio.com/api/v1"
PLATFORMS = ("youtube", "instagram")
HEX24 = re.compile(r"^[0-9a-f]{24}$")

# Hook patterns that measurably drive short-form retention/click-through.
HOOK_PATTERNS = [
    ("specific_number", re.compile(r"\d")),
    ("curiosity_gap", re.compile(r"\b(what|why|how|who|which|secret|nobody|until)\b", re.I)),
    ("big_stakes", re.compile(r"\$?\d[\d,.]*\s*(k|000)|\$1,000,000|\$250", re.I)),
    ("transformation_tease", re.compile(r"\b(until this happened|then this|you won'?t believe|turns? into)\b", re.I)),
    ("conflict_or_challenge", re.compile(r"\b(fight|vs\.?|versus|attack|survive|last to|battle|challenge)\b", re.I)),
]


def _get(api_key, path, params=None):
    import httpx

    r = httpx.get(f"{ZERNIO_API}{path}", params=params or {},
                  headers={"Authorization": f"Bearer {api_key}"}, timeout=60)
    if r.status_code == 402:
        fail("Zernio Analytics add-on is required on this plan -- enable/upgrade it "
             "in the Zernio dashboard, then re-run.", code="analytics_addon_required")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _as_list(d):
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for key in ("accounts", "data", "results", "items"):
            if isinstance(d.get(key), list):
                return d[key]
    return []


def resolve_account_ids(api_key):
    """env var first (when it looks like a real id), else discover via /accounts."""
    out = {}
    accounts = _as_list(_get(api_key, "/accounts") or {})
    by_platform = {}
    for acc in accounts:
        plat = acc.get("platform")
        aid = acc.get("_id") or acc.get("id")
        if plat in PLATFORMS and aid and HEX24.match(str(aid)):
            by_platform[plat] = str(aid)  # last valid wins; /v1/analytics wants the Mongo id
    for plat, env_name in (("youtube", "ZERNIO_YOUTUBE_ID"),
                           ("instagram", "ZERNIO_INSTAGRAM_ID")):
        env_val = (os.environ.get(env_name) or "").strip()
        # A placeholder/stale value (e.g. "zernio_yt_...") must not poison lookups.
        out[plat] = env_val if HEX24.match(env_val) else by_platform.get(plat)
    return out


def fetch_posts(api_key, platform, account_id, limit):
    posts = []
    for page in range(1, 10):  # 100/page hard ceiling per request; paginate defensively
        d = _get(api_key, "/analytics",
                 {"platform": platform, "accountId": account_id, "limit": 100,
                  "page": page, "sortBy": "date", "order": "desc"}) or {}
        batch = d if isinstance(d, list) else (d.get("posts") or [])
        posts.extend(batch)
        if len(batch) < 100:
            break
    return posts[:limit]


def normalize(post):
    plat0 = (post.get("platforms") or [{}])[0]
    a = plat0.get("analytics") or post.get("analytics") or {}
    dur_s = a.get("videoDurationSeconds") or 0
    avg_watch_ms = a.get("igReelsAvgWatchTime") or 0
    content = (post.get("content") or "").strip()
    return {
        "id": post.get("_id"),
        "url": post.get("platformPostUrl") or plat0.get("platformPostUrl"),
        "title": content.split("\n\n")[0][:120],
        "published_at": post.get("publishedAt"),
        "views": a.get("views", 0) or 0,
        "likes": a.get("likes", 0) or 0,
        "comments": a.get("comments", 0) or 0,
        "shares": a.get("shares", 0) or 0,
        "saves": a.get("saves", 0) or 0,
        "engagement_rate": a.get("engagementRate", 0) or 0,
        "duration_seconds": dur_s,
        "avg_watch_time_ms": avg_watch_ms,
        "retention_ratio": round(avg_watch_ms / 1000.0 / dur_s, 3) if dur_s else None,
    }


def explain_why(winner, medians, rank_of_platforms=None):
    """Metrics-backed WHY: what this video does that the typical post doesn't."""
    why = []
    if winner["views"] > 0:
        mult = winner["views"] / medians["views"] if medians["views"] else None
        if mult:
            why.append(f"views {winner['views']} = {mult:.1f}x the account median "
                       f"({medians['views']})")
        else:
            why.append(f"top views of all account posts ({winner['views']}; median is 0)")
    if winner["retention_ratio"] is not None:
        pct = f"{winner['retention_ratio'] * 100:.0f}%"
        why.append(f"average watch time covers ~{pct} of the clip -- strong retention, "
                   "the main distribution signal for Reels/Shorts")
    if winner["engagement_rate"] > 0 and winner["engagement_rate"] >= medians["engagement_rate"]:
        why.append(f"engagement rate {winner['engagement_rate']}% at/above the account "
                   f"median ({medians['engagement_rate']}%)")
    hooks = [name for name, rx in HOOK_PATTERNS if rx.search(winner["title"])]
    if hooks:
        why.append("hook uses proven pattern(s): " + ", ".join(hooks))
    interactions = winner["likes"] + winner["comments"] + winner["shares"] + winner["saves"]
    if interactions:
        why.append(f"{interactions} interactions ({winner['likes']} likes, "
                   f"{winner['saves']} saves, {winner['shares']} shares) signal quality to the algorithm")
    return why


def pick_winner(rows):
    """views desc -> engagement_rate desc -> recency desc."""
    def key(r):
        return (r["views"], r["engagement_rate"], r["published_at"] or "")
    ranked = sorted([r for r in rows], key=key, reverse=True)
    return (ranked[0], ranked[1:]) if ranked else (None, [])


def medians_of(rows):
    if not rows:
        return {"views": 0, "engagement_rate": 0}
    def med(vals):
        v = sorted(vals)
        n = len(v)
        return v[n // 2] if n % 2 else round((v[n // 2 - 1] + v[n // 2]) / 2, 2)
    return {"views": med([r["views"] for r in rows]),
            "engagement_rate": med([r["engagement_rate"] for r in rows])}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100,
                        help="Most recent posts per platform considered (default 100)")
    parser.add_argument("--out", default="state/best_videos.json")
    args = parser.parse_args()

    load_env()
    api_key = (os.environ.get("ZERNIO_API") or "").strip()
    if not api_key:
        fail("ZERNIO_API not set in API.env.")

    ids = resolve_account_ids(api_key)
    missing = [p for p in PLATFORMS if not ids.get(p)]
    if missing:
        fail(f"No valid Zernio account id resolvable for: {', '.join(missing)}. "
             "Fix the env vars or connect the channel in Zernio's dashboard.")

    store_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              args.out)
    try:
        with open(store_path, "r", encoding="utf-8") as f:
            store = json.load(f)
    except (OSError, json.JSONDecodeError):
        store = {}

    result_platforms, errors = {}, []
    for plat in PLATFORMS:
        try:
            rows = [normalize(p) for p in fetch_posts(api_key, plat, ids[plat], args.limit)]
        except Exception as e:
            errors.append({"platform": plat, "error": str(e)})
            continue
        winner, rest = pick_winner(rows)
        meds = medians_of(rows)
        entry = {
            "account_id": ids[plat],
            "posts_analyzed": len(rows),
            "median_views": meds["views"],
            "most_successful_video": None,
            "runner_up_url": rest[0]["url"] if rest else None,
        }
        if winner:
            entry["most_successful_video"] = {
                **{k: winner[k] for k in ("id", "url", "title", "published_at", "views",
                                          "likes", "comments", "shares", "saves",
                                          "engagement_rate", "duration_seconds",
                                          "avg_watch_time_ms", "retention_ratio")},
                "why_most_successful": explain_why(winner, meds),
            }
        result_platforms[plat] = entry

    if not result_platforms:
        fail("Could not fetch analytics for any platform.", details=errors)

    store.update({
        "_note": "Most successful video per platform workflow (YouTube / Instagram), "
                 "auto-refreshed by tools/update_best_videos.py after each real run_daily "
                 "pass. 'why_most_successful' is computed from the winner's metrics vs "
                 "account medians -- see the tool docstring for the ranking rule.",
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "ranking_rule": "views desc, then engagement_rate desc, then recency",
        "platforms": {**store.get("platforms", {}), **result_platforms},
    })
    if errors:
        store["last_errors"] = errors

    os.makedirs(os.path.dirname(store_path), exist_ok=True)
    tmp = store_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, ensure_ascii=False)
    os.replace(tmp, store_path)

    emit({
        "status": "ok",
        "out": args.out,
        "platforms": {p: {
            "most_successful": (result_platforms[p]["most_successful_video"] or {}).get("url"),
            "views": (result_platforms[p]["most_successful_video"] or {}).get("views"),
        } for p in result_platforms},
        "errors": errors,
    })


if __name__ == "__main__":
    main()
