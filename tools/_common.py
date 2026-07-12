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
# this project's own root (CI writes it there / standalone repo) OR somewhere above it
# (when kept nested inside the multi-project repo, currently at projects/clipping-auto/
# -- two levels up). Walk up so this keeps working regardless of nesting depth.
SHARED_ENV = next(
    (p / "API.env" for p in REPO_ROOT.parents if (p / "API.env").is_file()),
    REPO_ROOT.parent / "API.env",
)
TMP_DIR = REPO_ROOT / ".tmp"


def load_env():
    from dotenv import load_dotenv
    # Shared copy first as defaults, then this project's own root API.env with
    # override=True -- so a partial local file (e.g. just an account id) augments
    # the shared one instead of shadowing it entirely.
    if SHARED_ENV.is_file():
        load_dotenv(SHARED_ENV)
    local_env = REPO_ROOT / "API.env"
    if local_env.is_file():
        load_dotenv(local_env, override=True)


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

    stdin is pinned to DEVNULL: ffmpeg reads stdin for interactive keyboard commands,
    and in a CI/background context (e.g. GitHub Actions) that stdin is an open pipe
    ffmpeg blocks on -- which hung the render step for hours until the job timed out.
    None of our subprocesses need stdin, so closing it is always safe.
    Callers may pass timeout=<seconds>; a TimeoutExpired is re-raised with any partial
    stderr so a stall fails fast and legibly instead of burning the whole job budget.
    """
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **kwargs,
        )
    except subprocess.TimeoutExpired as e:
        prog = os.path.basename(str(cmd[0]))
        tail = "\n".join((e.stderr or "").strip().splitlines()[-15:]) if e.stderr else ""
        raise RuntimeError(f"{prog} timed out after {e.timeout:.0f}s\n{tail}")
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


# --- Instagram post log (weekly style-experiment tracking) ----------------
# Durable, append-only record of every successful Instagram publish, so
# check_style_experiment.py can later pull an experimental post's id plus a
# baseline of recent normal posts and compare performance via Zernio analytics.

def log_ig_post(post_id, style=None, experiment=False, context=None):
    import datetime
    path = REPO_ROOT / "state" / "ig_post_log.json"
    data = {"posts": []}
    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    data.setdefault("posts", []).append({
        "post_id": post_id,
        "posted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "style": style,
        "experiment": bool(experiment),
        "resolved": False,
        "context": context or {},
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
