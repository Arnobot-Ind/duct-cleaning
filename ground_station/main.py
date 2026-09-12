#!/usr/bin/env python3
"""
Ground Station — dual RTSP test viewer (PySide6).

A deliberately simple bring-up UI: a status top bar (logo, per-camera chips,
robot link, clock — see topbar.py) over two side-by-side camera panels that
reconnect on their own. No joystick / no command sending yet — this exists
purely to prove both RTSP streams decode on the ground station Pi before you
wire in Steps 5-9 of the guide.

Run:
    python3 main.py
    python3 main.py rtsp://192.168.1.30:8554/cam1 rtsp://192.168.1.30:8554/cam2

On Windows, launch run_ground_station.pyw instead — same app, no console window.

Recording is on the panel switches, not the keyboard (see recorder.py):

    switch 1, GPIO22 ... START / STOP
    switch 2, GPIO11 ... PAUSE / RESUME, inside the same file
    button,   GPIO9 .... SAVE - bank the clip so far and keep rolling

Keys:
    F        fullscreen toggle        S       snapshot both panels
    R        reconnect both           1/2     solo a camera, 0 = both
    Q / Esc  quit
    Space    start/stop  P  pause     Ctrl+S  save clip   (bench only - these
             do nothing while the panel switches are readable)
"""

from __future__ import annotations

import json
import math
import os
import re
import signal
import sys
import threading
import time
import traceback
from datetime import datetime

from PySide6.QtCore import Qt, QPointF, QRectF, QTimer
from PySide6.QtGui import (QColor, QFont, QImage, QPainter, QPainterPath, QPen,
                           QPixmap, QPolygonF, QShortcut, QKeySequence)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

import config
import inputs
import theme
import thermal
from inputs_panel import InputsPanel
from splash import SplashScreen
from uno_motors import MotorLink

# Analog staleness now lives in uno_motors, next to the loop that acts on it -
# the demand path is pulled on MotorLink's own thread rather than pushed from
# tick(), so main.py has no say in how old a sample may be. See set_source().

# One typeface across the app. The panel headers and the NO SIGNAL placeholder
# used the terminal mono; nothing here is tabular data, so it only ever read as
# a console that had been given a logo.
#
# Both names now come from theme.py, which stands Inter in for Apple's SF Pro
# and picks the right optical face for a given size (Inter Display >= 20px,
# Inter below). Kept as module-level names because the QFont() call sites below
# and in topbar.py still read better spelling out SANS than theme.FAMILY_TEXT.
SANS = theme.FAMILY_TEXT
MONO = SANS

# cv2, link, stream and topbar are NOT imported here — _load_video_stack() does
# it once main() has the logo on the screen. cv2 alone is ~2s of import on a Pi 4
# (numpy, then the whole FFMPEG/GStreamer stack), and every one of those seconds
# is spent before Qt can paint anything: at module scope the operator watches a
# bare console or a black X root instead of the splash. Importing them from a
# function binds the same module-level names, so the classes below are unchanged.
#
# `from __future__ import annotations` above is what keeps that honest — without
# it, CameraPanel's `stream: RTSPStream` annotation would be evaluated at class
# definition time, i.e. before the import has happened.
cv2 = None
LinkMonitor = RTSPStream = SessionManager = describe_backends = TopBar = None


def _load_video_stack():
    """Import the heavy half of the app. Idempotent."""
    global cv2, LinkMonitor, RTSPStream, SessionManager, describe_backends, TopBar
    import cv2
    from link import LinkMonitor
    from recorder import SessionManager
    from stream import RTSPStream, describe_backends
    from topbar import TopBar


class VideoCanvas(QWidget):
    """Draws the newest frame, letterboxed, or a NO SIGNAL placeholder."""

    def __init__(self):
        super().__init__()
        self._pixmap = None
        self._message = "CONNECTING..."
        self._highlight = False
        # When the current highlight began, for the one-shot sweep below.
        self._hl_started = 0.0
        # Thick enough to read at a glance from across a rig, thin enough not to
        # eat the edge of the picture. Drawn inside the canvas, so this many
        # pixels of video are covered on each side while lit.
        #
        # 8px on the operator's call 2026-08-20, up from 6.
        self.HL_BORDER = 8

        # FOUR SOLID ARROWS JABBING INWARD, the moving half of the lit cue.
        #
        # The static border alone is a state: on or off, and a border that is
        # merely present competes with everything else already drawn at the edge
        # of a panel. Motion is what the eye catches in peripheral vision while
        # it is busy reading a duct wall, which is the one moment this cue has
        # to work. The border says WHICH panel; this is what makes it register.
        #
        # SOLID BODIES, NOT LINE CHEVRONS - the operator's call 2026-08-20,
        # after two rounds of stroked chevrons read as too thin and too plain.
        # This is the stock "look here" pointer: a filled head on a stub shaft,
        # dark-edged so it holds its shape against a pale wall, jabbing at the
        # picture a few times and then gone.
        #
        # ALL FOUR AIM AT THE CENTRE, which is direction-agnostic, and that
        # turns out to matter: the panel that lights is the one being driven
        # TOWARD (FRONT under forward, BACK under reverse), so an arrow that
        # claimed a heading would claim the same heading on both cameras.
        # Pointing AT the picture says the only thing true of both - look here -
        # and forward from reverse is told apart by WHICH panel lights.
        self.HL_ARROW_HEAD_L = 34     # length of the head, px
        self.HL_ARROW_HEAD_W = 30     # half-width of the head at its base, px
        self.HL_ARROW_SHAFT_L = 30    # length of the stub behind the head, px
        self.HL_ARROW_SHAFT_W = 12    # half-width of that stub, px
        # THE DEFAULT ONLY. CameraPanel overwrites both of these per panel -
        # FRONT green, BACK red - so this pair is what an unrecognised camera
        # would get, not what either of the two on the rig actually uses. See
        # the block beside self.canvas in CameraPanel.__init__.
        self.HL_ARROW_RGB = (255, 59, 48)
        # A dark edge, not a light one. On a bright duct wall a saturated shape
        # with no outline loses its silhouette; against the dark panel border a
        # light outline would do the same. Dark holds up on both.
        self.HL_ARROW_EDGE = (74, 8, 4)
        self.HL_ARROW_GLOW = 16       # width of the soft halo under it, px
        self.HL_ARROW_S = 1.25        # seconds the whole gesture lasts
        self.HL_ARROW_JABS = 3        # how many times it lunges at the picture
        self.HL_ARROW_INSET = 16      # gap from the border at full retraction
        self.HL_ARROW_TRAVEL = 0.09   # jab distance, as a fraction of the
                                      # panel's short side
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_highlight(self, on):
        """Flip the letterbox surround white while this is the camera being
        driven toward.

        THE VIDEO ITSELF IS NEVER TINTED. An operator reading a duct wall needs
        the picture untouched; what lights is the area AROUND it. That area is
        real rather than a hairline: with config.VIDEO_ZOOM below 1 the frame is
        letterboxed on every side (at zoom 0 it is about a third of the panel),
        so the whole panel reads as lit without a single pixel of the image
        being altered.
        """
        if on != self._highlight:
            self._highlight = on
            if on:
                self._hl_started = time.monotonic()
            self.update()

    def set_pixmap(self, pixmap):
        self._pixmap = pixmap
        self.update()

    def clear(self, message="NO SIGNAL"):
        self._pixmap = None
        self._message = message
        self.update()

    def _blit_rects(self, pixmap):
        """(source, target) rectangles for config.VIDEO_ZOOM. None if unpaintable.

        Scaling straight to self.size() with KeepAspectRatio is the zoom==0 case
        and leaves a third of the panel black; KeepAspectRatioByExpanding is
        zoom==1 and throws away the sides of the frame. This walks between the
        two, and hands Qt only the part of the frame that will be visible — one
        scaled blit of the cropped region instead of scaling the whole frame up
        and then clipping most of it away, which matters on the Pi's CPU.
        """
        pw, ph = pixmap.width(), pixmap.height()
        vw, vh = self.width(), self.height()
        if not (pw and ph and vw and vh):
            return None

        fit = min(vw / pw, vh / ph)          # whole frame inside the panel
        cover = max(vw / pw, vh / ph)        # panel completely covered
        scale = min(fit / max(1e-3, 1.0 - config.VIDEO_ZOOM), cover)

        src_w, src_h = min(pw, vw / scale), min(ph, vh / scale)
        source = QRectF((pw - src_w) / 2, (ph - src_h) / 2, src_w, src_h)
        out_w, out_h = src_w * scale, src_h * scale
        target = QRectF((vw - out_w) / 2, (vh - out_h) / 2, out_w, out_h)
        return source, target

    def paintEvent(self, event):
        painter = QPainter(self)

        # ROUNDED VIDEO, clipped rather than merely drawn behind a rounded
        # frame. The panel is a card (theme.app_stylesheet()'s #panel rule) and
        # a card with square content punched into it reads as a bug — the video
        # has to take the corner with it. Clipping is what makes that true of
        # the PICTURE and not just of the letterbox band around it.
        #
        # RADIUS_MD here and concentric(RADIUS_MD, SPACE_2) on the panel are a
        # matched pair; see the layout margins in CameraPanel.__init__.
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()), theme.RADIUS_MD, theme.RADIUS_MD)
        painter.setClipPath(clip)
        # Antialias only the corner, not the blit: SmoothPixmapTransform stays
        # off below for the same reason it always has, and this flag does not
        # turn it on.
        painter.setRenderHint(QPainter.Antialiasing, True)

        # True black, not the old #05080b. theme.DARK explains why this one
        # value departs from Apple's #1C1C1E: any lift at all shows as a grey
        # frame around dark duct footage.
        painter.fillRect(self.rect(), QColor(theme.DARK["bg"]))

        rects = (
            self._blit_rects(self._pixmap)
            if self._pixmap is not None and not self._pixmap.isNull()
            else None
        )
        if rects is not None:
            # SmoothPixmapTransform stays off — the Pi 4 CPU is doing the
            # decode too, and this runs once per panel per frame.
            source, target = rects
            painter.drawPixmap(target, self._pixmap, source)
        else:
            # THE PLACEHOLDER NOW DISTINGUISHES TRYING FROM FAILED, which the
            # single red never did: "CONNECTING..." in the same red as "NO
            # SIGNAL" tells the operator a camera is broken during the two
            # seconds of every startup when it is merely still coming up.
            # Orange for in-progress, red for given-up — theme.py's status
            # mapping, and the reason it spends orange rather than red on a
            # reconnecting stream.
            trying = self._message.rstrip(".").upper() in ("CONNECTING",
                                                           "RECONNECTING")
            key = "orange" if trying else "red"
            painter.setPen(QColor(theme.DARK[key]))
            # Title3 at semibold, on the ramp. The old 13px bold mono was
            # smaller than the panel's own name label, which put the most
            # important words on the panel below the least important ones.
            painter.setFont(theme.font_for(theme.TITLE3, theme.W_SEMIBOLD))
            painter.drawText(self.rect(), Qt.AlignCenter, self._message)

        if self._highlight:
            # DRAWN OVER THE VIDEO, and it has to be: config.VIDEO_ZOOM
            # defaults to 1.0, which scales the picture to COVER the canvas, so
            # there is no letterbox band left to colour. A frame on top is the
            # only way to mark this view without tinting the image itself - and
            # the image is the thing the operator is actually reading, so it
            # stays untouched.
            #
            # Inset by half the pen width so the whole stroke lands inside the
            # widget instead of being clipped in half by its edge.
            #
            # ROUNDED, AND MITERJOIN IS GONE WITH THE SQUARE CORNER it existed
            # to sharpen: the canvas is now clipped to RADIUS_MD above, so a
            # mitred rectangle would have its four points sliced off by the clip
            # and read as a broken frame. The stroke follows the same corner the
            # video does, at the radius the inset leaves it — subtracting the
            # inset keeps this arc concentric with the clip rather than merely
            # near it, which is the same rule the panel and canvas follow.
            pen = QPen(QColor(theme.DARK["label"]), self.HL_BORDER)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            i = self.HL_BORDER / 2.0
            r = max(1.0, theme.RADIUS_MD - i)
            painter.drawRoundedRect(
                QRectF(self.rect()).adjusted(i, i, -i, -i), r, r)

            # ONE SWEEP, THEN GONE, on the operator's call 2026-08-20.
            # The train crosses the panel once, bottom edge to top edge, and
            # after that this branch draws nothing at all - the lit border is
            # the only thing that persists. A cue that kept marching for as long
            # as the stick was held would sit permanently over the picture, and
            # the picture is the thing actually being read: the sweep announces
            # the panel and then gets out of the way.
            #
            # Re-armed on every off->on edge (see set_highlight), so easing off
            # and driving again plays it again.
            #
            # ANIMATED OFF THE CLOCK, NOT OFF A TIMER, which is what makes it
            # affordable: update() here repaints the whole canvas, and the
            # expensive part of that is re-blitting the scaled frame, so a 20Hz
            # animation timer would very nearly DOUBLE this panel's repaint cost
            # on a Pi already near 145% CPU for two decodes. Taking progress
            # from the monotonic clock means the sweep advances on the repaints
            # the video is already causing (~25/s) and adds none of its own.
            # It also self-clears for free: when the sweep is over this draws
            # nothing, and the next video frame repaints the panel without it.
            u = (time.monotonic() - self._hl_started) / self.HL_ARROW_S
            if u < 1.0:
                # ANTIALIASING ONLY FROM HERE. These are the sole diagonals on
                # the panel and they look hand-cut without it, but the hint is
                # set AFTER the frame blit on purpose - turning it on any
                # earlier would put the smoothing on the scaled video too, which
                # is the one operation on this path that cannot afford it.
                painter.setRenderHint(QPainter.Antialiasing, True)

                cx = self.width() / 2.0
                cy = self.height() / 2.0
                # Measured off the SHORT side so the jab covers the same number
                # of pixels whichever way round the panel is - a camera rotated
                # 90 degrees makes these tall rather than wide.
                reach = min(self.width(), self.height()) * self.HL_ARROW_TRAVEL

                # A RAISED COSINE, which is what makes this read as a jab rather
                # than a slide: it leaves the edge slowly, drives through the
                # middle of each lunge at full speed and eases into the far end,
                # then comes back the same way. Whole cycles, so the arrow is
                # retracted at the end of the gesture instead of being cut off
                # mid-lunge when the fade takes it.
                lunge = 0.5 - 0.5 * math.cos(2.0 * math.pi * self.HL_ARROW_JABS * u)
                # In fast, out slower, so it announces itself and then leaves
                # without a hard cut.
                fade = min(1.0, u / 0.10, (1.0 - u) / 0.25)
                alpha = max(0.0, min(1.0, fade))

                # Start point and inward unit vector, one per edge. Each arrow
                # rides its own normal toward the middle of the picture.
                edges = (
                    (cx, self.HL_ARROW_INSET, 0.0, 1.0),                     # top
                    (cx, self.height() - self.HL_ARROW_INSET, 0.0, -1.0),    # bottom
                    (self.HL_ARROW_INSET, cy, 1.0, 0.0),                     # left
                    (self.width() - self.HL_ARROW_INSET, cy, -1.0, 0.0),     # right
                )

                hl, hw = self.HL_ARROW_HEAD_L, self.HL_ARROW_HEAD_W
                sl, sw = self.HL_ARROW_SHAFT_L, self.HL_ARROW_SHAFT_W

                for sx, sy, dx, dy in edges:
                    # Tip of the arrow after this frame's lunge.
                    ax = sx + dx * (reach * lunge + hl)
                    ay = sy + dy * (reach * lunge + hl)

                    # ONE POLYGON FOR THE WHOLE ARROW, walked from the tip down
                    # one side and back up the other. (-dy, dx) is the
                    # perpendicular, so this single piece of geometry serves all
                    # four edges without a case for each - which is the reason
                    # the four can never drift out of agreement.
                    def at(back, side):
                        return QPointF(ax - dx * back - dy * side,
                                       ay - dy * back + dx * side)

                    body = QPolygonF([
                        at(0, 0),           # tip
                        at(hl, hw),         # head, one side
                        at(hl, sw),         # in to the shaft
                        at(hl + sl, sw),    # tail, same side
                        at(hl + sl, -sw),   # tail, other side
                        at(hl, -sw),        # back out to the head
                        at(hl, -hw),
                    ])

                    # Halo first, underneath everything: a wide soft stroke of
                    # the arrow's own colour. This is what lifts it off a duct
                    # wall - a shape with no glow disappears into any surface
                    # near its own colour, and the picture underneath is not
                    # ours to darken.
                    glow = QPen(QColor(*self.HL_ARROW_RGB, int(70 * alpha)),
                                self.HL_ARROW_GLOW)
                    glow.setJoinStyle(Qt.RoundJoin)
                    painter.setPen(glow)
                    painter.setBrush(Qt.NoBrush)
                    painter.drawPolygon(body)

                    # Then the solid body and its dark edge in one pass.
                    painter.setPen(QPen(QColor(*self.HL_ARROW_EDGE,
                                               int(230 * alpha)), 2))
                    painter.setBrush(QColor(*self.HL_ARROW_RGB, int(245 * alpha)))
                    painter.drawPolygon(body)

                painter.setBrush(Qt.NoBrush)


class CameraPanel(QWidget):
    """Title strip (status dot + name) over the video canvas, for one camera."""

    def __init__(self, stream: RTSPStream):
        super().__init__()
        self.stream = stream
        self._last_seq = -1

        # HEADER SHOWS THE FULL NAME - "CAM 1 <dot> FRONT", not just "CAM 1".
        #
        # Reverted on the operator's call 2026-08-19, same day the number-only
        # form went in. The reasoning then was that the FRONT/BACK word only
        # widened the strip and repeated what the side-by-side layout already
        # said; with the strip now pinned at 32px there is room for it, and the
        # word is what the highlight below is actually keyed on, so showing it
        # makes the lit strip self-explanatory.
        #
        # The top-bar chips in topbar.py are still number-only and were NOT
        # touched - they are a different, narrower row.
        #
        # This is display only: stream.name already carried the label, and it
        # still feeds the snapshot slug, the stat-line tag and camera_label()'s
        # burn-in. Changing this does NOT rename a file.
        self.name_label = QLabel(stream.name)
        self.name_label.setObjectName("panelName")

        self.dot = QLabel("●")
        self.dot.setObjectName("dot")

        # Nothing else goes in here. Resolution, frame rate and backend are
        # bring-up numbers, not operating ones: they never change while the
        # stream is up, and the top bar's chip already carries the two things
        # that do (live / not, and how fast). Read them from the console with
        # `python3 check_streams.py` when you actually need them.
        # WHICH WAY THIS CAMERA LOOKS, matched on the name rather than on the
        # panel's index, so re-ordering cameras.txt cannot silently swap which
        # strip lights up. A camera that is neither (recorder.py's "FULL VIEW")
        # simply never highlights.
        _up = stream.name.upper()
        self.is_front = "FRONT" in _up
        self.is_back = "BACK" in _up
        self._highlight = False

        header = QHBoxLayout()
        # On the grid. The strip's HEIGHT is still pinned to 40px below on the
        # operator's call — these are only the insets within it, and SPACE_4
        # leading lines the camera name up with the video's left edge once the
        # canvas inset below is taken into account.
        # Zero inset here ON PURPOSE: the panel layout below already insets this
        # strip by SPACE_2, and that is the same inset it gives the video canvas.
        # Sharing it is what puts the status dot exactly on the video's left
        # edge instead of 8px inboard of it — a vertical the eye picks up
        # immediately across two panels sitting side by side.
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(theme.SPACE_2)
        # RECORDING TAG, right end of every camera header, 2026-08-24.
        #
        # ON THE PANEL RATHER THAN THE TOP BAR because "is THIS being kept" is a
        # question about a picture, and the answer belongs on the picture. Both
        # panels carry one and each answers for its OWN feed - with
        # RECORD_CAMERAS at its front-only default the back panel stays blank
        # while the front reads REC, which one bar-wide chip could never say.
        # Two tags say it without the operator moving their eyes off the video.
        #
        # It is only ever REC or PAUSED - see set_recording(). The post-session
        # PROCESSING / MERGING run stays on the strip, because by then there is
        # no live picture for it to be about.
        self.rec_tag = QLabel("")
        self.rec_tag.setObjectName("panelRec")
        self.rec_tag.setFixedHeight(theme.PANEL_TAG_H)
        self.rec_tag.setAlignment(Qt.AlignCenter)
        self.rec_tag.hide()

        header.addWidget(self.dot)
        header.addWidget(self.name_label)
        header.addStretch(1)
        header.addWidget(self.rec_tag)

        # Kept on self: set_highlight() restyles it every time the drive
        # direction changes.
        self.header_widget = QWidget()
        self.header_widget.setObjectName("panelHeader")
        self.header_widget.setLayout(header)
        # 40px flat, on the operator's call 2026-08-19 - the third number that
        # day (22 natural -> 28 "+25%" -> 32 -> 40), so it is deliberately one
        # constant and not something derived.
        #
        # PINNED rather than grown from the margins because the number IS the
        # requirement: the strip is 40px whatever the label does. Content is
        # ~21px, so it centres with ~9px of air either side. A much larger font
        # would clip, and the fix then is to raise THIS number, not the margins.
        self.header_widget.setFixedHeight(40)

        self.canvas = VideoCanvas()

        # CAM 1 / FRONT JABS GREEN, CAM 2 / BACK JABS RED, on the operator's
        # call 2026-08-20. That is the third arrangement in one day - front was
        # red, then briefly blue, then red again, and now green - so the reasons
        # each one existed are kept below rather than rewritten away. Read them
        # before changing this pair again.
        #
        # KEYED ON is_back, i.e. on the camera's NAME, exactly like the
        # highlight itself above - not on the panel's index. Re-ordering
        # cameras.txt therefore cannot leave the front camera flashing the back
        # camera's colour, which an index would have allowed. A camera that is
        # neither keeps the canvas default and never highlights anyway.
        #
        # NEITHER COLOUR IS A GO/STOP SIGNAL. This is the first thing on the
        # panel that tells the two cameras apart by colour rather than by
        # position, and it is worth saying what it does not mean: green on the
        # front is not permission to move, and red on the back is not a command
        # to stop. Both mean "this is the view you are being driven toward".
        # The pairing only has to make forward and reverse look different at a
        # glance.
        #
        # THE ARGUMENT AGAINST RED ANYWHERE STILL STANDS AND IS WORTH LEAVING
        # WRITTEN DOWN: this panel already spends #e0564a on failure - the
        # disconnected status dot and the NO SIGNAL text - so a red jab is a
        # neighbour of the one colour here that means something is wrong. Blue
        # was the one hue on this panel carrying no status meaning at all; it
        # was chosen deliberately for that, and overridden just as deliberately.
        # Moving red from the front to the back does not answer that objection,
        # it relocates it. If a red jab is ever taken for a fault on the rig,
        # this is the line to change, and blue is the option already tried.
        #
        # What keeps it workable is that both colours are far hotter and more
        # saturated than either status colour, and they MOVE - a jab that
        # crosses the panel in a second does not read like a dot sitting still.
        if self.is_back:
            self.canvas.HL_ARROW_RGB = (255, 59, 48)
            self.canvas.HL_ARROW_EDGE = (74, 8, 4)
        else:
            self.canvas.HL_ARROW_RGB = (46, 226, 90)
            self.canvas.HL_ARROW_EDGE = (5, 62, 24)

        layout = QVBoxLayout(self)
        # CONCENTRIC INSET. The canvas is inset SPACE_2 (8px) inside the panel
        # on three sides, which is what earns the panel its radius: theme.py
        # rounds the canvas at RADIUS_MD (12) and the panel at
        # concentric(12, 8) = 20, so the two corners are struck from the same
        # centre. Change this inset and the panel radius must move with it —
        # that is the whole point of theme.concentric() rather than a literal.
        #
        # Top margin is 0 because the header strip provides the top air itself
        # and is pinned to 40px; giving it a margin too would push the video
        # down without the strip getting any taller.
        layout.setContentsMargins(theme.SPACE_2, 0, theme.SPACE_2, theme.SPACE_2)
        layout.setSpacing(theme.SPACE_1)
        layout.addWidget(self.header_widget)
        layout.addWidget(self.canvas, 1)

        self.setObjectName("panel")
        # WITHOUT THIS THE CARD DOES NOT EXIST. CameraPanel is a plain QWidget
        # subclass, and Qt only paints a style-sheet background/border/radius on
        # such a widget if it is told to style its own background — otherwise
        # the #panel rule is parsed, matched and then silently ignored.
        # topbar.py carries the same note for the same reason.
        #
        # The old #panel rule had this bug too, which is why the panels have
        # always sat directly on the window ground with no card and no border
        # despite the rule asking for both since the file was written.
        self.setAttribute(Qt.WA_StyledBackground, True)

    def set_recording(self, status):
        """Show REC / PAUSED on this panel, or nothing.

        Blinks on the same wall-clock phase as the strip and the top bar, so
        every recording indicator in the app pulses together rather than three
        of them beating against each other.
        """
        state = (status or {}).get("state")
        if state not in ("RECORDING", "PAUSED"):
            self.rec_tag.hide()
            return
        if state == "RECORDING":
            lit = int(time.monotonic() * 2.8) % 2 == 0
            self.rec_tag.setText(f"{'\u25cf' if lit else '\u25cb'}  REC")
            tone = theme.DARK["red"]
        else:
            # Two bars, not a dot - paused is the state most easily mistaken for
            # recording, and the glyph carries that further than colour does.
            self.rec_tag.setText("\u275a\u275a  PAUSED")
            tone = theme.DARK["orange"]
        self.rec_tag.setStyleSheet(
            f"#panelRec {{ color:{tone};"
            f" background: rgba(235, 235, 245, 0.10);"
            f" border: 1px solid {tone};"
            f" border-radius: {theme.capsule(theme.PANEL_TAG_H)}px;"
            f" padding: 0 12px; font-family: {theme.stack_for(theme.FOOTNOTE)};"
            f" font-size: {theme.FOOTNOTE}px; font-weight: {theme.W_SEMIBOLD}; }}")
        self.rec_tag.show()

    def set_highlight(self, on):
        """Light this WHOLE camera frame - it is the one being driven toward.

        Forward lights FRONT, reverse lights BACK, neutral lights neither, so
        the panel answers "which view matters right now" without the operator
        having to think about it.

        WHITE, and around the whole frame rather than just the title strip, on
        the operator's call 2026-08-19: the strip alone was too easy to miss
        while looking at the video itself, which is the one moment it has to be
        readable. The border carries it around the video; the strip going solid
        white is what makes it unmissable in peripheral vision.

        THE BORDER WIDTH DOES NOT CHANGE, and that matters more than it looks:
        the app rule is already 2px (raised from 1px for exactly this), so
        lighting a panel only swaps a COLOUR. Growing the border instead would
        reflow the video by a pixel on every direction change, which reads as
        the picture twitching.

        #panelName is forced dark here because it inherits #d7e0ea from the
        QWidget rule, which is invisible on white. The status dot is NOT touched
        - refresh() sets its colour inline, and an inline sheet on the widget
        itself outranks this one, so it keeps its red/green on the white strip.

        An INLINE stylesheet rather than a dynamic property + unpolish/polish:
        the property route re-polishes the whole widget on every change, which
        at the UI frame rate is a lot of style recomputation for a colour swap.
        Clearing back to "" restores the app-level rules.

        Guarded on no-change because this is called every tick for every panel;
        without the guard Qt would reparse both sheets ~10x a second per camera.
        """
        if on == self._highlight:
            return
        self._highlight = on
        # ONLY THE CAMERA VIEW LIGHTS, on the operator's call 2026-08-19.
        # An earlier version whitened the title strip and the panel border too;
        # that marked the whole panel, which was more than was wanted. The strip
        # and the border now keep their idle look and the canvas carries the
        # whole indication on its own.
        self.canvas.set_highlight(on)

    def refresh(self):
        """Pull the newest frame (if any) and update the status line."""
        frame, seq = self.stream.latest()

        if frame is not None and seq != self._last_seq:
            self._last_seq = seq
            h, w = frame.shape[:2]
            # Format_BGR888 consumes OpenCV's native byte order — no cvtColor needed.
            # .copy() detaches the QImage from the numpy buffer, which is about to
            # be replaced by the capture thread.
            image = QImage(frame.data, w, h, frame.strides[0], QImage.Format_BGR888).copy()
            self.canvas.set_pixmap(QPixmap.fromImage(image))
        elif not self.stream.connected:
            self.canvas.clear(self.stream.status_text.upper())

        connected = self.stream.connected
        self.dot.setStyleSheet(f"color: {'#38c172' if connected else '#e0564a'};")


class GroundStationWindow(QWidget):
    def __init__(self, cameras):
        super().__init__()
        self.setWindowTitle(f"Arnobot — {config.TITLE}")
        self.resize(1280, 620)

        self.streams = [
            RTSPStream(
                name=name,
                url=url,
                latency_ms=config.RTSP_LATENCY_MS,
                protocols=config.RTSP_PROTOCOL,
                hw_decode=config.USE_HW_DECODE,
                reconnect_delay=config.RECONNECT_DELAY_S,
                read_fail_limit=config.READ_FAIL_LIMIT,
                stall_timeout=config.READ_STALL_TIMEOUT_S,
                reconnect_max_delay=config.RECONNECT_MAX_DELAY_S,
                connect_stagger=config.CONNECT_STAGGER_S,
                session_wait=config.SESSION_WAIT_S,
                unreachable_max_delay=config.UNREACHABLE_MAX_DELAY_S,
                rotate=config.rotate_for(index),
            )
            for index, (name, url) in enumerate(cameras)
        ]
        # Filename identity, distinct from the display name: 'CAM 1 · FRONT'
        # on screen is cam1_front on disk. SessionManager reads .slug.
        for index, stream in enumerate(self.streams):
            stream.slug = config.camera_slug(index)
        self.panels = [CameraPanel(s) for s in self.streams]

        # --- top bar --------------------------------------------------------
        # Hand it the camera list rather than letting it read config: these URLs
        # may have come from cameras.txt or argv, and each chip's tooltip has to
        # show the stream it is ACTUALLY pulling from.
        self.topbar = TopBar(cameras, config.LOGO_PATH, config.TITLE)

        # Separate question, separate answer: the cameras are standalone IP
        # cameras, so live video proves nothing about the robot and this has to
        # be probed on its own. See link.py.
        self.link = LinkMonitor(
            config.ROBOT_LINK_HOST,
            config.ROBOT_LINK_PORT,
            interval=config.LINK_POLL_S,
            timeout=config.LINK_TIMEOUT_S,
        )
        self.link.start()

        # --- operator controls ----------------------------------------------
        # Local hardware, not the robot: the switches, joystick and pot are
        # wired to THIS Pi. The reader owns the GPIO/ADS lines on its own thread
        # and never raises at us, so a missing chip or a busy pin degrades to a
        # message in the strip instead of taking the video viewer down with it.
        self.inputs = inputs.InputReader()
        self.inputs.start()
        self.inputs_panel = InputsPanel()

        # --- motor tether ----------------------------------------------------
        # The Uno is on the eth0 wire at 192.168.50.20, reached over UDP. Same
        # contract as InputReader: its thread owns the socket and never raises
        # at us, so an absent Uno degrades to a dead link instead of taking the
        # viewer down. UDP sends never fail loudly, so the link's health comes
        # from whether ACKs come back, not from whether send() threw.
        self.motors = MotorLink()
        # The drive path pulls its own inputs, on MotorLink's thread, at
        # SEND_HZ. Wired BEFORE start() so the very first pass already has a
        # source and there is no window where the wheels wait on a UI frame.
        # THROUGH THE GATE, not straight off the reader - see _drive_source().
        # While the USB chooser is open the panel is driving the POPUP, and the
        # same stick must not also be driving the wheels.
        self.motors.set_source(self._drive_source)
        self.motors.start()

        # --- recording -------------------------------------------------------
        # Driven entirely by the panel switches via inputs.py: switch 1 (GPIO22)
        # starts and stops, switch 2 (GPIO11) pauses and resumes inside the same
        # file, and the GPIO9 button banks the clip so far and keeps rolling.
        # The encoders read the SAME decoded frames the panels are drawing, so
        # recording opens no second RTSP session and what lands on disk is what
        # the operator saw. See recorder.py.
        self.session = SessionManager(self.streams)

        # Keyboard fallback, used only when the reader has no switch state to
        # give (no gpiod, pins busy, INPUTS_ENABLED=0). Letting the keys fight a
        # live switch would mean the panel showing STOPPED while a file grows.
        self._kbd_session = None

        # The USB backup daemon's status file, re-read at most once a second.
        # A missing or stale file is simply "no transfer" - the daemon is a
        # separate root process and the viewer must not care whether it is up.
        self._usb = {}
        self._usb_read_at = 0.0
        # The USB chooser, while it is open. usb_backup.py now mounts a stick
        # and stops; this is what actually moves anything - see usb_chooser.
        self._usb_dialog = None
        self._usb_dev_shown = None

        # --- video row ------------------------------------------------------
        video_row = QHBoxLayout()
        # 8pt grid (theme.SPACE_*), not the old freehand 10s. The gutter BETWEEN
        # the two panels is deliberately wider than the margin around them:
        # equal values made the pair read as one wide letterboxed image with a
        # seam, which is exactly the reading an operator must not have when the
        # two views are front and back. SPACE_3 apart, SPACE_2 to the edges.
        video_row.setContentsMargins(theme.SPACE_2, theme.SPACE_2,
                                     theme.SPACE_2, theme.SPACE_2)
        video_row.setSpacing(theme.SPACE_3)
        for panel in self.panels:
            video_row.addWidget(panel, 1)

        # No footer: every action it held has a key, the keys are listed in the
        # module docstring, and on the ground station there is no mouse in the
        # first place. Removing it also gives the panels the full window below
        # the bar, which is the only thing an operator is actually looking at.
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.topbar)
        root.addLayout(video_row, 1)
        # Below the video: the operator looks up at the picture and down at the
        # controls, and the strip is fixed-height so it never steals from the
        # panels as the window grows.
        root.addWidget(self.inputs_panel)

        self.setStyleSheet(STYLESHEET)
        self._install_shortcuts()

        for stream in self.streams:
            stream.start()

        self._in_tick = False       # see tick()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(max(1, int(1000 / config.UI_FPS)))

        # The SoC temperature gets its own slow timer rather than riding tick():
        # silicon takes seconds to change, and there is no reason to touch sysfs
        # 30 times a second. Read once up front so the chip is populated before
        # the first interval elapses instead of showing a dash for two seconds.
        self.temp_timer = QTimer(self)
        self.temp_timer.timeout.connect(self.refresh_temp)
        self.temp_timer.start(max(200, int(config.TEMP_POLL_S * 1000)))
        self.refresh_temp()

        # Always-on correlation log: what the motors were asked to do, next to
        # what each camera was doing, once a second.
        #
        # This exists because the interesting failure - "the cameras stall while
        # I drive" - only happens with a human on the stick, and a diagnostic
        # that needs the operator and the investigator online simultaneously
        # never gets captured. Logging it continuously means the next drive
        # records its own evidence. Reads MotorLink's published state rather
        # than sniffing the wire, so it needs no root and no tcpdump.
        self.corr_timer = QTimer(self)
        self.corr_timer.timeout.connect(self.log_correlation)
        self.corr_timer.start(1000)

        # KEEP GSTREAMER BEHIND THE UI - see stream.renice_worker_threads and
        # config.STREAM_THREAD_NICE. Periodic rather than once at startup because
        # GStreamer creates these threads itself and makes a fresh set on every
        # camera reconnect; a set that was never lowered is a set competing with
        # the UI again, and reconnects are routine on this rig.
        #
        # Ten seconds is far more often than reconnects happen and costs a walk
        # of /proc/self/task, which is a few dozen small reads.
        self.nice_timer = QTimer(self)
        self.nice_timer.timeout.connect(self._renice_workers)
        self.nice_timer.start(10000)
        self._renice_workers()

        # THE 20 GB RING BUFFER, checked while recording rather than only at
        # startup. A boot-time-only check would let a long run walk straight past
        # the cap and fill the card, which is the failure this exists to prevent.
        #
        # 30s is chosen against the fill rate, not picked round: at the measured
        # 26 MB/min the card gains ~13 MB between checks, so the cap can only
        # ever be overshot by a fraction of one 20-second segment.
        self.prune_timer = QTimer(self)
        self.prune_timer.timeout.connect(self._prune_recordings)
        self.prune_timer.start(30000)

    def _prune_recordings(self):
        """Drop the oldest sessions if /recordings is over the cap.

        ON A WORKER THREAD because it stats every session folder and may delete
        several, and this is called from the UI timer - doing it inline would
        drop frames on a rig where the video feed is the whole point. Never
        allowed to raise: a housekeeping job must not take the viewer down.
        """
        if getattr(self, "_pruning", False):
            return                      # a slow sweep must not stack up
        self._pruning = True

        live = getattr(self.session, "session_dir", None)

        def run():
            try:
                import recorder as _rec
                _rec.prune_recordings(config.RECORD_DIR, config.RECORD_MAX_BYTES,
                                      protect=live, log=print)
            except Exception as exc:
                print("prune failed: %s" % exc)
            finally:
                self._pruning = False

        threading.Thread(target=run, name="prune", daemon=True).start()

    def _renice_workers(self):
        """Lower GStreamer's decode threads. Never allowed to disturb the UI.

        The import is LOCAL because main.py pulls names out of stream rather than
        importing the module ("from stream import RTSPStream, ..."), so there is
        no module object at this scope. The first version of this called
        stream.renice_worker_threads() and raised NameError into the except
        below on every tick - silently, which is what made it look like the
        renice simply had no effect.
        """
        try:
            import stream as _stream
            _stream.renice_worker_threads()
        except Exception:
            pass

    # -- actions -------------------------------------------------------------

    def _install_shortcuts(self):
        def bind(seq, slot):
            QShortcut(QKeySequence(seq), self, activated=slot)

        bind("F", self.toggle_fullscreen)
        bind("F11", self.toggle_fullscreen)
        bind("S", self.snapshot)
        bind("R", self.reconnect_all)
        bind("Q", self.close)
        bind("Esc", self.close)
        bind("1", lambda: self.solo(0))
        bind("2", lambda: self.solo(1))
        bind("0", lambda: self.solo(None))
        # Recording, for a bench with no panel wired to it. Ignored the moment
        # the switches are readable — see _session_state().
        bind("Space", self.kbd_start_stop)
        bind("P", self.kbd_pause)
        bind("Ctrl+S", self.session_save)

    # -- recording -------------------------------------------------------------

    def kbd_start_stop(self):
        self._kbd_session = (None if self._kbd_session in ("RECORDING", "PAUSED")
                             else "RECORDING")

    def kbd_pause(self):
        if self._kbd_session == "RECORDING":
            self._kbd_session = "PAUSED"
        elif self._kbd_session == "PAUSED":
            self._kbd_session = "RECORDING"

    def session_save(self):
        # Bench-only key, so it stands in for the whole gesture: a held key
        # does not deliver a measurable 3s level the way GPIO25 does, so in the
        # post-stop window Ctrl+S claims the recording directly.
        if not self.session.finalize():
            self.session.save_clip()

    def _usb_status(self):
        """The backup daemon's published state, or {} when there is none.

        Re-read at most once a second - it is a tmpfs file, but 30 reads a
        second for a value that changes every two is still noise. A file whose
        daemon has stopped stamping it is treated as absent rather than shown:
        a strip stuck on "transferring" after the daemon died would have the
        operator waiting on a copy that is not happening.
        """
        now = time.monotonic()
        if now - self._usb_read_at >= 1.0:
            self._usb_read_at = now
            try:
                with open(config.USB_STATUS_PATH, encoding="utf-8") as fh:
                    usb = json.load(fh) or {}
            except (OSError, ValueError):
                usb = {}
            if time.time() - (usb.get("updated") or 0) > 10.0:
                usb = {}
            self._usb = usb
        return self._usb

    def _drive_source(self):
        """The snapshot the motor link is allowed to act on.

        NEUTRAL WHILE THE CHOOSER IS OPEN, operator 2026-08-26: "when popup on
        to not operate any switch, means if on to not on in bot". The popup is
        driven with the joystick and the SAVE button - the very controls that
        otherwise drive the wheels, the rod and the brush - so leaving the drive
        path connected would have every menu movement also move the robot.

        The stick is zeroed rather than the reader being stopped: MotorLink
        judges a missing sample as a dead link and trips its failsafe, which is
        a fault state on the strip. A live snapshot reading centre is the honest
        description of what the operator is asking the WHEELS to do while they
        are looking at a file list - which is nothing.

        Everything else in the snapshot is passed through untouched, so the ADC
        health, the pot and the camera panels keep updating behind the popup.
        """
        snap = self.inputs.latest()
        if self._usb_dialog is None:
            return snap
        gated = dict(snap or {})
        joy = dict(gated.get("joy") or {})
        joy["x"] = 0.0
        joy["y"] = 0.0
        gated["joy"] = joy
        # The rod and the brush are switch-driven; a switch left thrown must not
        # act while the operator is in a menu either.
        gated["actuator"] = None
        gated["brush"] = None
        return gated

    def _sync_usb_dialog(self, usb):
        """Open the chooser when a stick is mounted, close it when it is gone.

        DRIVEN BY THE DAEMON'S STATE, not by a device node this process watched
        for itself: usb_backup.py is the thing with root, and it publishes
        `mounted` with the path once the stick is ready to be written to. Acting
        any earlier would offer SAVE against a mountpoint that does not exist.

        _usb_dev_shown stops a dialog reopening every frame for the same stick
        after the operator closes it - the state stays "mounted" for as long as
        the stick is in, so the device is the thing to remember, not the state.
        """
        state = (usb or {}).get("state")
        dev = (usb or {}).get("device")
        mount = (usb or {}).get("mount")

        if state == "mounted" and mount and dev != self._usb_dev_shown:
            if self._usb_dialog is None:
                try:
                    from usb_chooser import UsbChooser
                    self._usb_dev_shown = dev
                    self._usb_dialog = UsbChooser(config.RECORD_DIR, mount, self)
                    self._usb_dialog.finished.connect(self._usb_dialog_closed)
                    self._usb_dialog.show()
                except Exception as exc:      # never take the viewer down
                    print("usb chooser failed: %s" % exc, flush=True)
                    self._usb_dialog = None

        # The stick went away. Close it rather than leaving buttons pointed at
        # a mountpoint that is no longer there.
        if state in (None, "idle") and self._usb_dialog is not None:
            self._usb_dialog.reject()
            self._usb_dialog = None
            self._usb_dev_shown = None

    def _usb_dialog_closed(self, _result):
        self._usb_dialog = None

    def _session_state(self, snapshot):
        """Switch state if the panel is readable, otherwise the keyboard latch.

        One source at a time and the hardware always wins: two of them would let
        a key press and a lever disagree, and the whole point of the strip is
        that it shows what the recorder is actually doing.
        """
        state = snapshot.get("session")
        return state if state is not None else (self._kbd_session or "STOPPED")

    def solo(self, index):
        for i, panel in enumerate(self.panels):
            panel.setVisible(index is None or i == index)

    def toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def reconnect_all(self):
        for stream in self.streams:
            stream.reconnect()

    def snapshot(self):
        # There is no status line left to report into, so this goes to stdout —
        # visible when the app was started from a terminal, silently dropped
        # under pythonw. The camera chips going green is the feedback that
        # matters; whether a PNG landed is something you check afterwards.
        os.makedirs(config.SNAPSHOT_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        saved = 0
        for stream in self.streams:
            frame, _ = stream.latest()
            if frame is None:
                continue
            slug = stream.name.lower().replace(" ", "")
            path = os.path.join(config.SNAPSHOT_DIR, f"{stamp}_{slug}.png")
            if cv2.imwrite(path, frame):
                saved += 1
        print(f"snapshot: saved {saved} file(s) to {config.SNAPSHOT_DIR}")

    # -- main loop -----------------------------------------------------------

    def tick(self):
        """The UI frame. NON-REENTRANT, and that is the whole point.

        WHY THIS GUARD EXISTS. Any modal opened from inside a frame - the USB
        chooser's delete confirmation is the one that caught us - runs its own
        NESTED event loop via exec(). That loop keeps servicing timers, so this
        QTimer went on firing at UI_FPS and re-entered tick() while the previous
        call was still parked inside the modal. Thirty new frames a second, each
        one refreshing both camera panels, stacked up behind a dialog waiting
        for a human.

        The result was not a slow UI, it was a dead machine: the Pi ran out of
        headroom, SSH stopped answering, and it needed a power cycle. That is
        the "popup opens and after all freeze pi ssh and all" report.

        Returning early is correct rather than merely safe - a frame that
        arrives while the last one is still running has nothing new to draw, and
        the modal is what the operator is looking at anyway.
        """
        if self._in_tick:
            return
        self._in_tick = True
        try:
            self._tick_body()
        finally:
            self._in_tick = False

    def _tick_body(self):
        for index, panel in enumerate(self.panels):
            panel.refresh()
            stream = panel.stream
            self.topbar.set_camera(
                index, stream.connected, stream.fps, stream.status_text
            )

        # The chip reports a real measurement and nothing else - no inferring a
        # healthy robot from healthy video, because they are different machines
        # on different addresses.
        #
        # That measurement is now the Uno command link, not LinkMonitor's TCP
        # probe. LinkMonitor still points at ROBOT_LINK_HOST:PORT, which defaults
        # to the guide's robot-Pi RTSP port (192.168.1.30:8554) - a host that
        # does not exist on this rig, so the chip read DISCONNECTED forever even
        # with the Arduino answering every packet. The Arduino link is UDP, so it
        # cannot be probed by connecting a TCP socket to it either; the honest
        # test is whether it is ACKing the commands we send, which MotorLink
        # already tracks over a rolling window (see uno_motors.ACK_MIN_PCT).
        # If ROBOT_LINK_HOST is ever pointed at something real that speaks TCP,
        # that probe counts too - either being up means the robot is reachable.
        motor_state = self.motors.latest() or {}
        self.topbar.set_robot(bool(motor_state.get("ok")) or self.link.connected)

        # Light the camera we are driving TOWARD: forward -> FRONT, reverse ->
        # BACK, neutral -> neither.
        #
        # Read from the MOTOR DEMAND, not the stick, so it follows what the
        # wheels were actually told - which means it also stays dark through a
        # failsafe trip (safeState zeroes both) and rides the slew ramp instead
        # of flicking on before the robot has moved.
        #
        # left + right, because that is the pair's common component: a spin in
        # place is +x and -x, which sums to zero and correctly lights nothing,
        # while a curve still lights whichever end it is heading for.
        #
        # A NEGATIVE SUM IS FORWARD ON THIS RIG, AND THAT IS NOT A TYPO.
        # Confirmed on the robot 2026-08-19: the first version of this used the
        # obvious "positive is forward" and lit the BACK camera when the stick
        # was pushed forward. The sign here is a property of the whole chain -
        # the stick's wiring, INVERT_X/INVERT_Y in inputs.py, and whichever way
        # round the driver's DIR lines are landed - so it is empirical, exactly
        # like the dot's signs in inputs_panel.py.
        #
        # WORTH KNOWING WHEN READING TELEMETRY: it means a logged "L=+237 R=+237"
        # is the robot REVERSING, not driving forward. Nothing is wrong with the
        # drive; the label is just the opposite of the naive reading.
        drive = (motor_state.get("left") or 0) + (motor_state.get("right") or 0)
        for panel in self.panels:
            panel.set_highlight((drive < 0 and panel.is_front)
                                or (drive > 0 and panel.is_back))
        self.topbar.set_clock(datetime.now().strftime("%H:%M:%S"))

        # Same push model as the top bar - latest() is a cheap dict copy off the
        # reader thread, so this costs nothing at the UI frame rate. Taken once
        # and shared: the panel and the motors must act on the SAME sample, or
        # the strip can show a stick position the wheels never got.
        snapshot = self.inputs.latest()

        # Recording before the panel is told anything: on_inputs() applies the
        # switch state and the save-button edge count, so the status the strip
        # renders this frame is the one the recorder is already acting on rather
        # than one frame stale.
        if self._usb_dialog is not None:
            # THE POPUP OWNS THE PANEL. Recording must not start, stop or bank a
            # clip because the operator moved the stick to reach DELETE ALL -
            # and the SAVE button means "tick this row" in here, not "save the
            # session". Both are held off until the popup closes, at which point
            # set_state() sees the real switch again and carries on.
            self._usb_dialog.on_inputs(snapshot)
        else:
            self.session.set_state(self._session_state(snapshot))
            self.session.on_save_button(snapshot.get("save_presses"))
        # The hold level as well as the press edges: holding SAVE for 3s after
        # a stop is what finalizes the recording into /recordings.
        if self._usb_dialog is None:
            self.session.on_save_hold(snapshot.get("save_held_s"))
        # Expires the post-stop confirm window, which can delete the recording.
        # After the save button and the hold, so a press or a completed hold
        # landing on the last frame of the window is honoured rather than raced.
        self.session.poll()
        status = self.session.status()
        # The USB transfer rides in the same status dict the strip renders, so
        # "data is transferring" appears exactly where the operator already
        # looks for recording state.
        status["usb"] = self._usb_status()
        self._sync_usb_dialog(status["usb"])
        # Drawing the strip must never be able to strand the motors. Everything
        # below this block is the demand the Uno keeps transmitting at 50Hz, so
        # an exception raised in here used to skip the rest of tick() and leave
        # MotorLink repeating the LAST demand forever - the wheels kept driving
        # on a stale stick position. That is not hypothetical: a partial ADC read
        # left joy['y'] None and inputs_panel's f-string raised on every frame.
        # The panel is display; the motor demand below is safety. Never let the
        # first take out the second.
        try:
            self.inputs_panel.set_state(snapshot, status)
            for panel in self.panels:
                # A camera that is only on screen (config.RECORD_CAMERAS) never
                # shows REC: the tag answers "is THIS picture being kept", and
                # for the back camera the honest answer is no.
                panel.set_recording(
                    status if self.session.records(panel.stream) else None)
            self.topbar.set_usb(status.get("usb"))
        except Exception:
            traceback.print_exc()

        # NOTHING BELOW THIS POINT DRIVES THE ROBOT ANY MORE, deliberately.
        #
        # tick() used to push the stick, the rod, the brush and the lamp into
        # MotorLink from here, which quietly made every demand depend on this Qt
        # timer running inside SAMPLE_STALE_S (250 ms). This timer also refreshes
        # the camera panels: measured 2026-08-19, the 1 Hz correlation timer on
        # this same thread missed its deadline by 2 s or more 433 times, and each
        # of those aged the demand out, mixed it to (0, 0) and stopped the robot
        # until the UI caught up - then made it climb the 400 ms slew ramp again.
        # The old comment here claimed a slow frame "can never starve the Uno",
        # which was true and beside the point: the Uno kept receiving packets, it
        # was the DEMAND INSIDE them that had been zeroed.
        #
        # MotorLink now pulls inputs.latest() on its own thread (see
        # set_source()), so a slow repaint costs a stale PANEL and nothing else.
        # Keep it that way - do not reintroduce a set_* call in tick().

    def log_correlation(self):
        """One line a second pairing motor demand with camera state.

        Never allowed to raise: this is instrumentation, and instrumentation
        that can take down the viewer is worse than no instrumentation.
        """
        try:
            m = self.motors.latest() or {}
            snap = self.inputs.latest()
            joy = snap.get("joy") or {}
            pot = snap.get("pot") or {}
            rej = snap.get("adc_rejects") or {}
            parts = [
                time.strftime("%H:%M:%S"),
                f"L={m.get('left') or 0:+4d}",
                f"R={m.get('right') or 0:+4d}",
                f"act={m.get('act') or 0:+2d}",
                f"brush={m.get('brush') or 0}",
                # The lamp demand actually leaving in the 5th field - added
                # 2026-08-18 hunting "the light does not work": the pot column
                # proved the KNOB was read, but nothing recorded what the Uno
                # was told, and the flashed sketch cannot be interrogated.
                f"light={m.get('light') or 0}",
                f"uno={'ok' if m.get('ok') else 'DOWN'}",
            ]
            # THE RAW ADC COUNTS, and they are the point of this addition. A line
            # reading `L=+237 R=+237` is indistinguishable from an operator pushing
            # the stick forward unless the counts behind it are recorded too: three
            # channels sitting on the same value is a dead bus, whereas three
            # unrelated values is a hand. Diagnosing the phantom demand of
            # 2026-08-17 needed exactly this and had to infer it from the wire.
            def _c(v):
                # int(round()) rather than trusting the producer: this
                # formatter is integer-only, and the one time a float reached
                # it the whole correlation log went silent for a night behind
                # the try/except below. Instrumentation that can disable itself
                # on a type change is worse than instrumentation that rounds.
                return "----" if v is None else f"{int(round(v)):5d}"
            parts.append(f"raw=x{_c(joy.get('x_raw'))},y{_c(joy.get('y_raw'))}"
                         f",p{_c(pot.get('raw'))}")
            parts.append(f"adc={snap.get('adc_hz') or 0:.1f}Hz")
            # Only when non-zero, so a healthy run does not carry four counters
            # that never move and the ones that DO move are conspicuous.
            fired = " ".join(f"{k}{v}" for k, v in sorted(rej.items()) if v)
            if fired:
                parts.append(f"rej={fired}")
            for s in self.streams:
                tag = s.name.replace(" ", "")
                parts.append(
                    f"{tag}={'up' if s.connected else 'DOWN'}"
                    f"/{s.fps:.0f}fps/r{s.reconnects}"
                )
            path = os.path.expanduser(
                os.environ.get("MOTOR_CAM_LOG", "~/motor_cam.log"))
            # Cap it rather than filling the SD card - a full root filesystem
            # takes X, the viewer and any ssh session with it.
            try:
                if os.path.getsize(path) > 5_000_000:
                    os.replace(path, path + ".1")
            except OSError:
                pass
            # buffering=1 (line buffered) and a flush, because this log was found
            # holding 1580 NUL bytes mid-file on 2026-08-17. That is the ext4
            # delayed-allocation signature of an unclean exit: the inode's size had
            # been committed but the data blocks had not, so the gap reads back as
            # zeros. It matters beyond tidiness - a single NUL makes grep treat the
            # whole file as binary and silently refuse to print matches, which is
            # how a correlator nobody can grep stops being a correlator.
            with open(path, "a", encoding="utf-8", buffering=1) as fh:
                fh.write(" ".join(parts) + "\n")
                fh.flush()
        except Exception:
            pass

    def refresh_temp(self):
        self.topbar.set_temp(thermal.read_c())

    def closeEvent(self, event):
        self.timer.stop()
        self.temp_timer.stop()
        self.link.stop()
        # First of the workers to go: it is the only one holding a file that is
        # invalid until it is closed properly, and it needs the streams it is
        # reading from to still exist while it finishes.
        self.session.stop()
        # Motors before inputs: stopping the link sends a final STOP, and it
        # should go out while the app is still otherwise alive rather than
        # racing the rest of the teardown.
        self.motors.stop()
        self.inputs.stop()
        for stream in self.streams:
            stream.stop()
        event.accept()


# The app's whole stylesheet now lives in theme.py, next to the colour and type
# tokens it is built from, so a retheme is one file instead of four. What used
# to be here was five rules of hand-mixed hex that had drifted out of step with
# topbar.py's palette; see theme.app_stylesheet() for what replaced each one and
# why. Kept as a module-level name so the setStyleSheet() call above is unchanged.
STYLESHEET = theme.app_stylesheet()


def show_window(window, app):
    if not config.START_FULLSCREEN:
        window.show()
        return

    window.showFullScreen()
    # showFullScreen() only sets the _NET_WM_STATE_FULLSCREEN hint, and on the
    # ground station there is NO window manager to act on it - the window just
    # stays at its requested size in the corner. Force the geometry to the
    # screen rectangle so it fills the display either way.
    screen = app.primaryScreen()
    if screen is not None:
        window.setGeometry(screen.geometry())


def _install_shutdown_handler(window):
    """Close the window cleanly when systemd or a script sends us a signal.

    WHY QT NEEDS HELP HERE. closeEvent() is what stops the recorders and merges
    the run, and it fires when a WINDOW closes - but a shutdown does not close
    windows, it sends SIGTERM to processes. Python's default handler raises
    SystemExit from whichever thread happens to be running, Qt's event loop
    unwinds, and the app dies without closeEvent ever being called. A recording
    in flight was left as unmerged parts every single time the Pi was powered
    off, which is what the operator hit on 2026-09-02.

    THE 0-MS TIMER IS THE TRICK, and it is not decoration. A Python signal
    handler runs between bytecodes, and calling into Qt from there is not safe -
    it can re-enter the event loop from inside C++ code that is mid-call. Posting
    a zero-delay timer defers the close to the event loop's own thread, where
    closeEvent can run normally. This is the standard shape for signals in Qt and
    the reason it is written this way rather than calling close() directly.
    """
    def _bye(signum, _frame):
        QTimer.singleShot(0, window.close)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _bye)
        except (ValueError, OSError):
            # Not the main thread, or a signal this platform will not take.
            # Losing one of the three is survivable; the boot-time recovery
            # pass is the backstop.
            pass

    # WAKE THE INTERPRETER. Qt's event loop sits in a C++ select() that does not
    # return for a signal, so without a periodic trip back into Python the
    # handler above can be delayed until the next mouse move - or for ever, on a
    # headless panel with nothing generating events.
    nudge = QTimer(window)
    nudge.timeout.connect(lambda: None)
    nudge.start(250)
    window._shutdown_nudge = nudge      # keep a reference or Qt collects it


def main():
    # Optional: pass one or two RTSP URLs on the command line.
    urls = sys.argv[1:]

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Arnobot Ground Station")

    # Logo up FIRST — ahead of the cv2 import, the capture threads and the
    # window, which is everything slow. show_on_primary() ends in
    # processEvents(), so the splash is actually painted before the next line
    # blocks; from here to hand_over() there is always something branded on the
    # screen and never a console or a bare root window.
    splash = SplashScreen(config.LOGO_PATH) if config.SPLASH_ENABLED else None
    if splash is not None:
        splash.show_on_primary(app)
        splash.set_status("loading video pipeline")

    _load_video_stack()

    # FINISH WHAT A POWER CUT INTERRUPTED, before anything starts recording.
    #
    # The shutdown handler covers a clean stop, but the 5V simply going away
    # cannot be caught by anything - so a run cut that way survives as numbered
    # per-clip parts, and usb_chooser will not offer a session in that state.
    # This joins them into the normal per-camera files on the way up.
    #
    # In a thread because it is disk-bound and the splash is on screen: a card
    # holding several interrupted sessions would otherwise stall the boot behind
    # a progress-less logo. Nothing else touches /recordings until the operator
    # starts a run, and the idle guard inside declines anything recently written.
    # SNAPSHOT FIRST, on this thread, before the window exists and before any
    # switch can start a run. Everything in this list predates the viewer, so
    # none of it can be live and the idle guard is not needed - which matters,
    # because that guard is what would otherwise skip the very session we are
    # here to rescue: a reboot takes well under its 120s and the interrupted
    # folder still looks "recently written" on the way back up.
    try:
        _existing = [d for d in os.listdir(config.RECORD_DIR)
                     if os.path.isdir(os.path.join(config.RECORD_DIR, d))]
    except OSError:
        _existing = []

    def _recover():
        if not _existing:
            return
        try:
            import recorder as _rec
            n = _rec.recover_unfinished_sessions(
                config.RECORD_DIR, idle_s=0.0, only=_existing, log=print)
            if n:
                print("recovered %d interrupted clip file(s)" % n)
            # AND NAME THEM PROPERLY. A run cut by a power failure never reached
            # _finalize_dir_name(), so its folder still says only when it began.
            # Same snapshot, same reasoning: nothing in it can be live.
            _rec.stamp_unfinished_sessions(
                config.RECORD_DIR, idle_s=0.0, only=_existing)
        except Exception as exc:                  # never block the viewer
            print("recovery pass failed: %s" % exc)

    threading.Thread(target=_recover, name="recover", daemon=True).start()

    cameras = (
        [(config.camera_name(i), url) for i, url in enumerate(urls)]
        if urls else config.CAMERAS
    )

    # Goes nowhere under pythonw.exe (no console, sys.stdout is None, and print()
    # is a documented no-op in that case) — which is the point of the .pyw
    # launcher. Run main.py from a terminal when you want to read this.
    print(describe_backends())
    for name, url in cameras:
        print(f"  {name}: {url}")

    if splash is None:
        window = GroundStationWindow(cameras)
        _install_shutdown_handler(window)
        show_window(window, app)
        sys.exit(app.exec())

    splash.set_status("starting video pipeline")
    window = GroundStationWindow(cameras)
    _install_shutdown_handler(window)
    total = len(window.streams)
    started = time.monotonic()

    def hand_over():
        if not poll.isActive():
            return          # already handed over (Esc raced the timer)
        poll.stop()
        show_window(window, app)
        # Mapped after the splash and with no window manager to restack them,
        # so the viewer is already on top by the time the splash goes.
        splash.finish(window)

    def poll_ready():
        waited = time.monotonic() - started
        live = sum(1 for s in window.streams if s.connected)
        splash.set_status(
            f"{live}/{total} camera{'s' if total != 1 else ''} live"
            if live else "connecting to cameras"
        )
        if waited < config.SPLASH_MIN_S:
            return
        if (live == total
                or (live and waited >= config.SPLASH_PARTIAL_S)
                or waited >= config.SPLASH_MAX_S):
            hand_over()

    poll = QTimer()
    poll.timeout.connect(poll_ready)
    poll.start(150)
    splash.on_skip = hand_over      # Esc / Space skips the wait

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
