"""Shared helpers for tools/ scripts: env loading, brand theme loading, JSON I/O,
and ffmpeg/ffprobe wrappers used across the clipping pipeline.

Every tool is a standalone CLI script that prints one JSON object to stdout and
exits 0 on success. On failure it still prints a JSON object (with an "error"
key) so callers can parse stdout either way, and exits 1.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# This is the STANDALONE automation copy (clipping-auto). API.env can live EITHER at
# this project's own root (CI writes it there / standalone repo) OR one level up (when
# kept as a sibling of clipping/ inside the multi-project repo). Try both, prefer own root.
_ENV_CANDIDATES = (REPO_ROOT / "API.env", REPO_ROOT.parent / "API.env")
SHARED_ENV = next((p for p in _ENV_CANDIDATES if p.is_file()), _ENV_CANDIDATES[0])
TMP_DIR = REPO_ROOT / ".tmp"


def load_env():
    from dotenv import load_dotenv
    # Load whichever API.env exists; if neither does, keys are expected in the real env.
    for p in _ENV_CANDIDATES:
        if p.is_file():
            load_dotenv(p)
            return


def load_theme():
    with open(REPO_ROOT / "brand" / "theme.json", "r", encoding="utf-8") as f:
        return json.load(f)


def emit(data):
    text = json.dumps(data, indent=2, ensure_ascii=False)
    try:
        print(text)
    except UnicodeEncodeError:
        # Windows console codepages (e.g. cp1252) can't print arbitrary
        # Unicode (curly quotes, em-dashes, emoji) that scraped articles
        # often contain. Fall back to escaping non-ASCII rather than crashing.
        print(json.dumps(data, indent=2, ensure_ascii=True))


def fail(message, **extra):
    emit({"error": message, **extra})
    sys.exit(1)


# --- filesystem -----------------------------------------------------------

def tmp_path(name):
    """Absolute path inside .tmp/ (created on demand). Use for intermediates."""
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    return str(TMP_DIR / name)


# --- ffmpeg / ffprobe -----------------------------------------------------
# The whole video pipeline shells out to the system ffmpeg (installed via
# `winget install Gyan.FFmpeg`, which ships a libass-enabled build). We resolve
# the binaries once here so every tool fails with the same clear message if the
# install is missing, and so a bundled fallback (imageio-ffmpeg) can be slotted
# in later without touching every tool.

class FFmpegMissing(RuntimeError):
    pass


def ffmpeg_bin():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    # Optional fallback if a pip-bundled binary is installed.
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    raise FFmpegMissing(
        "ffmpeg not found on PATH. Install it once: winget install Gyan.FFmpeg "
        "(full build, includes libass needed for caption burning)."
    )


def ffprobe_bin():
    exe = shutil.which("ffprobe")
    if exe:
        return exe
    raise FFmpegMissing(
        "ffprobe not found on PATH. It ships with ffmpeg: winget install Gyan.FFmpeg."
    )


def run(cmd, **kwargs):
    """Run a subprocess, raising RuntimeError with captured stderr on failure.

    `cmd` is a list of args. Returns the CompletedProcess on success.
    """
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )
    if proc.returncode != 0:
        prog = os.path.basename(str(cmd[0]))
        tail = (proc.stderr or "").strip().splitlines()[-15:]
        raise RuntimeError(f"{prog} failed (exit {proc.returncode}):\n" + "\n".join(tail))
    return proc


def ffprobe_json(path):
    """Return ffprobe's full format+streams JSON for a media file."""
    proc = run([
        ffprobe_bin(), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ])
    return json.loads(proc.stdout)
