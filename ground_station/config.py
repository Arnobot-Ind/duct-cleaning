"""
Central configuration for the ground station.

Every value can be overridden with an environment variable, so you can point the
GUI at a different Pi without editing code:

    ROBOT_PI_IP=192.168.1.31 python3 main.py
"""

import os

_HERE = os.path.dirname(os.path.abspath(__file__))

# --- Network -----------------------------------------------------------------
# Static IPs from the guide (Step 2):
#   Ground Station Pi 4 ......... 192.168.1.10
#   Arduino + Ethernet Shield ... 192.168.1.20
#   Robot-side Pi (cameras) ..... 192.168.1.30
ROBOT_PI_IP = os.environ.get("ROBOT_PI_IP", "192.168.1.30")
RTSP_PORT = int(os.environ.get("RTSP_PORT", "8554"))

# Fallback URLs for the guide's two-Pi layout (cameras served by the robot Pi).
# The rig in use does NOT work that way any more - it runs two IP cameras on
# their own addresses, listed in cameras.txt, which overrides everything here.
CAM1_URL = os.environ.get("CAM1_URL", f"rtsp://{ROBOT_PI_IP}:{RTSP_PORT}/cam1")
CAM2_URL = os.environ.get("CAM2_URL", f"rtsp://{ROBOT_PI_IP}:{RTSP_PORT}/cam2")

# What each camera IS, not just its number: CAM 1 looks out the FRONT of the
# robot, CAM 2 out the BACK (operator spec 2026-08-18). The label rides on the
# panel header, the top-bar chip and the recording file names, so footage from
# a duct can be told apart without remembering which number faced which way.
_LABELS_RAW = os.environ.get("CAM_LABELS", "FRONT,BACK")
CAM_LABELS = [v.strip() for v in _LABELS_RAW.split(",")]


def camera_label(index):
    """FRONT / BACK / '' for camera `index` (0-based)."""
    return CAM_LABELS[index] if index < len(CAM_LABELS) else ""


def camera_name(index):
    """Display name: 'CAM 1 · FRONT'."""
    label = camera_label(index)
    return f"CAM {index + 1} · {label}" if label else f"CAM {index + 1}"


def camera_slug(index):
    """Filename stem: 'cam1_front'. Filesystem-safe, unlike the display name."""
    label = camera_label(index)
    return f"cam{index + 1}_{label.lower()}" if label else f"cam{index + 1}"


# WHICH CAMERAS A SESSION WRITES TO DISK. Operator 2026-09-12: "i want to keep
# just front camera video needed to store". The back camera is for driving;
# the front one is the footage. Writing both doubled the card, the stick and
# the join for a file nobody opened.
#
# Camera NUMBERS as the panel shows them - "1" is CAM 1 · FRONT alone, "1,2"
# is both, "2" the back alone - and an empty value records every camera. The
# screen is untouched: every camera is still decoded and shown, this only
# decides what a session records. The REC tag on a panel follows it, so a
# camera that is not being written never claims to be.
_RECORD_CAMERAS_RAW = os.environ.get("RECORD_CAMERAS", "1")


def _parse_camera_numbers(raw):
    """'1,2' -> {0, 1}; empty or nothing valid -> None, meaning every camera.

    Nothing-valid falls back to ALL rather than NONE on purpose: a typo in an
    environment file must not silently produce a session with no video in it.
    """
    picked = set()
    for token in (raw or "").split(","):
        token = token.strip()
        if token.isdigit() and int(token) >= 1:
            picked.add(int(token) - 1)
    return picked or None


RECORD_CAMERAS = _parse_camera_numbers(_RECORD_CAMERAS_RAW)


def camera_recorded(index):
    """True if camera `index` (0-based) is written to disk during a session."""
    return RECORD_CAMERAS is None or index in RECORD_CAMERAS


# ONE CAMERA ON SCREEN AT A TIME. Operator 2026-09-12: "i want just display one
# camera at once ... by default front camera and can be change by pressing
# save button". The two panels shared a 1024-wide screen, so each picture was
# half size; one at a time gives the picture the whole width.
#
# VIEW_SINGLE=1 shows one panel - VIEW_START_CAMERA (1 = CAM 1 · FRONT) first -
# and the panel SAVE button (GPIO5) swaps to the other. Both streams keep
# decoding underneath, so the swap is instant and the top-bar chips still
# report both cameras. Recording is unaffected: the recorder reads the streams,
# not the panels, so the front camera records whichever one is on screen.
#
# The SAVE button was free for this: banking a clip on it is off
# (RECORD_SAVE_BUTTON), and its one live job - ticking rows in the USB chooser
# - is kept, because the popup owns the button while it is open. Do not turn
# RECORD_SAVE_BUTTON back on with this set, or one press would do both.
#
# VIEW_SINGLE=0 restores the side-by-side pair. Keys 1 / 2 / 0 / C work either way.
VIEW_SINGLE = os.environ.get("VIEW_SINGLE", "1") == "1"
VIEW_START_CAMERA = int(os.environ.get("VIEW_START_CAMERA", "1") or 1)


# (label, url) pairs rendered left-to-right in the GUI.
CAMERAS = [
    (camera_name(0), CAM1_URL),
    (camera_name(1), CAM2_URL),
]

# Simplest way to set your two streams: edit cameras.txt next to this file,
# one RTSP URL per line. If that file exists it wins over everything above.
_CAM_FILE = os.path.join(_HERE, "cameras.txt")
if os.path.exists(_CAM_FILE):
    with open(_CAM_FILE, encoding="utf-8") as fh:
        _urls = [
            line.strip()
            for line in fh
            if line.strip() and not line.lstrip().startswith("#")
        ]
    if _urls:
        CAMERAS = [(camera_name(i), url) for i, url in enumerate(_urls)]
        CAM1_URL = CAMERAS[0][1]
        CAM2_URL = CAMERAS[1][1] if len(CAMERAS) > 1 else CAM1_URL

# --- Robot link ----------------------------------------------------------------
# Drives the CONNECTED / DISCONNECTED chip in the top bar. This is deliberately
# NOT derived from the camera URLs: the cameras are standalone IP cameras on
# 192.168.1.10x, so their reachability says nothing about the robot. A chip
# labelled ROBOT that is really reporting on a camera is worse than no chip.
#
# The only thing listening on the robot Pi today is MediaMTX, hence the RTSP
# port. Point this at the Arduino once Step 8's sketch answers on 192.168.1.20.
ROBOT_LINK_HOST = os.environ.get("ROBOT_LINK_HOST", ROBOT_PI_IP)
ROBOT_LINK_PORT = int(os.environ.get("ROBOT_LINK_PORT", str(RTSP_PORT)))

# Each poll opens and closes a TCP connection, which MediaMTX writes a line to
# its log for. Raise this if that noise bothers you - the chip just goes stale
# by that much longer.
LINK_POLL_S = float(os.environ.get("LINK_POLL_S", "2.0"))
LINK_TIMEOUT_S = float(os.environ.get("LINK_TIMEOUT_S", "1.5"))

# --- Ground station health ---------------------------------------------------
# How often the top bar re-reads this machine's SoC temperature. Unlike the robot
# probe above this is a sysfs file read, not a network round trip, so it is
# essentially free - the interval exists only because doing it at the 30fps UI
# rate would be pointless. See thermal.py.
TEMP_POLL_S = float(os.environ.get("TEMP_POLL_S", "2.0"))

# --- RTSP tuning -------------------------------------------------------------
# rtspsrc jitter buffer. 200ms measured best against a real 25fps IP camera;
# 50ms starves the decoder and 500ms is no better, see gst_pipeline() for the
# full measurements. Every ms here is added glass-to-glass delay, so do not
# raise it without measuring.
# 50 ms, down from 200 on 2026-08-31. This is DIRECT LAG: rtspsrc holds the
# picture this long before the decoder ever sees it, so 200 ms was 200 ms of the
# robot's past on screen.
#
# THE NOTE IN stream.py THAT SAID 50 WAS WORSE IS OUT OF DATE, and it is worth
# knowing why rather than just overriding it. That measurement - "latency=50
# drop-on-latency=true (no queue) .... 19.2 fps" - was taken BEFORE the leaky
# queue was added after the decoder. Without a queue a short buffer starves the
# sink and frames are lost; with one, the queue absorbs the jitter the buffer
# used to. Re-measured 2026-08-31 on the current pipeline, at the source rate
# throughout:
#
#     latency=200 .... 26.3 fps
#     latency=100 .... 26.0 fps
#     latency=50  .... 25.7 fps
#
# So the 150 ms is free. If the picture ever starts stuttering on a congested
# link, this is the first knob to put back up - the buffer exists to absorb
# network jitter, and a lossy link needs more of it than a quiet one.
RTSP_LATENCY_MS = int(os.environ.get("RTSP_LATENCY_MS", "50"))

# "tcp" is far more reliable on a long tether (no silent packet loss).
# Switch to "udp" only if you are chasing the absolute lowest latency.
RTSP_PROTOCOL = os.environ.get("RTSP_PROTOCOL", "tcp")

# Hardware H.264 decode (v4l2h264dec). LEAVE THIS OFF unless you re-measure.
#
# It sounds like an obvious win and is not. Measured on this Pi 4 against the
# 720p25 camera, with the viewer stopped so the baseline was 0.1% CPU:
#
#   software avdec_h264 .... 25.1 fps, 14.7% system CPU   <-- better on both
#   hardware v4l2h264dec ... 13.7 fps, 20.4% system CPU
#
# The V4L2 M2M decoder hands back NV12 in DMA buffers, and converting those to
# the BGR that OpenCV requires costs more CPU than the software decode ever
# saved - while also halving the frame rate. Software decode of one 720p stream
# is only ~0.6 of a core, so there is nothing to rescue here.
# OFF on 2026-08-31, and this was costing two thirds of the frame rate.
#
# v4l2h264dec is the Pi's hardware H.264 decoder and on paper it is the right
# choice - it should cost less CPU and less latency than avdec_h264 in software.
# Measured on this rig against a live camera, it does neither:
#
#     latency=200  hw=True ....  7.8 fps      hw=False .... 26.3 fps
#     latency=100  hw=True .... 11.5 fps      hw=False .... 26.0 fps
#     latency=50   hw=True ....  8.0 fps      hw=False .... 25.7 fps
#
# A THIRD OF THE SOURCE RATE, at every buffer setting, and GStreamer says why:
# "v4l2h264dec0: 1 initial frames were not dequeued: bug in decoder". The
# element does not hand its frames back cleanly to an appsink, so most of them
# never reach the UI. That is visible as a laggy, stuttering picture rather than
# as an error, which is why it survived so long.
#
# Software decode costs CPU - main.py sits near 190% with two 720p streams - and
# that is the trade being made deliberately: a Pi 4 has four cores and nothing
# else wants them, while dropped frames cannot be bought back.
#
# Worth retrying if the v4l2 stack is ever updated; the measurement above is the
# bar it has to clear.
USE_HW_DECODE = os.environ.get("USE_HW_DECODE", "0") == "1"

# --- Behaviour ---------------------------------------------------------------
# Start fullscreen. On the ground station there is NO window manager (X runs the
# app directly from .xinitrc), so a normal window just sits at its requested size
# in the top-left corner with dead space around it - nothing exists to maximise
# it. Set START_FULLSCREEN=0 when developing on a desktop.
START_FULLSCREEN = os.environ.get("START_FULLSCREEN", "1") == "1"

UI_FPS = int(os.environ.get("UI_FPS", "30"))  # GUI repaint rate

# How much of the frame you allow to be cropped so the picture covers more of
# the panel. The cameras are 16:9 and two of them side by side on a 1920x1080
# screen can only be ~945 wide, hence ~531 tall - which leaves a third of the
# screen black above and below the video.
#
#   0.0 ... whole frame, letterboxed (nothing hidden, biggest black bands)
#   0.2 ... 20% of the frame's width is cropped away, picture is 25% taller
#   1.0 ... picture covers the panel completely, ~45% of the width is gone
#
# It is a *crop*, not a zoom out of thin air: whatever this hides, the operator
# never sees. Raise it for a fuller screen, drop it to 0 to see everything the
# camera does.
# Default raised 0.2 -> 1.0 so the picture COVERS its panel with no black bands.
# This matters much more now the cameras are rotated: a 90-degree rotation makes
# the frame portrait inside a landscape panel, and at 0.2 that left roughly half
# the panel black down both sides. Covering crops instead - at 1.0 nothing is
# letterboxed but the parts of the frame outside the panel's aspect are not
# shown. Set VIDEO_ZOOM=0 to see the whole frame again, bands and all.
VIDEO_ZOOM = max(0.0, min(1.0, float(os.environ.get("VIDEO_ZOOM", "1.0"))))

# Rotate every decoded frame, clockwise degrees: 0, 90, 180 or 270.
#
# The cameras are mounted on their sides on this rig, so the default is 90.
# Applied in the capture thread with cv2.rotate (an optimised transpose) rather
# than as a QPainter transform per panel per frame: doing it once at the source
# keeps it off the GUI thread on a Pi that is already software-decoding two
# streams, and means the RECORDER writes the same orientation you are looking
# at. Rotating in the GUI would leave recordings sideways.
#
# 90 and 270 swap width and height, which VIDEO_ZOOM's geometry picks up on its
# own because it reads the frame's real dimensions.
# One value rotates every camera the same way; a comma-separated list sets them
# individually in cameras.txt order, so "180,90" is CAM 1 upside down and CAM 2
# on its side. A short list reuses its last value for the remaining cameras,
# which keeps a single value working when a third camera is added.
# Both cameras re-aimed on the operator's eye, 2026-08-25:
#   CAM 1 FRONT  180 -> 90 -> 0   "3 time right" then "1 time left":
#                                180+270 = 90, then 90-90 = 0
#   CAM 2 BACK    90 ->  0   "rotate 1 time left"  = -90,  and  90- 90 =  0
#
# Note this changes the MERGED view's geometry as well as the screen: full_nnn
# sizes its canvas from the two cameras' dimensions, and rotating one of them by
# a quarter turn swaps its width and height. That is not a fault, but it is why
# the canvas may no longer be the 2560x1280 the recorder's notes describe.
_ROT_RAW = os.environ.get("VIDEO_ROTATE", "0,0")
VIDEO_ROTATE_LIST = [int(v) % 360 for v in _ROT_RAW.split(",") if v.strip()]
VIDEO_ROTATE = VIDEO_ROTATE_LIST[0] if VIDEO_ROTATE_LIST else 0


def rotate_for(index):
    """Clockwise degrees for camera `index` (0-based). See VIDEO_ROTATE."""
    if not VIDEO_ROTATE_LIST:
        return 0
    return VIDEO_ROTATE_LIST[min(index, len(VIDEO_ROTATE_LIST) - 1)]

# Wordmark in the top bar, next to the logo.
TITLE = os.environ.get("GS_TITLE", "GROUND CONTROL STATION")

# --- Loading screen ----------------------------------------------------------
# The Arnobot logo covers the whole startup: X comes up on a bare black root,
# then cv2 + PySide6 import (~2s on a Pi 4), then the RTSP connect. Set SPLASH=0
# when developing, so the window appears immediately.
SPLASH_ENABLED = os.environ.get("SPLASH", "1") == "1"
LOGO_PATH = os.environ.get("LOGO_PATH", os.path.join(_HERE, "assets", "arnobot_logo.png"))

SPLASH_MIN_S = float(os.environ.get("SPLASH_MIN_S", "1.5"))   # don't flash past
SPLASH_MAX_S = float(os.environ.get("SPLASH_MAX_S", "8.0"))   # hand over regardless
# If one camera is dead, don't sit on the logo for the full timeout waiting for
# it — show the UI (with NO SIGNAL on that panel) once the others are up.
SPLASH_PARTIAL_S = float(os.environ.get("SPLASH_PARTIAL_S", "3.5"))
RECONNECT_DELAY_S = 2.0                        # wait before retrying a dead stream
READ_FAIL_LIMIT = 50                           # consecutive bad reads -> reconnect

# Ceiling for the exponential backoff between failed connection attempts.
#
# THIS IS THE SETTING THAT MAKES TWO CAMERAS WORK AT ONCE. Retrying a dead
# camera every RECONNECT_DELAY_S forever is what broke the rig: each attempt
# opens three separate TCP connections to the camera (reachability probe, codec
# DESCRIBE, then rtspsrc itself), so a flat 2s retry is ~90 connections/minute
# per camera. Measured on the real rig with both cameras configured, tcpdump
# counted 22 new connections to :554 in 30s across the pair. These cameras run a
# tiny embedded TCP stack and stop answering *even ARP* under that, which shows
# up as ICMP loss and "host unreachable" - so the retry storm was manufacturing
# the very outage it was retrying against, and each stall triggered more
# retries. One camera stayed under the threshold; two did not, which is exactly
# why it only ever failed with both configured.
RECONNECT_MAX_DELAY_S = float(os.environ.get("RECONNECT_MAX_DELAY_S", "20.0"))

# Backoff ceiling for the case where the camera is not on the wire at all.
#
# Deliberately much shorter than RECONNECT_MAX_DELAY_S, because the two failures
# want opposite treatment and lumping them together made outages longer than
# they needed to be:
#
#   camera ABSENT (no TCP connect, ARP fails) - it is powered down or rebooting.
#     Retrying costs ONE SYN that nothing answers, so it cannot overload anything,
#     and the only thing a long backoff buys is up to 20s of extra black panel
#     after the camera is already back. Poll briskly.
#   camera PRESENT but refusing RTSP - this is the session-limit case, and every
#     attempt is a real connection the camera has to service. This is the one
#     that needs the long backoff (see SESSION_WAIT_S).
#
# Measured: CAM 1 sat on "no signal - retrying" long after the camera answered.
UNREACHABLE_MAX_DELAY_S = float(os.environ.get("UNREACHABLE_MAX_DELAY_S", "4.0"))

# Minimum spacing between connection attempts across ALL cameras. The cameras
# share one tether, so two of them opening RTSP sessions simultaneously is the
# worst case; this staggers them instead. 0 disables the gate.
CONNECT_STAGGER_S = float(os.environ.get("CONNECT_STAGGER_S", "1.0"))

# How long to wait for a previous, abandoned RTSP session to actually die before
# opening a new one to the same camera.
#
# NEVER set this to 0. These cameras accept only a very small number of
# concurrent RTSP sessions, and a stalled reader keeps its session open until
# rtspsrc's tcp-timeout expires (~5s). Reconnecting inside that window asks the
# camera for a second session it cannot give, and it answers by resetting the
# connection - "Could not write to resource" in the GStreamer log. Measured on
# the rig: both cameras cycling connect/drop, CAM 1 live only 32% of the time
# with 17 reconnects in 3 minutes. Waiting for the old session first is what
# makes the reconnect land.
SESSION_WAIT_S = float(os.environ.get("SESSION_WAIT_S", "8.0"))
# Wall-clock seconds with no decoded frame before forcing a reconnect. Backstops
# the case where a camera half-closes its RTSP socket (CLOSE-WAIT) and cap.read()
# blocks forever, so READ_FAIL_LIMIT never trips and the panel freezes. Seen when
# a camera reboots/flaps under the viewer. Keep a couple of seconds above the
# real inter-frame gap so a momentarily slow camera is not torn down needlessly.
READ_STALL_TIMEOUT_S = float(os.environ.get("READ_STALL_TIMEOUT_S", "5.0"))

# How long to wait for the RTSP port to accept a TCP connection before declaring
# NO SIGNAL. On a direct LAN the round trip is sub-millisecond, so 4s is generous.
# This bounds the "tether unplugged" case - without it the OS TCP connect can
# hang for ~2 minutes because an unplugged cable produces no ARP reply at all.
OPEN_TIMEOUT_S = float(os.environ.get("OPEN_TIMEOUT_S", "4.0"))

SNAPSHOT_DIR = os.path.expanduser(os.environ.get("SNAPSHOT_DIR", "~/snapshots"))

# --- Recording ---------------------------------------------------------------
# Driven by the two panel switches (see inputs.py): switch 1 on GPIO22 is
# START/STOP, switch 2 on GPIO11 is PAUSE/RESUME. Each run gets its own
# directory under RECORD_DIR, named YYYYMMDD_HHMMSS_SESSIONnnn, with one file
# per RECORDED camera inside it - the front one alone unless RECORD_CAMERAS
# (above, next to the camera labels) says otherwise.
#
# /recordings (not ~/recordings) is the spec: ONE fixed folder on the Pi that
# the USB backup daemon (usb_backup.py) mirrors verbatim onto any stick that
# is plugged in. Created root-side with `sudo mkdir /recordings && sudo chown
# arnobot:arnobot /recordings`.
RECORD_DIR = os.path.expanduser(os.environ.get("RECORD_DIR", "/recordings"))

# Frames are re-encoded from the ones already decoded for the screen, so this is
# a straight CPU cost on top of the decode: ~0.4 of a core per 720p camera at 15.
# It is also the *wall-clock* sample rate, not just the header value - recorder.py
# writes one frame per tick whether or not the camera delivered a new one, so an
# hour of duct run is an hour of video and the timeline stays honest.
# WHICH CPU CORES THE RECORDING MAY USE. Empty disables the pinning entirely.
#
# Operator, 2026-09-01: "when i start recording to its generate lag on pi so use
# other core for saving so its not create a lagy". Recording adds two live
# encoders and, once a minute, an ffmpeg normalise - and on a 4-core Pi already
# running two camera decodes that is enough to make the UI and the 50 Hz drive
# frame wait behind video work.
#
# ONLY THE RECORDER IS PINNED, and that asymmetry is deliberate. Everything else
# is left free to roam all four cores, so restricting the recorder to 2 and 3
# does not take anything away from the UI - it guarantees the UI always has 0 and
# 1 to itself while recording, without capping what it can use when idle.
#
# Hard-partitioning both sides was the obvious alternative and is worse: it caps
# the decode at two cores even when nothing is recording, and the decode is the
# heavier job of the two.
RECORD_CPU_CORES = os.environ.get("RECORD_CPU_CORES", "2,3")

# How much to lower the recording threads' scheduling priority. 0 disables it.
#
# AFFINITY ALONE WAS NOT ENOUGH, which is the thing worth writing down. Pinning
# the recorder to cores 2 and 3 stops it taking 0 and 1 - but the UI thread is
# left free to roam all four, so it still lands on 2 and 3 and still queues
# behind an encoder there. Measured during a recording: the UI thread ran 3549ms
# and WAITED 653ms for a core in a 5 second window, with every core only ~60%
# busy. Not saturation - just the wrong thread going first.
#
# nice decides who goes first when both want the same core, so the two together
# say what is actually meant: the recorder may use these cores, and it may only
# have them when nothing else wants them.
#
# 10 rather than 19: the recorder must still keep up with a live camera, and 19
# is the floor reserved for work that genuinely does not matter if it never runs.
# The once-a-minute ffmpeg normalise IS that kind of work and does use 19.
RECORD_THREAD_NICE = int(os.environ.get("RECORD_THREAD_NICE", "10"))

# How much to lower GSTREAMER'S decode threads. 0 disables it.
#
# THEY OUTWEIGH THE UI AND RUN AT THE SAME PRIORITY, which is the whole reason
# this exists. Measured at idle, no recording: the main thread had used 9029
# jiffies while its GStreamer neighbours had used 10,700 between them - two
# rtpjitterbuffers and two queue:src workers, every one at nice 0 and free on all
# four cores. The UI thread waited 487ms out of every 5 seconds for a core with
# the machine only 60% busy. Not saturation, just an even fight it should not
# have been having.
#
# THE TRADE IS DELIBERATE: under load the video now drops a frame before the
# control surface stutters. On a machine that drives a robot that is the right
# way round, and there is headroom for both in the normal case.
#
# 5 rather than 10: the decoder still has to keep pace with a live 25 fps camera.
# It should lose an argument with the UI, not be pushed to the back of the queue.
STREAM_THREAD_NICE = int(os.environ.get("STREAM_THREAD_NICE", "5"))

# Which cores GStreamer's decode threads may use. Empty disables the pinning.
#
# NICE ALONE WAS NOT ENOUGH, and the reason is worth stating because it is the
# same lesson the recorder taught: priority decides who WINS a core, but the
# loser still has to find one. With every decode thread free on all four cores,
# the UI thread had to win an argument on whichever core it landed on, every
# time. Measured while recording: it waited 1220ms in every 5 seconds.
#
# Keeping the decode off core 0 leaves that core for the control path - the UI
# thread, the motor link and the ADC reader, none of which are pinned and all of
# which can therefore use it without an argument. They keep the other three too;
# this takes nothing away from them.
#
# THE ADC READER IS THE REASON THIS MATTERS BEYOND SMOOTHNESS. Its samples go
# stale in 250 ms and a stale sample sends zero, so a starved reader is not a
# cosmetic problem - it is the lamp switching off and the wheels dropping a
# command mid-drive.
STREAM_CPU_CORES = os.environ.get("STREAM_CPU_CORES", "1,2,3")


def stream_cores():
    """STREAM_CPU_CORES as a set of valid core numbers, or None if disabled."""
    raw = (STREAM_CPU_CORES or "").strip()
    if not raw:
        return None
    try:
        want = {int(v) for v in raw.split(",") if v.strip() != ""}
    except ValueError:
        return None
    cores = want & set(range(os.cpu_count() or 1))
    return cores or None


def record_cores():
    """RECORD_CPU_CORES as a set of valid core numbers, or None if disabled."""
    raw = (RECORD_CPU_CORES or "").strip()
    if not raw:
        return None
    try:
        want = {int(v) for v in raw.split(",") if v.strip() != ""}
    except ValueError:
        return None
    have = set(range(os.cpu_count() or 1))
    cores = want & have
    # Never return an empty set: that would be an affinity mask no thread can be
    # scheduled on. A machine with fewer cores than the config assumes falls back
    # to no pinning rather than to something unschedulable.
    return cores or None


RECORD_FPS = float(os.environ.get("RECORD_FPS", "15"))

# HOW OFTEN A RUNNING RECORDING ROLLS TO A NEW CLIP, seconds. 0 disables it and
# restores the old behaviour of one clip per run.
#
# THIS IS WHAT MAKES SAVING FAST, and it is worth knowing why a rollover buys
# anything at all. The recorder writes MPEG-4 while filming because that is the
# only codec cv2 can encode fast enough to keep up - 2.6s per 60 frames against
# 49s for H.264, measured 2026-08-31. Everything then has to be re-encoded to
# H.264 at save time, and that re-encode is the wait: a 100-minute session took
# about 25 minutes, because 90,000 frames is genuinely that much work.
#
# The encoder runs at about 4x realtime, so it can finish a clip long before the
# next one closes and still idle three quarters of the time. Rolling every
# RECORD_SEGMENT_S turns one enormous job at the end into a stream of small ones
# that keep pace with the filming, and leaves only the final partial clip plus
# the join to do at stop.
#
# 60s, on the operator's call after the numbers were measured 2026-08-31:
#
#     encode rate .......... 5.3x realtime
#      60s clip ............ encoded in 11.3s, then 49s idle
#     120s clip ............ encoded in 22.7s, then 97s idle
#
# Both keep up with room to spare, so the only thing the length decides is the
# TAIL - the unfinished clip still to encode when the operator presses stop.
# That is the only wait anyone actually feels, and 60s halves it: about 11
# seconds against 23. The cost is twice as many ffmpeg launches, which is cheap
# now each one is niced and runs on the hardware encoder.
#
# WHY THE ENCODER HAS THE HEADROOM AT ALL, since it is the whole basis of this:
# at 5.3x realtime it is idle about 80% of the time while filming. The old
# design saved that idle capacity up and spent it after the stop; this spends it
# as it goes. Nothing here is faster - the same frames are encoded either way -
# it is only moved off the end of the run, where somebody is waiting.
#
# Do not raise this past the point where a clip takes longer to encode than to
# film. At 5.3x that is far away, but a slower encoder or a busier Pi moves it.
# 60s - ON, restored on the operator's call 2026-09-01 after trying it off.
#
# WHAT TURNING IT OFF COSTS, which is why it came back: the whole session
# normalises after STOP instead of a clip at a time, so a long run takes minutes
# to save where this makes it about eleven seconds. The operator would rather
# have the fast save.
#
# WHAT IT COSTS TO HAVE IT ON, measured and unresolved. Rolling a clip mid-run meant an ffmpeg
# normalise ran WHILE the operator was driving, and that was felt: "its lag when
# video saving process start, video save is finished to its not lagy". Three
# attempts to make it unobtrusive all failed to help much, and the measurements
# say why:
#
#     no ffmpeg ................ UI waited  533ms per 5s
#     ffmpeg at nice 19 ........ UI waited 1042ms
#     ffmpeg at SCHED_IDLE ..... UI waited  947ms
#
# SCHED_IDLE is a class below every normal task and it still cost nearly double
# the wait. So the contention is NOT the CPU scheduler - it is memory bandwidth
# and the hardware video block, and no priority, affinity or I/O class reaches
# those. The work cannot be hidden; it can only be moved.
#
# SHORTER CLIPS WERE CONSIDERED AND REJECTED for the same reason. The operator
# asked whether 30s would help. The same total work would arrive in more
# frequent, shorter bursts - the lag chopped up rather than removed, and an
# interruption every 30s is worse to drive through than one every 60s.
#
# WHAT THIS COSTS: the whole session normalises after STOP instead, so a long run
# takes minutes to save where segmenting made it about eleven seconds. That is
# the trade the operator chose, and it is the right way round - the wait happens
# when nobody is driving, and driving is when lag actually matters.
#
# The machinery is all still here and still correct: set this to 60 and clips
# roll again. Nothing else needs changing.
# 60, BECAUSE A POWER CUT CANNOT BE CAUGHT. Operator 2026-09-02: "i am off pi
# without stop recording, to video is not ready for transfer".
#
# cv2.VideoWriter writes an MP4's moov atom - its index - only on release(). Pull
# the plug and that never happens, so the file holds every frame and no way to
# find them: ffmpeg reports "moov atom not found" and refuses to open it at all.
# 22 sessions on this card are unreadable for exactly that reason and no amount
# of repair logic will bring them back.
#
# AT 0 THE WHOLE RUN IS ONE FILE, so a cut at minute 59 of an hour costs the
# hour. At 60 the writer closes and reopens every minute, so everything before
# the last minute is already indexed and readable, and the cut costs a minute.
#
# THE COST IS A CONCATENATION AT STOP, and it is a stream copy - no decode, no
# encode, disk speed. A four-minute session joined in well under a second when
# this was measured, which is why segmenting is affordable here in a way that
# re-encoding never was. It was set to 0 on 2026-08-31 to kill an encode that
# ran per segment; that encode is gone (RECORD_NORMALIZE = 0), so the reason for
# 0 went with it and only its downside remained.
# 20, DOWN FROM 60 AFTER THE REBOOT TEST OF 2026-09-02.
#
# The test worked - the interrupted run came back merged and playable - but the
# run that was live at the moment of reboot had lasted 12 seconds against a 60
# second segment, so nothing had closed and the whole thing was unreadable. That
# is the honest shape of this setting: EVERYTHING SHORTER THAN ONE SEGMENT IS
# LOST, so the segment length is the size of the hole a power cut leaves.
#
# 20 makes that hole a third of what it was. The cost is three times the parts -
# 3 per camera per minute - joined at stop by a stream copy that runs at disk
# speed, so a long session costs seconds, not minutes. It is affordable here
# only because RECORD_NORMALIZE is 0; with the old per-segment encode this
# number could not have gone below a minute.
#
# NOW 10, down from 20, because the segment length IS the minimum recording that
# survives a power cut. Operator 2026-09-02: "i want to when restart all small
# and big video saved". A run shorter than one segment has nothing closed, so no
# moov index was ever written and there is no video to recover - at 20 that made
# every clip under 20 seconds unrecoverable, which is a lot of short inspection
# runs.
#
# 10 is close to the practical floor. Below it the segment approaches the
# keyframe interval, and a segment that cannot start on a keyframe stops being
# joinable by stream copy - the join would need a re-encode, which is the thing
# this whole design exists to avoid. If clips under 10 seconds must survive too,
# the answer is not a smaller number here but a container that tolerates
# truncation (Matroska, or fragmented MP4), which is a change to how the writer
# is opened rather than to this constant.
RECORD_SEGMENT_S = float(os.environ.get("RECORD_SEGMENT_S", "10"))


# HOW MUCH OF THE CARD RECORDINGS MAY OCCUPY, in GB. Operator 2026-09-02: "i want
# to use only 20 gb, and when its above saved 20 gb to delete first recording -
# means when last save to first delete when storage full".
#
# A ring buffer, oldest out first. At the measured 1.56 GB/hour for both cameras
# this holds about 12 hours 48 minutes, and a run that would push past it evicts
# the oldest COMPLETE session rather than stopping or filling the card.
#
# 20 AGAINST 22.8 GB FREE IS THE POINT, not a rounding. The card is 29 GB with
# the OS on the same filesystem - there is no separate partition - so recordings
# and /var share what is left. Leaving ~2.8 GB spare is what keeps logs, the
# journal and temp files writable, and a completely full filesystem is a
# well-known way to corrupt a card.
RECORD_MAX_GB = float(os.environ.get("RECORD_MAX_GB", "20"))
RECORD_MAX_BYTES = int(RECORD_MAX_GB * 1e9)

# Join the numbered clips into one file per camera at the end of a save.
#
# THIS IS THE SLOW PART OF A SAVE, and it is slow for a reason no amount of CPU
# will change. /recordings is the SD card and it writes at about 20 MB/s,
# measured 2026-09-01. The join reads every clip and writes one combined file,
# so it moves the whole session across that card TWICE - roughly 100 seconds per
# gigabyte per camera. The operator sees the dial race through the normalise
# steps, which are already done by then, and stop dead around 60-80% while this
# runs.
#
# TURNING IT OFF LEAVES THE NUMBERED CLIPS - cam1_front_001.mp4, _002 and so on.
# They are already normalised H.264 and play on anything; there are just several
# per camera instead of one. The USB backup copies them all either way.
#
# So this is a straight trade with no cleverness available: one tidy file per
# camera, or a save that ends when the last clip finishes normalising. Nothing in
# between, because concatenating MP4s means rewriting the container and that
# means writing the bytes.
# ON, and with RECORD_SEGMENT_S at 0 it costs nothing. A session that is one
# clip per camera has nothing to concatenate, so _join RENAMES the file instead
# of copying it - see the len(parts) == 1 branch there. That is where the whole
# 100-seconds-per-gigabyte disappears to: not optimised, avoided.
RECORD_JOIN_CLIPS = os.environ.get("RECORD_JOIN_CLIPS", "1") == "1"

# "mp4v" (MPEG-4 Part 2) is the one fourcc the apt OpenCV can always write into
# a .mp4 without an external encoder. "avc1" gives smaller files where the build
# has libx264, and dies with a warning where it does not - so it is not default.
RECORD_FOURCC = os.environ.get("RECORD_FOURCC", "mp4v")
RECORD_EXT = os.environ.get("RECORD_EXT", ".mp4")

# 0 keeps the camera's native size. Set e.g. 960 to shrink 720p before encoding
# if the Pi runs out of CPU with both cameras recording.
RECORD_MAX_WIDTH = int(os.environ.get("RECORD_MAX_WIDTH", "0"))

# Centre-crop every camera to ONE COMMON SQUARE before encoding. 0 disables it
# and each camera records its own native frame again.
#
# Why a square (operator spec 2026-08-19: "store both camera feed to same
# quality same size ... both in feed same size width and height"). The two
# cameras are mounted 90 degrees apart, so after VIDEO_ROTATE one is 1280x720
# and the other 720x1280. Two pictures cannot be the same width AND the same
# height unless they share an aspect ratio, so one had to be chosen:
#
#   * pad both into a common box - keeps every pixel, but the pictures are
#     still different shapes and the files carry a lot of black;
#   * fit the portrait camera into a landscape frame - drops CAM 2 from 0.92MP
#     to 0.29MP, which is most of the detail on a duct camera;
#   * CROP both to the largest square either can supply - what this is. The
#     pictures come out genuinely identical, 720x720, at native pixel density
#     with nothing scaled or stretched. It costs the left/right edges of the
#     landscape camera and the top/bottom of the portrait one, permanently.
#
# 720 is not arbitrary: it is min(1280, 720), the largest square that fits
# inside both cameras' frames, so neither is ever upscaled to reach it.
RECORD_SQUARE_PX = int(os.environ.get("RECORD_SQUARE_PX", "720"))

# A DIGITAL-RECORDER TIMESTAMP IN THE TOP-RIGHT OF EVERY RECORDED FRAME.
# Operator 2026-09-12: "a small white digital recording timestamp overlay
# showing date and time, formatted as 2026-09-12 13:42:18, slightly
# transparent, monospaced digital font, subtle camera-recorder OSD style,
# fixed to the video frame ... i want to store time also". Footage from a duct
# now says WHEN as well as what, inside the pixels, so it survives every copy.
#
# Burned in by the ENCODER thread only (recorder.TimestampOsd): the live
# screen keeps the clean picture, and the stamp is drawn on a copy so the
# decode buffer the panels share is never written to. On the Pi it renders
# DejaVu Sans Mono Bold through OpenCV's freetype module; a build without
# freetype falls back to OpenCV's Hershey font, same place, same size.
#
# Cost: one small alpha blend per frame (a strip ~250x28 px on a 720 frame)
# plus one glyph render per SECOND, because the text only changes then.
RECORD_TIMESTAMP = os.environ.get("RECORD_TIMESTAMP", "1") == "1"
RECORD_TIMESTAMP_FORMAT = os.environ.get("RECORD_TIMESTAMP_FORMAT",
                                         "%Y-%m-%d %H:%M:%S")
# 1.0 is solid white; the spec asked for slightly transparent.
RECORD_TIMESTAMP_ALPHA = float(os.environ.get("RECORD_TIMESTAMP_ALPHA", "0.85"))

# Re-encode the per-camera masters AFTER a save so the two cameras produce files
# of the same SIZE, not merely the same pixel dimensions (operator spec
# 2026-08-19: "one file store too much size and other low").
#
# Why it cannot be done live. The masters are written by cv2.VideoWriter with
# RECORD_FOURCC, and the only fourcc that keeps up on this Pi is mp4v, which
# exposes no bitrate control whatsoever. Measured here 2026-08-19, both cameras
# at 720x720/15 with the viewer running: avc1 (libx264 in-process) collapsed to
# ~3 fps - 46 frames where mp4v wrote 137 - and left a file with no moov atom.
# Two in-process x264 encoders do not fit beside two decodes and the UI. So the
# live path stays mp4v and the size is fixed offline, on idle cores, exactly the
# way COMBINED_AFTER_SAVE already fixes the full view.
# OFF, 2026-09-01. This is the setting that buys "fast AND no lag AND one file".
#
# Normalising re-encodes every recorded frame from MPEG-4 to H.264, and that
# encode is the only expensive thing a save does. It has to happen SOMEWHERE:
# during the recording, where it lagged the driving, or after the stop, where it
# made the operator wait minutes. Three attempts to schedule it out of the way
# all failed - nice 19, SCHED_IDLE and core pinning each left the UI waiting
# nearly twice as long, because the contention is memory bandwidth and the
# hardware video block, not CPU time.
#
# So it is not done at all. The clips stay as the MPEG-4 the recorder wrote,
# which plays anywhere, and a save becomes a rename.
#
# WHAT IT COSTS: measured on a real 58-second clip, mpeg4 is 19 MB against h264
# at 8 MB - about 2.2x the disk. A 100-minute two-camera session is therefore
# roughly 3.8 GB rather than 1.7 GB, against 18 GB free.
#
# WATCH THE FREE SPACE. That is about five long sessions rather than eleven, and
# the honest fix for that is a bigger or faster card, not turning this back on.
RECORD_NORMALIZE = os.environ.get("RECORD_NORMALIZE", "0") == "1"

# A CAMERA THAT DROPS OUT PAUSES ITS RECORDING instead of being papered over,
# operator 2026-08-26: "if its camera off to auto paused in saved and when
# recieved to its continue".
#
# What it used to do: hold the last good frame and keep writing it at
# RECORD_FPS, so the file stayed real-time and a 20s dropout became 20s of a
# frozen picture. That is honest about the CLOCK and dishonest about the
# PICTURE, and it spends bitrate on a still.
#
# What it does now: after this many seconds with no new decoded frame, the
# writer stops taking frames. The file stays OPEN and the clip does not roll,
# so when video comes back it continues into the same file - no new clip, no
# gap in the numbering, nothing extra for the merge to join.
#
# THE TRADE, stated plainly: the recording is now shorter than wall-clock by
# whatever the camera missed, and if only ONE camera drops, front.mp4 and
# back.mp4 come out different lengths and no longer line up in time. That is
# the unavoidable cost of not recording the dropout. Set this to 0 to go back
# to frame-holding, which keeps the two files the same length.
#
# The grace period matters: a single late frame is normal on RTSP and must not
# chop the video. One second is several frames' worth of patience.
RECORD_STALL_PAUSE_S = float(os.environ.get("RECORD_STALL_PAUSE_S", "1.0"))

# CONSTANT bitrate, deliberately not CRF. CRF gives both cameras identical
# QUALITY and lets the busier scene produce the bigger file - measured 1.56 MB
# against 1.29 MB for the same clip, which is the behaviour being complained
# about. Pinning minrate=maxrate=bufsize=b:v with nal-hrd=cbr makes x264 pad to
# the target instead, so two clips of equal length come out equal size.
#
# The cost is real and is the operator's call: at a fixed rate the camera
# looking at more detail gets less of it. 1200k is chosen against measurement -
# mp4v was spending 2.2-2.8 Mbit/s on these same 720x720 frames, and an offline
# x264 pass at visually-equivalent quality wanted ~1.1 Mbit/s.
RECORD_NORM_BITRATE = os.environ.get("RECORD_NORM_BITRATE", "1200k")
RECORD_NORM_PRESET = os.environ.get("RECORD_NORM_PRESET", "veryfast")

# WHICH ENCODER DOES THE WORK. h264_v4l2m2m is the Pi 4's HARDWARE H.264
# encoder; libx264 is the software one that was here until 2026-08-26.
#
# MEASURED ON THIS PI, one 15 s 720p clip:
#
#     libx264 -preset veryfast    16.2 s     <- what normalising used to cost
#     libx264 -preset ultrafast    8.8 s
#     h264_v4l2m2m                 6.4 s     <- 2.5x faster than veryfast
#
# Normalising runs on EVERY clip of EVERY camera, so it is the dominant cost of
# a save and the reason the operator reported the merge as too slow. Moving it
# onto the VideoCore leaves the CPU free for the decode side as well, which the
# stopwatch above does not even capture.
#
# THE x264-ONLY FLAGS ARE SKIPPED when this is not libx264 - preset, profile,
# level and -x264-params are x264 spellings and the v4l2m2m wrapper rejects
# them. The CBR bitrate below is what pins the size either way, and it is the
# part that actually matters: it is why two cameras looking at very different
# scenes still produce files of the same length.
#
# SET THIS BACK TO libx264 if the hardware encoder ever produces unplayable
# files - it is a wrapper around a kernel driver and is fussier than x264 about
# odd resolutions. Quality at a fixed bitrate is slightly worse; speed is the
# trade being made.
RECORD_NORM_VCODEC = os.environ.get("RECORD_NORM_VCODEC", "h264_v4l2m2m")

# H.264 profile and level for the normalised masters.
#
# The share complaint that produced this ("when i store, file stored file
# extension is not supported for share, but merged video is") was never about
# the extension: all three files are .mp4. It was the CODEC inside. Measured on
# a real session 2026-08-19:
#
#     cam1_front_001.mp4   mpeg4, Simple Profile, tag mp4v   <- refused
#     cam2_back_001.mp4    mpeg4, Simple Profile, tag mp4v   <- refused
#     full_001.mp4         h264,  Constrained Baseline, avc1 <- shared fine
#
# mp4v is MPEG-4 Part 2, which phones and messaging apps stopped accepting years
# ago; the full view already went out as H.264 because it is built by ffmpeg
# rather than by cv2.VideoWriter. Re-encoding the masters fixes it on its own -
# pinning the profile is what keeps it fixed on the oldest handset likely to be
# handed one of these.
#
# BASELINE, NOT MAIN - round two of the same complaint, 2026-08-20: "the merged
# video was perfect to store and share but single cam was not". Both files were
# H.264 by then, so this time the difference was WHICH H.264:
#
#     full_001.mp4        Constrained Baseline, 0 B-frames, level 3.2  <- shared
#     cam1_front_001.mp4  Main,                 2 B-frames, level 4.0  <- did not
#
# The full view was only Constrained Baseline by accident: COMBINED_PRESET is
# ultrafast, and ultrafast turns CABAC and B-frames off. That accident is the
# configuration that demonstrably travels, so the masters are now pinned to the
# same thing deliberately - and so is the full view, see COMBINED_PROFILE.
#
# B-frames are the expensive part to decode and the part simple players get
# wrong; baseline drops them and CABAC with them. At CBR the file SIZE does not
# move (RECORD_NORM_BITRATE pins it) - what is spent is a little quality at the
# same bitrate, which is a fair price for a file that opens on the first try.
RECORD_NORM_PROFILE = os.environ.get("RECORD_NORM_PROFILE", "baseline")
RECORD_NORM_LEVEL = os.environ.get("RECORD_NORM_LEVEL", "3.2")

# Equal bitrate only yields equal size if the files are equally LONG, and they
# are not: each camera's recorder thread runs its own tick loop, so one measured
# clip closed at 87 frames on CAM 1 and 91 on CAM 2. That 267ms also drifts the
# two halves of the full view apart. Trim every camera in a clip to the shortest
# one's frame count.
#
# Trimmed from the TAIL, so the head alignment the full view depends on
# (CameraRecorder._clip_first_write -> its lead-in padding) is untouched.
RECORD_NORM_MATCH_FRAMES = os.environ.get("RECORD_NORM_MATCH_FRAMES", "1") == "1"

# Hard stop on one master's re-encode, so a wedged ffmpeg cannot hang the save.
RECORD_NORM_TIMEOUT_S = float(os.environ.get("RECORD_NORM_TIMEOUT_S", "900"))

# Stop writing rather than fill the SD card. A full root filesystem does not
# just lose the recording - it takes X, the viewer and this SSH session with it,
# which is a far worse failure than a truncated video.
RECORD_MIN_FREE_MB = int(os.environ.get("RECORD_MIN_FREE_MB", "512"))

# Seconds to hold a finished recording waiting for the SAVE button before
# deleting it. Stopping does not keep the run any more - the operator has this
# long to say the run was worth keeping, and silence means it was not.
#
# THIS DELETES VIDEO. It is the one setting here that can lose work, so it is
# deliberately generous and deliberately loud in the UI: the strip counts the
# window down and the SAVE pill pulses for the whole of it. Set
# RECORD_CONFIRM_S=0 to go back to keeping everything automatically.
#
# 15 -> 10 on operator spec 2026-08-19: the window only has to outlast the walk
# from the panel back to the screen, and RECORD_SAVE_HOLD_S extends it while the
# button is down, so a hold begun on the last second still lands.
# 0 = KEEP EVERYTHING AUTOMATICALLY, operator's instruction 2026-08-26: "its
# auto save when stop recording, remove save button logic".
#
# At 0 the confirm window never opens: _stop_session keeps the run, toasts
# SAVED and starts the merge immediately, and the GPIO25 press-and-hold is
# never consulted. The button's other job - banking a clip mid-run - still
# works, but with the join stage in recorder.py it no longer changes what
# lands on the stick, only where the internal split points are.
#
# THIS IS A DELETING DECISION REVERSED. Any positive value restores the old
# behaviour: a window of that many seconds after STOP in which footage is
# DISCARDED unless claimed by a hold. That was in force earlier the same day;
# it is off now because the operator asked for it, not because it was wrong.
# THE SAVE BUTTON NO LONGER BANKS CLIPS. Operator 2026-08-27: "remove totally
# save button logic in the recording, it make auto save after stop recording".
#
# The gesture was: a tap while rolling closes the current clip and opens the
# next, so an operator could mark something worth keeping without stopping. It
# worked exactly as designed - which was the problem. GPIO25 chatters at about
# four edges a second, and the recorder banked a clip for every one of them:
# session12 collected 565 clips in 676 seconds, session06 168. Each clip then
# needs three ffmpeg stages, so those sessions could never finish processing,
# sat on PROCESSING for ever, and the load they generated is what stopped
# systemd petting the 60-second hardware watchdog and hard-reset the Pi.
#
# So the tap does nothing now. A run is one clip per camera, start to stop, and
# stopping keeps it automatically (RECORD_CONFIRM_S = 0). Set
# RECORD_SAVE_BUTTON=1 to bring the gesture back once the switch is repaired.
RECORD_SAVE_BUTTON = os.environ.get("RECORD_SAVE_BUTTON", "0") == "1"

# ARMING DELAY. Operator 2026-08-27: "when start recording to its start after 3
# second not instant and before 3 second not start the recording".
#
# Throwing the run lever no longer starts writing immediately - it arms, and
# recording begins RECORD_START_DELAY_S later. Drop the lever inside that window
# and NOTHING is recorded: no session directory, no clip, nothing to discard.
#
# It earns its place twice. It gives the operator a beat to get their hand off
# the panel and out of shot, and it makes a knocked lever free - this rig has
# already produced three dated empty folders in ten seconds from switch bounce
# alone, and a start that costs nothing until it has been held is a start that
# bounce cannot fake.
#
# 0 disables the delay and starts on the throw, as before.
RECORD_START_DELAY_S = float(os.environ.get("RECORD_START_DELAY_S", "3.0"))

RECORD_CONFIRM_S = float(os.environ.get("RECORD_CONFIRM_S", "0"))

# How long a run must last before stopping it is worth asking about. Operator's
# instruction 2026-08-25: "when start recording and its more than 3 second and
# press stop button to show save 10 second".
#
# Was hardcoded at 1.0 s inside recorder._stop_session, which is why it could
# not be tuned from here. 1 s was chosen to swallow switch bounce; 3 s also
# swallows the deliberate-but-pointless run - a lever knocked on and off while
# the operator repositions - so the confirm window is only spent on footage
# somebody might actually want.
#
# Runs SHORTER than this are dropped silently: no window, no toast, and the
# session directory is removed if nothing was written into it.
RECORD_MIN_RUN_S = float(os.environ.get("RECORD_MIN_RUN_S", "3.0"))

# Build a third file per clip - full_nnn.mp4 - with BOTH cameras side by side in
# one frame, each tile labelled. This is what gets handed to whoever asked for
# "the video" singular; the per-camera files stay because they are the masters.
#
# BUILT AFTER THE SAVE, NOT WHILE RECORDING (operator spec 2026-08-19). It used
# to be a third live encoder, and that is what made it wrong on both counts the
# operator complained about:
#
#   * it scaled both cameras to COMBINED_HEIGHT=480, well under the 720 the
#     cameras actually stream, because a live 720 encoder does not fit; and
#   * it gave each camera a width from its OWN aspect ratio, so with CAM 2
#     rotated 90 the tiles came out 852px + 270px - a 76/24 split, measured on
#     /recordings/20260819_143315_SESSION009/full_001.mp4 at 1122x480.
#
# Measured on this Pi 4, a live 50/50 canvas at stream resolution is not
# available at any setting: 2560x1280 encodes at 9.4fps against the 15 it would
# need, the hardware H.264 encoder tops out at 1920x1920 so it cannot help, and
# even 2560x720 leaves only 11% margin on top of two decodes and two encoders.
#
# Building it from the finished per-camera files instead costs NOTHING while
# recording, gets both cameras at their exact native size, and only spends the
# effort on runs the operator actually kept. The tradeoff is that full_nnn.mp4
# appears a beat after the save rather than growing live - the strip reports the
# build so the operator knows it is happening.
# OFF since 2026-08-26, operator's instruction: "not save merge video, remove,
# only save front and back". A session now leaves TWO files, one per camera.
#
# WHAT TURNING THIS OFF ACTUALLY SKIPS. The per-clip hstack encode - the single
# most expensive stage of a save, because it decodes two streams, scales both
# and encodes a third. Normalising still runs (RECORD_NORMALIZE), so the
# per-camera files are still real h264 that anything can play, and the join
# still reduces them to one file per camera.
#
# So this is not only a file the operator did not want: it is most of the wait
# after a save. Expect the processing to finish in a fraction of the time it
# took while the side-by-side was being built.
#
# Set back to 1 to get full.mp4 again. Nothing else has to change - the join
# already looks for the per-clip full_nnn files and simply finds none.
COMBINED_AFTER_SAVE = os.environ.get("COMBINED_AFTER_SAVE", "0") == "1"

# 0 = each camera at its native size, which is the point of building offline:
# the half-width is the widest camera and the canvas height the tallest, so
# nothing is downscaled and neither camera is stretched - the leftover inside
# each half is padded black. With CAM 1 at 1280x720 and CAM 2 rotated to
# 720x1280 that is a 2560x1280 file, exactly 50/50.
#
# Set a pixel cap (e.g. 960) if that is too big for whoever receives it; tiles
# are then scaled down together, so the 50/50 split survives.
COMBINED_MAX_HALF = int(os.environ.get("COMBINED_MAX_HALF", "0"))

# The offline encoder. Speed here is wall clock the operator waits AFTER a save,
# measured on the Pi 4 at 2560x1280: ultrafast/crf28 lands near 1x realtime,
# superfast/crf28 is ~0.43x for ~40% smaller files. libx264 rather than the
# mp4v the live recorders use - the full view travels to other machines, and
# H.264 plays everywhere MPEG-4 Part 2 does not.
# h264_v4l2m2m since 2026-08-26 - the Pi's hardware encoder. See
# RECORD_NORM_VCODEC for the measurements. The side-by-side decodes two streams
# and encodes one, so handing the encode to the VideoCore also gives the two
# decoders a core each.
COMBINED_VCODEC = os.environ.get("COMBINED_VCODEC", "h264_v4l2m2m")

# Bitrate for any encoder that is NOT libx264. CRF is an x264 idea; the v4l2m2m
# wrapper ignores it and falls back to ffmpeg's 200 kbps default, which looks
# exactly like a broken camera. This is the number that stops that happening.
COMBINED_BITRATE = os.environ.get("COMBINED_BITRATE", "4M")
COMBINED_PRESET = os.environ.get("COMBINED_PRESET", "ultrafast")
COMBINED_CRF = os.environ.get("COMBINED_CRF", "28")

# STATED, NOT INHERITED. The full view has always come out Constrained Baseline
# and that is why it shares everywhere - but it came out that way only because
# ultrafast happens to disable CABAC and B-frames. Anyone who changed the preset
# for smaller files (superfast is right there in the comment above) would have
# silently turned the one reliably shareable file on the card into a Main
# profile one, with nothing to say it had happened until a handset refused it.
# Saying it explicitly costs nothing and cannot drift. Same pair as
# RECORD_NORM_PROFILE, deliberately: the masters and the merged view should not
# be two different kinds of H.264.
COMBINED_PROFILE = os.environ.get("COMBINED_PROFILE", "baseline")
COMBINED_LEVEL = os.environ.get("COMBINED_LEVEL", "3.2")

# Hard stop on one clip's build, so a wedged ffmpeg cannot leave the strip
# saying BUILDING for ever. Generous: 0.43-1.2x realtime means a 20 minute clip
# is inside this, and a build that trips it leaves the per-camera masters intact.
COMBINED_TIMEOUT_S = float(os.environ.get("COMBINED_TIMEOUT_S", "3600"))

# Kept for the headless test and for anyone who still wants the old live third
# encoder. Default OFF - see COMBINED_AFTER_SAVE above for why.
RECORD_COMBINED = os.environ.get("RECORD_COMBINED", "0") == "1"
COMBINED_HEIGHT = int(os.environ.get("COMBINED_HEIGHT", "480"))

# Where usb_backup.py (a separate root daemon) publishes its transfer status,
# and where the viewer reads it from to show "data is transferring" in the
# strip. /run is tmpfs: root-writable, world-readable, gone on reboot - all
# three of which are right for live status.
USB_STATUS_PATH = os.environ.get("USB_STATUS_PATH", "/run/usb_backup_status.json")

# How long the SAVE button must be HELD, after a stop, to claim the recording.
# A press-and-hold rather than a tap on purpose (operator spec 2026-08-18):
# the same physical button banks clips mid-run on a tap, so the gesture that
# permanently keeps a whole session is made deliberate. The confirm window
# above is extended while the button is down, so a hold started on the last
# second still lands.
RECORD_SAVE_HOLD_S = float(os.environ.get("RECORD_SAVE_HOLD_S", "2.0"))

