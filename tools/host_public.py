"""Expose a local video at a PUBLIC https URL.

Instagram's Graph API can't take a local file — it fetches the Reel from a public `video_url`
(Facebook's servers download it). This uploads the mp4 to a free, keyless file host and returns
the direct link. Fallback chain: catbox.moe -> 0x0.st.

For production you'd point this at your own bucket (set IG_PUBLIC_BASE_URL + an upload step);
the free hosts are fine for low volume but can rate-limit or expire, so treat a whole-chain
failure as "skip Instagram this run" upstream.

Usage:
    python tools/host_public.py --video .tmp/final.mp4

Prints JSON: {"url": "https://...", "provider": "catbox"|"0x0"}
"""
import argparse
import os

from _common import load_env, emit, fail


def upload_catbox(path):
    import httpx

    with open(path, "rb") as f:
        files = {"fileToUpload": (os.path.basename(path), f, "video/mp4")}
        data = {"reqtype": "fileupload"}
        resp = httpx.post("https://catbox.moe/user/api.php", data=data, files=files, timeout=180)
    resp.raise_for_status()
    url = resp.text.strip()
    if not url.startswith("http"):
        raise RuntimeError(f"catbox returned no URL: {url[:200]}")
    return url


def upload_0x0(path):
    import httpx

    with open(path, "rb") as f:
        files = {"file": (os.path.basename(path), f, "video/mp4")}
        resp = httpx.post("https://0x0.st", files=files,
                          headers={"User-Agent": "ai-videos-auto/1.0"}, timeout=180)
    resp.raise_for_status()
    url = resp.text.strip()
    if not url.startswith("http"):
        raise RuntimeError(f"0x0.st returned no URL: {url[:200]}")
    return url


PROVIDERS = [("catbox", upload_catbox), ("0x0", upload_0x0)]


def host(path):
    """Return (url, provider). Raises RuntimeError if the whole chain fails."""
    errors = {}
    for name, fn in PROVIDERS:
        try:
            return fn(path), name
        except Exception as e:
            errors[name] = str(e)
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
