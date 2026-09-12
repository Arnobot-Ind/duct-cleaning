#!/usr/bin/env python3
"""
Recording: panel switches -> video files on disk.

Same split as stream.py / inputs.py - this module owns the encoders and their
threads, main.py owns the single UI timer and pushes state in. Nothing here
touches Qt.

WHAT DRIVES IT (inputs.py decodes the pins, this acts on the decode):

    switch 1, red leg, GPIO22 .... START / STOP
    switch 2, green leg, GPIO11 .. PAUSE / RESUME
    button,   GPIO9 .............. SAVE - while rolling, a tap cuts the clip
                                   and keeps rolling; after STOP, HOLDING it
                                   for RECORD_SAVE_HOLD_S (2s) finalizes the
                                   session into RECORD_DIR (/recordings)

WHY IT RE-ENCODES THE DECODED FRAMES rather than copying the RTSP stream with
ffmpeg, which would be nearly free:

  * These cameras are cheap IP units with a small cap on concurrent RTSP
    sessions. A second connection per camera, opened exactly when the operator
    starts recording, is the one moment you cannot afford the viewer to lose its
    picture - and that is the failure it would produce.
  * What lands on disk is then provably what the operator saw, including the
    reconnects. A `-c copy` file and the screen can silently disagree.

The cost is real and budgeted: ~0.4 of a core per 720p camera at RECORD_FPS=15,
on top of the ~0.6 the decode already takes. Two cameras recording is ~2 of the
Pi 4's 4 cores. Drop RECORD_FPS or set RECORD_MAX_WIDTH if that is too tight.

Frames are written on a wall clock, not on stream arrival: one frame per tick
whether or not the camera delivered a new one. An hour of duct run is therefore
an hour of video, and a camera that stalls for 20 s leaves 20 s of held frame
instead of a jump cut that hides the outage. Time in PAUSE is not written at
all, which is what makes pause a cut rather than a freeze.

Exercise it headless, with no switches and no cameras:

    python3 recorder.py rtsp://192.168.1.103:554/stream
"""

from __future__ import annotations

import io
import os
import shutil
import re
import subprocess
import threading
import time
from datetime import datetime

import config

# cv2 is loaded lazily, exactly as main.py defers it and for the same reason:
# inputs_panel.py and topbar.py import hms() from this module, main.py imports
# inputs_panel at module scope, and cv2 is ~2s of import on a Pi 4 - every
# second of it spent before the splash can paint. Only the encoder thread needs
# it, and by the time that runs main.py has loaded the video stack anyway.
cv2 = None
np = None


def _load_cv2():
    global cv2, np
    if cv2 is None:
        import cv2 as _cv2
        import numpy as _np
        cv2 = _cv2
        np = _np

# inputs.py owns the vocabulary; importing it here keeps one definition of the
# three state strings rather than a second set that can drift.
from inputs import PAUSED, RECORDING, STOPPED

# A fourth state that no switch produces: the window after STOP in which the
# recording exists on disk but has not been claimed. It lives here rather than
# in inputs.py precisely because it is not a switch position - it is what the
# recorder is doing while it waits to be told whether the run was worth keeping.
PENDING = "SAVE?"


def _stamp(when=None):
    # YYYYMMDD_HHMMSS - the operator-specified convention for session names.
    return (when or datetime.now()).strftime("%Y%m%d_%H%M%S")


# Written by usb_backup.py at the end of a verified transfer, holding the unix
# time that transfer finished. See _next_session_no.
SESSION_RESET_MARKER = ".session_reset"


def _reset_epoch(root):
    """Unix time of the last verified USB backup, or 0.0 if there has not been one."""
    try:
        with io.open(os.path.join(root, SESSION_RESET_MARKER),
                     encoding="utf-8") as fh:
            return float(fh.read().strip().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def _session_started(name):
    """Unix time out of a session directory's own NAME, or None if it has none.

    The name rather than the mtime on purpose: clearing a backed-up session's
    files touches its directory, so its mtime moves to roughly when the backup
    ran and cannot be compared against the backup's own clock. The name is
    stamped once, by _start(), and only ever gains the end time.

    Format, operator's spec 2026-08-26:

        session01 date 26-08-26 start 20-07-56 end 20-15-30

    "/" and ":" are what the spec asked for and neither can be used: "/" is the
    path separator, and ":" is illegal on the FAT32/exFAT stick this footage is
    copied to - a directory named with one would be silently skipped or mangled
    by the transfer. "-" everywhere is the closest legal spelling.

    THE OLD FORMAT STILL PARSES. Sessions recorded before this change are named
    20260826_200756_SESSION001..., and they are still on the card and still owed
    a backup - a parser that could not date them would restart the numbering on
    top of them.
    """
    # Current: date DD-MM-YY start HH-MM-SS
    m = re.search(r"date (\d\d-\d\d-\d\d) start (\d\d-\d\d-\d\d)", name)
    if m:
        try:
            return time.mktime(time.strptime("%s %s" % (m.group(1), m.group(2)),
                                             "%d-%m-%y %H-%M-%S"))
        except ValueError:
            return None
    # Legacy: YYYYMMDD_HHMMSS leading the name
    try:
        return time.mktime(time.strptime(name[:15], "%Y%m%d_%H%M%S"))
    except (ValueError, TypeError):
        return None



def stamp_unfinished_sessions(root, idle_s=120.0, only=None):
    """Give an end time to any session folder that never got one.

    _finalize_dir_name() appends "end HH-MM-SS" when a run stops. If the ground
    station dies or is restarted mid-recording, that call never happens and the
    folder is left with only a start time - `session50 date 01-09-26 start
    17-10-58`. It is not damaged, but it reads as a run that never ended and it
    sorts oddly against its neighbours. Several were left behind by the restarts
    of 2026-09-01, which is what prompted this.

    The end time comes from the NEWEST FILE INSIDE, which is when the footage
    actually stopped growing - far closer to the truth than now(), and it stays
    correct however long the folder sat unstamped before anyone noticed.

    IDLE GUARD: a folder touched within idle_s is assumed to be recording and is
    left alone. This runs at the start of a new session, when nothing else should
    be live, but stamping a running session's directory would put "end" on a run
    still in progress - so the cheap check is worth having.
    """
    try:
        names = os.listdir(root)
    except OSError:
        return
    now = time.time()
    for name in names:
        # `only` restricts the sweep the same way the recovery pass does - the
        # boot path passes the folders that existed before the viewer started,
        # which is what lets it use idle_s=0 without risking a live run.
        if only is not None:
            wanted = {only} if isinstance(only, str) else set(only)
            if name not in wanted:
                continue
        if " end " in name or not re.search(r"session(\d+)", name, re.I):
            continue
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        try:
            newest = max((os.path.getmtime(os.path.join(path, f))
                          for f in os.listdir(path)), default=None)
        except OSError:
            continue
        if newest is None or now - newest < idle_s:
            continue
        try:
            os.rename(path, os.path.join(
                root, "%s end %s" % (name, time.strftime("%H-%M-%S",
                                                         time.localtime(newest)))))
        except OSError:
            # Cosmetic only - the footage is untouched and every path into it
            # still resolves. Never let a tidy-up cost a recording.
            pass


_PART_NAME_RE = re.compile(r"^(.+)_(\d{3})" + re.escape(config.RECORD_EXT) + r"$", re.I)


def recover_unfinished_sessions(root, idle_s=120.0, log=None, only=None):
    """Merge the per-clip parts of any session that never got finalised.

    WHY THIS EXISTS. Operator 2026-09-02: "i am off pi without stop recording,
    to video is not ready for transfer - so make this, when in between recording
    pi off, to recording save when pi off".

    A run writes one file per camera PER CLIP - cam1_front_001.mp4 and so on -
    and only joins them into 1_front.mp4 / 2_back.mp4 when the operator STOPS.
    Cut the power mid-recording and the join never happens, so the footage is all
    there but sits as numbered parts. usb_chooser.session_files() then refuses to
    offer the session, by design, because a folder holding parts is one whose
    merge has not finished - which is exactly the "not ready for transfer" the
    operator hit. Eight sessions were stranded that way when this was written.

    A HARD POWER CUT CANNOT BE CAUGHT, so this does not try. There is no signal,
    no unmount, no last-gasp handler - the 5V simply stops. The only thing that
    can be done is to finish the job on the way back up, which is what this is:
    run at startup, before anything else touches /recordings.

    STREAM COPY, never a re-encode. The parts already share a codec, a size and a
    frame rate because one camera wrote them all back to back, which is the
    concat demuxer's precondition. A copy runs at disk speed and cannot lose
    quality; an encode here would take longer than the recording did and would
    fight the cameras for the same hardware.

    THE PARTS ARE ONLY DELETED AFTER A VERIFIED JOIN - ffmpeg exited clean and
    the output is non-empty. If anything is off, the parts stay and the session
    is left un-transferable rather than silently truncated. Footage that exists
    in an awkward shape beats footage that does not exist.
    """
    def say(msg):
        if log:
            log(msg)

    if not _have_ffmpeg():
        say("recover: ffmpeg missing, leaving parts alone")
        return 0

    try:
        names = os.listdir(root)
    except OSError:
        return 0

    now = time.time()
    recovered = 0
    for name in sorted(names):
        # `only` narrows the sweep to named folders - a single name for the
        # shutdown path, or a whole set for the boot pass, which snapshots what
        # exists BEFORE the viewer can start recording and passes that. Either
        # way a session created after the snapshot is never touched, which is
        # what makes idle_s=0 safe there.
        if only is not None:
            wanted = {only} if isinstance(only, str) else set(only)
            if name not in wanted:
                continue
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        try:
            files = os.listdir(path)
        except OSError:
            continue

        groups = {}
        newest = 0.0
        for fn in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(path, fn)))
            except OSError:
                pass
            m = _PART_NAME_RE.match(fn)
            if m:
                groups.setdefault(m.group(1), []).append((int(m.group(2)), fn))
        if not groups:
            continue

        # LEAVE A LIVE SESSION ALONE. This runs at startup, when nothing should
        # be recording, but a folder still being written to must never have its
        # parts concatenated and deleted underneath the encoder.
        if now - newest < idle_s:
            say("recover: %s looks active, skipping" % name)
            continue

        for slug, parts in sorted(groups.items()):
            parts.sort()

            # SET ASIDE PARTS FFMPEG CANNOT OPEN, before trying to join them.
            #
            # A clip killed before its segment closed has no moov atom - the
            # index an MP4 writes only on close - so it holds frames and no way
            # to find them. ffprobe rejects it outright and the concat below
            # fails on the whole group because of one bad member.
            #
            # WHY RENAME RATHER THAN DELETE: the bytes are still the operator's
            # footage and a specialist tool can sometimes rebuild an index from a
            # good file of the same shape. Deleting forecloses that. Renaming
            # takes the file out of the _\d{3}.mp4 pattern, which is what
            # usb_chooser.session_files() reads as "this session is still being
            # worked on" - so a session whose parts are all dead stops reporting
            # itself as processing for ever, which is what it did before this.
            live = []
            for no, fn in parts:
                full = os.path.join(path, fn)
                try:
                    rc = subprocess.call(
                        ["ffprobe", "-v", "error", "-show_entries",
                         "format=duration", "-of", "default=nw=1:nk=1", full],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    rc = 1
                if rc == 0:
                    live.append((no, fn))
                else:
                    try:
                        os.rename(full, full + ".unreadable")
                        say("recover: %s/%s has no index, set aside" % (name, fn))
                    except OSError:
                        pass
            if not live:
                say("recover: %s/%s nothing readable to join" % (name, slug))
                continue
            parts = live
            # cam1_front -> 1_front, matching what _join_all() writes on a clean
            # stop, so a recovered session is indistinguishable from a normal one.
            m = re.search(r"cam(\d+)_(.+)$", slug, re.I)
            stem = ("%s_%s" % (m.group(1), m.group(2))) if m else slug
            out = os.path.join(path, stem + config.RECORD_EXT)
            if os.path.exists(out):
                continue

            lst = os.path.join(path, ".recover_%s.txt" % stem)
            try:
                with open(lst, "w", encoding="utf-8") as fh:
                    for _no, fn in parts:
                        full = os.path.join(path, fn)
                        fh.write("file '%s'\n" % full.replace("'", "'\\''"))
            except OSError as exc:
                say("recover: %s/%s list failed: %s" % (name, stem, exc))
                continue

            tmp = out + ".part"
            try:
                rc = subprocess.call(
                    # -f mp4 IS REQUIRED, because the output is written to
                    # <name>.mp4.part and ffmpeg guesses the muxer from the
                    # EXTENSION - ".part" matches nothing, so without this it
                    # exits 1 having written nothing and every recovery
                    # "failed" while the same command run by hand succeeded.
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                     "-nostdin", "-f", "concat", "-safe", "0", "-i", lst,
                     "-c", "copy", "-f", "mp4", tmp],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as exc:
                rc, _ = 1, say("recover: %s/%s ffmpeg failed: %s" % (name, stem, exc))

            ok = rc == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0
            if ok:
                try:
                    os.replace(tmp, out)
                    for _no, fn in parts:
                        os.remove(os.path.join(path, fn))
                    recovered += 1
                    say("recover: %s -> %s (%d parts)"
                        % (name, os.path.basename(out), len(parts)))
                except OSError as exc:
                    say("recover: %s/%s cleanup failed: %s" % (name, stem, exc))
            else:
                # Leave everything as it was. A failed join is recoverable by
                # hand; a deleted part is not.
                say("recover: %s/%s join failed, parts kept" % (name, stem))
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            try:
                os.remove(lst)
            except OSError:
                pass

    return recovered


def _dir_bytes(path):
    """Bytes used by one session folder. Missing files count as zero."""
    total = 0
    try:
        for fn in os.listdir(path):
            try:
                total += os.path.getsize(os.path.join(path, fn))
            except OSError:
                pass
    except OSError:
        pass
    return total


def prune_recordings(root, max_bytes, protect=None, log=None, max_per_pass=1):
    """Keep /recordings under max_bytes by deleting the OLDEST sessions first.

    Operator 2026-09-02: "when its above saved 20 gb to delete first recording -
    means when last save to first delete when storage full". A ring buffer: the
    newest recording is always the one kept, and age is what decides who goes.

    OLDEST BY THE NAME, NOT BY MTIME. Every folder carries "date DD-MM-YY start
    HH-MM-SS" and _session_started() parses it. mtime is the wrong clock here -
    it moves when a session is merged, stamped or backed up, so the folder
    touched most recently is often the OLDEST footage. Sorting on it would evict
    close to at random.

    THE LIVE SESSION IS NEVER A CANDIDATE. `protect` is the directory currently
    being recorded into; deleting it would pull files from under an open encoder.
    If everything else has already gone and the live run alone still exceeds the
    cap, this stops rather than touching it - a recording in progress outranks a
    limit, and the card still has the OS's spare room underneath.

    RETURNS the number of sessions removed. Deleting is permanent and there is no
    backup check: the operator asked for a ring buffer, and a ring buffer that
    declines to overwrite is just a full disk.
    """
    def say(msg):
        if log:
            log(msg)

    try:
        names = [d for d in os.listdir(root)
                 if os.path.isdir(os.path.join(root, d))]
    except OSError:
        return 0

    sizes = {n: _dir_bytes(os.path.join(root, n)) for n in names}
    total = sum(sizes.values())
    if total <= max_bytes:
        return 0

    protect_name = os.path.basename(protect.rstrip("/")) if protect else None

    def age(n):
        started = _session_started(n)
        if started is not None:
            return started
        try:
            return os.path.getmtime(os.path.join(root, n))
        except OSError:
            return 0.0

    order = sorted((n for n in names if n != protect_name), key=age)

    # ONE SESSION PER PASS. Operator 2026-09-02: "not directly three session
    # delete - delete one one session when required space ... and again same
    # check, if again used to again one session delete".
    #
    # The sweep runs every 30 seconds, so freeing one session, re-measuring, and
    # freeing another only if it is STILL over is the same end state reached in
    # steps. The difference matters: measuring once and then deleting everything
    # that arithmetic said would be needed can over-delete, because a merge
    # finishing between the measurement and the deletions changes the totals
    # underneath it. Deleting one and looking again cannot.
    #
    # It also bounds the damage of a wrong cap. Set max_bytes too low by mistake
    # and this removes one session per 30s - visible, and stoppable - instead of
    # emptying the card in a single sweep.
    removed = 0
    for n in order:
        if total <= max_bytes or removed >= max_per_pass:
            break
        path = os.path.join(root, n)
        freed = sizes.get(n, 0)
        try:
            shutil.rmtree(path)
        except OSError as exc:
            say("prune: could not remove %s: %s" % (n, exc))
            continue
        total -= freed
        removed += 1
        say("prune: removed %s (%.1f MB), now %.1f GB of %.1f GB"
            % (n, freed / 1e6, total / 1e9, max_bytes / 1e9))

    # WHOLE SESSIONS ONLY - the live run's own segments are never touched.
    # Operator 2026-09-02, on seeing a test that trimmed inside the live run:
    # "when session 7 is running live to delete session 1 2 3, not 4 5 6".
    #
    # A last-resort trim of the live session's oldest segments was written and
    # removed again on the same day. It only ever ran once every OTHER session
    # was already gone, but it made a recording in progress lose its own opening
    # minutes, and a run that is still going is not something to eat into. If the
    # cap cannot be met without doing that, the cap goes unmet and says so.
    #
    # WHAT PROTECTS THE CARD IN THAT CASE is the headroom under the cap: 20 GB
    # against 22.8 GB free, so even a single run past the limit has room before
    # the filesystem is in trouble. It takes 12h48m of continuous recording to
    # get there.
    # TWO DIFFERENT SITUATIONS, and they must not read the same. Being over the
    # cap with sessions still to go is NORMAL - one goes per pass, so the next
    # sweep continues. Being over with nothing left but the live run is the end
    # of the line. The single message this replaced claimed the second whenever
    # the first was true, which would have anyone reading the log believe the
    # card was down to one session while six sat there.
    if total > max_bytes:
        more = [n for n in order if os.path.isdir(os.path.join(root, n))]
        if more:
            say("prune: %.1f GB over a %.1f GB cap - %d session(s) still to go, "
                "one per pass" % (total / 1e9, max_bytes / 1e9, len(more)))
        else:
            say("prune: %.1f GB over a %.1f GB cap - only the live recording is "
                "left and it is not touched" % (total / 1e9, max_bytes / 1e9))
    return removed


def _next_session_no(root):
    """1 + the highest SESSIONnnn in `root`, counting only sessions newer than
    the last verified USB backup - so a stick that has taken the footage away
    resets the numbering to 001 (operator spec 2026-08-19).

    Scanned from disk on every start rather than counted in memory: the counter
    has to survive viewer restarts, and the directory listing IS the durable
    record of how many sessions exist. An unreadable root (first boot, no
    /recordings yet) starts at 1.

    Sessions older than the reset are SKIPPED, never deleted. One is only still
    on the card because usb_backup deliberately kept something - a file inside
    its 30s active grace, or a full view built after that run's scan - and that
    footage is still owed a backup. Skipping restarts the count without touching
    it. Nothing collides: the directory name carries a timestamp as well as the
    number, so a second SESSION001 is a different directory from the first, on
    the Pi and on the stick alike.
    """
    # Tidy up anything a previous run left unstamped, while the directory is
    # being listed anyway and before this session adds itself to it.
    #
    # idle_s=0 HERE ON PURPOSE. This runs as a new session is being numbered, so
    # the previous one stopped moments ago and nothing is recording - the guard
    # would only skip the folder we are here to stamp. That is exactly what
    # happened in the 2026-09-02 reboot test: session01 was merged correctly but
    # kept a start-only name, because it had been written to seconds earlier and
    # the 120s guard declined it.
    stamp_unfinished_sessions(root, idle_s=0.0)

    epoch = _reset_epoch(root)
    highest = 0
    try:
        for name in os.listdir(root):
            # Both spellings: "session01 date ..." since 2026-08-26, and the
            # older "..._SESSION001". Case-insensitive and unanchored, because
            # the number is no longer the last thing in the name.
            m = re.search(r"session(\d+)", name, re.IGNORECASE)
            if not m:
                continue
            if epoch:
                started = _session_started(name)
                if started is not None and started <= epoch:
                    continue
            highest = max(highest, int(m.group(1)))
    except OSError:
        pass
    return highest + 1


def hms(seconds):
    """Elapsed seconds as H:MM:SS / M:SS. Used by the panel and the top bar."""
    if seconds is None:
        return "--:--"
    seconds = int(seconds)
    h, m, s = seconds // 3600, (seconds // 60) % 60, seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def free_mb(path):
    """Free megabytes on the filesystem holding `path`, or None if unknown."""
    probe = path
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    try:
        return shutil.disk_usage(probe).free / (1024 * 1024)
    except OSError:
        return None


class CombinedView:
    """A pseudo-stream: every real camera side by side in ONE frame.

    Quacks exactly like RTSPStream where CameraRecorder is concerned - .name,
    .slug, .latest() -> (frame, seq) - so the recorder that encodes it is the
    same code that encodes a camera, and the full view inherits the wall-clock
    timeline, the disk-space guard and the error reporting for free.

    Composition happens INSIDE latest(), i.e. on the encoder thread at
    RECORD_FPS, not per decoded frame: two resizes to COMBINED_HEIGHT plus an
    hstack ~ a millisecond, and doing it 15x/s instead of 25x/s per camera is
    the cheap direction. The GUI never sees this object.

    A camera that has not delivered yet gets its LAST frame, then a black tile,
    so one dead camera cannot black out the other's half of the record - the
    same hold-the-last-frame honesty the per-camera files have.
    """

    def __init__(self, streams, height=None):
        self.streams = streams
        self.name = "FULL VIEW"
        self.slug = "full"
        self.height = height or config.COMBINED_HEIGHT
        self._last = [None] * len(streams)
        self._seq = 0

    def _tile(self, index, frame):
        h = self.height
        if frame is None:
            frame = self._last[index]
        if frame is None:
            # Nothing ever decoded: a black 16:9 placeholder keeps the canvas
            # geometry stable until the camera shows up.
            return np.zeros((h, h * 16 // 9, 3), dtype=np.uint8)
        self._last[index] = frame
        fh, fw = frame.shape[:2]
        tile = cv2.resize(frame, (max(2, int(round(fw * h / fh))) // 2 * 2, h),
                          interpolation=cv2.INTER_AREA)
        label = config.camera_label(index) or f"CAM {index + 1}"
        # Label burned into the pixels, not into metadata: the file gets copied
        # to USB and watched anywhere, so FRONT/BACK has to travel inside it.
        cv2.putText(tile, label, (8, 24), cv2.FONT_HERSHEY_SIMPLEX,
                    0.7, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(tile, label, (8, 24), cv2.FONT_HERSHEY_SIMPLEX,
                    0.7, (255, 255, 255), 1, cv2.LINE_AA)
        return tile

    def latest(self):
        frames, seq_sum = [], 0
        for stream in self.streams:
            frame, seq = stream.latest()
            frames.append(frame)
            seq_sum += seq or 0
        if all(f is None for f in frames) and all(f is None for f in self._last):
            return None, 0
        canvas = np.hstack([self._tile(i, f) for i, f in enumerate(frames)])
        # Monotonic even if a camera reconnects and its seq resets.
        self._seq = max(self._seq + 1, seq_sum)
        return canvas, self._seq


class CameraRecorder(threading.Thread):
    """Encodes one camera into one file per clip. Never raises at callers.

    The contract is the same as InputReader's: every failure becomes `error`
    text that the panel can show, because a recorder that takes the viewer down
    with it is worse than one that admits it is not recording.
    """

    def __init__(self, stream, slug):
        super().__init__(daemon=True)
        self.stream = stream
        self.slug = slug

        self._lock = threading.Lock()
        # NOT self._stop: threading.Thread already has a private _stop() that
        # Thread.join() calls to reap a finished thread, so an Event of that
        # name shadows it and join() dies with "'Event' object is not callable".
        # It only shows up at shutdown, which is the worst place to find it.
        self._stopping = threading.Event()
        self._writer = None
        self._size = None            # (w, h) the open writer was built for
        self._path = None
        self._pending = None         # next clip path, picked up by the thread
        self._close = False          # asked to finish the open clip
        self._next_path = None       # thread-local: where the open clip goes

        # Published, read under the lock by status().
        self._frames = 0
        self._clip_frames = 0
        self._error = None
        self._rolling = False
        # True while this camera has gone quiet and the writer is holding
        # off. NOT an error: the clip is open, the file is fine, and the
        # moment frames come back it carries on into the same file.
        self._stalled = False
        # Monotonic instant this clip's FIRST frame was written, or None if it
        # has not written one yet. The full view is built from the finished
        # files (see FullViewBuilder) and those files do not all start at the
        # same moment: a camera that connects late produces a shorter file that
        # begins later, because the writer opens on the first real frame rather
        # than at the roll. Measured on SESSION009 that was 143 frames against
        # 128 - a full second of skew, which hstacked blind would show the
        # operator two different instants side by side and call it one frame.
        # Publishing the instant lets the session turn it into a lead-in pad.
        self._clip_first_write = None
        # The burned-in timestamp, built on the first frame (it is sized from
        # the frame) and rebuilt if the frame height ever changes. See
        # TimestampOsd and config.RECORD_TIMESTAMP.
        self._osd = None

    # -- public ---------------------------------------------------------------

    def begin_clip(self, path):
        """Finish whatever is open and start writing `path`."""
        with self._lock:
            self._pending = path
            self._close = True

    def end_clip(self):
        """Finish the open clip and write nothing until begin_clip() again."""
        with self._lock:
            self._pending = None
            self._close = True

    def set_rolling(self, rolling):
        """True only while the session is RECORDING - PAUSE writes nothing."""
        with self._lock:
            self._rolling = bool(rolling)

    def idle(self):
        """True when no file is open, so its clips are safe to move or delete."""
        with self._lock:
            return self._writer is None and not self._close

    def status(self):
        with self._lock:
            return {
                "slug": self.slug,
                "path": self._path,
                "frames": self._frames,
                "clip_frames": self._clip_frames,
                "error": self._error,
                "bytes": self._bytes(self._path),
                "clip_first_write": self._clip_first_write,
                "stalled": self._stalled,
            }

    def stop(self):
        self._stopping.set()

    # -- internals ------------------------------------------------------------

    @staticmethod
    def _bytes(path):
        try:
            return os.path.getsize(path) if path else 0
        except OSError:
            return 0

    @staticmethod
    def _square(frame):
        """Centre-crop to config.RECORD_SQUARE_PX square. See that setting.

        Crop first, scale only if the square asked for is not the one the frame
        can give: at the default 720 both cameras crop exactly and never resize,
        so what lands on disk is native pixels, not resampled ones.
        """
        px = config.RECORD_SQUARE_PX
        if not px:
            return frame
        h, w = frame.shape[:2]
        side = min(w, h)
        if side <= 0:
            return frame
        x, y = (w - side) // 2, (h - side) // 2
        frame = frame[y:y + side, x:x + side]
        if side != px:
            frame = cv2.resize(frame, (px, px), interpolation=cv2.INTER_AREA)
        return frame

    def _target_size(self, frame):
        h, w = frame.shape[:2]
        limit = config.RECORD_MAX_WIDTH
        if limit and w > limit:
            scale = limit / float(w)
            # Both dimensions forced even: most encoders reject odd sizes, and
            # cv2.VideoWriter reports that by quietly not opening.
            w, h = int(w * scale) // 2 * 2, int(h * scale) // 2 * 2
        return w, h

    def _open_writer(self, path, frame):
        size = self._target_size(frame)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fourcc = cv2.VideoWriter_fourcc(*config.RECORD_FOURCC)
        writer = cv2.VideoWriter(path, fourcc, config.RECORD_FPS, size)
        if not writer.isOpened():
            writer.release()
            # The usual cause is a fourcc this OpenCV build cannot encode
            # (avc1 without libx264), and OpenCV only warns on stderr - which
            # goes nowhere under the .pyw launcher. Say it where it will be read.
            with self._lock:
                self._error = f"cannot encode {config.RECORD_FOURCC} -> {path}"
            return None, None
        return writer, size

    def _release(self):
        if self._writer is not None:
            self._writer.release()
        self._writer, self._size = None, None

    def _stamped(self, frame):
        """`frame` with the OSD timestamp, on a COPY - the original is shared.

        Never lets a caption cost footage: any failure in the renderer hands
        the clean frame straight back to the encoder.
        """
        h = frame.shape[0]
        if self._osd is None or self._osd.for_height != h:
            try:
                self._osd = TimestampOsd(h, config.RECORD_TIMESTAMP_ALPHA)
            except Exception:
                self._osd = None
                return frame
        try:
            return self._osd.stamp(frame)
        except Exception:
            return frame

    def _write(self, frame):
        w, h = self._size
        if (frame.shape[1], frame.shape[0]) != (w, h):
            # The camera can come back at a different resolution after a
            # reconnect. Resizing keeps one continuous clip; a new writer
            # mid-clip would truncate the file the operator is watching grow.
            frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
        self._writer.write(frame)

    def _pin(self):
        """Confine THIS thread to the recording cores - see config.record_cores.

        os.sched_setaffinity(0, ...) applies to the CALLING THREAD on Linux, not
        to the whole process, which is what makes it usable from inside one
        thread of a program whose other threads must stay free to roam.

        Best effort by design: any failure leaves the thread scheduled normally,
        which is exactly how it behaved before this existed. Encoding on the
        wrong core is a performance question; refusing to encode is a lost
        recording.
        """
        try:
            cores = config.record_cores()
            if cores:
                os.sched_setaffinity(0, cores)
        except Exception:
            pass
        # AND yield to anything more urgent on those cores - see
        # config.RECORD_THREAD_NICE. os.nice() applies to the calling THREAD on
        # Linux, the same way sched_setaffinity does, so this lowers the encoder
        # without touching the UI or the drive loop.
        try:
            if config.RECORD_THREAD_NICE:
                os.nice(config.RECORD_THREAD_NICE)
        except Exception:
            pass

    def run(self):
        self._pin()
        _load_cv2()
        period = 1.0 / max(1.0, config.RECORD_FPS)
        next_tick = time.monotonic()
        last_frame = None
        checked_disk = 0.0
        last_seq = None            # stream.latest() bumps this per decode
        last_fresh = None          # when a NEW frame last arrived

        try:
            while not self._stopping.is_set():
                next_tick += period
                now = time.monotonic()
                # A long stall (a reconnect storm, a busy Pi) must not turn into
                # a burst of catch-up writes that all carry the same frame.
                if next_tick < now:
                    next_tick = now
                self._stopping.wait(max(0.0, next_tick - now))
                if self._stopping.is_set():
                    break

                with self._lock:
                    rolling, close_now, pending = (
                        self._rolling, self._close, self._pending)
                    self._close = False
                    if pending is not None:
                        self._pending = None

                if close_now:
                    # end_clip() sends pending=None, so this clears the target
                    # as well as closing the file. Leaving the old path behind
                    # would have the next roll reopen - and truncate - the clip
                    # that was just saved.
                    self._release()
                    self._next_path = pending
                    with self._lock:
                        self._clip_frames = 0
                        self._path = pending
                        self._clip_first_write = None

                if not rolling or self._next_path is None:
                    continue
                path = self._next_path

                frame, seq = self.stream.latest()
                fresh = seq != last_seq
                if fresh:
                    last_seq, last_fresh = seq, now
                if frame is not None:
                    # Before last_frame is remembered, so a dropout holds the
                    # cropped frame and the clip never changes size mid-file.
                    frame = self._square(frame)
                if frame is None:
                    # Hold the last good frame so a hiccup shorter than the
                    # grace period below does not chop the video. Before the
                    # FIRST frame there is nothing to hold, so the file simply
                    # starts when video does.
                    frame = last_frame
                if frame is None:
                    with self._lock:
                        self._error = "waiting for video"
                    continue
                last_frame = frame

                # THE CAMERA IS GONE: hold off rather than record a still.
                # The writer is deliberately left OPEN - closing it would end
                # the clip, and the operator asked for the recording to carry
                # on in the SAME file when the picture comes back. See
                # config.RECORD_STALL_PAUSE_S for the trade this makes.
                grace = config.RECORD_STALL_PAUSE_S
                if (grace > 0 and last_fresh is not None
                        and now - last_fresh > grace):
                    with self._lock:
                        self._stalled = True
                    continue
                if self._stalled:
                    with self._lock:
                        self._stalled = False

                if self._writer is None:
                    writer, size = self._open_writer(path, frame)
                    if writer is None:
                        # Do not spin retrying a broken encoder every tick.
                        self._stopping.wait(1.0)
                        continue
                    self._writer, self._size = writer, size
                    with self._lock:
                        self._error = None
                        # The writer opens on the first frame it will write, so
                        # this is that frame's instant - see _clip_first_write.
                        if self._clip_first_write is None:
                            self._clip_first_write = time.monotonic()

                # Checked here rather than by the session, because this is the
                # thread that is about to make the file bigger.
                if now - checked_disk > 5.0:
                    checked_disk = now
                    free = free_mb(path)
                    if free is not None and free < config.RECORD_MIN_FREE_MB:
                        self._release()
                        with self._lock:
                            self._error = f"disk full - {free:.0f} MB free"
                        self._stopping.wait(5.0)
                        continue

                if config.RECORD_TIMESTAMP:
                    frame = self._stamped(frame)

                try:
                    self._write(frame)
                except Exception as exc:            # encoder blew up mid-clip
                    with self._lock:
                        self._error = f"write failed: {exc}"
                    self._release()
                    continue

                with self._lock:
                    self._frames += 1
                    self._clip_frames += 1
        finally:
            self._release()


def _have_ffmpeg():
    """True if both ffmpeg and ffprobe are on PATH. Cached after the first look."""
    global _FFMPEG_OK
    if _FFMPEG_OK is None:
        _FFMPEG_OK = bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))
    return _FFMPEG_OK


_FFMPEG_OK = None

# A font for the burned-in FRONT/BACK labels. DejaVu ships with Raspberry Pi OS;
# if it is ever missing the build still runs, just without labels, because an
# unlabelled full view is far better than no full view.
_FONT = next((f for f in (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
) if os.path.exists(f)), "")

# A MONOSPACED face for the recorder timestamp - see TimestampOsd. Fixed-width
# digits are what stop the stamp jittering as the seconds tick over. Bold, so
# it reads at the small size a corner caption has to be.
_MONO_FONT = next((f for f in (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansMono-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
) if os.path.exists(f)), "")


class TimestampOsd:
    """Burns "2026-09-12 13:42:18" into the top-right of a recorded frame.

    The look the operator asked for (2026-09-12): small, white, slightly
    transparent, monospaced, camera-recorder OSD, fixed to the frame. A thin
    dark halo under the glyphs keeps it legible over a bright duct wall
    without turning it into a box.

    HOW IT STAYS CHEAP. The text changes once a second, so the glyphs are
    rendered once a second into a fixed-size strip whose alpha is kept as
    float; every frame then pays one blend over that strip (~250x28 px on a
    720 frame) and one frame copy. The strip's size and the ink position
    inside it are measured ONCE at construction from reference renders, so
    the stamp sits in exactly the same pixels from one second to the next.

    Backend: OpenCV's freetype module with _MONO_FONT when both exist (the
    Pi), else the built-in Hershey font. Hershey is not quite monospaced, so
    the strip carries a little slack on the right for it.
    """

    def __init__(self, frame_h, alpha):
        self.for_height = frame_h
        self.alpha = max(0.0, min(1.0, float(alpha)))
        self.px = max(12, frame_h // 32)            # 22 px glyphs on a 720 frame
        self.margin = max(6, frame_h // 45)         # 16 px in from the edges
        self.pad = 3
        self._ft = None
        if _MONO_FONT and hasattr(cv2, "freetype"):
            try:
                ft = cv2.freetype.createFreeType2()
                ft.loadFontData(_MONO_FONT, 0)
                self._ft = ft
            except Exception:
                self._ft = None
        self.backend = "freetype" if self._ft is not None else "hershey"
        # A scratch canvas big enough for any timestamp at this size; the ink
        # lands in the same place on every render because the origin is fixed.
        self._org = (self.px, 2 * self.px)
        self._canvas_hw = (4 * self.px, 28 * self.px)
        self._crop = self._measure()
        self._text = None
        self._a_txt = None
        self._a_halo = None

    # -- rendering ------------------------------------------------------------

    def _draw(self, text):
        """The glyph mask for `text` on the scratch canvas, uint8 0..255."""
        h, w = self._canvas_hw
        canvas = np.zeros((h, w, 3), np.uint8)
        if self._ft is not None:
            self._ft.putText(canvas, text, self._org, self.px,
                             (255, 255, 255), -1, cv2.LINE_AA, False)
        else:
            # Hershey's cap height is ~22 px at scale 1; org is the baseline.
            scale = self.px / 22.0
            thick = max(1, int(round(self.px / 11.0)))
            cv2.putText(canvas, text, (self._org[0], self._org[1] + self.px),
                        cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255),
                        thick, cv2.LINE_AA)
        return canvas[:, :, 0]

    @staticmethod
    def _bbox(mask):
        ys, xs = np.where(mask > 0)
        if len(ys) == 0:
            return None
        return int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1

    def _measure(self):
        """(y0, y1, x0, x1) of the strip on the scratch canvas.

        Height from every glyph the stamp can contain, so a '-' sitting low
        or a ':' sitting high never changes the strip; width from a real
        timestamp, which is the same length every second.
        """
        tall = self._bbox(self._draw("0123456789-:"))
        wide = self._bbox(self._draw(
            datetime.now().strftime(config.RECORD_TIMESTAMP_FORMAT)))
        if tall is None or wide is None:
            raise RuntimeError("timestamp font rendered nothing")
        slack = 0 if self._ft is not None else self.px // 2
        y0 = max(0, tall[0] - self.pad)
        y1 = min(self._canvas_hw[0], tall[1] + self.pad)
        x0 = max(0, wide[2] - self.pad)
        x1 = min(self._canvas_hw[1], wide[3] + self.pad + slack)
        return y0, y1, x0, x1

    def _compose(self, text):
        y0, y1, x0, x1 = self._crop
        mask = self._draw(text)[y0:y1, x0:x1]
        halo = cv2.dilate(mask, np.ones((3, 3), np.uint8))
        self._a_txt = (mask.astype(np.float32) / 255.0 * self.alpha)[..., None]
        self._a_halo = (halo.astype(np.float32) / 255.0
                        * self.alpha * 0.75)[..., None]
        self._text = text

    # -- per frame --------------------------------------------------------------

    def stamp(self, frame):
        """A copy of `frame` with the current time in its top-right corner."""
        text = datetime.now().strftime(config.RECORD_TIMESTAMP_FORMAT)
        if text != self._text:
            self._compose(text)
        a_txt, a_halo = self._a_txt, self._a_halo
        sh, sw = a_txt.shape[:2]
        fh, fw = frame.shape[:2]
        if sh + self.margin > fh or sw + self.margin > fw:
            return frame                      # frame too small for a caption
        y0, x0 = self.margin, fw - self.margin - sw
        out = frame.copy()
        roi = out[y0:y0 + sh, x0:x0 + sw].astype(np.float32)
        roi *= (1.0 - a_halo)                             # dark halo first
        roi = roi * (1.0 - a_txt) + 255.0 * a_txt         # then white glyphs
        out[y0:y0 + sh, x0:x0 + sw] = roi.astype(np.uint8)
        return out


class FullViewBuilder(threading.Thread):
    """Builds full_nnn.mp4 - both cameras side by side - AFTER a session is kept.

    This used to be a third live encoder (CombinedView, still below and still
    reachable via RECORD_COMBINED=1). It moved off the recording path because the
    operator asked for the full view at the resolution the cameras actually
    stream, split 50/50, and on this Pi 4 that is not available live at any
    setting - see config.COMBINED_AFTER_SAVE for the measurements.

    Built from the finished per-camera files it is free of all that: the cameras
    are already at native size on disk, all four cores are idle once recording
    has stopped, and a run the operator discarded costs nothing at all because
    this never starts for one.

    GEOMETRY - the fix for the 76/24 split. Each camera gets a half exactly as
    wide as the WIDEST camera and as tall as the TALLEST, and sits centred inside
    it on black. Nothing is scaled, so nothing is softened; nothing is stretched,
    so nothing is distorted; and because both halves are the same box by
    construction the split cannot drift with aspect ratio the way the old
    per-tile sizing did. CAM 1 at 1280x720 beside CAM 2 rotated to 720x1280
    gives a 2560x1280 canvas, 1280 to each.

    ALIGNMENT. The per-camera files do not all start at the same instant - each
    writer opens on its own camera's first frame - so every input is padded at
    the head by its own start skew (CameraRecorder._clip_first_write). Without
    that, SESSION009's measured 1s skew would have put two different moments side
    by side and called it one frame.

    Failure is always non-destructive: the per-camera masters are the record and
    this only ever adds a file. A clip that cannot be built is reported and
    skipped, and the rest of the session still builds.
    """

    def __init__(self, session_dir, clips, offsets, labels, on_done=None,
                 join=True):
        super().__init__(daemon=True)
        # join=False is the WHILE-RECORDING pass - see SessionRecorder._prep_clip.
        # It normalises a clip that has just been closed and stops there, because
        # joining is a whole-session operation and the session is still running.
        # The stop-time pass then finds that clip already H.264 and skips it, so
        # the same work is never paid for twice.
        #
        # NAMED _do_join, NOT _join: _join is already a METHOD on this class (the
        # ffmpeg concat that produces front.mp4 and back.mp4). An attribute of
        # that name would shadow it and the session would finish with numbered
        # fragments and no joined output at all.
        self._do_join = join
        self.session_dir = session_dir
        self.clips = clips              # {clip_no: [(slug, path), ...]}
        # Slug -> FRONT / BACK, used to name the joined outputs. Already passed
        # in for the burned-in tile captions; _join_all reuses it so the files
        # on the stick read front.mp4 and back.mp4.
        self.offsets = offsets          # {clip_no: {slug: lead_in_seconds}}
        self.labels = labels            # {slug: "FRONT"}
        self._on_done = on_done
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._proc = None
        # Published for the strip.
        # WHAT THIS JOB WILL ACTUALLY DO, counted in equal steps, so overall
        # progress can be a real 0..1 instead of something the UI infers.
        #
        # The UI used to guess it from the stage NAME, dividing by a fixed list
        # of three - normalising, building, joining. Two of those usually do not
        # run: COMBINED_AFTER_SAVE is off, so "building" is skipped entirely, and
        # a while-recording pass skips "joining" too because joining is a
        # whole-session operation. A dial fed that guess could only ever fill the
        # fraction of stages that happened to execute, which is why it stopped
        # partway and sat there looking wedged.
        #
        # Only this object knows which stages it will run, so it is the only
        # thing that can count them.
        steps = 0
        if config.RECORD_NORMALIZE:
            steps += len(clips)
        if config.COMBINED_AFTER_SAVE:
            steps += len(clips)
        # The join, when this pass does one, is one more step - added in
        # _overall() rather than here because `join` is stored after this runs.
        self._steps_total = steps
        self._steps_done = 0
        self._state = "queued"          # queued / building / done / error
        self._clip = 0
        self._total = len(clips)
        self._built = 0
        self._frac = 0.0
        self._error = None

    # -- published ------------------------------------------------------------

    def _overall(self):
        """0..1 across the WHOLE job. Caller holds _lock."""
        joining = self._do_join and config.RECORD_JOIN_CLIPS
        total = self._steps_total + (1 if joining else 0)
        if total <= 0:
            return 1.0 if self._state == "done" else 0.0
        done = self._steps_done + max(0.0, min(1.0, self._frac))
        return max(0.0, min(1.0, done / float(total)))

    def status(self):
        with self._lock:
            return {
                "overall": self._overall(),
                "state": self._state,
                "clip": self._clip,
                "clips_total": self._total,
                "built": self._built,
                "frac": self._frac,
                "error": self._error,
            }

    def stop(self):
        self._stopping.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    # -- internals ------------------------------------------------------------

    @staticmethod
    def _probe(path):
        """(width, height, frames) for one file, or None if it is not readable."""
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,nb_frames",
                 "-of", "csv=p=0", path],
                capture_output=True, text=True, timeout=20).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        parts = out.split(",")
        if len(parts) < 2:
            return None
        try:
            w, h = int(parts[0]), int(parts[1])
        except ValueError:
            return None
        try:
            frames = int(parts[2])
        except (IndexError, ValueError):
            frames = 0
        return w, h, frames

    def _geometry(self, probes):
        """(half_w, canvas_h): the widest camera by the tallest, both even."""
        half_w = max(p[0] for p in probes)
        canvas_h = max(p[1] for p in probes)
        cap = config.COMBINED_MAX_HALF
        if cap and half_w > cap:
            # Scale the whole canvas, not one tile - the 50/50 has to survive.
            canvas_h = int(canvas_h * cap / float(half_w))
            half_w = cap
        # Even both ways: yuv420p subsamples chroma 2x2 and libx264 refuses odd
        # dimensions outright.
        return max(2, half_w // 2 * 2), max(2, canvas_h // 2 * 2)

    def _filter(self, slugs, offsets, half_w, canvas_h):
        chains, tiles = [], []
        for i, slug in enumerate(slugs):
            lead = max(0.0, offsets.get(slug, 0.0))
            label = (self.labels.get(slug) or slug).upper()
            chain = "[%d:v]" % i
            if lead > 0.001:
                # Black lead-in so every input shares one t=0. start_mode=add
                # prepends real frames rather than shifting timestamps, which is
                # what hstack's frame pairing actually reads.
                chain += ("tpad=start_duration=%.3f:start_mode=add:color=black,"
                          % lead)
            chain += ("scale=%d:%d:force_original_aspect_ratio=decrease,"
                      "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:black"
                      % (half_w, canvas_h, half_w, canvas_h))
            if _FONT:
                # Burned into the pixels on purpose: this file gets copied to a
                # stick and watched on someone else's laptop, so FRONT/BACK has
                # to travel inside the video, not in metadata.
                chain += (",drawtext=fontfile=%s:text=%s:x=16:y=14:"
                          "fontsize=%d:fontcolor=white:borderw=3"
                          ":bordercolor=black"
                          % (_FONT, label, max(18, canvas_h // 32)))
            chain += "[t%d]" % i
            chains.append(chain)
            tiles.append("[t%d]" % i)
        chains.append("%shstack=inputs=%d[v]" % ("".join(tiles), len(slugs)))
        return ";".join(chains)

    def _normalize_clip(self, members):
        """Re-encode one clip's per-camera masters to a common size. Problems list.

        Never fatal and never destructive: a master that cannot be re-encoded is
        left byte-for-byte as it was. The per-camera file is the record, while
        shrinking it and matching it to its sibling are both conveniences -
        neither is worth risking the only copy of a duct run for.
        """
        if not config.RECORD_NORMALIZE:
            return []
        usable = []
        for slug, path in members:
            try:
                if os.path.getsize(path) <= 0:
                    continue
            except OSError:
                continue
            probe = self._probe(path)
            if probe and probe[2] > 0:
                usable.append((slug, path, probe[2]))
        if not usable:
            return []
        # The shortest camera sets the length for every camera in the clip: a
        # fixed bitrate only produces an equal SIZE across an equal duration.
        # config.RECORD_NORM_MATCH_FRAMES has why they differ in the first place.
        target = 0
        if config.RECORD_NORM_MATCH_FRAMES and len(usable) > 1:
            target = min(frames for _s, _p, frames in usable)
        problems = []
        for i, (slug, path, _frames) in enumerate(usable):
            if self._stopping.is_set():
                break
            reason = self._normalize_one(path, target)
            if reason:
                problems.append("%s: %s" % (slug, reason))
            # COUNTED AFTER THE WORK, NOT BEFORE. This read
            # `frac = i / len(usable)` at the TOP of the loop, so with the rig's
            # two cameras it reported 0.0 and then 0.5 and the loop ended - the
            # dial stopped dead on 50% and sat there looking wedged, which is
            # exactly what the operator filmed. Progress means work FINISHED.
            with self._lock:
                self._frac = (i + 1) / float(len(usable))
        return problems

    @staticmethod
    def _codec_of(path):
        """Video codec name for one file, or None if it cannot be read."""
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of",
                 "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, timeout=20)
            return (out.stdout or "").strip() or None
        except Exception:
            return None

    def _normalize_one(self, path, frames):
        """One master -> constant-bitrate H.264 of `frames` frames, in place.

        Returns None on success, else a reason. The master is only ever replaced
        by a file that has been probed and found readable.
        """
        # ALREADY DONE? The recorder writes MPEG-4 while filming because that is
        # the only codec cv2 can encode fast enough to keep up - measured
        # 2026-08-31 at 2.6s per 60 frames against 49s for H.264, an 18x gap that
        # would stall a live recording outright. So "still mpeg4" means this
        # master has not been through here yet, and "h264" means it has.
        #
        # NOT AN OPTIMISATION - it is what makes the while-recording pass safe.
        # Without it the stop-time pass would re-encode every clip the recording
        # pass already did, and a session would take LONGER than before rather
        # than finishing in seconds.
        if self._codec_of(path) == "h264":
            return None

        rate = config.RECORD_NORM_BITRATE
        head, tail = os.path.split(path)
        # Leading dot: usb_backup skips dotfiles, so a stick plugged in while
        # this is running cannot copy a half-written master onto itself.
        tmp = os.path.join(head, "." + tail + ".norm")
        # THE WHILE-RECORDING PASS RUNS AT THE BACK OF THE QUEUE. Operator,
        # 2026-08-31: "its very laging so its make lag datas send in bot". It
        # was, and this is why.
        #
        # main.py already sits near 245% on a 4-core Pi while filming - two
        # camera decodes plus two live encodes - so there is about one core of
        # headroom and no more. Normalising a clip in that gap took the load
        # average to 10, and at 2.5x oversubscription everything queues: the UI
        # paint, the 50 Hz joystick frame, the ACK the Uno is waiting for. A
        # video encoder was competing with the drive loop on equal terms.
        #
        # nice 19 and one thread fixes that without giving up the feature. This
        # work is OPPORTUNISTIC - the whole point is that it happens in time the
        # machine is not otherwise using - so it should run only when nothing
        # else wants the core, and it takes longer in exchange. It still finishes
        # far inside the roll interval: at 4x realtime a 120s clip needs ~30s of
        # CPU, and even a quarter of a core clears it in time.
        #
        # The STOP-time pass is left at normal priority deliberately. Nothing is
        # being driven then, the operator is waiting on it, and it should have
        # the machine.
        # THE WHILE-RECORDING PASS RUNS IN THE IDLE CLASS, NOT MERELY NICE.
        #
        # Operator, 2026-09-01: "its lag when video saving process start, video
        # save is finished to its not lagy". Exactly the window this ffmpeg runs
        # in, so nice 19 and an affinity mask were not enough - and the reason is
        # what nice actually means. A nice 19 task is still a NORMAL task: it
        # gets a small share of every core it sits on even while something else
        # wants that core, and a small share of a Pi core is enough to make a
        # 30 Hz repaint miss its slot.
        #
        # SCHED_IDLE is a class BELOW every normal task. It cannot take time from
        # the UI at all; it fills the gaps the UI leaves. That is precisely the
        # bargain this work wants - the encoder runs at about 5x realtime and
        # needs roughly a fifth of the interval, so it can afford to wait for
        # scraps and still finish long before the next clip closes.
        #
        # ionice -c 3 is the same idea for the disk: idle-class I/O, so writing
        # the normalised file cannot hold up a frame being written by the live
        # recorder next to it.
        #
        # THE OPERATOR ASKED WHETHER SHORTER CLIPS WOULD HELP. They would not:
        # the same total work would arrive in more frequent, shorter bursts, so
        # the lag would be chopped up rather than removed - and interrupted every
        # 30s instead of every 60s is worse to drive, not better. The fix has to
        # be that the work yields, not that it is sliced differently.
        #
        # The STOP-time pass keeps normal priority: nothing is being driven then
        # and the operator is waiting on it, so it should have the machine.
        cores = config.record_cores()
        pin = ["taskset", "-c", ",".join(str(c) for c in sorted(cores))] if cores else []
        if self._do_join:
            nice = []
        else:
            nice = (pin + ["chrt", "--idle", "0"]
                    + ["ionice", "-c", "3"]
                    + ["nice", "-n", "19"])
        threads = [] if self._do_join else ["-threads", "1"]
        cmd = nice + ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-nostdin"] + threads + [
               "-i", path,
               "-c:v", config.RECORD_NORM_VCODEC,
               "-b:v", rate, "-minrate", rate, "-maxrate", rate, "-bufsize", rate,
               "-r", "%g" % config.RECORD_FPS,
               "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        if config.RECORD_NORM_VCODEC == "libx264":
            # x264 SPELLINGS ONLY. The v4l2m2m wrapper rejects every one of
            # these and the whole encode fails, so they are not passed to it.
            #
            # nal-hrd=cbr is what actually pins the size - without it x264 reads
            # the bitrate as a ceiling and undershoots on whichever camera is
            # looking at the emptier scene, which is the file-size difference
            # this normalising exists to remove. force-cfr keeps one frame per
            # tick so -frames:v means the same thing on both cameras. The
            # hardware encoder is CBR by nature and needs neither.
            cmd += ["-preset", config.RECORD_NORM_PRESET,
                    "-profile:v", config.RECORD_NORM_PROFILE,
                    "-level", config.RECORD_NORM_LEVEL,
                    "-x264-params", "nal-hrd=cbr:force-cfr=1"]
        if frames > 0:
            cmd += ["-frames:v", str(frames)]
        # -f mp4 explicitly: the muxer is normally picked from the extension and
        # the extension here is .norm.
        cmd += ["-f", "mp4", tmp]

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, text=True)
        except OSError as exc:
            return "ffmpeg: %s" % exc
        with self._lock:
            self._proc = proc
        err = ""
        try:
            _out, err = proc.communicate(timeout=config.RECORD_NORM_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            err = "timed out after %.0fs" % config.RECORD_NORM_TIMEOUT_S
        finally:
            with self._lock:
                self._proc = None

        if proc.returncode != 0:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            lines = [ln for ln in (err or "").strip().splitlines() if ln.strip()]
            return lines[-1][:120] if lines else "ffmpeg exit %d" % proc.returncode

        # Probed before it is allowed to overwrite anything: a zero-byte or
        # headerless .norm replacing a good master would turn a cosmetic step
        # into the one thing this must never do.
        probe = self._probe(tmp)
        if not probe or probe[2] <= 0:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return "re-encode produced an unreadable file"
        try:
            os.replace(tmp, path)
        except OSError as exc:
            return "replace failed: %s" % exc
        return None

    def _build_clip(self, clip_no, members):
        """One clip -> one full_nnn.mp4. Returns None on success, else a reason."""
        usable = []
        for slug, path in members:
            try:
                if os.path.getsize(path) <= 0:
                    continue
            except OSError:
                continue
            probe = self._probe(path)
            if probe:
                usable.append((slug, path, probe))
        if len(usable) < 2:
            # One camera is not a side-by-side. Not an error - a dead camera is a
            # normal state on this rig, and the master file still exists.
            return None

        slugs = [u[0] for u in usable]
        paths = [u[1] for u in usable]
        probes = [u[2] for u in usable]
        offsets = self.offsets.get(clip_no, {})
        half_w, canvas_h = self._geometry(probes)
        out = os.path.join(
            self.session_dir, "full_%03d%s" % (clip_no, config.RECORD_EXT))
        # Built under a .part name and renamed on success. The USB daemon scans
        # this directory on its own two-second clock and will happily copy a file
        # that is still growing; under the final name that would put a truncated
        # full view on the stick and, worse, one whose size then matches on the
        # next insertion. os.replace is atomic on the same filesystem, so the
        # daemon only ever sees no file or a finished one.
        part = out + ".part"

        # Longest padded input, so the progress fraction means something.
        fps = max(1.0, config.RECORD_FPS)
        expect = max(int(p[2] + max(0.0, offsets.get(sl, 0.0)) * fps)
                     for sl, p in zip(slugs, probes))

        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin"]
        for path in paths:
            cmd += ["-i", path]
        cmd += ["-filter_complex",
                self._filter(slugs, offsets, half_w, canvas_h),
                "-map", "[v]", "-r", "%g" % config.RECORD_FPS,
                "-c:v", config.COMBINED_VCODEC]
        if config.COMBINED_VCODEC == "libx264":
            cmd += ["-preset", config.COMBINED_PRESET,
                    "-crf", str(config.COMBINED_CRF),
                    # See config.COMBINED_PROFILE: the full view is the file that
                    # already travels, and this is what stops it quietly ceasing
                    # to if the preset above is ever retuned.
                    "-profile:v", config.COMBINED_PROFILE,
                    "-level", config.COMBINED_LEVEL,
                    # Anything that opens an mp4 can open this one.
                    "-movflags", "+faststart"]
        else:
            # NOT libx264: -crf is an x264 idea and this encoder ignores it,
            # falling back to ffmpeg's 200 kbps default - which looks exactly
            # like a broken camera. An explicit bitrate is not optional here.
            cmd += ["-b:v", config.COMBINED_BITRATE,
                    "-movflags", "+faststart"]
        # -f mp4 explicitly: the muxer is normally picked from the extension
        # and the extension here is .part.
        cmd += ["-pix_fmt", "yuv420p", "-progress", "pipe:1", "-nostats",
                "-f", "mp4", part]

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
        except OSError as exc:
            return "ffmpeg: %s" % exc
        with self._lock:
            self._proc = proc

        deadline = time.monotonic() + config.COMBINED_TIMEOUT_S
        # Both aborts used to `return` from inside this loop, which skipped the
        # cleanup below and left full_nnn.mp4.part on the card for good. A
        # stranded .part is not cosmetic: usb_backup reads those temporaries as
        # "the recorder is still working" and would hold the stick waiting for a
        # build that died minutes ago.
        abort = None
        try:
            for line in proc.stdout:
                if self._stopping.is_set():
                    proc.kill()
                    abort = "cancelled"
                    break
                if time.monotonic() > deadline:
                    proc.kill()
                    abort = "timed out after %.0fs" % config.COMBINED_TIMEOUT_S
                    break
                if line.startswith("frame=") and expect > 0:
                    try:
                        done = int(line.split("=", 1)[1].strip())
                    except ValueError:
                        continue
                    with self._lock:
                        self._frac = min(1.0, done / float(expect))
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            err = ""
            try:
                err = (proc.stderr.read() or "")[:400]
                proc.stderr.close()
            except Exception:
                pass
            proc.wait()
            with self._lock:
                self._proc = None

        if abort or proc.returncode != 0:
            # Leave nothing half-written to be mistaken for a real file, or for
            # work still in progress.
            try:
                if os.path.exists(part):
                    os.remove(part)
            except OSError:
                pass
            if abort:
                return abort
            lines = [ln for ln in err.strip().splitlines() if ln.strip()]
            return lines[-1][:120] if lines else "ffmpeg exit %d" % proc.returncode
        try:
            os.replace(part, out)
        except OSError as exc:
            return "rename failed: %s" % exc
        return None

    def _join(self, out_stem, parts):
        """Concatenate `parts` into one file named out_stem. None on success.

        STREAM COPY, NOT RE-ENCODE. Everything reaching here has already been
        through _normalize_clip, so every part shares a codec, a size and a
        frame rate - which is exactly the precondition the concat demuxer wants.
        A copy runs at disk speed instead of ffmpeg speed, so joining a long
        session costs seconds rather than the minutes a second encode would, and
        it cannot lose quality because nothing is decoded.
        """
        out = os.path.join(self.session_dir, out_stem + config.RECORD_EXT)
        if not parts:
            return None
        if len(parts) == 1:
            # One clip is already the answer. Renaming beats spawning ffmpeg to
            # copy a file onto itself, and it cannot fail halfway.
            try:
                os.replace(parts[0], out)
                return None
            except OSError as exc:
                return "rename: %s" % exc

        # The list file lives beside the parts so relative paths resolve, and is
        # named with a leading dot so _sweep_orphans treats it as a temporary.
        lst = os.path.join(self.session_dir, ".join_%s.txt" % out_stem)
        try:
            with open(lst, "w", encoding="utf-8") as fh:
                for path in parts:
                    # Single quotes are the concat demuxer's escape, and a path
                    # this code generates never contains one - but a session
                    # directory is built from a timestamp and a root the
                    # operator can change, so it is escaped rather than trusted.
                    fh.write("file '%s'\n" % path.replace("'", "'\\''"))
        except OSError as exc:
            return "list: %s" % exc

        part_out = out + ".part"
        # -nostdin AND stdin=DEVNULL, BOTH, AND THIS IS NOT BELT AND BRACES.
        #
        # THE FREEZE THIS FIXES, 2026-08-26: the first real session to reach the
        # join stage stopped the ENTIRE ground station dead - viewer, supervisor,
        # unclutter and backdrop, all in state T, resuming on SIGCONT and being
        # stopped again within the second.
        #
        # ffmpeg reads stdin for its interactive keys. This one inherited the
        # terminal, because .xinitrc's children live in their own process group
        # (pgid 971 against the tty's foreground 857) - and a BACKGROUND process
        # group that reads its controlling terminal is sent SIGTTIN, which stops
        # the whole group. Nothing had written a line of error anywhere: the
        # process table was the only place it showed.
        #
        # Every other ffmpeg in this file already passes -nostdin. This one did
        # not, and it is the only one added on the day it broke.
        #
        # -nostdin tells ffmpeg not to read; stdin=DEVNULL means it has nothing
        # to read even if a future flag change forgets. Either alone would fix
        # today's bug; both together mean the failure cannot come back as a
        # single dropped argument.
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
               "-f", "concat", "-safe", "0", "-i", lst,
               "-c", "copy", "-movflags", "+faststart", "-f", "mp4", part_out]
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, text=True)
        except OSError as exc:
            return "ffmpeg: %s" % exc
        with self._lock:
            self._proc = proc
        err = ""
        try:
            err = (proc.communicate(timeout=config.COMBINED_TIMEOUT_S)[1] or "")[:300]
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            err = "timed out"
        finally:
            with self._lock:
                self._proc = None
        try:
            os.remove(lst)
        except OSError:
            pass
        if proc.returncode != 0 or err == "timed out":
            try:
                os.remove(part_out)
            except OSError:
                pass
            return err or "ffmpeg exit %s" % proc.returncode
        try:
            os.replace(part_out, out)
        except OSError as exc:
            return "rename: %s" % exc
        # Only now are the parts expendable. Deleting before the join verified
        # would turn a failed merge into lost footage.
        for path in parts:
            try:
                os.remove(path)
            except OSError:
                pass
        return None

    def _join_all(self):
        """Reduce a session to exactly three files: full, and one per camera.

        WHY THIS EXISTS (operator 2026-08-26): "make three video only full front
        and back". Recording still writes one file per camera PER CLIP, because
        that is what lets a pause split cleanly and what bounds the loss if
        power drops mid-run - but an operator wants one video per camera and one
        combined, not nine files whose numbering only means something to the
        code that wrote them.

        Named from the camera LABELS, so they read front/back rather than a slug
        derived from an RTSP hostname.
        """
        problems = []
        by_slug = {}
        for clip_no in sorted(self.clips):
            for slug, path in self.clips[clip_no]:
                if os.path.exists(path):
                    by_slug.setdefault(slug, []).append(path)

        for slug, parts in by_slug.items():
            label = (self.labels.get(slug) or slug).strip().lower() or slug
            # NUMBERED, so the cameras land in camera order. Operator 2026-09-01:
            # "i want to first video is front and second is back".
            #
            # They were named from the label alone - front.mp4 and back.mp4 -
            # which sorts BACK first in every file manager, on the Pi and on the
            # stick, because b precedes f. The front camera is the one an
            # operator opens first and it was always the second row.
            #
            # The camera number is already in the slug (cam1_front), put there
            # by config.camera_slug(), so nothing new has to be derived or
            # configured: 1_front.mp4, 2_back.mp4, and full.mp4 after them.
            #
            # NOT the bare slug, which would give cam1_front.mp4 - that is one
            # underscore away from the per-clip parts (cam1_front_001.mp4) and
            # the two would be easy to confuse by eye when a join goes wrong.
            m = re.search(r"cam(\d+)", slug, re.IGNORECASE)
            stem = ("%s_%s" % (m.group(1), label)) if m else label
            reason = self._join(stem, parts)
            if reason:
                problems.append("%s: %s" % (stem, reason))

        # The side-by-side clips, if any were built.
        fulls = []
        for clip_no in sorted(self.clips):
            p = os.path.join(self.session_dir,
                             "full_%03d%s" % (clip_no, config.RECORD_EXT))
            if os.path.exists(p):
                fulls.append(p)
        if fulls:
            reason = self._join("full", fulls)
            if reason:
                problems.append("full: %s" % reason)
        return problems

    def run(self):
        if not self.clips:
            with self._lock:
                self._state = "done"
            return
        if not _have_ffmpeg():
            with self._lock:
                self._state = "error"
                self._error = "ffmpeg not installed"
            return

        failures = []
        for clip_no in sorted(self.clips):
            if self._stopping.is_set():
                break
            # Masters first, so the full view is built FROM the normalised
            # files and inherits their equal length - otherwise hstack would
            # still be pairing a frame from one camera against a moment the
            # other one never recorded.
            with self._lock:
                self._state, self._clip, self._frac = ("normalising", clip_no, 0.0)
            try:
                problems = self._normalize_clip(self.clips[clip_no])
            except Exception as exc:            # never take the viewer down
                problems = [str(exc)[:120]]
            for problem in problems:
                failures.append("clip %03d master: %s" % (clip_no, problem))

            if not config.COMBINED_AFTER_SAVE:
                # Normalise-only mode: this clip is finished.
                with self._lock:
                    self._built += 1
                    self._steps_done += 1
                    self._frac = 1.0
                continue
            if self._stopping.is_set():
                break

            with self._lock:
                self._state, self._frac = "building", 0.0
            try:
                reason = self._build_clip(clip_no, self.clips[clip_no])
            except Exception as exc:            # never take the viewer down
                reason = str(exc)[:120]
            with self._lock:
                # Counted whether or not it succeeded: a failed clip is a step
                # the job will not repeat, and a dial that stalls on a failure
                # says only that something is wrong - which the error state
                # already says, and says better.
                self._steps_done += 1
                if reason:
                    failures.append("clip %03d: %s" % (clip_no, reason))
                else:
                    self._built += 1
                    self._frac = 1.0

        # ONE FILE PER CAMERA PLUS ONE COMBINED - see _join_all. Runs even when
        # some clips failed: the clips that DID build are still worth joining,
        # and a session that half-merged is more useful than one left as
        # numbered fragments.
        if (not self._stopping.is_set() and self._do_join
                and config.RECORD_JOIN_CLIPS):
            with self._lock:
                self._state, self._frac = "joining", 0.0
            try:
                failures.extend(self._join_all())
            except Exception as exc:            # never take the viewer down
                failures.append("join: %s" % str(exc)[:120])
            with self._lock:
                self._frac = 1.0

        with self._lock:
            if failures:
                self._state = "error"
                self._error = failures[0]
            else:
                self._state = "done"
        if self._on_done is not None:
            try:
                self._on_done(self)
            except Exception:
                pass


def _sweep_orphans(root, older_than=60.0):
    """Remove recorder temporaries left behind by a viewer that was killed.

    _normalize_one writes .<name>.norm and _build_clip writes full_nnn.mp4.part,
    each renamed over the real file on success. A SIGTERM mid-encode - which is
    every pass of the .xinitrc supervision loop, 49 of them in one afternoon on
    2026-08-20 - leaves the temporary behind instead, and nothing ever came back
    for it. It cannot be copied away either: usb_backup.plan_copy skips both
    patterns deliberately, because a half-written file is not footage. So the
    orphan is never backed up, never cleared, and _discard_if_empty cannot take
    the directory with it - that uses rmdir(), which refuses a directory holding
    anything at all. SESSION001 of that afternoon was the result: a session
    folder with no video in it, 1.6 MB of orphan, and no path that could ever
    remove either of them.

    Called once at startup, where no build of this process can be running. The
    mtime guard is for the case that is not this process: a second viewer, or a
    build still finishing under a supervisor that has already started the next.
    """
    if not root or not os.path.isdir(root):
        return
    now = time.time()
    for base, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith((".norm", ".part")):
                continue
            path = os.path.join(base, name)
            try:
                if now - os.path.getmtime(path) < older_than:
                    continue
                os.remove(path)
            except OSError:
                pass
    # Whatever is left empty was a session that never had video in it. rmdir(),
    # never rmtree(), for exactly the reason _discard_if_empty gives: it refuses
    # a directory holding anything, so this cannot delete footage even if the
    # sweep above is wrong about what counts as a temporary.
    for base, dirs, _files in os.walk(root):
        for name in dirs:
            try:
                os.rmdir(os.path.join(base, name))
            except OSError:
                pass


class SessionManager:
    """The recording session: state machine, output paths, one recorder each.

    main.py pushes the decoded switch state in every UI frame and reads status()
    back out. Everything below the API is idempotent, so calling set_state()
    thirty times a second with the same value costs nothing.

    Layout on disk - one directory per session, one file per RECORDED camera
    per clip (config.RECORD_CAMERAS: the front camera alone by default), plus
    the side-by-side FULL VIEW when more than one is recorded (see CombinedView):

        /recordings/20260815_134500_SESSION001/cam1_front_001.mp4
                                               cam2_back_001.mp4
                                               full_001.mp4
                                               cam1_front_002.mp4  <- after SAVE
    """

    # How long the SAVED confirmation stays on screen. Long enough to read
    # after looking back up from the panel, short enough not to be furniture.
    TOAST_S = float(os.environ.get("SAVE_TOAST_S", "6.0"))

    def __init__(self, streams, root=None):
        self.root = root or config.RECORD_DIR
        # Before anything opens a file: clear temporaries a killed viewer
        # left behind, and the empty session folders they pinned in place.
        _sweep_orphans(self.root)
        # ONLY THE CAMERAS config.RECORD_CAMERAS NAMES GET A RECORDER - the
        # front one alone by default, operator 2026-09-12. Cut here, at the one
        # place the recorder list is built, so the clip paths, the join, the
        # labels and the full view all follow without knowing. Each stream
        # keeps its CAMERA number alongside, because the FRONT/BACK label comes
        # from that number and not from a position in a list with a hole in it.
        indexed = [(i, s) for i, s in enumerate(streams)
                   if config.camera_recorded(i)]
        if not indexed:
            # A RECORD_CAMERAS naming only cameras that do not exist would
            # record nothing. Footage beats a typo: fall back to every camera.
            indexed = list(enumerate(streams))
        sources = [s for _i, s in indexed]
        # The FULL VIEW is a session output, so it is created here rather than
        # by main.py: anything that records through a SessionManager gets the
        # combined file with no extra wiring, the headless test included.
        if config.RECORD_COMBINED and len(sources) > 1:
            sources.append(CombinedView(sources[:]))
        self.recorders = [
            CameraRecorder(
                s, getattr(s, "slug", None) or s.name.lower().replace(" ", ""))
            for s in sources
        ]
        # The streams this session writes, by slug, so main.py can ask
        # records() and keep the REC tag off a camera that is only on screen.
        self._recorded_slugs = {
            rec.slug for rec in self.recorders[:len(indexed)]}
        for rec in self.recorders:
            rec.start()

        self.state = STOPPED
        self.session_dir = None
        self.session_started = None
        self.clip = 0

        # Recorded seconds, i.e. wall clock spent RECORDING with the pauses
        # taken out - which is both what the operator means by "how long" and
        # the playback length of the files.
        self._rolled = 0.0          # closed-out time, before the current roll
        self._roll_since = None     # monotonic when the current roll began
        self._clip_rolled = 0.0
        self._clip_since = None

        self._toast = None          # (text, detail, monotonic_expiry)
        self._last_saves = None
        self._arm_at = None     # monotonic when the run lever was thrown     # last save_presses count acted on
        self._hold_s = 0.0          # how long SAVE is currently held (pushed in)

        # Every file this session has handed to a recorder, so a discard can
        # remove exactly what was written and nothing else - see _discard().
        self._written = []
        # The unclaimed recording, if any: see PENDING.
        self._pending = None

        # Per clip, what the full view will be built from once the run is kept:
        # {clip_no: [(slug, path), ...]} and the head skew between those files,
        # {clip_no: {slug: seconds}}. Collected as the clips close because that
        # is the only moment the writers' first-frame instants are still known -
        # nothing on disk records which camera started late.
        self._clip_members = {}
        self._clip_offsets = {}
        # Display labels for the burned-in tile captions, by slug. Looked up by
        # CAMERA number, not recorder position: with the back camera the only
        # one recorded, recorder 0 is CAM 2 and must still read BACK.
        self._labels = {}
        for (i, _s), rec in zip(indexed, self.recorders):
            self._labels[rec.slug] = config.camera_label(i) or rec.slug
        for rec in self.recorders[len(indexed):]:
            self._labels[rec.slug] = rec.slug       # the full view, if any
        # The running (or last) full-view build. See FullViewBuilder.
        self._builder = None
        # Kept sessions whose build has not started yet, oldest first. A save
        # landing while an earlier build is still running QUEUES here rather
        # than replacing it - see _start_full_view.
        self._build_queue = []
        self._build_lock = threading.Lock()
        self._building = False
        # Wall time the last build finished, so status() can tell "processing
        # done, nothing has taken it away yet" (READY TO TRANSFER) apart from
        # "done and already on a stick".
        self._built_at = None

    def records(self, stream):
        """True if `stream` is one this session writes (see RECORD_CAMERAS)."""
        slug = (getattr(stream, "slug", None)
                or getattr(stream, "name", "").lower().replace(" ", ""))
        return slug in self._recorded_slugs

    # -- clock ----------------------------------------------------------------

    def _elapsed(self):
        extra = 0.0 if self._roll_since is None else time.monotonic() - self._roll_since
        return self._rolled + extra

    def _clip_elapsed(self):
        extra = 0.0 if self._clip_since is None else time.monotonic() - self._clip_since
        return self._clip_rolled + extra

    def _roll(self, rolling):
        """Start/stop both clocks. Idempotent."""
        now = time.monotonic()
        if rolling and self._roll_since is None:
            self._roll_since, self._clip_since = now, now
        elif not rolling and self._roll_since is not None:
            self._rolled += now - self._roll_since
            self._clip_rolled += now - self._clip_since
            self._roll_since = self._clip_since = None

    # -- clips ----------------------------------------------------------------

    def _clip_paths(self):
        return {
            rec.slug: os.path.join(
                self.session_dir, f"{rec.slug}_{self.clip:03d}{config.RECORD_EXT}")
            for rec in self.recorders
        }

    def _begin_clip(self):
        self.clip += 1
        paths = self._clip_paths()
        for rec in self.recorders:
            rec.begin_clip(paths[rec.slug])
        self._written.extend(paths.values())
        self._clip_members[self.clip] = [
            (rec.slug, paths[rec.slug]) for rec in self.recorders]
        self._clip_rolled = 0.0
        self._clip_since = time.monotonic() if self.state == RECORDING else None

    def _end_clip(self):
        # Read the first-write instants BEFORE asking the recorders to close:
        # end_clip() clears them on the encoder thread's next tick.
        self._capture_skew(self.clip)
        for rec in self.recorders:
            rec.end_clip()

    def _capture_skew(self, clip_no):
        """Record how far behind the earliest camera each other one started.

        A writer opens on its own camera's first frame, so two files from one
        clip can begin up to seconds apart - 1s of it measured on SESSION009.
        Relative to the earliest is all the builder needs, and it avoids caring
        where the clip's zero was, which PAUSE would otherwise complicate.
        """
        if not clip_no:
            return
        firsts = {}
        for rec in self.recorders:
            when = rec.status().get("clip_first_write")
            if when is not None:
                firsts[rec.slug] = when
        if not firsts:
            return
        earliest = min(firsts.values())
        self._clip_offsets[clip_no] = {
            slug: max(0.0, when - earliest) for slug, when in firsts.items()}

    # -- session --------------------------------------------------------------

    def _start(self):
        # Anything still unclaimed loses its window here. Silence is a discard
        # everywhere else in this flow, and a second run starting is a stronger
        # signal than silence that the operator has moved on from the first.
        if self._pending:
            self._resolve_pending(keep=False, reason="superseded")

        # The previous run's READY TO TRANSFER belongs to the previous run. Its
        # build keeps going (see _start_full_view - it is queued, never killed);
        # this only stops the strip offering "plug the USB in now" to an operator
        # who has visibly moved on and started recording again.
        if not self._building:
            self._builder = None
            self._built_at = None

        self.session_started = datetime.now()
        # session01 date 26-08-26 start 20-07-56   - the end time is appended
        # when the run stops, see _finalize_dir_name. Spaces and "-" only: see
        # the note in _session_started for why the spec's "/" and ":" cannot be
        # used on a directory that gets copied to a FAT32 stick.
        self.session_dir = os.path.join(
            self.root,
            "session%02d date %s start %s" % (
                _next_session_no(self.root),
                self.session_started.strftime("%d-%m-%y"),
                self.session_started.strftime("%H-%M-%S")))
        os.makedirs(self.session_dir, exist_ok=True)
        self.clip = 0
        self._written = []
        self._rolled = self._clip_rolled = 0.0
        self._roll_since = self._clip_since = None
        self._begin_clip()

    def _finalize_dir_name(self, stopped_at):
        """Fold the stop time into the session folder's name.

        Operator 2026-08-26: the folder should say which session it is, on what
        date, and between which times. The start half is known when the folder
        is created; the STOP half only exists now, so the directory is renamed
        rather than named once.

            20260826_200756_SESSION001              during the run
            20260826_200756_SESSION001_to_201530    after it

        THE FIRST 15 CHARACTERS ARE LOAD-BEARING and must stay a
        %Y%m%d_%H%M%S stamp: _session_started() slices name[:15] to decide
        whether a session predates the last verified USB backup, and a session
        it cannot date is one the numbering will not skip. That is why the stop
        time is appended rather than the whole name being made prettier.

        Renaming a directory that still has open files in it is safe here - the
        encoders hold file descriptors, and on Linux those follow the inode, not
        the path. Waiting for them would block the UI thread for up to
        _await_closed's timeout on every stop, for no benefit.
        """
        old = self.session_dir
        if not old or not os.path.isdir(old):
            return
        base = os.path.basename(old)
        if " end " in base:                 # already renamed - stop pressed twice
            return
        new = os.path.join(os.path.dirname(old),
                           "%s end %s" % (base, stopped_at.strftime("%H-%M-%S")))
        try:
            os.rename(old, new)
        except OSError:
            # A failed rename is cosmetic: the footage is exactly where it was
            # and every path still points at it. Never let it cost a recording.
            return

        # EVERY REMEMBERED PATH MOVES WITH IT. _written drives the discard, and
        # _clip_members is what the merge reads - a stale absolute path here
        # means a session that silently builds nothing.
        def remap(path):
            if path == old or path.startswith(old + os.sep):
                return os.path.join(new, os.path.relpath(path, old))
            return path

        self.session_dir = new
        self._written = [remap(p) for p in self._written]
        self._clip_members = {
            clip: [(slug, remap(p)) for slug, p in entries]
            for clip, entries in self._clip_members.items()
        }

    def _stop_session(self):
        self._roll(False)
        held = self._elapsed()
        self._end_clip()
        for rec in self.recorders:
            rec.set_rolling(False)
        self._roll_since = self._clip_since = None
        # Before the keep/discard decision below, because both of them read the
        # paths this rewrites - the merge from _clip_members, the discard from
        # _written. See _finalize_dir_name.
        self._finalize_dir_name(datetime.now())

        # Nothing worth asking about: no time on the clock, or the cameras never
        # delivered a frame so there is no file to keep either way.
        wrote = any(rec.status()["frames"] for rec in self.recorders)
        if (held < config.RECORD_MIN_RUN_S or not wrote
                or config.RECORD_CONFIRM_S <= 0):
            if held >= config.RECORD_MIN_RUN_S and wrote:
                self._toast_now("SAVED", f"{self._clip_word()}  {hms(held)}")
                # RECORD_CONFIRM_S=0 keeps everything automatically, and used to
                # return straight out of here - so in that mode the merge and the
                # master re-encode never ran at all and no session ever got a
                # full view. Auto-keep is still a keep; give it the same pipeline
                # a confirmed one gets.
                self._start_full_view({
                    "dir": self.session_dir,
                    "members": dict(self._clip_members),
                    "offsets": dict(self._clip_offsets),
                })
            self._discard_if_empty()
            return

        # The recorder threads close their writers on their next tick, up to
        # 1/RECORD_FPS later. The window is orders of magnitude longer than
        # that, so the files are always complete before it can be resolved.
        self._pending = {
            "until": time.monotonic() + config.RECORD_CONFIRM_S,
            # A HOLD ALREADY IN PROGRESS DOES NOT COUNT. save_held_s measures
            # how long the button has been continuously down, and the panel now
            # carries a LATCHING save switch - so a switch thrown minutes ago
            # reports a hold of minutes, satisfies RECORD_SAVE_HOLD_S on the
            # very first frame after STOP, and claims the session before the
            # operator sees the window at all. That is the auto-save reported
            # 2026-08-25 ("when i stop recording to its auto save by it self").
            #
            # So a hold that predates the window is masked until the switch is
            # released, exactly as the pause lever is. The gesture the spec
            # describes is "press and hold to claim", and a lever left thrown
            # is not a press.
            "hold_masked": (self._hold_s or 0.0) > 0.0,
            "held": held,
            "clips": self.clip,
            "dir": self.session_dir,
            "files": list(self._written),
            # Snapshotted here so a keep can start the build even though _start()
            # for the next run has already reset the live bookkeeping.
            "members": dict(self._clip_members),
            "offsets": dict(self._clip_offsets),
        }

    def _clip_word(self, clips=None):
        clips = self.clip if clips is None else clips
        return f"{clips} clip{'s' if clips != 1 else ''}"

    # -- the confirm window ----------------------------------------------------

    def pending_left(self):
        """Seconds remaining to claim the last recording, or None if not waiting."""
        if not self._pending:
            return None
        return max(0.0, self._pending["until"] - time.monotonic())

    def _resolve_pending(self, keep, reason=""):
        pending, self._pending = self._pending, None
        if not pending:
            return
        held, clips = pending["held"], pending["clips"]
        if keep:
            self._toast_now("SAVED", f"{self._clip_word(clips)}  {hms(held)}")
            self._start_full_view(pending)
            return

        # The encoder threads close their writers on their own tick, so a
        # discard fired the instant after STOP - which "superseded" is - can
        # land while the file is still open. On Windows os.remove then raises
        # and the delete silently does nothing; on Linux it unlinks a file the
        # writer is still filling. Wait for them, briefly and boundedly.
        self._await_closed()

        # Remove exactly the files this session handed out, then the directory -
        # os.remove per known path and a bare rmdir, never rmtree. A recursive
        # delete pointed at a path built from a timestamp is one bad join away
        # from taking something else with it, and this runs unattended.
        failed = 0
        for path in pending["files"]:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass                    # never opened - the camera was dead
            except OSError:
                failed += 1
        try:
            os.rmdir(pending["dir"])
        except OSError:
            pass
        if pending["dir"] == self.session_dir:
            self.session_dir = None

        detail = f"{self._clip_word(clips)}  {hms(held)}"
        if failed:
            # Do not claim a discard that did not happen - the operator would
            # walk away believing the card is clear when it is not.
            self._toast_now("DISCARD FAILED",
                            f"{failed} file{'s' if failed != 1 else ''} still on disk")
        else:
            self._toast_now("DISCARDED",
                            f"{detail}  ({reason})" if reason else detail)

    def _start_full_view(self, pending):
        """Kick off the side-by-side build for a session that was just kept.

        Only on a KEEP: a discarded run has had its files deleted, and spending
        a minute of Pi on footage the operator threw away was the other half of
        what made the old live encoder wasteful.
        """
        # The builder still has work when normalising is off: naming the output.
        # Without RECORD_JOIN_CLIPS here it bailed out and left the clip called
        # cam1_front_001.mp4, when a one-clip session only needs a rename to
        # front.mp4 - which costs nothing and is the whole point of this path.
        if not (config.COMBINED_AFTER_SAVE or config.RECORD_NORMALIZE
                or config.RECORD_JOIN_CLIPS):
            return
        # The encoders close on their own tick; the build reads those files.
        self._await_closed()
        members = {
            clip: [(slug, path) for slug, path in entries
                   if os.path.exists(path)]
            for clip, entries in (pending.get("members") or {}).items()
        }
        # A one-camera clip can never be a side-by-side, but it still wants
        # normalising AND joining - with RECORD_CAMERAS at its front-only
        # default EVERY clip is one camera, and dropping it here would leave
        # the session as cam1_front_001.mp4 with no 1_front.mp4 ever made. The
        # >1 rule only applies when building is all this thread would have to do.
        least = (1 if (config.RECORD_NORMALIZE or config.RECORD_JOIN_CLIPS)
                 else 2)
        members = {c: e for c, e in members.items() if len(e) >= least}
        if not members:
            return
        job = (pending["dir"], members, pending.get("offsets") or {}, True)
        with self._build_lock:
            if self._building:
                # One at a time - two builds would fight over all four cores and
                # both finish later than running them in order. But QUEUE the
                # second rather than stopping the first: an operator who saves a
                # run and starts the next one straight away used to have the
                # first session's merge killed halfway, so that session ended up
                # on the stick with per-camera files and no full view at all.
                self._build_queue.append(job)
                return
            self._building = True
            self._launch_build(job)

    def _launch_build(self, job):
        """Start one queued build. The caller holds _build_lock."""
        session_dir, members, offsets, join = job
        self._builder = FullViewBuilder(session_dir, members, offsets,
                                        self._labels, on_done=self._build_done,
                                        join=join)
        self._builder.start()

    def _prep_clip(self, clip_no):
        """Normalise one just-closed clip WHILE THE RECORDING CONTINUES.

        This is what makes the save fast. Measured 2026-08-31, the hardware
        encoder normalises at about 4x realtime, so a clip finalised the moment
        it closes finishes long before the next one does, and the encoder still
        spends most of its time idle. At stop only the final partial clip and the
        join remain - seconds, instead of the ~25 minutes a 100-minute session
        needed when every frame was re-encoded after the fact.

        Queued through the SAME single-slot lane as the stop-time build, so the
        two can never run together and fight over four cores.

        NOTHING HERE IS LOAD-BEARING. If the prep never runs, or fails, the
        stop-time pass normalises the clip exactly as it always did - it simply
        finds mpeg4 instead of h264 and does the work. This is an optimisation
        that fails safe, which is the only kind worth putting in the path that
        saves an operator's footage.
        """
        members = {clip_no: [(slug, path)
                             for slug, path in self._clip_members.get(clip_no, [])
                             if os.path.exists(path)]}
        least = 1 if config.RECORD_NORMALIZE else 2
        if len(members[clip_no]) < least:
            return
        offsets = {clip_no: dict(self._clip_offsets.get(clip_no, {}))}
        job = (self.session_dir, members, offsets, False)
        with self._build_lock:
            if self._building:
                self._build_queue.append(job)
                return
            self._building = True
            self._launch_build(job)

    def _build_done(self, _builder):
        """Runs ON the builder thread as it finishes. Starts the next queued job.

        _building rather than _builder.is_alive() is what the queue turns on:
        this callback fires from inside run(), while the thread is still alive,
        so a save landing in that instant would see a live builder, queue behind
        it, and then never be popped by anyone.
        """
        self._built_at = time.time()
        with self._build_lock:
            if self._build_queue:
                self._launch_build(self._build_queue.pop(0))
            else:
                self._building = False

    def _await_closed(self, timeout=1.5):
        """Block until every encoder has released its file, or `timeout`.

        Only ever called on a state transition, never per frame, and normally
        returns at once - by the time a confirm window expires the writers have
        been shut for fifteen seconds. The bound exists so a wedged encoder
        thread cannot freeze the UI, not because waiting is expected.
        """
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if all(rec.idle() for rec in self.recorders):
                return True
            time.sleep(0.02)
        return False

    def poll(self):
        """Expire the confirm window. main.py calls this once per UI frame.

        Deliberately not folded into status(): a getter that deletes files is a
        getter nobody can call safely, including from a debugger.
        """
        if self._pending and time.monotonic() >= self._pending["until"]:
            self._resolve_pending(keep=False, reason="not saved")

        # ROLL THE CLIP ON A TIMER so the save happens DURING the recording -
        # see config.RECORD_SEGMENT_S and _prep_clip. Exactly what the SAVE tap
        # does, minus the toast: close this clip, open the next, and hand the
        # closed one to the encoder while filming continues.
        #
        # Driven from poll() because main.py already calls it once per UI frame,
        # so the check costs a float compare and needs no thread of its own.
        seg = config.RECORD_SEGMENT_S
        if (seg > 0 and self.state == RECORDING and self.session_dir
                and not self._pending and self._clip_elapsed() >= seg):
            closing = self.clip
            self._capture_skew(self.clip)
            self._begin_clip()
            self._prep_clip(closing)

    def _discard_if_empty(self):
        """Remove the session directory if nothing was ever written into it.

        With the cameras off the wire a start/stop leaves a dated, empty
        directory - and three of those appeared in ten seconds during bring-up,
        from switch bounce alone. A directory per non-recording is worse than
        useless: it makes the recording list a place you have to sort real runs
        out of. rmdir(), never rmtree() - it refuses on a directory holding
        anything at all, so this cannot delete footage even if the frame counts
        are wrong.
        """
        if not self.session_dir:
            return
        try:
            if not os.listdir(self.session_dir):
                os.rmdir(self.session_dir)
                self.session_dir = None
        except OSError:
            pass

    def set_state(self, state):
        """Push the decoded switch state. Safe to call at the UI frame rate.

        THE RUN ARMS BEFORE IT STARTS - see config.RECORD_START_DELAY_S. A throw
        of the lever is held here, not acted on, until the delay has elapsed;
        dropping it inside the window leaves no session behind at all, because
        _start() was never called.
        """
        if state not in (RECORDING, PAUSED, STOPPED):
            return

        # --- arming ---------------------------------------------------------
        delay = config.RECORD_START_DELAY_S
        if delay > 0 and self.state == STOPPED and state in (RECORDING, PAUSED):
            now = time.monotonic()
            if self._arm_at is None:
                self._arm_at = now
                return                      # armed; nothing written yet
            if now - self._arm_at < delay:
                return                      # still counting; keep waiting
            # held long enough - fall through and start for real
        self._arm_at = None

        if state == self.state:
            # Still has to keep the clock honest across the frames *between*
            # transitions - _roll() is the idempotent part.
            self._roll(state == RECORDING)
            return

        was, self.state = self.state, state
        if was == STOPPED:
            self._start()
        if state == STOPPED:
            self._stop_session()
            return

        self._roll(state == RECORDING)
        for rec in self.recorders:
            rec.set_rolling(state == RECORDING)

    def save_clip(self):
        """The GPIO25 press. It means two different things by design:

        While rolling, it closes this clip and opens the next without stopping -
        so an operator who just drove past something worth keeping can bank it
        and let the run continue, rather than having to stop and lose whatever
        comes next down the duct.

        In the window after STOP, a tap only HINTS: claiming the whole session
        is a press-and-hold (RECORD_SAVE_HOLD_S, see on_save_hold), so a stray
        tap of the same button that banks clips mid-run cannot silently commit
        a whole recording. The hint teaches the gesture at the exact moment it
        is needed.
        """
        if self._pending:
            # No toast: the strip's pending line already reads "hold SAVE ...",
            # and a toast would sit on top of the live keep-holding countdown
            # for TOAST_S - exactly the seconds the operator needs to see it.
            return False

        if self.state == STOPPED or self.session_dir is None:
            self._toast_now("NOTHING TO SAVE", "not recording")
            return False

        held = self._clip_elapsed()
        frames = max((r.status()["frames"] for r in self.recorders), default=0)
        label = f"clip {self.clip:03d}"
        # BEFORE rolling over, and the reason is the whole multi-clip full view.
        # _begin_clip closes the open file and opens the next one, and the
        # encoder thread wipes _clip_first_write as it does - so this is the last
        # instant the two cameras' start skew for the clip being banked still
        # exists anywhere. Only _stop_session used to capture it (via _end_clip),
        # which meant that in a run of five clips only the FIFTH was aligned:
        # full_001..004 were hstacked with a zero lead-in and put two different
        # moments side by side, up to the 1s of skew measured on SESSION009.
        closing = self.clip
        self._capture_skew(self.clip)
        self._begin_clip()
        # Finalise the clip just banked while the run carries on - see _prep_clip.
        self._prep_clip(closing)

        if frames == 0:
            self._toast_now("SAVED (EMPTY)", f"{label}  no video")
        else:
            self._toast_now("SAVED", f"{label}  {hms(held)}")
        return True

    def on_save_button(self, presses):
        """Fire save_clip() once per new press of the GPIO25 button.

        Counted edges, not levels - see inputs.SAVE_PIN. The first call only
        primes the counter, or a viewer restarted mid-session would fire a save
        it was never asked for. `>` not `!=`, so the counter resetting to 0
        (which _blank() does whenever the reader loses the pins) is a no-op
        rather than a phantom press.
        """
        presses = presses or 0
        if self._last_saves is None or presses > self._last_saves:
            fire = self._last_saves is not None
            self._last_saves = presses
            # DISABLED BY DEFAULT - see config.RECORD_SAVE_BUTTON. The counter is
            # still tracked so re-enabling it cannot fire a backlog of presses
            # that arrived while it was off.
            if fire and config.RECORD_SAVE_BUTTON:
                self.save_clip()

    def finalize(self):
        """Claim the unclaimed recording NOW. True if there was one to claim.

        The programmatic form of the 3s hold - the keyboard fallback and
        shutdown use it, the GPIO path arrives via on_save_hold().
        """
        if not self._pending:
            return False
        self._resolve_pending(keep=True)
        return True

    def on_save_hold(self, held_s):
        """Push how long GPIO25 has been held. main.py calls this every frame.

        The press-and-hold that finalizes a stopped recording (operator spec
        2026-08-18): after STOP, holding SAVE for RECORD_SAVE_HOLD_S claims the
        session into RECORD_DIR. Level-driven rather than edge-driven because a
        hold IS a level - and while the button is down the confirm window is
        pushed back, so a hold that starts on the window's last second is
        honoured instead of the countdown deleting the files mid-hold.
        """
        self._hold_s = held_s or 0.0
        if not self._pending:
            return
        if self._pending.get("hold_masked"):
            # Clears only on a real release, so the next hold is a fresh one.
            if self._hold_s <= 0.0:
                self._pending["hold_masked"] = False
            return
        if self._hold_s <= 0.0:
            return
        if self._hold_s >= config.RECORD_SAVE_HOLD_S:
            self._resolve_pending(keep=True)
            return
        self._pending["until"] = max(
            self._pending["until"],
            time.monotonic()
            + (config.RECORD_SAVE_HOLD_S - self._hold_s) + 1.0)

    def on_inputs(self, snapshot):
        """Apply one whole inputs.py snapshot. Convenience for callers with no
        keyboard fallback to merge in - main.py drives the two halves itself."""
        state = snapshot.get("session")
        if state is not None:
            self.set_state(state)
        self.on_save_button(snapshot.get("save_presses"))

    # -- readout --------------------------------------------------------------

    def _toast_now(self, text, detail):
        self._toast = (text, detail, time.monotonic() + self.TOAST_S)

    def toast(self):
        """(text, detail) for the confirmation banner, or None once it expires."""
        if self._toast is None:
            return None
        text, detail, expiry = self._toast
        if time.monotonic() > expiry:
            self._toast = None
            return None
        return text, detail

    def status(self):
        cams = [rec.status() for rec in self.recorders]
        left = self.pending_left()
        return {
            # PENDING outranks the switch state: the switch says STOPPED, but
            # what the operator has to act on is the unanswered question.
            "state": PENDING if left is not None else self.state,
            # Seconds still to wait before a thrown lever starts writing, or
            # None when nothing is arming. See RECORD_START_DELAY_S.
            "arming_left": (
                max(0.0, config.RECORD_START_DELAY_S
                    - (time.monotonic() - self._arm_at))
                if self._arm_at is not None else None),
            "pending_left": left,
            "pending_held": self._pending["held"] if self._pending else None,
            "pending_clips": self._pending["clips"] if self._pending else None,
            # Progress of the press-and-hold, so the strip can count it down
            # live instead of the operator guessing when 3 seconds is up.
            "save_hold": self._hold_s if self._pending else None,
            "save_hold_need": config.RECORD_SAVE_HOLD_S,
            "elapsed": self._elapsed(),
            "clip": self.clip,
            "clip_elapsed": self._clip_elapsed(),
            "dir": self.session_dir,
            "cameras": cams,
            "bytes": sum(c["bytes"] for c in cams),
            # One line for the strip: the first real error, or None.
            "error": next((c["error"] for c in cams if c["error"]), None),
            "free_mb": free_mb(self.session_dir or self.root),
            "toast": self.toast(),
            # None until a session has been kept; then the strip reports the
            # side-by-side build, which outlives the SAVED toast on a long run.
            "full_view": self._full_view_status(),
        }

    def _full_view_status(self):
        """The build's own status, plus whether the result is waiting for a stick.

        `ready` is the operator-facing half: processing has FINISHED and nothing
        has taken the footage away since, so this is the moment to plug the USB
        in. Without it the strip went straight from MERGING back to "idle", and
        the only way to know the merge had finished was to guess - which is how
        sticks got plugged in mid-build and went home without the merged file.

        A backup that has since run makes it not-ready again: the reset marker is
        stamped when a transfer verifies, so a marker newer than the build means
        this session is already on a stick.
        """
        if self._builder is None:
            return None
        fv = self._builder.status()
        fv["queued"] = len(self._build_queue)
        fv["ready"] = bool(
            fv["state"] in ("done", "error")
            and not self._build_queue
            and self._built_at is not None
            and _reset_epoch(self.root) < self._built_at)
        return fv

    def stop(self):
        if self.state != STOPPED:
            self.set_state(STOPPED)
        # Shutting the viewer down is not the operator declining to save - it is
        # the window being cut short before they could answer. Keep it. The rule
        # is "unclaimed footage is discarded", and footage nobody was given the
        # chance to claim is not unclaimed.
        if self._pending:
            self._resolve_pending(keep=True)
        for rec in self.recorders:
            rec.stop()
        for rec in self.recorders:
            rec.join(timeout=3.0)

        # MERGE THE LIVE RUN BEFORE WE GO, synchronously. Operator 2026-09-02:
        # "i want also in live recording".
        #
        # Everything above closes the files, and _resolve_pending(keep=True)
        # queues the merge - but that merge runs in a FullViewBuilder thread, and
        # on a shutdown the process is gone long before it finishes. The footage
        # survives as numbered parts and usb_chooser refuses to offer it, which
        # is the "not ready for transfer" this fixes. The recovery pass at the
        # next boot would eventually catch it, but that is no help to someone who
        # powers the Pi down in order to take the card away.
        #
        # A STREAM COPY IS FAST ENOUGH TO DO HERE. It runs at disk speed - the
        # 4-minute session this was tested on joined in well under a second - so
        # it fits inside systemd's TERM-to-KILL grace period with room to spare.
        # An encode would not, which is exactly why this joins and never encodes.
        #
        # idle_s=0 because the folder was being written to a moment ago by
        # definition; the usual idle guard exists to protect a LIVE session from
        # a background sweep, and here we are the thing that just stopped it.
        if self.session_dir:
            try:
                recover_unfinished_sessions(
                    os.path.dirname(self.session_dir), idle_s=0.0,
                    only=os.path.basename(self.session_dir))
            except Exception:
                # Never let a tidy-up stop the shutdown. The parts are on disk
                # either way and the next boot will pick them up.
                pass


def main():
    """Record from the URLs on the command line for 10s, pause 3s, save, stop."""
    import sys

    from stream import RTSPStream

    urls = sys.argv[1:] or [url for _, url in config.CAMERAS]
    streams = [
        RTSPStream(name=f"CAM {i + 1}", url=url,
                   latency_ms=config.RTSP_LATENCY_MS,
                   protocols=config.RTSP_PROTOCOL)
        for i, url in enumerate(urls)
    ]
    for s in streams:
        s.start()

    session = SessionManager(streams)
    script = [(RECORDING, 10), (PAUSED, 3), ("SAVE", 0), (RECORDING, 6),
              (STOPPED, 1)]
    try:
        for step, hold in script:
            if step == "SAVE":
                session.save_clip()
            else:
                session.set_state(step)
            print(f"-> {step}")
            for _ in range(int(hold * 2)):
                time.sleep(0.5)
                st = session.status()
                print(f"   {st['state']:<9} {hms(st['elapsed'])} "
                      f"clip {st['clip']} "
                      f"{st['bytes'] / 1e6:.1f} MB "
                      f"{[c['frames'] for c in st['cameras']]} "
                      f"{st['error'] or ''}")
    except KeyboardInterrupt:
        pass
    finally:
        session.stop()
        for s in streams:
            s.stop()
        print(f"files in {session.session_dir}")


if __name__ == "__main__":
    main()
