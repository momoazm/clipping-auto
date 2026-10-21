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
        --start 12.5 --end 58.0 [--style competitor] [--out .tmp/caps.ass]

Prints JSON: {"path","style","line_count","word_count"}
"""
import argparse
import json
import os
import re

from _common import load_env, emit, fail, load_theme, tmp_path, REPO_ROOT

PLAY_W, PLAY_H = 1080, 1920
SENTENCE_END = (".", "!", "?", "…")
GAP_BREAK = 0.9  # start a new caption line after a pause this long

# --- competitor hook banner -----------------------------------------------------
# Evidence (top MrBeast clip Shorts, 2026-10-21 review): the hook is NOT a 1.6-second title
# card. It is a big ALL-CAPS banner that stays pinned at the top for the WHOLE clip, with the
# money/number and the conflict word picked out in color. Viewers who join mid-clip still get
# the premise, and the banner never eats the punchline.
_HOOK_ALERT = {
    "LOST", "LOSES", "FAILED", "FAILS", "QUIT", "QUITS", "NEVER", "OUT", "ELIMINATED",
    "DISQUALIFIED", "LAST", "BROKE", "STOPPED", "CHEATED", "CAUGHT", "BUSTED", "WRONG",
    "BANNED", "REFUSED", "REJECTED", "FIRED", "MISTAKE", "IMPOSSIBLE", "SCAM", "TRAP",
}
_HOOK_ACCENT = {
    "MONEY", "MILLION", "MILLIONS", "THOUSAND", "CHALLENGE", "PRIZE", "WINNER", "WINS",
    "WON", "WIN", "FREE", "GIVEAWAY", "GAVE", "MAZE", "VAULT", "DEAL", "OFFER", "INVEST",
    "EQUITY", "PROFIT", "ROYALTY", "COMPANY", "BUSINESS", "REVENUE", "SALES",
}
_MONEY_TOKEN = re.compile(r"[$%]|\d", re.UNICODE)


def _hook_tokens(text):
    return [token for token in str(text or "").split() if token]


def wrap_hook(text, max_chars=18, max_lines=3):
    """Wrap the banner to <=3 short ALL-CAPS lines, never mid-word (competitor pattern)."""
    lines, current = [], []
    for token in _hook_tokens(text):
        trial = " ".join(current + [token])
        if len(trial) <= max_chars or not current:
            current.append(token)
        else:
            lines.append(current)
            current = [token]
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1] + ["..."]
    return lines


def hook_char_budget(hook_size):
    """How many characters fit on one banner line at this font size (1080-wide frame)."""
    return max(10, int((PLAY_W * 0.84) / (max(1, int(hook_size)) * 0.52)))


def hook_line_count(hook, hook_size=138):
    return len(wrap_hook(hook, hook_char_budget(hook_size)))


def hook_size_ladder(hook_size):
    """Font sizes to try, largest first, before the banner is allowed to truncate."""
    base = max(60, int(hook_size))
    ladder = [max(60, int(round(base * factor)))
              for factor in (1.0, 0.94, 0.88, 0.82, 0.76, 0.70, 0.64, 0.58)]
    out = []
    for size in ladder:
        if size not in out:
            out.append(size)
    return out


def fit_hook_layout(hook, hook_size=138, max_lines=3):
    """(font_size, lines) for the banner: shrink the type until the hook fits whole.

    A fixed 138px banner ellipsised a real source hook ("...OCEAN AT $16M..." on the
    2026-10-21 dry run), which reads like a broken template. The 3-line ceiling stays;
    the font gives way instead of the sentence.
    """
    tokens = _hook_tokens(hook)
    ladder = hook_size_ladder(hook_size)
    for size in ladder:
        lines = wrap_hook(hook, hook_char_budget(size), max_lines)
        if [token for line in lines for token in line] == tokens:
            return size, lines
    smallest = ladder[-1]
    return smallest, wrap_hook(hook, hook_char_budget(smallest), max_lines)


def hook_colors(style):
    """Resolve the banner palette from the preset, falling back to brand/theme.json."""
    palette = style.get("hook_colors")
    if not palette:
        return None
    theme = load_theme().get("colors", {})
    resolved = {}
    for role, value in palette.items():
        if value == "theme_gold":
            value = theme.get("gold", "#E6B23A")
        resolved[role] = hex_to_ass(value)
    resolved.setdefault("white", hex_to_ass("#FFFFFF"))
    return resolved


def hook_color_for(token, is_last, palette):
    bare = str(token or "").strip(".,!?;:\"'()[]")
    upper = bare.upper()
    if _MONEY_TOKEN.search(bare) or upper.isdigit():
        return palette.get("gold") or palette["white"]
    if upper in _HOOK_ALERT:
        return palette.get("alert") or palette["white"]
    if upper in _HOOK_ACCENT:
        return palette.get("accent") or palette["white"]
    if is_last:
        return palette.get("gold_light") or palette.get("gold") or palette["white"]
    return palette["white"]


def render_hook(hook, palette, uppercase=True, hook_size=138, lines=None):
    """Multi-color banner text: per-word colour tags + explicit \\N line breaks."""
    if lines is None:
        lines = wrap_hook(hook, hook_char_budget(hook_size))
    flat = [(token, index == len(lines) - 1 and pos == len(line) - 1)
            for index, line in enumerate(lines) for pos, token in enumerate(line)]
    rendered, position = [], 0
    for line in lines:
        parts = []
        for _ in line:
            token, is_last = flat[position]
            position += 1
            color = hook_color_for(token, is_last, palette)
            safe = esc(token.upper() if uppercase else token)
            if color == palette["white"]:
                parts.append(safe)
            else:
                parts.append(f"{{\\c{color}}}{safe}{{\\c{palette['white']}}}")
        rendered.append(" ".join(parts))
    return "\\N".join(rendered)


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


def default_style_name():
    """The configured default preset (env CLIP_CAPTION_STYLE wins for A/B runs)."""
    env = str(os.environ.get("CLIP_CAPTION_STYLE") or "").strip()
    if env:
        return env
    cfg_path = REPO_ROOT / "config" / "caption_styles.json"
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f).get("default_style", "hormozi")
    except (OSError, json.JSONDecodeError):
        return "hormozi"


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


def build_ass(words, style, animate, hook=None, hook_secs=1.6,
              total=None, cta_secs=0.0, cta_text=""):
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
    # Hook title card: a bigger, centered-upper line so the clip opens on a strong visual
    # hook (top-clipper convention). Highlight color, top alignment (an=8) pushed into the
    # upper third so it clears the lower-third captions and the speaker's face. Fades in/out
    # via \fad on the event. `hook_mode: persistent` keeps it up for the WHOLE clip with a
    # per-word multi-colour palette (the competitor format); the default stays the 1.6s flash.
    hook_size = int(style.get("hook_font_size") or min(150, int(size * 1.15)))
    hook_size = max(60, hook_size)
    hook_lines = None
    if hook:
        # Shrink the banner type until the whole hook fits (see fit_hook_layout).
        hook_size, hook_lines = fit_hook_layout(hook, hook_size)
    hook_margin = int(style.get("hook_margin_v_px", 380))
    palette = hook_colors(style)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {PLAY_W}
PlayResY: {PLAY_H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caps,{font},{size},{primary},{highlight},{outline},&H64000000,{bold},0,0,0,100,100,0,0,1,{out_px},{shadow},2,80,80,{margin_v},1
Style: Hook,{font},{hook_size},{highlight},{highlight},{outline},&H64000000,{bold},0,0,0,100,100,0,0,1,{out_px + 2},{shadow},8,110,110,{hook_margin},1
Style: CTA,{font},{min(80, int(size * 0.65))},{highlight},{highlight},{outline},&H64000000,{bold},0,0,0,100,100,0,0,1,{out_px},{shadow},2,60,60,150,1

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
        if palette:
            htext = render_hook(hook, palette, uppercase=upper, hook_size=hook_size,
                                lines=hook_lines)
        persistent = str(style.get("hook_mode", "")).lower() == "persistent"
        until = min(float(total), float(hook_secs)) if total else float(hook_secs)
        if persistent and total:
            until = float(total)
            fade = "{\\fad(150,0)}"
        else:
            fade = "{\\fad(150,300)}"
        events.append(
            f"Dialogue: 1,{ass_time(0)},{ass_time(until)},Hook,,0,0,0,,"
            f"{fade}{htext}")
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

    # Follow CTA: a bold pop-in over the LAST cta_secs of the clip, sitting well below the
    # caption band (MarginV 150 vs captions' ~320-360) so the two never collide. Burned onto
    # already-playing footage -- no runtime added, no bolt-on end screen (matches the pattern
    # used across the ranking-shorts/momoclips formats; see decisions/log.md 2026-07-12).
    if total and cta_secs > 0 and cta_text:
        eff = min(cta_secs, total)
        cs = max(0.0, total - eff)
        ctext = esc(cta_text.upper() if upper else cta_text)
        events.append(
            f"Dialogue: 1,{ass_time(cs)},{ass_time(total)},CTA,,0,0,0,,"
            "{\\fad(150,0)\\fscx120\\fscy120\\t(0,250,\\fscx100\\fscy100)}" + ctext)

    return header + "\n".join(events) + "\n", len(events)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--transcript", default=None)
    parser.add_argument("--start", type=float, required=True)
    parser.add_argument("--end", type=float, required=True)
    parser.add_argument("--style", default=None,
                        help="Caption preset from config/caption_styles.json "
                             "(default = config default_style, or CLIP_CAPTION_STYLE")
    parser.add_argument("--hook", default=None, help="Hook title-card text for the clip opening")
    parser.add_argument("--hook-secs", type=float, default=1.6,
                        help="How long the hook card stays up (short cold-open; default 1.6s)")
    parser.add_argument("--cta", dest="cta", action="store_true", default=True,
                        help="Follow CTA pop-in over the last --cta-secs of the clip (default ON).")
    parser.add_argument("--no-cta", dest="cta", action="store_false", help="Disable the follow CTA.")
    parser.add_argument("--cta-secs", type=float, default=2.2, help="CTA pop-in length in seconds.")
    parser.add_argument("--cta-text", default="FOLLOW FOR MORE", help="Follow CTA on-screen text.")
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

    total = max(0.0, args.end - args.start)
    all_words = transcript.get("words") or []
    words = []
    for w in all_words:
        if w["end"] <= args.start or w["start"] >= args.end or not w["w"]:
            continue
        start = max(0.0, w["start"] - args.start)
        end = min(total, w["end"] - args.start)
        if end > start:
            words.append({"w": w["w"], "start": start, "end": end})
    if not words:
        fail("No words fall inside this clip range; nothing to caption.",
             start=args.start, end=args.end)
        return

    style_name, style = load_style(args.style or default_style_name())
    animate = style.get("animation", "none") == "word-highlight"
    ass_text, line_count = build_ass(words, style, animate,
                                     hook=args.hook, hook_secs=args.hook_secs,
                                     total=total, cta_secs=args.cta_secs if args.cta else 0.0,
                                     cta_text=args.cta_text)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(ass_text)

    emit({"path": out_path, "style": style_name, "line_count": line_count,
          "word_count": len(words), "hook": bool(args.hook), "cta": bool(args.cta)})


if __name__ == "__main__":
    main()
