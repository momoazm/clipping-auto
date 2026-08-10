"""Rank transcript-selected clips against sampled source-video storyboards.

The transcript selector is good at finding spoken hooks, but it cannot tell whether a
vertical crop will show the event the hook describes.  This tool adds a small, bounded
multimodal pass: it samples each candidate at evenly spaced source times, lays the
frames out in a labelled storyboard, and asks Gemini to score the visible payoff and
identify the subject/action anchor for reframing.

The tool is deliberately non-blocking for the daily run.  Missing credentials,
provider errors, malformed JSON, or an OpenCV decode problem return the original text
candidates with ``visual_fallback`` metadata instead of turning a transient vision
outage into a failed upload slot.

Usage:
    python tools/rank_visual_clips.py --video source.mp4 \
        --transcript .tmp/transcript.json --clips .tmp/clips.json --count 6 \
        --out .tmp/clips_visual.json
"""

import argparse
import base64
import copy
import json
import os
from pathlib import Path

from _common import emit, load_env, tmp_path


PANEL_W = 220
PANEL_H = 124
LABEL_W = 185
ROW_H = 148
DEFAULT_FRAME_COUNT = 8
MAX_CANDIDATES = 12
MAX_ACTION_REFINES = 3
REFINE_PANEL_W = 360
REFINE_PANEL_H = 203
REFINE_ROW_H = 235


def _strip_fences(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text[: text.rfind("```")]
    i, j = text.find("{"), text.rfind("}")
    return text[i : j + 1] if i != -1 and j != -1 else text


def _number(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default  # reject NaN


def _clamp(value, low, high, default):
    number = _number(value, default)
    return max(low, min(high, number))


def _clip_text(words, start, end, limit=560):
    text = " ".join(
        str(word.get("w", "")).strip()
        for word in words
        if word.get("end", 0) > start and word.get("start", 0) < end and word.get("w")
    ).strip()
    return text[:limit].rstrip() + ("..." if len(text) > limit else "")


def _sample_times(start, end, count, semantic_anchor=None):
    duration = max(0.01, end - start)
    if semantic_anchor is not None:
        anchor = _number(semantic_anchor)
        if anchor is not None and start <= anchor <= end:
            # Transcript action words are much better temporal priors than a uniform
            # grid for short physical payoffs. Keep the first/last context frames, but
            # spend the remaining samples around the spoken event so a one-second
            # elimination cannot disappear between two storyboard panels.
            offsets = [-2.0, -0.8, -0.25, 0.0, 0.35, 0.9, 2.0, 4.0]
            times = [start, *(anchor + offset for offset in offsets), end]
            unique = []
            for time in times:
                clipped = max(start, min(end, time))
                if not unique or abs(clipped - unique[-1]) > 0.08:
                    unique.append(clipped)
            if len(unique) >= count:
                # Preserve chronological order and downsample only after the anchor
                # neighbourhood has been constructed.
                positions = [round(i * (len(unique) - 1) / (count - 1)) for i in range(count)]
                return [unique[pos] for pos in positions]
            while len(unique) < count:
                unique.append(end)
            return unique[:count]
    if count <= 1:
        return [start + duration / 2.0]
    # Include the exact span edges. The transcript selector has already snapped the
    # boundaries to words, so seeing both edges helps the model distinguish a setup
    # frame from a payoff frame without adding an arbitrary temporal bias.
    return [start + duration * i / (count - 1) for i in range(count)]


def _put_text(cv2, image, text, origin, scale=0.45, color=(235, 235, 235), thickness=1):
    cv2.putText(
        image, str(text), origin, cv2.FONT_HERSHEY_SIMPLEX,
        scale, color, thickness, cv2.LINE_AA,
    )


def _sample_frames(cv2, cap, times):
    """Decode one chronological pass instead of seeking once per storyboard panel."""
    if not times:
        return []
    frames = []
    cap.set(cv2.CAP_PROP_POS_MSEC, float(times[0]) * 1000.0)
    target_idx = 0
    while target_idx < len(times):
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        position = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if position != position:  # defensive NaN check for unusual decoders
            position = times[target_idx]
        while target_idx < len(times) and position + 0.05 >= times[target_idx]:
            frames.append(frame.copy())
            target_idx += 1
    while len(frames) < len(times):
        frames.append(None)
    return frames


def _storyboard(cv2, video, candidates, words, frame_count):
    """Return (JPEG bytes, absolute sample times by candidate id, canvas)."""
    import numpy as np

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError("OpenCV could not open the source video")

    rows = min(len(candidates), MAX_CANDIDATES)
    canvas = np.full(
        (max(1, rows) * ROW_H, LABEL_W + frame_count * PANEL_W, 3),
        (25, 25, 25), dtype=np.uint8,
    )
    sample_times = {}
    try:
        for row_idx, candidate in enumerate(candidates[:MAX_CANDIDATES]):
            cid = str(candidate["id"])
            start = float(candidate["start"])
            end = float(candidate["end"])
            semantic_anchor = candidate.get("action_anchor") if candidate.get("focus_mode") == "action" else None
            times = _sample_times(start, end, frame_count, semantic_anchor=semantic_anchor)
            sample_times[cid] = times
            row_y = row_idx * ROW_H
            label = canvas[row_y : row_y + ROW_H, :LABEL_W]
            label[:] = (34, 34, 34)
            _put_text(cv2, label, cid, (12, 25), scale=0.65, color=(80, 220, 255), thickness=2)
            _put_text(cv2, label, f"{start:.1f}-{end:.1f}s", (12, 48), scale=0.42)
            _put_text(cv2, label, "frames left -> right", (12, 69), scale=0.38,
                      color=(170, 170, 170))
            excerpt = _clip_text(words, start, end, limit=180)
            # Two compact transcript lines give the model semantic context without
            # making the image itself the source of truth for spoken wording.
            words_for_label = excerpt.split()
            line = ""
            lines = []
            for token in words_for_label:
                if len(line) + len(token) + 1 > 24:
                    lines.append(line)
                    line = token
                else:
                    line = (line + " " + token).strip()
            if line:
                lines.append(line)
            for line_idx, line in enumerate(lines[:4]):
                _put_text(cv2, label, line, (12, 91 + line_idx * 14), scale=0.29,
                          color=(205, 205, 205))

            frames = _sample_frames(cv2, cap, times)
            for frame_idx, (source_time, frame) in enumerate(zip(times, frames)):
                if frame is None:
                    panel = np.zeros((PANEL_H, PANEL_W, 3), dtype=np.uint8)
                    _put_text(cv2, panel, "decode failed", (8, PANEL_H // 2), scale=0.42,
                              color=(100, 100, 255))
                else:
                    panel = cv2.resize(frame, (PANEL_W, PANEL_H), interpolation=cv2.INTER_AREA)
                x = LABEL_W + frame_idx * PANEL_W
                canvas[row_y : row_y + PANEL_H, x : x + PANEL_W] = panel
                # Make frame identity unambiguous: the model must return the panel
                # number, not a pixel coordinate guessed from the whole canvas.
                cv2.rectangle(canvas, (x, row_y), (x + 58, row_y + 19), (0, 0, 0), -1)
                _put_text(cv2, canvas, f"{frame_idx + 1} {source_time - start:.1f}s",
                          (x + 4, row_y + 14), scale=0.31, color=(255, 255, 255))
    finally:
        cap.release()

    ok, encoded = cv2.imencode(".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    if not ok:
        raise RuntimeError("OpenCV could not encode the visual storyboard")
    return encoded.tobytes(), sample_times, canvas


def _refine_storyboard(cv2, video, candidate, frame_count):
    """Build a larger single-candidate storyboard for action/subject verification."""
    import numpy as np

    start = float(candidate["start"])
    end = float(candidate["end"])
    semantic_anchor = candidate.get("action_anchor")
    times = _sample_times(start, end, frame_count, semantic_anchor=semantic_anchor)
    canvas = np.full(
        (REFINE_ROW_H, frame_count * REFINE_PANEL_W, 3), (25, 25, 25), dtype=np.uint8,
    )
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError("OpenCV could not open the source video for action refinement")
    try:
        frames = _sample_frames(cv2, cap, times)
        for frame_idx, (source_time, frame) in enumerate(zip(times, frames)):
            if frame is None:
                panel = np.zeros((REFINE_PANEL_H, REFINE_PANEL_W, 3), dtype=np.uint8)
                _put_text(cv2, panel, "decode failed", (12, REFINE_PANEL_H // 2), scale=0.55,
                          color=(100, 100, 255))
            else:
                panel = cv2.resize(frame, (REFINE_PANEL_W, REFINE_PANEL_H), interpolation=cv2.INTER_AREA)
            x = frame_idx * REFINE_PANEL_W
            canvas[:REFINE_PANEL_H, x : x + REFINE_PANEL_W] = panel
            cv2.rectangle(canvas, (x, 0), (x + 82, 23), (0, 0, 0), -1)
            _put_text(cv2, canvas, f"{frame_idx + 1} {source_time - start:.2f}s",
                      (x + 6, 17), scale=0.39, color=(255, 255, 255))
    finally:
        cap.release()
    ok, encoded = cv2.imencode(".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    if not ok:
        raise RuntimeError("OpenCV could not encode the action-refinement storyboard")
    return encoded.tobytes(), times, canvas


def _vision_prompt(candidates, words, frame_count):
    lines = []
    for candidate in candidates[:MAX_CANDIDATES]:
        start, end = float(candidate["start"]), float(candidate["end"])
        excerpt = _clip_text(words, start, end)
        lines.append(f"{candidate['id']} [{start:.1f}-{end:.1f}s]: {excerpt}")
    candidate_text = "\n".join(lines)
    return f"""You are the final visual editor for a MrBeast vertical-short pipeline.

The attached image is a storyboard of {len(candidates[:MAX_CANDIDATES])} candidate clips. Each
candidate occupies one row, top to bottom. Within a row, panels are chronological from left to
right and are numbered 1-{frame_count}. The source is 16:9; subject_x must be the horizontal
center of the intended person/action in the original source frame, normalized from 0 (left edge)
to 1 (right edge), not the center of the storyboard row.

Transcript evidence for the rows (use this to understand the spoken payoff, but never invent a
visual event that the frames do not show):
{candidate_text}

Score each candidate for a publishable Short, not merely an interesting sentence. Prefer a visible,
self-contained payoff, a clear main subject, and an action/reveal that remains understandable in a
vertical crop. Penalize setup-only clips, graphic-only frames, empty wide shots, duplicate moments,
and reaction-only footage. A reaction can be retained only when the preceding payoff is visibly
included in the same candidate. For an elimination/exit/physical event, identify the contestant or
object that performs the event; never choose the host, an announcer, a bystander's reaction, or a
red X/money graphic as the subject. If the exact action is not visible, set confidence low and use
focus_mode "wide" when preserving the full scene is safer than a misleading zoom.

Return ONLY valid JSON in exactly this shape:
{{"ranked":[{{"id":"c01","keep":true,"visual_score":0,"focus_mode":"action|speaker|wide",
"action_frame":1,"subject_x":0.5,"confidence":0.0,"reaction_only":false,
"title_quote":"exact short quote from that row's transcript, or empty",
"reason":"one concise evidence-based sentence"}}]}}

Include one result for every candidate id. Rank best-first. `action_frame` is the panel where the
main visible payoff occurs; for speaker clips it is the clearest speaking subject frame. Never use
an unsupported title quote or claim an outcome absent from the transcript."""


def _refine_prompt(candidate, words, frame_count):
    start, end = float(candidate["start"]), float(candidate["end"])
    excerpt = _clip_text(words, start, end)
    anchor = candidate.get("action_anchor")
    anchor_note = (
        f"The transcript action anchor is around source time {float(anchor):.3f}s."
        if anchor is not None else "No transcript action anchor is available."
    )
    return f"""Inspect this single MrBeast candidate as a crop supervisor.

The attached image contains {frame_count} large chronological panels, numbered left to right.
The source span is {start:.3f}-{end:.3f}s. {anchor_note}
Transcript for this span:
"{excerpt}"

Identify the ONE main person or physical action that the Short must show. For an elimination or
exit, choose the contestant who leaves, not MrBeast/the host, a graphic, or people reacting after
the event. Use the panel that actually contains the action; if the action is not visible, say so,
mark reaction_only true only when the span is genuinely reaction-only, and use focus_mode "wide"
when a misleading close crop would be worse. subject_x is the intended subject center in the
original 16:9 source frame, normalized 0 left to 1 right.

Return ONLY JSON:
{{"keep":true,"focus_mode":"action|speaker|wide","action_frame":1,
"subject_x":0.5,"confidence":0.0,"reaction_only":false,
"reason":"one evidence-based sentence"}}"""


def _call_gemini(httpx, image_bytes, prompt):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    model = os.environ.get("GEMINI_VISION_MODEL") or os.environ.get(
        "GEMINI_TEXT_MODEL", "gemini-2.5-flash"
    )
    try:
        timeout = max(20.0, min(120.0, float(os.environ.get("CLIP_VISION_TIMEOUT_SEC", "90"))))
    except (TypeError, ValueError):
        timeout = 90.0
    body = {
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {
                "mime_type": "image/jpeg",
                "data": base64.b64encode(image_bytes).decode("ascii"),
            }},
        ]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "maxOutputTokens": 3000,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    response = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": key}, json=body, timeout=timeout,
    )
    response.raise_for_status()
    parts = response.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        raise RuntimeError("Gemini returned no visual ranking")
    return json.loads(_strip_fences(text))


def _boolean(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
    return default


def _normalise_refinement(raw, candidate, sample_times, frame_count):
    if not isinstance(raw, dict):
        raise ValueError("action refinement was not a JSON object")
    frame = int(round(_clamp(raw.get("action_frame"), 1, frame_count, 1)))
    focus = str(raw.get("focus_mode", "wide")).strip().lower()
    if focus not in {"action", "speaker", "wide"}:
        focus = "wide"
    confidence = _clamp(raw.get("confidence"), 0, 1, 0)
    subject_x = _clamp(raw.get("subject_x"), 0, 1, 0.5)
    reaction_only = _boolean(raw.get("reaction_only"), False)
    score = _number(raw.get("visual_score"), None)
    return {
        "keep": _boolean(raw.get("keep"), True) and not reaction_only,
        "visual_score": int(round(max(0, min(100, score)))) if score is not None else None,
        "focus_mode": focus,
        "action_frame": frame,
        "subject_x": round(subject_x, 4),
        "confidence": round(confidence, 4),
        "reaction_only": reaction_only,
        "reason": str(raw.get("reason", "") or "").strip()[:280],
        "visual_anchor": round(sample_times[frame - 1], 3),
    }


def _normalise_results(raw, candidates, sample_times, frame_count):
    if not isinstance(raw, dict) or not isinstance(raw.get("ranked"), list):
        raise ValueError("visual response did not contain a ranked list")
    allowed = {str(candidate["id"]): candidate for candidate in candidates}
    result = []
    seen = set()
    for item in raw["ranked"]:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id", "")).strip()
        if cid not in allowed or cid in seen:
            continue
        seen.add(cid)
        frame = int(round(_clamp(item.get("action_frame"), 1, frame_count, 1)))
        focus = str(item.get("focus_mode", "speaker")).strip().lower()
        if focus not in {"action", "speaker", "wide"}:
            focus = "speaker"
        score = int(round(_clamp(item.get("visual_score"), 0, 100, 0)))
        confidence = _clamp(item.get("confidence"), 0, 1, 0)
        subject_x = _clamp(item.get("subject_x"), 0, 1, 0.5)
        keep = _boolean(item.get("keep"), True)
        reaction_only = _boolean(item.get("reaction_only"), False)
        if reaction_only:
            keep = False
        entry = {
            "id": cid,
            "keep": keep,
            "visual_score": score,
            "focus_mode": focus,
            "action_frame": frame,
            "subject_x": round(subject_x, 4),
            "confidence": round(confidence, 4),
            "reaction_only": reaction_only,
            "title_quote": str(item.get("title_quote", "") or "").strip()[:180],
            "reason": str(item.get("reason", "") or "").strip()[:280],
            "visual_anchor": round(sample_times[cid][frame - 1], 3),
        }
        result.append(entry)
    # Complete a partial-but-parseable model response with the selector's candidates.
    # This keeps a truncated JSON response from silently shrinking a six-clip run to
    # one or two uploads. Missing entries are deliberately low-confidence and never
    # receive a new visual subject lock.
    for candidate in candidates:
        cid = str(candidate["id"])
        if cid in seen:
            continue
        start, end = float(candidate["start"]), float(candidate["end"])
        anchor = _number(candidate.get("action_anchor"), start + (end - start) / 2.0)
        frame = min(
            range(1, frame_count + 1),
            key=lambda index: abs(sample_times[cid][index - 1] - anchor),
        )
        result.append({
            "id": cid,
            "keep": True,
            "visual_score": int(round(_clamp(candidate.get("virality_score"), 0, 100, 0))),
            "focus_mode": str(candidate.get("focus_mode", "speaker")),
            "action_frame": frame,
            "subject_x": 0.5,
            "confidence": 0.0,
            "reaction_only": False,
            "title_quote": "",
            "reason": "Visual response omitted this candidate; retained as a low-confidence fallback.",
            "visual_anchor": round(sample_times[cid][frame - 1], 3),
        })
    # Models occasionally return a prose-sorted list with an imperfect score. The
    # numeric sort makes the selection deterministic and keeps virality as a tie-breaker.
    result.sort(
        key=lambda item: (
            -item["visual_score"],
            -(float(allowed[item["id"]].get("virality_score") or 0)),
            float(allowed[item["id"]]["start"]),
        )
    )
    return result


def _refine_action_candidates(cv2, httpx, video, candidates, words, visual, frame_count):
    """Re-check physical-event candidates at a larger resolution around their anchors."""
    visual_by_id = {item["id"]: item for item in visual}
    targets = [
        candidate for candidate in candidates
        if candidate.get("action_anchor") is not None
        or candidate.get("focus_mode") == "action"
    ]
    targets.sort(
        key=lambda candidate: (
            -float(visual_by_id.get(str(candidate.get("id")), {}).get("visual_score") or 0),
            -float(candidate.get("virality_score") or 0),
        )
    )
    refined = {}
    for candidate in targets[:MAX_ACTION_REFINES]:
        cid = str(candidate["id"])
        try:
            image_bytes, sample_times, _canvas = _refine_storyboard(
                cv2, video, candidate, frame_count,
            )
            raw = _call_gemini(
                httpx, image_bytes, _refine_prompt(candidate, words, frame_count),
            )
            refined[cid] = _normalise_refinement(
                raw, candidate, sample_times, frame_count,
            )
        except Exception:
            # Bulk ranking remains valid if one candidate's high-resolution refinement
            # hits a rate limit or decode issue. Do not turn a single bad frame into a
            # source-attempt failure.
            continue
    return refined


def _write_payload(path, payload):
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def _fallback(candidates, count, reason, out_path):
    clips = []
    for candidate in candidates[:count]:
        item = copy.deepcopy(candidate)
        item["visual_fallback"] = True
        item["visual_error"] = str(reason)[:240]
        clips.append(item)
    payload = {
        "provider": "visual-fallback",
        "count": len(clips),
        "target_count": count,
        "visual_fallback": True,
        "visual_error": str(reason)[:240],
        "clips": clips,
    }
    _write_payload(out_path, payload)
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True)
    parser.add_argument("--transcript", required=True)
    parser.add_argument("--clips", required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--frames", type=int, default=DEFAULT_FRAME_COUNT)
    parser.add_argument("--out", default=None)
    parser.add_argument("--storyboard-out", default=None,
                        help="Optional JPEG path for local visual QA")
    args = parser.parse_args()

    load_env()
    out_path = args.out or tmp_path("clips_visual.json")
    try:
        with open(args.clips, "r", encoding="utf-8") as handle:
            clips_data = json.load(handle)
        candidates = clips_data.get("clips", []) if isinstance(clips_data, dict) else clips_data
        valid_candidates = []
        for idx, item in enumerate(candidates or [], start=1):
            if not isinstance(item, dict):
                continue
            try:
                if float(item.get("end", 0)) <= float(item.get("start", 0)):
                    continue
            except (TypeError, ValueError):
                continue
            valid_candidates.append(dict(item, id=f"c{idx:02d}"))
        candidates = valid_candidates[:MAX_CANDIDATES]
        with open(args.transcript, "r", encoding="utf-8") as handle:
            transcript = json.load(handle)
        words = transcript.get("words", []) if isinstance(transcript, dict) else []
    except Exception as exc:
        # There is no useful fallback if selector input itself is unreadable.
        _write_payload(out_path, {"error": f"visual ranker input failed: {exc}", "clips": []})
        emit({"error": f"visual ranker input failed: {exc}", "clips": []})
        return

    if not candidates:
        payload = _fallback([], args.count, "no candidates", out_path)
        emit(payload)
        return

    frame_count = max(4, min(10, int(args.frames)))
    try:
        import cv2
        import httpx

        image_bytes, sample_times, canvas = _storyboard(
            cv2, args.video, candidates, words, frame_count,
        )
        if args.storyboard_out:
            Path(args.storyboard_out).parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(args.storyboard_out), canvas)
        raw = _call_gemini(
            httpx, image_bytes, _vision_prompt(candidates, words, frame_count),
        )
        visual = _normalise_results(raw, candidates, sample_times, frame_count)
        refinements = _refine_action_candidates(
            cv2, httpx, args.video, candidates, words, visual, frame_count,
        )
        for visual_item in visual:
            correction = refinements.get(visual_item["id"])
            if not correction:
                continue
            # The single-candidate pass is higher resolution and semantically anchored,
            # so its subject/frame/focus fields supersede the thumbnail pass. Preserve the
            # bulk score if the refinement omitted it.
            for key in ("keep", "focus_mode", "action_frame", "subject_x", "confidence",
                        "reaction_only", "reason", "visual_anchor"):
                visual_item[key] = correction[key]
            if correction.get("visual_score") is not None:
                visual_item["visual_score"] = correction["visual_score"]
        by_id = {str(candidate["id"]): candidate for candidate in candidates}
        ranked = []
        for visual_item in visual:
            candidate = copy.deepcopy(by_id[visual_item["id"]])
            original_focus = str(candidate.get("focus_mode", "speaker")).strip().lower()
            action_lock_allowed = (
                candidate.get("action_anchor") is not None
                or original_focus == "action"
            )
            candidate.update({
                "visual_score": visual_item["visual_score"],
                "visual_focus_mode": visual_item["focus_mode"],
                "visual_confidence": visual_item["confidence"],
                "visual_subject_x": visual_item["subject_x"],
                "visual_action_frame": visual_item["action_frame"],
                "visual_anchor": visual_item["visual_anchor"],
                "visual_reaction_only": visual_item["reaction_only"],
                "visual_reason": visual_item["reason"],
                "visual_title_quote": visual_item["title_quote"],
                "visual_refined": visual_item["id"] in refinements,
                "visual_fallback": False,
            })
            if (
                visual_item["focus_mode"] == "action"
                and visual_item["confidence"] >= 0.55
                and action_lock_allowed
            ):
                candidate["focus_mode"] = "action"
                candidate["action_anchor"] = visual_item["visual_anchor"]
                candidate["subject_x"] = visual_item["subject_x"]
            elif visual_item["focus_mode"] == "wide" and visual_item["confidence"] >= 0.55:
                candidate["focus_mode"] = "wide"
                candidate["subject_x"] = visual_item["subject_x"]
            # For a speaker result, retain the transcript/action metadata and only
            # record the visual finding; the normal active-speaker tracker remains the
            # safer crop controller until a high-confidence physical anchor exists.
            ranked.append((visual_item, candidate))

        # Keep explicit visual rejects out when there are enough alternatives, but never
        # return fewer than the orchestrator's minimum. This preserves delivery reliability
        # on a scene where the model is conservative about reaction footage.
        ranked.sort(
            key=lambda pair: (
                -pair[0]["visual_score"],
                -(float(pair[1].get("virality_score") or 0)),
                float(pair[1]["start"]),
            )
        )
        kept = [item for visual_item, item in ranked if visual_item["keep"]]
        if len(kept) < args.count:
            kept.extend(item for _visual_item, item in ranked if item not in kept)
        clips = kept[:args.count]
        payload = {
            "provider": "gemini-vision",
            "count": len(clips),
            "target_count": args.count,
            "candidate_count": len(candidates),
            "refined_count": len(refinements),
            "visual_fallback": False,
            "clips": clips,
        }
        _write_payload(out_path, payload)
        emit(payload)
    except Exception as exc:
        payload = _fallback(candidates, args.count, exc, out_path)
        emit(payload)


if __name__ == "__main__":
    main()
