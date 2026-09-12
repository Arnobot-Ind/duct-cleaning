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
frameless top-level widget the size of the screen. The panel's SAVE button
closes it; left / right on the stick skip ten seconds. The recordings carry
no audio, so nothing is lost by having no sound path.

NOT MODAL. QDialog.exec() runs a nested event loop, and this rig has already
been frozen once by one of those (see UsbChooser.on_inputs). The chooser keeps
ticking and simply routes the panel snapshot here while the player is up.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

import theme

# One stick push left or right moves this far. Ten seconds is a few metres of
# duct at driving speed - enough to skip a stretch, not enough to lose your
# place.
SKIP_S = 10.0


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

    def _step(self):
        cap = self._cap
        if cap is None or self._done:
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
        self._step()                        # land on it at once, not a tick later

    # -- paint ------------------------------------------------------------------

    def paintEvent(self, _event):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor("#000000"))
        bar_h = max(56, h // 14)

        if self._pix is not None:
            # Letterboxed into everything above the caption bar, never
            # stretched: a 720x720 recording stays square.
            area = QRectF(0, 0, w, h - bar_h)
            pw, ph = self._pix.width(), self._pix.height()
            scale = min(area.width() / pw, area.height() / ph)
            dw, dh = pw * scale, ph * scale
            x = area.x() + (area.width() - dw) / 2.0
            y = area.y() + (area.height() - dh) / 2.0
            p.setRenderHint(QPainter.SmoothPixmapTransform, False)
            p.drawPixmap(QRectF(x, y, dw, dh), self._pix, QRectF(0, 0, pw, ph))

        # THE CAPTION BAR: what is playing, where in it, and how to leave. The
        # same three things a video recorder's own playback screen shows.
        bar = QRectF(0, h - bar_h, w, bar_h)
        p.fillRect(bar, QColor(0, 0, 0, 200))
        frac = (self._frame_no / float(self.frames)) if self.frames else 0.0
        frac = max(0.0, min(1.0, frac))
        track = QRectF(24, bar.y() + 10, w - 48, 4)
        p.fillRect(track, QColor(255, 255, 255, 50))
        p.fillRect(QRectF(track.x(), track.y(), track.width() * frac, 4),
                   QColor(theme.DARK["blue"]))

        # Laid out from the RIGHT: the hint, then the clock, and the title gets
        # whatever is left and is elided to fit it. A centred clock collided
        # with a session name at 800 px wide - the names are long, and nothing
        # here may ever overprint the way out.
        ty, th = bar.y() + 20, bar_h - 24
        gap, edge = 28, w - 24
        hint = "◀ ▶  skip 10 s   ·   SAVE to exit"
        clock = "%s / %s" % (_hms(self.position), _hms(self.duration))
        p.setFont(theme.font_for(theme.SUBHEAD, theme.W_REGULAR))
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
        p.setFont(theme.font_for(theme.SUBHEAD, theme.W_SEMIBOLD))
        avail = max(0, int(x_clock - gap - 24))
        title = p.fontMetrics().elidedText(self._error or self.title,
                                           Qt.ElideMiddle, avail)
        p.drawText(QRectF(24, ty, avail, th), Qt.AlignLeft | Qt.AlignVCenter,
                   title)
        p.end()
