import argparse
import json
import sys
import os
from pathlib import Path

# Ensure the project root is in the path to load environment variables
HERE = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(HERE / "API.env")
except ImportError:
    pass

try:
    from zernio import Zernio
except ImportError:
    print(json.dumps({"error": "zernio-sdk is not installed. Add 'zernio-sdk' to requirements.txt"}))
    sys.exit(1)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True, help="Path to local video file")
    parser.add_argument("--caption", required=True, help="Post caption")
    parser.add_argument("--confirm", action="store_true", help="Actually post")
    args = parser.parse_args()

    # Extract secrets from environment
    api_key = os.environ.get("ZERNIO_API")
    ig_id = os.environ.get("ZERNIO_INSTAGRAM_ID")

    if not api_key or not ig_id:
        print(json.dumps({"error": "ZERNIO_API or ZERNIO_INSTAGRAM_ID missing from environment"}))
        sys.exit(1)

    if not args.confirm:
        print(json.dumps({"media_id": "dry_run_instagram_skipped"}))
        return

    try:
        # 1. Initialize the Zernio client
        client = Zernio(api_key=api_key)

        # 2. Upload the local video file directly
        # Note: The SDK handles the multipart/form-data upload automatically
        upload_result = client.media.upload(file_path=args.video)
        
        # 3. Retrieve the media URL from the upload response
        # Adjust the key (e.g., 'publicUrl' or 'id') based on your specific SDK version output
        media_url = upload_result.get("publicUrl") or upload_result.get("url")

        # 4. Publish to Instagram
        post = client.posts.create(
            content=args.caption,
            media_urls=[media_url],
            platforms=[{"platform": "instagram", "accountId": ig_id}],
            publish_now=True
        )

        print(json.dumps({"media_id": post.get("id", "posted_successfully")}))

    except Exception as e:
        print(json.dumps({"error": f"Zernio SDK Error: {str(e)}"}))
        sys.exit(1)

if __name__ == "__main__":
    main()
