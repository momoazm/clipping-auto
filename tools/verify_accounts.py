"""Fail-closed destination-account checks for the MrBeast clipping workflow.

The platform account IDs live in GitHub secrets, so a workflow can otherwise publish to a
different connected account without the repository making that visible. This check resolves
the configured Zernio IDs and the TikTok creator identity before any media upload begins.
It prints usernames only; credentials and opaque account IDs are never logged.
"""
import argparse
import json
import os
from pathlib import Path
import sys


ZERNIO_ACCOUNTS = "https://zernio.com/api/v1/accounts"
TIKTOK_TOKEN = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_CREATOR = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"


def normalize(value):
    return str(value or "").strip().lstrip("@").lower()


def stop(message):
    print(json.dumps({"error": message}, ensure_ascii=True), file=sys.stderr)
    raise SystemExit(1)


def account_id(account):
    for key in ("_id", "id", "accountId"):
        if account.get(key):
            return str(account[key])
    return ""


def account_name(account):
    return normalize(account.get("username") or account.get("handle") or account.get("displayName"))


def account_readiness(account):
    """Return an explicit provider readiness signal without guessing missing fields."""
    bad = {"disconnected", "disconnect", "inactive", "disabled", "expired", "error", "failed", "revoked"}
    good = {"connected", "active", "ready", "ok"}
    for key in ("status", "connectionStatus", "connection_state", "state"):
        value = normalize(account.get(key))
        if value in bad:
            return "not_ready", f"{key}={value}"
        if value in good:
            return "ready", f"{key}={value}"
    for key in ("connected", "isConnected", "active", "enabled"):
        if key in account and account.get(key) is False:
            return "not_ready", f"{key}=false"
        if key in account and account.get(key) is True:
            return "ready", f"{key}=true"
    return "unknown", "provider did not expose a readiness field"


def zernio_accounts():
    import httpx

    api_key = (os.environ.get("ZERNIO_API_KEY") or os.environ.get("ZERNIO_API") or "").strip()
    if not api_key:
        stop("Zernio account check: no ZERNIO_API_KEY/ZERNIO_API configured")
    try:
        response = httpx.get(ZERNIO_ACCOUNTS,
                             headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        stop(f"Zernio account check failed: {type(exc).__name__}")
    accounts = body.get("accounts", []) if isinstance(body, dict) else []
    if isinstance(accounts, dict):
        accounts = accounts.get("items") or accounts.get("data") or []
    if not isinstance(accounts, list):
        stop("Zernio account check returned an unexpected account list")
    return accounts


def verify_zernio(accounts, platform, expected, env_key, require_ready=False):
    configured = os.environ.get(env_key, "").strip()
    if not configured:
        stop(f"Zernio account check: {env_key} is empty")
    match = next((a for a in accounts
                  if isinstance(a, dict) and account_id(a) == configured), None)
    if not match:
        stop(f"Zernio account check: {env_key} is not among the connected accounts")
    actual_platform = normalize(match.get("platform"))
    actual_name = account_name(match)
    if actual_platform != platform:
        stop(f"Zernio account check: {env_key} is connected as {actual_platform or 'unknown'}, "
             f"not {platform}")
    if expected and actual_name != normalize(expected):
        stop(f"Zernio account check: {platform} is @{actual_name or 'unknown'}, "
              f"expected @{normalize(expected)}")
    readiness_state, readiness_detail = account_readiness(match)
    if require_ready and readiness_state == "not_ready":
        stop(f"Zernio account check: {platform} @{actual_name} is not ready ({readiness_detail})")
    return actual_name, {"state": readiness_state, "detail": readiness_detail}


def persist_tiktok_tokens(token, refresh=None):
    """Keep a refreshed token available to the later upload step on this runner."""
    env_path = Path("API.env")
    if not env_path.is_file():
        return
    values = {"TIKTOK_ACCESS_TOKEN": token}
    if refresh:
        values["TIKTOK_REFRESH_TOKEN"] = refresh
    lines = []
    seen = set()
    for line in env_path.read_text(encoding="utf-8").splitlines():
        key, separator, _ = line.partition("=")
        if separator and key in values:
            if key not in seen:
                lines.append(f"{key}={values[key]}")
                seen.add(key)
        else:
            lines.append(line)
    for key, value in values.items():
        if key not in seen:
            lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def refresh_access_token():
    import httpx

    refresh = os.environ.get("TIKTOK_REFRESH_TOKEN", "").strip()
    key = os.environ.get("TIKTOK_CLIENT_KEY", "").strip()
    secret = os.environ.get("TIKTOK_CLIENT_SECRET", "").strip()
    if not (refresh and key and secret):
        return None
    try:
        response = httpx.post(
            TIKTOK_TOKEN,
            data={"client_key": key, "client_secret": secret,
                  "grant_type": "refresh_token", "refresh_token": refresh},
            headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30,
        )
        response.raise_for_status()
        payload = response.json() or {}
        token = str(payload.get("access_token") or "").strip()
        if not token:
            return None
        new_refresh = str(payload.get("refresh_token") or "").strip() or refresh
        os.environ["TIKTOK_ACCESS_TOKEN"] = token
        os.environ["TIKTOK_REFRESH_TOKEN"] = new_refresh
        persist_tiktok_tokens(token, new_refresh)
        return token
    except Exception:
        return None


def access_token():
    token = os.environ.get("TIKTOK_ACCESS_TOKEN", "").strip()
    if token:
        return token
    if not any(os.environ.get(key, "").strip() for key in
               ("TIKTOK_REFRESH_TOKEN", "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET")):
        return None
    token = refresh_access_token()
    if not token:
        stop("TikTok account check: no access token or usable refresh credentials")
    return token


def creator_info(token):
    import httpx

    try:
        response = httpx.post(
            TIKTOK_CREATOR,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=30,
        )
        try:
            body = response.json()
        except Exception:
            body = None
        return response, body
    except Exception:
        return None, None


def verify_tiktok(expected):
    token = access_token()
    if not token:
        return None
    response, body = creator_info(token)
    error = body.get("error") if isinstance(body, dict) else None
    failed = (response is None or response.status_code >= 400 or
              (isinstance(error, dict) and error.get("code") not in (None, "", "ok", 0)))
    if failed:
        refreshed = refresh_access_token()
        if refreshed and refreshed != token:
            response, body = creator_info(refreshed)
            error = body.get("error") if isinstance(body, dict) else None
            token = refreshed
        error_code = error.get("code") if isinstance(error, dict) else None
        if (response is None or response.status_code >= 400 or
                error_code not in (None, "", "ok", 0)):
            stop("TikTok creator check failed or returned an API error")
    creator = (body.get("data") or {}) if isinstance(body, dict) else {}
    actual = normalize(creator.get("creator_username"))
    if not actual:
        stop("TikTok creator check returned no creator username")
    if expected and actual != normalize(expected):
        stop(f"TikTok creator is @{actual}, expected @{normalize(expected)}")
    return actual


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instagram", default=None, help="Expected Instagram username")
    parser.add_argument("--youtube", default=None, help="Expected YouTube username")
    parser.add_argument("--tiktok", default=os.environ.get("TIKTOK_EXPECTED_USERNAME"),
                        help="Expected TikTok username")
    parser.add_argument("--require-ready", action="store_true",
                        help="Fail when Zernio explicitly reports a disconnected account")
    args = parser.parse_args()

    try:
        from _common import load_env
        load_env()
    except Exception:
        pass

    accounts = zernio_accounts()
    verified = {}
    readiness = {}
    if args.instagram:
        actual, info = verify_zernio(accounts, "instagram", args.instagram,
                                     "ZERNIO_INSTAGRAM_ID", args.require_ready)
        verified["instagram"] = "@" + actual
        readiness["instagram"] = info
    if args.youtube:
        actual, info = verify_zernio(accounts, "youtube", args.youtube,
                                     "ZERNIO_YOUTUBE_ID", args.require_ready)
        verified["youtube"] = "@" + actual
        readiness["youtube"] = info
    if args.tiktok:
        actual = verify_tiktok(args.tiktok)
        verified["tiktok"] = ("@" + actual) if actual else "skipped: credentials not configured"
    print(json.dumps({"status": "verified", "accounts": verified, "readiness": readiness}, ensure_ascii=True))


if __name__ == "__main__":
    main()
