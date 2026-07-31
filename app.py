import json
import os
import socket
import subprocess
import threading
import time
import uuid

import cups
from flask import Flask, jsonify, render_template, request
from flask_httpauth import HTTPDigestAuth
from werkzeug.utils import secure_filename

import config

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32))
app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH
app.config["UPLOAD_FOLDER"] = config.UPLOAD_FOLDER

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

auth = HTTPDigestAuth()


@auth.get_password
def get_pw(username):
    return config.DIGEST_USERS.get(username)


def allowed_file(filename: str) -> bool:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in config.ALLOWED_EXTENSIONS


def cups_conn():
    return cups.Connection(host=config.CUPS_HOST, port=config.CUPS_PORT)


def docx_to_pdf(docx_path: str) -> str:
    out_dir = os.path.dirname(docx_path)
    subprocess.run(
        [config.LIBREOFFICE_BIN, "--headless", "--convert-to", "pdf",
         "--outdir", out_dir, docx_path],
        check=True, timeout=60,
    )
    base = os.path.splitext(os.path.basename(docx_path))[0]
    return os.path.join(out_dir, base + ".pdf")


def send_to_cups(file_path: str, job_title: str, printer: str, copies: int = 1) -> int:
    conn = cups_conn()
    printer = printer or conn.getDefault()
    if not printer:
        raise RuntimeError("No default CUPS printer configured.")
    return conn.printFile(printer, file_path, job_title, {"copies": str(copies)})


# ── Wake-on-LAN / host probing ─────────────────────────────────────────────────

WAKE_TARGETS_FILE = os.path.join(os.path.dirname(__file__), "wake_targets.json")
# Common printer ports: JetDirect, IPP, HTTP, HTTPS
_PROBE_PORTS = (9100, 631, 80, 443)


def _load_wake_targets() -> list:
    if not os.path.exists(WAKE_TARGETS_FILE):
        return []
    with open(WAKE_TARGETS_FILE) as f:
        return json.load(f)


def _save_wake_targets(targets: list) -> None:
    with open(WAKE_TARGETS_FILE, "w") as f:
        json.dump(targets, f, indent=2)


def _wol_magic_packet(mac: str) -> bytes:
    clean = mac.replace(":", "").replace("-", "").replace(".", "").upper()
    if len(clean) != 12:
        raise ValueError(f"Invalid MAC address: {mac!r}")
    mac_bytes = bytes.fromhex(clean)
    return b"\xff" * 6 + mac_bytes * 16


def _send_wol(mac: str, host: str | None = None) -> None:
    packet = _wol_magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        # Limited broadcast — only reaches devices on the server's local subnet.
        for port in (9, 7):
            s.sendto(packet, ("<broadcast>", port))
        # Unicast to the printer's known IP — routable across subnets/routers.
        if host:
            for port in (9, 7):
                s.sendto(packet, (host, port))


def _probe_host(host: str, timeout: float = 2.0) -> dict:
    """Probe all printer ports in parallel; returns {port: bool}."""
    results: dict = {}
    lock = threading.Lock()

    def _check(port: int) -> None:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                with lock:
                    results[port] = True
        except OSError:
            with lock:
                results[port] = False

    threads = [threading.Thread(target=_check, args=(p,), daemon=True) for p in _PROBE_PORTS]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout + 0.5)
    return results


# ── Pages ──────────────────────────────────────────────────────────────────────

@app.route("/")
@auth.login_required
def index():
    return render_template("index.html")


# ── Printers ───────────────────────────────────────────────────────────────────

@app.route("/printers")
@auth.login_required
def list_printers():
    try:
        conn = cups_conn()
        printers = list(conn.getPrinters().keys())
        default = conn.getDefault()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify({"printers": printers, "default": default})


# ── Print ──────────────────────────────────────────────────────────────────────

@app.route("/print", methods=["POST"])
@auth.login_required
def print_file():
    if "file" not in request.files:
        return jsonify({"error": "No file part in request."}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "No file selected."}), 400
    if not allowed_file(f.filename):
        return jsonify({"error": "Unsupported file type. Allowed: pdf, docx, tiff, tif."}), 415

    copies = max(1, min(int(request.form.get("copies", 1)), 99))
    printer_name = request.form.get("printer", "").strip() or config.CUPS_PRINTER

    safe_name = secure_filename(f.filename)
    save_path = os.path.join(app.config["UPLOAD_FOLDER"], f"{uuid.uuid4().hex}_{safe_name}")
    f.save(save_path)

    print_path = save_path
    try:
        if safe_name.rsplit(".", 1)[-1].lower() == "docx":
            print_path = docx_to_pdf(save_path)
        job_id = send_to_cups(print_path, job_title=safe_name,
                              printer=printer_name, copies=copies)
    except subprocess.CalledProcessError:
        return jsonify({"error": "DOCX conversion failed. Is LibreOffice installed?"}), 500
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:
        return jsonify({"error": f"Print error: {exc}"}), 500
    finally:
        for p in {save_path, print_path}:
            try:
                os.remove(p)
            except OSError:
                pass

    return jsonify({"success": True, "job_id": job_id, "printer": printer_name or "default"})


# ── Job queue ──────────────────────────────────────────────────────────────────

_JOB_STATE_LABELS = {
    3: "Pending", 4: "Held",      5: "Processing",
    6: "Stopped", 7: "Canceled",  8: "Aborted",   9: "Completed",
}


@app.route("/jobs")
@auth.login_required
def list_jobs():
    which = request.args.get("which", "not-completed")
    if which not in ("not-completed", "completed", "all"):
        which = "not-completed"
    try:
        conn = cups_conn()
        raw = conn.getJobs(which_jobs=which, my_jobs=False)
        jobs = [
            {
                "id":          jid,
                "name":        attrs.get("job-name", "—"),
                "state":       attrs.get("job-state", 0),
                "state_label": _JOB_STATE_LABELS.get(attrs.get("job-state", 0), "Unknown"),
                "printer":     attrs.get("job-printer-uri", "").rstrip("/").split("/")[-1],
                "user":        attrs.get("job-originating-user-name", "—"),
                "size_kb":     attrs.get("job-k-octets", 0),
                "created":     attrs.get("time-at-creation", 0),
            }
            for jid, attrs in raw.items()
        ]
        jobs.sort(key=lambda j: j["created"], reverse=True)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify({"jobs": jobs})


@app.route("/jobs/<int:job_id>/cancel", methods=["POST"])
@auth.login_required
def cancel_job(job_id):
    try:
        cups_conn().cancelJob(job_id)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"success": True})


@app.route("/jobs/<int:job_id>/release", methods=["POST"])
@auth.login_required
def release_job(job_id):
    try:
        cups_conn().releaseJob(job_id)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"success": True})


# ── Wake targets ───────────────────────────────────────────────────────────────

@app.route("/wake/targets")
@auth.login_required
def get_wake_targets():
    return jsonify({"targets": _load_wake_targets()})


@app.route("/wake/targets", methods=["POST"])
@auth.login_required
def add_wake_target():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    host = (data.get("host") or "").strip()
    mac  = (data.get("mac")  or "").strip()

    if not name or not host:
        return jsonify({"error": "name and host are required"}), 400
    if mac:
        try:
            _wol_magic_packet(mac)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    targets = _load_wake_targets()
    target = {"id": uuid.uuid4().hex, "name": name, "host": host, "mac": mac}
    targets.append(target)
    _save_wake_targets(targets)
    return jsonify({"success": True, "target": target}), 201


@app.route("/wake/targets/<tid>", methods=["DELETE"])
@auth.login_required
def delete_wake_target(tid):
    targets = _load_wake_targets()
    new_targets = [t for t in targets if t["id"] != tid]
    if len(new_targets) == len(targets):
        return jsonify({"error": "Target not found"}), 404
    _save_wake_targets(new_targets)
    return jsonify({"success": True})


@app.route("/wake/targets/<tid>/probe", methods=["POST"])
@auth.login_required
def probe_wake_target(tid):
    targets = _load_wake_targets()
    target = next((t for t in targets if t["id"] == tid), None)
    if not target:
        return jsonify({"error": "Target not found"}), 404
    probe = _probe_host(target["host"])
    return jsonify({
        "online": any(probe.values()),
        "ports":  {str(k): v for k, v in probe.items()},
    })


@app.route("/wake/targets/<tid>/wake", methods=["POST"])
@auth.login_required
def wake_target(tid):
    targets = _load_wake_targets()
    target = next((t for t in targets if t["id"] == tid), None)
    if not target:
        return jsonify({"error": "Target not found"}), 404

    result: dict = {"wol_sent": False, "wol_error": None, "online": False, "ports": {}}

    if target.get("mac"):
        try:
            _send_wol(target["mac"], host=target["host"])
            result["wol_sent"] = True
        except Exception as exc:
            result["wol_error"] = str(exc)

    # Brief pause, then TCP probe (also wakes standby printers)
    time.sleep(1)
    probe = _probe_host(target["host"])
    result["online"] = any(probe.values())
    result["ports"]  = {str(k): v for k, v in probe.items()}
    return jsonify(result)


@app.route("/wake/all", methods=["POST"])
@auth.login_required
def wake_all_targets():
    targets = _load_wake_targets()
    if not targets:
        return jsonify({"error": "No wake targets configured"}), 400

    results: dict = {}
    for t in targets:
        r: dict = {"wol_sent": False, "wol_error": None}
        if t.get("mac"):
            try:
                _send_wol(t["mac"], host=t["host"])
                r["wol_sent"] = True
            except Exception as exc:
                r["wol_error"] = str(exc)
        results[t["id"]] = r

    time.sleep(2)

    def probe_one(t: dict) -> None:
        probe = _probe_host(t["host"])
        results[t["id"]]["online"] = any(probe.values())
        results[t["id"]]["ports"]  = {str(k): v for k, v in probe.items()}

    threads = [threading.Thread(target=probe_one, args=(t,), daemon=True) for t in targets]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    return jsonify({"results": results})


# ── Services ───────────────────────────────────────────────────────────────────

@app.route("/service/cups-browsed/restart", methods=["POST"])
@auth.login_required
def restart_cups_browsed():
    cmd = config.RESTART_CUPS_BROWSED_CMD.split()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return jsonify(
                {"error": result.stderr.strip() or f"Command exited {result.returncode}"}
            ), 500
    except FileNotFoundError:
        return jsonify({"error": f"Command not found: {cmd[0]}"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Restart timed out after 30 s"}), 500
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"success": True})


# ── Error handlers ─────────────────────────────────────────────────────────────

@app.errorhandler(413)
def too_large(_):
    return jsonify({"error": "File too large. Maximum size is 50 MB."}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
