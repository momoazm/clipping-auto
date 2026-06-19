"""Generate styled word-by-word caption subtitles (.ass) for one clip.

Pipeline role: turns the word timestamps from transcribe_video.py into the
animated, karaoke-highlight captions that define the viral look -- 2-4 words on
screen, the currently-spoken word highlighted, fat outline so it reads over any
footage. render_clip.py burns the result with libass.

Times are emitted CLIP-RELATIVE (word.start - clip_start) so they line up with a
reframed clip that begins at 0. Output is 1080x1920 PlayRes so positions/sizes
match the final frame. Styles come from config/caption_styles.json; the
'theme_gold' highlight token pulls brand/theme.json colors.gold so captions stay
on-brand.

Usage:
    python tools/build_captions.py [--transcript .tmp/transcript.json] \
        --start 12.5 --end 58.0 [--style hormozi] [--out .tmp/caps.ass]

Prints JSON: {"path","style","line_count","word_count"}
"""
import argparse
import json
import os

from _common import load_env, emit, fail, load_theme, tmp_path, REPO_ROOT

PLAY_W, PLAY_H = 1080, 1920
SENTENCE_END = (".", "!", "?", "…")
GAP_BREAK = 0.9  # start a new caption line after a pause this long


def hex_to_ass(hexcolor, alpha="00"):
    h = hexcolor.lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha}{b}{g}{r}".upper()


def ass_time(t):
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    cs = int(round((t - int(t)) * 100))
    if cs == 100:
        cs = 0
        s += 1
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def esc(text):
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").strip()


def load_style(name):
    cfg_path = REPO_ROOT / "config" / "caption_styles.json"
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    styles = cfg.get("styles", {})
    if name not in styles:
        name = cfg.get("default_style", "hormozi")
    style = dict(styles[name])
    if style.get("highlight_color") == "theme_gold":
        style["highlight_color"] = load_theme().get("colors", {}).get("gold", "#FFE100")
    return name, style


def group_screens(words, per_screen):
    """Break words into readable on-screen groups by count, pauses, sentences."""
    screens, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        ends_sentence = w["w"].endswith(SENTENCE_END)
        gap = (nxt["start"] - w["end"]) if nxt else 0
        if len(cur) >= per_screen or ends_sentence or gap >= GAP_BREAK or nxt is None:
            screens.append(cur)
            cur = []
    return screens


def build_ass(words, style, animate, hook=None, hook_secs=2.5):
    primary = hex_to_ass(style["primary_color"])
    highlight = hex_to_ass(style["highlight_color"])
    outline = hex_to_ass(style["outline_color"])
    bold = -1 if style.get("bold", True) else 0
    font = style.get("font", "Arial")
    size = int(style.get("font_size", 96))
    out_px = style.get("outline_px", 6)
    shadow = style.get("shadow_px", 2)
    margin_v = style.get("margin_v_px", 340)
    upper = style.get("uppercase", False)
    pop = float(style.get("pop_scale", 1.0))
    # Hook title card: a bigger, centered-upper line for the first few seconds so the
    # clip opens on a strong visual hook (top-clipper convention). Highlight color, top
    # alignment (an=8) pushed into the upper third so it clears the lower-third captions
    # and the speaker's face. Fades in/out via \fad on the event.
    hook_size = min(150, int(size * 1.15))

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {PLAY_W}
PlayResY: {PLAY_H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caps,{font},{size},{primary},{highlight},{outline},&H64000000,{bold},0,0,0,100,100,0,0,1,{out_px},{shadow},2,80,80,{margin_v},1
Style: Hook,{font},{hook_size},{highlight},{highlight},{outline},&H64000000,{bold},0,0,0,100,100,0,0,1,{out_px + 2},{shadow},8,110,110,380,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def render_word(text, active):
        text = esc(text.upper() if upper else text)
        if not animate:
            return text
        if active:
            tags = f"{{\\1c{highlight}"
            if pop != 1.0:
                tags += f"\\fscx{int(pop*100)}\\fscy{int(pop*100)}"
            tags += "}"
            return f"{tags}{text}{{\\r}}"
        return text

    events = []
    if hook:
        htext = esc(hook.upper() if upper else hook)
        events.append(
            f"Dialogue: 1,{ass_time(0)},{ass_time(hook_secs)},Hook,,0,0,0,,"
            f"{{\\fad(150,300)}}{htext}")
    screens = group_screens(words, style.get("words_per_screen", 3))
    for screen in screens:
        if not animate:
            start, end = screen[0]["start"], screen[-1]["end"]
            line = " ".join(esc(w["w"].upper() if upper else w["w"]) for w in screen)
            events.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Caps,,0,0,0,,{line}")
            continue
        # One event per active word so the highlight steps through the line.
        for j, w in enumerate(screen):
            start = w["start"]
            end = screen[j + 1]["start"] if j + 1 < len(screen) else w["end"]
            if end <= start:
                end = start + 0.12
            parts = [render_word(sw["w"], active=(k == j)) for k, sw in enumerate(screen)]
            events.append(
                f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Caps,,0,0,0,,{' '.join(parts)}")

    return header + "\n".join(events) + "\n", len(events)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--transcript", default=None)
    parser.add_argument("--start", type=float, required=True)
    parser.add_argument("--end", type=float, required=True)
    parser.add_argument("--style", default="hormozi")
    parser.add_argument("--hook", default=None, help="Hook title-card text for the clip opening")
    parser.add_argument("--hook-secs", type=float, default=2.5, help="How long the hook card stays up")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    load_env()
    tpath = args.transcript or tmp_path("transcript.json")
    out_path = args.out or tmp_path("caps.ass")

    if not os.path.isfile(tpath):
        fail(f"Transcript not found: {tpath}. Run transcribe_video.py first.")
        return
    with open(tpath, "r", encoding="utf-8") as f:
        transcript = json.load(f)

    all_words = transcript.get("words") or []
    words = [
        {"w": w["w"], "start": w["start"] - args.start, "end": w["end"] - args.start}
        for w in all_words
        if w["end"] > args.start and w["start"] < args.end and w["w"]
    ]
    if not words:
        fail("No words fall inside this clip range; nothing to caption.",
             start=args.start, end=args.end)
        return

    style_name, style = load_style(args.style)
    animate = style.get("animation", "none") == "word-highlight"
    ass_text, line_count = build_ass(words, style, animate,
                                     hook=args.hook, hook_secs=args.hook_secs)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(ass_text)

    emit({"path": out_path, "style": style_name, "line_count": line_count,
          "word_count": len(words), "hook": bool(args.hook)})


if __name__ == "__main__":
    main()
