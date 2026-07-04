"""Expose a local video at a PUBLIC https URL.

Instagram's Graph API (and our Zernio YouTube path) can't take a local file — they fetch the
video from a public `video_url`. This uploads the mp4 to a free, keyless file host and returns
the direct link.

Reliability notes (2026-07-04): the previous catbox->0x0 chain broke on GitHub's datacenter IPs
(catbox's Cloudflare WAF returns 412 to the default python-httpx User-Agent; 0x0 timed out), which
silently stopped ALL posting. Fixes: send a real browser User-Agent, and try SEVERAL independent
hosts so one bad/blocked host doesn't sink the run. A whole-chain failure is still surfaced so the
caller can skip delivery this run.

Usage:
    python tools/host_public.py --video .tmp/final.mp4

Prints JSON: {"url": "https://...", "provider": "..."}
"""
import argparse
import os

from _common import load_env, emit, fail

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _client():
    import httpx
    # A browser UA is what gets past catbox/0x0's Cloudflare WAF on datacenter IPs (the default
    # python-httpx UA is 412'd). follow_redirects so hosts that 302 to a CDN still resolve.
    return httpx.Client(timeout=180, follow_redirects=True,
                        headers={"User-Agent": UA, "Accept": "*/*"})


def upload_catbox(path):
    with open(path, "rb") as f, _client() as c:
        r = c.post("https://catbox.moe/user/api.php",
                   data={"reqtype": "fileupload"},
                   files={"fileToUpload": (os.path.basename(path), f, "video/mp4")},
                   headers={"Referer": "https://catbox.moe/", "Origin": "https://catbox.moe"})
    r.raise_for_status()
    url = r.text.strip()
    if not url.startswith("http"):
        raise RuntimeError(f"catbox returned no URL: {url[:200]}")
    return url


def upload_litterbox(path):
    """catbox's TEMPORARY host (72h). Same API; often works when the permanent host is finicky."""
    with open(path, "rb") as f, _client() as c:
        r = c.post("https://litterbox.catbox.moe/resources/internals/api.php",
                   data={"reqtype": "fileupload", "time": "72h"},
                   files={"fileToUpload": (os.path.basename(path), f, "video/mp4")})
    r.raise_for_status()
    url = r.text.strip()
    if not url.startswith("http"):
        raise RuntimeError(f"litterbox returned no URL: {url[:200]}")
    return url


def upload_tmpfiles(path):
    with open(path, "rb") as f, _client() as c:
        r = c.post("https://tmpfiles.org/api/v1/upload",
                   files={"file": (os.path.basename(path), f, "video/mp4")})
    r.raise_for_status()
    url = (r.json().get("data") or {}).get("url", "")
    if not url.startswith("http"):
        raise RuntimeError(f"tmpfiles returned no URL: {str(r.text)[:200]}")
    # The page URL is https://tmpfiles.org/<id>/<name>; the DIRECT download is /dl/<id>/<name>.
    return url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1)


def upload_uguu(path):
    """Temporary host (files expire ~3h) -- fine, the fetch happens within seconds."""
    with open(path, "rb") as f, _client() as c:
        r = c.post("https://uguu.se/upload.php",
                   files={"files[]": (os.path.basename(path), f, "video/mp4")})
    r.raise_for_status()
    files = (r.json() or {}).get("files") or []
    if not files or not files[0].get("url", "").startswith("http"):
        raise RuntimeError(f"uguu returned no URL: {str(r.text)[:200]}")
    return files[0]["url"]


def upload_0x0(path):
    with open(path, "rb") as f, _client() as c:
        r = c.post("https://0x0.st", files={"file": (os.path.basename(path), f, "video/mp4")})
    r.raise_for_status()
    url = r.text.strip()
    if not url.startswith("http"):
        raise RuntimeError(f"0x0.st returned no URL: {url[:200]}")
    return url


# Order = most-reliable-first observed from CI. Temporary hosts are fine: the consumer fetches
# the file within seconds of us returning the URL.
PROVIDERS = [
    ("catbox", upload_catbox),
    ("litterbox", upload_litterbox),
    ("tmpfiles", upload_tmpfiles),
    ("uguu", upload_uguu),
    ("0x0", upload_0x0),
]


def host(path):
    """Return (url, provider). Raises RuntimeError if the whole chain fails."""
    errors = {}
    for name, fn in PROVIDERS:
        try:
            return fn(path), name
        except Exception as e:
            errors[name] = str(e)[:160]
    raise RuntimeError(f"All public file hosts failed: {errors}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True)
    args = parser.parse_args()
    load_env()
    if not os.path.isfile(args.video):
        fail(f"Video not found: {args.video}")
        return
    try:
        url, provider = host(args.video)
    except RuntimeError as e:
        fail(str(e))
        return
    emit({"url": url, "provider": provider})


if __name__ == "__main__":
    main()
