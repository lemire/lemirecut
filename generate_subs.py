#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13,<3.14"
# dependencies = [
#   "whisper-timestamped",
#   "pysubs2",
#   "auditok",
# ]
# ///
"""
Step 1 of 2 — Transcribe a video and generate a karaoke .ass subtitle file.

Pipeline:  whisper-timestamped  ->  word-level timestamps  ->  pysubs2

Highlight modes:
  word   (default)  only the current word is lit; the rest stay white.
  sweep             classic karaoke: words fill with color progressively (\\k).

Usage (uv resolves the deps automatically the first time):
    uv run generate_subs.py IMG_20612.mov
    uv run generate_subs.py IMG_20612.mov --model large-v3 --lang en
    uv run generate_subs.py IMG_20612.mov --mode sweep --preset cyan

Then review/edit the .ass in any editor and burn it with burn_subs.py.
"""

import argparse
import os
import re
import sys

import pysubs2
import whisper_timestamped as whisper


# --- Color presets: (base/unsung rgb, highlight/sung rgb) --------------------
PRESETS = {
    "yellow": ((255, 255, 255), (255, 212, 0)),   # white -> gold
    "cyan":   ((255, 255, 255), (0, 220, 255)),   # white -> cyan
    "green":  ((255, 255, 255), (60, 230, 120)),  # white -> green
    "pink":   ((255, 255, 255), (255, 90, 170)),  # white -> pink
    "orange": ((255, 255, 255), (255, 140, 30)),  # white -> orange
}

SENTENCE_ENDINGS = (".", "!", "?", "…", ":", ";")


def ass_color(rgb):
    """RGB tuple -> ASS inline color string &HBBGGRR&."""
    r, g, b = rgb
    return "&H%02X%02X%02X&" % (b, g, r)


def build_lines(words, max_chars, max_gap, max_words):
    """Greedily group timestamped words into subtitle lines."""
    lines, cur, cur_len = [], [], 0
    for w in words:
        text = w["text"].strip()
        if not text:
            continue
        force_break = False
        if cur:
            prev = cur[-1]
            gap = w["start"] - prev["end"]
            too_long = cur_len + 1 + len(text) > max_chars
            too_many = len(cur) >= max_words
            long_pause = gap > max_gap
            sentence_end = prev["text"].strip().endswith(SENTENCE_ENDINGS) and cur_len > max_chars // 2
            force_break = too_long or too_many or long_pause or sentence_end
        if force_break:
            lines.append(cur)
            cur, cur_len = [], 0
        cur.append(w)
        cur_len += (1 if cur_len else 0) + len(text)
    if cur:
        lines.append(cur)
    return lines


def line_to_sweep(words):
    """Progressive-fill karaoke text using \\k tags (centisecond durations)."""
    parts, cursor = [], words[0]["start"]
    for w in words:
        lead = w["start"] - cursor
        if lead > 0.02:
            parts.append(r"{\k%d}" % round(lead * 100))
        dur = max(w["end"] - w["start"], 0.01)
        parts.append(r"{\k%d}%s " % (round(dur * 100), w["text"].strip()))
        cursor = w["end"]
    return "".join(parts).rstrip()


def line_to_word_events(words, base_rgb, hi_rgb):
    """One event per word: only the active word is highlighted, no reflow.

    Each word stays lit from its start until the next word begins, so the full
    line is on screen continuously and exactly one word is colored at a time.
    """
    base, hi = ass_color(base_rgb), ass_color(hi_rgb)
    texts = [w["text"].strip() for w in words]
    n = len(words)
    events = []
    for i in range(n):
        start = words[i]["start"]
        end = words[i + 1]["start"] if i + 1 < n else words[i]["end"]
        if end <= start:
            end = max(words[i]["end"], start + 0.05)
        parts = [r"{\c%s}%s" % (hi if j == i else base, t) for j, t in enumerate(texts)]
        events.append((round(start * 1000), round(end * 1000), " ".join(parts)))
    return events


def make_style(args, primary_rgb, secondary_rgb):
    style = pysubs2.SSAStyle()
    style.fontname = args.font
    style.fontsize = args.fontsize
    style.primarycolor = pysubs2.Color(*primary_rgb)
    style.secondarycolor = pysubs2.Color(*secondary_rgb)
    style.outlinecolor = pysubs2.Color(0, 0, 0)
    style.backcolor = pysubs2.Color(0, 0, 0, 160)
    style.bold = True
    style.outline = args.outline
    style.shadow = args.shadow
    style.borderstyle = 1
    style.alignment = pysubs2.Alignment.BOTTOM_CENTER
    style.marginv = args.margin_v
    style.marginl = 60
    style.marginr = 60
    return style


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video", help="input video/audio file")
    p.add_argument("-o", "--output", help="output .ass path (default: <video>.ass)")
    p.add_argument("--mode", default="word", choices=["word", "sweep"],
                   help="highlight style (default: word = one word at a time)")
    p.add_argument("--model", default="medium",
                   help="whisper model: tiny|base|small|medium|large-v3 (default: medium)")
    p.add_argument("--lang", default=None, help="language code e.g. fr, en (default: auto)")
    p.add_argument("--device", default="cpu", help="cpu | cuda | mps (default: cpu)")
    p.add_argument("--vad", nargs="?", const="auditok", default=False,
                   choices=["auditok", "silero"],
                   help="segment on detected speech: fixes smeared timings "
                        "around long pauses and dropped trailing speech "
                        "(default engine: auditok; silero needs torch.hub)")
    p.add_argument("--preset", default="yellow", choices=list(PRESETS),
                   help="highlight color (default: yellow)")
    p.add_argument("--font", default="Helvetica Neue", help="font name")
    p.add_argument("--fontsize", type=int, default=64, help="font size (px @ PlayResY)")
    p.add_argument("--outline", type=float, default=3.0, help="outline thickness")
    p.add_argument("--shadow", type=float, default=1.5, help="shadow depth")
    p.add_argument("--margin-v", type=int, default=70, help="bottom margin (px)")
    p.add_argument("--max-chars", type=int, default=38, help="max chars per line")
    p.add_argument("--max-words", type=int, default=9, help="max words per line")
    p.add_argument("--max-gap", type=float, default=0.7, help="pause (s) forcing a new line")
    args = p.parse_args()

    if not os.path.exists(args.video):
        sys.exit(f"error: no such file: {args.video}")
    out = args.output or os.path.splitext(args.video)[0] + ".ass"

    print(f"[1/3] loading whisper model '{args.model}' on {args.device} ...", flush=True)
    model = whisper.load_model(args.model, device=args.device)

    print(f"[2/3] transcribing {args.video} (this can take a while) ...", flush=True)
    audio = whisper.load_audio(args.video)
    result = whisper.transcribe(model, audio, language=args.lang, vad=args.vad,
                                beam_size=5, best_of=5, verbose=False)
    print(f"      language: {result.get('language', args.lang)}", flush=True)

    # Whisper hallucinates broadcast boilerplate in trailing silence/noise
    # ("Sous-titrage ST' 501", "subtitles by Amara.org", ...). Drop those
    # segments — they were never spoken.
    HALLUCINATIONS = re.compile(
        r"(?i)sous-?titr|amara\.org|subtitles? by|closed captions?|"
        r"merci d'avoir regard|thanks for watching")
    segments = [s for s in result["segments"]
                if not HALLUCINATIONS.search(s.get("text", ""))]
    if len(segments) < len(result["segments"]):
        print(f"      dropped {len(result['segments']) - len(segments)} "
              "hallucinated segment(s)", flush=True)

    words = [w for seg in segments for w in seg.get("words", [])]
    if not words:
        sys.exit("error: no words with timestamps were produced.")

    base_rgb, hi_rgb = PRESETS[args.preset]
    subs = pysubs2.SSAFile()
    subs.info["PlayResX"] = "1920"
    subs.info["PlayResY"] = "1080"
    subs.info["ScaledBorderAndShadow"] = "yes"

    print(f"[3/3] building '{args.mode}' karaoke .ass ({len(words)} words) ...", flush=True)
    if args.mode == "sweep":
        subs.styles["Karaoke"] = make_style(args, hi_rgb, base_rgb)
        for line in build_lines(words, args.max_chars, args.max_gap, args.max_words):
            subs.append(pysubs2.SSAEvent(
                start=round(line[0]["start"] * 1000), end=round(line[-1]["end"] * 1000),
                style="Karaoke", text=line_to_sweep(line)))
    else:  # word-at-a-time
        subs.styles["Karaoke"] = make_style(args, base_rgb, base_rgb)
        for line in build_lines(words, args.max_chars, args.max_gap, args.max_words):
            for start_ms, end_ms, text in line_to_word_events(line, base_rgb, hi_rgb):
                subs.append(pysubs2.SSAEvent(start=start_ms, end=end_ms,
                                             style="Karaoke", text=text))

    subs.save(out)
    print(f"\n✓ wrote {out}  ({len(subs)} events)")
    print(f"  then:  uv run burn_subs.py {args.video} {out}")


if __name__ == "__main__":
    main()
