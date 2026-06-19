"""Re-sync shared pipeline files FROM the sibling working copy ../clipping INTO this
automation copy — without touching automation-only files.

Use this when you've improved a tool in clipping/ and want the daily automation to pick
up the change. It copies the shared pipeline tools + brand/ + caption styles, but leaves
the automation-only files alone:
  - tools/_common.py (this copy has a location-agnostic API.env tweak)
  - run_daily.py, tools/find_source_video.py, tools/generate_hashtags.py
  - config/channels.json, state/, .github/

Usage:  python sync_from_clipping.py
"""
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "clipping"

# Shared pipeline tools pulled from ../clipping/tools. NOTE: _common.py AND
# download_video.py are deliberately excluded — this copy's _common has the standalone
# API.env lookup, and its download_video has the cloud cookie/bot-bypass tweak. Re-add
# them by hand if you ever want clipping/'s versions.
TOOLS = [
    "probe_video.py", "transcribe_video.py", "select_clips.py",
    "reframe_crop.py", "build_captions.py", "build_sfx.py", "build_music.py",
    "plan_effects.py", "render_clip.py", "upload_youtube.py", "youtube_auth_setup.py",
]
OTHER_FILES = ["config/caption_styles.json", "requirements.txt"]


def main():
    if not SRC.is_dir():
        print(f"Source working copy not found: {SRC}", file=sys.stderr)
        sys.exit(1)

    copied = []
    for t in TOOLS:
        s = SRC / "tools" / t
        if s.is_file():
            shutil.copy2(s, HERE / "tools" / t)
            copied.append(f"tools/{t}")

    for rel in OTHER_FILES:
        s = SRC / rel
        if s.is_file():
            (HERE / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, HERE / rel)
            copied.append(rel)

    # brand/ (theme.json, logo, the caption font) — mirror all files.
    if (SRC / "brand").is_dir():
        for p in (SRC / "brand").rglob("*"):
            if p.is_file():
                rel = p.relative_to(SRC)
                dst = HERE / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dst)
                copied.append(str(rel).replace("\\", "/"))

    print(f"Synced {len(copied)} file(s) from {SRC}:")
    for c in copied:
        print("  " + c)
    print("\nLeft untouched (automation-only): tools/_common.py, run_daily.py,")
    print("tools/find_source_video.py, tools/generate_hashtags.py, config/channels.json,")
    print("state/, .github/")


if __name__ == "__main__":
    main()
