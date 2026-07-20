# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "pyobjc-framework-Quartz",
#     "pyobjc-framework-AVFoundation",
#     "pyobjc-framework-ScreenCaptureKit",
#     "pyobjc-framework-Cocoa",
#     "pyobjc-framework-CoreMedia",
#     "ffmpeg-python",
# ]
# ///
"""Record a window with webcam PiP + zoom, while showing a teleprompter
overlay that is excluded from the capture.

The screen is captured with ScreenCaptureKit as a *desktop-independent
window* stream: only the chosen app window is recorded, so the floating
prompter (a separate NSWindow) never appears in the video. The prompter
also sets NSWindowSharingNone as a belt-and-suspenders measure.

Hotkeys (polled from the HID keyboard state — work while any app has focus):
  Ctrl+Shift+→ / Ctrl+Shift+N   next chunk
  Ctrl+Shift+← / Ctrl+Shift+P   previous chunk
  Ctrl+Shift+R                  restart from the first chunk
  Ctrl+Shift+C                  stop recording (same as Ctrl-C)

Usage:
  uv run recordprompt.py --script talk.txt
  uv run recordprompt.py --script talk.txt safari
  uv run recordprompt.py --script talk.txt 1280x720 --no-zoom
  uv run recordprompt.py --script talk.txt --lines   # one line per advance
"""
import math
import os
import re
import signal
import subprocess
import sys
import time

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSApplicationActivateIgnoringOtherApps,
    NSBackingStoreBuffered,
    NSColor,
    NSFont,
    NSFloatingWindowLevel,
    NSLineBreakByWordWrapping,
    NSMakeRect,
    NSRunningApplication,
    NSScreen,
    NSTextField,
    NSTextAlignmentCenter,
    NSView,
    NSWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorStationary,
    NSWindowSharingNone,
    NSWindowStyleMaskBorderless,
)
from CoreMedia import CMTimeMake
from Foundation import NSDate, NSObject, NSRunLoop, NSURL
from Quartz import (
    CGDisplayCopyDisplayMode,
    CGDisplayModeGetPixelWidth,
    CGDisplayPixelsWide,
    CGEventCreate,
    CGEventGetLocation,
    CGEventSourceKeyState,
    CGMainDisplayID,
    CGWindowListCopyWindowInfo,
    kCGEventSourceStateHIDSystemState,
    kCGNullWindowID,
    kCGWindowListExcludeDesktopElements,
    kCGWindowListOptionAll,
)
import AVFoundation
import ScreenCaptureKit as SCK
import ffmpeg

OUTPUT = "window_recording.mp4"
DEFAULT_SIZE = (1280, 720)
ZOOM = 2.0
ZOOM_HOLD = 1.2
ZOOM_TAU = 0.30
FOLLOW_TAU = 0.15
FPS = 30
PROMPTER_FRACTION = 0.30  # top 30% of the window
PROMPTER_ALPHA = 0.42     # dark scrim so text stays readable

SYSTEM_OWNERS = {
    "Window Server", "WindowManager", "Dock", "loginwindow",
    "Open and Save Panel Service", "LocalAuthenticationRemoteServic",
    "AvatarPickerMemojiPicker", "Notification Center", "Control Center",
    "Spotlight", "Screenshot",
}

# ANSI keycodes (physical keys, layout-independent for arrows/modifiers).
_KEY_LEFT, _KEY_RIGHT = 123, 124
_KEY_N, _KEY_P, _KEY_R, _KEY_C = 45, 35, 15, 8
_KEY_LCTRL, _KEY_RCTRL = 59, 62
_KEY_LSHIFT, _KEY_RSHIFT = 56, 60


def _hid_down(code):
    """True if the physical key is currently held (no Accessibility needed)."""
    return bool(CGEventSourceKeyState(kCGEventSourceStateHIDSystemState, code))


class HotkeyPoller:
    """Edge-trigger Ctrl+Shift+{←/→/N/P/R/C} by sampling the HID keyboard state.

    NSEvent global monitors and CGEvent taps need Accessibility / Input
    Monitoring and often never fire for a terminal-launched Python process.
    Polling HID state works while another app has focus.
    """

    def __init__(self, on_next, on_prev, on_restart, on_stop):
        self.on_next = on_next
        self.on_prev = on_prev
        self.on_restart = on_restart
        self.on_stop = on_stop
        self._held = None  # last fired action while its key is still down

    def poll(self):
        ctrl = _hid_down(_KEY_LCTRL) or _hid_down(_KEY_RCTRL)
        shift = _hid_down(_KEY_LSHIFT) or _hid_down(_KEY_RSHIFT)
        action = None
        if ctrl and shift:
            # Stop first so Ctrl+Shift+C is never ambiguous with other chords.
            if _hid_down(_KEY_C):
                action = "stop"
            elif _hid_down(_KEY_RIGHT) or _hid_down(_KEY_N):
                action = "next"
            elif _hid_down(_KEY_LEFT) or _hid_down(_KEY_P):
                action = "prev"
            elif _hid_down(_KEY_R):
                action = "restart"
        if action and action != self._held:
            if action == "next":
                self.on_next()
            elif action == "prev":
                self.on_prev()
            elif action == "restart":
                self.on_restart()
            else:
                self.on_stop()
        self._held = action


# ------------------------------------------------------------------- helpers
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
        if b["Width"] < 200 or b["Height"] < 150:
            continue
        if not onscreen and not name:
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
    zooms toward the cursor while it moves and eases back out when idle."""
    pts = [(t - t0, gx, gy) for t, gx, gy in samples if t >= t0]
    if len(pts) < 2:
        return False
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
        lines.append(f"{t:.4f} crop w {cw}, crop h {ch}, "
                     f"crop x 'min({cur_x},in_w-out_w)', "
                     f"crop y 'min({cur_y},in_h-out_h)';")
    if not lines:
        return False
    with open(path, "w") as f:
        f.write("\n".join(lines))
    return True


def load_chunks(path, by_line=False):
    """Split a script file into teleprompter chunks."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        sys.exit(f"Script file is empty: {path}")
    if by_line:
        chunks = [ln.strip() for ln in text.split("\n") if ln.strip()]
    else:
        chunks = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        # Single block of many lines → advance line by line instead.
        if len(chunks) == 1 and chunks[0].count("\n") >= 2:
            chunks = [ln.strip() for ln in chunks[0].split("\n") if ln.strip()]
    if not chunks:
        sys.exit(f"No readable chunks in {path}")
    return chunks


def primary_screen_height():
    """Height of the screen whose origin is (0,0), in points (Cocoa)."""
    for s in NSScreen.screens():
        f = s.frame()
        if f.origin.x == 0 and f.origin.y == 0:
            return f.size.height
    return NSScreen.mainScreen().frame().size.height


def quartz_rect_to_cocoa(x, y, w, h):
    """Convert CGWindowBounds (origin top-left of main display) to NSWindow frame."""
    cocoa_y = primary_screen_height() - y - h
    return NSMakeRect(float(x), float(cocoa_y), float(w), float(h))


def get_shareable_content():
    """Block until SCShareableContent is available (or fail)."""
    box = {}

    def handler(content, error):
        box["content"] = content
        box["error"] = error

    SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
        False, False, handler
    )
    for _ in range(100):
        NSRunLoop.currentRunLoop().runUntilDate_(
            NSDate.dateWithTimeIntervalSinceNow_(0.05))
        if box:
            break
    if box.get("error") is not None or box.get("content") is None:
        err = box.get("error")
        sys.exit(
            "ScreenCaptureKit could not list windows"
            + (f": {err}" if err else "")
            + "\nGrant Screen Recording permission to your terminal in "
              "System Settings → Privacy & Security."
        )
    return box["content"]


def find_sc_window(content, window_id):
    """Match a CGWindowNumber to an SCWindow."""
    for w in content.windows():
        if w.windowID() == window_id:
            return w
    return None


# ----------------------------------------------------------------- prompter
class Prompter:
    """Click-through floating overlay that sits on the recorded window.

    Capture excludes it because ScreenCaptureKit records only the target
    window's own surface; sharingType=None is an extra safeguard.
    """

    def __init__(self, chunks):
        self.chunks = chunks
        self.index = 0
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 100, 100),
            NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered,
            False,
        )
        self.window.setLevel_(NSFloatingWindowLevel)
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(0.0, PROMPTER_ALPHA)
        )
        self.window.setIgnoresMouseEvents_(True)
        self.window.setSharingType_(NSWindowSharingNone)
        self.window.setHasShadow_(False)
        self.window.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
        )
        self.window.setReleasedWhenClosed_(False)

        root = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 100))
        root.setAutoresizingMask_(18)  # width+height flexible

        self.counter = NSTextField.alloc().initWithFrame_(NSMakeRect(12, 8, 200, 18))
        self.counter.setBezeled_(False)
        self.counter.setDrawsBackground_(False)
        self.counter.setEditable_(False)
        self.counter.setSelectable_(False)
        self.counter.setTextColor_(
            NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.7)
        )
        self.counter.setFont_(NSFont.systemFontOfSize_(12))
        self.counter.setAutoresizingMask_(1)  # min-x flexible? keep left

        self.body = NSTextField.alloc().initWithFrame_(NSMakeRect(24, 28, 52, 60))
        self.body.setBezeled_(False)
        self.body.setDrawsBackground_(False)
        self.body.setEditable_(False)
        self.body.setSelectable_(False)
        self.body.setTextColor_(NSColor.whiteColor())
        self.body.setFont_(NSFont.boldSystemFontOfSize_(28))
        self.body.setAlignment_(NSTextAlignmentCenter)
        self.body.setLineBreakMode_(NSLineBreakByWordWrapping)
        if hasattr(self.body, "setUsesSingleLineMode_"):
            self.body.setUsesSingleLineMode_(False)
        if hasattr(self.body, "setMaximumNumberOfLines_"):
            self.body.setMaximumNumberOfLines_(0)
        self.body.setAutoresizingMask_(18)

        root.addSubview_(self.counter)
        root.addSubview_(self.body)
        self.window.setContentView_(root)
        self._layout_subviews(100, 100)
        self._render()

    def _layout_subviews(self, w, h):
        pad = 20
        self.counter.setFrame_(NSMakeRect(pad, h - 26, w - 2 * pad, 18))
        self.body.setFrame_(NSMakeRect(pad, 12, w - 2 * pad, h - 44))
        if hasattr(self.body, "setPreferredMaxLayoutWidth_"):
            self.body.setPreferredMaxLayoutWidth_(w - 2 * pad)
        # Scale font to window width.
        size = max(18, min(36, w / 36))
        self.body.setFont_(NSFont.boldSystemFontOfSize_(size))

    def place_on_window(self, quartz_bounds):
        """Position the overlay over the top strip of the target window."""
        x, y, w, h = quartz_bounds
        oh = max(120, int(h * PROMPTER_FRACTION))
        # Quartz top-left: overlay sits on the top edge of the window.
        ox, oy, ow = x, y, w
        frame = quartz_rect_to_cocoa(ox, oy, ow, oh)
        self.window.setFrame_display_(frame, True)
        self._layout_subviews(ow, oh)
        self.window.orderFrontRegardless()

    def _render(self):
        n = len(self.chunks)
        i = self.index
        self.counter.setStringValue_(
            f"{i + 1} / {n}   ·   ⌃⇧→ next  ⌃⇧← prev  ⌃⇧R restart  ⌃⇧C stop"
        )
        self.body.setStringValue_(self.chunks[i])

    def next(self):
        if self.index < len(self.chunks) - 1:
            self.index += 1
            self._render()
            return True
        return False

    def prev(self):
        if self.index > 0:
            self.index -= 1
            self._render()
            return True
        return False

    def restart(self):
        self.index = 0
        self._render()

    def close(self):
        self.window.orderOut_(None)
        self.window.close()


# ----------------------------------------------------------- SCK recording
class RecordingDelegate(NSObject):
    """SCRecordingOutput callbacks; records wall-clock start on monotonic."""

    def init(self):
        self = objc.super(RecordingDelegate, self).init()
        if self is None:
            return None
        self.t0 = None
        self.finished = False
        self.error = None
        return self

    def recordingOutputDidStartRecording_(self, output):
        self.t0 = time.monotonic()

    def recordingOutputDidFinishRecording_(self, output):
        self.finished = True

    def recordingOutput_didFailWithError_(self, output, error):
        self.error = error
        self.finished = True


def start_sck_window_capture(sc_window, out_path, width_px, height_px):
    """Start a ScreenCaptureKit window stream writing H.264 to out_path.

    Returns (stream, recording_output, delegate).
    """
    filt = SCK.SCContentFilter.alloc().initWithDesktopIndependentWindow_(sc_window)
    cfg = SCK.SCStreamConfiguration.alloc().init()
    cfg.setWidth_(width_px)
    cfg.setHeight_(height_px)
    cfg.setShowsCursor_(True)
    cfg.setMinimumFrameInterval_(CMTimeMake(1, FPS))
    cfg.setQueueDepth_(8)

    delg = RecordingDelegate.alloc().init()
    rocfg = SCK.SCRecordingOutputConfiguration.alloc().init()
    rocfg.setOutputURL_(NSURL.fileURLWithPath_(os.path.abspath(out_path)))
    rocfg.setOutputFileType_("public.mpeg-4")
    rocfg.setVideoCodecType_("avc1")
    rec = SCK.SCRecordingOutput.alloc().initWithConfiguration_delegate_(rocfg, delg)

    stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
        filt, cfg, None
    )
    ok, err = stream.addRecordingOutput_error_(rec, None)
    if not ok:
        sys.exit(f"ScreenCaptureKit addRecordingOutput failed: {err}")

    err_box = []

    def on_start(error):
        err_box.append(error)

    stream.startCaptureWithCompletionHandler_(on_start)
    for _ in range(100):
        NSRunLoop.currentRunLoop().runUntilDate_(
            NSDate.dateWithTimeIntervalSinceNow_(0.05))
        if err_box:
            break
    if err_box and err_box[0] is not None:
        sys.exit(
            f"ScreenCaptureKit failed to start: {err_box[0]}\n"
            "Grant Screen Recording permission to your terminal."
        )
    # Wait until the file sink reports it has started (or a short timeout).
    for _ in range(60):
        if delg.t0 is not None:
            break
        NSRunLoop.currentRunLoop().runUntilDate_(
            NSDate.dateWithTimeIntervalSinceNow_(0.05))
    if delg.t0 is None:
        # Capture is running; use now as a best-effort start stamp.
        delg.t0 = time.monotonic()
    return stream, rec, delg


def stop_sck_capture(stream, delg, timeout=10.0):
    done = []

    def on_stop(error):
        done.append(error)

    stream.stopCaptureWithCompletionHandler_(on_stop)
    t0 = time.time()
    while time.time() - t0 < timeout:
        NSRunLoop.currentRunLoop().runUntilDate_(
            NSDate.dateWithTimeIntervalSinceNow_(0.05))
        if delg.finished and done:
            break
    if delg.error is not None:
        print(f"ScreenCaptureKit recording error: {delg.error}")


# ------------------------------------------------------------------- CLI
def usage():
    print(__doc__.strip(), file=sys.stderr)
    sys.exit(2)


def parse_args(argv):
    script_path = None
    search = None
    target_size = DEFAULT_SIZE
    zoom_enabled = True
    by_line = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            usage()
        elif arg == "--script":
            i += 1
            if i >= len(argv):
                sys.exit("--script requires a path")
            script_path = argv[i]
        elif arg.startswith("--script="):
            script_path = arg.split("=", 1)[1]
        elif arg == "--no-zoom":
            zoom_enabled = False
        elif arg == "--lines":
            by_line = True
        else:
            m = re.fullmatch(r"(\d+)x(\d+)", arg)
            if m:
                target_size = (int(m.group(1)), int(m.group(2)))
            else:
                search = arg
        i += 1
    if not script_path:
        sys.exit("Missing --script PATH\n\n" + __doc__.strip())
    if not os.path.isfile(script_path):
        sys.exit(f"Script not found: {script_path}")
    return script_path, search, target_size, zoom_enabled, by_line


# =================================================================== main
ns_app = NSApplication.sharedApplication()
ns_app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

script_path, search, target_size, zoom_enabled, by_line = parse_args(sys.argv[1:])
chunks = load_chunks(script_path, by_line=by_line)
print(f"Script: {script_path}  ({len(chunks)} chunk{'s' if len(chunks) != 1 else ''})",
      flush=True)

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
print(f"Recording: {win['owner']}{title}", flush=True)

running = NSRunningApplication.runningApplicationWithProcessIdentifier_(win["pid"])
if running is not None:
    running.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    time.sleep(2)

if win["bounds"][2:4] != target_size:
    if resize_window(win["pid"], win["name"], *target_size):
        time.sleep(0.5)

bounds = current_bounds(win["id"]) or win["bounds"]
qx, qy, qw, qh = bounds
print(f"Window is {qw}×{qh} at ({qx},{qy})")

display = CGMainDisplayID()
scale = CGDisplayModeGetPixelWidth(CGDisplayCopyDisplayMode(display)) // CGDisplayPixelsWide(display)
# Recorded pixel size (even); refined below from SCK's contentRect when available.
rw, rh = (qw * scale) & ~1, (qh * scale) & ~1

# ---------------------------------------------------------------- prompter
prompter = Prompter(chunks)
prompter.place_on_window(bounds)


def _on_next():
    if prompter.next():
        print(f"  → chunk {prompter.index + 1}/{len(chunks)}", flush=True)
    else:
        print("  → already at last chunk", flush=True)


def _on_prev():
    if prompter.prev():
        print(f"  ← chunk {prompter.index + 1}/{len(chunks)}", flush=True)
    else:
        print("  ← already at first chunk", flush=True)


def _on_restart():
    prompter.restart()
    print("  ↺ restart", flush=True)


# Shared with SIGINT/SIGTERM and the Ctrl+Shift+C hotkey.
_stop = {"flag": False}


def _on_stop():
    print("  ■ stop", flush=True)
    _stop["flag"] = True


hotkeys = HotkeyPoller(_on_next, _on_prev, _on_restart, _on_stop)

# --------------------------------------------------------- shareable content
content = get_shareable_content()
sc_win = find_sc_window(content, win["id"])
if sc_win is None:
    # Window list can lag after resize/Space switch; refresh once.
    time.sleep(0.5)
    content = get_shareable_content()
    sc_win = find_sc_window(content, win["id"])
if sc_win is None:
    prompter.close()
    sys.exit(
        f"Could not match window id {win['id']} in ScreenCaptureKit's list.\n"
        "Is the window still open and on-screen?"
    )

# Prefer SCK's own scale for the stream dimensions.
filt_probe = SCK.SCContentFilter.alloc().initWithDesktopIndependentWindow_(sc_win)
sck_scale = filt_probe.pointPixelScale() or float(scale)
cr = filt_probe.contentRect()
rw = int(cr.size.width * sck_scale) & ~1
rh = int(cr.size.height * sck_scale) & ~1
if rw < 2 or rh < 2:
    rw, rh = (qw * scale) & ~1, (qh * scale) & ~1
print(f"Capture {rw}×{rh} px (scale {sck_scale:g}) — prompter excluded from stream",
      flush=True)

# ------------------------------------------------------------------- capture
screen_file = ".screen_tmp.mp4"
cam_file = ".cam_tmp.mkv"
cam_log = ".cam_tmp.log"
mic_file = ".mic_tmp.wav"
for p in (screen_file, cam_file, mic_file):
    if os.path.exists(p):
        os.remove(p)


def start_mic(path):
    """Record the default input to 16-bit PCM. Returns (recorder, t0)."""
    settings = {
        AVFoundation.AVFormatIDKey: 1819304813,  # kAudioFormatLinearPCM
        AVFoundation.AVSampleRateKey: 44100.0,
        AVFoundation.AVNumberOfChannelsKey: 1,
        AVFoundation.AVLinearPCMBitDepthKey: 16,
        AVFoundation.AVLinearPCMIsFloatKey: False,
        AVFoundation.AVLinearPCMIsBigEndianKey: False,
    }
    rec, err = AVFoundation.AVAudioRecorder.alloc().initWithURL_settings_error_(
        NSURL.fileURLWithPath_(os.path.abspath(path)), settings, None)
    if rec is None:
        print(f"Microphone capture failed to start ({err}) — recording without sound.")
        return None, None
    rec.prepareToRecord()
    t0 = time.monotonic()
    rec.record()
    return rec, t0


cam_in = ffmpeg.input(
    "0",
    f="avfoundation", framerate=30, thread_queue_size=1024,
    video_size=(1920, 1080),
    pixel_format="nv12",
)
cam_out = ffmpeg.output(
    cam_in, cam_file, vcodec="h264_videotoolbox",
).global_args("-nostdin")


def start_ffmpeg_capture(stream, log_path, tries=1):
    for attempt in range(tries):
        with open(log_path, "w") as log:
            p = subprocess.Popen(
                ffmpeg.compile(stream, overwrite_output=True),
                stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        for _ in range(80):
            if p.poll() is not None:
                break
            with open(log_path, errors="replace") as f:
                if "Output #" in f.read():
                    return p
            time.sleep(0.1)
        if p.poll() is None:
            return p
        time.sleep(1)
    return None


cam_proc = start_ffmpeg_capture(cam_out, cam_log, tries=3)
if cam_proc is None:
    print("Webcam capture failed to start — recording screen only.")

mic_rec, t_mic = start_mic(mic_file)
stream, rec_out, rec_delg = start_sck_window_capture(sc_win, screen_file, rw, rh)
t_screen = rec_delg.t0
procs = [p for p in (cam_proc,) if p]

print("Recording... Ctrl+Shift+C or Ctrl-C to stop.", flush=True)
print("Prompter: Ctrl+Shift+→ next · ← prev · R restart · C stop", flush=True)
mouse_samples = []
last_bounds = bounds


def _request_stop(signum, frame):
    _stop["flag"] = True


signal.signal(signal.SIGINT, _request_stop)
signal.signal(signal.SIGTERM, _request_stop)

while not _stop["flag"]:
    if cam_proc is not None and cam_proc.poll() is not None:
        # Camera died mid-run; keep going screen-only.
        cam_proc = None
    hotkeys.poll()
    loc = CGEventGetLocation(CGEventCreate(None))
    mouse_samples.append((time.monotonic(), loc.x, loc.y))
    # Keep the prompter glued to the window if it moves/resizes.
    b = current_bounds(win["id"])
    if b is not None and b != last_bounds:
        prompter.place_on_window(b)
        last_bounds = b
    NSRunLoop.currentRunLoop().runUntilDate_(
        NSDate.dateWithTimeIntervalSinceNow_(1 / 60))

if mic_rec:
    mic_rec.stop()
stop_sck_capture(stream, rec_delg)
for p in procs:
    if p.poll() is None:
        p.send_signal(signal.SIGINT)
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.terminate()
            p.wait()

prompter.close()

print("\nRecording stopped, compositing...")

# ----------------------------------------------------------------- composite
# Mouse positions are global points; convert to window-local points using the
# window origin at sample time is ideal, but the origin at start is close enough
# (same approach as record.py).
zoom_file = ".zoom_cmds.txt"
have_zoom = False
# Use the Quartz origin in points; scale applied inside write_zoom_cmds.
origin_x, origin_y = qx, qy
if zoom_enabled and t_screen:
    rel = [(t, gx - origin_x, gy - origin_y) for t, gx, gy in mouse_samples]
    have_zoom = write_zoom_cmds(zoom_file, rel, t_screen, rw, rh, sck_scale)


def with_zoom(video):
    if not have_zoom:
        return video
    return (video.filter("sendcmd", f=zoom_file)
                 .filter("crop")
                 .filter("scale", rw, rh))


have_mic = os.path.exists(mic_file) and os.path.getsize(mic_file) > 0


def audio_stream():
    if not have_mic:
        return None
    off = (t_mic - t_screen) if t_screen and t_mic else 0.0
    return (ffmpeg.input(mic_file, itsoffset=off).audio
                  .filter("aresample", **{"async": 1, "first_pts": 0}))


AUDIO_ARGS = {"acodec": "aac", "audio_bitrate": "192k"}


def screen_only(reason):
    scr = ffmpeg.input(screen_file)
    aud = audio_stream()
    vopts = {"vcodec": "h264_videotoolbox"} if have_zoom else {"vcodec": "copy"}
    streams = [with_zoom(scr.video) if have_zoom else scr.video]
    if aud is not None:
        streams.append(aud)
    out = ffmpeg.output(*streams, OUTPUT, **vopts,
                        **(AUDIO_ARGS if aud is not None else {}))
    ffmpeg.run(out, overwrite_output=True, quiet=True)
    print(f"Saved {OUTPUT} ({reason})")


if not os.path.exists(screen_file) or os.path.getsize(screen_file) == 0:
    sys.exit("Screen capture produced no file — check Screen Recording permission.")

have_cam = os.path.exists(cam_file) and os.path.getsize(cam_file) > 0
if not have_cam:
    screen_only(f"no webcam — see {cam_log}")
else:
    t_cam = first_start_time(cam_log)
    offset = (t_cam - t_screen) if t_screen and t_cam else 0.0

    cam_w = (rw // 4) & ~1
    margin = 10 * int(round(sck_scale))
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
        extras = [f"webcam offset {offset:+.2f}s", "prompter excluded"]
        if have_zoom:
            extras.append("zoom")
        print(f"Saved {OUTPUT} ({', '.join(extras)})")
    except ffmpeg.Error as e:
        print(e.stderr.decode(errors="replace").strip().splitlines()[-1])
        have_cam = False
        screen_only("webcam composite failed — screen only")

cleanup = [screen_file, cam_file, mic_file, zoom_file] + ([cam_log] if have_cam else [])
for f in cleanup:
    if os.path.exists(f):
        os.remove(f)
