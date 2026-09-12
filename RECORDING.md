# Recording, saving and USB transfer — how it actually works

Notes taken from the code on 2026-08-25 (`ground_station/recorder.py`,
`config.py`, `inputs.py`, `inputs_panel.py`, `topbar.py`, `main.py`,
`usb_backup.py`). Everything below is what the tree does, not what it should do.

---

## 1. Controls

| Control | Pin | What it does |
|---|---|---|
| Switch 1, red leg | GPIO22 | START / STOP the session |
| Switch 2, green leg | GPIO11 | PAUSE / RESUME inside a session |
| SAVE button | GPIO9 (momentary, ~0.18 s pulse) | **tap while ROLLING** = bank the clip and keep recording. **Hold 3 s after STOP** = keep the whole session |
| Keyboard (bench only) | `Space` / `P` / `Ctrl+S` | start-stop / pause / save. `Ctrl+S` calls `finalize()` first, falling back to `save_clip()` |

`inputs.py` polls the pins at 20 Hz and publishes two different things for the
one button, because they are two different gestures:

* `save_presses` — a running count of open→closed **edges**. A 0.18 s pulse is
  invisible to a 30 fps UI sampling levels, so edges are counted in the reader
  and `SessionManager.on_save_button()` fires once per increment.
* `save_held_s` — how long the button has been continuously **held**. That is
  what the 3 s claim runs on.

Hardware always wins: if the panel is readable, the keyboard latch is ignored
(`main._session_state`).

---

## 2. The state machine

```
STOPPED ──start──▶ RECORDING ⇄ PAUSED ──stop──▶ SAVE?  (10 s window)
                       │                          │
                    tap SAVE                 hold SAVE 3 s ──▶ KEPT ──▶ post-save build
                  (bank clip,                     │
                   keep rolling)              silence ──▶ DISCARDED (files deleted)
```

`PENDING` / `"SAVE?"` is a fourth state that no switch produces — it exists only
in `recorder.py` and it outranks the switch state in `status()`, because the
thing the operator has to act on is the unanswered question, not the lever.

Time in PAUSE is not written at all, so pause is a **cut**, not a freeze.
Frames are written on a wall clock (one per tick at `RECORD_FPS=15`, whether or
not the camera delivered a new one), so an hour of duct run is an hour of video
and a 20 s camera dropout leaves 20 s of held frame instead of a jump cut.

---

## 3. The 10-second window and the 3-second hold

This is the part that can lose footage, so the exact rules:

**Stopping does not keep the run.** On STOP (`_stop_session`):

* the clocks stop, the clip closes, camera skew is captured;
* if the run was shorter than **1.0 s**, or no camera ever delivered a frame, or
  `RECORD_CONFIRM_S=0` → no question is asked (short/empty runs are dropped,
  `RECORD_CONFIRM_S=0` auto-keeps and still runs the full build pipeline);
* otherwise a `_pending` record is created with `until = now + RECORD_CONFIRM_S`
  — **10 seconds** (`config.RECORD_CONFIRM_S`, was 15, cut to 10 on 2026-08-19).

**During those 10 s:**

* the REC chip in the top bar reads `SAVE?  7s` and blinks at ~1.4 Hz;
* the session block's clock becomes a countdown, and the strip reads
  `hold SAVE 3s to keep · 2 clips 1:14`;
* the moment the button goes down the strip switches to
  `keep holding SAVE · 2.4s`, counted down live;
* a **tap** during the window does nothing at all, deliberately — no toast, so
  it cannot cover the live countdown. The same button banks clips mid-run, so a
  stray tap must not be able to commit a whole session.

**Claiming it** — `on_save_hold(held_s)` is pushed every UI frame:

* at `held_s >= RECORD_SAVE_HOLD_S` (**3.0 s**) → `_resolve_pending(keep=True)`;
* while the button is down but short of 3 s, the deadline is pushed out to
  `now + (3 - held) + 1.0 s`, so **a hold started on the last second still
  lands** rather than the countdown deleting the files mid-hold.

**Not claiming it** — `SessionManager.poll()` (called once per UI frame from
`main.tick`, deliberately not from `status()`, because a getter that deletes
files is one nobody can call safely) fires `_resolve_pending(keep=False)`:

* waits up to 1.5 s for every encoder to release its file (`_await_closed`);
* `os.remove()` on **exactly the paths this session handed out**, then a bare
  `os.rmdir()` — never `rmtree`, anywhere in this codebase;
* strip shows `DISCARDED  2 clips 1:14  (not saved)` in recording-red, or
  `DISCARD FAILED  1 file still on disk` if a delete failed — it will not claim
  a discard that did not happen.

Two other exits from the window:

* **starting a new run** while one is unclaimed discards the old one with reason
  `superseded`;
* **shutting the viewer down** *keeps* it. The rule is "unclaimed footage is
  discarded", and footage nobody was given the chance to claim is not unclaimed.

---

## 4. What lands on disk

```
/recordings/20260815_134500_SESSION001/
        cam1_front_001.mp4      <- master, CAM 1 = FRONT
        cam2_back_001.mp4       <- master, CAM 2 = BACK
        full_001.mp4            <- both cameras side by side, built after SAVE
        cam1_front_002.mp4      <- clip 002, from a SAVE tap mid-run
        ...
```

* One directory per session, `YYYYMMDD_HHMMSS_SESSIONnnn`, under
  `RECORD_DIR=/recordings` (root-level, so the USB daemon has one fixed thing to
  mirror). One file per camera per clip.
* `SESSIONnnn` is `1 + highest on disk`, rescanned from the directory listing on
  every start — but **only counting sessions newer than the last verified USB
  backup**, so a stick that took the footage away resets numbering to 001.
* **Live encoding**: each camera is centre-cropped to a common **720×720 square**
  (`RECORD_SQUARE_PX`) and written by `cv2.VideoWriter` with fourcc `mp4v` at
  15 fps. The square is because the two cameras are mounted 90° apart — cropping
  is the only way both files come out genuinely identical in size and shape at
  native pixel density. `mp4v` is the only fourcc the Pi's OpenCV can keep up
  with (measured: `avc1` in-process collapsed to ~3 fps and left a file with no
  moov atom).
* Recording re-encodes the frames **already decoded for the screen** rather than
  opening a second RTSP session per camera — these cheap IP cameras cap
  concurrent sessions, and what lands on disk is then provably what the operator
  saw. Cost: ~0.4 core per camera on top of the ~0.6 the decode takes.
* Writing stops rather than filling the card: `RECORD_MIN_FREE_MB=512`, checked
  every 5 s by the encoder thread itself.

---

## 5. After the save: the post-save pipeline

Nothing heavy runs while recording. `FullViewBuilder` starts **only on a keep**
— a discarded run costs zero CPU. Per clip, in order:

**a) Normalise the masters** (`RECORD_NORMALIZE=1`) — ffmpeg re-encodes each
per-camera file *in place* to:

* H.264 **baseline**, level 3.2 (`RECORD_NORM_PROFILE/LEVEL`), `+faststart`;
* **constant** bitrate 1200k with `nal-hrd=cbr:force-cfr=1` — CBR not CRF, so
  the two cameras produce files of the same *size*, not merely the same
  dimensions;
* trimmed from the tail to the **shortest camera's frame count**
  (`RECORD_NORM_MATCH_FRAMES`), because equal bitrate only gives equal size over
  equal length — one measured clip closed at 87 frames on CAM 1 and 91 on CAM 2.

  Why baseline: the masters used to be MPEG-4 Part 2 (`mp4v`), which phones and
  messaging apps refuse; then they were Main profile with B-frames, which some
  handsets still refused. Baseline/3.2 is what demonstrably travels, and the
  merged file is pinned to the same thing so the two are not different kinds of
  H.264.

  Safety: written to a hidden `.<name>.norm` temp (dotfiles are skipped by the
  USB daemon), probed for readability, and only then `os.replace`d over the
  master. A re-encode that fails leaves the master byte-for-byte untouched.

**b) Build the merged view** (`COMBINED_AFTER_SAVE=1`) → `full_nnn.mp4`:

* both cameras **side by side, 50/50** — each half is as wide as the widest
  camera and as tall as the tallest, centred on black. Nothing scaled, nothing
  stretched. With CAM 1 at 1280×720 and CAM 2 rotated to 720×1280 that is a
  **2560×1280** canvas. (`COMBINED_MAX_HALF=0` = native; set e.g. 960 to cap.)
* each tile gets **FRONT / BACK burned into the pixels** (`drawtext`, DejaVu) —
  this file gets copied to a stick and watched on someone else's laptop, so the
  label has to travel inside the video, not in metadata.
* each input is padded at the head by its own **start skew** (`tpad`), because
  each writer opens on its own camera's first frame — 1 s of skew was measured
  on SESSION009, which hstacked blind would put two different moments side by
  side and call it one frame.
* libx264, ultrafast, crf 28, baseline/3.2, `+faststart`.
* written as `full_nnn.mp4.part` and `os.replace`d on success, so the USB daemon
  (which scans on its own 2 s clock) only ever sees no file or a finished one.

Ordering matters: masters are normalised **first**, so the merge is built from
the equal-length files.

**Queueing**: one build at a time. A save landing while a build is running is
**queued**, never allowed to kill it — an operator who saves and immediately
starts the next run used to end up with the first session on the stick with no
merged view at all.

Failure is always non-destructive: a clip that cannot be built is reported and
skipped, the rest of the session still builds, and the masters are the record.

---

## 6. What the GUI shows, end to end

Two places, kept in the same vocabulary: the **REC chip** in the top bar (next
to the picture) and the **detail strip** in the session block.

| Phase | REC chip | Strip |
|---|---|---|
| Rolling | `REC  1:14` (blinks) | recording, clip count |
| Paused | `PAUSED  1:14` (`❚❚`) | paused |
| Window open | `SAVE?  7s` (blinks) | `hold SAVE 3s to keep · 2 clips 1:14` |
| Button down | `SAVE?  7s` | `keep holding SAVE · 2.4s` |
| Kept | — | `SAVED   2 clips  1:14` (toast, 6 s) |
| Not kept | — | `DISCARDED  2 clips 1:14 (not saved)` (red) |
| Re-encoding masters | `PROCESSING  42%` | `PROCESSING VIDEO 42% · clip 1/3 · wait before plugging in USB` |
| Merging | `MERGING  61%` | `MERGING CAMERAS INTO ONE VIDEO 61% · clip 1/3 · wait before plugging in USB` |
| Build finished, nothing has taken it away | `READY  PLUG USB` | `READY TO TRANSFER · PLUG IN USB · 3 merged videos` |
| Build failed | `MERGE FAILED` | `MERGE FAILED · <reason> · camera files are safe` |

USB gets its **own chip** (transfer and recording are two different questions,
and both get asked at once every time a stick goes in):

| Daemon state | USB chip | Strip |
|---|---|---|
| no daemon / stale status | `USB  —` | — |
| idle, watching | `USB  READY` (`○`) | — |
| detected / mounting | `USB  OPENING` | `USB FOUND · do not remove` / `OPENING USB · do not remove` |
| scanning | `USB  CHECKING` | `CHECKING WHAT TO COPY · do not remove` |
| copying | `USB  COPYING 38%` (blinks) | `COPYING TO USB 38% · full_001.mp4 · 4/11 files` |
| finishing (settle) | `USB  FINISHING` | `FINISHING VIDEO FILES · DO NOT REMOVE USB` |
| clearing | `USB  CLEARING` | `FREEING SPACE ON PI · copy verified · 11 files` |
| done | `USB  COMPLETE` | `COPY COMPLETE · SAFE TO REMOVE USB · 11 files copied, 11 freed off Pi` |
| error | `USB  FAILED` | `COPY FAILED · <detail> · nothing deleted from Pi` |

Precedence in the strip: toast → confirm window → USB busy → USB done/error →
processing/merging → ready → merge error → camera error → idle. Operator
decisions in flight outrank machine progress; "do not pull the stick" outranks
everything routine.

`READY TO TRANSFER` is the load-bearing one: it means processing has finished
**and** no backup has run since. Without it the chip went from MERGING straight
back to STANDBY, and that is how sticks got plugged in mid-build and went home
without the merged file.

---

## 7. The USB transfer (separate root daemon)

`usb-backup.service` runs as root — mounting block devices needs root and the
viewer must never have it. The two meet in exactly two places: the viewer writes
into `/recordings`, the daemon publishes to `/run/usb_backup_status.json`
(tmpfs, stamped `updated`; the viewer re-reads it at 1 Hz and treats anything
older than 10 s as *no daemon* rather than showing a stuck "transferring").

Per insertion:

1. **detect** — `lsblk` poll every 2 s; USB-transport partitions only, never
   `/dev/mmcblk`, loop, ram, zram, dm.
2. **mount** — `/media/usb_backup-<dev>` (or an automounter's mount point).
   FAT/exFAT get `uid=1000,gid=1000` and a `time_offset` so Date Modified is
   right on a Windows laptop.
3. **scan** — walk `/recordings`, plan the copy. **Incremental**: same path and
   same size = skipped. Dotfiles and `.part`/`.norm` temporaries are never
   copied — a half-written file is not footage.
4. **copy** — `<stick>/recordings/...`, same structure and names, 4 MB chunks,
   byte progress published at most once a second.
5. **settle** — publishes `finishing`. Re-scans, and **waits**: nothing counts
   as transferred until the tree holds no `.norm`/`.part` temporaries and has
   been untouched for **10 s**. Files that appeared during the copy get copied
   (up to 3 extra passes). Bounded: 900 s if a temporary proves work is still in
   flight, 25 s otherwise — a live recording never converges, and hitting a
   bound just means the stragglers go on the next insertion.

   This step exists because of a measured loss on 2026-08-20: a stick plugged in
   the instant SAVE was confirmed was declared "done, safe to remove" at
   11:28:29 — and the recorder then rewrote cam1 at 11:28:29.8, cam2 at 11:28:36
   and the merged `full_001.mp4` at 11:28:45. The stick went home with two stale
   masters and no merged video.
6. **clear** — publishes `clearing`. **The Pi's copies are then deleted** (spec
   2026-08-18: the stick is the destination, the Pi is only a buffer). Per file,
   only if the stick's copy exists and matches the source's size *now*. A
   settled transfer deletes with no age guard; an unsettled one additionally
   requires the source to be 30 s untouched, so an active recording survives.
   Any copy error skips the clear entirely. `os.remove` per known file, then
   bare `rmdir` on directories that emptied. `USB_BACKUP_DELETE=0` disables it.
7. **finish** — append `BACKUP_INFO.txt` to the stick, `sync`, **unmount**. When
   the strip says COMPLETE the stick is already safe to pull.

**Session numbering reset**: if and only if there were no errors *and* the
transfer settled, `.session_reset` is written into `/recordings` holding the
finish time. `recorder._next_session_no` skips sessions older than that marker,
so the next recording is **SESSION001** again. Sessions deliberately held back
(inside the 30 s grace, or built after the scan) are *skipped, never deleted* —
they are still owed a backup, and the directory name carries a timestamp as well
as the number so a second SESSION001 never collides with the first.

A handled device is remembered until its node disappears; replugging re-triggers.

---

## 8. Housekeeping / edge cases worth knowing

* **Orphan sweep at startup** (`_sweep_orphans`): a SIGTERM mid-encode (every
  pass of the `.xinitrc` supervision loop — 49 in one afternoon on 2026-08-20)
  leaves `.norm`/`.part` behind. Nothing ever came back for them, they can never
  be copied away, and `rmdir` could not take the folder with them. Swept on
  every start, with a 60 s mtime guard so a build under another process survives.
* **Empty session folders** are removed on stop (`_discard_if_empty`) — with the
  cameras off the wire, switch bounce produced three dated empty folders in ten
  seconds during bring-up.
* **Skew capture on every clip**: `save_clip()` captures the two cameras' start
  skew *before* rolling over, because `_begin_clip` wipes it. Without that, in a
  run of five clips only the fifth was aligned.
* **Camera resolution change mid-clip** (after a reconnect) is resized into the
  open writer rather than starting a new file — a new writer mid-clip would
  truncate what the operator is watching grow.
* **The live combined encoder still exists** (`CombinedView`, `RECORD_COMBINED`)
  but is **off by default**. A live 2560×1280 canvas measured 9.4 fps against
  the 15 it needs, and the hardware encoder tops out at 1920×1920. The merged
  file is built after the save instead.

---

## 9. Settings that change this behaviour

| Env var | Default | Effect |
|---|---|---|
| `RECORD_DIR` | `/recordings` | where sessions go; also what the USB daemon mirrors |
| `RECORD_CONFIRM_S` | `10` | the resting window after STOP. **0 = keep everything automatically** |
| `RECORD_SAVE_HOLD_S` | `3.0` | how long SAVE must be held to claim a stopped run |
| `RECORD_FPS` | `15` | wall-clock write rate |
| `RECORD_SQUARE_PX` | `720` | common centre-crop square; `0` = native per camera |
| `RECORD_FOURCC` | `mp4v` | live fourcc (the only one that keeps up) |
| `RECORD_NORMALIZE` | `1` | re-encode masters to equal-size baseline H.264 after a save |
| `RECORD_NORM_BITRATE` | `1200k` | CBR target for the masters |
| `RECORD_NORM_MATCH_FRAMES` | `1` | trim every camera to the shortest in the clip |
| `COMBINED_AFTER_SAVE` | `1` | build `full_nnn.mp4` after a save |
| `COMBINED_MAX_HALF` | `0` | cap each tile's width; `0` = native (2560×1280 canvas) |
| `COMBINED_PRESET` / `COMBINED_CRF` | `ultrafast` / `28` | merge encode speed vs size |
| `RECORD_COMBINED` | `0` | the *old* live side-by-side encoder |
| `RECORD_MIN_FREE_MB` | `512` | stop writing rather than fill the card |
| `USB_BACKUP_DELETE` | `1` | delete the Pi's copy after a verified transfer |
| `USB_BACKUP_QUIET_S` | `10` | tree must be untouched this long before "settled" |
| `USB_BACKUP_GRACE_S` | `30` | unsettled transfers keep files written this recently |
| `SAVE_TOAST_S` | `6.0` | how long SAVED / DISCARDED stays on screen |

---

## 10. One inconsistency found

`recorder.py` docstrings at lines 1404, 1448 and 1475 still say **GPIO25** for
the SAVE button. The pin moved 25 → 9 on 2026-08-18 (`inputs.SAVE_PIN = 9`), and
the module docstring at line 13, `main.py` and the panel all say GPIO9. Comments
only — no behaviour depends on it, since the recorder never names a pin, it just
consumes `save_presses` / `save_held_s`. Worth fixing so the next person reading
`save_clip()` does not go looking at the wrong wire.
