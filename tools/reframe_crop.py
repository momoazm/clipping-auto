"""Reframe a horizontal clip to vertical 9:16 that follows the speaker.

Pipeline role: takes one chosen span [start,end] of the source video and renders
a 1080x1920 clip whose crop window PANS to keep the active speaker centered --
the difference between a Short that feels native and one that's an obvious
letterboxed desktop recording.

How it works (no torch / mediapipe -- 3.14-safe, OpenCV only):
  1. OpenCV samples frames across the span (~6 fps) and detects faces with the
     YuNet DNN detector (config/models/) -- it catches profile/angled/smaller
     faces that Haar misses; falls back to the bundled Haar cascade if the model
     is absent.
  2. It tracks ONE subject (the face nearest the previous pick, reseeding on big
     jumps/scene cuts) so the crop doesn't hop between people. That center path is
     interpolated to per-frame and zero-phase (forward-backward) smoothed, so the
     camera glides AND stays centered without lagging behind.
  3. The smoothed x is written as ffmpeg `sendcmd` keyframes driving the `crop`
     filter's x, then scaled to 1080x1920 -- a real motion pan, not a static crop.

Fallbacks: no faces found, or a source already narrower than 9:16 -> blurred
letterbox fit (`--mode letterbox` forces this). Audio for the span is carried
through so the output is previewable and reusable by render_clip.py.

Usage:
    python tools/reframe_crop.py --in video.mp4 --start 12.5 --end 58.0 \
        [--out .tmp/reframed.mp4] [--mode auto|track|letterbox]

Prints JSON: {"path","mode","faces_tracked","width","height","duration"}
"""
import argparse
import os

from _common import (load_env, emit, fail, run, ffmpeg_bin, ffprobe_json,
                     tmp_path, TMP_DIR, REPO_ROOT, FFmpegMissing)

OUT_W, OUT_H = 1080, 1920
CMD_FPS = 30.0          # per-frame crop keyframes => no staircase stutter in the pan
DETECT_FPS = 6.0        # how often we look for the face (finer = tracks tighter)
DETECT_WIDTH = 640      # downscale frames before detection (bigger = catches smaller faces)
EMA_ALPHA = 0.25        # smoothing strength; applied zero-phase so it does NOT lag
YUNET_PATH = REPO_ROOT / "config" / "models" / "face_detection_yunet_2023mar.onnx"


def _make_detector(cv2):
    """Prefer YuNet (DNN, handles profile/angled/smaller faces) -> Haar fallback.

    Returns (kind, obj). YuNet directly addresses 'didn't find the speaker at the
    start', where Haar misses non-frontal/small faces and the crop sits wrong.
    """
    if YUNET_PATH.is_file():
        try:
            det = cv2.FaceDetectorYN.create(
                str(YUNET_PATH), "", (320, 320),
                score_threshold=0.6, nms_threshold=0.3, top_k=50)
            return "yunet", det
        except Exception:
            pass
    return "haar", cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml")


def _detect(cv2, kind, det, small, min_face):
    """Return detections as list of (cx, area, score) in the small-frame pixels."""
    if kind == "yunet":
        h, w = small.shape[:2]
        det.setInputSize((w, h))
        _, faces = det.detect(small)
        out = []
        if faces is not None:
            for f in faces:
                x, y, fw, fh = float(f[0]), float(f[1]), float(f[2]), float(f[3])
                out.append((x + fw / 2.0, fw * fh, float(f[-1])))
        return out
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    faces = det.detectMultiScale(gray, 1.1, 5, minSize=(min_face, min_face))
    return [(x + w / 2.0, float(w * h), 1.0) for (x, y, w, h) in faces]


def detect_face_track(video, start, end):
    """Return (times[], centers_x[]) in SOURCE pixels, plus (src_w, src_h, fps).

    Tracks ONE subject across frames (the face nearest the previous pick) instead
    of grabbing the largest face each frame -- that's what made the crop jump
    between people. On a big jump (a scene cut) it reseeds to the most prominent
    face so it re-locks quickly.
    """
    import cv2

    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise RuntimeError("OpenCV could not open the video for face tracking.")
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0

    kind, det = _make_detector(cv2)
    times, centers = [], []
    step = max(1, int(round(fps / DETECT_FPS)))
    scale = DETECT_WIDTH / src_w if src_w > DETECT_WIDTH else 1.0
    min_face = max(20, int(src_h * 0.06 * scale))
    reseed_gap = DETECT_WIDTH * 0.45  # treat bigger horizontal jumps as a scene cut

    cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000.0)
    idx = 0
    prev = None
    while True:
        pos = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if pos > end:
            break
        ok, frame = cap.read()
        if not ok:
            break
        if idx % step == 0:
            small = cv2.resize(frame, (0, 0), fx=scale, fy=scale) if scale != 1.0 else frame
            dets = _detect(cv2, kind, det, small, min_face)
            if dets:
                prominent = max(dets, key=lambda d: d[2] * d[1])
                if prev is None:
                    pick = prominent
                else:
                    nearest = min(dets, key=lambda d: abs(d[0] - prev))
                    pick = prominent if abs(nearest[0] - prev) > reseed_gap else nearest
                prev = pick[0]
                times.append(round(pos - start, 3))
                centers.append(pick[0] / scale)  # back to source pixels
        idx += 1
    cap.release()
    return times, centers, src_w, src_h, fps


def build_pan_commands(times, centers, src_w, src_h, duration):
    """Dense, EMA-smoothed crop-x keyframes clamped to valid range."""
    import numpy as np

    crop_w = int(round(src_h * OUT_W / OUT_H))
    crop_w -= crop_w % 2
    if crop_w >= src_w:
        return None, crop_w  # can't crop horizontally -> caller uses letterbox

    max_x = src_w - crop_w
    n = max(2, int(duration * CMD_FPS))
    tc = np.linspace(0, duration, n)

    if times:
        cx = np.interp(tc, times, centers)
    else:
        cx = np.full(n, src_w / 2.0)

    # Zero-phase smoothing: a causal EMA always trails the subject (that reads as
    # laggy camera). Running the EMA forward then backward cancels the phase lag,
    # so the pan stays smooth but stays centered on the speaker.
    def _ema(arr, a):
        out = np.empty_like(arr)
        out[0] = arr[0]
        for i in range(1, len(arr)):
            out[i] = a * arr[i] + (1 - a) * out[i - 1]
        return out

    sm = _ema(_ema(cx, EMA_ALPHA)[::-1], EMA_ALPHA)[::-1]

    lines = []
    for t, c in zip(tc, sm):
        x = int(round(min(max(c - crop_w / 2.0, 0), max_x)))
        lines.append(f"{t:.3f} crop x {x};")
    return "\n".join(lines), crop_w


def render_track(video, start, dur, crop_w, src_h, cmds_text, out_path):
    # Unique cmds filename per output so concurrent/sequential clips never clash.
    stem = os.path.splitext(os.path.basename(out_path))[0]
    cmds_name = f"pan_{stem}.txt"
    (TMP_DIR / cmds_name).write_text(cmds_text + "\n", encoding="utf-8")
    vf = (
        f"sendcmd=f={cmds_name},"
        f"crop=w={crop_w}:h={src_h}:x=0:y=0,"
        f"scale={OUT_W}:{OUT_H},setsar=1,format=yuv420p"
    )
    # cwd=TMP_DIR so the sendcmd path is a bare filename (avoids ffmpeg filter
    # escaping of the absolute path -- this repo's path has a space and a drive
    # colon). Because cwd changes, input/output MUST be absolute paths.
    cmd = [
        ffmpeg_bin(), "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
        "-i", os.path.abspath(video),
        "-vf", vf, "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", os.path.abspath(out_path),
    ]
    run(cmd, cwd=str(TMP_DIR))


def render_letterbox(video, start, dur, out_path):
    vf = (
        f"split[bg][fg];"
        f"[bg]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W}:{OUT_H},gblur=sigma=24[bg2];"
        f"[fg]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease[fg2];"
        f"[bg2][fg2]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p"
    )
    cmd = [
        ffmpeg_bin(), "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(video),
        "-vf", vf, "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", str(out_path),
    ]
    run(cmd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="inp", required=True)
    parser.add_argument("--start", type=float, required=True)
    parser.add_argument("--end", type=float, required=True)
    parser.add_argument("--out", default=None)
    parser.add_argument("--mode", default="auto", choices=["auto", "track", "letterbox"])
    args = parser.parse_args()

    load_env()
    out_path = args.out or tmp_path("reframed.mp4")
    dur = args.end - args.start
    if dur <= 0:
        fail("--end must be greater than --start.")
        return
    if not os.path.isfile(args.inp):
        fail(f"Input not found: {args.inp}")
        return

    try:
        ffprobe_json(args.inp)  # validates ffmpeg present + file readable
    except FFmpegMissing as e:
        fail(str(e))
        return
    except Exception as e:
        fail(f"ffprobe could not read this file: {e}")
        return

    try:
        if args.mode == "letterbox":
            render_letterbox(args.inp, args.start, dur, out_path)
            emit({"path": out_path, "mode": "letterbox", "faces_tracked": 0,
                  "width": OUT_W, "height": OUT_H, "duration": round(dur, 3)})
            return

        try:
            times, centers, src_w, src_h, _fps = detect_face_track(args.inp, args.start, args.end)
        except Exception as e:
            # OpenCV trouble shouldn't kill the clip -- fall back to letterbox.
            render_letterbox(args.inp, args.start, dur, out_path)
            emit({"path": out_path, "mode": "letterbox", "faces_tracked": 0,
                  "width": OUT_W, "height": OUT_H, "duration": round(dur, 3),
                  "note": f"Face tracking unavailable ({e}); used letterbox."})
            return

        cmds_text, crop_w = build_pan_commands(times, centers, src_w, src_h, dur)
        if cmds_text is None or (args.mode == "auto" and not times):
            render_letterbox(args.inp, args.start, dur, out_path)
            why = "source narrower than 9:16" if cmds_text is None else "no faces detected"
            emit({"path": out_path, "mode": "letterbox", "faces_tracked": len(times),
                  "width": OUT_W, "height": OUT_H, "duration": round(dur, 3),
                  "note": f"Fell back to letterbox ({why})."})
            return

        render_track(args.inp, args.start, dur, crop_w, src_h, cmds_text, out_path)
        emit({"path": out_path, "mode": "track", "faces_tracked": len(times),
              "width": OUT_W, "height": OUT_H, "duration": round(dur, 3)})
    except FFmpegMissing as e:
        fail(str(e))
    except Exception as e:
        fail(f"Reframe failed: {e}")


if __name__ == "__main__":
    main()
