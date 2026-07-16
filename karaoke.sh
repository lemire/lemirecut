#!/usr/bin/env bash
#
# karaoke.sh — one command: video in, subtitled video out.
#
# Runs both steps of the pipeline:
#   1. generate_subs.py  (transcribe -> <video>.ass)
#   2. burn_subs.py      (burn + trim -> <video>_subbed.mp4)
#
# Usage:
#   ./karaoke.sh myclip.mov
#   ./karaoke.sh myclip.mov --model large-v3 --preset cyan   # extra args go to step 1
#
set -euo pipefail

if [[ $# -lt 1 || "$1" == "-h" || "$1" == "--help" ]]; then
    echo "usage: $0 <video> [extra generate_subs.py flags...]"
    echo "example: $0 clip.mov --model large-v3 --preset cyan"
    exit 1
fi

video="$1"; shift            # remaining args (if any) are forwarded to step 1
if [[ ! -f "$video" ]]; then
    echo "error: no such file: $video" >&2
    exit 1
fi

# uv lives in Homebrew's bin; make sure it's reachable in non-interactive shells.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ass="${video%.*}.ass"

echo "==> [1/2] generating subtitles for $video"
uv run "$here/generate_subs.py" "$video" "$@"

echo "==> [2/2] burning subtitles into the video"
uv run "$here/burn_subs.py" "$video" "$ass" -y

echo "==> done: ${video%.*}_subbed.mp4"
