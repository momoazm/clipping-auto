import argparse
import json
import sys
import os
from pathlib import Path

# Ensure the project root is in the path to load environment variables safely
HERE = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(HERE / "API.env")
except ImportError:
    pass

try:
    from zernio import Zernio
except ImportError:
    # Print a JSON error so the orchestrator catches it cleanly instead of crashing
    print(json.dumps({"error": "zernio-sdk is not installed. Please add 'zernio-sdk' to your requirements.txt or run 'pip install zernio-sdk' on your runner."}))
    sys.exit(1)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True, help="Path to the local video file (.mp4)")
    parser.add_argument("--caption", required=True, help="Post caption and hashtags")
    parser.add_argument("--confirm", action="store_true", help="Actually post to Instagram")
    args = parser.parse_args()

    # Extract secrets securely from the runner's environment variables
    api_key = os.environ.get("ZERNIO_API")
    ig_id = os.environ.get("ZERNIO_INSTAGRAM_ID")

    if not api_key or not ig_id:
        print(json.dumps({"error": "ZERNIO_API or ZERNIO_INSTAGRAM_ID is missing from your environment keys."}))
        sys.exit(1)

    if not os.path.exists(args.video):
        print(json.dumps({"error": f"Video file not found at path: {args.video}"}))
        sys.exit(1)

    if not args.confirm:
        print(json.dumps({"media_id": "dry_run_instagram_skipped"}))
        return

    try:
        # 1. Initialize the Zernio client
        client = Zernio(api_key=api_key)

        # 2. Upload the local video file securely to Zernio's media library
        upload_result = client.media.upload(file_path=args.video)
        
        # 3. Retrieve the media URL from the upload response payload
        media_url = upload_result.get("publicUrl") or upload_result.get("url")

        if not media_url:
            print(json.dumps({"error": "Failed to retrieve a valid media URL from Zernio after file upload."}))
            sys.exit(1)

        # 4. Publish the uploaded media to Instagram using your Account ID
        post = client.posts.create(
            content=args.caption,
            media_urls=[media_url],
            platforms=[{"platform": "instagram", "accountId": ig_id}],
            publish_now=True
        )

        # Output the successful post ID back to the main run_daily.py orchestrator
        print(json.dumps({"media_id": post.get("id", "posted_successfully")}))

    except Exception as e:
        print(json.dumps({"error": f"Zernio SDK Error: {str(e)}"}))
        sys.exit(1)

if __name__ == "__main__":
    main()
