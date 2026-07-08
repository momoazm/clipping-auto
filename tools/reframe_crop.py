"""Reframe a horizontal clip to vertical 9:16 that follows the ACTIVE SPEAKER.

Pipeline role: takes one chosen span [start,end] of the source video and renders
a 1080x1920 clip whose crop window PANS to keep the person who is actually talking
centered -- the difference between a Short that feels native and one that's an
obvious letterboxed desktop recording.

How it works (no torch / mediapipe -- 3.14-safe, OpenCV only):
  1. OpenCV samples frames across the span (~6 fps) and detects faces with the
     YuNet DNN detector (config/models/) -- it catches profile/angled/smaller
     faces that Haar misses; falls back to the bundled Haar cascade if the model
     is absent.
  2. For each detected face it estimates an ACTIVE-SPEAKER score from lip motion
     (the mouth ROI's frame-to-frame change, minus eye-ROI/head motion) PLUS a
     frame-diff MOTION cue -- how much of the scene's movement is at that face, so a
     gesturing/moving talker beats a static bystander. Selection favours the talker,
     not just the biggest face -- with hysteresis so it won't flick onto a
     momentarily-bigger bystander, and a hard cut (not a glide) when the subject
     changes or the shot cuts (so the crop never drifts through the empty gap
     between two people).
  2b. In shots with NO detected face (common in action/wide MrBeast footage) it
     follows the MOTION CENTROID -- the column where the frame-to-frame action is --
     instead of freezing or letterboxing, so the crop stays on "the main part".
  3. The chosen center path is interpolated to per-frame and zero-phase
     (forward-backward) smoothed WITHIN each shot, then written as ffmpeg `sendcmd`
     keyframes driving the `crop` filter's x, then scaled to 1080x1920.

Fallbacks: no faces AND no motion, or a source already narrower than 9:16 -> blurred
letterbox fit (`--mode letterbox` forces this). If lip motion can't be measured
(no landmarks / no match across frames) selection degrades to the old
prominence-based pick, so behaviour never gets worse than before. Audio for the
span is carried through so the output is previewable and reusable by render_clip.py.

Usage:
    python tools/reframe_crop.py --in video.mp4 --start 12.5 --end 58.0 \
        [--out .tmp/reframed.mp4] [--mode auto|track|letterbox]

Prints JSON: {"path","mode","faces_tracked","cuts","width","height","duration"}
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

# --- motion saliency ---------------------------------------------------------
# A cheap frame-diff on the already-downscaled detect frame. Gives us TWO things
# faces alone can't: (a) a target to follow in faceless action/wide shots (MrBeast
# footage is full of these -- previously they fell back to a blurred letterbox that
# threw away "the main part"), and (b) a per-face "is this the one actually moving/
# gesturing" cue that reinforces the lip-motion speaker score.
MOTION_DIFF_THRESH = 18   # per-pixel grayscale delta (0..255) that counts as motion
MOTION_FLOOR = 0.008      # mean frame motion below this = "static" -> ignore as noise
MOTION_LERP = 0.5         # how fast the crop chases the motion centroid in a faceless shot

# --- active-speaker selection tuning -----------------------------------------
RESEED_FRAC = 0.40      # a horizontal jump bigger than this fraction of width = a cut/new subject
# composite weights (lip/area/motion normalised 0..1 per sample, score ~0..1)
W_LIP, W_AREA, W_SCORE, W_MOTION = 0.45, 0.22, 0.08, 0.25
SWITCH_MARGIN = 0.15    # a challenger must beat the tracked face's composite by this to count
SWITCH_HOLD = 2         # ...for this many consecutive samples before we actually switch (hysteresis)
LIP_RIGID_COMP = 0.60   # how much eye-region (head) motion to subtract from mouth motion
MOUTH_PATCH = (28, 18)  # normalised mouth ROI size for frame-diff (w, h)
EYE_PATCH = (28, 12)    # normalised eye ROI size


def _make_detector(cv2):
    """Prefer YuNet (DNN, handles profile/angled/smaller faces + landmarks) -> Haar.

    Returns (kind, obj). YuNet directly addresses 'didn't find the speaker at the
    start', where Haar misses non-frontal/small faces, and its mouth/eye landmarks
    are what make the lip-motion speaker score possible.
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


def _patch(cv2, small, x0, y0, x1, y1, size):
    """Grayscale, histogram-equalised, fixed-size patch of small[y0:y1, x0:x1].

    Returns None if the box is degenerate/out of frame. Fixed size + equalisation
    make frame-to-frame diffs robust to the face moving/scaling and to lighting.
    """
    h, w = small.shape[:2]
    x0 = max(0, min(int(x0), w - 1)); x1 = max(x0 + 1, min(int(x1), w))
    y0 = max(0, min(int(y0), h - 1)); y1 = max(y0 + 1, min(int(y1), h))
    roi = small[y0:y1, x0:x1]
    if roi.size == 0:
        return None
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, size, interpolation=cv2.INTER_AREA)
    return cv2.equalizeHist(gray)


def _detect_full(cv2, kind, det, small, min_face):
    """Detect faces and pull mouth/eye ROIs. Returns list of dicts:
    {cx, area, score, mouth, eye} in SMALL-frame pixels (mouth/eye are patches)."""
    out = []
    if kind == "yunet":
        h, w = small.shape[:2]
        det.setInputSize((w, h))
        _, faces = det.detect(small)
        if faces is None:
            return out
        for f in faces:
            x, y, fw, fh = float(f[0]), float(f[1]), float(f[2]), float(f[3])
            # YuNet landmarks: 4,5 r-eye | 6,7 l-eye | 8,9 nose | 10,11 r-mouth | 12,13 l-mouth
            reye, leye = (float(f[4]), float(f[5])), (float(f[6]), float(f[7]))
            rmo, lmo = (float(f[10]), float(f[11])), (float(f[12]), float(f[13]))
            mcx, mcy = (rmo[0] + lmo[0]) / 2.0, (rmo[1] + lmo[1]) / 2.0
            mw = max(8.0, abs(lmo[0] - rmo[0]))
            mouth = _patch(cv2, small, mcx - 0.9 * mw, mcy - 0.6 * mw,
                           mcx + 0.9 * mw, mcy + 0.6 * mw, MOUTH_PATCH)
            ecx, ecy = (reye[0] + leye[0]) / 2.0, (reye[1] + leye[1]) / 2.0
            ew = max(8.0, abs(leye[0] - reye[0]))
            eye = _patch(cv2, small, ecx - 0.9 * ew, ecy - 0.5 * ew,
                         ecx + 0.9 * ew, ecy + 0.5 * ew, EYE_PATCH)
            out.append({"cx": x + fw / 2.0, "area": fw * fh, "score": float(f[-1]),
                        "mouth": mouth, "eye": eye})
        return out
    # Haar: no landmarks -> approximate mouth = lower third, eyes = upper third.
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    faces = det.detectMultiScale(gray, 1.1, 5, minSize=(min_face, min_face))
    for (x, y, w, h) in faces:
        mouth = _patch(cv2, small, x + 0.20 * w, y + 0.62 * h,
                       x + 0.80 * w, y + 0.95 * h, MOUTH_PATCH)
        eye = _patch(cv2, small, x + 0.15 * w, y + 0.18 * h,
                     x + 0.85 * w, y + 0.45 * h, EYE_PATCH)
        out.append({"cx": x + w / 2.0, "area": float(w * h), "score": 1.0,
                    "mouth": mouth, "eye": eye})
    return out


def _lip_activity(face, prev_faces, gate):
    """Mouth-ROI motion minus head (eye-ROI) motion vs the nearest face last sample.

    Returns 0.0 when it can't be measured (no landmarks or no match) so the score
    simply falls back to size/confidence rather than guessing.
    """
    import numpy as np
    if face["mouth"] is None or not prev_faces:
        return 0.0
    near = min(prev_faces, key=lambda p: abs(p["cx"] - face["cx"]))
    if abs(near["cx"] - face["cx"]) > gate or near["mouth"] is None:
        return 0.0
    mouth = float(np.mean(np.abs(face["mouth"].astype("int16") - near["mouth"].astype("int16")))) / 255.0
    rigid = 0.0
    if face["eye"] is not None and near["eye"] is not None:
        rigid = float(np.mean(np.abs(face["eye"].astype("int16") - near["eye"].astype("int16")))) / 255.0
    return max(0.0, mouth - LIP_RIGID_COMP * rigid)


def _motion_from_diff(np, cv2, gray, prev_gray, rows, scale):
    """Frame-diff motion. Returns (motion_cx_src or None, per-row local-motion list).

    Global centroid = intensity-weighted mean column of the thresholded diff (where
    the action is). Per-face local motion = the share of that column-motion landing
    inside each face's horizontal band, so a moving/gesturing face scores high and a
    static bystander scores ~0.
    """
    locals_ = [0.0] * len(rows)
    if prev_gray is None or prev_gray.shape != gray.shape:
        return None, locals_
    d = cv2.absdiff(gray, prev_gray)
    if float(d.mean()) / 255.0 < MOTION_FLOOR:
        return None, locals_          # whole frame basically static -> no motion cue
    _, dth = cv2.threshold(d, MOTION_DIFF_THRESH, 255, cv2.THRESH_BINARY)
    colmag = dth.sum(axis=0).astype("float64")   # per-column motion energy
    total = float(colmag.sum())
    if total <= 0:
        return None, locals_
    xs = np.arange(colmag.shape[0], dtype="float64")
    mcx_small = float((xs * colmag).sum() / total)
    for i, r in enumerate(rows):
        fw = (r["area"] * 0.8) ** 0.5            # approx face width from area
        lo = max(0, int(r["cx"] - 0.6 * fw))
        hi = min(colmag.shape[0], int(r["cx"] + 0.6 * fw))
        if hi > lo:
            locals_[i] = float(colmag[lo:hi].sum() / total)
    return mcx_small / scale, locals_


def detect_face_track(video, start, end):
    """Sample the span and return (samples, src_w, src_h, fps).

    samples = [{"t": clip_relative_secs, "mcx": motion_center_src_px|None,
                "dets": [{"cx","area","score","lip","motion"}, ...]}]
    with cx/mcx in SOURCE pixels. The heavy per-pixel work (ROI diffs, motion diff)
    is done here; the pure selection logic in choose_track() only sees these scalars.
    """
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise RuntimeError("OpenCV could not open the video for face tracking.")
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0

    kind, det = _make_detector(cv2)
    step = max(1, int(round(fps / DETECT_FPS)))
    scale = DETECT_WIDTH / src_w if src_w > DETECT_WIDTH else 1.0
    min_face = max(20, int(src_h * 0.06 * scale))
    gate_small = DETECT_WIDTH * RESEED_FRAC   # match window for lip diff (small-frame px)

    cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000.0)
    idx = 0
    prev_rows = []
    prev_gray = None
    samples = []
    while True:
        pos = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if pos > end:
            break
        ok, frame = cap.read()
        if not ok:
            break
        if idx % step == 0:
            small = cv2.resize(frame, (0, 0), fx=scale, fy=scale) if scale != 1.0 else frame
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            rows = _detect_full(cv2, kind, det, small, min_face)
            mcx, locals_ = _motion_from_diff(np, cv2, gray, prev_gray, rows, scale)
            dets = []
            for r, lm in zip(rows, locals_):
                lip = _lip_activity(r, prev_rows, gate_small)
                dets.append({"cx": r["cx"] / scale, "area": r["area"],
                             "score": r["score"], "lip": lip, "motion": lm})
            samples.append({"t": round(pos - start, 3), "mcx": mcx, "dets": dets})
            prev_rows = rows  # keep patches for next-sample diff
            prev_gray = gray
        idx += 1
    cap.release()
    return samples, src_w, src_h, fps


def choose_track(samples, reseed_gap):
    """Pure selection: decide the crop center per sample (active speaker, with
    hysteresis + shot cuts). Returns (times[], centers[], segments[]).

    `segments` increments whenever the tracked subject CUTS (a different person or
    a scene change); build_pan_commands snaps between segments instead of gliding,
    which is what stops the camera drifting through the empty space between people.
    No cv2/numpy here on purpose, so it's unit-testable with synthetic detections.
    """
    times, centers, segments = [], [], []
    cur_cx = None
    seg = -1
    want = {}  # challenger bucket -> consecutive samples it has dominated

    def composite(d, max_lip, max_area, max_motion):
        return (W_LIP * (d["lip"] / max_lip)
                + W_AREA * (d["area"] / max_area)
                + W_SCORE * d["score"]
                + W_MOTION * (d.get("motion", 0.0) / max_motion))

    bucket = max(1.0, reseed_gap * 0.2)
    for s in samples:
        dets = s["dets"]
        if not dets:
            # No face this sample. If there's motion, FOLLOW THE ACTION instead of
            # freezing/letterboxing (the MrBeast-footage win); else hold last center.
            mcx = s.get("mcx")
            if mcx is not None:
                if cur_cx is None:
                    cur_cx = mcx; seg += 1; want.clear()
                elif abs(mcx - cur_cx) > reseed_gap:
                    cur_cx = mcx; seg += 1; want.clear()          # action jumped -> cut
                else:
                    cur_cx = cur_cx + MOTION_LERP * (mcx - cur_cx)  # chase, damped
                times.append(s["t"]); centers.append(cur_cx); segments.append(seg)
            elif cur_cx is not None:          # hold position through a detection gap
                times.append(s["t"]); centers.append(cur_cx); segments.append(seg)
            continue
        max_lip = max((d["lip"] for d in dets), default=0.0) or 1.0
        max_area = max((d["area"] for d in dets), default=0.0) or 1.0
        max_motion = max((d.get("motion", 0.0) for d in dets), default=0.0) or 1.0
        best = max(dets, key=lambda d: composite(d, max_lip, max_area, max_motion))

        if cur_cx is None:                  # first lock
            cur_cx = best["cx"]; seg += 1; want.clear()
        else:
            nearest = min(dets, key=lambda d: abs(d["cx"] - cur_cx))
            if abs(nearest["cx"] - cur_cx) > reseed_gap:
                cur_cx = best["cx"]; seg += 1; want.clear()   # subject lost -> cut
            elif best is nearest:
                cur_cx = nearest["cx"]; want.clear()          # glide with our subject
            elif composite(best, max_lip, max_area, max_motion) - composite(nearest, max_lip, max_area, max_motion) >= SWITCH_MARGIN:
                key = round(best["cx"] / bucket)
                want = {key: want.get(key, 0) + 1}            # one challenger at a time
                if want[key] >= SWITCH_HOLD:
                    if abs(best["cx"] - cur_cx) > reseed_gap:
                        seg += 1                              # far switch -> cut
                    cur_cx = best["cx"]; want.clear()
                else:
                    cur_cx = nearest["cx"]                    # not yet; stay
            else:
                cur_cx = nearest["cx"]; want.clear()

        times.append(s["t"]); centers.append(cur_cx); segments.append(seg)
    return times, centers, segments


def _ema(arr, a):
    import numpy as np
    out = np.empty_like(arr, dtype=float)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = a * arr[i] + (1 - a) * out[i - 1]
    return out


def build_pan_commands(times, centers, segments, src_w, src_h, duration):
    """Dense crop-x keyframes: smooth WITHIN each shot, snap BETWEEN shots."""
    import numpy as np

    crop_w = int(round(src_h * OUT_W / OUT_H))
    crop_w -= crop_w % 2
    if crop_w >= src_w:
        return None, crop_w  # can't crop horizontally -> caller uses letterbox

    max_x = src_w - crop_w
    n = max(2, int(duration * CMD_FPS))
    tc = np.linspace(0, duration, n)

    if not times:
        cx = np.full(n, src_w / 2.0)
    else:
        times_a = np.asarray(times, dtype=float)
        centers_a = np.asarray(centers, dtype=float)
        seg_a = np.asarray(segments)
        cx = np.empty(n, dtype=float)
        # Assign each output frame to the shot active at that time (causal), so a
        # shot change reads as an instant cut, not a slide across the dead middle.
        idx = np.clip(np.searchsorted(times_a, tc, side="right") - 1, 0, len(times_a) - 1)
        tc_seg = seg_a[idx]
        for sg in np.unique(seg_a):
            mask = tc_seg == sg
            if not mask.any():
                continue
            smask = seg_a == sg
            st, sc = times_a[smask], centers_a[smask]
            if len(st) == 1:
                cx[mask] = sc[0]
            else:
                vals = np.interp(tc[mask], st, sc)
                # Zero-phase EMA (forward then backward) cancels phase lag, so the
                # pan stays smooth without trailing the subject.
                cx[mask] = _ema(_ema(vals, EMA_ALPHA)[::-1], EMA_ALPHA)[::-1]

    lines = []
    for t, c in zip(tc, cx):
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
            emit({"path": out_path, "mode": "letterbox", "faces_tracked": 0, "cuts": 0,
                  "width": OUT_W, "height": OUT_H, "duration": round(dur, 3)})
            return

        try:
            samples, src_w, src_h, _fps = detect_face_track(args.inp, args.start, args.end)
        except Exception as e:
            # OpenCV trouble shouldn't kill the clip -- fall back to letterbox.
            render_letterbox(args.inp, args.start, dur, out_path)
            emit({"path": out_path, "mode": "letterbox", "faces_tracked": 0, "cuts": 0,
                  "width": OUT_W, "height": OUT_H, "duration": round(dur, 3),
                  "note": f"Face tracking unavailable ({e}); used letterbox."})
            return

        reseed_gap = src_w * RESEED_FRAC
        times, centers, segments = choose_track(samples, reseed_gap)
        detected = sum(1 for s in samples if s["dets"])
        cmds_text, crop_w = build_pan_commands(times, centers, segments, src_w, src_h, dur)
        if cmds_text is None or (args.mode == "auto" and not times):
            render_letterbox(args.inp, args.start, dur, out_path)
            why = "source narrower than 9:16" if cmds_text is None else "no faces or motion detected"
            emit({"path": out_path, "mode": "letterbox", "faces_tracked": detected, "cuts": 0,
                  "width": OUT_W, "height": OUT_H, "duration": round(dur, 3),
                  "note": f"Fell back to letterbox ({why})."})
            return

        cuts = (len(set(segments)) - 1) if segments else 0
        render_track(args.inp, args.start, dur, crop_w, src_h, cmds_text, out_path)
        emit({"path": out_path, "mode": "track", "faces_tracked": detected, "cuts": cuts,
              "width": OUT_W, "height": OUT_H, "duration": round(dur, 3)})
    except FFmpegMissing as e:
        fail(str(e))
    except Exception as e:
        fail(f"Reframe failed: {e}")


if __name__ == "__main__":
    main()
