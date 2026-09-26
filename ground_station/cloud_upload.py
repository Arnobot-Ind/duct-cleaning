#!/usr/bin/env python3
"""
Cloud upload: send every finished recording in /recordings to the Saibya
Archive server whenever the Pi has internet.

WHY THIS EXISTS. Operator 2026-09-26: "i want to send the footage of this pi
to server, when this pi is connected to internet". The robots with the GCS
stack already do this with GCS/backend/cloud_sync; this Pi records its own way
(recorder.py -> /recordings/<session folder>/<n>_<camera>.mp4), so this is the
same upload protocol fitted to those folders, not a copy of cloud_sync.

THE PROTOCOL (Saibya Archive server, src/app/api/ingest/upload/route.ts):

    PUT <server>/api/ingest/upload
    Authorization: Bearer <device token>          one per robot, ductrobot01
    X-Object-Key: <robot>/sessions/<session>/<path>
    X-Content-SHA256: <hex sha256 of the body>
    Content-Length + raw bytes
    -> 200 stored (also for a repeat of an identical file)

The server writes to S3 and indexes it; the Pi never holds AWS keys. Stored
files never change, and once _COMPLETE.json is in a session nothing in it can.

WHAT A SESSION BECOMES:

    /recordings/session02 date 26-09-26 start 11-26-34 end 11-27-03/1_front.mp4
 -> ductrobot01/sessions/20260926_112634_session02/
        session.json                        first: start, end, cameras
        video/cam1/20260926_112634.ts       1-minute pieces, named by IST start
        video/cam1/20260926_112734.ts
        _COMPLETE.json                      last: the session is fully stored

The server only takes camera video as MPEG-TS under video/<cam>/, and its web
player needs H.264 - but recorder.py writes MPEG-4 Part 2 (cv2's mp4v), which
no browser decodes. So each mp4 is RE-ENCODED to H.264 in 60 s segments, on
the Pi 4's hardware encoder (h264_v4l2m2m: a 29 s clip took 3.7 s, measured
2026-09-26, against 14 s for libx264 on two cores), falling back to libx264 if
the hardware one fails. Sixty-second pieces are what the other robots send, so
the archive's player treats this Pi the same, and no piece gets near the
server's per-file limit however long the recording.

WHICH SESSIONS: only finished ones. A folder is finished when recorder.py has
renamed it with " end HH-MM-SS", nothing in it is still being merged (the same
test the USB chooser uses, see session_files). No waiting beyond that
(operator 2026-09-26: "after recording is saved in pi then send to cloud, no
one min") - only SETTLE_S, a few seconds, so a file still being flushed is not
read half-written. Oldest first.

THE PI KEEPS ITS COPY. This only reads /recordings and uploads; it never
deletes or changes anything there (operator 2026-09-26: "original recordings
not to delete"). The recorder, the playback screen and usb_backup.py are
untouched. A stick plugged in before the Pi is online takes the session with
it as before, and it is then not uploaded. A folder that vanishes mid-upload
is dropped - its session stays on the server as interrupted.

HEADERS IN EVERY SEGMENT. h264_v4l2m2m keeps the SPS/PPS only as extradata,
which the MPEG-TS muxer does not write - so its .ts files have no decoder
setup at all and nothing can play them (found 2026-09-26: the first two
sessions went up that way). dump_extra puts them in front of every keyframe.

OFFLINE IS NORMAL. Every POLL_S it asks the server's /api/health; no answer
means no internet and it tries again next round. Progress is kept per file in
STATE_PATH, so a reboot or a lost link resumes where it stopped.

Runs as the arnobot user (cloud-upload.service); it needs no root.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

# KEY=value lines, the same format as GCS/backend/credential.txt. Environment
# variables win over the file.
CREDENTIAL_PATH = os.path.expanduser(
    os.environ.get("CLOUD_UPLOAD_CREDENTIALS", "~/.config/cloud_upload/credential.txt"))


def _load_credentials(path):
    vals = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return vals


_CRED = _load_credentials(CREDENTIAL_PATH)


def _setting(name, default=""):
    return os.environ.get(name) or _CRED.get(name) or default


SERVER_URL = _setting("CLOUD_SYNC_SERVER_URL", "http://43.204.46.19").rstrip("/")
TOKEN = _setting("CLOUD_SYNC_DEVICE_TOKEN")
# The robot id on the server. The token only accepts keys under it.
PREFIX = _setting("CLOUD_SYNC_PREFIX", "ductrobot01").strip("/")

RECORD_DIR = os.environ.get("RECORD_DIR", "/recordings")
STATE_PATH = os.path.expanduser(
    os.environ.get("CLOUD_UPLOAD_STATE", "~/.local/state/cloud_upload/state.json"))
STATUS_PATH = os.environ.get("CLOUD_UPLOAD_STATUS", "/tmp/cloud_upload_status.json")
FFMPEG = os.environ.get("CLOUD_UPLOAD_FFMPEG", "ffmpeg")
# Tried in order. A keyframe every 2 s (at 15 fps) so the 60 s cuts land close
# to the minute - the segmenter can only cut on one.
ENCODERS = [
    ["-c:v", "h264_v4l2m2m", "-b:v", os.environ.get("CLOUD_UPLOAD_BITRATE", "1500k"),
     "-bsf:v", "dump_extra=freq=keyframe"],
    ["-c:v", "libx264", "-preset", "veryfast", "-crf", "25", "-threads", "2"],
]

POLL_S = float(os.environ.get("CLOUD_UPLOAD_POLL_S", "10"))
SETTLE_S = float(os.environ.get("CLOUD_UPLOAD_SETTLE_S", "5"))
SEGMENT_S = int(os.environ.get("CLOUD_UPLOAD_SEGMENT_S", "60"))
HTTP_TIMEOUT_S = float(os.environ.get("CLOUD_UPLOAD_TIMEOUT_S", "120"))
# A rejected token will not fix itself; do not hammer the server with it.
AUTH_BACKOFF_S = 300.0

# "session02 date 26-09-26 start 11-26-34 end 11-27-03" - recorder.py's name.
SESSION_RE = re.compile(
    r"^(?P<name>session\d+) date (?P<y>\d{2})-(?P<mo>\d{2})-(?P<d>\d{2}) "
    r"start (?P<h>\d{2})-(?P<mi>\d{2})-(?P<s>\d{2})"
    r"(?: end (?P<eh>\d{2})-(?P<emi>\d{2})-(?P<es>\d{2}))?$")
PART_RE = re.compile(r"_\d{3}\.mp4$", re.I)        # usb_chooser._PART_RE
CAM_RE = re.compile(r"^(\d+)_(.+)\.mp4$", re.I)     # "1_front.mp4"


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


# -- sessions on disk ----------------------------------------------------------

def session_files(path):
    """(finished .mp4 names, still_working) - usb_chooser.session_files, kept
    in step with it by hand so this daemon does not have to import Qt."""
    working = False
    finals = []
    try:
        names = os.listdir(path)
    except OSError:
        return [], False
    for n in names:
        if n.lower().endswith(".unreadable"):
            continue
        if n.startswith(".") or not n.lower().endswith(".mp4"):
            working = True
        elif PART_RE.search(n):
            working = True
        else:
            finals.append(n)
    return sorted(finals), working


def parse_folder(folder):
    """Folder name -> (session id, start datetime, end datetime) or None."""
    m = SESSION_RE.match(folder)
    if not m or m.group("eh") is None:
        return None                     # not ours, or still recording
    start = datetime(2000 + int(m["y"]), int(m["mo"]), int(m["d"]),
                     int(m["h"]), int(m["mi"]), int(m["s"]), tzinfo=IST)
    end = start.replace(hour=int(m["eh"]), minute=int(m["emi"]),
                        second=int(m["es"]))
    if end < start:
        end += timedelta(days=1)        # ran past midnight
    sid = "%s_%s" % (start.strftime("%Y%m%d_%H%M%S"), m["name"])
    return sid, start, end


def newest_mtime(path):
    newest = 0.0
    for entry in os.scandir(path):
        try:
            newest = max(newest, entry.stat().st_mtime)
        except OSError:
            pass
    return newest


def ready_sessions(done):
    """Finished, settled, not yet complete on the server - oldest first."""
    out = []
    try:
        folders = sorted(os.listdir(RECORD_DIR))
    except OSError:
        return out
    now = time.time()
    for folder in folders:
        path = os.path.join(RECORD_DIR, folder)
        parsed = parse_folder(folder)
        if parsed is None or not os.path.isdir(path):
            continue
        if done.get(parsed[0], {}).get("complete"):
            continue
        finals, working = session_files(path)
        if working or not finals:
            continue
        if now - newest_mtime(path) < SETTLE_S:
            continue
        out.append((parsed[1], folder, path, parsed, finals))
    out.sort()
    return [o[1:] for o in out]


# -- state ----------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data.get("sessions"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"sessions": {}}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_PATH)


def write_status(status):
    try:
        tmp = STATUS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f)
        os.replace(tmp, STATUS_PATH)
    except OSError:
        pass


# -- server ---------------------------------------------------------------------

class AuthError(Exception):
    """401/403: the token is wrong or belongs to another robot."""


def online():
    try:
        with urllib.request.urlopen(SERVER_URL + "/api/health", timeout=8) as r:
            return r.status == 200
    except Exception:
        return False


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def put_file(path, key):
    """Upload one file. True = stored (now or before). False = retry later."""
    size = os.path.getsize(path)
    headers = {
        "Authorization": "Bearer " + TOKEN,
        "X-Object-Key": key,
        "X-Content-SHA256": sha256_file(path),
        "Content-Length": str(size),
        "Content-Type": "application/octet-stream",
    }
    t0 = time.monotonic()
    try:
        with open(path, "rb") as body:
            req = urllib.request.Request(SERVER_URL + "/api/ingest/upload",
                                         data=body, headers=headers, method="PUT")
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
                reply = json.loads(r.read() or b"{}")
        log("uploaded %s (%d KB, %.1f s%s)" % (
            key, size // 1024, time.monotonic() - t0,
            ", already there" if reply.get("duplicate") else ""))
        return True
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}").get("error", "")
        except Exception:
            detail = ""
        if e.code in (401, 403):
            raise AuthError("%s %s: %s" % (e.code, key, detail))
        if e.code == 409:
            # Already stored with other bytes (a re-encode after a crash between
            # the upload and the state save), or the session is sealed. Either
            # way the server has this file and it cannot be replaced.
            log("server already has %s (%s)" % (key, detail))
            return True
        log("upload %s failed: HTTP %s %s" % (key, e.code, detail))
        return False
    except Exception as e:
        log("upload %s failed: %s" % (key, e))
        return False


def heartbeat(status):
    body = json.dumps(status).encode()
    req = urllib.request.Request(
        SERVER_URL + "/api/ingest/heartbeat", data=body, method="POST",
        headers={"Authorization": "Bearer " + TOKEN,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthError("heartbeat: HTTP %s" % e.code)
    except Exception:
        pass


# -- one session ----------------------------------------------------------------

def _decodable(out_dir):
    """True if every seg_*.ts in out_dir has a picture size ffprobe can read."""
    probe = os.path.join(os.path.dirname(FFMPEG), "ffprobe") if os.sep in FFMPEG else "ffprobe"
    segs = sorted(n for n in os.listdir(out_dir) if n.startswith("seg_") and n.endswith(".ts"))
    for n in segs:
        r = subprocess.run([probe, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width", "-of", "csv=p=0",
                            os.path.join(out_dir, n)], capture_output=True, text=True)
        # One line per program AND per stream, so look for any non-zero width.
        widths = [int(w) for w in re.findall(r"\d+", r.stdout)]
        if r.returncode != 0 or not any(widths):
            return False
    return bool(segs)


def encode(src, out_dir, start):
    """mp4 -> SEGMENT_S-long H.264 .ts pieces in out_dir, named by IST start
    time. Returns [(name, path)] in order, or None if every encoder failed.

    TWO STEPS, NOT ONE. Encoding straight into the segment muxer with
    h264_v4l2m2m produced pieces with no SPS/PPS at all - ffmpeg exited 0 but
    nothing could decode them (width 0, "non-existing PPS 0"): both sessions
    uploaded 2026-09-26 are black for that reason. Encoding to one plain .ts
    keeps the encoder's in-band SPS/PPS; cutting that with -c copy is then
    disk-speed and every piece decodes (checked on the Pi, 130 s -> 3 pieces).
    Each result is still probed, so a broken encode falls through to libx264
    instead of being uploaded."""
    listing = os.path.join(out_dir, "segments.csv")
    full = os.path.join(out_dir, "full.ts")
    base = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    for enc in ENCODERS:
        for old in os.listdir(out_dir):
            os.remove(os.path.join(out_dir, old))
        r = subprocess.run(base + ["-i", src, "-map", "0:v"] + enc + [
                               "-pix_fmt", "yuv420p", "-g", "30", "-f", "mpegts", full],
                           capture_output=True, text=True)
        if r.returncode == 0:
            r = subprocess.run(base + [
                "-i", full, "-map", "0:v", "-c", "copy",
                "-f", "segment", "-segment_time", str(SEGMENT_S),
                "-segment_format", "mpegts", "-reset_timestamps", "1",
                "-segment_list", listing, "-segment_list_type", "csv",
                os.path.join(out_dir, "seg_%04d.ts")], capture_output=True, text=True)
        if r.returncode == 0 and os.path.exists(listing) and _decodable(out_dir):
            os.remove(full)
            break
        log("%s failed on %s: %s" % (enc[1], src, r.stderr.strip()[-300:] or "pieces not decodable"))
    else:
        return None
    out = []
    with open(listing, newline="") as f:
        for row in csv.reader(f):
            if len(row) < 2:
                continue
            seg_start = start + timedelta(seconds=float(row[1]))
            out.append((seg_start.strftime("%Y%m%d_%H%M%S") + ".ts",
                        os.path.join(out_dir, row[0])))
    return out


def upload_session(state, folder, path, parsed, finals):
    sid, start, end = parsed
    entry = state["sessions"].setdefault(sid, {"folder": folder, "uploaded": []})
    uploaded = set(entry["uploaded"])
    base = "%s/sessions/%s/" % (PREFIX, sid)

    def send(local, name):
        key = base + name
        if key in uploaded:
            return True
        if not put_file(local, key):
            return False
        uploaded.add(key)
        entry["uploaded"] = sorted(uploaded)
        save_state(state)
        return True

    cams = {}
    for n in finals:
        m = CAM_RE.match(n)
        cam = "cam%s" % m.group(1) if m else os.path.splitext(n)[0]
        cams[cam] = n

    work = tempfile.mkdtemp(prefix="cloud_upload_")
    try:
        # Every piece first, so session.json can say how many there are.
        # Encoded afresh on every attempt: pieces are not kept between rounds,
        # and anything already stored is skipped by key below.
        pieces = []
        for cam, n in sorted(cams.items()):
            cam_dir = os.path.join(work, cam)
            os.makedirs(cam_dir)
            segs = encode(os.path.join(path, n), cam_dir, start)
            if segs is None:
                return False
            pieces += [(cam, name, p) for name, p in segs]

        manifest = {
            "session_id": sid,
            "robot_id": PREFIX,
            "status": "COMPLETED",
            "simulated": False,
            "video_segment_s": SEGMENT_S,
            "started_at": start.isoformat(),
            "started_unix": start.timestamp(),
            "ended_at": end.isoformat(),
            "ended_unix": end.timestamp(),
            "stop_reason": "RECORDING_SAVED",
            "trip": None,
            "streams": {cam: os.path.splitext(n)[0] for cam, n in cams.items()},
            "counts": {"video_segments": len(pieces)},
            "source": "DuctCleaning ground station (cloud_upload.py)",
            "pi_folder": folder,
        }
        mpath = os.path.join(work, "session.json")
        with open(mpath, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=1)
        if not send(mpath, "session.json"):
            return False

        for cam, name, p in pieces:
            if not send(p, "video/%s/%s" % (cam, name)):
                return False

        cpath = os.path.join(work, "_COMPLETE.json")
        with open(cpath, "w", encoding="utf-8") as f:
            json.dump({"session_id": sid, "files": len(pieces) + 1,
                       "completed_at": datetime.now(IST).isoformat()}, f)
        if not send(cpath, "_COMPLETE.json"):
            return False
        entry["complete"] = True
        save_state(state)
        log("session %s complete (%d pieces)" % (sid, len(pieces)))
        return True
    except OSError as e:
        # usb_backup.py moved the folder away mid-upload, most likely.
        log("session %s: %s - will retry if it is still there" % (sid, e))
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


# -- main loop ------------------------------------------------------------------

def main():
    if not TOKEN:
        log("no CLOUD_SYNC_DEVICE_TOKEN (set it in %s) - exiting" % CREDENTIAL_PATH)
        return 2
    log("cloud upload: %s -> %s as %s" % (RECORD_DIR, SERVER_URL, PREFIX))
    state = load_state()
    was_online = None
    while True:
        status = {"uploader": "cloud_upload.py", "session": None,
                  "updated": time.time(), "alerts": []}
        try:
            is_online = online()
            if is_online != was_online:
                log("server reachable" if is_online else "offline - waiting")
                was_online = is_online
            status["online"] = is_online
            if is_online:
                todo = ready_sessions(state["sessions"])
                status["pending_sessions"] = len(todo)
                heartbeat(status)
                for folder, path, parsed, finals in todo:
                    status["uploading"] = parsed[0]
                    write_status(status)
                    if not upload_session(state, folder, path, parsed, finals):
                        break               # link trouble: next round
                status.pop("uploading", None)
                status["pending_sessions"] = len(ready_sessions(state["sessions"]))
            write_status(status)
        except AuthError as e:
            log("server refused the device token (%s) - check %s; "
                "retrying in %d s" % (e, CREDENTIAL_PATH, AUTH_BACKOFF_S))
            status["alerts"] = ["token refused"]
            write_status(status)
            time.sleep(AUTH_BACKOFF_S)
            continue
        except Exception as e:              # never die on one bad round
            log("round failed: %r" % (e,))
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
