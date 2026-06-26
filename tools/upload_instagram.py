import argparse
import json
import os
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    print(json.dumps({"error": "The 'requests' library is missing from the runtime environment."}))
    sys.exit(1)

HERE = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(HERE / "API.env")
except ImportError:
    pass

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video-url", required=True, help="Public URL of the hosted video asset")
    ap.add_argument("--caption", default="", help="Text content and hashtags for the Reel")
    ap.add_argument("--confirm", action="store_true")
    args = ap.parse_args()

    # Targets your exact repo secrets
    api_key = os.environ.get("ZERNIO_API")
    account_id = os.environ.get("ZERNIO_INSTAGRAM_ID")

    if not api_key or not account_id:
        print(json.dumps({"error": "Missing ZERNIO_API or ZERNIO_INSTAGRAM_ID inside environment context"}))
        sys.exit(1)

    url = "https://zernio.com/api/v1/posts"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "content": args.caption,
        "mediaUrls": [args.video_url],
        "platforms": [
            {
                "platform": "instagram",
                "accountId": account_id
            }
        ],
        "publishNow": True
    }

    try:
        res = requests.post(url, headers=headers, json=payload)
        res.raise_for_status()
        data = res.json()
        
        post_id = data.get("id") or data.get("postId") or "zernio_success"
        print(json.dumps({"media_id": post_id}))
        
    except Exception as e:
        print(json.dumps({"error": f"Zernio API engine failure: {str(e)}"}))
        sys.exit(1)

if __name__ == "__main__":
    main()
