# C10 Button Displacement Test & Jig Calibration

Desktop application for testing the right, centre, and left buttons on a C10 device. The application controls the test jig through Arduino serial input and GRBL motion commands, displays live force/displacement graphs, records pass/fail results locally, and integrates with ERP services. A separate golden-device calibration workflow is included.

## Contents

- `main(3).py` — PyQt5 desktop UI, serial communication, GRBL homing/motion, live graphs, test sequence, serial validation, and SQL/WebSocket integrations.
- `erp(20261016-030521).py` — ERPNext API helpers, employee/job-card lookup, local SQLite pass/fail storage, retrying ERP uploads, and pass-count helpers.
- `jig_calib.py` — daily golden-device calibration rules, calibration database, ERP upload, and graph/PDF report generation.

Keep these three Python files together: `main(3).py` imports functions and calibration helpers from the other two files.

## Main features

- Employee and job-card workflow backed by ERPNext/Frappe APIs.
- Arduino data collection and GRBL-controlled button pressing.
- Right, centre, and left button test sequence with live force/displacement visualization.
- Local storage of pass/fail test records in SQLite, with CSV logs for ERP upload attempts.
- Retry mechanism for test records that have not yet been uploaded successfully.
- Daily golden-device calibration with configurable time window and dip limits.
- Calibration history in SQLite and per-attempt PNG graph/PDF report output.
- Optional SQL-server upload and WebSocket communication with the configured server.

## Requirements

Use the Python version supported by the target machine and by its installed PyQt5/serial packages. The code imports these packages/modules:

- `PyQt5`
- `matplotlib`
- `numpy`
- `pyserial` (imported as `serial`)
- `requests`
- `websocket-client` (imported as `websocket`)
- `schedule`
- `python-dotenv` (used by `erp.py`)
- Standard library modules including `sqlite3`, `json`, `logging`, `csv`, `threading`, and `os`

Install the third-party packages in a virtual environment where possible:

```bash
python -m venv .venv
```

Activate it, then install dependencies:

**Windows**
```powershell
.venv\Scripts\activate
python -m pip install PyQt5 matplotlib numpy pyserial requests websocket-client schedule python-dotenv
```

**Linux / Raspberry Pi**
```bash
source .venv/bin/activate
python -m pip install PyQt5 matplotlib numpy pyserial requests websocket-client schedule python-dotenv
```

On Raspberry Pi/Linux, PyQt5 and hardware-related packages may also need to be installed through the operating system package manager, depending on the OS/Python version.

## Configuration

### 1. ERP configuration

`erp.py` loads configuration from a file named `production.env` in the current working directory. Create that file and configure the values expected by the application:

```dotenv
api_key=YOUR_FRAPPE_API_KEY
api_secret=YOUR_FRAPPE_API_SECRET

employee_url=https://YOUR-ERP-HOST/api/resource/Employee
item_url=https://YOUR-ERP-HOST/api/resource/Item
job_card_url=https://YOUR-ERP-HOST
data_log_url=https://YOUR-ERP-HOST/api/resource/Job Card Data Log
dashborad_url=https://YOUR-ERP-HOST/api/resource/Rejections
erp_pass=https://YOUR-ERP-HOST/YOUR_PASS_ENDPOINT
erp_nova=YOUR_NOVA_ENDPOINT
```

Use the exact endpoint paths expected by your ERP deployment. The current code builds Job Card URLs by appending `/api/resource/Job Card` to `job_card_url`, so configure `job_card_url` as the ERP base URL (for example, `https://YOUR-ERP-HOST`) rather than as a full Job Card resource URL.

The variable name `dashborad_url` is misspelled in the code; preserve that spelling in `production.env` unless you also update the Python code.

### 2. Hardware and server configuration

`main(3).py` reads these optional environment variables:

```dotenv
C10_REQUEST_TIMEOUT=10
C10_WS_SERVER_URL=ws://192.168.0.200:8050
C10_SQL_UPLOAD_URL=http://192.168.0.200:8000/upload_sql_data_1
C10_ARDUINO_PORT=/dev/ttyUSB0
C10_GRBL_PORT=/dev/ttyUSB1
```

Change the serial ports to match the device names on the target machine. The Arduino and GRBL ports must be different. The defaults above are Linux/Raspberry Pi defaults; on Windows, use the appropriate COM ports, for example `COM3` and `COM4`.

### 3. Calibration settings

Edit the configuration near the top of `jig_calib.py`:

- `GOLDEN_DEVICE_SERIAL` — golden-device serial number.
- `CALIBRATION_START` and `CALIBRATION_END` — daily calibration window in 24-hour IST `(hour, minute)` format.
- `CALIBRATION_LIMITS` — accepted force/dip limits for each button.
- `C10_JIG_PASS_URL` — optional ERP calibration endpoint override.
- `C10_JIG_PASS_STATUS` and `C10_JIG_FAIL_STATUS` — optional ERP status labels.

The currently configured values are:

| Setting | Current value |
|---|---|
| Golden-device serial | `GD-C10-001` |
| Calibration start | `19:30` IST |
| Calibration end | `19:33` IST |
| Right dip limit | `12–14 N` |
| Centre dip limit | `10–12 N` |
| Left dip limit | `12–14 N` |

**Important:** the comments beside the current calibration time constants do not match their numeric values. The code uses `19:30–19:33` IST (7:30–7:33 PM). Confirm the intended production schedule before deployment.

## Run the application

1. Connect the Arduino and GRBL controller.
2. Confirm that the configured serial ports are correct and that the GRBL controller is powered and responding.
3. Configure ERP credentials/endpoints and the server URLs.
4. Install the Python dependencies.
5. Run the application from the directory containing all three files:

```bash
python "main(3).py"
```

If you rename `main(3).py`, update your launch script/service to use the new filename.

The application should initialize the local databases, show the desktop UI, and start the serial/homing workflow. Follow the UI prompts and ensure homing completes before starting a test.

## Test workflow

1. Enter/select the employee and required production information in the UI.
2. Ensure the device serial number and job card are valid for the selected model/system.
3. Home the jig before running a button test.
4. Start the test and allow the configured sequence to press the right, centre, and left buttons.
5. Review the live graphs and final result.
6. Pass/fail records are stored locally and the application attempts to send them to the configured ERP endpoints.
7. If an upload fails, check the logs and network/API configuration; pending records can be retried by the application's retry logic.

### Force and displacement

The serial graph code labels the axes as displacement (mm) and force (N). The main application treats the first value received from the Arduino as the force value in several plotting/test paths, while the second value is handled as displacement. Confirm that the firmware's transmitted field order and units match this assumption before relying on recorded values for quality decisions.

Dip detection is implemented in the main application and uses the force/displacement curve data. Review `find_dips()`, `filter_dips()`, and the test result handling before changing acceptance logic. Calibration is a golden-device verification; the report explicitly states that it does **not** adjust the force scale.

## Calibration workflow

- Calibration data is stored in `c10_btn_jig.db` beside `jig_calib.py`.
- Each attempt records the date/period, employee, model, system, status, dip values, limits, and reason.
- Calibration reports are written under `calibration_reports/<YYYY-MM-DD>/` as PNG graphs and PDF reports.
- A passing calibration is required for production after the configured calibration deadline.
- The current logic treats a failed calibration as a service-required condition and disables further calibration retry for that model/system for the rest of the IST day.
- The calibration values are also posted to the configured ERP `Jig Details` endpoint.

## Local data and generated files

Depending on the workflow, the application may create/use:

- `c20_Button1.db` — local pass/fail job-card records.
- `c10_btn_jig.db` — calibration history.
- `employees.json` — cached employee data.
- `pass_count.json` — local pass-count state.
- `job_card_pass_log.csv` and `job_card_fail_log.csv` — CSV upload logs.
- `calibration_reports/` — per-attempt calibration PNG/PDF files.

Back up databases and reports according to your site's traceability requirements. Do not delete local records until their upload status and retention requirements have been checked.

## Troubleshooting

| Symptom | Checks |
|---|---|
| ERP returns `401 AuthenticationError` | Confirm API key/secret, ERP site, user permissions, and endpoint configuration. |
| Job Card lookup returns `404` or no result | Check `job_card_url` base URL, production item, employee mapping, and Job Card status. |
| Serial port cannot open | Check the port name, cable, permissions, and whether another process is using it. |
| GRBL does not respond or homing fails | Verify the GRBL port, USB connection, controller power, firmware response, and machine limits. |
| SQL upload fails | Check `C10_SQL_UPLOAD_URL`, server availability, network connectivity, and server-side logs. |
| WebSocket connection fails | Check `C10_WS_SERVER_URL`, port `8050` (or configured port), firewall, and server status. |
| Calibration is blocked | Check the IST time window, today's calibration row, configured limits, and whether a failure has locked the model/system for the day. |
| Calibration report is missing | Check write permissions and logs for errors creating `calibration_reports/`. |
| Printer integration is unavailable | Confirm that `cassinipart2_label.py` and its dependencies are installed; the import is optional in the current code. |

## Security and deployment notes

- **Do not commit credentials or tokens.** Keep `production.env` out of version control and use environment variables or a secret manager for credentials.
- The provided `jig_calib.py` contains ERP API credentials directly in the source. Move them to environment variables before sharing or deploying the code, and rotate those credentials if this file has been exposed beyond its intended environment.
- Verify the `is_jig_active()` implementation before production use. In the provided source it returns `True` immediately, so its ERP Active-jig lookup code is unreachable and production does not actually validate the jig's Active status through that function.
- Confirm the calibration time values, force limits, serial data field order, ERP Select status values, and all endpoints with the responsible engineering/ERP team before using the system for production quality decisions.
- Run the application only on a machine connected to the intended test hardware and ensure motion emergency-stop and safe operating procedures are available.

## Notes

This README documents the three supplied Python files. Server-side WebSocket/SQL services, Arduino firmware, GRBL configuration, ERP schema/custom fields, and optional printer modules are external dependencies and are not included here.
