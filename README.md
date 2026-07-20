# 🎤 Karaoke Subtitles

Generate clean, word-synced karaoke subtitles for any video, then burn them in.

Two small scripts, a two-step workflow, full control over styling — powered by
[`whisper-timestamped`](https://github.com/linto-ai/whisper-timestamped) for
word-level timing, [`pysubs2`](https://pysubs2.readthedocs.io) for the `.ass`
file, and `ffmpeg` (libass) for the render.

```
 record.py ──▶ video ──▶ generate_subs.py ──▶ subtitles.ass ──▶ burn_subs.py ──▶ subbed.mp4
 (optional:               (transcribe +          (review /         (render with
  screen+cam)              word timing)           edit here)         libass)
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

---

## Recording a screencast — `record.py`

Don't have a video yet? `record.py` records one: a chosen window with
microphone audio, your webcam as a rounded picture-in-picture, and a
Screen-Studio-style 2× zoom that follows the mouse.

```bash
uv run record.py                     # pick a window from the menu
uv run record.py safari              # only list windows matching "safari"
uv run record.py 1920x1080           # resize the window to 1920×1080 first
uv run record.py --no-zoom           # keep a static framing
```

Stop with **Ctrl-C** — the recording is then composited and saved as
`window_recording.mp4`, ready for `./karaoke.sh window_recording.mp4`.

What happens on a run:

1. A numbered menu lists your windows — including those on other Spaces
   (marked `[other space]`). With a single match it starts right away.
2. The chosen window is brought to the front and resized to a clean recording
   size (default **1280×720** points → 2560×1440 pixels on Retina; override
   with a `WxH` argument). Full-screen windows can't be resized.
3. Screen+mic and webcam are captured by **two separate ffmpeg processes**
   (two avfoundation inputs in one process starve each other to ~3 fps),
   then composited when you stop: webcam at ¼ of the window width,
   bottom-right, rounded corners, lip-sync aligned via the capture clock.
4. While the mouse moves, the view eases into a **2× zoom centered on the
   cursor** and eases back out after ~1.2 s of stillness (`--no-zoom` to
   disable; tunables are constants at the top of the script).

| Argument | Effect |
|----------|--------|
| `<text>` | filter the window list (app name or title substring) |
| `<W>x<H>` | resize the window to W×H points before recording (default `1280x720`) |
| `--no-zoom` | disable the mouse-following zoom |

**Permissions** (System Settings → Privacy & Security, all for your terminal
app): **Screen Recording** (capture), **Microphone** (audio), **Camera**
(webcam PiP — without it you get a screen-only recording), **Accessibility** +
**Automation → System Events** (the automatic window resize).

**Notes**

- Keep the window visible while recording: the capture crops the *screen*, so
  covering or moving the window records whatever replaces it.
- If the webcam fails to start, the recording continues screen-only and the
  reason lands in `.cam_tmp.log`.
- The device names ("Capture screen 0", "Micro MacBook Pro", camera index
  `0`) are for this machine — list yours with
  `ffmpeg -f avfoundation -list_devices true -i ""`.

## Recording with a teleprompter — `recordprompt.py`

Same output as `record.py` (window + mic + webcam PiP + mouse zoom), plus a
**click-through teleprompter overlay** on the window that you advance while
speaking. The overlay is **excluded from the recording**: the screen is
captured with ScreenCaptureKit as a desktop-independent *window* stream (only
that app window’s surface), so the floating prompter never appears in the
video.

```bash
# talk.txt — blank line between chunks (or use --lines for one line per advance)
uv run recordprompt.py --script talk.txt
uv run recordprompt.py --script talk.txt safari
uv run recordprompt.py --script talk.txt 1280x720 --no-zoom
uv run recordprompt.py --script talk.txt --lines
```

**Hotkeys** (work while the demo app has focus):

| Shortcut | Action |
|----------|--------|
| **Ctrl+Shift+→** or **Ctrl+Shift+N** | next chunk |
| **Ctrl+Shift+←** or **Ctrl+Shift+P** | previous chunk |
| **Ctrl+Shift+R** | restart from the first chunk |
| **Ctrl+Shift+C** | stop recording (same as Ctrl-C) |

Stop with **Ctrl+Shift+C** or **Ctrl-C** — compositing writes `window_recording.mp4`.

**Script format:** paragraphs separated by a blank line are one advance each.
If the file is a single paragraph with many lines, it advances line by line
(or force that with `--lines`).

Hotkeys are read from the keyboard HID state (no Accessibility/Input Monitoring
grant required for the chord itself). You still need Screen Recording, Mic,
and Camera as with `record.py`.

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
output is **trimmed to the spoken range** — it starts at the first word and
ends at the next real silence after the last word (so untranscribed trailing
speech isn't chopped mid-sound). Pass `--exact-end` to cut exactly at the last
word, or `--no-trim` to keep the full video.

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
| `--vad` | off | segment on detected speech (`--vad` = auditok, `--vad silero`); known hallucinations ("Sous-titrage…", "subtitles by…") are always dropped |
| `--max-chars` | `38` | line length before wrapping |
| `--max-words` | `9` | words per line cap |
| `--max-gap` | `0.7` | pause (seconds) that forces a new line |

## Options (step 2)

| Flag | Default | Notes |
|------|---------|-------|
| `--no-trim` | off | keep the full video (default trims to the spoken range) |
| `--exact-end` | off | cut at the last word instead of extending to the next silence |
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
| `record.py` | record a window: mic + webcam PiP + mouse-following zoom |
| `recordprompt.py` | same as `record.py`, plus a capture-excluded teleprompter overlay |
| `karaoke.sh` | one-command wrapper — runs both steps |
| `generate_subs.py` | Step 1 — transcribe → styled `.ass` |
| `burn_subs.py` | Step 2 — burn `.ass` → mp4 |
| `README.md` | this file |
