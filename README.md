# 🎤 Karaoke Subtitles

Generate clean, word-synced karaoke subtitles for any video, then burn them in.

Two small scripts, a two-step workflow, full control over styling — powered by
[`whisper-timestamped`](https://github.com/linto-ai/whisper-timestamped) for
word-level timing, [`pysubs2`](https://pysubs2.readthedocs.io) for the `.ass`
file, and `ffmpeg` (libass) for the render.

```
 video ──▶ generate_subs.py ──▶ subtitles.ass ──▶ burn_subs.py ──▶ subbed.mp4
            (transcribe +          (review /         (render with
             word timing)           edit here)         libass)
```

---


## Example

[![Using AI to build your own software](https://img.youtube.com/vi/Falc7QnHy7k/maxresdefault.jpg)](https://www.youtube.com/watch?v=Falc7QnHy7k)

## Requirements

Everything runs through [**uv**](https://docs.astral.sh/uv/) — you don't create
or activate a virtualenv yourself. The first `uv run` reads the dependency
header inside each script and sets up an isolated, cached environment on Python
3.13 automatically.

| Tool | Install |
|------|---------|
| **uv** | `brew install uv` (already installed here) |
| **ffmpeg with libass** | `brew install ffmpeg-full` — the *regular* Homebrew `ffmpeg` bottle has **no** libass and cannot burn subtitles |

> `burn_subs.py` auto-detects `ffmpeg-full` at
> `/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg` (it's keg-only and not on your
> `PATH`). Override with `--ffmpeg /path/to/ffmpeg` if needed.

---

## Quick start

One command — video in, subtitled video out:

```bash
./karaoke.sh IMG_20612.mov
```

That runs both steps below and writes `IMG_20612_subbed.mp4`. Extra flags are
forwarded to step 1, e.g. `./karaoke.sh IMG_20612.mov --model large-v3 --preset cyan`.

Prefer to run the steps yourself (to review/edit the `.ass` in between)? Read on.

## Usage

### Step 1 — Generate the subtitles

```bash
uv run generate_subs.py IMG_20612.mov
```

This transcribes the audio and writes `IMG_20612.ass`. Open it in any text
editor (or [Aegisub](https://aegisub.org)) to fix a word or nudge a timing —
then continue to step 2.

### Step 2 — Burn them into the video

```bash
uv run burn_subs.py IMG_20612.mov IMG_20612.ass
```

Produces `IMG_20612_subbed.mp4` (H.264, web-ready `+faststart`). By default the
output is **trimmed to the spoken range** — it starts at the first word and ends
at the last word. Pass `--no-trim` to keep the full video.

---

## Highlight modes

| Mode | Look | Flag |
|------|------|------|
| **word** *(default)* | One word lit at a time — the highlight jumps word to word. | `--mode word` |
| **sweep** | Classic karaoke — each word fills with color and stays lit. | `--mode sweep` |

<sub>Example — the current word in gold, everything else white:</sub>

```
first  for  you  know  for  war  we  wanted
 ▲ lit
```

---

## Options (step 1)

| Flag | Default | Notes |
|------|---------|-------|
| `--mode` | `word` | `word` (one at a time) or `sweep` (progressive fill) |
| `--model` | `medium` | `tiny · base · small · medium · large-v3` — bigger = more accurate, slower |
| `--lang` | auto | force e.g. `en`, `fr` to skip detection |
| `--preset` | `yellow` | highlight color: `yellow · cyan · green · pink · orange` |
| `--font` | `Helvetica Neue` | any installed font |
| `--fontsize` | `64` | at 1080p |
| `--vad` | off | trims hallucinated text during silence |
| `--max-chars` | `38` | line length before wrapping |
| `--max-words` | `9` | words per line cap |
| `--max-gap` | `0.7` | pause (seconds) that forces a new line |

## Options (step 2)

| Flag | Default | Notes |
|------|---------|-------|
| `--no-trim` | off | keep the full video (default trims to first…last word) |
| `--pad` | `0.1` | seconds of lead/tail around the speech so words aren't clipped (`0` = exact cut) |
| `--crf` | `18` | x264 quality — lower is better/larger |
| `--preset` | `medium` | x264 speed preset |
| `--fonts-dir` | — | extra font directory for libass |
| `--ffmpeg` | auto | path to a libass-enabled ffmpeg |
| `-y` | — | overwrite the output |

---

## Tips

- **Wrong words?** Re-run step 1 with `--model large-v3`, or just edit the
  `.ass` text directly — the timings are preserved either way.
- **Different vibe?** `--preset cyan --font "Avenir Next" --fontsize 72`.
- **Fastest possible test run:** `--model small` (lower accuracy, seconds not
  minutes).

## Files

| File | |
|------|--|
| `karaoke.sh` | one-command wrapper — runs both steps |
| `generate_subs.py` | Step 1 — transcribe → styled `.ass` |
| `burn_subs.py` | Step 2 — burn `.ass` → mp4 |
| `README.md` | this file |
