"""Publish a finished Short to TikTok with the Content Posting API.

The clipping workflow previously had no TikTok delivery path at all. This tool mirrors the
ranking-shorts uploader: it uses FILE_UPLOAD, streams bounded chunks instead of reading the whole
MP4 into memory, retries transient API errors, and only reports success after TikTok confirms the
publish. Run without ``--confirm`` for a safe preview.
"""
import argparse
import math
import os
import time

from _common import emit, fail, load_env

API = "https://open.tiktokapis.com/v2"
PRIVACY = ["SELF_ONLY", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "PUBLIC_TO_EVERYONE"]
MAX_CHUNK = 64 * 1024 * 1024


def delay(attempt, response=None):
    retry_after = (response.headers.get("Retry-After") if response is not None else "") or ""
    return min(30, max(1, int(retry_after))) if retry_after.isdigit() else min(30, 2 ** attempt)


def response_error(response):
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            return f"{error.get('code', 'api_error')}: {error.get('message', '')}".strip(": ")
        if isinstance(body, dict) and body.get("message"):
            return str(body["message"])
    except Exception:
        pass
    return response.text[:240].strip()


def access_token():
    import httpx

    token = os.environ.get("TIKTOK_ACCESS_TOKEN", "").strip()
    refresh = os.environ.get("TIKTOK_REFRESH_TOKEN", "").strip()
    key = os.environ.get("TIKTOK_CLIENT_KEY", "").strip()
    secret = os.environ.get("TIKTOK_CLIENT_SECRET", "").strip()
    if refresh and key and secret:
        try:
            r = httpx.post(f"{API}/oauth/token/", data={
                "client_key": key, "client_secret": secret,
                "grant_type": "refresh_token", "refresh_token": refresh,
            }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
            r.raise_for_status()
            new = r.json().get("access_token")
            if new:
                return new
        except Exception:
            pass
    return token


def put_chunk(client, url, data, start, total):
    end = start + len(data) - 1
    headers = {"Content-Type": "video/mp4", "Content-Length": str(len(data)),
               "Content-Range": f"bytes {start}-{end}/{total}"}
    for attempt in range(4):
        try:
            r = client.put(url, content=data, headers=headers, timeout=300)
            if 200 <= r.status_code < 300:
                return
            if r.status_code != 429 and r.status_code < 500:
                raise RuntimeError(f"HTTP {r.status_code}: {response_error(r)}")
            if attempt == 3:
                raise RuntimeError(f"HTTP {r.status_code}: {response_error(r)}")
            time.sleep(delay(attempt, r))
        except RuntimeError:
            raise
        except Exception as exc:
            if attempt == 3:
                raise RuntimeError(str(exc)) from exc
            time.sleep(delay(attempt))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--privacy", choices=PRIVACY, default="SELF_ONLY")
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--poll-timeout", type=int, default=180)
    args = ap.parse_args()

    load_env()
    if not os.path.isfile(args.video):
        fail(f"Video not found: {args.video}")
    token = access_token()
    if not token:
        fail("No TikTok access token or usable refresh credentials. Configure "
             "TIKTOK_ACCESS_TOKEN or TIKTOK_REFRESH_TOKEN + client credentials.")
    size = os.path.getsize(args.video)
    if size <= 0:
        fail("TikTok video is empty.")
    if not args.confirm:
        emit({"status": "preview", "would_upload": True, "platform": "tiktok",
              "title": args.title, "privacy": args.privacy, "video": args.video,
              "size_bytes": size})
        return

    import httpx
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    chunk_size = min(MAX_CHUNK, size)
    body = {"post_info": {"title": args.title[:2200], "privacy_level": args.privacy,
                           "disable_comment": False, "disable_duet": False,
                           "disable_stitch": False},
            "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                            "chunk_size": chunk_size,
                            "total_chunk_count": max(1, math.ceil(size / chunk_size))}}
    with httpx.Client() as client:
        publish_id = upload_url = None
        last = None
        for attempt in range(4):
            try:
                r = client.post(f"{API}/post/publish/video/init/", headers=headers,
                                json=body, timeout=60)
                if r.status_code == 429 or r.status_code >= 500:
                    last = f"HTTP {r.status_code}: {response_error(r)}"
                    if attempt < 3:
                        time.sleep(delay(attempt, r))
                        continue
                r.raise_for_status()
                data = r.json().get("data", {})
                publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
                if publish_id and upload_url:
                    break
                last = response_error(r) or "no upload URL"
            except Exception as exc:
                last = str(exc)
            if attempt < 3:
                time.sleep(delay(attempt))
        if not publish_id or not upload_url:
            fail(f"TikTok init failed after retries: {last or 'no upload URL'}")

        try:
            with open(args.video, "rb") as f:
                offset = 0
                while offset < size:
                    data = f.read(min(chunk_size, size - offset))
                    if not data:
                        raise RuntimeError(f"unexpected EOF at byte {offset}")
                    put_chunk(client, upload_url, data, offset, size)
                    offset += len(data)
        except Exception as exc:
            fail(f"TikTok byte upload failed: {exc}", publish_id=publish_id)

        status, last_error = None, None
        deadline = time.time() + args.poll_timeout
        while time.time() < deadline:
            try:
                r = client.post(f"{API}/post/publish/status/fetch/", headers=headers,
                                json={"publish_id": publish_id}, timeout=30)
                r.raise_for_status()
                status = (r.json().get("data", {}) or {}).get("status")
                if status in ("PUBLISH_COMPLETE", "FAILED"):
                    break
                last_error = None
            except Exception as exc:
                last_error = str(exc)
            time.sleep(5)
        if status == "PUBLISH_COMPLETE":
            emit({"status": "uploaded", "platform": "tiktok", "publish_id": publish_id,
                  "tiktok_status": status, "privacy": args.privacy})
        elif status == "FAILED":
            fail("TikTok rejected the video during processing.", publish_id=publish_id,
                 tiktok_status=status)
        else:
            fail("TikTok publish status timed out; the post may still be processing. "
                 "Do not immediately retry with the same video.", publish_id=publish_id,
                 tiktok_status=status, poll_error=last_error, ambiguous=True)


if __name__ == "__main__":
    main()
