"""
Full-screen playback of one recording, driven from the panel.

WHY THIS EXISTS. Operator 2026-09-12: "when usb is plugged in, then one card
open, there i want to give playback option, play option along with all
current, and then exit ... keep video full screen is open and exit". The USB
chooser is where the operator meets their recordings, so it is where they get
to look at one before deciding what to do with it.

IN-PROCESS, NOT AN EXTERNAL PLAYER. ffplay is on the Pi, but it wants a
keyboard to quit and would fight the viewer for the screen with no window
manager to referee. This decodes with cv2.VideoCapture - the same OpenCV that
wrote the file - on a QTimer at the file's own frame rate, and paints into a
frameless top-level widget the size of the screen. A tap on the panel's SAVE
button pauses / resumes, holding it for three seconds closes the player
(operator 2026-09-26: "when save button is pressed, then pause that video and
also when that button is hold for 3 sec then exit"); a tap left / right on the stick skips five seconds, and holding it
there for three seconds rewinds / fast-forwards until it is let go. The
recordings carry no audio, so nothing is lost by having no sound path.

THE PICTURE HAS THE WHOLE SCREEN. Operator 2026-09-26: "after playing video
show video in full screen". The caption bar used to own a strip at the bottom
and the video was fitted into what was left; now the video covers the entire
screen (see FILL_MODE) and the bar is drawn OVER it, only for a few seconds after the
player opens or the stick is touched, then it gets out of the way.

NOT MODAL. QDialog.exec() runs a nested event loop, and this rig has already
been frozen once by one of those (see UsbChooser.on_inputs). The chooser keeps
ticking and simply routes the panel snapshot here while the player is up.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication, QWidget

import theme

# One stick push left or right moves this far. Operator 2026-09-26: "in skip
# instead of 10 second keep 5 second" - ten overshot the spot they were after.
SKIP_S = 5.0

# HOLD THE STICK OVER THIS LONG and the tap turns into a continuous rewind /
# fast-forward (operator 2026-09-26: "when joystick is hold more then 3 sec
# left or right accordingly it should rewind or forward"). It then jumps
# SEEK_STEP_S every SEEK_EVERY_S until the stick is let go: a steady five
# seconds of recording per second held (operator, same day: "in 1sec keep
# forward of 5 sec like constant instead of 10x"). Five small jumps rather
# than one 5 s leap so it moves smoothly; steps rather than a faster frame
# clock because cv2 decodes forward from a keyframe on every seek, which the
# Pi can do five times a second but not at 75 fps.
HOLD_SEEK_S = 3.0
SEEK_STEP_S = 1.0
SEEK_EVERY_S = 0.2

# How long the caption bar stays over the picture after the last touch, and
# how long a "+5 s" badge lingers after the skip that raised it.
OSD_S = 3.0
BADGE_S = 0.9

# HOLD SAVE THIS LONG TO LEAVE THE PLAYER. A shorter press is a tap and
# pauses / resumes, on RELEASE - see UsbChooser._player_inputs.
EXIT_HOLD_S = 3.0

# HOW THE PICTURE MEETS THE SCREEN. The recordings are 720x720 and the panel
# is 1920x1080, so fitting a square keeps only the middle 56% and leaves wide
# black bars - "instead of half screen keep take full screen" (operator
# 2026-09-26). "stretch" fills the screen and keeps every pixel of the
# recording, at the cost of widening it; "crop" fills it at the true shape and
# cuts the top and bottom off; "fit" is the old letterbox.
FILL_MODE = "stretch"


def _hms(seconds):
    seconds = max(0, int(seconds))
    return "%d:%02d" % (seconds // 60, seconds % 60)


class PlaybackView(QWidget):
    """One recording, full screen, until SAVE is pressed or the file ends.

    `finished` fires when the last frame has been shown or the file could not
    be opened. It does NOT fire on stop(): the chooser calls that itself when
    it closes the player, and a signal there would just re-enter it.
    """

    finished = Signal()

    def __init__(self, path, title):
        super().__init__(None)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint
                            | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.path = path
        self.title = title
        self._pix = None
        self._frame_no = 0
        self._error = None
        self._done = False
        # Stick state for stick(): which way it is held, since when, and when
        # the next held-seek step is due.
        self._dir = 0
        self._dir_since = 0.0
        self._seek_next = None
        self._badge = None
        self._badge_until = 0.0
        self._osd_until = 0.0
        self.paused = False
        self._exit_frac = 0.0               # how far through the exit hold

        import cv2                          # already loaded by main.py
        self._cv2 = cv2
        self._cap = cv2.VideoCapture(path)
        if not self._cap.isOpened():
            self._error = "cannot open this recording"
            self.fps, self.frames = 15.0, 0
        else:
            self.fps = float(self._cap.get(cv2.CAP_PROP_FPS) or 0.0)
            if not (1.0 <= self.fps <= 120.0):
                self.fps = 15.0             # a header with no usable rate
            self.frames = int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.duration = self.frames / self.fps if self.frames else 0.0

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._step)

    # -- lifecycle ------------------------------------------------------------

    def start(self):
        # showFullScreen() only sets the _NET_WM_STATE_FULLSCREEN hint, and the
        # Pi runs no window manager to honour it - so size it to the screen by
        # hand, exactly as main.py does for the viewer window.
        screen = QApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(screen.geometry())
        self.showFullScreen()
        self.raise_()
        self.activateWindow()
        self._osd_until = time.monotonic() + OSD_S
        if self._error:
            # Long enough to read the reason, then hand the panel back.
            QTimer.singleShot(2500, self._finish)
            return
        self._step()
        self._timer.start(max(10, int(round(1000.0 / self.fps))))

    def stop(self):
        """Close and release. Safe to call twice; never emits finished."""
        self._done = True
        self._timer.stop()
        cap, self._cap = self._cap, None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        self.close()

    def _finish(self):
        if self._done:
            return
        self._done = True
        self._timer.stop()
        self.finished.emit()

    # -- playback ---------------------------------------------------------------

    @property
    def position(self):
        """Seconds into the file."""
        return self._frame_no / self.fps if self.fps else 0.0

    def _step(self, force=False):
        cap = self._cap
        if cap is None or self._done:
            return
        if self.paused and not force:
            # The frame clock keeps ticking while paused so the badge, the
            # caption bar and the exit hold still animate and time out.
            self.update()
            return
        ok, frame = cap.read()
        if not ok or frame is None:
            self._finish()                  # the end of the file
            return
        h, w = frame.shape[:2]
        # Format_BGR888 takes OpenCV's byte order as is; .copy() detaches the
        # QImage from the buffer the next read() is about to overwrite.
        image = QImage(frame.data, w, h, frame.strides[0],
                       QImage.Format_BGR888).copy()
        self._pix = QPixmap.fromImage(image)
        pos = cap.get(self._cv2.CAP_PROP_POS_FRAMES)
        self._frame_no = int(pos) if pos and pos > 0 else self._frame_no + 1
        self.update()

    def skip(self, seconds):
        """Jump `seconds` forward (or back, when negative) and show it now."""
        cap = self._cap
        if cap is None or not self.frames or self._done:
            return
        target = int(self._frame_no + seconds * self.fps)
        target = max(0, min(self.frames - 1, target))
        cap.set(self._cv2.CAP_PROP_POS_FRAMES, target)
        self._frame_no = target
        self._step(force=True)              # land on it at once, even paused

    def stick(self, direction):
        """The stick's left / right, once per UI frame: -1, 0 or +1.

        A fresh push skips SKIP_S at once and flashes the jump on that side of
        the picture. Kept over for HOLD_SEEK_S, it becomes a rewind /
        fast-forward that runs until the stick comes back to centre.
        """
        now = time.monotonic()
        if not direction:
            self._dir, self._seek_next = 0, None
            return
        self._osd_until = now + OSD_S
        if direction != self._dir:
            self._dir, self._dir_since, self._seek_next = direction, now, None
            self.skip(SKIP_S * direction)
            self._show_badge("skip", direction)
            return
        if now - self._dir_since < HOLD_SEEK_S:
            return
        if self._seek_next is None or now >= self._seek_next:
            self._seek_next = now + SEEK_EVERY_S
            self.skip(SEEK_STEP_S * direction)
        self._show_badge("seek", direction)

    def toggle_pause(self):
        """SAVE tapped: freeze on the current frame, or carry on from it."""
        if self._done or self._error:
            return
        self.paused = not self.paused
        self._osd_until = time.monotonic() + OSD_S
        self.update()

    def set_exit_hold(self, frac):
        """How far SAVE is through its EXIT_HOLD_S hold, 0..1 (0 = not held)."""
        frac = max(0.0, min(1.0, frac))
        if frac != self._exit_frac:
            self._exit_frac = frac
            self.update()

    def _show_badge(self, kind, direction):
        self._badge = (kind, direction)
        self._badge_until = time.monotonic() + BADGE_S
        self.update()

    # -- paint ------------------------------------------------------------------

    def paintEvent(self, _event):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor("#000000"))
        # EVERYTHING DRAWN OVER THE PICTURE SCALES WITH THE SCREEN. The panel
        # screen is 1920x1080; sizes tuned at 800x480 read as small print there.
        k = max(1.0, h / 720.0)
        bar_h = int(56 * k)
        now = time.monotonic()

        if self._pix is not None:
            # The whole screen, per FILL_MODE.
            pw, ph = self._pix.width(), self._pix.height()
            src = QRectF(0, 0, pw, ph)
            dst = QRectF(0, 0, w, h)
            if FILL_MODE == "crop":
                scale = max(w / pw, h / ph)
                sw, sh = w / scale, h / scale
                src = QRectF((pw - sw) / 2.0, (ph - sh) / 2.0, sw, sh)
            elif FILL_MODE != "stretch":
                scale = min(w / pw, h / ph)
                dw, dh = pw * scale, ph * scale
                dst = QRectF((w - dw) / 2.0, (h - dh) / 2.0, dw, dh)
            p.setRenderHint(QPainter.SmoothPixmapTransform, False)
            p.drawPixmap(dst, self._pix, src)

        if self._badge and now < self._badge_until:
            self._paint_seek(p, w, h, k, now)
        if self.paused:
            self._paint_paused(p, w, h, k)
        if self._exit_frac > 0.0:
            self._paint_exit_hold(p, w, k)

        # The caption bar only while it is wanted - see the module docstring.
        # Always while paused: a still picture with no clock looks frozen.
        if not self._error and not self.paused and now >= self._osd_until:
            p.end()
            return

        # THE CAPTION BAR: what is playing, where in it, and how to leave. The
        # same three things a video recorder's own playback screen shows.
        pad = 24 * k
        bar = QRectF(0, h - bar_h, w, bar_h)
        p.fillRect(bar, QColor(0, 0, 0, 200))
        frac = (self._frame_no / float(self.frames)) if self.frames else 0.0
        frac = max(0.0, min(1.0, frac))
        th_track = max(4.0, 5 * k)
        track = QRectF(pad, bar.y() + 10 * k, w - 2 * pad, th_track)
        p.fillRect(track, QColor(255, 255, 255, 50))
        p.fillRect(QRectF(track.x(), track.y(), track.width() * frac, th_track),
                   QColor(theme.DARK["blue"]))

        # Laid out from the RIGHT: the hint, then the clock, and the title gets
        # whatever is left and is elided to fit it. A centred clock collided
        # with a session name at 800 px wide - the names are long, and nothing
        # here may ever overprint the way out.
        ty = track.bottom() + 4 * k
        th = bar.bottom() - ty
        gap, edge = 28 * k, w - pad
        hint = ("◀ ▶  %g s  ·  SAVE: %s  ·  hold SAVE %g s: exit"
                % (SKIP_S, "play" if self.paused else "pause", EXIT_HOLD_S))
        clock = "%s / %s" % (_hms(self.position), _hms(self.duration))
        p.setFont(theme.font_for(int(theme.SUBHEAD * k), theme.W_REGULAR))
        fm = p.fontMetrics()
        hint_w = fm.horizontalAdvance(hint)
        clock_w = fm.horizontalAdvance(clock)
        p.setPen(QColor(255, 255, 255, 170))
        p.drawText(QRectF(edge - hint_w, ty, hint_w, th),
                   Qt.AlignLeft | Qt.AlignVCenter, hint)
        x_clock = edge - hint_w - gap - clock_w
        p.setPen(QColor("#FFFFFF"))
        p.drawText(QRectF(x_clock, ty, clock_w, th),
                   Qt.AlignLeft | Qt.AlignVCenter, clock)
        p.setFont(theme.font_for(int(theme.SUBHEAD * k), theme.W_SEMIBOLD))
        avail = max(0, int(x_clock - gap - pad))
        title = p.fontMetrics().elidedText(self._error or self.title,
                                           Qt.ElideMiddle, avail)
        p.drawText(QRectF(pad, ty, avail, th), Qt.AlignLeft | Qt.AlignVCenter,
                   title)
        p.end()

    def _paint_seek(self, p, w, h, k, now):
        """The skip / seek feedback, the way phone video players do it.

        Operator 2026-09-26: "also show properly fast forward". A shaded arc
        on the side the stick went, three arrows lighting up in turn, and
        under them what happened - "5 seconds" for a tap, "FORWARD 5 s/sec" and
        the time reached for a hold. The arrows are painted, not font glyphs,
        so they look the same whatever fonts the Pi has.
        """
        kind, direction = self._badge
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)

        # The arc: an ellipse centred just off the screen edge, so only its
        # inner curve shows - rewind on the left, forward on the right.
        rx, ry = w * 0.34, h * 0.78
        cx = w + rx * 0.45 if direction > 0 else -rx * 0.45
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 38))
        p.drawEllipse(QPointF(cx, h / 2.0), rx, ry)

        # The middle of the visible part of the arc is where the content goes.
        # The visible part is rx * 0.55 wide.
        mid_x = (w - rx * 0.55 / 2.0) if direction > 0 else rx * 0.55 / 2.0
        cy = h / 2.0

        # Three arrows, lit one after another in the direction of travel -
        # faster while held, so a seek looks faster than a skip.
        size = 26 * k
        step = size * 1.05
        rate = 9.0 if kind == "seek" else 5.0
        lit = int(now * rate) % 3
        total = step * 2 + size
        x0 = mid_x - total / 2.0
        for i in range(3):
            order = i if direction > 0 else 2 - i
            alpha = 255 if order == lit else 110
            left = x0 + i * step
            if direction > 0:
                tri = QPolygonF([QPointF(left, cy - size * 0.6),
                                 QPointF(left + size, cy),
                                 QPointF(left, cy + size * 0.6)])
            else:
                tri = QPolygonF([QPointF(left + size, cy - size * 0.6),
                                 QPointF(left, cy),
                                 QPointF(left + size, cy + size * 0.6)])
            p.setBrush(QColor(255, 255, 255, alpha))
            p.drawPolygon(tri)

        # What happened, under the arrows.
        if kind == "skip":
            lines = ["%g seconds" % SKIP_S]
        else:
            lines = ["%s  %g s/sec" % ("FORWARD" if direction > 0
                                       else "REWIND",
                                       SEEK_STEP_S / SEEK_EVERY_S),
                     "%s / %s" % (_hms(self.position), _hms(self.duration))]
        p.setPen(QColor("#FFFFFF"))
        y = cy + size * 0.6 + 14 * k
        for n, text in enumerate(lines):
            p.setFont(theme.font_for(int((theme.HEADLINE if n == 0
                                          else theme.SUBHEAD) * k),
                                     theme.W_SEMIBOLD if n == 0
                                     else theme.W_REGULAR))
            lh = p.fontMetrics().height()
            p.drawText(QRectF(mid_x - rx, y, 2 * rx, lh), Qt.AlignCenter, text)
            y += lh + 2 * k
        p.restore()

    def _paint_paused(self, p, w, h, k):
        """A big pause sign in the middle, like any player, so a still picture
        reads as paused and not as a hung decoder."""
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        r = 56 * k
        cx, cy = w / 2.0, h / 2.0
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        p.drawEllipse(QPointF(cx, cy), r, r)
        bw, bh, gap = 14 * k, 44 * k, 12 * k
        p.setBrush(QColor("#FFFFFF"))
        p.drawRoundedRect(QRectF(cx - gap / 2.0 - bw, cy - bh / 2.0, bw, bh),
                          3 * k, 3 * k)
        p.drawRoundedRect(QRectF(cx + gap / 2.0, cy - bh / 2.0, bw, bh),
                          3 * k, 3 * k)
        p.setPen(QColor("#FFFFFF"))
        p.setFont(theme.font_for(int(theme.HEADLINE * k), theme.W_SEMIBOLD))
        p.drawText(QRectF(cx - 200 * k, cy + r + 10 * k, 400 * k,
                          p.fontMetrics().height()), Qt.AlignCenter, "PAUSED")
        p.restore()

    def _paint_exit_hold(self, p, w, k):
        """While SAVE is held: a pill at the top filling towards the exit, so
        the operator knows to keep holding - and that letting go cancels."""
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        text = "Keep holding SAVE to exit"
        p.setFont(theme.font_for(int(theme.SUBHEAD * k), theme.W_SEMIBOLD))
        fm = p.fontMetrics()
        pw = fm.horizontalAdvance(text) + 48 * k
        ph = fm.height() + 28 * k
        pill = QRectF((w - pw) / 2.0, 28 * k, pw, ph)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 190))
        p.drawRoundedRect(pill, ph / 2.0, ph / 2.0)
        p.setPen(QColor("#FFFFFF"))
        p.drawText(QRectF(pill.x(), pill.y() + 6 * k, pw, fm.height()),
                   Qt.AlignCenter, text)
        track = QRectF(pill.x() + 24 * k, pill.bottom() - 12 * k,
                       pw - 48 * k, 4 * k)
        p.setBrush(QColor(255, 255, 255, 60))
        p.drawRoundedRect(track, 2 * k, 2 * k)
        p.setBrush(QColor(theme.DARK["red"]))
        p.drawRoundedRect(QRectF(track.x(), track.y(),
                                 track.width() * self._exit_frac,
                                 track.height()), 2 * k, 2 * k)
        p.restore()
