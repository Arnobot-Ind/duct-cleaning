# Implementation prompt — duct-robot recording, save-confirm, merge and USB transfer

Hand this whole document to an implementer (human or model) to rebuild the
feature from nothing. Every number in it is load-bearing and was measured on the
real rig; the "why" notes exist so the constraints are not optimised away.

---

## 0. Context you are building into

A duct-cleaning robot rig. A **Raspberry Pi 4 ground station** runs a fullscreen
PyQt5 viewer showing two IP cameras streamed over RTSP from a second Pi on the
robot. The operator drives from a physical panel of switches wired to the ground
station's GPIO. Everything runs headless-on-boot under X with a supervision loop
that SIGTERMs and restarts the viewer.

Existing modules you plug into (do not rewrite them):

* `stream.py` — one `RTSPStream` per camera, decoding in its own thread.
  `stream.latest()` returns `(frame_bgr_or_None, seq)`. Each stream carries
  `.name` and `.slug`.
* `inputs.py` — a GPIO reader thread polling the panel at 20 Hz and publishing
  one snapshot dict via `inputs.latest()`.
* `main.py` — owns the single Qt UI timer (~30 fps). It pushes state into
  everything and reads status back out. **Nothing outside `main.py` touches Qt.**
* `topbar.py` — the status bar of chips above the video.
* `inputs_panel.py` — the side panel, including the session block and its
  one-line detail "strip".
* `config.py` — every tunable, each read from an env var with a default.

Two cameras, mounted **90° apart**: CAM 1 = FRONT arrives 1280×720, CAM 2 = BACK
arrives 720×1280 after rotation. `config.camera_label(i)` → `FRONT`/`BACK`,
`config.camera_slug(i)` → `cam1_front`/`cam2_back`.

Target: Pi 4, 4 cores. Two RTSP decodes already cost ~0.6 core each.

---

## 1. What to build

1. `recorder.py` — the recording engine. Owns encoder threads and the
   post-save ffmpeg pipeline. Touches no Qt.
2. Recording UI in `topbar.py` (a REC chip and a USB chip) and in
   `inputs_panel.py` (session block + detail strip).
3. `usb_backup.py` + `usb-backup.service` — a **separate root daemon** that
   mirrors the recordings onto any USB stick, verifies, then frees the Pi.
4. Config entries in `config.py` for every tunable in §11.

Hard rule for all of it: **a recorder that takes the viewer down with it is
worse than one that admits it is not recording.** Every failure becomes text in
a status dict; nothing raises at a caller.

---

## 2. Controls and the input contract

| Control | Pin | Meaning |
|---|---|---|
| Switch 1 (red leg) | GPIO22 | START / STOP |
| Switch 2 (green leg) | GPIO11 | PAUSE / RESUME |
| SAVE button | GPIO9 | two gestures — see below |

All controls are **active-low / fail-dead**: an unplugged or snapped wire must
read OFF.

The SAVE button is momentary and pulses low for ~0.18 s. A 30 fps UI sampling
levels will miss it, so `inputs.py` publishes **both** of these and the recorder
consumes both:

* `save_presses` — running count of open→closed **edges** since start-up.
* `save_held_s` — seconds the button has been continuously held, `0.0` when open.

Decode the two switches into one of three strings: `"RECORDING"`, `"PAUSED"`,
`"STOPPED"`. `PAUSED` only exists inside a run — the pause lever with the record
lever off is `STOPPED`, not a fourth state.

Keyboard fallback for the bench: `Space` start/stop, `P` pause, `Ctrl+S` save.
When the panel is readable the hardware always wins; the keyboard latch is used
only when `snapshot["session"] is None`. A held key delivers no measurable
level, so `Ctrl+S` must call the programmatic claim directly (see §3).

---

## 3. State machine and the save gesture

```
STOPPED ──start──▶ RECORDING ⇄ PAUSED ──stop──▶ SAVE?  (RECORD_CONFIRM_S)
                       │                          │
                    tap SAVE                 hold SAVE 3 s ──▶ KEPT ──▶ post-save build
                  (bank clip,                     │
                   keep rolling)              silence ──▶ DISCARDED (files deleted)
```

Add a fourth state `PENDING = "SAVE?"` that **no switch produces** — it is what
the recorder is doing while it waits to be told whether the run was worth
keeping. In the published status it **outranks** the switch state, because what
the operator must act on is the unanswered question, not the lever position.

**The SAVE button means two different things by design:**

* **Tap while ROLLING** — close the current clip and open the next one without
  stopping, so an operator who just drove past something worth keeping can bank
  it and let the run continue.
* **Press-and-HOLD for `RECORD_SAVE_HOLD_S` (3.0 s) after STOP** — claim the
  whole session. A hold, not a tap, precisely because the same button banks
  clips: a stray tap must never be able to commit a whole recording.
* **Tap during the confirm window** — does nothing, and shows **no toast**: the
  strip is already showing the live "keep holding" countdown, and a 6 s toast
  would cover exactly the seconds the operator needs to see.

**On STOP:**

1. Stop the clocks, close the clip, capture the cameras' start skew (§6).
2. If elapsed < **1.0 s**, or no camera ever wrote a frame, or
   `RECORD_CONFIRM_S <= 0`: ask nothing. If it was a real run (≥1 s and frames
   written) still toast `SAVED` **and still run the whole post-save pipeline** —
   auto-keep is a keep. Otherwise remove the session directory if it is empty.
3. Else create a pending record: `until = monotonic() + RECORD_CONFIRM_S`
   (**10 s**), plus the elapsed time, the clip count, the directory, the full
   list of files written, and snapshots of the clip members and skews (snapshot
   them, because the next run's `_start()` resets the live bookkeeping and a
   keep must still be able to start the build).

**During the window**, every UI frame pushes `save_held_s` in:

* `held >= RECORD_SAVE_HOLD_S` → resolve as **keep**;
* `0 < held < need` → push the deadline out to
  `max(until, monotonic() + (need - held) + 1.0)`, so **a hold started on the
  window's last second is honoured** instead of the countdown deleting the files
  mid-hold.

**Expiry** must be a separate `poll()` that `main.py` calls once per UI frame,
**not** folded into the status getter — a getter that deletes files is one
nobody can call safely, a debugger included. Call order in `main.tick()` is:
push switch state → `on_save_button()` → `on_save_hold()` → `poll()` → read
`status()`, so a press or a completed hold landing on the last frame wins.

**Resolving as discard:**

1. Wait, bounded to 1.5 s, for every encoder to release its file. The encoder
   threads close on their own tick, and a discard fired the instant after STOP
   (the `superseded` case) can otherwise land while the file is still open —
   which on Windows makes `os.remove` fail silently and on Linux unlinks a file
   still being written.
2. `os.remove()` **exactly the paths this session handed out**, then a bare
   `os.rmdir()`. **Never `rmtree`, anywhere in this codebase** — a recursive
   delete pointed at a path built from a timestamp is one bad join away from
   taking something else with it, and this runs unattended.
3. Toast `DISCARDED  <n> clips  <h:mm:ss>  (<reason>)`. If any delete failed,
   toast `DISCARD FAILED  <n> files still on disk` instead — never claim a
   discard that did not happen, or the operator walks away believing the card is
   clear when it is not.

**Two other exits:**

* Starting a new run while one is unclaimed discards the old one with reason
  `superseded` — a second run beginning is a stronger signal than silence.
* **Shutting the viewer down KEEPS the pending run.** The rule is "unclaimed
  footage is discarded", and footage nobody was given the chance to claim is not
  unclaimed.

---

## 4. Disk layout

```
/recordings/20260815_134500_SESSION001/
        cam1_front_001.mp4      master, CAM 1 = FRONT
        cam2_back_001.mp4       master, CAM 2 = BACK
        full_001.mp4            both cameras side by side, built AFTER the save
        cam1_front_002.mp4      clip 002, from a SAVE tap mid-run
```

* One directory per session, `YYYYMMDD_HHMMSS_SESSIONnnn`; one file per camera
  per clip; file stem is the camera slug.
* `RECORD_DIR` defaults to **`/recordings`**, root-level and not under `~`, so
  the backup daemon has exactly one fixed thing to mirror. Created once with
  `sudo mkdir /recordings && sudo chown <user>:<user> /recordings`.
* **Session numbering**: `1 + the highest _SESSIONnnn found on disk`, rescanned
  from the directory listing on every start rather than counted in memory — the
  counter has to survive viewer restarts, and the listing is the durable record.
  An unreadable root starts at 1.
* **Numbering resets after a verified backup**: skip any session whose *name*
  timestamps it at or before the time in `RECORD_DIR/.session_reset` (§9). Use
  the name, never the mtime — clearing a backed-up session's files touches its
  directory. Skipped sessions are **never deleted**: one is only still on the
  card because the backup deliberately kept it, so that footage is still owed a
  transfer. Nothing collides, because the directory name carries a timestamp as
  well as a number.

---

## 5. Live encoding rules

* **Re-encode the frames already decoded for the screen.** Do *not* open a
  second RTSP session per camera with `ffmpeg -c copy`, even though it would be
  nearly free: these cheap IP cameras cap concurrent RTSP sessions, and the one
  moment you cannot afford the viewer to lose its picture is the moment the
  operator starts recording. It also makes the file provably what the operator
  saw, reconnects included. Budget: ~0.4 core per 720p camera at 15 fps.
* **One frame per wall-clock tick** at `RECORD_FPS = 15`, whether or not the
  camera delivered a new one; hold the last good frame across a dropout. An hour
  of duct run is then an hour of video, and a 20 s outage leaves 20 s of held
  frame rather than a jump cut that hides it. Never burst catch-up writes after
  a stall.
* **Write nothing while PAUSED** — that is what makes pause a cut, not a freeze.
* **Centre-crop every camera to one common square**, `RECORD_SQUARE_PX = 720`,
  before encoding. The cameras are 90° apart, so two pictures cannot be the same
  width *and* the same height unless they share an aspect ratio. Padding keeps
  black; fitting the portrait camera into landscape throws away 0.29 MP of 0.92;
  cropping to the largest square **both** cameras can supply gives genuinely
  identical pictures at native pixel density with nothing resampled. 720 is
  `min(1280, 720)` — never an upscale. Crop before remembering the frame as
  "last good", so a dropout cannot change the file's size mid-clip.
* **Fourcc `mp4v` via `cv2.VideoWriter`.** It is the one fourcc the Pi's apt
  OpenCV can always write into `.mp4` without an external encoder. Measured
  2026-08-19 with the viewer running, both cameras at 720×720/15: in-process
  `avc1` collapsed to ~3 fps (46 frames where mp4v wrote 137) and left a file
  with no moov atom. Two in-process x264 encoders do not fit beside two decodes
  and the UI. Size and codec are fixed **offline** instead (§7).
* If a writer fails to open, publish the reason as text (OpenCV only warns on
  stderr, which goes nowhere under a `.pyw` launcher) and back off 1 s — never
  spin retrying every tick.
* A camera that comes back at a different resolution after a reconnect is
  **resized into the open writer**, not given a new file: a new writer mid-clip
  truncates the clip the operator is watching grow.
* **Disk guard**: every 5 s, on the encoder thread that is about to make the
  file bigger, stop writing if free space < `RECORD_MIN_FREE_MB` (512). A full
  root filesystem does not just lose the recording — it takes X, the viewer and
  the SSH session with it.
* Force even dimensions anywhere you scale; most encoders reject odd sizes, and
  `cv2.VideoWriter` reports that by quietly not opening.

---

## 6. Clip skew — capture it or the merge is wrong

Each writer opens on **its own camera's first frame**, so two files from one
clip can start seconds apart (1 s measured on a real session: 143 frames against
128). Publish the monotonic instant of each clip's first write, and when a clip
closes, record each camera's offset from the earliest as a lead-in.

Capture the skew **before** rolling over to the next clip as well as on stop —
opening the next clip clears the first-write instants on the encoder thread's
next tick, and that is the last instant the banked clip's skew exists anywhere.
Get this wrong and, in a run of five clips, only the fifth is aligned; the other
four hstack two different moments and call it one frame.

---

## 7. The post-save pipeline (runs only on a KEEP)

Nothing heavy runs while recording. A discarded run must cost **zero** CPU.
Start one background builder for the session, and per clip do these in order:

### a) Normalise the per-camera masters, in place

Why: the two masters must come out the same **size**, not merely the same pixel
dimensions, and they must be shareable. Real complaints, in order: *"one file
store too much size and other low"*; *"file extension is not supported for
share, but merged video is"* (all three were `.mp4` — the difference was the
codec: the masters were MPEG-4 Part 2, which phones stopped accepting years
ago); then *"the merged video was perfect to store and share but single cam was
not"* (both were H.264 by then — the difference was Main profile with B-frames
against Constrained Baseline).

```
ffmpeg -hide_banner -loglevel error -y -nostdin -i <master>
  -c:v libx264 -preset veryfast
  -profile:v baseline -level 3.2
  -b:v 1200k -minrate 1200k -maxrate 1200k -bufsize 1200k
  -x264-params nal-hrd=cbr:force-cfr=1
  -r 15 -pix_fmt yuv420p -movflags +faststart
  [-frames:v <shortest camera's frame count>]
  -f mp4 <tmp>
```

* **CBR, not CRF, deliberately.** CRF gives equal *quality* and lets the busier
  scene produce the bigger file — measured 1.56 MB against 1.29 MB for one clip,
  which is the behaviour being complained about. `nal-hrd=cbr` makes x264 pad to
  the target instead. The cost is real and is the operator's call: at a fixed
  rate the camera looking at more detail gets less of it. 1200k is measured, not
  guessed — mp4v was spending 2.2–2.8 Mbit/s on the same frames.
* **Baseline, not Main.** B-frames are the expensive part to decode and the part
  simple players get wrong. At CBR the file size does not move; what is spent is
  a little quality for a file that opens on the first try.
* **Trim to the shortest camera in the clip.** Equal bitrate only gives equal
  size over equal length, and each recorder runs its own tick loop — one clip
  closed at 87 frames on CAM 1 and 91 on CAM 2. Trim from the **tail**, so the
  head alignment the merge depends on is untouched.
* **Temp name must be a dotfile** (`.<name>.norm`): the USB daemon skips
  dotfiles, so a stick plugged in mid-re-encode cannot copy a half-written
  master.
* **Probe the temp before it is allowed to overwrite anything.** A zero-byte or
  headerless result replacing a good master turns a cosmetic step into the one
  thing this must never do. Then `os.replace`.
* Timeout `RECORD_NORM_TIMEOUT_S` (900 s) so a wedged ffmpeg cannot hang a save.
* Never fatal, never destructive: a master that cannot be re-encoded is left
  byte-for-byte as it was and reported. The master is the record; matching it to
  its sibling is a convenience, and no convenience is worth the only copy of a
  duct run.

### b) Build the merged view `full_nnn.mp4`

This is the file handed to whoever asks for "the video", singular. The
per-camera masters stay, because they are the masters.

**Build it after the save, not live.** As a third live encoder it was wrong on
both counts: it scaled both cameras to 480 (a live 720 encoder does not fit) and
gave each tile a width from its own aspect ratio, so with CAM 2 rotated the
tiles came out 852 px + 270 px — a 76/24 split, measured at 1122×480. On this
Pi 4 a live 50/50 canvas at stream resolution is not available at any setting:
2560×1280 encodes at 9.4 fps against the 15 it needs, and the hardware H.264
encoder tops out at 1920×1920 so it cannot help. Built from the finished files
it costs nothing while recording, gets both cameras at native size, and is only
spent on runs the operator kept.

* **Geometry**: half-width = the widest camera, canvas height = the tallest,
  both forced even. Each camera is scaled with
  `force_original_aspect_ratio=decrease` and **padded centred on black** into
  its half — nothing upscaled, nothing stretched, and because both halves are
  the same box by construction the 50/50 cannot drift with aspect ratio.
  1280×720 beside 720×1280 → a **2560×1280** canvas. `COMBINED_MAX_HALF` caps
  the half-width by scaling the whole canvas, so the 50/50 survives.
* **Alignment**: prepend each input's own skew with
  `tpad=start_duration=<lead>:start_mode=add:color=black`. `add` prepends real
  frames rather than shifting timestamps, which is what `hstack`'s frame pairing
  actually reads.
* **Labels burned into the pixels** with `drawtext` (DejaVu, white, `borderw=3`
  black, `fontsize=max(18, canvas_h//32)`, at 16,14): this file gets copied to a
  stick and watched on someone else's laptop, so FRONT/BACK has to travel inside
  the video, not in metadata. If no font exists, build it unlabelled — an
  unlabelled full view is far better than no full view.
* `hstack=inputs=N`, then
  `-r 15 -c:v libx264 -preset ultrafast -crf 28 -profile:v baseline -level 3.2
   -movflags +faststart -pix_fmt yuv420p -progress pipe:1 -nostats -f mp4`.
  **State the profile explicitly** even though `ultrafast` happens to disable
  CABAC and B-frames: anyone retuning the preset for smaller files would
  otherwise silently turn the one reliably shareable file on the card into a
  Main-profile one, with nothing to say it had happened until a handset refused
  it.
* **Write to `full_nnn.mp4.part` and `os.replace` on success.** The USB daemon
  scans on its own 2 s clock and will happily copy a file that is still growing;
  under the final name that would put a truncated video on the stick and, worse,
  one whose size then matches on the next insertion.
* Parse `-progress` output for a live fraction against the expected padded frame
  count. Timeout `COMBINED_TIMEOUT_S` (3600 s).
* On abort **or** non-zero exit, delete the `.part`. Do not `return` out of the
  progress loop and skip the cleanup: a stranded `.part` is read by the backup
  daemon as "the recorder is still working" and will hold a stick waiting for a
  build that died minutes ago.
* Failure is per-clip and non-destructive: report it, skip that clip, keep
  building the rest.

**Order matters**: normalise first, so the merge is built from the equal-length
files. Otherwise hstack still pairs a frame from one camera against a moment the
other never recorded.

**Queue, never kill.** One build at a time — two would fight over all four cores
and both finish later. A save landing while a build runs is **queued**; an
operator who saved a run and immediately started the next one used to have the
first session's merge killed halfway, so that session reached the stick with no
full view at all. Drive the queue off an explicit `building` flag, not
`thread.is_alive()`: the completion callback fires from inside `run()` while the
thread is still alive, so a save in that instant would queue behind it and never
be popped.

---

## 8. Status contract and what the GUI must say

`SessionManager.status()` returns one dict per UI frame, and `main.py` bolts the
USB daemon's dict onto it as `status["usb"]` so the transfer appears exactly
where the operator already looks for recording state:

```python
{
  "state": "RECORDING" | "PAUSED" | "STOPPED" | "SAVE?",   # SAVE? outranks the switch
  "pending_left": float | None,        # seconds left to claim
  "pending_held": float | None,        # how long the pending run was
  "pending_clips": int | None,
  "save_hold": float | None,           # live hold progress, so the strip can count down
  "save_hold_need": float,             # 3.0
  "elapsed": float, "clip": int, "clip_elapsed": float,
  "dir": str | None, "cameras": [ ... ], "bytes": int,
  "error": str | None,                 # first camera error, or None
  "free_mb": float | None,
  "toast": (text, detail) | None,      # expires after SAVE_TOAST_S = 6 s
  "full_view": {                       # None until a session has been kept
     "state": "queued"|"normalising"|"building"|"done"|"error",
     "clip": int, "clips_total": int, "built": int, "frac": float,
     "error": str | None, "queued": int,
     "ready": bool,                    # see below
  },
  "usb": { ... },                      # bolted on by main.py, {} when absent
}
```

`ready` = the build finished (`done` or `error`), nothing is queued behind it,
**and** no verified backup has run since it finished (compare the build's finish
time against `.session_reset`). This is the operator-facing half: processing has
finished and nothing has taken the footage away, so *this* is the moment to plug
the USB in. Without it the UI went straight from MERGING back to idle, and the
only way to know the merge had finished was to guess — which is how sticks got
plugged in mid-build and went home without the merged file.

**Two display surfaces, one vocabulary.** The REC chip lives in the top bar
because that is the nearest place to the picture; the strip in the side panel
carries the sentence.

| Phase | REC chip | Strip |
|---|---|---|
| Rolling | `REC  1:14`, blinking ~1.4 Hz | recording + clip count |
| Paused | `PAUSED  1:14` with `❚❚` | paused |
| Window open | `SAVE?  7s`, blinking | `hold SAVE 3s to keep · 2 clips 1:14` |
| Button down | `SAVE?  7s` | `keep holding SAVE · 2.4s` |
| Kept | — | `SAVED   2 clips  1:14` (toast) |
| Discarded | — | `DISCARDED  2 clips 1:14 (not saved)` in **recording-red**, not amber — footage was deleted and that must not look like a save that found nothing |
| Normalising | `PROCESSING  42%` | `PROCESSING VIDEO 42% · clip 1/3 · wait before plugging in USB` |
| Merging | `MERGING  61%` | `MERGING CAMERAS INTO ONE VIDEO 61% · clip 1/3 · wait before plugging in USB` |
| Ready | `READY  PLUG USB` | `READY TO TRANSFER · PLUG IN USB · 3 merged videos` |
| Merge failed | `MERGE FAILED` | `MERGE FAILED · <reason> · camera files are safe` |
| Nothing happening | `STANDBY` | `idle · 12.4 GB free` |

USB gets its **own chip**, not another line in the REC chip: the transfer and
the recording are two different questions and both get asked at once every time
a stick goes in.

| Daemon state | USB chip | Strip |
|---|---|---|
| no daemon / status >10 s stale | `USB  —` | — |
| idle, watching the bus | `USB  READY` (`○`) | — |
| detected / mounting | `USB  OPENING` | `USB FOUND · do not remove` / `OPENING USB · do not remove` |
| scanning | `USB  CHECKING` | `CHECKING WHAT TO COPY · do not remove` |
| copying | `USB  COPYING 38%`, blinking | `COPYING TO USB 38% · <file> · 4/11 files` |
| finishing (settle) | `USB  FINISHING` | `FINISHING VIDEO FILES · DO NOT REMOVE USB` |
| clearing | `USB  CLEARING` | `FREEING SPACE ON PI · copy verified · 11 files` |
| done | `USB  COMPLETE` | `COPY COMPLETE · SAFE TO REMOVE USB · 11 files copied, 11 freed off Pi` |
| error | `USB  FAILED` | `COPY FAILED · <detail> · nothing deleted from Pi` |

**Strip precedence**, highest first: toast → confirm window → USB busy → USB
done/error → normalising/merging → ready → merge error → camera error → idle.
Operator decisions in flight outrank machine progress, and "do not pull the
stick" outranks everything routine.

Rules for the UI layer:

* Blink off the **wall clock** (`int(monotonic()*rate) % 2`), never off a frame
  counter, or the blink rate follows the frame rate when the Pi is busy.
* Name **every** phase, including the short ones. Detect, mount and the copy
  plan are seconds each on a full stick, and a chip that goes blank through them
  reads as "the stick did not take" at precisely the moment it must not. An
  unknown state must render as busy, not vanish.
* `STOPPED` does not mean FINISHED — while the post-save pipeline runs, the chip
  must not say STANDBY. That is exactly what sends an operator to the USB socket
  while ffmpeg is still writing.
* Drawing the strip must never be able to strand the robot: wrap the UI update
  in `try/except` and keep the motor demand below it. A partial ADC read once
  left a value `None`, the panel's f-string raised on every frame, and the
  wheels kept driving on a stale stick position.

---

## 9. The USB backup daemon

Its own **root** systemd unit, never inside the viewer: mounting block devices
needs root and the viewer must never have it. The two meet in exactly two
places — the viewer writes into `/recordings`, and the daemon publishes JSON to
`/run/usb_backup_status.json` (tmpfs: root-writable, world-readable, gone on
reboot, all three correct for live status). Stamp every publish with `updated`;
the viewer re-reads at 1 Hz and treats anything older than 10 s as **no daemon**
rather than showing a strip stuck on "transferring" after the daemon died.

Cycle per insertion:

1. **detect** — poll `lsblk` every 2 s. A partition counts only if its disk
   arrived over USB transport; never `/dev/mmcblk*` (the SD card the Pi runs
   from), `/dev/loop*`, `/dev/ram*`, `/dev/zram*`, `/dev/dm-*`. Publish
   `detected` the instant the node is seen. Remember a handled device until its
   node disappears, so leaving the stick in does not loop; replug re-triggers.
2. **mount** — at `/media/usb_backup-<dev>`, unless an automounter got there
   first, in which case use its mount point. Accept vfat/exfat/ntfs/ext2/3/4;
   log and ignore anything else. For FAT-family pass `uid=1000,gid=1000` so the
   files are readable as the viewer's user, plus a computed `time_offset` so the
   copies carry the right Date Modified on a Windows laptop.
3. **scan** (`scanning`) — walk `/recordings` once and return *all* files (the
   clear pass needs the complete list), the subset to copy, and its byte total.
   **Incremental**: same relative path and same size on the stick = skip.
   **Never copy dotfiles or `.part`/`.norm`** — those are the recorder's and the
   daemon's scratch, not footage; copying one puts a truncated file on the stick
   and the clear pass would then delete state the recorder still needs.
4. **copy** (`copying`) — into `<mount>/recordings/...`, same structure and
   names, 4 MB chunks, `copystat` best-effort. Publish file name, index, file
   count and bytes at most **once a second** — the viewer polls at 1 Hz and
   per-chunk publishes are pure churn.
5. **settle** (`finishing`) — the step that exists because of a measured loss.
   On 2026-08-20 a stick plugged in the moment SAVE was confirmed was declared
   "done, safe to remove" at 11:28:29.32 — and the recorder then rewrote cam1 at
   11:28:29.83, cam2 at 11:28:36.39 and wrote the merged `full_001.mp4` at
   11:28:45.72. All three re-scans had run inside the same second, found nothing
   new, and called it settled: the stick went home with two stale masters, no
   merged video, and a reset marker claiming everything had transferred.

   So re-scan **and wait**. The transfer counts as settled only when there is
   nothing left to copy, no `.norm`/`.part` temporaries, **and** the tree has
   been untouched for `SETTLE_QUIET_S` (10 s). Files that appear during the
   transfer are copied, up to `SETTLE_PASSES` (3) extra passes. Two different
   caps, because there are two different things you can be waiting for: a
   temporary present is proof of work that **will** finish, so wait up to
   `SETTLE_BUILD_MAX_S` (900 s); anything else might be an active recording,
   which never converges, so wait only `SETTLE_QUIET_MAX_S` (25 s) — enough to
   cover the gaps between the recorder's ffmpeg stages. A temporary that has not
   been touched for `SETTLE_TEMP_STALE_S` (120 s) is debris from a killed
   viewer, not live work, and must not hold the stick. Exclude the reset marker
   from the quiet clock — the daemon writes it, so counting it would mean every
   transfer restarted its own window. Hitting a bound simply means the reset is
   not earned this time and the stragglers go on the next insertion.
6. **clear** (`clearing`) — **the Pi's copies are then deleted.** The stick is
   the recording's destination; the Pi is only a buffer. Per file, delete only
   if the stick's copy exists and matches the source's size **now** (not at plan
   time — a file that grew during the copy fails the compare and is kept), and
   only if the resolved path is genuinely inside `RECORD_DIR`. An **unsettled**
   transfer adds a third guard: the source must not have been written for
   `ACTIVE_GRACE_S` (30 s), so a session still being recorded survives. A
   **settled** transfer drops that guard — settling already proved the same
   thing and more, and keeping it meant the merged file, written seconds before
   the transfer finishes by construction, failed the age test every single time
   and `/recordings` never emptied. Any copy error skips the clear entirely.
   `os.remove` per known file, then bare `rmdir` on directories that emptied.
   No `rmtree`. `USB_BACKUP_DELETE=0` disables clearing for bring-up.
7. **finish** — append a receipt to `BACKUP_INFO.txt` on the stick (when, how
   many copied / already present / errors / cleared, how long), `os.sync()`,
   then **unmount**. That unmount is the point: when the UI says COMPLETE, the
   stick is *already* safe to pull. If unmount fails, say so and leave it
   mounted — better a mounted stick than a corrupted one, and the data is
   already synced.
8. **numbering reset** — if and only if there were **no errors** *and* the
   transfer **settled**, write the finish time into `RECORD_DIR/.session_reset`.
   Both conditions are load-bearing: no-errors means everything offered to the
   stick landed, settled means nothing was left un-offered. Neither alone is
   "all data transferred". Leave the marker in place rather than consuming it,
   so the reset survives a viewer restart.

Log everything to a capped log file. Provide `--once` for a single verbose scan
so it can be exercised without hardware.

---

## 10. Housekeeping the first version will forget

* **Sweep orphans at startup.** A SIGTERM mid-encode — every pass of the
  supervision loop, 49 of them in one afternoon — leaves `.norm`/`.part` behind.
  Nothing comes back for them, the backup deliberately never copies them, and
  `rmdir` cannot take the folder while they sit in it. One real session ended up
  as a folder with no video, 1.6 MB of orphan, and no path that could ever
  remove either. Sweep both patterns at start-up with a 60 s mtime guard (so a
  build belonging to another process survives), then `rmdir` whatever emptied.
* **Delete session directories that never got a frame.** With the cameras off
  the wire, switch bounce produced three dated empty folders in ten seconds
  during bring-up. A folder per non-recording makes the recording list a place
  you have to sort real runs out of. `rmdir` only — it refuses on a directory
  holding anything, so it cannot delete footage even if the frame counts lie.
* Name the stop `Event` anything but `self._stop`: `threading.Thread` already
  has a private `_stop()` that `join()` calls, and shadowing it makes `join()`
  die with *"'Event' object is not callable"* — at shutdown, the worst place to
  find it.
* Load `cv2` lazily. It is ~2 s of import on a Pi 4 and every second is spent
  before the splash can paint; only the encoder thread needs it.
* Fire the clip-save on **counted edges**, and prime the counter on the first
  call — a viewer restarted mid-session must not fire a save it was never asked
  for. Compare with `>`, not `!=`, so a counter reset to 0 when the reader loses
  the pins is a no-op rather than a phantom press.
* Everything below the public API must be idempotent: `main.py` calls
  `set_state()` thirty times a second with the same value and it must cost
  nothing while still keeping the elapsed clock honest between transitions.

---

## 11. Config — every tunable, with its default

| Name | Default | Meaning |
|---|---|---|
| `RECORD_DIR` | `/recordings` | session root; also what the daemon mirrors |
| `RECORD_FPS` | `15` | wall-clock write rate |
| `RECORD_FOURCC` / `RECORD_EXT` | `mp4v` / `.mp4` | live encoder |
| `RECORD_SQUARE_PX` | `720` | common centre-crop square, `0` = native |
| `RECORD_MAX_WIDTH` | `0` | optional downscale before encoding |
| `RECORD_MIN_FREE_MB` | `512` | stop writing rather than fill the card |
| `RECORD_CONFIRM_S` | `10` | the window after STOP; **`0` = keep everything** |
| `RECORD_SAVE_HOLD_S` | `3.0` | hold to claim a stopped run |
| `SAVE_TOAST_S` | `6.0` | how long SAVED / DISCARDED stays up |
| `RECORD_NORMALIZE` | `1` | re-encode masters after a save |
| `RECORD_NORM_BITRATE` | `1200k` | CBR target |
| `RECORD_NORM_PRESET` | `veryfast` | |
| `RECORD_NORM_PROFILE` / `_LEVEL` | `baseline` / `3.2` | |
| `RECORD_NORM_MATCH_FRAMES` | `1` | trim to the shortest camera |
| `RECORD_NORM_TIMEOUT_S` | `900` | |
| `COMBINED_AFTER_SAVE` | `1` | build `full_nnn.mp4` after a save |
| `COMBINED_MAX_HALF` | `0` | cap tile width, `0` = native |
| `COMBINED_VCODEC` / `_PRESET` / `_CRF` | `libx264` / `ultrafast` / `28` | |
| `COMBINED_PROFILE` / `_LEVEL` | `baseline` / `3.2` | stated, never inherited |
| `COMBINED_TIMEOUT_S` | `3600` | |
| `RECORD_COMBINED` / `COMBINED_HEIGHT` | `0` / `480` | the old live encoder, kept for the headless test |
| `CAM_LABELS` | `FRONT,BACK` | drives labels, chips and file names |
| `USB_STATUS_PATH` | `/run/usb_backup_status.json` | |
| `USB_BACKUP_DELETE` | `1` | clear the Pi after a verified transfer |
| `USB_BACKUP_POLL_S` | `2.0` | lsblk poll |
| `USB_BACKUP_QUIET_S` | `10` | quiet before "settled" |
| `USB_BACKUP_SETTLE_PASSES` | `3` | extra copy rounds |
| `USB_BACKUP_BUILD_MAX_S` | `900` | cap while a temporary proves live work |
| `USB_BACKUP_QUIET_MAX_S` | `25` | cap when nothing explains the writes |
| `USB_BACKUP_TEMP_STALE_S` | `120` | a temporary older than this is debris |
| `USB_BACKUP_GRACE_S` | `30` | unsettled transfers keep files this fresh |

Every one of them read from the environment with the default inline, and every
non-obvious default carrying the measurement that produced it as a comment.

---

## 12. Acceptance checks

1. **Headless smoke test**: `python3 recorder.py rtsp://<cam1> rtsp://<cam2>`
   records 10 s, pauses 3 s, saves, stops — with no GPIO and no Qt.
2. Stop a 30 s run and do nothing → after 10 s the strip reads `DISCARDED`, the
   session directory is gone, and nothing outside it was touched.
3. Stop and hold SAVE for 3 s → `SAVED`, then `PROCESSING`, then `MERGING`, then
   `READY PLUG USB`. Both masters end up within a few percent of each other in
   size, `ffprobe` reports `h264 / Constrained Baseline` for all three files, and
   both play on a phone.
4. Start the hold on the **last second** of the window → it still lands.
5. Tap SAVE three times mid-run → clips 001–004 exist for both cameras, and
   every `full_00n.mp4` has the two cameras in step, not just the last one.
6. Save two runs back to back → the second build **queues**; both sessions end
   up with a full view.
7. `ffprobe full_001.mp4` → 2560×1280, tiles exactly 1280 each, FRONT/BACK
   legible, no drift between halves.
8. Pull the power mid-merge, restart → the orphan `.part` is swept and the
   session folder does not survive empty.
9. Plug a stick in **during** a merge → the strip says `FINISHING VIDEO FILES ·
   DO NOT REMOVE USB`, and the merged file that lands after the scan still
   reaches the stick before COMPLETE.
10. Complete a clean transfer → `/recordings` is empty except `.session_reset`,
    the stick holds `recordings/` plus `BACKUP_INFO.txt`, it is already
    unmounted, and the **next** recording is `SESSION001`.
11. Yank the stick mid-copy → the daemon reports the error and **nothing is
    deleted from the Pi**.
12. Kill the daemon → within 10 s the USB chip falls back to `USB  —`, not a
    frozen percentage.
