"""Transcribe a video to word-level-timestamped text via the Groq Whisper API.

Pipeline role: produces the transcript that select_clips.py scores and that
build_captions.py turns into animated word-by-word captions, so WORD timestamps
are the whole point.

How it works:
  1. ffmpeg extracts a small 16 kHz mono audio track (low-bitrate mp3) so even a
     long video stays well under Groq's per-file size limit.
  2. If the extracted audio still exceeds the size budget, it's split into
     time-based chunks; each chunk is transcribed and its timestamps are shifted
     by the chunk's start offset, then merged back into one continuous transcript.
  3. Model: whisper-large-v3-turbo with response_format=verbose_json and
     timestamp_granularities=["word","segment"].

Only Groq is wired up (it's the configured key and is fast + free-tier friendly).
If the whole chain fails, that surfaces to the agent rather than looping.

Usage:
    python tools/transcribe_video.py --in path/to/video.mp4 [--lang en] [--out .tmp/transcript.json]

Prints JSON: {"path","language","duration","word_count","segment_count","chunks"}
The full transcript {language,duration,segments,words} is written to --out.
"""
import argparse
import json
import math
import os

from _common import load_env, emit, fail, run, ffmpeg_bin, ffprobe_json, tmp_path, FFmpegMissing

# Default to the full-accuracy model (better than -turbo on noisy/crowd audio,
# which is where captions were weakest). Override via --model or GROQ_WHISPER_MODEL.
DEFAULT_MODEL = "whisper-large-v3"
# Groq's free tier caps upload size ~25 MB, but a single big upload also tends to
# blow the request timeout. Keep each chunk small enough to transcribe quickly and
# retry cheaply (a ~39 min source -> ~19 MB audio used to go as one request and time
# out; at 14 MB it splits into two bounded requests).
MAX_CHUNK_BYTES = 14 * 1024 * 1024
AUDIO_BITRATE = "64k"  # 16 kHz mono mp3 @ 64k ~= 0.48 MB/min
# Light, conservative cleanup that helps ASR without distorting speech:
# drop sub-90Hz rumble, then dynamically normalize so quiet talkers come up.
AUDIO_FILTER = "highpass=f=90,dynaudnorm=f=200:g=15"


def extract_audio(video_path, out_path, start=None, duration=None):
    cmd = [ffmpeg_bin(), "-y"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(video_path)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", "-af", AUDIO_FILTER,
            "-b:a", AUDIO_BITRATE, str(out_path)]
    run(cmd)
    return out_path


def _transcription_to_dict(resp):
    """Groq SDK returns a pydantic-ish object; coerce to a plain dict."""
    for attr in ("model_dump", "to_dict"):
        fn = getattr(resp, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    if isinstance(resp, dict):
        return resp
    # Last resort: pull known fields off attributes.
    return {
        "text": getattr(resp, "text", ""),
        "language": getattr(resp, "language", None),
        "words": getattr(resp, "words", None),
        "segments": getattr(resp, "segments", None),
    }


def groq_transcribe(audio_path, language, model):
    from groq import Groq

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set")

    # Audio uploads + full transcription routinely exceed the SDK's short default
    # timeout (surfaces as "Request timed out."). Give it generous headroom and let
    # the SDK transparently retry transient timeouts/5xx.
    client = Groq(api_key=api_key, timeout=600.0, max_retries=3)
    kwargs = dict(
        model=model,
        response_format="verbose_json",
        timestamp_granularities=["word", "segment"],
    )
    if language and language != "auto":
        kwargs["language"] = language

    with open(audio_path, "rb") as f:
        resp = client.audio.transcriptions.create(
            file=(os.path.basename(audio_path), f.read()), **kwargs)
    return _transcription_to_dict(resp)


def _norm_words(raw):
    out = []
    for w in raw or []:
        text = (w.get("word") if isinstance(w, dict) else getattr(w, "word", None)) or ""
        start = w.get("start") if isinstance(w, dict) else getattr(w, "start", None)
        end = w.get("end") if isinstance(w, dict) else getattr(w, "end", None)
        if start is None or end is None:
            continue
        out.append({"w": text.strip(), "start": float(start), "end": float(end)})
    return out


def _norm_segments(raw):
    out = []
    for s in raw or []:
        g = s.get if isinstance(s, dict) else (lambda k, d=None: getattr(s, k, d))
        start, end = g("start"), g("end")
        if start is None or end is None:
            continue
        out.append({"text": (g("text") or "").strip(), "start": float(start), "end": float(end)})
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="inp", required=True)
    parser.add_argument("--lang", default="auto", help="ISO code like 'en', or 'auto' to detect")
    parser.add_argument("--model", default=None, help="Groq Whisper model (default whisper-large-v3)")
    parser.add_argument("--out", default=None, help="Transcript JSON path (default .tmp/transcript.json)")
    args = parser.parse_args()

    load_env()
    out_path = args.out or tmp_path("transcript.json")
    model = args.model or os.environ.get("GROQ_WHISPER_MODEL") or DEFAULT_MODEL

    if not os.path.isfile(args.inp):
        fail(f"Input not found: {args.inp}")
        return

    try:
        info = ffprobe_json(args.inp)
    except FFmpegMissing as e:
        fail(str(e))
        return
    except Exception as e:
        fail(f"ffprobe could not read this file: {e}")
        return

    total_dur = float(info.get("format", {}).get("duration") or 0)
    if not any(s.get("codec_type") == "audio" for s in info.get("streams", [])):
        fail("This video has no audio track to transcribe.", path=args.inp)
        return

    # Extract full audio, then decide whether to chunk by actual file size.
    try:
        full_audio = extract_audio(args.inp, tmp_path("audio_full.mp3"))
    except FFmpegMissing as e:
        fail(str(e))
        return
    except Exception as e:
        fail(f"ffmpeg audio extraction failed: {e}")
        return

    size = os.path.getsize(full_audio)
    if size <= MAX_CHUNK_BYTES or total_dur <= 0:
        offsets = [(0.0, None, full_audio)]
    else:
        n_chunks = math.ceil(size / MAX_CHUNK_BYTES)
        chunk_dur = total_dur / n_chunks
        offsets = []
        for i in range(n_chunks):
            start = i * chunk_dur
            cpath = tmp_path(f"audio_chunk_{i:02d}.mp3")
            try:
                extract_audio(args.inp, cpath, start=start, duration=chunk_dur)
            except Exception as e:
                fail(f"ffmpeg failed extracting chunk {i}: {e}")
                return
            offsets.append((start, chunk_dur, cpath))

    words, segments = [], []
    try:
        for start, _dur, apath in offsets:
            t = groq_transcribe(apath, args.lang, model)
            for w in _norm_words(t.get("words")):
                words.append({"w": w["w"], "start": w["start"] + start, "end": w["end"] + start})
            for s in _norm_segments(t.get("segments")):
                segments.append({"text": s["text"], "start": s["start"] + start, "end": s["end"] + start})
            language = t.get("language")
    except Exception as e:
        fail(f"Groq transcription failed: {e}. (Only Groq is configured for transcription.)")
        return

    words.sort(key=lambda x: x["start"])
    segments.sort(key=lambda x: x["start"])

    transcript = {
        "source": args.inp,
        "language": language,
        "duration": total_dur or (words[-1]["end"] if words else 0),
        "segments": segments,
        "words": words,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(transcript, f, ensure_ascii=False, indent=2)

    emit({
        "path": out_path,
        "language": language,
        "model": model,
        "duration": transcript["duration"],
        "word_count": len(words),
        "segment_count": len(segments),
        "chunks": len(offsets),
    })


if __name__ == "__main__":
    main()
