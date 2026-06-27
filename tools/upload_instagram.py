import argparse
import json
import sys
import os

try:
    from zernio import Zernio
except ImportError:
    sys.exit("zernio-sdk is not installed. Please add it to requirements.txt")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True, help="Path to local video file")
    parser.add_argument("--caption", required=True, help="Post caption")
    parser.add_argument("--confirm", action="store_true", help="Actually post")
    args = parser.parse_args()

    api_key = os.environ.get("ZERNIO_API")
    ig_id = os.environ.get("ZERNIO_INSTAGRAM_ID")

    if not api_key or not ig_id:
        sys.exit("ZERNIO_API or ZERNIO_INSTAGRAM_ID is missing from your environment variables.")

    if not args.confirm:
        print(json.dumps({"media_id": "dry_run_instagram_skipped"}))
        return

    try:
        # 1. Initialize the official Zernio client
        client = Zernio(api_key=api_key)

        # 2. Upload the local video directly to Zernio's media library
        upload_result = client.media.upload(args.video)
        media_url = upload_result["publicUrl"]

        # 3. Publish to Instagram using the uploaded media
        post = client.posts.create(
            content=args.caption,
            media_urls=[media_url],
            platforms=[{"platform": "instagram", "accountId": ig_id}],
            publish_now=True
        )

        # Print success JSON so your pipeline log catches it
        print(json.dumps({"media_id": "posted_successfully"}))

    except Exception as e:
        sys.exit(f"Zernio API Error: {str(e)}")

if __name__ == "__main__":
    main()
