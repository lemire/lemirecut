# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "pyobjc-framework-Quartz",
#     "ffmpeg-python",
# ]
# ///
import math
import os
import re
import signal
import subprocess
import sys
import time

from Quartz import (
    CGWindowListCopyWindowInfo,
    kCGWindowListOptionAll,
    kCGWindowListExcludeDesktopElements,
    kCGNullWindowID,
    CGMainDisplayID,
    CGDisplayCopyDisplayMode,
    CGDisplayModeGetPixelWidth,
    CGDisplayPixelsWide,
    CGDisplayPixelsHigh,
    CGEventCreate,
    CGEventGetLocation,
)
from AppKit import NSRunningApplication, NSApplicationActivateIgnoringOtherApps
import ffmpeg

OUTPUT = "window_recording.mp4"
DEFAULT_SIZE = (1280, 720)  # window size in points -> 2560x1440 pixels on Retina
ZOOM = 2.0            # zoom level while the mouse moves (--no-zoom to disable)
ZOOM_HOLD = 1.2       # seconds of mouse stillness before zooming back out
ZOOM_TAU = 0.30       # zoom in/out easing time constant
FOLLOW_TAU = 0.15     # camera-follow easing time constant
FPS = 30

SYSTEM_OWNERS = {
    "Window Server", "WindowManager", "Dock", "loginwindow",
    "Open and Save Panel Service", "LocalAuthenticationRemoteServic",
    "AvatarPickerMemojiPicker", "Notification Center", "Control Center",
    "Spotlight", "Screenshot",
}

def list_windows(search_term=None):
    """List application windows on all Spaces, front-most Space first."""
    windows = CGWindowListCopyWindowInfo(
        kCGWindowListOptionAll | kCGWindowListExcludeDesktopElements, kCGNullWindowID
    )
    results = []
    for w in windows:
        owner = w.get("kCGWindowOwnerName", "")
        name = w.get("kCGWindowName", "")
        b = w["kCGWindowBounds"]
        onscreen = bool(w.get("kCGWindowIsOnscreen", False))
        if w.get("kCGWindowLayer", 0) != 0 or owner in SYSTEM_OWNERS:
            continue
        if b["Width"] < 200 or b["Height"] < 150:  # toolbars, minimized-window proxies
            continue
        if not onscreen and not name:  # hidden nameless windows are rarely wanted
            continue
        if search_term and search_term.lower() not in (owner + name).lower():
            continue
        results.append({
            "id": w["kCGWindowNumber"],
            "pid": w["kCGWindowOwnerPID"],
            "owner": owner,
            "name": name,
            "onscreen": onscreen,
            "bounds": (int(b["X"]), int(b["Y"]), int(b["Width"]), int(b["Height"])),
        })
    results.sort(key=lambda r: (not r["onscreen"], -(r["bounds"][2] * r["bounds"][3])))
    return results

def current_bounds(window_id):
    """Re-fetch a window's bounds by id (it may have moved or changed Space)."""
    for w in CGWindowListCopyWindowInfo(kCGWindowListOptionAll, kCGNullWindowID):
        if w["kCGWindowNumber"] == window_id:
            b = w["kCGWindowBounds"]
            return int(b["X"]), int(b["Y"]), int(b["Width"]), int(b["Height"])
    return None

def resize_window(pid, title, width, height):
    """Resize a window to width x height points via the Accessibility API."""
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    pick = (f'first window of proc whose name is "{safe_title}"' if title
            else "window 1 of proc")
    script = f'''
    tell application "System Events"
        set proc to first process whose unix id is {pid}
        try
            set win to {pick}
        on error
            set win to window 1 of proc
        end try
        set size of win to {{{width}, {height}}}
    end tell'''
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"Could not resize window ({r.stderr.strip()})")
        print("(full-screen windows cannot be resized; or grant Accessibility "
              "permission to your terminal in System Settings > Privacy & Security)")
        return False
    return True

def first_start_time(log_path):
    """Extract avfoundation's capture start timestamp from an ffmpeg log."""
    with open(log_path, errors="replace") as f:
        m = re.search(r"start: (\d+\.\d+)", f.read())
    return float(m.group(1)) if m else None

def write_zoom_cmds(path, samples, t0, rw, rh, scale):
    """Turn mouse samples into sendcmd instructions animating a crop that
    zooms toward the cursor while it moves and eases back out when idle.

    samples: [(monotonic_t, global_x_pts, global_y_pts)]; t0: capture start
    on the same clock; rw/rh: recorded region size in px; the region origin
    is subtracted by the caller.
    """
    pts = [(t - t0, gx, gy) for t, gx, gy in samples if t >= t0]
    if len(pts) < 2:
        return False
    # Times at which the mouse actually moved (> 2 points).
    move_t = [t2 for (t1, x1, y1), (t2, x2, y2) in zip(pts, pts[1:])
              if abs(x2 - x1) + abs(y2 - y1) > 2]
    z, cx, cy = 1.0, rw / 2, rh / 2
    last_move = -1e9
    si = mi = 0
    dt = 1.0 / FPS
    ease_z = 1 - math.exp(-dt / ZOOM_TAU)
    ease_c = 1 - math.exp(-dt / FOLLOW_TAU)
    prev = None
    lines = []
    for n in range(int(pts[-1][0] * FPS)):
        t = n * dt
        while si < len(pts) - 1 and pts[si + 1][0] <= t:
            si += 1
        while mi < len(move_t) and move_t[mi] <= t:
            last_move = move_t[mi]
            mi += 1
        mx = min(max(pts[si][1] * scale, 0), rw)
        my = min(max(pts[si][2] * scale, 0), rh)
        target = ZOOM if t - last_move < ZOOM_HOLD else 1.0
        z += (target - z) * ease_z
        cx += (mx - cx) * ease_c
        cy += (my - cy) * ease_c
        cw = int(rw / z) & ~1
        ch = int(rh / z) & ~1
        cur_x = int(min(max(cx - cw / 2, 0), rw - cw)) & ~1
        cur_y = int(min(max(cy - ch / 2, 0), rh - ch)) & ~1
        if (cw, ch, cur_x, cur_y) == prev:
            continue
        prev = (cw, ch, cur_x, cur_y)
        # w/h as numbers, x/y as clamped expressions: sendcmd applies the
        # four sub-commands one at a time and crop validates after each, so
        # bare numbers can transiently exceed the frame and get rejected.
        lines.append(f"{t:.4f} crop w {cw}, crop h {ch}, "
                     f"crop x 'min({cur_x},in_w-out_w)', "
                     f"crop y 'min({cur_y},in_h-out_h)';")
    if not lines:
        return False
    with open(path, "w") as f:
        f.write("\n".join(lines))
    return True

# ---------------------------------------------------------------- pick window
search = None
target_size = DEFAULT_SIZE
zoom_enabled = True
for arg in sys.argv[1:]:
    m = re.fullmatch(r"(\d+)x(\d+)", arg)
    if arg == "--no-zoom":
        zoom_enabled = False
    elif m:
        target_size = (int(m.group(1)), int(m.group(2)))
    else:
        search = arg

candidates = list_windows(search)
if not candidates:
    sys.exit("No matching window found" + (f" for {search!r}" if search else ""))

if len(candidates) == 1:
    win = candidates[0]
else:
    for i, c in enumerate(candidates, 1):
        title = f" — {c['name']}" if c["name"] else ""
        marker = "" if c["onscreen"] else "  [other space]"
        print(f"  [{i:2}] {c['owner']}{title}  ({c['bounds'][2]}×{c['bounds'][3]}){marker}")
    while True:
        try:
            choice = input(f"Window to record [1-{len(candidates)}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            sys.exit("\nCancelled.")
        if choice.isdigit() and 1 <= int(choice) <= len(candidates):
            break
        print("Invalid choice.")
    win = candidates[int(choice) - 1]

title = f" — {win['name']}" if win["name"] else ""
print(f"Recording: {win['owner']}{title}")

# The capture records the visible screen, so bring the chosen window to the front.
app = NSRunningApplication.runningApplicationWithProcessIdentifier_(win["pid"])
if app is not None:
    app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    time.sleep(2)  # let the Space-switch animation finish

# Resize to a clean recording size (points; x2 pixels on Retina).
if win["bounds"][2:4] != target_size:
    if resize_window(win["pid"], win["name"], *target_size):
        time.sleep(0.5)

bounds = current_bounds(win["id"]) or win["bounds"]
x, y, w, h = bounds
print(f"Window is {w}×{h} at ({x},{y})")

# Clamp to the display: a window hanging off-screen would give ffmpeg a
# negative crop origin.
disp_w, disp_h = CGDisplayPixelsWide(CGMainDisplayID()), CGDisplayPixelsHigh(CGMainDisplayID())
x2, y2 = min(x + w, disp_w), min(y + h, disp_h)
x, y = max(x, 0), max(y, 0)
w, h = x2 - x, y2 - y

# Window bounds are in points; screen capture is in pixels (2x on Retina).
display = CGMainDisplayID()
scale = CGDisplayModeGetPixelWidth(CGDisplayCopyDisplayMode(display)) // CGDisplayPixelsWide(display)
x, y, w, h = x * scale, y * scale, (w * scale) & ~1, (h * scale) & ~1

# ------------------------------------------------------------------- capture
# Screen and webcam are captured by two SEPARATE ffmpeg processes: two
# avfoundation inputs in one process starve each other down to ~3 fps.
# List devices with: ffmpeg -f avfoundation -list_devices true -i ""
# .mkv: stays readable even if a capture process dies without finalizing.
screen_file, cam_file = ".screen_tmp.mkv", ".cam_tmp.mkv"
screen_log, cam_log = ".screen_tmp.log", ".cam_tmp.log"
mic_file, mic_log = ".mic_tmp.mkv", ".mic_tmp.log"

screen_in = ffmpeg.input(
    "Capture screen 0",  # video only — the mic is its own process, see below
    f="avfoundation", framerate=FPS, capture_cursor=1,
    thread_queue_size=1024,
)
# r/fps_mode: avfoundation only emits frames when the screen changes;
# force constant frame rate so idle stretches don't leave timeline gaps.
screen_out = ffmpeg.output(
    screen_in.video.filter("crop", w=w, h=h, x=x, y=y),
    screen_file, vcodec="h264_videotoolbox", r=FPS, fps_mode="cfr",
).global_args("-nostdin")

# The microphone gets its OWN process too. Sharing one avfoundation input
# with the screen means one packet queue: whenever the h264 encoder falls
# behind the raw 3456x2234 feed (it runs at ~0.98x, so it always does
# eventually) the audio packets behind it are dropped, and the gaps are
# what you hear as breakup. Alone, mic capture holds 1.00x indefinitely.
# flac: lossless, so the single aac encode at composite time is the only
# lossy step (the old path encoded aac twice, the first at ~69 kb/s).
mic_in = ffmpeg.input(
    ":Micro MacBook Pro",  # leading colon = no video device
    f="avfoundation", thread_queue_size=1024,
)
mic_out = ffmpeg.output(mic_in, mic_file, acodec="flac").global_args("-nostdin")

cam_in = ffmpeg.input(
    "0",  # [0] Caméra MacBook Pro (matching by accented name fails)
    f="avfoundation", framerate=30, thread_queue_size=1024,
    video_size=(1920, 1080),  # tuple! ffmpeg-python unpacks a string into "1x9".
                              # Without size+format the camera opens at 3fps portrait.
    pixel_format="nv12",
)
cam_out = ffmpeg.output(
    cam_in, cam_file, vcodec="h264_videotoolbox",
).global_args("-nostdin")

def start_capture(stream, log_path, tries=1):
    """Launch an ffmpeg capture and wait until it actually starts encoding."""
    for attempt in range(tries):
        with open(log_path, "w") as log:
            p = subprocess.Popen(
                ffmpeg.compile(stream, overwrite_output=True),
                stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        for _ in range(80):  # up to 8s
            if p.poll() is not None:
                break
            with open(log_path, errors="replace") as f:
                if "Output #" in f.read():
                    return p
            time.sleep(0.1)
        if p.poll() is None:  # started but never reported: keep it anyway
            return p
        time.sleep(1)  # camera may need a moment to free up before a retry
    return None

# Start the webcam first: opening both capture sessions at the same instant
# makes the camera fail with an I/O error.
cam_proc = start_capture(cam_out, cam_log, tries=3)
if cam_proc is None:
    print("Webcam capture failed to start — recording screen only.")
mic_proc = start_capture(mic_out, mic_log, tries=3)
if mic_proc is None:
    print("Microphone capture failed to start — recording without sound.")
screen_proc = start_capture(screen_out, screen_log)
if screen_proc is None:
    for p in (cam_proc, mic_proc):
        if p:
            p.terminate()
    sys.exit(f"Screen capture failed to start — see {screen_log}")
procs = [p for p in (screen_proc, cam_proc, mic_proc) if p]

print("Recording... press Ctrl-C to stop.")
interrupted = False
mouse_samples = []  # sampled on the capture clock for the zoom effect
try:
    while procs[0].poll() is None:
        loc = CGEventGetLocation(CGEventCreate(None))
        mouse_samples.append((time.monotonic(), loc.x, loc.y))
        time.sleep(1 / 60)
except KeyboardInterrupt:
    # Ctrl-C already reached the ffmpeg children (same process group);
    # a second SIGINT would make ffmpeg hard-exit and truncate the file.
    interrupted = True
for p in procs:
    if not interrupted and p.poll() is None:
        p.send_signal(signal.SIGINT)
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.terminate()
        p.wait()
print("\nRecording stopped, compositing...")

# ----------------------------------------------------------------- composite
t_screen = first_start_time(screen_log)

zoom_file = ".zoom_cmds.txt"
have_zoom = False
if zoom_enabled and t_screen:
    # Zoom works in region-relative coordinates: shift the global mouse
    # positions by the recorded region's origin (in points).
    rel = [(t, gx - x / scale, gy - y / scale) for t, gx, gy in mouse_samples]
    have_zoom = write_zoom_cmds(zoom_file, rel, t_screen, w, h, scale)

def with_zoom(video):
    """2x zoom following the mouse, driven by the sendcmd script."""
    if not have_zoom:
        return video
    return (video.filter("sendcmd", f=zoom_file)
                 .filter("crop")
                 .filter("scale", w, h))

# Mic audio is aligned exactly like the webcam: avfoundation logs every
# input's start on the same host clock, so the difference is the startup
# delay. aresample first_pts=0 pads the leading gap with silence so the
# sample count matches the timeline — Whisper reads samples, not
# timestamps, and a gap desyncs every subtitle after it.
t_mic = first_start_time(mic_log)
have_mic = os.path.exists(mic_file) and os.path.getsize(mic_file) > 0

def audio_stream():
    """Mic track shifted onto the screen capture's timeline, or None."""
    if not have_mic:
        return None
    off = (t_mic - t_screen) if t_screen and t_mic else 0.0
    return (ffmpeg.input(mic_file, itsoffset=off).audio
                  .filter("aresample", **{"async": 1, "first_pts": 0}))

AUDIO_ARGS = {"acodec": "aac", "audio_bitrate": "192k"}

def screen_only(reason):
    scr = ffmpeg.input(screen_file)
    aud = audio_stream()
    # Without zoom the screen track passes through untouched — no re-encode.
    vopts = {"vcodec": "h264_videotoolbox"} if have_zoom else {"vcodec": "copy"}
    streams = [with_zoom(scr.video) if have_zoom else scr.video]
    if aud is not None:
        streams.append(aud)
    out = ffmpeg.output(*streams, OUTPUT, **vopts,
                        **(AUDIO_ARGS if aud is not None else {}))
    ffmpeg.run(out, overwrite_output=True, quiet=True)
    print(f"Saved {OUTPUT} ({reason})")

have_cam = os.path.exists(cam_file) and os.path.getsize(cam_file) > 0
if not have_cam:
    screen_only(f"no webcam — see {cam_log}")
else:
    # Align the two captures: avfoundation logs each input's start on the
    # same host clock, so the difference is the webcam's startup delay.
    t_cam = first_start_time(cam_log)
    offset = (t_cam - t_screen) if t_screen and t_cam else 0.0

    # Picture-in-picture: webcam at 1/4 of the window width, bottom-right,
    # with rounded corners cut by an alpha mask.
    cam_w = (w // 4) & ~1
    margin = 10 * scale
    radius = cam_w // 12
    rounded = (
        f"st(0,hypot(max(abs(X-(W-1)/2)-(W/2-{radius}),0),"
        f"max(abs(Y-(H-1)/2)-(H/2-{radius}),0)));"
        f"255*clip({radius}+0.5-ld(0),0,1)"
    )
    scr = ffmpeg.input(screen_file)
    cam = ffmpeg.input(cam_file, itsoffset=offset)
    small = (cam.video.filter("scale", cam_w, -2)
                      .filter("format", "yuva420p")
                      .filter("geq", lum="lum(X,Y)", cb="cb(X,Y)",
                              cr="cr(X,Y)", a=rounded))
    video = ffmpeg.filter(
        [with_zoom(scr.video), small], "overlay",
        x=f"W-w-{margin}", y=f"H-h-{margin}", eof_action="repeat",
    )
    aud = audio_stream()
    streams = [video] + ([aud] if aud is not None else [])
    try:
        ffmpeg.run(
            ffmpeg.output(*streams, OUTPUT, vcodec="h264_videotoolbox",
                          **(AUDIO_ARGS if aud is not None else {})),
            overwrite_output=True, quiet=True,
        )
        extras = [f"webcam offset {offset:+.2f}s"] + (["zoom"] if have_zoom else [])
        print(f"Saved {OUTPUT} ({', '.join(extras)})")
    except ffmpeg.Error as e:
        print(e.stderr.decode(errors="replace").strip().splitlines()[-1])
        have_cam = False
        screen_only("webcam composite failed — screen only")

# Keep the camera log around when the webcam failed, for diagnosis.
cleanup = ([screen_file, cam_file, mic_file, screen_log, zoom_file]
           + ([cam_log] if have_cam else []) + ([mic_log] if have_mic else []))
for f in cleanup:
    if os.path.exists(f):
        os.remove(f)
