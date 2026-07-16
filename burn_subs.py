#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""
Step 2 of 2 — Burn a karaoke .ass subtitle file into a video with ffmpeg.

By default the output is trimmed to the spoken range — it starts at the first
word and ends at the last word (read from the .ass timings). Disable with
--no-trim; adjust the breathing room with --pad.

Usage:
    uv run burn_subs.py IMG_20612.mov IMG_20612.ass
    uv run burn_subs.py IMG_20612.mov IMG_20612.ass -o final.mp4 --crf 20
    uv run burn_subs.py IMG_20612.mov IMG_20612.ass --no-trim
    uv run burn_subs.py IMG_20612.mov IMG_20612.ass --pad 0.3

The .ass styling (fonts, colors, karaoke sweep) is rendered by libass, so what
you see matches the .ass file exactly.
"""

import argparse
import os
import shutil
import subprocess
import sys


def find_ffmpeg(explicit):
    """Locate an ffmpeg binary that has the libass 'ass' filter compiled in.

    The regular Homebrew `ffmpeg` bottle ships WITHOUT libass; `ffmpeg-full`
    (keg-only, not on PATH) has it. Prefer an explicit path, then ffmpeg-full,
    then whatever is on PATH.
    """
    candidates = []
    if explicit:
        candidates.append(explicit)
    candidates.append("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg")  # Apple Silicon
    candidates.append("/usr/local/opt/ffmpeg-full/bin/ffmpeg")     # Intel
    path_ff = shutil.which("ffmpeg")
    if path_ff:
        candidates.append(path_ff)

    for ff in candidates:
        if not ff or not (os.path.isabs(ff) and os.path.exists(ff) or shutil.which(ff)):
            continue
        try:
            out = subprocess.run([ff, "-hide_banner", "-filters"],
                                 capture_output=True, text=True).stdout
        except OSError:
            continue
        if any(line.split()[1:2] == ["ass"] for line in out.splitlines() if line.split()):
            return ff
    return None


def escape_for_filter(path):
    """Escape a path for use inside an ffmpeg filtergraph value."""
    # ffmpeg filter parsing: escape backslash, then ':' and quotes.
    p = path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return p


def _parse_ass_time(t):
    """ASS timestamp 'H:MM:SS.cc' -> seconds (float)."""
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def ass_spoken_range(path):
    """Return (first_start_s, last_end_s) across all Dialogue events, or None."""
    starts, ends = [], []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("Dialogue:"):
                # Dialogue: Layer,Start,End,Style,Name,ML,MR,MV,Effect,Text
                fields = line.split(",", 9)
                if len(fields) >= 3:
                    try:
                        starts.append(_parse_ass_time(fields[1].strip()))
                        ends.append(_parse_ass_time(fields[2].strip()))
                    except ValueError:
                        continue
    if not starts:
        return None
    return min(starts), max(ends)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video", help="input video")
    p.add_argument("subs", help="input .ass subtitle file")
    p.add_argument("-o", "--output", help="output file (default: <video>_subbed.mp4)")
    p.add_argument("--crf", type=int, default=18,
                   help="x264 quality, lower=better (default: 18)")
    p.add_argument("--preset", default="medium",
                   help="x264 speed preset (default: medium)")
    p.add_argument("--fonts-dir", default=None,
                   help="directory of extra fonts for libass")
    p.add_argument("--ffmpeg", default=None,
                   help="path to a libass-enabled ffmpeg (auto-detects ffmpeg-full)")
    p.add_argument("--no-trim", action="store_true",
                   help="keep the full video (default: trim to first..last word)")
    p.add_argument("--pad", type=float, default=0.1,
                   help="seconds of lead/tail around the speech so words aren't "
                        "clipped (default: 0.1; use 0 for an exact cut)")
    p.add_argument("-y", "--yes", action="store_true", help="overwrite output")
    args = p.parse_args()

    ffmpeg = find_ffmpeg(args.ffmpeg)
    if not ffmpeg:
        sys.exit("error: no libass-enabled ffmpeg found.\n"
                 "       install one with:  brew install ffmpeg-full")
    for f in (args.video, args.subs):
        if not os.path.exists(f):
            sys.exit(f"error: no such file: {f}")

    out = args.output or os.path.splitext(args.video)[0] + "_subbed.mp4"

    ass = f"ass=filename={escape_for_filter(args.subs)}"
    if args.fonts_dir:
        ass += f":fontsdir={escape_for_filter(args.fonts_dir)}"

    # Decide whether/how to trim to the spoken range.
    trim_args, audio_args = [], ["-c:a", "copy"]
    if not args.no_trim:
        rng = ass_spoken_range(args.subs)
        if rng is None:
            print("note: no subtitle events found — not trimming.", flush=True)
        else:
            start = max(0.0, rng[0] - args.pad)
            end = rng[1] + args.pad
            dur = end - start
            # Output-side seek: subs keep their absolute times and render
            # correctly, then the window is muxed from 0. Audio is re-encoded
            # because stream-copy can't cut precisely.
            trim_args = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}"]
            audio_args = ["-c:a", "aac", "-b:a", "192k"]
            print(f"trimming to spoken range: {start:.2f}s .. {end:.2f}s "
                  f"({dur:.2f}s, pad {args.pad}s)", flush=True)

    cmd = [
        ffmpeg, "-y" if args.yes else "-n",
        "-i", args.video,
        *trim_args,
        "-vf", ass,
        "-c:v", "libx264", "-crf", str(args.crf), "-preset", args.preset,
        "-pix_fmt", "yuv420p",
        *audio_args,
        "-movflags", "+faststart",
        out,
    ]
    print("running:", " ".join(cmd), "\n", flush=True)
    rc = subprocess.run(cmd).returncode
    if rc == 0:
        print(f"\n✓ wrote {out}")
    else:
        sys.exit(rc)


if __name__ == "__main__":
    main()
