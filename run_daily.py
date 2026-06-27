if IG_ENABLED:
                    try:
                        # We now pass the local video file path directly to the tool
                        # instead of trying to host it on a public URL first
                        ig = run_tool("upload_instagram.py", "--video", short,
                                      "--caption", desc, "--confirm")
                        
                        entry["instagram_media_id"] = ig.get("media_id")
                        log(f"clip {n}: Instagram -> {ig.get('media_id')}")
                    except Exception as e:
                        # If Instagram fails, we log it but the YouTube upload is preserved
                        log(f"clip {n}: Instagram FAILED (YouTube upload still kept): {e}")
                        summary.setdefault("instagram_errors", []).append({"clip": n, "error": str(e)})
