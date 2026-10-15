"""Golden-device calibration configuration, ERP upload, reports and UI mixin.

Keep this file beside the main application. Change timings, golden serial and
limits here. The main application owns the shared UI, serial ports and motion.
"""
from datetime import datetime, timedelta, timezone
import os
import json
import math
import logging
import sqlite3
import requests
from PyQt5 import QtCore
from PyQt5.QtWidgets import QMessageBox
from matplotlib.figure import Figure

logger = logging.getLogger(__name__)

GOLDEN_DEVICE_SERIAL = "GD-C10-001"
CALIBRATION_START = (19, 30)   # 6:03 PM IST
CALIBRATION_END = (19, 33)     # 6:05 PM IST
# =========================================================
if not (0 <= CALIBRATION_START[0] <= 23 and 0 <= CALIBRATION_START[1] <= 59
        and 0 <= CALIBRATION_END[0] <= 23 and 0 <= CALIBRATION_END[1] <= 59):
    raise ValueError("Calibration times must be valid 24-hour (hour, minute) pairs")
CALIBRATION_START_MINUTES = CALIBRATION_START[0] * 60 + CALIBRATION_START[1]
CALIBRATION_END_MINUTES = CALIBRATION_END[0] * 60 + CALIBRATION_END[1]
if CALIBRATION_START_MINUTES >= CALIBRATION_END_MINUTES:
    raise ValueError("Calibration end must be after start on the same IST day")
CALIBRATION_START_TEXT = f"{CALIBRATION_START[0]:02d}:{CALIBRATION_START[1]:02d}"
CALIBRATION_END_TEXT = f"{CALIBRATION_END[0]:02d}:{CALIBRATION_END[1]:02d}"


JIG_PASS = os.getenv("C10_JIG_PASS_URL", "https://aptener.frappe.cloud/api/resource/Jig Details")


JIG_API_KEY = "9f100ff3d2ac064"
JIG_API_SECRET = "41bd84caf3161e2"


ERP_JIG_URL = "https://aptener.frappe.cloud"
IST = timezone(timedelta(hours=5, minutes=30))

def jig_headers():
    return {"Authorization": f"token {JIG_API_KEY.strip()}:{JIG_API_SECRET.strip()}",
            "Content-Type": "application/json", "Accept": "application/json"}

def is_jig_active(qc_system: str) -> bool:
    return True
    """Fail closed if ERP cannot confirm an Active jig for this QC system."""
    try:
        response = requests.get(
            f"{ERP_JIG_URL}/api/resource/Jig Details", headers=jig_headers(),
            params={"filters": json.dumps([["qc_system", "=", qc_system.strip()],
                                            ["status", "=", "Active"]]),
                    "limit_page_length": 1}, timeout=10)
        if response.status_code != 200:
            logger.error("Jig Active check failed: HTTP %s", response.status_code)
            return False
        return bool(response.json().get("data", []))
    except Exception:
        logger.exception("Cannot confirm jig Active status")
        return False

def ist_now():
    return datetime.now(IST)

def send_jig_pass(employee, qc_system, dips, status, serialnumber=GOLDEN_DEVICE_SERIAL):
    """Send the same JSON stored in local dips_json to Jig Details.values.

    An existing JSON string is passed through without double encoding.
    Status labels may be configured to match the ERP Select field options.
    """
    try:
        if (not JIG_API_KEY.strip() or not JIG_API_SECRET.strip()
                or JIG_API_KEY == "YOUR_API_KEY" or JIG_API_SECRET == "YOUR_API_SECRET"):
            logger.error("Set JIG_API_KEY and JIG_API_SECRET in this script before uploading calibration.")
            return False
        jig_headers = {
            "Authorization": f"token {JIG_API_KEY.strip()}:{JIG_API_SECRET.strip()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        dips_json = dips if isinstance(dips, str) else json.dumps(dips, allow_nan=False)
        json.loads(dips_json)  # Reject invalid input before uploading.
        payload = {"data": {
            "calibrate_by": employee,
            "serial_number": serialnumber,
            "calibrate_date": ist_now().strftime("%Y-%m-%d %H:%M:%S"),
            "values": dips_json,
            "qc_system": "CASSINI-03",
            "calibration_status": status,
        }}
        response = requests.post(JIG_PASS, headers=jig_headers, json=payload, timeout=10)
        if response.status_code not in (200, 201):
            logger.error("Jig calibration ERP upload failed: HTTP %s: %s",
                         response.status_code, response.text[:1000])
            return False
        return True
    except Exception:
        logger.exception("Jig calibration ERP upload failed")
        return False


# Calibration is a golden-device verification, not a force-scale adjustment.
# Daily calibration window uses CALIBRATION_START / CALIBRATION_END at the top.
CALIBRATION_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "c10_btn_jig.db")

# Update these limits here; both checks, prompts and DB use this mapping.
CALIBRATION_LIMITS = {"right": (12.0, 14.0), "centre": (10.0, 12.0), "left": (12.0, 14.0)}
CALIBRATION_BUTTONS = {"1": "right", "2": "centre", "3": "left"}

def calibration_dip_ok(button, value):
    low, high = CALIBRATION_LIMITS[button]
    return value is not None and math.isfinite(value) and low <= value <= high

def calibration_limits_text():
    return "; ".join(f"{name}: {low:g}–{high:g} N" for name, (low, high) in CALIBRATION_LIMITS.items())

def normal_plot_displacement(values):
    """Same zero-seeded EMA used by the normal plot (alpha=0.99)."""
    smoothed = [0]
    for i in range(1, len(values)):
        smoothed.append(smoothed[i-1] * 0.99 + (1 - 0.99) * values[i])
    return smoothed


def save_calibration_report(attempt_id, period, employee, model, dips, curves, passed, reason, finished_at):
    """Save one separate graph and a one-page PDF for this calibration attempt.

    Uses Matplotlib's built-in PDF backend; no extra PDF package is required.
    Curves contain (force_N, displacement_mm), just like the serial data.
    """
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.backends.backend_pdf import PdfPages
    import textwrap

    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration_reports", period)
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    stem = f"calibration_{stamp}_attempt_{attempt_id}"
    graph_path = os.path.join(folder, stem + ".png")
    pdf_path = os.path.join(folder, stem + ".pdf")
    colors = {"right": "#4A9EFF", "centre": "#F5A623", "left": "#2ECC71"}

    def draw_graph(ax):
        any_data = False
        for name in CALIBRATION_LIMITS:
            force, displacement = curves.get(name, ([], []))
            if not force or not displacement:
                continue
            any_data = True
            displacement = normal_plot_displacement(displacement)
            ax.plot(displacement, force, color=colors[name],
                    label=f"{name.capitalize()} {[round(dips[name], 2)] if dips.get(name) is not None else []}", linewidth=1.2)
            value = dips.get(name)
            if value is not None and math.isfinite(value):
                index = min(range(len(force)), key=lambda i: abs(force[i] - value))
                ax.scatter([displacement[index]], [force[index]], color=colors[name], s=25)
        ax.set_xlabel("Displacement (mm)")
        ax.set_ylabel("Force (N)")
        ax.set_title("Golden-device calibration - recorded curves", fontsize=11)
        ax.grid(alpha=0.25)
        if any_data:
            ax.legend(fontsize=9)
        else:
            ax.text(0.5, 0.5, "No curve data recorded", transform=ax.transAxes, ha="center")

    graph = Figure(figsize=(9, 5), tight_layout=True)
    FigureCanvasAgg(graph)
    draw_graph(graph.add_subplot(111))
    graph.savefig(graph_path, dpi=160)
    graph.clear()

    report = Figure(figsize=(8.27, 11.69), facecolor="white")
    FigureCanvasAgg(report)
    report.text(0.08, 0.95, "C10 JIG CALIBRATION", fontsize=19, weight="bold")
    report.text(0.08, 0.92, f"Result: {'PASS' if passed else 'FAIL'}", fontsize=14,
                color="#208044" if passed else "#bb2828", weight="bold")
    report.text(0.08, 0.875, f"Completed: {finished_at}\nCalibration date ({CALIBRATION_START_TEXT}-{CALIBRATION_END_TEXT} IST): {period}\n"
                f"Attempt: {attempt_id}   Model: {model}   Employee: {employee}", fontsize=10, linespacing=1.7, va="top")
    table_ax = report.add_axes([0.08, 0.665, 0.84, 0.14])
    table_ax.axis("off")
    rows = []
    for name, (low, high) in CALIBRATION_LIMITS.items():
        value = dips.get(name)
        valid = calibration_dip_ok(name, value)
        rows.append([name.capitalize(), f"{value:.2f}" if value is not None and math.isfinite(value) else "Missing",
                     f"{low:g} - {high:g}", "IN RANGE" if valid else "OUT OF RANGE"])
    table = table_ax.table(cellText=rows, colLabels=["Button", "Measured dip (N)", "Limits (N)", "Dip check"],
                           cellLoc="center", loc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1, 1.8)
    for (row, col), cell in table.get_celld().items():
        cell.set_edgecolor("#d1d9e0")
        if row == 0:
            cell.set_facecolor("#e9eff5")
            cell.set_text_props(weight="bold")
    ax = report.add_axes([0.12, 0.25, 0.79, 0.34])
    draw_graph(ax)
    detail = reason or "All three dips are within the configured limits."
    detail = detail.replace("–", "-").replace("—", "-")
    wrapped = textwrap.wrap(detail, 95)
    report.text(0.08, 0.17, "Observation: " + "\n".join(wrapped[:6]), fontsize=9, va="top", linespacing=1.5)
    report.text(0.08, 0.055, "Golden-device verification only. This test does not adjust the force scale.", fontsize=8, color="#555555")
    try:
        with PdfPages(pdf_path + ".tmp") as pdf:
            pdf.savefig(report)
        os.replace(pdf_path + ".tmp", pdf_path)
    finally:
        report.clear()
    return graph_path, pdf_path


def calibration_period(now=None):
    now = now or ist_now()
    if now.tzinfo is not None:
        now = now.astimezone(IST)
    return now.date().isoformat()


def init_calibration_db():
    with sqlite3.connect(CALIBRATION_DB) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS jig_calibration (
            id INTEGER PRIMARY KEY AUTOINCREMENT, period TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, employee_id TEXT,
            model TEXT, system TEXT, status TEXT NOT NULL,
            right_dip_n REAL, centre_dip_n REAL, left_dip_n REAL,
            dips_json TEXT, reason TEXT, lower_n REAL, upper_n REAL)""")
        columns = {row[1] for row in db.execute("PRAGMA table_info(jig_calibration)")}
        if "limits_json" not in columns:
            db.execute("ALTER TABLE jig_calibration ADD COLUMN limits_json TEXT")
        if "serialnumber" not in columns:
            db.execute("ALTER TABLE jig_calibration ADD COLUMN serialnumber TEXT")


class JigCalibrationMixin:
    """Calibration callbacks using the host window UI and motion results."""

    def _calibration_valid(self):
        now = ist_now()
        start = now.replace(hour=CALIBRATION_START[0], minute=CALIBRATION_START[1], second=0, microsecond=0)
        with sqlite3.connect(CALIBRATION_DB) as db:
            row = db.execute(
                "SELECT status, limits_json, started_at FROM jig_calibration WHERE period=? AND model=? AND system=? "
                "ORDER BY id DESC LIMIT 1",
                (calibration_period(now), self.selected_model, self.erp_system)).fetchone()
        if not row or row[0] != "PASS" or row[1] != json.dumps(CALIBRATION_LIMITS, sort_keys=True):
            return False
        try:
            started = datetime.fromisoformat(row[2])
            if started.tzinfo is None:
                started = started.replace(tzinfo=IST)
            return start <= started.astimezone(IST) <= now
        except (TypeError, ValueError):
            return False


    def _calibration_failed_today(self):
        # A failed attempt locks this model/system for the rest of the IST day.
        now = ist_now()
        start = now.replace(hour=CALIBRATION_START[0], minute=CALIBRATION_START[1], second=0, microsecond=0)
        with sqlite3.connect(CALIBRATION_DB) as db:
            rows = db.execute(
                "SELECT started_at FROM jig_calibration WHERE period=? AND model=? AND system=? AND status='FAIL'",
                (calibration_period(now), self.selected_model, self.erp_system)).fetchall()
        for (stamp,) in rows:
            try:
                started = datetime.fromisoformat(stamp)
                if started.tzinfo is None:
                    started = started.replace(tzinfo=IST)
                if start <= started.astimezone(IST) <= now:
                    return True
            except (TypeError, ValueError):
                continue
        return False


    def _select_test_mode(self, mode):
        if getattr(self, "_run_thread", None) is not None and self._run_thread.isRunning():
            self._refresh_mode_buttons()
            return
        now = ist_now()
        if self._calibration_failed_today():
            self._refresh_mode_buttons()
            return
        if self._calibration_valid():
            self._selected_test_mode = "production"
            self._refresh_mode_buttons()
            return
        if mode == "production" and (now.hour * 60 + now.minute) >= CALIBRATION_END_MINUTES and not self._calibration_valid():
            mode = "calibration"
            QMessageBox.warning(self, "Calibration required", f"After {CALIBRATION_END_TEXT} IST production is blocked until today's golden-device calibration passes.")
        self._selected_test_mode = mode
        self._refresh_mode_buttons()


    def _refresh_mode_buttons(self):
        now = ist_now()
        minutes = now.hour * 60 + now.minute
        try:
            valid = self._calibration_valid()
            failed = self._calibration_failed_today()
        except Exception:
            logger.exception("Could not read calibration state")
            self.production_mode_button.setEnabled(False)
            self.calibration_mode_button.setEnabled(False)
            self.pushButton_19.setEnabled(False)
            return
        running = getattr(self, "_run_thread", None) is not None and self._run_thread.isRunning()
        busy = running or getattr(self, "_homing_in_progress", False)
        if not busy:
            if valid or minutes < CALIBRATION_START_MINUTES:
                self._selected_test_mode = "production"
            elif minutes >= CALIBRATION_END_MINUTES and not failed:
                self._selected_test_mode = "calibration"
        calibration = self._selected_test_mode == "calibration"
        # Change the input only on a mode transition, never on every timer tick.
        previous_input_mode = getattr(self, "_serial_input_mode", None)
        if previous_input_mode != self._selected_test_mode:
            if calibration:
                self._production_serial_before_calibration = self.lineEdit_3.text()
                self.lineEdit_3.setText(GOLDEN_DEVICE_SERIAL)
            elif previous_input_mode == "calibration":
                self.lineEdit_3.setText(getattr(self, "_production_serial_before_calibration", ""))
            self._serial_input_mode = self._selected_test_mode
        self.lineEdit_3.setReadOnly(calibration)
        production_allowed = not failed and (valid or minutes < CALIBRATION_END_MINUTES)
        calibration_allowed = not valid and not failed and minutes >= CALIBRATION_START_MINUTES
        self.production_mode_button.setChecked(not calibration)
        self.calibration_mode_button.setChecked(calibration)
        self.production_mode_button.setEnabled(production_allowed and not busy)
        self.calibration_mode_button.setEnabled(calibration_allowed and not busy)
        self.pushButton_19.setEnabled(
            not busy and getattr(self, "_homed", False)
            and (calibration_allowed if calibration else production_allowed))
        self.calibration_mode_button.setToolTip(
            "Calibration failed - service required; retry disabled" if failed else
            "Today's calibration already passed" if valid else
            f"Calibration opens at {CALIBRATION_START_TEXT} IST" if minutes < CALIBRATION_START_MINUTES else
            "Run golden-device calibration")


    def _check_calibration_reset(self):
        now = ist_now()
        running = getattr(self, "_run_thread", None) is not None and self._run_thread.isRunning()
        if running:
            return  # Finish the current motion safely; gate the next production unit.
        try:
            valid = self._calibration_valid()
        except Exception:
            logger.exception("Calibration status could not be read")
            valid = False
        overdue = (now.hour * 60 + now.minute) >= CALIBRATION_END_MINUTES and not valid
        if valid:
            self._selected_test_mode = "production"
        elif overdue:
            self._selected_test_mode = "calibration"
        self._refresh_mode_buttons()
        if self._calibration_failed_today():
            message = "CALIBRATION FAILED - service required; production and retry blocked"
        elif valid:
            message = f"Today's calibration: PASS | {CALIBRATION_START_TEXT}-{CALIBRATION_END_TEXT} IST"
        elif overdue:
            message = f"CALIBRATION REQUIRED - production blocked after {CALIBRATION_END_TEXT} IST"
        elif now.hour * 60 + now.minute >= CALIBRATION_START_MINUTES:
            message = f"Calibration due by {CALIBRATION_END_TEXT} IST - select Calibration mode"
        else:
            message = f"Calibration window: {CALIBRATION_START_TEXT}-{CALIBRATION_END_TEXT} IST today"
        self.calibration_schedule_label.setText(message)
        # Inform once per day in this session; opening the app during the
        # reminder window also shows it. Never interrupt homing or a test.
        today = calibration_period(now)
        if (CALIBRATION_START_MINUTES <= now.hour * 60 + now.minute < CALIBRATION_END_MINUTES and not valid
                and not getattr(self, "_homing_in_progress", False)
                and getattr(self, "_homed", False)
                and self._calibration_reminder_date != today):
            self._calibration_reminder_date = today  # Set before opening a modal.
            QMessageBox.information(self, "Calibration time",
                f"It is calibration time ({CALIBRATION_START_TEXT}-{CALIBRATION_END_TEXT} IST).\n"
                "Please select Calibration mode and test the golden device.\n"
                f"You can continue production until {CALIBRATION_END_TEXT} IST. After {CALIBRATION_END_TEXT}, "
                "production will be blocked until today's calibration passes.")


    def _finish_calibration(self, error=""):
        if self._calibration_attempt is None:
            return
        dips = {}
        failures = []
        for name, key in (("right", "1"), ("centre", "2"), ("left", "3")):
            result = getattr(self, "_force_press_results", {}).get(key, {})
            value = result.get("dip")
            dips[name] = value
            if not calibration_dip_ok(name, value) or result.get("failure"):
                failures.append(f"{name}: {value if value is not None else 'missing'} N; expected {CALIBRATION_LIMITS[name][0]:g}–{CALIBRATION_LIMITS[name][1]:g} N")
        if error:
            failures.append(error)
        if calibration_period() != self._calibration_started_period:
            failures.append("IST date changed; run calibration again")
        passed = not failures
        reason = "; ".join(failures)
        try:
            with sqlite3.connect(CALIBRATION_DB) as db:
                db.execute("""UPDATE jig_calibration SET finished_at=?, status=?,
                    right_dip_n=?, centre_dip_n=?, left_dip_n=?, dips_json=?, reason=? WHERE id=?""",
                    (ist_now().isoformat(), "PASS" if passed else "FAIL",
                     dips["right"], dips["centre"], dips["left"], json.dumps(dips), reason,
                     self._calibration_attempt))
        except Exception as exc:
            passed = False
            reason = f"Could not save calibration: {exc}"
        report_note = ""
        try:
            curves = {name: self.parse_data(data) for name, data in (
                ("right", self.data1), ("centre", self.data2), ("left", self.data3))}
            graph_path, pdf_path = save_calibration_report(
                self._calibration_attempt, self._calibration_started_period,
                self.employee_id, self.selected_model, dips, curves, passed, reason,
                ist_now().isoformat())
            report_note = f"\nGraph saved: {graph_path}\nPDF saved: {pdf_path}"
            logger.info("Calibration graph: %s; PDF: %s", graph_path, pdf_path)
        except Exception as exc:
            logger.exception("Calibration report could not be saved")
            report_note = f"\nCalibration report could not be saved: {exc}"
        # Send exactly the same JSON representation as the local dips_json.
        erp_status = os.getenv("C10_JIG_PASS_STATUS", "PASS") if passed else os.getenv("C10_JIG_FAIL_STATUS", "FAIL")
        erp_sent = send_jig_pass(self.employee_id, self.erp_system, json.dumps(dips), erp_status)
        if erp_sent:
            report_note += "\nCalibration values sent to Jig Details."
        else:
            report_note += "\nERP upload failed: calibration values were not sent to Jig Details. Local result is retained."
        self._calibration_attempt = None
        self._calibration_mode = False
        self.lineEdit_3.setText("")
        if passed:
            self._selected_test_mode = "production"
        self._refresh_mode_buttons()
        if passed:
            self._anim_finish_pass()
            self._progress_done_pass()
            self.label_2.setText("CALIBRATION PASS — remove golden device, then scan production unit")
            QMessageBox.information(self, "Calibration passed", "Today's calibration passed. Next calibration tomorrow.")
        else:
            self._anim_finish_fail()
            self._progress_done_fail()
            self.label_2.setText("SERVICE REQUIRED — production blocked")
            QMessageBox.warning(self, "Service required", reason + "\nProduction blocked. Service required. Calibration retry is disabled for today." + report_note)


    @QtCore.pyqtSlot()
    def _calibration_aborted(self):
        self._finish_calibration("Motor/serial sequence aborted")

