from jig_calib import (
    JigCalibrationMixin,
    init_calibration_db,
    GOLDEN_DEVICE_SERIAL,
    CALIBRATION_START,
    CALIBRATION_END,
    CALIBRATION_START_MINUTES,
    CALIBRATION_END_MINUTES,
    CALIBRATION_START_TEXT,
    CALIBRATION_END_TEXT,
    normal_plot_displacement,
    CALIBRATION_DB,
    CALIBRATION_LIMITS,
    CALIBRATION_BUTTONS,
    calibration_dip_ok,
    calibration_limits_text,
    calibration_period,
    ist_now,
    is_jig_active,
)
from erp import fetch_job_card_for_employee


from datetime import datetime, timedelta, timezone

# ===== CHANGE CALIBRATION TIMES ONLY HERE (24-hour IST) =====

import sys
import os
import logging
import requests
import threading
import math
import serial
from PyQt5.QtWidgets import QApplication, QMainWindow, QPushButton, QVBoxLayout, QWidget, QLineEdit,QFrame,QComboBox,QCompleter,QMessageBox
from PyQt5.QtCore import QThread, pyqtSignal, Qt, QTimer
import matplotlib.pyplot as plt
import time
import base64
import gzip
import sqlite3
import matplotlib
import schedule
from PyQt5.QtGui import QPainterPath

from erp import (
    API_KEY, API_SECRET, ERP_DASHBOARD_URL, ERP_JOB_CARD_URL, ERP_PASS,
    HEADERS, clear_pass_count, fetch_employees, get_pass_count,
    increment_pass_count, init_db, insert_fail_record, insert_pass_record,
)

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT = float(os.getenv("C10_REQUEST_TIMEOUT", "10"))
WS_SERVER_URL = os.getenv("C10_WS_SERVER_URL", "ws://192.168.0.200:8050")
SQL_UPLOAD_URL = os.getenv("C10_SQL_UPLOAD_URL", "http://192.168.0.200:8000/upload_sql_data_1")
ARDUINO_PORT = os.getenv("C10_ARDUINO_PORT", "/dev/ttyUSB1")
GRBL_PORT = os.getenv("C10_GRBL_PORT", "/dev/ttyUSB0")




try:
    from cassinipart2_label import print_fail_label_from_params
except Exception as printer_import_error:
    print(f"[printer] Import failed: {printer_import_error}")
    print_fail_label_from_params = None
matplotlib.use('Qt5Agg')
import websocket
import multiprocessing
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtWidgets import QLabel
from PyQt5.QtGui import QPixmap
import json


# ═══════════════════════════════════════════════════════════════════
#  ANIMATION SUPPORT
# ═══════════════════════════════════════════════════════════════════

from PyQt5.QtGui import QPainter, QColor, QPen, QFont
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import numpy as np


# ═══════════════════════════════════════════════════════════════════
#  LIVE GRAPH CANVAS — real-time Arduino readings with dip animation
# ═══════════════════════════════════════════════════════════════════

class ViewToggle(QtWidgets.QAbstractButton):
    """Animated sliding switch: unchecked Technician, checked Admin."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setFixedSize(56, 28)
        self.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
        self.setAccessibleName("Admin view")
        self.setToolTip("Switch between Technician and Admin views")
        self._position = 0.0
        self._animation = QtCore.QPropertyAnimation(self, b"position", self)
        self._animation.setDuration(180)
        self._animation.setEasingCurve(QtCore.QEasingCurve.InOutCubic)
        self.toggled.connect(self._animate)

    @QtCore.pyqtProperty(float)
    def position(self):
        return self._position

    @position.setter
    def position(self, value):
        self._position = value
        self.update()

    def _animate(self, checked):
        self._animation.stop()
        self._animation.setStartValue(self._position)
        self._animation.setEndValue(1.0 if checked else 0.0)
        self._animation.start()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        track = QtGui.QColor("#2586E8" if self.isChecked() else "#273C50")
        painter.setPen(QtGui.QPen(QtGui.QColor("#78BBFF" if self.hasFocus() else "#45617A"), 1))
        painter.setBrush(track)
        painter.drawRoundedRect(QtCore.QRectF(1, 1, 54, 26), 13, 13)
        x = 4 + 28 * self._position
        painter.setPen(Qt.NoPen)
        painter.setBrush(QtGui.QColor(0, 0, 0, 55))
        painter.drawEllipse(QtCore.QRectF(x, 5, 20, 20))
        painter.setBrush(QtGui.QColor("#FFFFFF"))
        painter.drawEllipse(QtCore.QRectF(x, 4, 20, 20))
        painter.end()


class TestResultPanel(QtWidgets.QWidget):
    """Large in-place result; graph compatibility hooks do no rendering."""
    def __init__(self):
        super().__init__()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.addStretch()
        self.title = QtWidgets.QLabel()
        self.title.setAlignment(Qt.AlignCenter)
        self.detail = QtWidgets.QLabel()
        self.detail.setAlignment(Qt.AlignCenter)
        self.detail.setWordWrap(True)
        layout.addWidget(self.title)
        layout.addWidget(self.detail)
        layout.addStretch()
        self._admin = False
        self._graph = None
        self.show_state("READY", "Scan a serial number to begin", "#8FAAC0")

    def show_state(self, title, detail="", color="#00FFD1"):
        self.title.setText(title)
        self.title.setStyleSheet(f"color:{color};font-size:72px;font-weight:900;background:transparent;border:none;")
        self.detail.setText(detail)
        self.detail.setStyleSheet("color:#C8DFF0;font-size:20px;background:transparent;border:none;padding:16px;")

    def set_admin(self, enabled):
        self._admin = bool(enabled)
        if enabled and self._graph is None:
            self._graph = LiveGraphCanvas()
            self.layout().addWidget(self._graph, 1)
        self.title.setVisible(not enabled)
        self.detail.setVisible(not enabled)
        for index in (0, 3):
            spacer = self.layout().itemAt(index).spacerItem()
            if spacer:
                spacer.changeSize(0, 0, QtWidgets.QSizePolicy.Minimum,
                                  QtWidgets.QSizePolicy.Ignored if enabled else QtWidgets.QSizePolicy.Expanding)
        if self._graph is not None:
            self._graph.setVisible(enabled)
            if enabled:
                self._graph.draw_idle()
        self.layout().invalidate()

    def reset(self):
        self.show_state("TESTING", "Please wait for the result")
        if self._graph is not None:
            self._graph.reset()

    def append_point(self, *args):
        if self._admin and self._graph is not None:
            self._graph.append_point(*args)

    def mark_dip(self, *args):
        if self._admin and self._graph is not None:
            self._graph.mark_dip(*args)

    def begin_peak_hold(self, *args):
        if self._admin and self._graph is not None:
            self._graph.begin_peak_hold(*args)


class LiveGraphCanvas(FigureCanvas):
    COLORS = {
        "right":  "#4A9EFF",
        "centre": "#F5A623",
        "left":   "#2ECC71",
    }
    _REDRAW_INTERVAL = 150  
    _PEAK_HOLD_RELEASE_N = 0.8

    def __init__(self, parent=None):
        screen_r = QtWidgets.QApplication.primaryScreen().geometry()
        fw = max(6, screen_r.width()  / 80)
        fh = max(4.5, screen_r.height() / 80)
        self.fig = Figure(figsize=(fw, fh), facecolor="#080C12")
        super().__init__(self.fig)
        self.setParent(parent)

        self.ax = self.fig.add_subplot(111)
        self._style_axes()

        # Raw buffers: disp_mm and force_N as received from Arduino
        self._raw  = {k: ([], []) for k in ("right", "centre", "left")}
        # EMA-smoothed force (same alpha=0.99 as plot())
        self._smooth = {k: [] for k in ("right", "centre", "left")}

        # Dip markers: (disp_mm, smoothed_force)
        self._dips = {k: [] for k in ("right", "centre", "left")}

        # Turnaround hold state (see _PEAK_HOLD_RELEASE_N)
        self._hold = {k: False for k in ("right", "centre", "left")}
        self._peak = {k: float("-inf") for k in ("right", "centre", "left")}

        self._dirty = False
        self._redraw_timer = QTimer(self)
        self._redraw_timer.setInterval(self._REDRAW_INTERVAL)
        self._redraw_timer.timeout.connect(self._throttled_redraw)
        self._redraw_timer.start()

        self._flash_timer = QTimer(self)
        self._flash_timer.setInterval(40)
        self._flash_timer.timeout.connect(self._flash_tick)
        self._flash_frame  = 0
        self._flash_total  = 24
        self._flash_active = False

        self.fig.subplots_adjust(left=0.10, right=0.95, top=0.95, bottom=0.12)

    def _style_axes(self):
        ax = self.ax
        ax.set_facecolor("#080C12")
        ax.tick_params(colors="#FFFFFF", labelsize=8)
        ax.xaxis.label.set_color("#FFFFFF")
        ax.yaxis.label.set_color("#FFFFFF")
        ax.set_xlabel("Displacement (mm)",         fontsize=8, color="#FFFFFF", fontweight="bold")
        ax.set_ylabel("Force (N)", fontsize=8, color="#FFFFFF", fontweight="bold")
        for spine in ax.spines.values():
            spine.set_edgecolor("#3A5070")
        ax.grid(True, color="#1A2538", linewidth=0.5, linestyle="--", alpha=0.6)

    def reset(self):
        for k in self._raw:
            self._raw[k]    = ([], [])
            self._smooth[k] = []
            self._dips[k]   = []
            self._hold[k]   = False
            self._peak[k]   = float("-inf")
        self._flash_timer.stop()
        self._flash_active = False
        self._dirty = True

    def append_point(self, key: str, disp_mm: float, force_n: float):
        """Append a raw point and update EMA — same alpha=0.99 as plot()."""
        if key not in self._raw:
            return
        # NOTE: the first Arduino field (named disp_mm here) is the force.
        if self._hold[key]:
            if disp_mm > self._peak[key]:
                pass                      # still rising (decel overshoot): draw it
            elif disp_mm > self._peak[key] - self._PEAK_HOLD_RELEASE_N:
                return                    # stationary sag/wobble at the top: skip
            else:
                self._hold[key] = False   # retract under way: resume drawing
        if disp_mm > self._peak[key]:
            self._peak[key] = disp_mm
        self._raw[key][0].append(disp_mm)
        self._raw[key][1].append(force_n)

        # EMA on force, alpha=0.99 — identical to plot()
        alpha = 0.99
        s = self._smooth[key]
        if not s:
            s.append(force_n)
        else:
            s.append(s[-1] * alpha + (1 - alpha) * force_n)

        self._dirty = True

    def begin_peak_hold(self, key: str):
        """Called when the press is cancelled; hides the turnaround dwell."""
        if key in self._hold:
            self._hold[key] = True

    def mark_dip(self, key: str, disp_mm: float, force_n: float):
        if key not in self._dips:
            return
        self._dips[key].append((disp_mm, force_n))
        self._start_flash()
        self._dirty = True

    def _throttled_redraw(self):
        if self._dirty:
            self._dirty = False
            self._paint()

    def _paint(self):
        self.ax.cla()
        self._style_axes()

        has_data = False
        for key in ("right", "centre", "left"):
            disp_list = self._raw[key][0]
            smooth_list = self._smooth[key]
            if not disp_list or not smooth_list:
                continue
            has_data = True
            color = self.COLORS[key]

            # Plot: force on X, displacement on Y — vertical peaks orientation
            self.ax.plot(smooth_list, disp_list,
                         color=color, linewidth=3.0,
                         alpha=0.15, zorder=1)  # glow halo
            self.ax.plot(smooth_list, disp_list,
                         color=color, linewidth=1.8,
                         label=f"{key.capitalize()} Button",
                         alpha=0.95, zorder=2)

            # Dip markers
            for (dx, dy) in self._dips[key]:
                # dx=disp_mm (X axis), dy=force_N (Y axis)
                # Find nearest index in raw disp list for accurate Y (force) placement
                raw_disp = self._raw[key][0]
                if raw_disp:
                    nearest_idx = min(range(len(raw_disp)), key=lambda i, rd=raw_disp, d=dx: abs(rd[i] - d))
                    if nearest_idx < len(smooth_list):
                        dy = smooth_list[nearest_idx]

                if self._flash_active:
                    t = self._flash_frame / max(self._flash_total, 1)
                    pulse = 0.35 + 0.65 * abs(np.sin(t * np.pi))
                    outer = int(320 * pulse); inner = int(90 * pulse)
                else:
                    outer = 200; inner = 70

                # Glowing dot at dip — (force on X, disp on Y)
                self.ax.scatter([dy], [dx], s=outer, color=color,
                                alpha=0.25, edgecolors="none", zorder=4)
                self.ax.scatter([dy], [dx], s=inner, color=color,
                                alpha=1.0, edgecolors="#ffffff",
                                linewidths=1.2, zorder=5)

                # Crosshair lines at dip
                self.ax.axvline(dy, color=color, linewidth=0.5, alpha=0.25, linestyle=":", zorder=3)
                self.ax.axhline(dx, color=color, linewidth=0.5, alpha=0.25, linestyle=":", zorder=3)

                # Label near dot — fixed pixel offset so it never stretches the axis
                self.ax.annotate(
                    f"{dx:.2f} N",
                    xy=(dy, dx),
                    xycoords="data",
                    xytext=(10, 10),
                    textcoords="offset points",
                    fontsize=8, color="#FFFFFF",
                    fontfamily="monospace", fontweight="bold",
                    ha="left", va="bottom",
                    arrowprops=dict(arrowstyle="-", color=color, lw=0.8, alpha=0.7),
                    bbox=dict(boxstyle="round,pad=0.35",
                              fc="#0D1825", ec=color,
                              lw=1.5, alpha=0.97),
                    zorder=8,
                )

        if has_data:
            # Clamp axes to actual data range — prevent annotations from stretching the plot
            all_disp  = [x for k in ("right","centre","left") for x in self._raw[k][0]]
            all_force = [y for k in ("right","centre","left") for y in self._smooth[k]]
            if all_disp and all_force:
                x_max = max(all_force) * 1.15
                y_max = max(all_disp)  * 1.08
                self.ax.set_xlim(0, max(x_max, 1))
                self.ax.set_ylim(0, max(y_max, 1))

            leg = self.ax.legend(
                loc="upper left", fontsize=7.5,
                facecolor="#0A1020", edgecolor="#3A5070",
                labelcolor="#FFFFFF",
                framealpha=0.92,
            )
            for line in leg.get_lines():
                line.set_linewidth(2.5)

        self.draw_idle()

    def _start_flash(self):
        self._flash_frame  = 0
        self._flash_active = True
        self._flash_timer.start()

    def _flash_tick(self):
        self._flash_frame += 1
        self._dirty = True
        if self._flash_frame >= self._flash_total:
            self._flash_timer.stop()
            self._flash_active = False


def force_n_label(n):
    return f"{n:.1f} N"


# ═══════════════════════════════════════════════════════════════════
#  PER-BUTTON MINI LIVE GRAPH  (sits in left panel beside waveform)
# ═══════════════════════════════════════════════════════════════════
class ButtonLiveGraph(FigureCanvas):
    _REDRAW_INTERVAL = 150

    def __init__(self, key, color, label, parent=None):
        screen_r = QtWidgets.QApplication.primaryScreen().geometry()
        fw = max(2.5, screen_r.width() / 280)
        fh = max(1.4, screen_r.height() / 360)
        self.fig = Figure(figsize=(fw, fh), facecolor="#080C12")
        super().__init__(self.fig)
        self.setParent(parent)
        self._key   = key
        self._color = color
        self._label = label
        self.ax = self.fig.add_subplot(111)
        self._style_axes()
        self._raw    = ([], [])
        self._smooth = []
        self._dips   = []
        self._dirty  = False
        self._redraw_timer = QTimer(self)
        self._redraw_timer.setInterval(self._REDRAW_INTERVAL)
        self._redraw_timer.timeout.connect(self._throttled_redraw)
        self._redraw_timer.start()
        self.fig.subplots_adjust(left=0.18, right=0.97, top=0.88, bottom=0.22)

    def _style_axes(self):
        ax = self.ax
        ax.set_facecolor("#080C12")
        ax.tick_params(colors="#FFFFFF", labelsize=6)
        ax.xaxis.label.set_color("#FFFFFF")
        ax.yaxis.label.set_color("#FFFFFF")
        ax.set_xlabel("Disp (mm)",      fontsize=6, color="#FFFFFF", fontweight="bold")
        ax.set_ylabel("Force (N)",      fontsize=6, color="#FFFFFF", fontweight="bold")
        for spine in ax.spines.values():
            spine.set_edgecolor("#3A5070")
        ax.grid(True, color="#1A2538", linewidth=0.4, linestyle="--", alpha=0.5)
        ax.set_title(self._label, fontsize=7, color=self._color,
                     fontfamily="monospace", pad=2, fontweight="bold")

    def reset(self):
        self._raw    = ([], [])
        self._smooth = []
        self._dips   = []
        self._dirty  = True

    def append_point(self, disp_mm: float, force_n: float):
        self._raw[0].append(disp_mm)
        self._raw[1].append(force_n)
        alpha = 0.99
        s = self._smooth
        s.append(force_n if not s else s[-1] * alpha + (1 - alpha) * force_n)
        self._dirty = True

    def mark_dip(self, disp_mm: float, force_n: float):
        self._dips.append((disp_mm, force_n))
        self._dirty = True

    def _throttled_redraw(self):
        if self._dirty:
            self._dirty = False
            self._paint()

    def _paint(self):
        self.ax.cla()
        self._style_axes()
        dl, sl = self._raw[0], self._smooth   # dl=disp_mm list, sl=smoothed_force list
        if dl and sl:
            # displacement on X, smoothed force on Y — correct orientation
            self.ax.plot(dl, sl, color=self._color, linewidth=3.0, alpha=0.15, zorder=1)
            self.ax.plot(dl, sl, color=self._color, linewidth=1.4, alpha=0.95, zorder=2)
            for (dx, dy) in self._dips:
                # dx=disp_mm (X), dy=force raw — find accurate smoothed force for Y
                nearest_idx = min(range(len(dl)), key=lambda i, d=dx: abs(dl[i] - dx))
                dy_accurate = sl[nearest_idx] if nearest_idx < len(sl) else dy
                # scatter at (disp_X, force_Y)
                self.ax.scatter([dx], [dy_accurate], s=55, color=self._color,
                                alpha=1.0, edgecolors="#ffffff", linewidths=0.8, zorder=5)
                self.ax.scatter([dx], [dy_accurate], s=120, color=self._color,
                                alpha=0.2, edgecolors="none", zorder=4)
                self.ax.vlines(dx, 0, dy_accurate, colors=self._color,
                               linestyles=":", linewidth=0.5, alpha=0.35, zorder=3)
                self.ax.text(dx, dy_accurate, f"  {dx:.2f} N", fontsize=6,
                             color="#FFFFFF", fontfamily="monospace",
                             fontweight="bold", va="bottom", zorder=6,
                             bbox=dict(boxstyle="round,pad=0.2", fc="#0D1825",
                                       ec=self._color, lw=0.8, alpha=0.9))
        self.draw_idle()


# ═══════════════════════════════════════════════════════════════════
#  TEST PROGRESS BAR  — large, per-button fill bars, always visible
# ═══════════════════════════════════════════════════════════════════
class TestProgressBar(QWidget):
    """
    Horizontal bar showing test stages.
    Each button section fills with its colour when active/done.
    Layout:  [HOME]──[●BTN 1 ████░░]──[●BTN 2 ████░░]──[●BTN 3 ████░░]──[DONE]
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(48)
        self._step   = 0     # 0=idle 1=btn1 2=btn2 3=btn3 4=done
        self._result = None
        self._build()

    def _build(self):
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(16, 6, 16, 6)
        lay.setSpacing(0)

        # HOME pill
        home_lbl = QtWidgets.QLabel("⌂  HOME")
        home_lbl.setAlignment(Qt.AlignCenter)
        home_lbl.setFixedWidth(72)
        home_lbl.setStyleSheet(
            "color:#9BBCCC; font-size:10px; font-weight:700; letter-spacing:1px;"
            " font-family:Consolas,monospace; background:transparent; border:none;")
        lay.addWidget(home_lbl)
        self._home_lbl = home_lbl

        # 3 button segments
        self._segs = []
        configs = [
            ("BTN 1", "#4A9EFF", 1),
            ("BTN 2", "#F5A623", 2),
            ("BTN 3", "#2ECC71", 3),
        ]
        for name, color, step in configs:
            # connector line before segment
            conn = QtWidgets.QFrame()
            conn.setFixedHeight(3)
            conn.setStyleSheet("background:#1F2E42; border:none;")
            lay.addWidget(conn, stretch=1)

            seg = QtWidgets.QWidget()
            seg.setFixedHeight(34)
            seg.setMinimumWidth(140)
            seg.setStyleSheet(
                f"background:#080C12; border:1px solid #1F2E42;"
                f" border-radius:8px;")
            seg_lay = QtWidgets.QHBoxLayout(seg)
            seg_lay.setContentsMargins(10, 0, 10, 0)
            seg_lay.setSpacing(8)

            # coloured dot indicator
            dot = QtWidgets.QLabel("●")
            dot.setStyleSheet(f"color:#1F2E42; font-size:14px;"
                              f" background:transparent; border:none;")
            dot.setFixedWidth(18)

            # label
            lbl = QtWidgets.QLabel(name)
            lbl.setStyleSheet(f"color:#9BBCCC; font-size:10px; font-weight:700;"
                              f" letter-spacing:1px; font-family:Consolas,monospace;"
                              f" background:transparent; border:none;")

            # fill progress bar
            bar_wrap = QtWidgets.QWidget()
            bar_wrap.setFixedHeight(8)
            bar_wrap.setStyleSheet(f"background:#1A2538; border-radius:4px; border:none;")
            bar_inner = QtWidgets.QWidget(bar_wrap)
            bar_inner.setFixedHeight(8)
            bar_inner.setFixedWidth(0)
            bar_inner.setStyleSheet(f"background:{color}; border-radius:4px; border:none;")
            bar_inner.setObjectName(f"bar_inner_{step}")

            seg_lay.addWidget(dot)
            seg_lay.addWidget(lbl)
            seg_lay.addWidget(bar_wrap, stretch=1)

            lay.addWidget(seg)
            self._segs.append({
                "seg": seg, "dot": dot, "lbl": lbl,
                "bar_wrap": bar_wrap, "bar_inner": bar_inner,
                "color": color, "step": step, "conn": conn
            })

        # connector + DONE pill
        done_conn = QtWidgets.QFrame()
        done_conn.setFixedHeight(3)
        done_conn.setStyleSheet("background:#1F2E42; border:none;")
        lay.addWidget(done_conn, stretch=1)
        self._done_conn = done_conn

        done_lbl = QtWidgets.QLabel("✓  DONE")
        done_lbl.setAlignment(Qt.AlignCenter)
        done_lbl.setFixedWidth(72)
        done_lbl.setStyleSheet(
            "color:#9BBCCC; font-size:10px; font-weight:700; letter-spacing:1px;"
            " font-family:Consolas,monospace; background:transparent; border:none;")
        lay.addWidget(done_lbl)
        self._done_lbl = done_lbl

        # pulse timer for active segment
        self._pulse_val = 0.0
        self._pulse_dir = 1
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(40)
        self._pulse_timer.timeout.connect(self._pulse_tick)

    def _pulse_tick(self):
        self._pulse_val += 0.06 * self._pulse_dir
        if self._pulse_val >= 1.0:
            self._pulse_val = 1.0; self._pulse_dir = -1
        elif self._pulse_val <= 0.2:
            self._pulse_val = 0.2; self._pulse_dir = 1
        # update active bar width
        for s in self._segs:
            if s["step"] == self._step:
                w = s["bar_wrap"].width()
                s["bar_inner"].setFixedWidth(max(0, int(w * self._pulse_val)))
                break

    def set_step(self, step: int, result=None):
        self._step   = step
        self._result = result
        self._pulse_timer.stop()

        # HOME label
        if step >= 1:
            self._home_lbl.setStyleSheet(
                "color:#00FFD1; font-size:10px; font-weight:700; letter-spacing:1px;"
                " font-family:Consolas,monospace; background:transparent; border:none;")
        else:
            self._home_lbl.setStyleSheet(
                "color:#9BBCCC; font-size:10px; font-weight:700; letter-spacing:1px;"
                " font-family:Consolas,monospace; background:transparent; border:none;")

        for s in self._segs:
            color = s["color"]
            if s["step"] < step:
                # completed — full bar, bright style
                s["seg"].setStyleSheet(
                    f"background:#080C12; border:1.5px solid {color}; border-radius:8px;")
                s["dot"].setStyleSheet(
                    f"color:{color}; font-size:16px; background:transparent; border:none;")
                s["lbl"].setStyleSheet(
                    f"color:{color}; font-size:10px; font-weight:700; letter-spacing:1px;"
                    f" font-family:Consolas,monospace; background:transparent; border:none;")
                s["conn"].setStyleSheet(f"background:{color}; border:none;")
                # fill bar 100%
                QTimer.singleShot(0, lambda bi=s["bar_inner"], bw=s["bar_wrap"]:
                                  bi.setFixedWidth(bw.width()))
            elif s["step"] == step:
                # active — pulsing
                s["seg"].setStyleSheet(
                    f"background:#080C12; border:2px solid {color}; border-radius:8px;")
                s["dot"].setStyleSheet(
                    f"color:{color}; font-size:18px; background:transparent; border:none;")
                s["lbl"].setStyleSheet(
                    f"color:{color}; font-size:10px; font-weight:800; letter-spacing:1px;"
                    f" font-family:Consolas,monospace; background:transparent; border:none;")
                s["conn"].setStyleSheet(f"background:{color}; border:none;")
                self._pulse_val = 0.5
                self._pulse_timer.start()
            else:
                # pending
                s["seg"].setStyleSheet(
                    "background:#080C12; border:1px solid #1F2E42; border-radius:8px;")
                s["dot"].setStyleSheet(
                    "color:#1F2E42; font-size:14px; background:transparent; border:none;")
                s["lbl"].setStyleSheet(
                    "color:#9BBCCC; font-size:10px; font-weight:700; letter-spacing:1px;"
                    " font-family:Consolas,monospace; background:transparent; border:none;")
                s["conn"].setStyleSheet("background:#1F2E42; border:none;")
                s["bar_inner"].setFixedWidth(0)

        # DONE pill
        if step == 4:
            done_color = "#2ECC71" if result == "pass" else "#FF4F4F"
            icon = "✓" if result == "pass" else "✗"
            self._done_lbl.setStyleSheet(
                f"color:{done_color}; font-size:11px; font-weight:800; letter-spacing:1px;"
                f" font-family:Consolas,monospace; background:transparent; border:none;")
            self._done_lbl.setText(f"{icon}  DONE")
            self._done_conn.setStyleSheet(f"background:{done_color}; border:none;")
        else:
            self._done_lbl.setStyleSheet(
                "color:#9BBCCC; font-size:10px; font-weight:700; letter-spacing:1px;"
                " font-family:Consolas,monospace; background:transparent; border:none;")
            self._done_lbl.setText("✓  DONE")
            self._done_conn.setStyleSheet("background:#1F2E42; border:none;")

# ═══════════════════════════════════════════════════════════════════════
# class _DeviceWidget(QtWidgets.QWidget):

#     BTN_IDX = {"right button": 0, "centre button": 1, "left button": 2}

#     def __init__(self, color, label, sf, parent=None):
#         super().__init__(parent)
#         self._color    = color
#         self._label    = label.lower()
#         self._active   = False
#         self._pulse    = 0.0
#         self._press    = 0.0
#         self._press_dn = True
#         self._ripples  = []
#         self._force    = 0.0
#         self._scan     = 0.0
#         self._spark_t  = 0.0
#         self.setAttribute(Qt.WA_OpaquePaintEvent, False)
#         self.setMinimumSize(60, 40)

#     def set_active(self, active: bool):
#         self._active = active
#         if active:
#             self._press = 0.0; self._press_dn = True; self._scan = 0.0

#     def set_force(self, norm: float):
#         if norm > 0.10 and self._force <= 0.10:
#             for d in (0.0, 0.14, 0.28):
#                 self._ripples.append([d, 1.0])
#         self._force = norm

#     def tick(self, pulse: float):
#         self._pulse = pulse
#         if self._active:
#             spd = 0.11
#             if self._press_dn:
#                 self._press = min(1.0, self._press + spd)
#                 if self._press >= 1.0: self._press_dn = False
#             else:
#                 self._press = max(0.0, self._press - spd*0.42)
#                 if self._press <= 0.0: self._press_dn = True
#             self._scan = (self._scan + 0.018) % 1.0
#         else:
#             self._press = max(0.0, self._press - 0.07)
#         self._spark_t = (self._spark_t + 0.030) % 1.0
#         self._ripples = [
#             [r[0]+0.020, max(0.0, r[1]-0.016)]
#             for r in self._ripples if r[1] > 0.02
#         ]
#         self.update()

#     def reset(self):
#         self._active = False; self._press = 0.0
#         self._force = 0.0; self._ripples = []; self._scan = 0.0
#         self.update()

#     @staticmethod
#     def _hexpath(cx, cy, w, h):
    
#         hw = w // 2
#         hh = h // 2

#         # 🔥 CUSTOM SHAPE (matches your image)
#         pts = [
#             (cx - int(hw*0.85), cy),          # left mid
#             (cx - int(hw*0.45), cy - hh),     # top-left
#             (cx + int(hw*0.45), cy - hh),     # top-right
#             (cx + int(hw*0.95), cy - int(hh*0.35)),  # right-top slope
#             (cx + int(hw*0.95), cy + int(hh*0.35)),  # right-bottom slope
#             (cx + int(hw*0.45), cy + hh),     # bottom-right
#             (cx - int(hw*0.45), cy + hh),     # bottom-left
#         ]

#         path = QPainterPath()
#         path.moveTo(*pts[0])
#         for pt in pts[1:]:
#             path.lineTo(*pt)
#         path.closeSubpath()

#         return path

#     def paintEvent(self, _):
#         import math
#         from PyQt5.QtGui import (QPainter, QColor, QPen, QLinearGradient,
#                                   QRadialGradient, QPainterPath, QFont)
#         p = QPainter(self)
#         p.setRenderHint(QPainter.Antialiasing)
#         W, H = self.width(), self.height()
#         p.fillRect(0, 0, W, H, QColor(8, 14, 22))

#         hx = self._color.lstrip('#')
#         cr, cg, cb = int(hx[0:2],16), int(hx[2:4],16), int(hx[4:6],16)

#         # ── sizing — true regular hexagon: h = w * sin(60°) ≈ w * 0.866 ──
#         import math as _math
#         pad = max(6, int(min(W,H)*0.05))
#         aw, ah = W-pad*2, H-pad*2
#         # For regular hex: width = 2*r, height = sqrt(3)*r
#         # So if we fit into aw×ah: r = min(aw/2, ah/sqrt(3))
#         r_from_w = aw / 2
#         r_from_h = ah / _math.sqrt(3)
#         hex_r    = min(r_from_w, r_from_h)
#         dw = int(hex_r * 2.2)   # 🔥 make wider
#         dh = int(hex_r * 1.4)   # 🔥 compress height
#         cx2, cy2 = W//2, H//2
#         x0, y0 = cx2-dw//2, cy2-dh//2
#         x1, y1 = cx2+dw//2, cy2+dh//2

#         body = self._hexpath(cx2, cy2, dw, dh)

#         # ── outer halo ────────────────────────────────────────────────
#         if self._active and self._pulse > 0:
#             hr   = int(dw*0.54 + 12*self._pulse)
#             halo = QRadialGradient(cx2, cy2, hr)
#             ha   = int(52*self._pulse)
#             halo.setColorAt(0.68, QColor(cr,cg,cb,0))
#             halo.setColorAt(0.86, QColor(cr,cg,cb,ha))
#             halo.setColorAt(1.00, QColor(cr,cg,cb,0))
#             p.setPen(Qt.NoPen); p.setBrush(halo)
#             p.drawEllipse(cx2-hr, cy2-hr, hr*2, hr*2)

#         # ── body fill ─────────────────────────────────────────────────
#  
#                 idle.setColorAt(0.7, QColor(30,46,66,242))
#                 idle.setColorAt(1.0, QColor(14,22,36,242))
#                 p.setBrush(idle)
#                 p.drawEllipse(bcx-r, bcy-r, r*2, r*2)
#                 # specular
#                 gl = QRadialGradient(bcx-r//3, bcy-r//3, int(r*0.50))
#                 gc = QColor(255,255,255); gc.setAlphaF(0.13)
#                 ge = QColor(255,255,255); ge.setAlphaF(0.0)
#                 gl.setColorAt(0.0,gc); gl.setColorAt(1.0,ge)
#                 p.setPen(Qt.NoPen); p.setBrush(gl)
#                 p.drawEllipse(bcx-r, bcy-r, r*2, r*2)

#             # label inside circle
#             la   = 255 if (mine and self._active) else 145
#             lcol = QColor(255,255,255,la)
#             fs   = max(5, int(r*0.58))
#             p.setFont(QFont("Consolas", fs, QFont.Bold))
#             lbl_str = ["R","C","L"][bi]
#             dy = bcy + (sink if mine else 0)
#             if mine and self._active:
#                 p.setPen(QColor(0,0,0,90))
#                 p.drawText(bcx-r+1, dy-r+1, r*2, r*2, Qt.AlignCenter, lbl_str)
#             p.setPen(lcol)
#             p.drawText(bcx-r, dy-r, r*2, r*2, Qt.AlignCenter, lbl_str)

#         # ── ripples from active circle ────────────────────────────────
#         if btn_idx >= 0 and self._ripples and btn_idx < len(centres):
#             rcx, rcy = centres[btn_idx]
#             maxr = int(min(dw,dh)*0.55)
#             for ring in self._ripples:
#                 rr  = int(maxr*ring[0])
#                 ra  = int(ring[1]*150)
#                 pw  = max(1, int(1.8*ring[1]))
#                 p.setPen(QPen(QColor(cr,cg,cb,ra//3), pw+2))
#                 p.setBrush(Qt.NoBrush)
#                 p.drawEllipse(rcx-rr-1,rcy-rr-1,rr*2+2,rr*2+2)
#                 p.setPen(QPen(QColor(min(255,cr+45),min(255,cg+35),min(255,cb+15),ra), pw))
#                 p.drawEllipse(rcx-rr,rcy-rr,rr*2,rr*2)

#         # ── spark dots ────────────────────────────────────────────────
#         if self._active and self._pulse > 0.32:
#             for i in range(6):
#                 angle = (self._spark_t + i/6)*2*math.pi
#                 ex = cx2 + int(dw*0.44*math.cos(angle))
#                 ey = cy2 + int(dh*0.42*math.sin(angle))
#                 sa3 = int(100*self._pulse*abs(math.sin(self._spark_t*math.pi*3+i)))
#                 if sa3 < 15: continue
#                 p.setPen(Qt.NoPen)
#                 p.setBrush(QColor(min(255,cr+50),min(255,cg+42),min(255,cb+16),sa3))
#                 p.drawEllipse(ex-2,ey-2,4,4)

#         # ── force bar ─────────────────────────────────────────────────
#         if self._active and self._force > 0.01:
#             bw = int(dw*0.48); bh = 3
#             bx = cx2-bw//2;   by = y1-bh-int(dh*0.08)
#             fa3 = int(195*self._force*(0.44+0.56*self._pulse))
#             bgr = QLinearGradient(bx,by,bx+bw,by)
#             bgr.setColorAt(0.0, QColor(cr,cg,cb,0))
#             bgr.setColorAt(0.5, QColor(min(255,cr+38),min(255,cg+28),min(255,cb+10),fa3))
#             bgr.setColorAt(1.0, QColor(cr,cg,cb,0))
#             p.setPen(Qt.NoPen); p.setBrush(bgr)
#             p.drawRoundedRect(bx,by,int(bw*min(1.0,self._force)),bh,2,2)

#         # ── pulsing border ────────────────────────────────────────────
#         if self._active:
#             ba3 = int(55+182*self._pulse)
#             p.setPen(QPen(QColor(cr,cg,cb,ba3), 1.5))
#             p.setBrush(Qt.NoBrush)
#             p.drawPath(self._hexpath(cx2, cy2, dw-1, dh-1))

#         p.end()

"""
Drop-in replacement for _DeviceWidget.
Paste this class into your main file, replacing the existing _DeviceWidget class entirely.

Requirements:
  pip install PyQt5 PyQtSvg          # or: pip install PyQt5-sip PyQt5 PyQt5-Qt5
  The SVG file  "1774520602656_C10_svg_Small.svg"  must sit next to the main .py script,
  renamed to  "c10_device.svg"  (or change SVG_PATH below).

Changes vs original:
  • paintEvent no longer draws the custom hexagon — it renders the C10 SVG instead.
  • All R / C / L circle animations (glow, press, ripple, spark, scan-line) are kept
    intact and painted on top of the SVG.
  • Button positions are expressed as (nx, ny) fractions of the widget rect —
    tune CIRCLES if needed to match the physical button locations on your device.
"""

import os
import math

from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import (
    QPainter, QColor, QPen, QLinearGradient,
    QRadialGradient, QPainterPath, QFont,
)
# QtSvg is part of PyQt5 — install with:  pip install PyQt5
from PyQt5.QtSvg import QSvgRenderer


# ── Resolve SVG path relative to this file ────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
SVG_PATH = os.path.join(_HERE, "c10_device.svg")


class _DeviceWidget(QtWidgets.QWidget):
    """
    Renders the C10 product SVG as the device body and overlays the animated
    R / C / L button circles on top with all original effects preserved.
    """

    BTN_IDX = {"right button": 0, "centre button": 1, "left button": 2}

    # ── Normalised (x, y) positions of each button circle inside the widget ──
    # (0,0) = top-left corner of the widget rect.
    # Adjust these until the dots sit exactly over the physical buttons.
    CIRCLES = [
        (0,  0.58, 0.28),   # R — upper right
        (1,  0.66, 0.54),   # C — middle
        (2,  0.44, 0.76),   # L — lower left
    ]

    def __init__(self, color, label, sf, parent=None):
        super().__init__(parent)
        self._color    = color
        self._label    = label.lower()
        self._active   = False
        self._pulse    = 0.0
        self._press    = 0.0
        self._press_dn = True
        self._ripples  = []
        self._force    = 0.0
        self._scan     = 0.0
        self._spark_t  = 0.0

        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.setMinimumSize(60, 40)

        # Load SVG renderer once — reused every paintEvent
        self._svg = QSvgRenderer(SVG_PATH)
        if not self._svg.isValid():
            print(f"[_DeviceWidget] WARNING: SVG not loaded from {SVG_PATH}")

    # ── Public API (unchanged) ─────────────────────────────────────────────────

    def set_active(self, active: bool):
        self._active = active
        if active:
            self._press = 0.0
            self._press_dn = True
            self._scan = 0.0

    def set_force(self, norm: float):
        if norm > 0.10 and self._force <= 0.10:
            for d in (0.0, 0.14, 0.28):
                self._ripples.append([d, 1.0])
        self._force = norm

    def tick(self, pulse: float):
        self._pulse = pulse
        if self._active:
            spd = 0.11
            if self._press_dn:
                self._press = min(1.0, self._press + spd)
                if self._press >= 1.0:
                    self._press_dn = False
            else:
                self._press = max(0.0, self._press - spd * 0.42)
                if self._press <= 0.0:
                    self._press_dn = True
            self._scan = (self._scan + 0.018) % 1.0
        else:
            self._press = max(0.0, self._press - 0.07)

        self._spark_t = (self._spark_t + 0.030) % 1.0
        self._ripples = [
            [r[0] + 0.020, max(0.0, r[1] - 0.016)]
            for r in self._ripples if r[1] > 0.02
        ]
        self.update()

    def reset(self):
        self._active = False
        self._press  = 0.0
        self._force  = 0.0
        self._ripples = []
        self._scan   = 0.0
        self.update()

    # ── Paint ──────────────────────────────────────────────────────────────────

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)

        W, H = self.width(), self.height()

        # ── 1. Dark background ────────────────────────────────────────────────
        p.fillRect(0, 0, W, H, QColor(8, 14, 22))

        # ── 2. Render SVG centred, keeping aspect ratio ───────────────────────
        if self._svg.isValid():
            svgSize  = self._svg.defaultSize()           # QSize
            svgAR    = svgSize.width() / max(svgSize.height(), 1)
            widgetAR = W / max(H, 1)

            if widgetAR > svgAR:
                rh = H
                rw = int(H * svgAR)
            else:
                rw = W
                rh = int(W / svgAR)

            rx = (W - rw) // 2
            ry = (H - rh) // 2
            svgRect = QtCore.QRectF(rx, ry, rw, rh)
            self._svg.render(p, svgRect)
        else:
            # Fallback: plain dark rect if SVG missing
            p.fillRect(0, 0, W, H, QColor(20, 32, 48))

        # ── 3. Teal scan-line sweep (active only) ─────────────────────────────
        if self._active:
            sy  = int(self._scan * H)
            sg  = QLinearGradient(0, sy - 10, 0, sy + 10)
            sa  = int(52 * self._pulse)
            hx  = self._color.lstrip('#')
            cr, cg, cb = int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
            sg.setColorAt(0.0, QColor(cr, cg, cb, 0))
            sg.setColorAt(0.5, QColor(cr, cg, cb, sa))
            sg.setColorAt(1.0, QColor(cr, cg, cb, 0))
            p.setPen(Qt.NoPen)
            p.setBrush(sg)
            p.drawRect(0, sy - 10, W, 20)

        # ── 4. Button circles ─────────────────────────────────────────────────
        hx  = self._color.lstrip('#')
        cr, cg, cb = int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)

        btn_idx = self.BTN_IDX.get(self._label, -1)
        r = max(8, int(min(W, H) * 0.11))   # circle radius

        centres = []
        for (bi, nx, ny) in self.CIRCLES:
            bcx = int(nx * W)
            bcy = int(ny * H)
            centres.append((bcx, bcy))
            mine = (bi == btn_idx)
            sink = int(r * 0.14 * self._press) if mine else 0

            if mine and self._active:
                # ── outer glow halo ───────────────────────────────────────────
                gr  = int(r * (1.8 + 0.5 * self._pulse))
                grd = QRadialGradient(bcx, bcy + sink, gr)
                ga2 = int(90 * self._pulse)
                grd.setColorAt(0.0, QColor(cr, cg, cb, ga2))
                grd.setColorAt(0.6, QColor(cr, cg, cb, ga2 // 3))
                grd.setColorAt(1.0, QColor(cr, cg, cb, 0))
                p.setPen(Qt.NoPen)
                p.setBrush(grd)
                p.drawEllipse(bcx - gr, bcy + sink - gr, gr * 2, gr * 2)

                # ── pulsing ring ──────────────────────────────────────────────
                ring_a = int(150 + 100 * self._pulse)
                p.setPen(QPen(QColor(
                    min(255, cr + 65), min(255, cg + 48), min(255, cb + 22), ring_a), 2))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(bcx - r, bcy + sink - r, r * 2, r * 2)

                # ── filled circle — radial gradient ───────────────────────────
                br3 = 0.28 + 0.72 * self._pulse
                fr  = int(min(255, cr * br3 + 115))
                fg3 = int(min(255, cg * br3 + 68))
                fb3 = int(min(255, cb * br3 + 26))
                face = QRadialGradient(bcx - r // 4, bcy + sink - r // 4, r)
                face.setColorAt(0.0, QColor(fr, fg3, fb3, 255))
                face.setColorAt(0.5, QColor(cr, cg, cb, 248))
                face.setColorAt(1.0, QColor(
                    max(0, cr - 55), max(0, cg - 55), max(0, cb - 55), 238))
                p.setBrush(face)
                p.setPen(Qt.NoPen)
                ir = int(r * 0.90)
                p.drawEllipse(bcx - ir, bcy + sink - ir, ir * 2, ir * 2)

                # ── top shine ────────────────────────────────────────────────
                shine = QRadialGradient(bcx - r // 3, bcy + sink - r // 3, int(r * 0.60))
                sc1 = QColor(255, 255, 255)
                sc1.setAlphaF(0.52 * (0.18 + 0.82 * self._pulse))
                sc2 = QColor(255, 255, 255)
                sc2.setAlphaF(0.0)
                shine.setColorAt(0.0, sc1)
                shine.setColorAt(1.0, sc2)
                p.setBrush(shine)
                p.drawEllipse(bcx - ir, bcy + sink - ir, ir * 2, ir * 2)

            elif mine and self._press > 0.02:
                # ── fading press ──────────────────────────────────────────────
                fa = int(135 * self._press)
                frd = QRadialGradient(bcx, bcy, r)
                frd.setColorAt(0.0, QColor(cr, cg, cb, fa))
                frd.setColorAt(1.0, QColor(cr, cg, cb, fa // 3))
                p.setBrush(frd)
                p.setPen(QPen(QColor(cr, cg, cb, fa), 1.5))
                p.drawEllipse(bcx - r, bcy - r, r * 2, r * 2)

            else:
                # ── idle dark circle ──────────────────────────────────────────
                p.setPen(QPen(QColor(78, 110, 142, 88), 1.5))
                idle = QRadialGradient(bcx - r // 4, bcy - r // 4, r)
                idle.setColorAt(0.0, QColor(55, 76, 100, 242))
                idle.setColorAt(0.7, QColor(30, 46, 66,  242))
                idle.setColorAt(1.0, QColor(14, 22, 36,  242))
                p.setBrush(idle)
                p.drawEllipse(bcx - r, bcy - r, r * 2, r * 2)
                # specular glint
                gl = QRadialGradient(bcx - r // 3, bcy - r // 3, int(r * 0.50))
                gc = QColor(255, 255, 255); gc.setAlphaF(0.13)
                ge = QColor(255, 255, 255); ge.setAlphaF(0.0)
                gl.setColorAt(0.0, gc); gl.setColorAt(1.0, ge)
                p.setPen(Qt.NoPen); p.setBrush(gl)
                p.drawEllipse(bcx - r, bcy - r, r * 2, r * 2)

            # ── letter label ─────────────────────────────────────────────────
            la   = 255 if (mine and self._active) else 145
            lcol = QColor(255, 255, 255, la)
            fs   = max(5, int(r * 0.58))
            p.setFont(QFont("Consolas", fs, QFont.Bold))
            lbl_str = ["R", "C", "L"][bi]
            dy = bcy + (sink if mine else 0)
            if mine and self._active:
                p.setPen(QColor(0, 0, 0, 90))
                p.drawText(bcx - r + 1, dy - r + 1, r * 2, r * 2,
                           Qt.AlignCenter, lbl_str)
            p.setPen(lcol)
            p.drawText(bcx - r, dy - r, r * 2, r * 2,
                       Qt.AlignCenter, lbl_str)

        # ── 5. Ripples from active button ─────────────────────────────────────
        if btn_idx >= 0 and self._ripples and btn_idx < len(centres):
            rcx, rcy = centres[btn_idx]
            maxr = int(min(W, H) * 0.40)
            for ring in self._ripples:
                rr  = int(maxr * ring[0])
                ra  = int(ring[1] * 150)
                pw  = max(1, int(1.8 * ring[1]))
                p.setPen(QPen(QColor(cr, cg, cb, ra // 3), pw + 2))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(rcx - rr - 1, rcy - rr - 1, rr * 2 + 2, rr * 2 + 2)
                p.setPen(QPen(QColor(
                    min(255, cr + 45), min(255, cg + 35), min(255, cb + 15), ra), pw))
                p.drawEllipse(rcx - rr, rcy - rr, rr * 2, rr * 2)

        # ── 6. Spark dots around active button ────────────────────────────────
        if self._active and self._pulse > 0.32 and btn_idx >= 0 and btn_idx < len(centres):
            scx, scy = centres[btn_idx]
            for i in range(6):
                angle = (self._spark_t + i / 6) * 2 * math.pi
                ex = scx + int(r * 2.2 * math.cos(angle))
                ey = scy + int(r * 2.2 * math.sin(angle))
                sa3 = int(100 * self._pulse * abs(
                    math.sin(self._spark_t * math.pi * 3 + i)))
                if sa3 < 15:
                    continue
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(
                    min(255, cr + 50), min(255, cg + 42), min(255, cb + 16), sa3))
                p.drawEllipse(ex - 2, ey - 2, 4, 4)

        p.end()
class HapticCard(QWidget):
    NUM_BARS = 40
    MAX_N    = 15.0

    def __init__(self, label, color, sf=1.0, parent=None):
        super().__init__(parent)
        self._color      = color
        self._label      = label
        self._sf         = sf
        self._bars       = [0.0] * self.NUM_BARS
        self._cur_n      = 0.0
        self._max_n      = 0.0
        self._max_disp   = 0.0
        self._cur_d      = 0.0
        self._dip_n      = None
        self._dip_d      = None
        self._duration_s = None
        self._active     = False
        self._ended      = False
        self._dip_flash  = 0
        self._elapsed    = QtCore.QElapsedTimer()
        # Animation state
        self._pulse      = 0.0    # 0..1 glow pulse phase
        self._pulse_up   = True
        self._enter_frame= 0      # entrance slide-in frames (0=idle)
        # DIP reveal animation
        self._dip_reveal     = False   # True while counting up
        self._dip_count_val  = 0.0     # current animated value
        self._dip_count_target = 0.0
        self._dip_shimmer    = 0.0     # 0..1 shimmer phase on dip box
        self._dip_shimmer_up = True
        self._dip_particles  = []      # list of (x,y,vx,vy,alpha,color) for dip pop
        self._build_ui(color, label, sf)
        self._timer = QTimer(self)
        self._timer.setInterval(30)   # 33fps for smooth animation
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _build_ui(self, color, label, sf=1.0):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(8, 5, 8, 5)
        root.setSpacing(4)

        hdr = QtWidgets.QHBoxLayout()
        self._dot_lbl = QtWidgets.QLabel("⬤")
        self._dot_lbl.setStyleSheet(f"color:{color}88; font-size:{int(9*sf)}px; background:transparent; border:none;")
        self._title_lbl = QtWidgets.QLabel(label.upper())
        self._title_lbl.setStyleSheet(
            f"color:#D0E8FF; font-size:{int(10*sf)}px; font-weight:800;"
            f" letter-spacing:3px; font-family:Consolas,monospace; background:transparent; border:none;"
        )
        self._status_lbl = QtWidgets.QLabel("STANDBY")
        self._status_lbl.setStyleSheet(
            f"color:#5A7A9A; font-size:{int(7*sf)}px; font-weight:700;"
            f" letter-spacing:1px; font-family:Consolas,monospace; background:transparent; border:none;"
        )
        hdr.addWidget(self._dot_lbl)
        hdr.addWidget(self._title_lbl)
        hdr.addStretch()
        hdr.addWidget(self._status_lbl)
        root.addLayout(hdr)

        # ── Device shape widget — replaces waveform bars ──────────────
        self.wave_widget = _DeviceWidget(color, label, sf, self)
        self.wave_widget.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                                       QtWidgets.QSizePolicy.Expanding)
        self.wave_widget.setMinimumHeight(60)
        self.wave_widget.setStyleSheet("background:transparent; border:none;")
        root.addWidget(self.wave_widget, stretch=1)

        stats = QtWidgets.QHBoxLayout(); stats.setSpacing(5)

        def stat_box(label_txt):
            w = QtWidgets.QWidget()
            w.setStyleSheet(
                f"background:#0D1828; border-radius:7px;"
                f" border:1.5px solid {color}55;"
            )
            vl = QtWidgets.QVBoxLayout(w)
            vl.setContentsMargins(10, 6, 10, 6); vl.setSpacing(2)
            lbl = QtWidgets.QLabel(label_txt)
            lbl.setStyleSheet(
                f"color:#C8DFF0; font-size:{max(8, int(8*sf))}px; font-weight:800;"
                f" letter-spacing:1px; font-family:Consolas,monospace;"
                f" background:transparent; border:none;"
            )
            val = QtWidgets.QLabel("—")
            val.setStyleSheet(
                f"color:{color}; font-size:{max(16, int(16*sf))}px; font-weight:900;"
                f" font-family:Consolas,monospace; background:transparent; border:none;"
            )
            vl.addWidget(lbl); vl.addWidget(val)
            return w, val

        box_max,  self.max_n_lbl    = stat_box("MAX FORCE")
        box_dur,  self.duration_lbl = stat_box("DURATION")
        box_dip,  self.dip_lbl      = stat_box("DIP POINT")
        self._box_dip = box_dip
        self._admin_stat_boxes = (box_max, box_dur)
        stats.addWidget(box_max); stats.addWidget(box_dur); stats.addWidget(box_dip)
        box_max.hide(); box_dur.hide()
        root.addLayout(stats)
        self._update_border(False)

    def highlight_dip(self, passed: bool):
        """Animate DIP POINT reveal: count-up + shimmer + particle burst."""
        if passed:
            self._dip_result_bg     = "#031A0A"
            self._dip_result_border = "#00FF88"
            self._dip_result_fg     = "#00FF88"
        else:
            self._dip_result_bg     = "#1A0303"
            self._dip_result_border = "#FF4444"
            self._dip_result_fg     = "#FF5555"
        self._dip_passed = passed

        # Start count-up from 0 → actual dip value
        target = float(self._dip_d) if self._dip_d is not None else 0.0
        self._dip_count_target = target
        self._dip_count_val    = 0.0
        self._dip_reveal       = True
        self._dip_shimmer      = 0.0
        self._dip_shimmer_up   = True

        # Spawn small particle burst inside the stat box area
        import random as _rnd
        self._dip_particles = []
        color = self._dip_result_border
        for _ in range(14):
            angle = _rnd.uniform(0, 6.283)
            speed = _rnd.uniform(0.4, 2.2)
            import math as _math
            self._dip_particles.append([
                0.5, 0.5,                              # x, y (normalised 0..1 in box)
                _math.cos(angle) * speed * 0.018,      # vx
                _math.sin(angle) * speed * 0.018,      # vy
                1.0,                                    # alpha
                color
            ])

    def _update_border(self, active):
        if active:
            # Strong highlighted glow border when button is being pressed
            glow_alpha = int(100 + 155 * self._pulse)
            r = int(self._color[1:3], 16)
            g = int(self._color[3:5], 16)
            b = int(self._color[5:7], 16)
            self.setStyleSheet(
                f"HapticCard {{ background: rgba({r},{g},{b},18); border-radius:10px;"
                f" border:3px solid {self._color}; }}"
            )
        else:
            self.setStyleSheet(
                f"HapticCard {{ background:#080E18; border-radius:10px;"
                f" border:1px solid {self._color}28; }}"
            )

    def start_run(self):
        self._bars       = [0.0] * self.NUM_BARS
        self._cur_n=0.0; self._cur_d=0.0; self._max_n=0.0; self._max_disp=0.0
        self._dip_n=None; self._dip_d=None
        self._duration_s=None
        self._active=True
        self._ended=False
        self._dip_flash=0
        self._enter_frame = 20   # trigger entrance animation
        self._elapsed.start()
        self.duration_lbl.setText("0.0 s")
        self.dip_lbl.setText("—")
        self.max_n_lbl.setText("—")
        # Animate header to active state — bright white + vivid color
        self._dot_lbl.setStyleSheet(
            f"color:{self._color}; font-size:{int(11*self._sf)}px; background:transparent; border:none;"
        )
        self._title_lbl.setStyleSheet(
            f"color:#FFFFFF; font-size:{int(11*self._sf)}px; font-weight:900;"
            f" letter-spacing:3px; font-family:Consolas,monospace; background:transparent; border:none;"
        )
        self._status_lbl.setText("● ACTIVE")
        self._status_lbl.setStyleSheet(
            f"color:{self._color}; font-size:{int(8*self._sf)}px; font-weight:800;"
            f" letter-spacing:1px; font-family:Consolas,monospace; background:transparent; border:none;"
        )
        self._update_border(True)
        self.wave_widget.set_active(True)

    def push(self, force_n: float, disp_mm: float):
        # Called on main thread via signal
        # If end_run() already called, ignore new data so bars decay cleanly
        if self._ended:
            return
        if not self._active:
            self._active = True
            self._elapsed.start()
        self._cur_n = force_n
        self._cur_d = disp_mm
        if force_n > self._max_n:
            self._max_n    = force_n
            self._max_disp = disp_mm   # displacement at peak force
        norm = min(1.0, max(0.0, force_n / self.MAX_N))
        self._bars.append(norm)
        if len(self._bars) > self.NUM_BARS:
            self._bars.pop(0)

    def end_run(self):
        # Lock the duration — no push() can restart timer after this
        if self._active and self._elapsed.isValid():
            self._duration_s = self._elapsed.elapsed() / 1000.0
        self._active = False
        self._ended  = True    # ← permanent lock — triggers bar decay in _tick()
        self.wave_widget.set_active(False)

    def flash_dip(self, force_n=None, disp_mm=None):
        self._dip_flash = 20
        if force_n is not None: self._dip_n = force_n
        if disp_mm is not None: self._dip_d = disp_mm
        # Freeze the duration timer at the exact moment dip is detected
        self.end_run()

    def reset(self):
        self._bars=[0.0]*self.NUM_BARS; self._cur_n=0.0; self._max_n=0.0; self._max_disp=0.0; self._cur_d=0.0
        self._dip_n=None; self._dip_d=None; self._duration_s=None; self._active=False; self._ended=False; self._dip_flash=0
        self._enter_frame=0; self._pulse=0.0
        self._dip_reveal = False; self._dip_count_val = 0.0; self._dip_count_target = 0.0
        self._dip_shimmer = 0.0; self._dip_shimmer_up = True; self._dip_particles = []
        if hasattr(self, '_dip_result_border'): del self._dip_result_border
        self._update_border(False)
        self.wave_widget.reset()
        # Restore idle header
        self._dot_lbl.setStyleSheet(f"color:{self._color}88; font-size:{int(9*self._sf)}px; background:transparent; border:none;")
        self._title_lbl.setStyleSheet(f"color:#D0E8FF; font-size:{int(10*self._sf)}px; font-weight:800; letter-spacing:3px; font-family:Consolas,monospace; background:transparent; border:none;")
        self._status_lbl.setText("STANDBY")
        self._status_lbl.setStyleSheet(f"color:#5A7A9A; font-size:{int(7*self._sf)}px; font-weight:700; letter-spacing:1px; font-family:Consolas,monospace; background:transparent; border:none;")
        # Reset dip box — fully visible idle state
        self._box_dip.setStyleSheet(f"background:#0A1420; border-radius:6px; border:1px solid {self._color}44;")
        self.dip_lbl.setStyleSheet(f"color:{self._color}; font-size:{max(16, int(16*self._sf))}px; font-weight:900; font-family:Consolas,monospace; background:transparent; border:none;")
        self.max_n_lbl.setText("—"); self.duration_lbl.setText("—"); self.dip_lbl.setText("—")

    def _tick(self):
        # ── Pulse animation when active ───────────────────────────────
        if self._active:
            step = 0.06
            if self._pulse_up:
                self._pulse = min(1.0, self._pulse + step)
                if self._pulse >= 1.0: self._pulse_up = False
            else:
                self._pulse = max(0.0, self._pulse - step)
                if self._pulse <= 0.0: self._pulse_up = True
            self._update_border(True)

        # ── Entrance animation — bars pop in from zero ────────────────
        if self._enter_frame > 0:
            self._enter_frame -= 1

        # ── Decay bars smoothly to zero after run ends ────────────────
        if self._ended:
            decayed = False
            for i in range(len(self._bars)):
                if self._bars[i] > 0.005:
                    self._bars[i] *= 0.72
                    decayed = True
                else:
                    self._bars[i] = 0.0
            if decayed:
                pass  # device widget ticked separately
            else:
                # All bars gone — update header to finished state
                if self._dip_d is not None:
                    self._status_lbl.setText("● DONE")
                    self._status_lbl.setStyleSheet(
                        f"color:#00FF88; font-size:{int(7*self._sf)}px; font-weight:700;"
                        f" letter-spacing:1px; font-family:Consolas,monospace; background:transparent; border:none;"
                    )

        if self._max_disp > 0:
            self.max_n_lbl.setText(f"{self._max_disp:.2f} N")
        if self._active and self._elapsed.isValid():
            self.duration_lbl.setText(f"{self._elapsed.elapsed()/1000.0:.1f} s")
        elif self._duration_s is not None:
            self.duration_lbl.setText(f"{self._duration_s:.1f} s")

        # Only write raw dip text when NOT in count-up animation
        if self._dip_d is not None and not self._dip_reveal and not hasattr(self, '_dip_result_border'):
            self.dip_lbl.setText(f"{self._dip_d:.2f} N")

        if self._dip_flash > 0:
            self._dip_flash -= 1
        elif not self._active:
            self._update_border(False)

        # ── DIP count-up animation ────────────────────────────────────
        if self._dip_reveal:
            speed = max(0.06, (self._dip_count_target - self._dip_count_val) * 0.12)
            self._dip_count_val = min(self._dip_count_target,
                                      self._dip_count_val + speed)
            # Progress 0→1 for visual intensity
            prog = self._dip_count_val / max(self._dip_count_target, 0.001)
            alpha_hex = format(int(80 + 160 * prog), '02X')

            # Box border brightens as count rises
            self._box_dip.setStyleSheet(
                f"background:{self._dip_result_bg}; border-radius:6px;"
                f" border:2px solid {self._dip_result_border}{alpha_hex};"
            )
            # Text colour brightens too — always fully opaque so it's readable
            text_alpha = format(min(255, int(160 + 95 * prog)), '02X')
            self.dip_lbl.setStyleSheet(
                f"color:{self._dip_result_fg}{text_alpha};"
                f" font-size:{max(16, int(16*self._sf))}px; font-weight:900;"
                f" font-family:Consolas,monospace; background:transparent; border:none;"
            )
            self.dip_lbl.setText(f"{self._dip_count_val:.2f} N")

            if abs(self._dip_count_val - self._dip_count_target) < 0.005:
                self._dip_count_val = self._dip_count_target
                self._dip_reveal = False
                # Final locked-in style — large, bright, unmissable
                self._box_dip.setStyleSheet(
                    f"background:{self._dip_result_bg}; border-radius:6px;"
                    f" border:2px solid {self._dip_result_border};"
                )
                self.dip_lbl.setStyleSheet(
                    f"color:{self._dip_result_fg};"
                    f" font-size:{max(16, int(16*self._sf))}px; font-weight:900;"
                    f" font-family:Consolas,monospace; background:transparent; border:none;"
                )
                self.dip_lbl.setText(f"{self._dip_count_val:.2f} N")

        # ── DIP shimmer — breathes the border after reveal ────────────
        elif hasattr(self, '_dip_result_border'):
            step = 0.035
            if self._dip_shimmer_up:
                self._dip_shimmer = min(1.0, self._dip_shimmer + step)
                if self._dip_shimmer >= 1.0: self._dip_shimmer_up = False
            else:
                self._dip_shimmer = max(0.2, self._dip_shimmer - step)
                if self._dip_shimmer <= 0.2: self._dip_shimmer_up = True
            glow_a = format(int(100 + 155 * self._dip_shimmer), '02X')
            # Update box border only — never touch dip_lbl style so value stays visible
            self._box_dip.setStyleSheet(
                f"background:{self._dip_result_bg}; border-radius:6px;"
                f" border:2px solid {self._dip_result_border}{glow_a};"
            )

        # ── Tick dip particles ────────────────────────────────────────
        alive = []
        for pt in self._dip_particles:
            pt[0] += pt[2]; pt[1] += pt[3]
            pt[4] = max(0.0, pt[4] - 0.04)
            if pt[4] > 0.0:
                alive.append(pt)
        self._dip_particles = alive

        # ── Drive device widget each tick ─────────────────────────────
        norm_force = min(1.0, self._cur_n / max(self.MAX_N, 0.001))
        self.wave_widget.set_force(norm_force if self._active else 0.0)
        self.wave_widget.tick(self._pulse)

class ScanLineOverlay(QWidget):
    """
    Transparent overlay drawn on top of the graph canvas.
    Shows a sweeping teal scan line + pulsing corner brackets
    + a stage label. Completely hidden when motor is idle.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setStyleSheet("background: transparent;")
        self._scan_y   = 0.0   # 0.0..1.0 normalised scan position
        self._pulse    = 0.0   # corner-bracket brightness 0..1
        self._pulse_up = True
        self._dot_off  = 0     # scrolling dot grid phase
        self._stage    = ""
        self._active   = False

        self._timer = QTimer(self)
        self._timer.setInterval(16)          # ~60 fps
        self._timer.timeout.connect(self._tick)

    def start(self, stage=""):
        self._active = True
        self._stage  = stage
        self._scan_y = 0.0
        self.resize(self.parent().size())
        self.raise_()
        self.show()
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self._active = False
        self.hide()

    def set_stage(self, stage):
        self._stage = stage
        self.update()

    def _tick(self):
        self._scan_y += 0.003
        if self._scan_y > 1.08:
            self._scan_y = -0.05

        step = 0.035
        if self._pulse_up:
            self._pulse = min(1.0, self._pulse + step)
            if self._pulse >= 1.0: self._pulse_up = False
        else:
            self._pulse = max(0.0, self._pulse - step)
            if self._pulse <= 0.0: self._pulse_up = True

        self._dot_off = (self._dot_off + 1) % 20
        self.update()

    def paintEvent(self, _event):
        if not self._active:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()

        # ── drifting dot grid ──────────────────────────────────────
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 255, 209, 14))
        sp = 22
        for x in range(0, W + sp, sp):
            for y in range(-sp + self._dot_off, H + sp, sp):
                p.drawEllipse(x - 1, y - 1, 2, 2)

        # ── scan line ─────────────────────────────────────────────
        sy = int(self._scan_y * H)
        grad = QLinearGradient(0, sy - 24, 0, sy + 24)
        grad.setColorAt(0.0, QColor(0, 255, 209,   0))
        grad.setColorAt(0.45, QColor(0, 255, 209,  55))
        grad.setColorAt(0.5, QColor(0, 255, 209, 200))
        grad.setColorAt(0.55, QColor(0, 255, 209,  55))
        grad.setColorAt(1.0, QColor(0, 255, 209,   0))
        pen = QPen(); pen.setBrush(grad); pen.setWidth(2)
        p.setPen(pen)
        p.drawLine(0, sy, W, sy)

        # ── corner brackets ────────────────────────────────────────
        a   = int(60 + 180 * self._pulse)
        pad = 14; sz = 20
        pen2 = QPen(QColor(0, 255, 209, a), 2)
        p.setPen(pen2)
        for cx, cy, dx, dy in [
            (pad,       pad,       +1, +1),
            (W - pad,   pad,       -1, +1),
            (pad,       H - pad,   +1, -1),
            (W - pad,   H - pad,   -1, -1),
        ]:
            p.drawLine(cx, cy, cx + dx * sz, cy)
            p.drawLine(cx, cy, cx,           cy + dy * sz)

        # ── stage label ────────────────────────────────────────────
        if self._stage:
            label_a = int(120 + 120 * self._pulse)
            p.setPen(QColor(0, 255, 209, label_a))
            f = QFont("Consolas", 10, QFont.Bold)
            p.setFont(f)
            p.drawText(pad + 4, pad + 16, self._stage)

        p.end()


def _sleep_animated(ms):
    """
    Drop-in replacement for time.sleep() that keeps the Qt event loop
    alive so timers and paint events fire during long gcode waits.
    """
    from PyQt5.QtCore import QEventLoop
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()


class EmployeeEntry(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("bluArmor — Login")
        self.setWindowFlags(QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint)
        screen_geometry = QtWidgets.QApplication.primaryScreen().geometry()
        self.setGeometry(screen_geometry)
        self.setStyleSheet("QWidget { background-color: #080C12; font-family: 'Segoe UI', sans-serif; }")
        self.raise_(); self.activateWindow(); self.show()
        self.initUI()

    def initUI(self):
        root = QVBoxLayout(self)
        root.setAlignment(Qt.AlignCenter)
        root.setContentsMargins(0,0,0,0)

        card = QFrame()
        card.setFixedSize(460, 500)
        card.setStyleSheet("""
            QFrame {
                background-color: #0E1520;
                border-radius: 16px;
                border: 1px solid #1F2E42;
            }
        """)
        vbox = QVBoxLayout(card)
        vbox.setContentsMargins(44, 38, 44, 38)
        vbox.setSpacing(0)

        brand = QLabel("bluArmor")
        brand.setStyleSheet("color:#00FFD1; font-size:20px; font-weight:800; letter-spacing:1px; background:transparent; border:none;")
        vbox.addWidget(brand)
        vbox.addSpacing(3)

        sub = QLabel("BUTTON DISPLACEMENT JIG SYSTEM")
        sub.setStyleSheet("color:#9BBCCC; font-size:9px; font-weight:700; letter-spacing:3px; font-family:Consolas,monospace; background:transparent; border:none;")
        vbox.addWidget(sub)
        vbox.addSpacing(26)

        div = QFrame(); div.setFrameShape(QFrame.HLine); div.setFixedHeight(1)
        div.setStyleSheet("background:#1F2E42; border:none;")
        vbox.addWidget(div)
        vbox.addSpacing(26)

        heading = QLabel("Select Employee")
        heading.setStyleSheet("color:#F0F8FF; font-size:20px; font-weight:700; background:transparent; border:none;")
        vbox.addWidget(heading)
        vbox.addSpacing(4)

        hint = QLabel("Search or choose your name to begin the session")
        hint.setStyleSheet("color:#9BBCCC; font-size:12px; background:transparent; border:none;")
        vbox.addWidget(hint)
        vbox.addSpacing(20)

        self.emp_dropdown = QComboBox()
        self.emp_dropdown.setEditable(True)
        self.emp_dropdown.setFixedHeight(44)
        self.emp_dropdown.setStyleSheet("""
            QComboBox {
                background:#080C12; border:1.5px solid #263548; border-radius:8px;
                color:#00FFD1; font-size:13px; font-weight:600;
                font-family:Consolas,monospace; padding:0 12px; letter-spacing:1px;
            }
            QComboBox:focus { border-color:#00FFD1; }
            QComboBox::drop-down { border:none; width:28px; }
            QComboBox QAbstractItemView {
                background:#0E1520; border:1px solid #1F2E42; color:#F0F8FF;
                selection-background-color:rgba(0,255,209,.1); selection-color:#00FFD1;
                font-size:13px; padding:4px; outline:0;
            }
            QLineEdit { background:transparent; color:#00FFD1; border:none; font-size:13px; font-family:Consolas,monospace; }
        """)
        employee_list = fetch_employees()
        if employee_list:
            self.emp_dropdown.addItems(employee_list)
            completer = QCompleter(employee_list)
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            self.emp_dropdown.setCompleter(completer)
        else:
            self.emp_dropdown.addItem("No employees available")
        self.emp_dropdown.setCurrentIndex(-1)
        vbox.addWidget(self.emp_dropdown)
        vbox.addSpacing(16)

        model_label = QLabel("Select Model")
        model_label.setStyleSheet(
            "color:#F0F8FF; font-size:14px; font-weight:700; "
            "background:transparent; border:none;"
        )
        vbox.addWidget(model_label)
        vbox.addSpacing(8)

        self.model_dropdown = QComboBox()
        self.model_dropdown.setFixedHeight(44)
        self.model_dropdown.addItems(["C10", "HS3"])
        self.model_dropdown.setCurrentIndex(-1)
        self.model_dropdown.setStyleSheet(self.emp_dropdown.styleSheet())
        vbox.addWidget(self.model_dropdown)
        vbox.addSpacing(16)

        fetch_btn = QPushButton("PROCEED  →")
        fetch_btn.setFixedHeight(48)
        fetch_btn.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
        fetch_btn.setStyleSheet("""
            QPushButton { background:#00FFD1; color:#030E0A; border:none; border-radius:9px; font-size:13px; font-weight:800; letter-spacing:3px; }
            QPushButton:hover { background:#33FFE0; }
            QPushButton:pressed { background:#00C9A7; }
        """)
        fetch_btn.clicked.connect(self.fetch_details)
        vbox.addWidget(fetch_btn)
        root.addWidget(card)

    def fetch_details(self):
        employee_id = self.emp_dropdown.currentText().strip()
        selected_model = self.model_dropdown.currentText().strip().upper()

        if not employee_id:
            self.show_popup(self, "warn", "Error", "Please select an Employee ID")
            return

        if selected_model not in ("C10", "HS3"):
            self.show_popup(self, "warn", "Error", "Please select a Model")
            return

        model_config = {
            "C10": {
                "item_code": "SF-C10-Button Test",
                "system": "c10ButtonDisplacement",
            },
            "HS3": {
                "item_code": "SF-HS3-Button Test",
                "system": "hs3cassini",
            },
        }

        config = model_config[selected_model]
        # job_card_id = fetch_job_card_for_employee(employee_id, config["item_code"])
        job_card_id = "123"

        if not job_card_id:
            self.show_popup(
                self,
                "warn",
                "Error",
                f"No active Job Card found for {config['item_code']}",
            )
            return

        self.test_window = Ui_MainWindow(
            employee_id,
            job_card_id,
            selected_model,
            config["system"],
        )
        self.test_window.show()
        self.close()
class RunThread(QThread):
    """
    Runs the full motor sequence off the main thread so the UI
    stays responsive. The main thread only receives signals.
    plot_ready is emitted when motor work is done — plot() runs
    on the main thread via this signal, avoiding the segfault.
    """
    finished  = pyqtSignal()
    plot_ready = pyqtSignal()   # ← triggers plot() on main thread

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        self._fn()
        self.finished.emit()



class SerialThread(QThread):
    data_received = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    def __init__(self, port, poort, baudrate):
        super().__init__()
        self.port = port
        self.poort = poort  # Kept for existing callers; this is the GRBL port.
        self.baudrate = baudrate
        self.serial_port = None
        self.ser = None
        self.running = False
        self.callback = None
        self.pending_grbl_commands = 0
        self._keep_grbl_open = False
        self._homing_requested = False

    def connect_ports(self):
        if os.path.realpath(self.port) == os.path.realpath(self.poort):
            raise ValueError("Arduino and GRBL must use different serial ports")
        try:
            if self.ser is None or not self.ser.is_open:
                self.ser = serial.Serial(self.poort, self.baudrate, timeout=0.1, write_timeout=1)
                self._wait_grbl_startup()
            if self.serial_port is None or not self.serial_port.is_open:
                self.serial_port = serial.Serial(self.port, self.baudrate, timeout=0.1, write_timeout=1)
        except Exception:
            self.close_ports()
            raise

    def _wait_grbl_startup(self):
        """Wait for controller startup before sending any queued commands."""
        self.pending_grbl_commands = 0
        self._homing_requested = False
        deadline = time.monotonic() + 4.0
        next_query = 0.0
        saw_banner = False
        while time.monotonic() < deadline:
            response = self.ser.readline().decode("utf-8", errors="ignore").strip()
            if response.startswith("Grbl"):
                print(f"GRBL boot: {response}", flush=True)
                saw_banner = True
                self.ser.write(b"?")
                next_query = time.monotonic() + 0.2
            elif response.startswith("<") and (saw_banner or time.monotonic() >= deadline - 2.0):
                print(f"GRBL connected: {response}", flush=True)
                return  # Alarm is reported, never silently unlocked.
            if time.monotonic() >= next_query:
                self.ser.write(b"?")
                next_query = time.monotonic() + 0.2
        raise RuntimeError("No GRBL startup/status response; check GRBL port and cable")

    def close_ports(self, close_grbl=True):
        ports = (self.serial_port, self.ser) if close_grbl else (self.serial_port,)
        for port in ports:
            if port is not None and port.is_open:
                try:
                    port.close()
                except serial.SerialException:
                    logger.exception("Could not close serial port")

    def run(self):
        try:
            self.connect_ports()
            pending_line = bytearray()
            while self.running:
                # Preserve partial samples across serial read timeouts.
                raw = self.serial_port.readline()
                if not raw:
                    continue
                pending_line.extend(raw)
                while b"\n" in pending_line:
                    line, _, remainder = pending_line.partition(b"\n")
                    pending_line = bytearray(remainder)
                    data = line.decode("utf-8", errors="ignore").strip()
                    if not data:
                        continue
                    self.data_received.emit(data)
                    if self.callback:
                        self.callback(data)
        except Exception as exc:
            logger.exception("Serial reader failed")
            self.error_occurred.emit(str(exc))
        finally:
            self.running = False
            self.close_ports(close_grbl=not self._keep_grbl_open)

    def start(self, priority=QThread.InheritPriority):
        self.running = True
        super().start(priority)

    def stop(self, keep_grbl_open=False):
        self._keep_grbl_open = keep_grbl_open
        self.running = False
        if self.isRunning():
            self.wait()
        self.close_ports(close_grbl=not keep_grbl_open)

    def send_gcode(self, gcode, callback=None):
        self.callback = callback
        self.connect_ports()
        if gcode == "$H":
            if self._homing_requested:
                print("HOME already requested; duplicate $H ignored", flush=True)
                return
            self._homing_requested = True
        if not gcode.startswith("$J="):
            print(f"GRBL TX: {gcode}", flush=True)
        self.ser.write(gcode.encode("utf-8") + b"\n")
        self.pending_grbl_commands += 1




class Ui_MainWindow(JigCalibrationMixin, QMainWindow):
    # Thread-safe signals — emitted from SerialThread, received on main thread
    _sig_push_right  = QtCore.pyqtSignal(float, float)
    _sig_push_centre = QtCore.pyqtSignal(float, float)
    _sig_push_left   = QtCore.pyqtSignal(float, float)
    _sig_motor_error = QtCore.pyqtSignal(str)

    def __init__(self, employee_id, job_card_id, selected_model="C10",
                 erp_system="c10ButtonDisplacement"):
        super().__init__()
        self.employee_id = employee_id
        self.job_card_id = job_card_id
        self.selected_model = selected_model
        self.erp_system = erp_system

        # ── initialise data lists BEFORE setupUi so clear_all_datas works ──
        self.data1=[]
        self.data2=[]
        self.data3=[]
        self.data4=[]
        self.data5=[]
        self.serial_number = None

        self.setupUi(self)
        self._sig_motor_error.connect(self._show_motor_error)

        self.serial_thread = SerialThread(ARDUINO_PORT, GRBL_PORT, 115200)
        self.serial_thread.data_received.connect(self.handle_data)
        self.serial_thread.error_occurred.connect(self._show_serial_error)

        self.pass_count = get_pass_count(self.employee_id, self.job_card_id)
        self._calibration_mode = False
        self._calibration_attempt = None
        self._observed_calibration_period = None
        self._calibration_reminder_date = None
        init_calibration_db()
        self._calibration_timer = QTimer(self)
        self._calibration_timer.timeout.connect(self._check_calibration_reset)
        self._homed = False
        self._homing_in_progress = False
        self.pushButton_19.setEnabled(False)
        QTimer.singleShot(0, self.home_gcode)
        self._calibration_timer.start(1000)
        self._check_calibration_reset()



    def setupUi(self, MainWindow):

        BG     = "#080C12"
        S1     = "#121E2E"
        S2     = "#162030"
        S3     = "#1E2D40"
        BORDER = "#2A4060"
        BORDER2= "#345070"
        ACCENT = "#00FFD1"
        WARN   = "#F5A623"
        BLUE   = "#4A9EFF"
        GREEN  = "#2ECC71"
        RED    = "#FF4F4F"
        TEXT   = "#FFFFFF"
        TEXT2  = "#D0E4F8"
        TEXT3  = "#B0CCE0"
        MONO   = "'Consolas','Courier New',monospace"
        SANS   = "'Segoe UI','SF Pro Display',sans-serif"

        GLOBAL_CSS = f"""
            QMainWindow,QWidget {{
                background:{BG}; color:{TEXT};
                font-family:{SANS};
            }}
            QLabel  {{ background:transparent; border:none; color:{TEXT}; }}
            QScrollBar:vertical {{
                background:{BG}; width:5px; border-radius:3px;
            }}
            QScrollBar::handle:vertical {{
                background:{BORDER2}; border-radius:3px; min-height:20px;
            }}
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {{ height:0; }}
        """

        MainWindow.setObjectName("MainWindow")
        screen = QtWidgets.QApplication.primaryScreen().geometry()
        SW, SH = screen.width(), screen.height()
        MainWindow.resize(SW, SH)
        MainWindow.showMaximized()
        # Scale factor relative to reference 1366x800
        SF = min(SW / 1366.0, SH / 800.0)
        MainWindow.setWindowTitle("bluArmor — Button Displacement Jig")
        MainWindow.setStyleSheet(GLOBAL_CSS)

        self.centralwidget = QtWidgets.QWidget(MainWindow)
        root_vbox = QtWidgets.QVBoxLayout(self.centralwidget)
        root_vbox.setContentsMargins(0,0,0,0)
        root_vbox.setSpacing(0)

        # progress_bar stub — progress bar removed from UI, stub prevents crashes
        class _NullProgress:
            def set_step(self, *a, **kw): pass
        self.progress_bar = _NullProgress()

        # ── TOP BAR ───────────────────────────────────────────────────
        topbar = QtWidgets.QWidget()
        topbar.setFixedHeight(int(44*SF))
        topbar.setStyleSheet(f"background:{S1}; border-bottom:1px solid {BORDER};")
        tb_lay = QtWidgets.QHBoxLayout(topbar)
        tb_lay.setContentsMargins(20,0,20,0); tb_lay.setSpacing(12)

        import os as _os

        class _RoundedLogo(QtWidgets.QWidget):
            """Paints the logo pixmap clipped to a rounded rect with soft faded edges."""
            def __init__(self, px, w, h, radius=7, parent=None):
                super().__init__(parent)
                self._px     = px.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self._radius = radius
                self.setFixedSize(self._px.width(), self._px.height())
                self.setAttribute(Qt.WA_TranslucentBackground)

            def paintEvent(self, _event):
                from PyQt5.QtGui import QPainter, QPainterPath, QColor, QLinearGradient
                p = QPainter(self)
                p.setRenderHint(QPainter.Antialiasing)
                p.setRenderHint(QPainter.SmoothPixmapTransform)
                W, H = self.width(), self.height()
                # clip to rounded rectangle
                path = QtGui.QPainterPath()
                path.addRoundedRect(0, 0, W, H, self._radius, self._radius)
                p.setClipPath(path)
                p.drawPixmap(0, 0, self._px)
                # soft left+right edge fade so it blends into topbar
                for side_x, x2 in [(0, 18), (W - 18, W)]:
                    fade = QLinearGradient(side_x, 0, x2, 0)
                    fade.setColorAt(0.0, QColor(18, 30, 46, 210))
                    fade.setColorAt(1.0, QColor(18, 30, 46, 0))
                    p.setPen(Qt.NoPen)
                    p.setBrush(fade)
                    p.drawRect(side_x, 0, abs(x2 - side_x), H)
                p.end()

        _logo_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "bluarmor_logo.jpeg")
        _logo_px   = QtGui.QPixmap(_logo_path)
        if not _logo_px.isNull():
            brand_lbl = _RoundedLogo(_logo_px, 110, 34, radius=7)
        else:
            brand_lbl = QtWidgets.QLabel("bluArmor")
            brand_lbl.setStyleSheet(f"color:{ACCENT}; font-size:16px; font-weight:800; letter-spacing:1px;")
        tb_sep = QtWidgets.QFrame()
        tb_sep.setFixedSize(1,20)
        tb_sep.setStyleSheet(f"background:{BORDER};")
        self.label_5 = QtWidgets.QLabel("Button Displacement Test Infra")
        self.label_5.setObjectName("label_5")
        self.label_5.setStyleSheet(f"color:{TEXT2}; font-size:11px; letter-spacing:1px; font-weight:600;")
        tb_lay.addWidget(brand_lbl)
        tb_lay.addWidget(tb_sep)
        tb_lay.addWidget(self.label_5)
        tb_lay.addStretch()

        # ── session info chips — unified muted steel, ultra-compact ──
        CHIP_BG     = "#111B28"
        CHIP_BORDER = "#2C4058"
        CHIP_KEY    = "#5A7A94"
        CHIP_VAL    = "#C8DCF0"

        def top_chip(label_txt, val_txt):
            w = QtWidgets.QWidget()
            w.setFixedHeight(int(22*SF))
            w.setStyleSheet(
                f"background:{CHIP_BG}; border:1px solid {CHIP_BORDER}; border-radius:4px;"
            )
            hl = QtWidgets.QHBoxLayout(w)
            hl.setContentsMargins(5, 0, 6, 0); hl.setSpacing(3)
            lk = QtWidgets.QLabel(label_txt)
            lk.setStyleSheet(
                f"color:{CHIP_KEY}; font-size:6px; font-weight:900;"
                f" letter-spacing:0.8px; font-family:Consolas,monospace;"
                f" background:transparent; border:none;"
            )
            sep = QtWidgets.QLabel("·")
            sep.setStyleSheet(f"color:{CHIP_BORDER}; font-size:8px; background:transparent; border:none;")
            lv2 = QtWidgets.QLabel(val_txt)
            lv2.setStyleSheet(
                f"color:{CHIP_VAL}; font-size:8px; font-weight:800;"
                f" font-family:Consolas,monospace; background:transparent; border:none;"
            )
            lv2.setMinimumWidth(28)
            hl.addWidget(lk); hl.addWidget(sep); hl.addWidget(lv2)
            return w, lv2

        chip_emp, self.lbl_employee = top_chip("EMP",   "—")
        chip_job, self.lbl_jobcard  = top_chip("JOB",   "—")
        chip_mod, self.lbl_model    = top_chip("MODEL", "C10")
        tb_lay.addWidget(chip_emp)
        tb_lay.addWidget(chip_job)
        tb_lay.addWidget(chip_mod)

        # ── thin separator ─────────────────────────────────────────────
        tb_sep2 = QtWidgets.QFrame(); tb_sep2.setFixedSize(1,20)
        tb_sep2.setStyleSheet(f"background:{BORDER};")
        tb_lay.addWidget(tb_sep2)
        tb_lay.addSpacing(4)

        # ── JOB CONTROL buttons — unified muted steel ─────────────────
        BTN_FG     = "#9BB8D0"
        BTN_BG     = "transparent"
        BTN_BORDER = "#2C4058"

        def tb_job_btn(icon, label, obj):
            b = QtWidgets.QPushButton(f"{icon} {label}")
            b.setObjectName(obj)
            b.setFixedHeight(int(28*SF))
            b.setMinimumWidth(int(68*SF))
            b.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
            b.setStyleSheet(f"""
                QPushButton {{
                    background:{BTN_BG};
                    color:{BTN_FG};
                    border:1px solid {BTN_BORDER};
                    border-radius:5px;
                    font-size:9px; font-weight:700;
                    letter-spacing:0.5px;
                    font-family:Consolas,monospace;
                    padding:0 8px;
                }}
                QPushButton:hover {{
                    background:#1A2E44;
                    color:#D8EAF8;
                    border-color:#4A6880;
                }}
                QPushButton:pressed {{
                    background:#0F1E2E;
                    color:{BTN_FG};
                }}
            """)
            return b

        self.pause_button    = tb_job_btn("⏸", "PAUSE",    "pause_button")
        self.resume_button   = tb_job_btn("▶", "RESUME",   "resume_button")
        self.complete_button = tb_job_btn("✓", "COMPLETE", "complete_button")
        tb_lay.addWidget(self.pause_button)
        tb_lay.addWidget(self.resume_button)
        tb_lay.addWidget(self.complete_button)
        tb_lay.addSpacing(8)

        view_wrap = QtWidgets.QWidget()
        view_wrap.setStyleSheet("background:transparent;border:none;")
        view_layout = QtWidgets.QHBoxLayout(view_wrap)
        view_layout.setContentsMargins(8, 0, 8, 0)
        view_layout.setSpacing(9)
        self.technician_view_label = QtWidgets.QLabel("Technician")
        self.admin_view_label = QtWidgets.QLabel("Admin")
        self.technician_view_label.setStyleSheet("color:#FFFFFF;font-size:11px;font-weight:700;")
        self.admin_view_label.setStyleSheet("color:#71899E;font-size:11px;font-weight:600;")
        self.admin_view_toggle = ViewToggle()
        self.admin_view_toggle.toggled.connect(self._set_admin_view)
        view_layout.addWidget(self.technician_view_label)
        view_layout.addWidget(self.admin_view_toggle)
        view_layout.addWidget(self.admin_view_label)
        tb_lay.addWidget(view_wrap)

        self.topbar_clock = QtWidgets.QLabel("--:--:--")
        self.topbar_clock.setStyleSheet(f"color:#C0D4E8; font-size:11px; font-family:Consolas,monospace;")
        tb_lay.addWidget(self.topbar_clock)
        root_vbox.addWidget(topbar)

        # ── STATUS BANNER ─────────────────────────────────────────────
        self.label_2 = QtWidgets.QLabel("●   NOT ACTIVATED")
        self.label_2.setObjectName("label_2")
        self.label_2.setAlignment(Qt.AlignCenter)
        self.label_2.setFixedHeight(int(36*SF))
        self.label_2.setStyleSheet(f"""
            QLabel {{
                background:{S2};
                color:{TEXT3};
                font-size:12px; font-weight:700;
                letter-spacing:3px;
                font-family:Consolas,monospace;
                border-bottom:1px solid {BORDER};
            }}
        """)
        root_vbox.addWidget(self.label_2)

        # ── BODY ROW ─────────────────────────────────────────────────
        body_row = QtWidgets.QWidget()
        body_row.setStyleSheet(f"background:{BG};")
        body_hbox = QtWidgets.QHBoxLayout(body_row)
        body_hbox.setContentsMargins(0,0,0,0)
        body_hbox.setSpacing(0)

        # ─────────────────────────────────────────────────────────────────
        #  COLUMN 1 — Controls  (stretch=22 → ~22% of screen)
        # ─────────────────────────────────────────────────────────────────
        col1_w = QtWidgets.QWidget()
        col1_w.setStyleSheet(f"background:{S1}; border-right:1px solid {BORDER};")
        col1_vbox = QtWidgets.QVBoxLayout(col1_w)
        col1_vbox.setContentsMargins(0,0,0,0)
        col1_vbox.setSpacing(0)

        # inner scroll so it never clips on very small screens
        col1_inner = QtWidgets.QWidget()
        col1_inner.setStyleSheet("background:transparent;")
        lv = QtWidgets.QVBoxLayout(col1_inner)
        lv.setContentsMargins(0,0,0,0); lv.setSpacing(0)

        def sec_divider():
            d = QtWidgets.QFrame(); d.setFixedHeight(1)
            d.setStyleSheet(f"background:{BORDER}; margin:0 10px;")
            return d

        def sec_label(txt):
            l = QtWidgets.QLabel(txt)
            l.setStyleSheet(f"color:#9BBCCC; font-size:8px; font-weight:700; letter-spacing:2px; font-family:Consolas,monospace; padding:6px 12px 4px 12px;")
            return l

        def info_row(key, val, val_color=TEXT):
            w = QtWidgets.QWidget(); w.setStyleSheet("background:transparent;")
            wl = QtWidgets.QVBoxLayout(w)
            wl.setContentsMargins(10,0,10,6); wl.setSpacing(2)
            k = QtWidgets.QLabel(key)
            k.setStyleSheet(f"color:#9BBCCC; font-size:8px; font-weight:700; letter-spacing:1px; font-family:Consolas,monospace;")
            v = QtWidgets.QLabel(val)
            v.setStyleSheet(f"color:{val_color}; font-size:10px; font-weight:600; font-family:Consolas,monospace; background:{BG}; border:1px solid {BORDER2}; border-radius:5px; padding:3px 8px;")
            wl.addWidget(k); wl.addWidget(v)
            return w, v

        # LIVE COUNT
        lv.addWidget(sec_label("LIVE COUNT"))
        pf_c = QtWidgets.QWidget(); pf_c.setStyleSheet("background:transparent;")
        pf_h = QtWidgets.QHBoxLayout(pf_c)
        pf_h.setContentsMargins(10,0,10,6); pf_h.setSpacing(5)
        pass_card = QtWidgets.QWidget()
        pass_card.setStyleSheet(f"background:#05180F; border:1px solid #143A22; border-radius:7px;")
        pc_lay = QtWidgets.QVBoxLayout(pass_card); pc_lay.setContentsMargins(8,7,8,7); pc_lay.setSpacing(1)
        pc_lbl = QtWidgets.QLabel("PASS"); pc_lbl.setStyleSheet(f"color:{GREEN}; font-size:8px; font-weight:700; letter-spacing:2px; font-family:Consolas,monospace;")
        self.pass_count_lbl = QtWidgets.QLabel("0"); self.pass_count_lbl.setStyleSheet(f"color:{GREEN}; font-size:26px; font-weight:800;")
        pc_lay.addWidget(pc_lbl); pc_lay.addWidget(self.pass_count_lbl)
        fail_card = QtWidgets.QWidget()
        fail_card.setStyleSheet(f"background:#180505; border:1px solid #3A1414; border-radius:7px;")
        fc_lay = QtWidgets.QVBoxLayout(fail_card); fc_lay.setContentsMargins(8,7,8,7); fc_lay.setSpacing(1)
        fc_lbl = QtWidgets.QLabel("FAIL"); fc_lbl.setStyleSheet(f"color:{RED}; font-size:8px; font-weight:700; letter-spacing:2px; font-family:Consolas,monospace;")
        self.fail_count_lbl = QtWidgets.QLabel("0"); self.fail_count_lbl.setStyleSheet(f"color:{RED}; font-size:26px; font-weight:800;")
        fc_lay.addWidget(fc_lbl); fc_lay.addWidget(self.fail_count_lbl)
        pf_h.addWidget(pass_card); pf_h.addWidget(fail_card)
        lv.addWidget(pf_c)
        lv.addWidget(sec_divider())

        # ── SCAN & RUN — styled card ──────────────────────────────────
        scan_card = QtWidgets.QWidget()
        scan_card.setStyleSheet(f"""
            QWidget {{
                background:#0A1520;
                border:1px solid {BORDER2};
                border-radius:10px;
            }}
        """)
        scan_card_lay = QtWidgets.QVBoxLayout(scan_card)
        scan_card_lay.setContentsMargins(10, 8, 10, 10); scan_card_lay.setSpacing(6)

        # Header row with icon
        scan_hdr_lay2 = QtWidgets.QHBoxLayout(); scan_hdr_lay2.setSpacing(6)
        scan_icon = QtWidgets.QLabel("⬡")
        scan_icon.setStyleSheet(f"color:{ACCENT}; font-size:12px; background:transparent; border:none;")
        scan_title = QtWidgets.QLabel("SCAN & RUN")
        scan_title.setStyleSheet(f"color:#FFFFFF; font-size:9px; font-weight:800; letter-spacing:2px; font-family:Consolas,monospace; background:transparent; border:none;")
        scan_sub = QtWidgets.QLabel("Scan barcode to begin test")
        scan_sub.setStyleSheet(f"color:#5A7A90; font-size:8px; font-family:Consolas,monospace; background:transparent; border:none;")
        scan_hdr_lay2.addWidget(scan_icon); scan_hdr_lay2.addWidget(scan_title); scan_hdr_lay2.addStretch()
        scan_card_lay.addLayout(scan_hdr_lay2)
        scan_card_lay.addWidget(scan_sub)

        # divider
        sdiv = QtWidgets.QFrame(); sdiv.setFixedHeight(1)
        sdiv.setStyleSheet(f"background:{BORDER}; border:none;")
        scan_card_lay.addWidget(sdiv)

        self.lineEdit_3 = QtWidgets.QLineEdit()
        self.lineEdit_3.setObjectName("lineEdit_3")
        self.lineEdit_3.setFixedHeight(max(54, int(56*SF)))
        self.lineEdit_3.setPlaceholderText("⬡  Scan serial number…")
        self.lineEdit_3.setStyleSheet(f"""
            QLineEdit {{
                background:#060C16; border:1.5px solid {BORDER2}; border-radius:8px;
                color:{ACCENT}; font-size:18px; font-weight:700;
                font-family:Consolas,monospace; padding:0 12px; letter-spacing:1px;
            }}
            QLineEdit:focus {{ border-color:{ACCENT}; background:#080E1A; }}
        """)
        scan_card_lay.addWidget(self.lineEdit_3)

        self.pushButton_19 = QtWidgets.QPushButton("▶  RUN TEST")
        self.pushButton_19.setObjectName("pushButton_19")
        self.pushButton_19.setFixedHeight(int(40*SF))
        self.pushButton_19.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
        self.pushButton_19.setStyleSheet(f"""
            QPushButton:enabled {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 {ACCENT}, stop:1 #00C8A8);
                color:#030E0A; border:none;
                border-radius:8px; font-size:13px; font-weight:800; letter-spacing:2px;
            }}
            QPushButton:enabled:hover  {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 #33FFE0, stop:1 #00FFD1);
            }}
            QPushButton:enabled:pressed{{ background:{ACCENT}; }}
            QPushButton:disabled {{
                background:{S3}; color:{TEXT3}; border:1px solid {BORDER};
                border-radius:8px; font-size:13px; font-weight:800; letter-spacing:2px;
            }}
        """)
        mode_row = QtWidgets.QVBoxLayout()
        mode_row.setSpacing(8)
        self.production_mode_button = QtWidgets.QPushButton("PRODUCTION")
        self.calibration_mode_button = QtWidgets.QPushButton("CALIBRATION")
        for button in (self.production_mode_button, self.calibration_mode_button):
            button.setCheckable(True)
            button.setMinimumHeight(34)
            button.setStyleSheet("QPushButton {color:#cddded;background:#162030;border:1px solid #3A5070;border-radius:6px;padding:6px;} QPushButton:checked {color:white;background:#205c99;border:2px solid #4A9EFF;}")
            mode_row.addWidget(button)
        self.production_mode_button.clicked.connect(lambda: self._select_test_mode("production"))
        self.calibration_mode_button.clicked.connect(lambda: self._select_test_mode("calibration"))
        self._selected_test_mode = "production"
        self.production_mode_button.setChecked(True)
        self.calibration_schedule_label = QtWidgets.QLabel(f"Calibration: {CALIBRATION_START_TEXT}-{CALIBRATION_END_TEXT} IST daily")
        self.calibration_schedule_label.setWordWrap(True)
        self.calibration_schedule_label.setStyleSheet("color:#F5A623; font-size:11px;")
        scan_card_lay.insertLayout(0, mode_row)
        scan_card_lay.insertWidget(1, self.calibration_schedule_label)
        scan_card_lay.addWidget(self.pushButton_19)

        scan_wrap = QtWidgets.QWidget(); scan_wrap.setStyleSheet("background:transparent;")
        sw_lay = QtWidgets.QVBoxLayout(scan_wrap)
        sw_lay.setContentsMargins(10,6,10,6); sw_lay.setSpacing(0)
        sw_lay.addWidget(scan_card)
        lv.addWidget(scan_wrap)
        lv.addWidget(sec_divider())

        # comboBox stub (needed by code references but hidden)
        self.comboBox = QtWidgets.QComboBox()
        self.comboBox.setObjectName("comboBox")
        self.comboBox.addItem("C10")
        self.comboBox.hide()

        # MACHINE
        lv.addWidget(sec_label("MACHINE"))
        hm_c = QtWidgets.QWidget(); hm_c.setStyleSheet("background:transparent;")
        hm_lay = QtWidgets.QVBoxLayout(hm_c)
        hm_lay.setContentsMargins(10,0,10,6); hm_lay.setSpacing(0)
        self.pushButton_7 = QtWidgets.QPushButton("⌂   HOME AXIS")
        self.pushButton_7.setObjectName("pushButton_7")
        self.pushButton_7.setFixedHeight(int(36*SF))
        self.pushButton_7.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
        self.pushButton_7.setStyleSheet(f"""
            QPushButton {{
                background:{S3}; color:{ACCENT}; border:1px solid rgba(0,255,209,.3);
                border-radius:8px; font-size:12px; font-weight:700; letter-spacing:1px;
            }}
            QPushButton:hover {{ background:rgba(0,255,209,.1); border-color:{ACCENT}; }}
            QPushButton:pressed {{ background:rgba(0,255,209,.05); }}
        """)
        # Keep the object for existing internal references; remove manual Home from UI.
        self.pushButton_7.hide()

        # ── Haptic button image below HOME AXIS — large with shimmer ─
        import os as _os2
        _haptic_img_path = _os2.path.join(_os2.path.dirname(_os2.path.abspath(__file__)), "haptic_button_image.jpeg")
        if _os2.path.exists(_haptic_img_path):

            # ── Shimmer overlay widget drawn on top of the image ──────
            class _ShimmerImage(QtWidgets.QWidget):
                def __init__(self, img_path, parent=None):
                    super().__init__(parent)
                    self._px      = QtGui.QPixmap(img_path)
                    self._phase   = 0.0
                    self._glow    = 0.0
                    self._glow_up = True
                    self.setMinimumHeight(160)
                    self.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                                       QtWidgets.QSizePolicy.Fixed)
                    t = QTimer(self)
                    t.setInterval(30)
                    t.timeout.connect(self._tick)
                    t.start()

                def _tick(self):
                    self._phase = (self._phase + 0.018) % 1.0
                    step = 0.022
                    if self._glow_up:
                        self._glow = min(1.0, self._glow + step)
                        if self._glow >= 1.0: self._glow_up = False
                    else:
                        self._glow = max(0.0, self._glow - step)
                        if self._glow <= 0.0: self._glow_up = True
                    self.update()

                def paintEvent(self, event):
                    from PyQt5.QtGui import QPainter, QLinearGradient, QColor, QPainterPath
                    p = QPainter(self)
                    p.setRenderHint(QPainter.Antialiasing)
                    p.setRenderHint(QPainter.SmoothPixmapTransform)
                    W, H = self.width(), self.height()

                    # ── clip to rounded rect ──────────────────────────
                    path = QPainterPath()
                    path.addRoundedRect(0, 0, W, H, 10, 10)
                    p.setClipPath(path)

                    # ── draw scaled image filling full width ──────────
                    if not self._px.isNull():
                        scaled = self._px.scaled(W, H, Qt.KeepAspectRatioByExpanding,
                                                  Qt.SmoothTransformation)
                        ox = (scaled.width()  - W) // 2
                        oy = (scaled.height() - H) // 2
                        p.drawPixmap(0, 0, scaled, ox, oy, W, H)

                    # ── dark vignette overlay to blend with dark UI ───
                    vign = QLinearGradient(0, 0, 0, H)
                    vign.setColorAt(0.0, QColor(8, 14, 22, 180))
                    vign.setColorAt(0.3, QColor(8, 14, 22, 40))
                    vign.setColorAt(0.7, QColor(8, 14, 22, 40))
                    vign.setColorAt(1.0, QColor(8, 14, 22, 200))
                    p.setPen(Qt.NoPen)
                    p.setBrush(vign)
                    p.drawRect(0, 0, W, H)

                    # ── animated teal shimmer sweep ───────────────────
                    sweep_x = int(self._phase * (W + 260)) - 130
                    shimmer = QLinearGradient(sweep_x - 80, 0, sweep_x + 80, 0)
                    shimmer.setColorAt(0.0, QColor(0, 255, 209,  0))
                    shimmer.setColorAt(0.4, QColor(0, 255, 209, 28))
                    shimmer.setColorAt(0.5, QColor(0, 255, 209, 70))
                    shimmer.setColorAt(0.6, QColor(0, 255, 209, 28))
                    shimmer.setColorAt(1.0, QColor(0, 255, 209,  0))
                    p.setBrush(shimmer)
                    p.drawRect(0, 0, W, H)

                    # ── pulsing teal border glow ──────────────────────
                    border_a = int(80 + 160 * self._glow)
                    pen = QtGui.QPen(QColor(0, 255, 209, border_a), 2)
                    p.setPen(pen)
                    p.setBrush(Qt.NoBrush)
                    p.drawRoundedRect(1, 1, W - 2, H - 2, 10, 10)

                    # ── "C10" label at bottom ────────────────────────
                    label_a = int(200 + 55 * self._glow)
                    p.setPen(QColor(0, 255, 209, label_a))
                    f = QtGui.QFont("Consolas", 11, QtGui.QFont.Bold)
                    p.setFont(f)
                    p.drawText(10, H - 10, "C10")

                    p.end()

            haptic_widget = _ShimmerImage(_haptic_img_path)
            hm_lay.addSpacing(6)
            hm_lay.addWidget(haptic_widget)

        lv.addWidget(hm_c)
        lv.addWidget(sec_divider())
        lv.addStretch()

        # Utility buttons + clock at bottom
        def util_btn(label, obj=None):
            b = QtWidgets.QPushButton(label)
            if obj: b.setObjectName(obj)
            b.setFixedHeight(int(28*SF))
            b.setStyleSheet(f"""
                QPushButton {{
                    background:{S2}; color:{TEXT2}; border:1px solid {BORDER2};
                    border-radius:6px; font-size:10px; font-weight:600;
                }}
                QPushButton:hover {{ color:{TEXT}; border-color:#35465A; }}
            """)
            return b
        util_c = QtWidgets.QWidget(); util_c.setStyleSheet("background:transparent;")
        ul = QtWidgets.QHBoxLayout(util_c)
        ul.setContentsMargins(12,4,12,4); ul.setSpacing(5)
        self.pushButton_14 = util_btn("Clear Log", "pushButton_14")
        self.pushButton_8  = util_btn("Refresh",   "pushButton_8")
        ul.addWidget(self.pushButton_14); ul.addWidget(self.pushButton_8)
        lv.addWidget(util_c)

        self.pushButton_12 = QtWidgets.QPushButton("✕   Exit")
        self.pushButton_12.setObjectName("pushButton_12")
        self.pushButton_12.setFixedHeight(int(30*SF))
        self.pushButton_12.setStyleSheet(f"""
            QPushButton {{
                background:#160808; color:{RED}; border:1px solid #3A1010;
                border-radius:7px; font-size:11px; font-weight:700; margin:0 12px 4px 12px;
            }}
            QPushButton:hover {{ background:#220C0C; border-color:{RED}; }}
        """)
        lv.addWidget(self.pushButton_12)

        self.footer_clock = QtWidgets.QLabel("--:--:--")
        self.footer_clock.setAlignment(Qt.AlignCenter)
        self.footer_clock.setStyleSheet(f"color:{TEXT3}; font-size:9px; font-family:Consolas,monospace; padding:2px 0 5px 0;")
        lv.addWidget(self.footer_clock)

        col1_vbox.addWidget(col1_inner)
        body_hbox.addWidget(col1_w, stretch=17)

        # ─────────────────────────────────────────────────────────────────
        #  COLUMN 2 — Right / Centre / Left waveforms + mini graphs  (stretch=33)
        # ─────────────────────────────────────────────────────────────────
        col2_w = QtWidgets.QWidget()
        col2_w.setStyleSheet(f"background:{S2}; border-right:1px solid {BORDER};")
        col2_vbox = QtWidgets.QVBoxLayout(col2_w)
        col2_vbox.setContentsMargins(0,0,0,0); col2_vbox.setSpacing(0)

        # Column 2 header
        # Column 2 header removed (LIVE FORCE MONITOR not needed)
        # Keep hidden textEdit stub so plot() code doesn't crash
        self.textEdit = QtWidgets.QTextEdit()
        self.textEdit.setObjectName("textEdit")
        self.textEdit.hide()

        # 3 stacked button rows — HapticCard full width, forced equal height
        _wave_rows = []
        for key, color, label in [
            ("right",  "#4A9EFF", "Right Button"),
            ("centre", "#F5A623", "Centre Button"),
            ("left",   "#2ECC71", "Left Button"),
        ]:
            row_w = QtWidgets.QWidget()
            row_w.setStyleSheet(f"background:{BG}; border-bottom:1px solid {BORDER};")
            row_lay = QtWidgets.QHBoxLayout(row_w)
            row_lay.setContentsMargins(4,4,4,4); row_lay.setSpacing(0)

            hc = HapticCard(label, color, sf=SF * 0.85)
            hc.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
            row_lay.addWidget(hc)
            col2_vbox.addWidget(row_w, stretch=1)
            _wave_rows.append(row_w)

            if key == "right":
                self.haptic_right    = hc; self.btn_graph_right  = hc
            elif key == "centre":
                self.haptic_centre   = hc; self.btn_graph_centre = hc
            else:
                self.haptic_left     = hc; self.btn_graph_left   = hc

        # Force all 3 rows to always be equal height
        _hdr_h = int(28*SF)
        def _equalize():
            avail = col2_w.height() - _hdr_h
            per   = max(50, avail // 3)
            for rw in _wave_rows:
                rw.setMinimumHeight(per)
                rw.setMaximumHeight(per)
        QTimer.singleShot(300, _equalize)

        _orig_col2_re = col2_w.resizeEvent if hasattr(col2_w, 'resizeEvent') else None
        def _col2_re(e, _o=_orig_col2_re):
            _equalize()
            if _o: _o(e)
        col2_w.resizeEvent = _col2_re

        body_hbox.addWidget(col2_w, stretch=28)

        # ─────────────────────────────────────────────────────────────────
        #  COLUMN 3 — Live Graph (flex, fills remaining space)
        # ─────────────────────────────────────────────────────────────────
        col3_w = QtWidgets.QWidget()
        col3_w.setObjectName("widget_3")
        self.widget_3 = col3_w
        col3_vbox = QtWidgets.QVBoxLayout(col3_w)
        col3_vbox.setContentsMargins(0,0,0,0); col3_vbox.setSpacing(0)

        # Graph panel
        self.widget_4 = QtWidgets.QWidget()
        self.widget_4.setObjectName("widget_4")
        self.widget_4.setStyleSheet(f"background:{S1};")
        gv = QtWidgets.QVBoxLayout(self.widget_4)
        gv.setContentsMargins(0,0,0,0); gv.setSpacing(0)

        # Graph header
        gh = QtWidgets.QWidget(); gh.setFixedHeight(int(34*SF))
        gh.setStyleSheet(f"background:{S1}; border-bottom:1px solid {BORDER};")
        ghl = QtWidgets.QHBoxLayout(gh)
        ghl.setContentsMargins(14,0,14,0); ghl.setSpacing(10)
        gt = QtWidgets.QLabel("TEST RESULT")
        self.result_panel_heading = gt
        gt.setStyleSheet(f"color:#9BBCCC; font-size:8px; font-weight:700; letter-spacing:2px; font-family:Consolas,monospace;")
        ghl.addWidget(gt); ghl.addStretch()
        self.label_6 = QtWidgets.QLabel(""); self.label_6.setObjectName("label_6")
        ghl.addWidget(self.label_6)
        gv.addWidget(gh)

        # Canvas
        canvas_wrap = QtWidgets.QWidget()
        canvas_wrap.setStyleSheet(f"background:{BG}; margin:6px; border-radius:8px; border:1px solid {BORDER};")
        cw_lay = QtWidgets.QVBoxLayout(canvas_wrap)
        cw_lay.setContentsMargins(0,0,0,0)
        self.live_graph = TestResultPanel()
        self.live_graph.setObjectName("live_graph")
        cw_lay.addWidget(self.live_graph)

        # graph_stack compat — always index 0
        self.graph_stack = QtWidgets.QStackedWidget()
        self.graph_stack.setStyleSheet("background:transparent;")
        self.graph_stack.addWidget(canvas_wrap)
        self.result_label = QtWidgets.QLabel(); self.result_label.setObjectName("result_label")
        self.graph_stack.addWidget(self.result_label)
        self.graph_stack.setCurrentIndex(0)
        gv.addWidget(self.graph_stack, stretch=1)

        # ScanLine overlay
        self.canvas = self.live_graph
        self.scan_overlay = ScanLineOverlay(self.canvas)
        self.scan_overlay.hide()
        _orig_resize = self.canvas.resizeEvent if hasattr(self.canvas, 'resizeEvent') else None
        def _canvas_resize(e, _orig=_orig_resize):
            self.scan_overlay.resize(self.canvas.size())
            if _orig: _orig(e)
        self.canvas.resizeEvent = _canvas_resize

        col3_vbox.addWidget(self.widget_4, stretch=1)
        body_hbox.addWidget(col3_w, stretch=55)

        root_vbox.addWidget(body_row, stretch=1)

        # ── hidden compat refs ────────────────────────────────────────
        self.widget   = topbar
        self.widget_2 = body_row
        self.graph_widget = self.widget_4

        # stubs for unused original buttons
        self.pushButton_9  = QtWidgets.QPushButton(); self.pushButton_9.setObjectName("pushButton_9")
        self.pushButton_10 = QtWidgets.QPushButton(); self.pushButton_10.setObjectName("pushButton_10")
        self.pushButton_11 = QtWidgets.QPushButton(); self.pushButton_11.setObjectName("pushButton_11")
        self.pushButton_13 = QtWidgets.QPushButton(); self.pushButton_13.setObjectName("pushButton_13")
        self.pushButton_13.setCheckable(True)
        self.label   = QtWidgets.QLabel(); self.label.setObjectName("label")
        self.label_3 = QtWidgets.QLabel(); self.label_3.setObjectName("label_3")
        self.label_4 = QtWidgets.QLabel(); self.label_4.setObjectName("label_4")

        MainWindow.setCentralWidget(self.centralwidget)

        # ── Connect thread-safe push signals to haptic cards ──────────
        self._sig_push_right.connect(self.haptic_right.push)
        self._sig_push_centre.connect(self.haptic_centre.push)
        self._sig_push_left.connect(self.haptic_left.push)

        # ── Signal connections ─────────────────────────────────────────
        self.pushButton_14.clicked.connect(self.clear_graph)
        self.pushButton_19.clicked.connect(self.send_gcode_btn1)
        self.pushButton_19.setEnabled(True)
        self.pushButton_7.clicked.connect(self.home_gcode)
        self.pause_button.clicked.connect(self.pause_jobcard)
        self.resume_button.clicked.connect(self.resume_jobcard)
        self.complete_button.clicked.connect(self.submit_jobcard)

        self.retranslateUi(MainWindow)
        QtCore.QMetaObject.connectSlotsByName(MainWindow)

        # ── BODY ROW (left + right) ───────────────────────────────────
        body_row = QtWidgets.QWidget()
        body_row.setStyleSheet(f"background:{BG};")
        body_hbox = QtWidgets.QHBoxLayout(body_row)
        body_hbox.setContentsMargins(0,0,0,0)
        body_hbox.setSpacing(0)

        # ═══════════════════════════════════════════════════════════════
        #  LEFT PANEL
        # ═══════════════════════════════════════════════════════════════
        self.full_menu_widget = QtWidgets.QWidget()
        self.full_menu_widget.setObjectName("full_menu_widget")
        self.full_menu_widget.setFixedWidth(int(400*SF))
        self.full_menu_widget.setStyleSheet(f"background:{S1}; border-right:1px solid {BORDER};")

        left_scroll = QtWidgets.QScrollArea()
        left_scroll.setWidget(self.full_menu_widget)
        left_scroll.setWidgetResizable(True)
        left_scroll.setFixedWidth(int(400*SF))
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left_scroll.setStyleSheet(f"QScrollArea{{background:{S1};border:none;}} QScrollBar:vertical{{background:{S1};width:4px;}} QScrollBar::handle:vertical{{background:{BORDER2};}}")

        lv = QtWidgets.QVBoxLayout(self.full_menu_widget)
        lv.setContentsMargins(0,0,0,0); lv.setSpacing(0)

        def sec_divider():
            d = QtWidgets.QFrame()
            d.setFixedHeight(1)
            d.setStyleSheet(f"background:{BORDER}; margin:0 14px;")
            return d

        def sec_label(txt):
            l = QtWidgets.QLabel(txt)
            l.setStyleSheet(f"color:{TEXT3}; font-size:9px; font-weight:700; letter-spacing:2.5px; font-family:Consolas,monospace; padding:14px 16px 8px 16px;")
            return l

        def info_row(key, val, val_color=TEXT):
            w = QtWidgets.QWidget()
            w.setStyleSheet("background:transparent;")
            wl = QtWidgets.QVBoxLayout(w)
            wl.setContentsMargins(14,0,14,10); wl.setSpacing(3)
            k = QtWidgets.QLabel(key)
            k.setStyleSheet(f"color:{TEXT3}; font-size:9px; font-weight:700; letter-spacing:1.5px; font-family:Consolas,monospace;")
            v = QtWidgets.QLabel(val)
            v.setStyleSheet(f"color:{val_color}; font-size:12px; font-weight:600; font-family:Consolas,monospace; background:{BG}; border:1px solid {BORDER2}; border-radius:7px; padding:6px 10px;")
            wl.addWidget(k); wl.addWidget(v)
            return w, v

    # ── Animation helpers ─────────────────────────────────────────────
    def _anim_start_run(self, stage="INITIALISING…"):
        self.graph_stack.setCurrentIndex(0)
        self.live_graph.show_state("TESTING", stage)
        self._set_status_running(f"▶   {stage}")
        self.pushButton_19.setEnabled(False)
        self.pushButton_7.setEnabled(False)

    def _is_overlay_alive(self):
        """Return True only when the C++ ScanLineOverlay object still exists."""
        try:
            _ = self.scan_overlay.isVisible()
            return True
        except RuntimeError:
            return False

    def _rebuild_overlay(self):
        """Re-create the overlay — live_graph persists, so just re-parent."""
        try:
            self.scan_overlay = ScanLineOverlay(self.live_graph)
            self.scan_overlay.hide()
            self.scan_overlay.resize(self.live_graph.size())
        except (RuntimeError, AttributeError):
            pass

    def _anim_set_stage(self, stage):
        self.live_graph.show_state("TESTING", stage)
        self._set_status_running(f"▶   {stage}")

    def _anim_finish_pass(self):
        try:
            if self._is_overlay_alive():
                self.scan_overlay.stop()
        except RuntimeError:
            pass

        # Update the status banner
        self.label_2.setStyleSheet("""
            QLabel {
                background: #0A1C12; color: #7DCFA0;
                font-size:12px; font-weight:800; letter-spacing:3px;
                font-family:Consolas,monospace; border-bottom:1px solid #2A6040;
            }
        """)
        self.label_2.setText("✓   PASS — TEST COMPLETE")

        self._show_result_popup(passed=True)
        # Production plot supplies DIP details after the result is finalized.

        self.pushButton_19.setEnabled(True)
        self.pushButton_7.setEnabled(True)

    def _set_admin_view(self, enabled):
        self.technician_view_label.setStyleSheet("color:%s;font-size:11px;font-weight:700;" % ("#71899E" if enabled else "#FFFFFF"))
        self.admin_view_label.setStyleSheet("color:%s;font-size:11px;font-weight:700;" % ("#78BBFF" if enabled else "#71899E"))
        self.live_graph.set_admin(enabled)
        for card in (self.haptic_right, self.haptic_centre, self.haptic_left):
            for box in card._admin_stat_boxes:
                box.setVisible(enabled)
        self.result_panel_heading.setText("DISPLACEMENT – FORCE LIVE GRAPH" if enabled else "TEST RESULT")

    def _show_result_popup(self, passed: bool, dip_text: str = ""):
        self.graph_stack.setCurrentIndex(0)
        self.live_graph.show_state("PASS" if passed else "FAIL", dip_text or self.label_2.text(),
                                   "#00FF88" if passed else "#FF4F4F")

    @QtCore.pyqtSlot()
    def _anim_finish_fail(self):
        try:
            if self._is_overlay_alive():
                self.scan_overlay.stop()
        except RuntimeError:
            pass
        self.label_2.setStyleSheet("""
            QLabel {
                background: #1A0606; color: #FF4F4F;
                font-size:12px; font-weight:700; letter-spacing:3px;
                font-family:Consolas,monospace; border-bottom:1px solid #3A1010;
            }
        """)
        self.label_2.setText("✗   The serial number is invalid previous stage not done")
        self._flash_border("#FF4F4F")

        # ── Centred FAIL popup ────────────────────────────────────────
        self._show_result_popup(passed=False)

        self.pushButton_19.setEnabled(True)
        self.pushButton_7.setEnabled(True)

    def _anim_reset(self):
        try:
            if self._is_overlay_alive():
                self.scan_overlay.stop()
        except RuntimeError:
            pass
        self.label_2.setStyleSheet("""
            QLabel {
                background: #141D2B; color: #3A4F65;
                font-size:12px; font-weight:700; letter-spacing:3px;
                font-family:Consolas,monospace; border-bottom:1px solid #1F2E42;
            }
        """)
        self.label_2.setText("●   NOT ACTIVATED")
        self.pushButton_19.setEnabled(True)
        self.pushButton_7.setEnabled(True)

    # ── Slots called via invokeMethod from RunThread ───────────────
    # These run on the main thread — safe to touch any Qt widget.
    @QtCore.pyqtSlot()
    def _anim_set_stage_right(self):
        self._anim_set_stage("BTN 1 — PRESSING")

    @QtCore.pyqtSlot()
    def _anim_set_stage_centre(self):
        self._anim_set_stage("BTN 2 — POSITIONING")

    @QtCore.pyqtSlot()
    def _anim_set_stage_centre2(self):
        self._anim_set_stage("BTN 2 — PRESSING")

    @QtCore.pyqtSlot()
    def _anim_set_stage_left(self):
        self._anim_set_stage("BTN 3 — POSITIONING")

    @QtCore.pyqtSlot()
    def _anim_set_stage_left2(self):
        self._anim_set_stage("BTN 3 — PRESSING")

    @QtCore.pyqtSlot()
    def _anim_set_stage_home(self):
        self._anim_set_stage("RETURNING HOME…")

    # ── HapticCard start/end — must run on main thread ────────────
    @QtCore.pyqtSlot()
    def _haptic_right_start(self):
        self.haptic_right.start_run()

    @QtCore.pyqtSlot()
    def _haptic_right_end(self):
        self.haptic_right.end_run()

    @QtCore.pyqtSlot()
    def _haptic_centre_start(self):
        self.haptic_centre.start_run()

    @QtCore.pyqtSlot()
    def _haptic_centre_end(self):
        self.haptic_centre.end_run()

    @QtCore.pyqtSlot()
    def _haptic_left_start(self):
        self.haptic_left.start_run()

    @QtCore.pyqtSlot()
    def _haptic_left_end(self):
        self.haptic_left.end_run()

    # ── Progress bar slots ────────────────────────────────────────
    @QtCore.pyqtSlot()
    def _progress_btn1(self):
        self.progress_bar.set_step(1)

    @QtCore.pyqtSlot()
    def _progress_btn2(self):
        self.progress_bar.set_step(2)

    @QtCore.pyqtSlot()
    def _progress_btn3(self):
        self.progress_bar.set_step(3)

    @QtCore.pyqtSlot()
    def _progress_done_pass(self):
        self.progress_bar.set_step(4, "pass")

    @QtCore.pyqtSlot()
    def _progress_done_fail(self):
        self.progress_bar.set_step(4, "fail")

    def _set_status_running(self, text):
        self.label_2.setStyleSheet("""
            QLabel {
                background: #0A1E14; color: #00FFD1;
                font-size:12px; font-weight:700; letter-spacing:3px;
                font-family:Consolas,monospace; border-bottom:1px solid #1F2E42;
            }
        """)
        self.label_2.setText(text)

    def _flash_border(self, hex_color):
        """Flash the graph canvas border safely — vivid 3px flash."""
        try:
            wrap = self.canvas.parent()
            if not wrap:
                return
            wrap.setStyleSheet(
                f"background:#080C12; margin:12px; border-radius:10px; border:3px solid {hex_color};"
            )

            def reset_border():
                try:
                    if wrap and wrap.parent():
                        wrap.setStyleSheet(
                            "background:#080C12; margin:12px; border-radius:10px; border:1px solid #1F2E42;"
                        )
                except RuntimeError:
                    pass

            QTimer.singleShot(500, reset_border)

        except Exception:
            pass

    def retranslateUi(self, MainWindow):
        MainWindow.setWindowTitle("bluArmor — Button Displacement Jig")
        self.label_5.setText("Button Displacement Test Infra")
        self.pushButton_19.setText("▶   RUN TEST")
        self.pushButton_12.setText("✕   Exit")
        self.pause_button.setText("⏸ Pause")
        self.resume_button.setText("▶ Resume")
        self.complete_button.setText("✓ Done")
        self.label_2.setText("●   NOT ACTIVATED")
        self.label_6.setText("")

    # ─────────────────────────────────────────────────────────────────
    #  STYLED POPUP — uses QDialog so CSS works on all platforms/themes
    #  kind: "info" | "warn" | "error"
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def show_popup(parent, kind, title, message):
        from PyQt5.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QDialogButtonBox
        from PyQt5.QtCore import Qt

        COLORS = {
            "info":  ("#00FFD1", "#0D2E22"),
            "warn":  ("#F5A623", "#2E1E00"),
            "error": ("#F85149", "#2E0A0A"),
        }
        accent, btn_bg = COLORS.get(kind, COLORS["info"])

        dlg = QDialog(parent)
        dlg.setWindowTitle(title)
        dlg.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        dlg.setModal(True)
        dlg.setMinimumWidth(380)
        dlg.setStyleSheet(f"""
            QDialog {{
                background-color: #161B22;
                border: 1.5px solid #30363D;
                border-radius: 12px;
            }}
            QLabel {{
                background: transparent;
                border: none;
                color: #E6EDF3;
            }}
            QPushButton {{
                background-color: {btn_bg};
                color: {accent};
                border: 1.5px solid {accent};
                border-radius: 8px;
                padding: 9px 38px;
                font-size: 13px;
                font-weight: 700;
                font-family: 'Segoe UI', sans-serif;
                min-width: 100px;
                min-height: 40px;
            }}
            QPushButton:hover {{
                background-color: {accent};
                color: #0D1117;
            }}
            QPushButton:pressed {{
                background-color: transparent;
                color: {accent};
            }}
        """)

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(14)

        # ── colour bar at top ──────────────────────────────────────────
        bar = QtWidgets.QFrame()
        bar.setFixedHeight(4)
        bar.setStyleSheet(f"background:{accent}; border-radius:2px; border:none;")
        layout.addWidget(bar)

        # ── icon + title row ───────────────────────────────────────────
        icon_map = {"info": "ℹ", "warn": "⚠", "error": "✕"}
        title_row = QHBoxLayout()
        title_row.setSpacing(10)

        icon_lbl = QtWidgets.QLabel(icon_map.get(kind, "ℹ"))
        icon_lbl.setStyleSheet(f"color:{accent}; font-size:20px; font-weight:700;")
        title_lbl = QtWidgets.QLabel(f"<b>{title}</b>")
        title_lbl.setStyleSheet("color:#E6EDF3; font-size:16px; font-weight:700;")
        title_row.addWidget(icon_lbl)
        title_row.addWidget(title_lbl)
        title_row.addStretch()
        layout.addLayout(title_row)

        # ── message ────────────────────────────────────────────────────
        msg_lbl = QtWidgets.QLabel(message)
        msg_lbl.setWordWrap(True)
        msg_lbl.setStyleSheet("color:#8B949E; font-size:13px; line-height:1.5;")
        layout.addWidget(msg_lbl)

        layout.addSpacing(6)

        # ── OK button ──────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QtWidgets.QPushButton("OK")
        ok_btn.setCursor(QtGui.QCursor(Qt.PointingHandCursor))
        ok_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

        dlg.exec_()

    def clear_all_datas(self):
        self.data1.clear()
        self.data2.clear()
        self.data3.clear()
        self.data4.clear()
        self.data5.clear()

    def start_serial(self):
        self.clear_all_datas()

        previous = self.serial_thread
        previous.stop(keep_grbl_open=True)
        grbl_port = previous.ser
        pending = previous.pending_grbl_commands
        previous.ser = None  # Ownership moves to the replacement reader.

        self.serial_thread = SerialThread(ARDUINO_PORT, GRBL_PORT, 115200)
        self.serial_thread.ser = grbl_port
        self.serial_thread.pending_grbl_commands = pending
        self.serial_thread._homing_requested = previous._homing_requested
        self.serial_thread.data_received.connect(self.handle_data)
        self.serial_thread.error_occurred.connect(self._show_serial_error)
        try:
            self.serial_thread.connect_ports()
        except Exception as exc:
            logger.exception("Serial connection failed")
            self._show_serial_error(str(exc))
            return False
        self.serial_thread.start()

        # ── Reset all UI for a clean second/third/nth run ─────────
        try:
            self.live_graph.reset()
            self.graph_stack.setCurrentIndex(0)
            self.haptic_right.reset()
            self.haptic_centre.reset()
            self.haptic_left.reset()
            self.progress_bar.set_step(0)
        except AttributeError:
            pass

    @QtCore.pyqtSlot(str)
    def _show_motor_error(self, message):
        self._haptic_right_end()
        self._haptic_centre_end()
        self._haptic_left_end()
        self._anim_finish_fail()
        self._progress_done_fail()
        QMessageBox.warning(self, "Motor test stopped", message)

    def _show_serial_error(self, message):
        QMessageBox.warning(self, "Serial connection", message)

    def send_gcode(self, gcode, callback=None):
        # entered_gcode = self.gcode_edit.text()
        # if entered_gcode:
        # 
        self.serial_thread.send_gcode(gcode, callback)
    def home_gcode(self):
        if getattr(self, "_homing_in_progress", False):
            return
        if getattr(self, "_run_thread", None) is not None and self._run_thread.isRunning():
            return
        self._homing_in_progress = True
        self._homed = False
        self.pushButton_19.setEnabled(False)
        self.pushButton_7.setEnabled(False)
        self.label_2.setText("HOMING AUTOMATICALLY - please wait")
        self._home_error = ""

        def home_worker():
            try:
                self.serial_thread.connect_ports()
                port = self.serial_thread.ser
                # Only this worker reads GRBL replies during startup homing.
                self.send_gcode("$H", self.handle_data)
                deadline = time.monotonic() + 90.0
                buffer = bytearray()
                while time.monotonic() < deadline:
                    buffer.extend(port.readline())
                    while b"\n" in buffer:
                        line, _, remainder = buffer.partition(b"\n")
                        buffer[:] = remainder
                        response = line.decode("utf-8", errors="replace").strip()
                        if response == "ok":
                            self.serial_thread.pending_grbl_commands = max(0, self.serial_thread.pending_grbl_commands - 1)
                        elif response.startswith(("error", "ALARM", "Grbl")):
                            raise RuntimeError(f"Homing failed: {response}")
                        elif response.startswith("<Idle") and self.serial_thread.pending_grbl_commands == 0:
                            self.serial_thread._homing_requested = False
                            return
                        elif response.startswith(("<Alarm", "<Door", "<Hold")):
                            raise RuntimeError(f"Homing stopped: {response}")
                    port.write(b"?")
                    time.sleep(0.05)
                raise RuntimeError("Homing timed out; check limit switches and controller")
            except Exception as exc:
                self._home_error = str(exc)
                logger.exception("Automatic homing failed")

        self._home_thread = RunThread(home_worker)
        self._home_thread.finished.connect(self._home_finished)
        self._home_thread.start()

    @QtCore.pyqtSlot()
    def _home_finished(self):
        self._homing_in_progress = False
        self._homed = not bool(self._home_error)
        self.pushButton_7.setEnabled(True)
        self.pushButton_19.setEnabled(self._homed)
        self.serial_thread._homing_requested = False
        if self._homed:
            self.label_2.setText("HOME COMPLETE - ready")
            self._check_calibration_reset()
        else:
            self.label_2.setText("HOMING FAILED - test blocked")
            QMessageBox.warning(self, "Homing failed", self._home_error + "\nResolve the issue and restart the application to home automatically.")


    # def check_serial_exists(self,serial_number):
    #     return True
        
       
    #     """
    #     Check if serial number exists in ERP under the given system.
    #     Returns True if exists, False if not.
    #     """
    #     SYSTEM_NAME =["c10Functionality"]
    #     filters = [
    #         ["device_serialnumber", "=", serial_number.strip()],
    #         ["system", "in", SYSTEM_NAME]
    #     ]

    #     try:
    #         response = requests.get(
    #             ERP_NOVA,
    #             headers=HEADERS,
    #             params={"filters": json.dumps(filters)}
    #         )

    #         if response.status_code != 200:
    #             print(f"❌ ERP Error: Status code {response.status_code}")
    #             return False  # Treat error as not found (or handle as needed)

    #         data = response.json()
    #         results = data.get("data", [])

    #         if results:
    #             print(f"❌ Serial number '{serial_number}' already exists in '{SYSTEM_NAME}'")
    #             return True
    #         else:
    #             print(f"✅ Serial number '{serial_number}' is new in '{SYSTEM_NAME}'")
    #             return False

    #     except Exception as e:
    #         logger.exception("Exception connecting to ERP: %s", e)
    #         return False


    def _query_serial_records(self, serial_number, endpoint, filters, fields=None):
        params = {"filters": json.dumps(filters)}
        if fields is not None:
            params["fields"] = json.dumps(fields)
        try:
            response = requests.get(endpoint, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
            if response.status_code != 200:
                logger.error("ERP serial lookup failed: HTTP %s", response.status_code)
                self._last_serial_lookup_error = f"HTTP {response.status_code}"
                return False
            self._last_serial_lookup_error = None
            return bool(response.json().get("data", []))
        except Exception as exc:
            self._last_serial_lookup_error = str(exc)
            logger.exception("ERP serial lookup failed")
            return False  # Existing caller behavior retained; error recorded separately.

    def check_serial_existss(self, serial_number):
        return self._query_serial_records(serial_number, ERP_DASHBOARD_URL, [
            ["device_serialnumber", "=", serial_number.strip()],
            ["status", "in", ["fail", "completely fail"]],
        ])

    def check_serial_exists(self,serial_number):
        
       
        """
        Check if serial number exists in ERP under the given system.
        Returns True if exists, False if not.
        """
        SYSTEM_NAME =["C10PostChargingCurrent","HS3PostChargingCurrent"]
        filters = [
            ["device_serialnumber", "=", serial_number.strip()],
            ["system", "in", SYSTEM_NAME],
            ["status", "=", "pass"]
        ]

        try:
            response = requests.get(
                ERP_PASS,
                headers=HEADERS,
                params={"filters": json.dumps(filters)}
            )

            if response.status_code != 200:
                print(f"❌ ERP Error: Status code {response.status_code}")
                return False  # Treat error as not found (or handle as needed)

            data = response.json()
            results = data.get("data", [])

            if results:
                print(f"❌ Serial number '{serial_number}' already exists in '{SYSTEM_NAME}'")
                return True
            else:
                print(f"✅ Serial number '{serial_number}' is new in '{SYSTEM_NAME}'")
                return False

        except Exception as e:
            print(f"⚠️ Exception connecting to ERP: {e}")
            return False


    def validate_serial_1(self, serial_number):
        return True
        server_url=WS_SERVER_URL
        try:
            ws = websocket.create_connection(server_url, timeout=REQUEST_TIMEOUT)
            print("🌐 Connected to WebSocket server")
            message = {
                "type": "functionality",
                "serial": serial_number
            }
            ws.send(json.dumps(message))
            response_func = ws.recv()
            data_func = json.loads(response_func)
            try:
                ws.send(json.dumps(message))
                response = ws.recv()
                data_func = json.loads(response)

                status = data_func.get("status", 0)
                print(data_func)

                status=data_func.get("status")
                if status == "exists":
                    print("✅ Functionality check passed.")
                    return True
                elif status=="not_found":
                    print("❌ Functionality not completed for this serial.")
                    ws.close()
                    return False
                else:
                    print("❌ Unexpected status:", status)
                    return False

            except Exception as e:
                print(f"❗ Error validating serial'{serial_number}':", e)
                return False

        except Exception as e:
            print(f"❗ Error validating serial '{serial_number}':", e)
            return False
        
    def check_serial(self,serial_number): 
        server_url = WS_SERVER_URL

        try:
            ws = websocket.create_connection(server_url, timeout=REQUEST_TIMEOUT)
            print("Connected to WebSocket server (Fetch Data)")

            message = {
                "type": "sleepcurrent",
                "serial": serial_number
            }
            ws.send(json.dumps(message))

            response = ws.recv()
            print(response)
            ws.close()
            print("Connection closed\n")

        
            data = json.loads(response)
            print(data)

            
            if data.get("status") == "success" :
                first_entry = data["data"][0]  
                batch = first_entry.get("batch")
                if batch:
                    return batch

                else:
                    print("BATCH not found in response.")
            else:
                print("Unexpected or error response:", data)

        except Exception as e:
            print(f"Error fetching data for serial '{serial_number}':", e)
    
    
          
    def _press_until_dip_plus_2n(self, callback):
        """Continuous F80 press, cancellable at first local force dip + 2 N.

        Requires GRBL 1.1 jogging. No fixed pressing Z endpoint.
        Arduino tuples use force in the first field, as in plot().
        """
        port = self.serial_thread.ser
        old_timeout = port.timeout
        port.timeout = 0.01
        active = False
        motion_lock = threading.Lock()
        samples = []
        state = {"dip": None, "last_sample": time.monotonic(), "cancelled": False, "failure": None,
                 "peak": None, "cancel_force": None, "cancel_rate": None,
                 "stop_reason": None, "last_force_at_stop": None}

        # ── Predictive stop ──────────────────────────────────────────
        # The plunger cannot stop instantly: between the cancel being
        # decided and the axis standing still (serial delay + GRBL
        # deceleration) the force keeps rising. So the cancel is sent
        # when the force is *predicted* to reach dip + 2 N by the time
        # the axis has stopped:  force + rise_rate * stop_lead >= target.
        # stop_lead is measured after every press and refined
        # automatically, so it does not need hand tuning.
        RATE_WINDOW_S = 0.20          # history used to measure N/s
        stop_lead = getattr(self, "_stop_lead_s", 0.10)
        history = []                  # (monotonic time, force)

        def rise_rate():
            """Least-squares force slope (N/s) over the recent window; 0 if unknown."""
            if len(history) < 4 or history[-1][0] - history[0][0] < 0.04:
                return 0.0
            t0 = history[0][0]
            ts = [t - t0 for t, _ in history]
            fs = [f for _, f in history]
            mt = sum(ts) / len(ts); mf = sum(fs) / len(fs)
            den = sum((t - mt) ** 2 for t in ts)
            if den <= 0:
                return 0.0
            return max(0.0, sum((t - mt) * (f - mf) for t, f in zip(ts, fs)) / den)

        graph_key = {"handle_data_btn1": "right", "handle_data_btn2": "centre",
                     "handle_data_btn3": "left"}.get(getattr(callback, "__name__", ""))

        def cancel_jog(reason):
            # Caller holds motion_lock.
            state["stop_reason"] = reason
            state["last_force_at_stop"] = samples[-1] if samples else None
            state["cancelled"] = True
            port.write(b"\x85")
            if graph_key:
                self.live_graph.begin_peak_hold(graph_key)

        def collect(data):
            callback(data)
            point = self._parse_point(data)
            if not active or point is None or not math.isfinite(point[0]):
                return
            force = point[0]
            now_s = time.monotonic()
            state["last_sample"] = now_s
            if state["cancelled"]:
                # Track where the force actually ends up after the stop.
                if state["peak"] is None or force > state["peak"]:
                    state["peak"] = force
            else:
                history.append((now_s, force))
                while history and now_s - history[0][0] > RATE_WINDOW_S:
                    del history[0]
            samples.append(force)
            if len(samples) > 3:
                del samples[0]
            if state["dip"] is None and len(samples) == 3:
                a, b, c = samples
                if 3 <= b <= 30 and b <= a and b < c and max(a-b, c-b) > 0.01:
                    state["dip"] = b
                    print(f"Dip: {b:.2f} N; maximum: {b + 2.0:.2f} N")
            if state["dip"] is not None and state["dip"] > 20.0:
                state["failure"] = "Force was out of range 20"
            target = None if state["dip"] is None else state["dip"] + 2.0
            # Retain the motion safety stop; only the detected DIP decides
            # the 20 N quality failure, not the subsequent peak.
            if force >= 20.0 and state["dip"] is None:
                state["failure"] = "No dip found before 20 N safety stop"
            rate = rise_rate() if target is not None else 0.0
            predicted = force + rate * stop_lead
            if state["failure"] or force >= 20.0 or (target is not None and predicted >= target):
                with motion_lock:
                    if not state["cancelled"]:
                        state["cancel_force"] = force
                        state["cancel_rate"] = rate
                        state["peak"] = force
                        if state["failure"]:
                            reason = state["failure"]
                        elif force >= 20.0:
                            reason = "20 N force cutoff"
                        else:
                            reason = "Predicted dip + 2 N target"
                        cancel_jog(reason)

        response_buffer = bytearray()

        def read_response():
            # GRBL replies can also be split by a serial timeout.
            if b"\n" not in response_buffer:
                response_buffer.extend(port.readline())
            if b"\n" not in response_buffer:
                return ""
            line, _, remainder = response_buffer.partition(b"\n")
            response_buffer[:] = remainder
            response = line.decode("utf-8", errors="ignore").strip()
            if response.startswith(("error", "ALARM", "Grbl")):
                print(f"GRBL RX: {response}", flush=True)
            if response.startswith("Grbl"):
                self.serial_thread.pending_grbl_commands = 0
                self.serial_thread._homing_requested = False
                raise RuntimeError("GRBL restarted during the test. Click HOME before retrying; check USB connection/power if it repeats")
            if response == "ok" or response.startswith("error"):
                self.serial_thread.pending_grbl_commands = max(
                    0, self.serial_thread.pending_grbl_commands - 1)
                if self.serial_thread.pending_grbl_commands == 0:
                    self.serial_thread._homing_requested = False
            return response

        def wait_idle(timeout=10.0):
            # G4 P0 is a planner synchronization barrier. Consume every
            # queued command acknowledgement before requesting fresh status.
            self.send_gcode("G4 P0", callback)
            deadline = time.monotonic() + timeout
            while self.serial_thread.pending_grbl_commands:
                response = read_response()
                if response.startswith(("error", "ALARM")):
                    raise RuntimeError(f"GRBL positioning: {response}")
                if time.monotonic() >= deadline:
                    raise RuntimeError("GRBL positioning acknowledgement timed out")
            port.reset_input_buffer()  # Only stale status remains after all acks.
            response_buffer.clear()
            while time.monotonic() < deadline:
                port.write(b"?")
                while time.monotonic() < deadline:
                    response = read_response()
                    if response.startswith("<"):
                        break
                    if response.startswith(("error", "ALARM")):
                        raise RuntimeError(f"GRBL: {response}")
                if response.startswith("<Idle"):
                    print(f"GRBL ready: {response}", flush=True)
                    return
                if response.startswith(("<Hold", "<Alarm", "<Door")):
                    raise RuntimeError(f"GRBL cannot press in this state: {response}")
                time.sleep(0.02)
            raise RuntimeError("GRBL did not reach Idle")

        try:
            # Finish original positioning before starting the cancellable press.
            wait_idle()
            active = True
            state["last_sample"] = time.monotonic()
            # Short relative jogs are streamed ahead without waiting for each
            # move to finish. The force callback cancels the entire jog queue.
            # 0.5 mm is a streaming increment, not a button travel endpoint.
            press_started = time.monotonic()
            next_segment = press_started
            deadline = press_started + 8.0
            last_query = 0.0
            segments_sent = 0
            stop_query_started = False
            while True:
                now = time.monotonic()
                if not state["cancelled"]:
                    if now - state["last_sample"] > 0.5:
                        state["failure"] = "Force readings stopped during press"
                    elif now >= deadline:
                        state["failure"] = "No force stop reached within press timeout"
                    if state["failure"]:
                        with motion_lock:
                            if not state["cancelled"]:
                                cancel_jog(state["failure"])
                    elif now >= next_segment:
                        with motion_lock:
                            if not state["cancelled"]:
                                self.send_gcode("$J=G91 G21 Z0.5 F80", collect)
                                segments_sent += 1
                        # Keep a short queue ahead of motion for continuous F80.
                        # Absolute scheduling avoids accumulating serial-loop
                        # delays, which previously allowed the queue to run dry.
                        # Each segment is an increment, never a final press Z.
                        next_segment = press_started + max(0.0, (segments_sent - 1) * 0.375)
                # After cancellation, discard previously requested status
                # reports only after every jog acknowledgement is consumed.
                # Ask for fresh stop status immediately, without the normal
                # 50 ms polling delay before retraction.
                if state["cancelled"] and not stop_query_started and not self.serial_thread.pending_grbl_commands:
                    port.reset_input_buffer()
                    response_buffer.clear()
                    port.write(b"?")
                    last_query = time.monotonic()
                    stop_query_started = True
                response = read_response()
                if response.startswith(("error", "ALARM")):
                    raise RuntimeError(f"GRBL press rejected: {response}")
                if stop_query_started and response.startswith("<Idle") and not self.serial_thread.pending_grbl_commands:
                    key = callback.__name__.replace("handle_data_btn", "")
                    self._force_press_results[key] = dict(state)
                    dip = state["dip"]
                    dip_text = "none" if dip is None else f"{dip:.2f} N"
                    target_text = "none" if dip is None else f"{dip + 2.0:.2f} N"
                    force = state["last_force_at_stop"]
                    force_text = "none" if force is None else f"{force:.2f} N"
                    elapsed = time.monotonic() - press_started
                    button = graph_key.title() if graph_key else f"Button {key}"
                    print(f"[PRESS {button}] Retracting: {state['stop_reason']} | "
                          f"dip={dip_text}, target={target_text}, last force={force_text}, "
                          f"elapsed={elapsed:.2f} s", flush=True)
                    # Learn the real stop lead from this press.
                    if (state["dip"] is not None and not state["failure"]
                            and state["cancel_rate"] and state["cancel_rate"] > 2.0
                            and state["peak"] is not None):
                        measured = (state["peak"] - state["cancel_force"]) / state["cancel_rate"]
                        measured = min(0.25, max(0.0, measured))
                        self._stop_lead_s = 0.6 * stop_lead + 0.4 * measured
                        print(f"Stop: target {state['dip'] + 2.0:.2f} N, cancel sent at "
                              f"{state['cancel_force']:.2f} N ({state['cancel_rate']:.1f} N/s), "
                              f"peak {state['peak']:.2f} N; stop lead measured "
                              f"{measured*1000:.0f} ms, next {self._stop_lead_s*1000:.0f} ms",
                              flush=True)
                    return
                if state["cancelled"] and now > deadline + 3.0:
                    raise RuntimeError("GRBL did not stop after jog cancellation")
                query_interval = 0.01 if stop_query_started else 0.05
                if time.monotonic() - last_query >= query_interval:
                    port.write(b"?")
                    last_query = time.monotonic()
                time.sleep(0.001 if state["cancelled"] else 0.005)

        except Exception:
            port.write(b"\x85")
            port.write(b"!")
            raise
        finally:
            active = False
            self.serial_thread.callback = callback
            port.timeout = old_timeout








    def send_gcode_btn1(self):
        """
        Button press sequence runs in a RunThread so the UI never freezes.
        RUN TEST button re-enables only after the thread finishes.
        """
        if getattr(self, "_homing_in_progress", False) or not getattr(self, "_homed", False):
            QMessageBox.information(self, "Homing required", "Wait for automatic homing to complete. If it failed, resolve the issue and restart the application.")
            return
        if getattr(self, "_run_thread", None) is not None and self._run_thread.isRunning():
            return
        try:
            if self._calibration_failed_today():
                self._refresh_mode_buttons()
                QMessageBox.warning(self, "Service required", "Today's calibration failed. Production and calibration retry are blocked.")
                return
            now = ist_now()
            overdue = (now.hour * 60 + now.minute) >= CALIBRATION_END_MINUTES and not self._calibration_valid()
            if overdue:
                self._selected_test_mode = "calibration"
            if self._calibration_valid():
                self._selected_test_mode = "production"
            required = self._selected_test_mode == "calibration"
            self._refresh_mode_buttons()
            if required and now.hour * 60 + now.minute < CALIBRATION_START_MINUTES:
                QMessageBox.information(self, "Calibration window", f"Calibration opens at {CALIBRATION_START_TEXT} IST. Production is available until {CALIBRATION_END_TEXT} IST.")
                return
            if not required:
                if not is_jig_active(self.erp_system):
                    QMessageBox.warning(self, "Production blocked", "ERP could not confirm an Active jig for this QC system. Check jig status, credentials and connection.")
                    return
                # Recheck after the ERP request in case it crossed the configured deadline.
                if (ist_now().hour * 60 + ist_now().minute) >= CALIBRATION_END_MINUTES and not self._calibration_valid():
                    self._selected_test_mode = "calibration"
                    self._refresh_mode_buttons()
                    QMessageBox.warning(self, "Calibration required", f"{CALIBRATION_END_TEXT} IST deadline reached. Run golden-device calibration first.")
                    return
        except Exception as exc:
            QMessageBox.warning(self, "Calibration database error", f"Production blocked: {exc}")
            return
        self._calibration_mode = required
        if required:
            answer = QMessageBox.question(self, "Daily golden-device calibration",
                f"Place the GOLDEN DEVICE in the jig.\n{calibration_limits_text()}\n"
                "Start calibration using the existing G-code?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                self._calibration_mode = False
                return
            self._calibration_started_period = calibration_period()
            try:
                with sqlite3.connect(CALIBRATION_DB) as db:
                    cur = db.execute("""INSERT INTO jig_calibration
                        (period, started_at, employee_id, model, system, status, limits_json, serialnumber)
                        VALUES (?, ?, ?, ?, ?, 'RUNNING', ?, ?)""",
                        (self._calibration_started_period, ist_now().isoformat(),
                         str(self.employee_id), self.selected_model, self.erp_system,
                         json.dumps(CALIBRATION_LIMITS, sort_keys=True), GOLDEN_DEVICE_SERIAL))
                    self._calibration_attempt = cur.lastrowid
            except Exception as exc:
                self._calibration_mode = False
                QMessageBox.warning(self, "Calibration database error", f"Production blocked: {exc}")
                return
        self._anim_start_run("CALIBRATION" if required else "BTN 1 — POSITIONING")
        self.clear_graph()
        if self.start_serial() is False:
            self._anim_reset()
            if self._calibration_mode:
                self._finish_calibration("Serial connection failed")
            return
        self._force_press_results = {}

        self.serial_number = GOLDEN_DEVICE_SERIAL if self._calibration_mode else self.lineEdit_3.text()
        if not self.serial_number:
            print("ENTER SERIAL NUMBER")
            self._anim_reset()
            return

        def _run():
            press_worker = None
            press_errors = []
            clearance_move = "G0 Z2.410"

            def check_press_finished():
                if press_worker is not None:
                    press_worker.join(timeout=25.0)
                    if press_worker.is_alive():
                        raise RuntimeError("Press worker did not finish; sequence aborted")
                if press_errors:
                    raise press_errors[0]

            def launch_press(position, callback, return_home=False):
                nonlocal press_worker
                check_press_finished()

                def press():
                    try:
                        self.send_gcode(position, callback)
                        self._press_until_dip_plus_2n(callback)
                        self.send_gcode(clearance_move, callback)
                        if self._calibration_mode:
                            result = self._force_press_results.get(callback.__name__.replace("handle_data_btn", ""), {})
                            
                            if result.get("failure"):
                                raise RuntimeError(result["failure"])
                        if return_home:
                            self.send_gcode("G0 X10.762 Y56.259", callback)
                            self.send_gcode("G0 Z6.410", callback)
                    except Exception as exc:
                        press_errors.append(exc)
                        print(f"MOTOR ERROR: {exc}", flush=True)
                        self._sig_motor_error.emit(str(exc))

                press_worker = threading.Thread(target=press, daemon=True)
                press_worker.start()

            # Use plain time.sleep — we are already off the main thread
            time.sleep(3)
            # if selected_model ="C10":
            if not self.check_serial_exists(self.serial_number):
                QtCore.QMetaObject.invokeMethod(self, "_anim_finish_fail", Qt.QueuedConnection)
                QtCore.QMetaObject.invokeMethod(self, "_progress_done_fail", Qt.QueuedConnection)
                return

            if self.check_serial_existss(self.serial_number):
                QtCore.QMetaObject.invokeMethod(self, "_anim_finish_fail", Qt.QueuedConnection)
                QtCore.QMetaObject.invokeMethod(self, "_progress_done_fail", Qt.QueuedConnection)
                return


           
            # ── BTN 1 ────────────────────────────────────────────
            QtCore.QMetaObject.invokeMethod(self, "_anim_set_stage_right",   Qt.QueuedConnection)
            QtCore.QMetaObject.invokeMethod(self, "_haptic_right_start",     Qt.QueuedConnection)
            QtCore.QMetaObject.invokeMethod(self, "_progress_btn1",          Qt.QueuedConnection)
            self.send_gcode("G21 G90", self.handle_data_btn1)
            self.send_gcode(clearance_move, self.handle_data_btn1)
            launch_press("G0 X140.358 Y40.161", self.handle_data_btn1)
            time.sleep(14)
            check_press_finished()
            QtCore.QMetaObject.invokeMethod(self, "_haptic_right_end", Qt.QueuedConnection)

            # ── BTN 2 ────────────────────────────────────────────
            QtCore.QMetaObject.invokeMethod(self, "_anim_set_stage_centre",  Qt.QueuedConnection)
            time.sleep(4)
            QtCore.QMetaObject.invokeMethod(self, "_anim_set_stage_centre2", Qt.QueuedConnection)
            QtCore.QMetaObject.invokeMethod(self, "_haptic_centre_start",    Qt.QueuedConnection)
            QtCore.QMetaObject.invokeMethod(self, "_progress_btn2",          Qt.QueuedConnection)
            launch_press("G0 X131.846 Y56.243", self.handle_data_btn2)
            time.sleep(10)
            check_press_finished()
            QtCore.QMetaObject.invokeMethod(self, "_haptic_centre_end", Qt.QueuedConnection)

            # ── BTN 3 ────────────────────────────────────────────
            QtCore.QMetaObject.invokeMethod(self, "_anim_set_stage_left",    Qt.QueuedConnection)
            time.sleep(1.5)
            QtCore.QMetaObject.invokeMethod(self, "_anim_set_stage_left2",   Qt.QueuedConnection)
            QtCore.QMetaObject.invokeMethod(self, "_haptic_left_start",      Qt.QueuedConnection)
            QtCore.QMetaObject.invokeMethod(self, "_progress_btn3",          Qt.QueuedConnection)
            launch_press("G0 X140.358 Y72.357", self.handle_data_btn3, return_home=True)
            QtCore.QMetaObject.invokeMethod(self, "_anim_set_stage_home",    Qt.QueuedConnection)
            time.sleep(12)
            check_press_finished()
            QtCore.QMetaObject.invokeMethod(self, "_haptic_left_end", Qt.QueuedConnection)

            self.serial_thread.stop(keep_grbl_open=True)
            self._run_thread.plot_ready.emit()

        def _safe_run():
            try:
                _run()
            except Exception as exc:
                if self._calibration_mode:
                    QtCore.QMetaObject.invokeMethod(self, "_calibration_aborted", Qt.QueuedConnection)
                print(f"Dip-controlled press aborted: {exc}")
                self.serial_thread.stop(keep_grbl_open=True)
                QtCore.QMetaObject.invokeMethod(self, "_anim_finish_fail", Qt.QueuedConnection)
                QtCore.QMetaObject.invokeMethod(self, "_progress_done_fail", Qt.QueuedConnection)

        self._run_thread = RunThread(_safe_run)
        self._run_thread.plot_ready.connect(self.plot)
        self._run_thread.finished.connect(self._check_calibration_reset)
        self._run_thread.start()


    def send_gcode_btn2(self):
        pass  # merged into send_gcode_btn1 _run()

    def send_gcode_btn3(self):
        pass  # merged into send_gcode_btn1 _run()

    def send_gcode_btn4(self):
        self.send_gcode("$H", self.handle_data_btn4)
    
    def send_tstick_btn1(self):
        self.send_gcode("G0 X170.1 y68",self.handle_data_btn4)
            # self.label_2.setText("FUNCTIONALITY DONE")    
        self.send_gcode("G0 z25",self.handle_data_btn4)
        self.send_gcode("G1 Z28 f80",self.handle_data_btn4)#15.1
        self.send_gcode("G0 Z5",self.handle_data_btn4)
    def send_tstick_btn2(self):
        time.sleep(2)
        # self.send_gcode("$H",self.handle_data_btn2)
        self.send_gcode("G0 X56 z20", self.handle_data_btn5)
        self.send_gcode("G1 Z23 f80", self.handle_data_btn5)
        self.send_gcode("G0 z20", self.handle_data_btn5)#15
        self.send_gcode("G0 x0 y0 z0", self.handle_data_btn5)


    def handle_data(self, data):
        # print(f"Received data: {data}")
        pass

    def _parse_point(self, data):
        """Parse '(x, y)' string from Arduino → (float, float) or None."""
        try:
            cleaned = data.replace('(', '').replace(')', '').strip()
            parts = cleaned.split(',')
            return float(parts[0]), float(parts[1])
        except Exception:
            return None

    def handle_data_btn1(self, data):
        self.data1.append(data)
        pt = self._parse_point(data)
        if pt:
            x, y = pt
            self.live_graph.append_point("right", x, y)
            self._sig_push_right.emit(y, x)

    def handle_data_btn2(self, data):
        self.data2.append(data)
        pt = self._parse_point(data)
        if pt:
            x, y = pt
            self.live_graph.append_point("centre", x, y)
            self._sig_push_centre.emit(y, x)

    def handle_data_btn3(self, data):
        self.data3.append(data)
        pt = self._parse_point(data)
        if pt:
            x, y = pt
            self.live_graph.append_point("left", x, y)
            self._sig_push_left.emit(y, x)
    def handle_data_btn4(self, data):
        self.data4.append(data)

        print(f"Received data for Btn4: {data}")   
        
    def handle_data_btn5(self, data):
        self.data5.append(data)

        print(f"Received data for Btn5: {data}")
    def parse_data(self, data):
        print(data)
        x_values = []
        y_values = []

        for point in data:
                    try:
                    # Convert the string representation of tuple to an actual tuple
                        point_tuple = eval(point.replace('(', '').replace(')', ''))

                    # if point_tuple[0] < 5 and point_tuple[0] > 0.1:
                        y_values.append(point_tuple[1])
                        x_values.append(point_tuple[0])
                        if point_tuple[0] <= 10 and point_tuple[0] >= 25:
                        # self.pause_gcode()
                    #     # Break the loop if x value is greater than or equal to 6
                                break
                    except Exception as e:
                        logger.exception("error processing data: %s", e)
        return x_values, y_values

    def find_dips(self, x_values, threshold=0.01, force_one=False):
        dips = []
        cleaned = [x for x in x_values if x > 0]
        if not cleaned:
            return dips

        max_idx = cleaned.index(max(cleaned))
        for i in range(1, max_idx):
            if cleaned[i] <= cleaned[i-1] and cleaned[i] <= cleaned[i+1]:
                left_diff = abs(cleaned[i-1] - cleaned[i])
                right_diff = abs(cleaned[i+1] - cleaned[i])
                if left_diff > threshold or right_diff > threshold:
                    dips.append(round(cleaned[i], 2))

        # Remove near-duplicates
        final_dips = []
        for d in dips:
            if all(abs(existing - d) > threshold for existing in final_dips):
                final_dips.append(d)

        if not final_dips and force_one:
            final_dips = [round(min(cleaned[:max_idx+1]), 2)]
        return final_dips
        

    def filter_dips(self, dips, curve_name):
        if curve_name.lower() in ["right", "centre", "left"]:
            return [d for d in dips if 3 <= d <= 25.5]
        else:
            return dips


        
    def pause_gcode(self):
        self.send_gcode("M226",self.handle_data)
        print("Pausing G-code execution at x = 5")
    def clear_graph(self):
        try:
            self.live_graph.reset()
            self.graph_stack.setCurrentIndex(0)
            self.haptic_right.reset()
            self.haptic_centre.reset()
            self.haptic_left.reset()
            self.progress_bar.set_step(0)
        except AttributeError:
            pass



    def plot(self):
        if self._calibration_mode:
            self._finish_calibration()
            return
        x1, y1 = self.parse_data(self.data1)
        x2, y2 = self.parse_data(self.data2)
        x3, y3 = self.parse_data(self.data3)

        # ── EMA ──────────────────────────────────────────────────
        z1 = normal_plot_displacement(y1)
        z2 = normal_plot_displacement(y2)
        z3 = normal_plot_displacement(y3)

        total_data = [(z1, x1), (z2, x2), (z3, x3)]

        # ── Dip detection ────────────────────────────────────────
        # Use the first dip latched by the press controller for every output.
        # Preserve the existing list format, with at most one value per button.
        press_results = getattr(self, "_force_press_results", {})
        dips = {}
        for key, button_id, values in (
                ("right", "1", x1), ("centre", "2", x2), ("left", "3", x3)):
            if button_id in press_results:
                dip = press_results[button_id].get("dip")
                dips[key] = [] if dip is None else [round(dip, 2)]
            else:
                dips[key] = self.filter_dips(self.find_dips(values), key)[:1]
        print("Dips found:", dips)

        self.textEdit.clear()
        self.textEdit.append("DIPs: " +
            "  ".join(f"{k.upper()[0]}:{v[0]:.2f} N" if v else f"{k.upper()[0]}:—"
                      for k, v in dips.items()))

        # ── Save SVG silently (no plt.show, no page flip) ────────
        imageName = f'plot_{self.serial_number}_{datetime.now().strftime("%d%m%Y_%H%M%S")}.svg'
        try:
            fig_s, ax_s = plt.subplots()
            ax_s.plot(z1, x1, label=f"Right {dips['right']}",   color='#4A9EFF')
            ax_s.plot(z2, x2, label=f"Center {dips['centre']}", color='#F5A623')
            ax_s.plot(z3, x3, label=f"Left {dips['left']}",     color='#2ECC71')
            ax_s.set_xlabel('Displacement (mm)'); ax_s.set_ylabel('Force (N)')
            ax_s.legend(); fig_s.savefig(imageName); plt.close(fig_s)
        except Exception as e:
            print(f"SVG save error: {e}"); imageName = ""

        # ── Live graph stays on index 0 — NO page flip ───────────
        self.graph_stack.setCurrentIndex(0)

        # ── Dip markers on combined live graph + per-button graphs
        card_map = {"right": self.haptic_right, "centre": self.haptic_centre, "left": self.haptic_left}
        smooth_map = {"right": z1, "centre": z2, "left": z3}
        for key, raw_x, raw_y in [("right", x1, y1), ("centre", x2, y2), ("left", x3, y3)]:
            zk = smooth_map[key]
            for dip_x in dips[key]:
                if raw_x:
                    idx = min(range(len(raw_x)), key=lambda i, rx=raw_x, dx=dip_x: abs(rx[i]-dx))
                    dip_force_raw = raw_y[idx] if idx < len(raw_y) else 0.0
                    # Use smoothed force at same index so marker lands ON the curve
                    dip_force_smooth = zk[idx] if idx < len(zk) else dip_force_raw
                    self.live_graph.mark_dip(key, dip_x, dip_force_smooth)
                    card_map[key].flash_dip(force_n=dip_force_raw, disp_mm=dip_x)

        # ── Pass / Fail — inline status banner, NO popup ─────────
        if not dips["right"] or not dips["centre"] or not dips["left"]:
            auto_result = "0"
            fail_reason = "One or more DIP not detected"
            right_dip = centre_dip = left_dip = 0
        else:
            right_dip  = dips["right"][0]
            centre_dip = dips["centre"][0]
            left_dip   = dips["left"][0]
            if not (8.5 <= right_dip <= 20.5):
                auto_result = "0"; fail_reason = f"Right DIP out of range ({right_dip} N)"
            elif not (8 <= centre_dip <= 13):
                auto_result = "0"; fail_reason = f"Centre DIP out of range ({centre_dip} N)"
            elif not (8.5 <= left_dip <= 20.5):
                auto_result = "0"; fail_reason = f"Left DIP out of range ({left_dip} N)"
            else:
                auto_result = "1"; fail_reason = ""

        force_failures = []
        for key, result in getattr(self, "_force_press_results", {}).items():
            if result.get("failure"):
                force_failures.append(f"Button {key}: {result['failure']}")
        for key in ("right", "centre", "left"):
            if dips[key] and dips[key][0] > 20.0:
                force_failures.append(f"{key.title()} Button: Force was out of range 20")
        if force_failures:
            auto_result = "0"
            fail_reason = "; ".join(force_failures)

        print("AUTO RESULT:", auto_result, fail_reason)

        # ── Highlight each card's DIP box green/red individually ──────
        if getattr(self, "_force_press_results", {}).get("1", {}).get("failure") or not dips["right"] or not (8.5 <= dips["right"][0] <= 20.5):
            self.haptic_right.highlight_dip(passed=False)
        else:
            self.haptic_right.highlight_dip(passed=True)

        if getattr(self, "_force_press_results", {}).get("2", {}).get("failure") or not dips["centre"] or not (8 <= dips["centre"][0] <= 13):
            self.haptic_centre.highlight_dip(passed=False)
        else:
            self.haptic_centre.highlight_dip(passed=True)

        if getattr(self, "_force_press_results", {}).get("3", {}).get("failure") or not dips["left"] or not (8.5 <= dips["left"][0] <= 20.5):
            self.haptic_left.highlight_dip(passed=False)
        else:
            self.haptic_left.highlight_dip(passed=True)

        if auto_result == "1":
            self._anim_finish_pass()
            self._progress_done_pass()
            result_text = "✓   PASS — TEST COMPLETE"
            self.label_2.setText(result_text)
            # show dip values in popup
            QTimer.singleShot(50, lambda: self._show_result_popup(
                passed=True,
                dip_text=f"R: {right_dip:.2f} N    C: {centre_dip:.2f} N    L: {left_dip:.2f} N"
            ))
        else:
            self._anim_finish_fail()
            self._progress_done_fail()
            self.label_2.setText(f"✗   FAIL — {fail_reason}")
            self._show_result_popup(passed=False, dip_text=fail_reason)

        self.handle_button_press(imageName, total_data, auto_result, dips)
        return total_data

    def handle_button_press(self, imageName, total_data, result, dips):
        print("11")
        # _anim_finish already called from plot()
        self.save_data(self.serial_number, total_data, result, imageName, dips)
        if not self.employee_id or not self.job_card_id:
            print("Error: Employee ID or Job Card ID is missing.")
            return
        if result == "1":
            new_count = increment_pass_count(self.employee_id, self.job_card_id)
            self.pass_count = new_count
            insert_pass_record(self.serial_number, self.batch, self.employee_id, self.job_card_id, dips, self.erp_system)
        elif result == "0":
            # Keep the existing ERP fail flow exactly as it is.
            insert_fail_record(self.serial_number, self.batch, self.employee_id, self.job_card_id, dips, self.erp_system)

            # Printer-only parameters: serial number, QC system, test datetime,
            # failed button and its failed DIP point.
            printer_issues = []
            printer_failed_points = []
            button_names = {
                "right": "Right Button Failed",
                "centre": "Centre Button Failed",
                "left": "Left Button Failed",
            }

            for button_key in ("right", "centre", "left"):
                button_values = dips.get(button_key) or []
                failed_point = button_values[0] if button_values else None

                if failed_point is None:
                    printer_issues.append(
                        button_names[button_key].replace("Failed", "Not Detected")
                    )
                    printer_failed_points.append("No DIP")
                elif not (8.5 <= failed_point <= 20.5):
                    printer_issues.append(button_names[button_key])
                    printer_failed_points.append(failed_point)

            if not printer_issues:
                printer_issues = ["Button Test Failed"]
                printer_failed_points = ["Unknown point"]

            if print_fail_label_from_params is not None:
                print_fail_label_from_params(
                    device_no=self.serial_number,
                    qc_system="CASSINI-C10",
                    test_datetime=datetime.now(),
                    issue=printer_issues,
                    failed_point=printer_failed_points,
                )
            else:
                print("[printer] Fail label skipped because printer module import failed")


    def save_data(self, serial_no: str, graph: list,result, imageName: str,dips=None):
        print("RESULT:", result)
        batch=self.check_serial(self.serial_number)
        self.batch=batch
        print(batch)
        try:
            
            graph_payload = {
                 "graph":graph,
                 "dips":dips,
                 "serial_no": serial_no,
            }

            # ✅ Convert → compress → base64 encode
            

            encoded_graph_old = base64.b64encode(
                gzip.compress(json.dumps(graph_payload).encode("utf-8"))
            ).decode("utf-8")


            # ✅ Save graph data into database
            self.save_graph(serial_no,encoded_graph_old,imageName, result,dips)

            # ~ # ✅ Try sending data to server
            # self.send_data_to_server()

        except Exception as e:
            logger.exception("Error in save_data(): %s", e)



    def save_graph(self, serial_no: str, graph, imagename: str, result: int, dips=None):
        """Save graph and dips into SQLite safely"""
        conn = None
        try:

            # --- Ensure dips is stored as JSON string ---
            dips_str = json.dumps(dips) if isinstance(dips, (dict, list)) else str(dips)

            # --- Convert and compress graph ---
            if isinstance(graph, (dict, list)):
                graph_json = json.dumps(graph)
            else:
                graph_json = str(graph)

            # ~ compressed_graph = base64.b64encode(
                # ~ gzip.compress(graph_json.encode("utf-8"))
            # ~ ).decode("utf-8")

            # --- Insert into database ---
            conn = sqlite3.connect("c10_btn_jig.db")
            cur = conn.cursor()
            cur.execute('''
                CREATE TABLE IF NOT EXISTS ampl_btndisp (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sr_number CHAR(17) NOT NULL,
                    filename CHAR(50) NOT NULL,
                    graph TEXT,
                    data_send TEXT,
                    result INTEGER,
                    dips TEXT,
                    date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            ''')

            cur.execute(
                '''INSERT OR IGNORE INTO ampl_btndisp
                (sr_number, filename, graph, data_send, result, dips)
                VALUES (?, ?, ?, ?, ?, ?)''',
                (serial_no, imagename, graph, "no", result, dips_str)
            )

            conn.commit()
            print(f"✅ Saved graph for {serial_no}")
            return True

        except Exception as e:
            logger.exception("Error in save_graph")
            return False
        finally:
            if conn is not None:
                conn.close()


    def send_data_to_server(self):
        server_url = SQL_UPLOAD_URL

        conn = None
        cur = None
        try:
            conn = sqlite3.connect('c10_btn_jig.db')
            cur = conn.cursor()

            cur.execute("""
                SELECT sr_number, filename, graph, result,dips,date
                FROM ampl_btndisp WHERE data_send='no'
            """)
            rows = cur.fetchall()

            data_list = []
            for row in rows:
                data_list.append({
                    'sr_number': row[0],
                    'filename': row[1],
                    'graph': row[2],  # already base64 string
                    'result': row[3],
                    "dips": row[4],
                    'date': row[5],
                })

            if not data_list:
                print("⚠ No unsent data found.")
                return

            response = requests.post(server_url, json=data_list, timeout=10)
            print("Response from server:", response.text)

            if 'SQL data uploaded successfully' in response.text:
                cur.execute("UPDATE ampl_btndisp SET data_send='yes' WHERE data_send='no'")
                conn.commit()
                print("✅ Marked uploaded data as sent.")

        except Exception as e:
            logger.exception("Error uploading SQL data: %s", e)
        finally:
            if cur is not None:
                cur.close()
            if conn is not None:
                conn.close()

    def pause_jobcard(self, checked=False):
            try:
                job_card = self.job_card_id
                print(job_card)
                qty = get_pass_count(self.employee_id, job_card) 

                if qty <= 0:
                    self.show_popup(self, "warn", "Invalid", "Quantity must be greater than 0.")
                    return

                url = f"{ERP_JOB_CARD_URL}/api/resource/Job Card/{job_card}"
                HEADERS = {
                    "Authorization": f"token {API_KEY}:{API_SECRET}",
                    "Content-Type": "application/json",
                }
                response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
                if response.status_code != 200:
                    self.show_popup(self, "warn", "Error", "Job Card not found.")
                    return

                job_data = response.json().get("data", {})
                time_logs = job_data.get("time_logs", [])

                if not time_logs:
                    self.show_popup(self, "warn", "Error", "No time logs to pause.")
                    return

                time_logs[-1]["to_time"] = datetime.now().isoformat()
                time_logs[-1]["completed_qty"] = qty

                payload = {"time_logs": time_logs}
                response = requests.put(url, headers=HEADERS, json=payload, timeout=REQUEST_TIMEOUT)

                if response.status_code == 200:
                    self.show_popup(self, "info", "Paused", "✅ Job paused and quantity saved.")
                    clear_pass_count(self.employee_id, job_card)  # ✅ Clear JSON
                    self.jobcard_count = 0  
                else:
                    self.show_popup(self, "error", "Error", f"❌ Error: {response.text}")
            except Exception as e:
                self.show_popup(self, "error", "Exception", str(e))


    def resume_jobcard(self, checked=False):
        try:
            job_card = self.job_card_id
            employee_id = self.employee_id

            url = f"{ERP_JOB_CARD_URL}/api/resource/Job Card/{job_card}"
            response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if response.status_code != 200:
                self.show_popup(self, "warn", "Error", "Job Card not found.")
                return

            job_data = response.json().get("data", {})
            time_logs = job_data.get("time_logs", [])

            last_to_time = time_logs[-1].get("to_time") if time_logs else None
            if not last_to_time:
                self.show_popup(self, "warn", "Error", "No paused time log found.")
                return

            new_log = {
                "from_time": last_to_time,
                "employee": employee_id,
                "completed_qty": 0
            }
            time_logs.append(new_log)

            payload = {"time_logs": time_logs}
            response = requests.put(url, headers=HEADERS, json=payload, timeout=REQUEST_TIMEOUT)

            if response.status_code == 200:
                self.show_popup(self, "info", "Resumed", "▶️ Resumed successfully.")
                clear_pass_count(employee_id, job_card)  # ✅ Clear JSON
                self.jobcard_count = 0  

            else:
                self.show_popup(self, "error", "Error", f"❌ {response.text}")
        except Exception as e:
            self.show_popup(self, "error", "Exception", str(e))



    def submit_jobcard(self):
        try:
            job_card = self.job_card_id
            completed_qty = get_pass_count(self.employee_id,job_card)# The qty to set in the last time log

            if not job_card or completed_qty <= 0:
                self.show_popup(self, "warn", "Invalid", "❌ Invalid Job Card or quantity.")
                return

            # Step 1: Fetch job card
            url = f"{ERP_JOB_CARD_URL}/api/resource/Job Card/{job_card}"
            response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if response.status_code != 200:
                self.show_popup(self, "error", "Error", "❌ Job Card not found.")
                return

            job_data = response.json().get("data", {})
            time_logs = job_data.get("time_logs", [])
            if not time_logs:
                self.show_popup(self, "warn", "Error", "❌ No active time log found.")
                return

            # Step 2: Update last time log's completed_qty to current count (replace)
            time_logs[-1]["completed_qty"] = completed_qty
            time_logs[-1]["to_time"] =datetime.now().isoformat()

            # Step 3: Calculate total completed quantity (sum all time logs completed_qty)
            total_qty = sum(float(log.get("completed_qty", 0)) for log in time_logs)

            # Step 4: Update for_quantity to total_qty
            update_payload = {
                "for_quantity": total_qty,
                "time_logs": time_logs
            }

            # Step 5: Update job card in ERPNext
            update_response = requests.put(url, headers=HEADERS, json=update_payload, timeout=REQUEST_TIMEOUT)
            if update_response.status_code != 200:
                self.show_popup(self, "error", "Error", "❌ Failed to update time log.")
                return

            # Step 6: Verify updated job card
            verify_response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if verify_response.status_code != 200:
                self.show_popup(self, "error", "Error", "❌ Failed to verify updated Job Card.")
                return

            updated_data = verify_response.json().get("data", {})
            for_qty = float(updated_data.get("for_quantity", 0))
            total_completed_qty = float(updated_data.get("total_completed_qty", 0))

            # Check for consistency
            if for_qty != total_completed_qty:
                self.show_popup(self, "warn", "Mismatch", f"❌ Cannot submit. for_quantity ({for_qty}) ≠ total_completed_qty ({total_completed_qty})")
                return

            # Step 7: Submit job card
            submit_url = f"{ERP_JOB_CARD_URL}/api/resource/Job Card/{job_card}?run_method=submit"
            submit_response = requests.post(submit_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)

            if submit_response.status_code == 200:
                self.show_popup(self, "info", "Submitted", "✅ Job Card submitted successfully!")
                clear_pass_count(self.employee_id, job_card)  # ✅ Clear file count
                self.jobcard_count = 0 
    # Reset count after submit if needed
            else:
                self.show_popup(self, "error", "Submit Failed", f"❌ {submit_response.text}")

        except Exception as e:
            self.show_popup(self, "error", "Exception", str(e))

if __name__ == '__main__':
    
    init_db()
    app = QApplication(sys.argv)
    window = EmployeeEntry()
    window.show()
    sys.exit(app.exec_()) 
