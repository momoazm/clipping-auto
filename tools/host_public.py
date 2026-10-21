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


def verify_public_url(url, expected_size=0, timeout=45):
    """Raise unless `url` really serves this MP4 (not an HTML error page / login wall).

    Instagram reported "Video Transcoding Error ... MediaInfoExtractionException" on the
    2026-10-21 live run when tmpfiles.org handed back a page instead of the file; the
    post then failed minutes later with no hint why. Checking the first bytes (ISO-BMFF
    `ftyp` box, or at least a video content-type) catches that before anything is posted.
    Range is requested so the check costs a few KB, never the whole file.
    """
    import httpx
    headers = {"User-Agent": UA, "Accept": "*/*", "Range": "bytes=0-4095"}
    with httpx.stream("GET", url, headers=headers, follow_redirects=True,
                      timeout=timeout) as response:
        status = response.status_code
        if status not in (200, 206):
            raise RuntimeError(f"HTTP {status}")
        content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        length = response.headers.get("content-length")
        head = b""
        for chunk in response.iter_bytes():
            head += chunk
            if len(head) >= 512:
                break
    if len(head) < 32:
        raise RuntimeError("empty response body")
    looks_mp4 = b"ftyp" in head[:64]
    if not looks_mp4 and not content_type.startswith("video/"):
        raise RuntimeError(f"not video bytes (content-type={content_type or 'none'})")
    if (expected_size and status == 200 and length and length.isdigit()
            and abs(int(length) - int(expected_size)) > 1024):
        raise RuntimeError(f"size mismatch ({length} vs {expected_size})")


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


def upload_pixeldrain(path):
    """Anonymous Pixeldrain upload -> a direct-download API URL (reliable, range-capable).

    First choice since 2026-10-21: it serves the exact bytes back (verified), which is what
    Instagram's transcoder needs to extract media info.
    """
    with open(path, "rb") as f, _client() as c:
        r = c.post("https://pixeldrain.com/api/file/",
                   files={"file": (os.path.basename(path), f, "video/mp4")})
    r.raise_for_status()
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    file_id = data.get("id") if isinstance(data, dict) else None
    if not file_id:
        raise RuntimeError(f"pixeldrain returned no file id: {str(r.text)[:200]}")
    return f"https://pixeldrain.com/api/file/{file_id}?download"


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
    # uguu first since 2026-10-21: on the live run the chain handed Instagram a tmpfiles.org
    # URL that served text/html, and the post died minutes later with an opaque
    # "Video Transcoding Error". uguu was the only host that returned the exact video bytes
    # in a same-machine probe (pixeldrain 401, catbox 0-byte, litterbox 500, tmpfiles HTML).
    # Files expire in ~3h, which is irrelevant: the platform fetches within seconds.
    ("uguu", upload_uguu),
    ("pixeldrain", upload_pixeldrain),
    ("catbox", upload_catbox),
    ("litterbox", upload_litterbox),
    ("tmpfiles", upload_tmpfiles),
    ("0x0", upload_0x0),
]


def host(path, exclude=None, verify=True):
    """Return (url, provider). Raises RuntimeError if the whole chain fails.

    Every provider's URL must pass `verify_public_url` before it is returned: a host that
    answers with a login wall / HTML error page would otherwise be handed to Instagram and
    fail the post's transcode minutes later.
    """
    excluded = {str(name).strip().lower() for name in (exclude or []) if str(name).strip()}
    size = os.path.getsize(path)
    errors = {}
    for name, fn in PROVIDERS:
        if name.lower() in excluded:
            continue
        try:
            url = fn(path)
            if verify:
                verify_public_url(url, expected_size=size)
            return url, name
        except Exception as e:
            errors[name] = str(e)[:160]
    raise RuntimeError(f"All public file hosts failed: {errors}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True)
    parser.add_argument("--exclude", default="",
                        help="Comma-separated providers to skip (used to force a different "
                             "host when a platform rejects the first URL).")
    parser.add_argument("--no-verify", action="store_true",
                        help="Skip the MP4-bytes check (diagnostics only; never in CI).")
    args = parser.parse_args()
    load_env()
    if not os.path.isfile(args.video):
        fail(f"Video not found: {args.video}")
        return
    exclude = [part for part in args.exclude.split(",") if part.strip()]
    try:
        url, provider = host(args.video, exclude=exclude, verify=not args.no_verify)
    except RuntimeError as e:
        fail(str(e))
        return
    emit({"url": url, "provider": provider, "verified": not args.no_verify,
          "byte_size": os.path.getsize(args.video)})


if __name__ == "__main__":
    main()
