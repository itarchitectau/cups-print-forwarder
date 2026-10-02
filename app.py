import io
import json
import logging
import os
import re
import socket
import sqlite3
import subprocess
import threading
import time
import uuid

import cups
from flask import Flask, Response, jsonify, render_template, request, send_file
from flask_httpauth import HTTPDigestAuth
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from PIL import Image
from werkzeug.utils import secure_filename

import config

# Decompression bomb protection — reject images larger than 30 MP
Image.MAX_IMAGE_PIXELS = 30_000_000

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32))
app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH
app.config["UPLOAD_FOLDER"] = config.UPLOAD_FOLDER

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

auth = HTTPDigestAuth()
limiter = Limiter(key_func=get_remote_address, app=app, default_limits=[])


# ── CSRF-like protection ───────────────────────────────────────────────────────

@app.before_request
def require_xhr_for_mutations():
    if request.method in ("POST", "DELETE", "PATCH", "PUT"):
        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            return jsonify({"error": "Forbidden"}), 403


@auth.get_password
def get_pw(username):
    return config.DIGEST_USERS.get(username)


def allowed_file(filename: str) -> bool:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in config.ALLOWED_EXTENSIONS


# ── Magic bytes validation ─────────────────────────────────────────────────────

_MAGIC: dict[str, list[tuple[int, bytes]]] = {
    "pdf":  [(0, b"%PDF")],
    "docx": [(0, b"PK\x03\x04")],
    "tiff": [(0, b"II*\x00"), (0, b"MM\x00*")],
    "tif":  [(0, b"II*\x00"), (0, b"MM\x00*")],
}


def _check_magic(path: str, ext: str) -> bool:
    checks = _MAGIC.get(ext.lower())
    if not checks:
        return False
    with open(path, "rb") as f:
        header = f.read(8)
    return any(header[off : off + len(sig)] == sig for off, sig in checks)


def cups_conn():
    return cups.Connection(host=config.CUPS_HOST, port=config.CUPS_PORT)


# LibreOffice concurrency guard — prevent OOM from simultaneous conversions
_LO_SEM = threading.Semaphore(2)


def docx_to_pdf(docx_path: str) -> str:
    out_dir = os.path.dirname(docx_path)
    with _LO_SEM:
        subprocess.run(
            [
                config.LIBREOFFICE_BIN,
                "--headless", "--norestore", "--nofirststartwizard",
                "--convert-to", "pdf",
                "--outdir", out_dir,
                docx_path,
            ],
            check=True, timeout=60,
        )
    base = os.path.splitext(os.path.basename(docx_path))[0]
    return os.path.join(out_dir, base + ".pdf")


_VALID_SIDES = {"one-sided", "two-sided-long-edge", "two-sided-short-edge"}
_VALID_COLOR_MODES = {"color", "monochrome"}
_PAGE_RANGE_RE = re.compile(r'^\d+(-\d+)?(,\d+(-\d+)?)*$')


def send_to_cups(
    file_path: str,
    job_title: str,
    printer: str,
    copies: int = 1,
    page_ranges: str = "",
    sides: str = "",
    color_mode: str = "",
) -> int:
    conn = cups_conn()
    printer = printer or conn.getDefault()
    if not printer:
        raise RuntimeError("No default CUPS printer configured.")
    options: dict = {"copies": str(copies)}
    if page_ranges:
        options["page-ranges"] = page_ranges
    if sides and sides in _VALID_SIDES:
        options["sides"] = sides
    if color_mode and color_mode in _VALID_COLOR_MODES:
        options["print-color-mode"] = color_mode
    return conn.printFile(printer, file_path, job_title, options)


# ── Wake-on-LAN / host probing ─────────────────────────────────────────────────

WAKE_TARGETS_FILE = os.path.join(os.path.dirname(__file__), "wake_targets.json")
_PROBE_PORTS = (9100, 631, 80, 443)
_wake_lock = threading.Lock()


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
        for port in (9, 7):
            s.sendto(packet, ("<broadcast>", port))
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


# ── Local job store ────────────────────────────────────────────────────────────

JOBS_DB = os.path.join(os.path.dirname(__file__), "jobs.db")


def _init_jobs_db() -> None:
    with sqlite3.connect(JOBS_DB) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id  INTEGER PRIMARY KEY,
                name    TEXT    NOT NULL DEFAULT '',
                printer TEXT    NOT NULL DEFAULT '',
                user    TEXT    NOT NULL DEFAULT '',
                created INTEGER NOT NULL DEFAULT 0,
                deleted INTEGER NOT NULL DEFAULT 0
            )
        """)


_init_jobs_db()


def _store_job(job_id: int, name: str, printer: str, user: str) -> None:
    with sqlite3.connect(JOBS_DB) as db:
        db.execute(
            "INSERT OR IGNORE INTO jobs (job_id, name, printer, user, created, deleted) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (job_id, name, printer, user, int(time.time())),
        )


def _get_stored_jobs() -> dict:
    with sqlite3.connect(JOBS_DB) as db:
        rows = db.execute(
            "SELECT job_id, name, printer, user, created, deleted FROM jobs"
        ).fetchall()
    return {
        row[0]: {
            "name": row[1], "printer": row[2], "user": row[3],
            "created": row[4], "deleted": bool(row[5]),
        }
        for row in rows
    }


def _mark_job_deleted(job_id: int) -> None:
    with sqlite3.connect(JOBS_DB) as db:
        db.execute(
            "INSERT OR IGNORE INTO jobs (job_id, name, printer, user, created, deleted) "
            "VALUES (?, '', '', '', 0, 0)",
            (job_id,),
        )
        db.execute("UPDATE jobs SET deleted=1 WHERE job_id=?", (job_id,))


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
        logger.error("CUPS list printers failed: %s", exc)
        return jsonify({"error": "Failed to connect to CUPS."}), 503
    return jsonify({"printers": printers, "default": default})


# ── Preview ────────────────────────────────────────────────────────────────────

@app.route("/preview", methods=["POST"])
@auth.login_required
@limiter.limit("20 per minute")
def preview_document():
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files["file"]
    if not f.filename or not allowed_file(f.filename):
        return jsonify({"error": "Unsupported file type"}), 415

    safe_name = secure_filename(f.filename)
    ext = safe_name.rsplit(".", 1)[-1].lower()
    save_path = os.path.join(app.config["UPLOAD_FOLDER"], f"{uuid.uuid4().hex}_{safe_name}")
    f.save(save_path)

    extra: list = []
    try:
        if not _check_magic(save_path, ext):
            return jsonify({"error": "File content does not match its extension."}), 415

        if ext in ("pdf", "docx"):
            out_path = save_path
            if ext == "docx":
                out_path = docx_to_pdf(save_path)
                extra.append(out_path)
            with open(out_path, "rb") as fp:
                data = fp.read()
            return Response(data, mimetype="application/pdf",
                            headers={"Content-Disposition": "inline"})

        # TIFF — return first page as PNG
        img = Image.open(save_path)
        try:
            img.seek(0)
        except (EOFError, AttributeError):
            pass
        if img.mode not in ("RGB", "RGBA", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return send_file(buf, mimetype="image/png")

    except subprocess.CalledProcessError:
        logger.error("LibreOffice conversion failed for %s", safe_name)
        return jsonify({"error": "DOCX conversion failed. Is LibreOffice installed?"}), 500
    except Exception as exc:
        logger.error("Preview error for %s: %s", safe_name, exc)
        return jsonify({"error": "Preview generation failed."}), 500
    finally:
        for p in {save_path, *extra}:
            try:
                os.remove(p)
            except OSError:
                pass


# ── Print ──────────────────────────────────────────────────────────────────────

@app.route("/print", methods=["POST"])
@auth.login_required
@limiter.limit("30 per minute")
def print_file():
    if "file" not in request.files:
        return jsonify({"error": "No file part in request."}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "No file selected."}), 400
    if not allowed_file(f.filename):
        return jsonify({"error": "Unsupported file type. Allowed: pdf, docx, tiff, tif."}), 415

    try:
        copies = max(1, min(int(request.form.get("copies", 1)), 99))
    except (ValueError, TypeError):
        copies = 1

    printer_name = request.form.get("printer", "").strip() or config.CUPS_PRINTER

    page_ranges = request.form.get("page_ranges", "").strip()
    if page_ranges and not _PAGE_RANGE_RE.match(page_ranges):
        return jsonify({"error": "Invalid page range. Use formats like: 1-5  or  1,3,7-10"}), 400

    sides = request.form.get("sides", "").strip()
    if sides not in _VALID_SIDES:
        sides = ""

    color_mode = request.form.get("color_mode", "").strip()
    if color_mode not in _VALID_COLOR_MODES:
        color_mode = ""

    safe_name = secure_filename(f.filename)
    ext = safe_name.rsplit(".", 1)[-1].lower()
    save_path = os.path.join(app.config["UPLOAD_FOLDER"], f"{uuid.uuid4().hex}_{safe_name}")
    f.save(save_path)

    print_path = save_path
    try:
        if not _check_magic(save_path, ext):
            return jsonify({"error": "File content does not match its extension."}), 415

        if printer_name:
            conn = cups_conn()
            if printer_name not in conn.getPrinters():
                return jsonify({"error": f"Unknown printer: {printer_name}"}), 400

        if ext == "docx":
            print_path = docx_to_pdf(save_path)

        job_id = send_to_cups(
            print_path, job_title=safe_name,
            printer=printer_name, copies=copies,
            page_ranges=page_ranges, sides=sides,
            color_mode=color_mode,
        )
    except subprocess.CalledProcessError:
        logger.error("LibreOffice conversion failed for %s", safe_name)
        return jsonify({"error": "DOCX conversion failed. Is LibreOffice installed?"}), 500
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:
        logger.error("Print error for %s: %s", safe_name, exc)
        return jsonify({"error": "Failed to send job to printer."}), 500
    finally:
        for p in {save_path, print_path}:
            try:
                os.remove(p)
            except OSError:
                pass

    _store_job(job_id, name=safe_name, printer=printer_name or "default", user=auth.current_user())
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
        raw = conn.getJobs(
            which_jobs=which,
            my_jobs=False,
            requested_attributes=[
                "job-id",
                "job-name",
                "job-state",
                "job-printer-uri",
                "job-originating-user-name",
                "job-k-octets",
                "time-at-creation",
            ],
        )
        stored = _get_stored_jobs()

        jobs = []
        for jid, attrs in raw.items():
            rec = stored.get(jid, {})
            if rec.get("deleted"):
                continue
            state = attrs.get("job-state") or 0
            jobs.append({
                "id":         jid,
                "name":       attrs.get("job-name") or rec.get("name") or "—",
                "state":      state,
                "state_label": _JOB_STATE_LABELS.get(state, "Unknown"),
                "printer":    (attrs.get("job-printer-uri") or "").rstrip("/").split("/")[-1]
                              or rec.get("printer") or "—",
                "user":       attrs.get("job-originating-user-name") or rec.get("user") or "—",
                "size_kb":    attrs.get("job-k-octets") or 0,
                "created":    attrs.get("time-at-creation") or rec.get("created") or 0,
                "local_only": False,
            })

        # Include locally stored jobs that CUPS no longer tracks (purged after restart).
        if which in ("completed", "all"):
            cups_ids = set(raw.keys())
            for jid, rec in stored.items():
                if jid not in cups_ids and not rec["deleted"]:
                    jobs.append({
                        "id":         jid,
                        "name":       rec["name"] or "—",
                        "state":      9,
                        "state_label": "Completed",
                        "printer":    rec["printer"] or "—",
                        "user":       rec["user"] or "—",
                        "size_kb":    0,
                        "created":    rec["created"] or 0,
                        "local_only": True,
                    })

        jobs.sort(key=lambda j: j["created"], reverse=True)
    except Exception as exc:
        logger.error("List jobs failed: %s", exc)
        return jsonify({"error": "Failed to retrieve job queue."}), 503
    return jsonify({"jobs": jobs})


@app.route("/jobs/<int:job_id>/cancel", methods=["POST"])
@auth.login_required
def cancel_job(job_id):
    try:
        cups_conn().cancelJob(job_id)
    except Exception as exc:
        logger.error("Cancel job %d failed: %s", job_id, exc)
        return jsonify({"error": "Failed to cancel job."}), 500
    return jsonify({"success": True})


@app.route("/jobs/<int:job_id>/release", methods=["POST"])
@auth.login_required
def release_job(job_id):
    try:
        cups_conn().setJobHoldUntil(job_id, "no-hold")
    except Exception as exc:
        logger.error("Release job %d failed: %s", job_id, exc)
        return jsonify({"error": "Failed to release job."}), 500
    return jsonify({"success": True})


@app.route("/jobs/<int:job_id>/delete", methods=["POST"])
@auth.login_required
def delete_job(job_id):
    # Best-effort purge from CUPS — may fail if already purged; always update local store.
    try:
        cups_conn().cancelJob(job_id, purge=True)
    except Exception:
        pass
    _mark_job_deleted(job_id)
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

    with _wake_lock:
        targets = _load_wake_targets()
        target = {"id": uuid.uuid4().hex, "name": name, "host": host, "mac": mac}
        targets.append(target)
        _save_wake_targets(targets)
    return jsonify({"success": True, "target": target}), 201


@app.route("/wake/targets/<tid>", methods=["DELETE"])
@auth.login_required
def delete_wake_target(tid):
    with _wake_lock:
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
            logger.error("WOL failed for target %s: %s", tid, exc)
            result["wol_error"] = "WOL send failed."

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
                logger.error("WOL failed for target %s: %s", t["id"], exc)
                r["wol_error"] = "WOL send failed."
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
            logger.error("cups-browsed restart failed (exit %d): %s", result.returncode, result.stderr)
            return jsonify(
                {"error": f"Command exited with code {result.returncode}."}
            ), 500
    except FileNotFoundError:
        return jsonify({"error": f"Command not found: {cmd[0]}"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Restart timed out after 30 s"}), 500
    except Exception as exc:
        logger.error("cups-browsed restart error: %s", exc)
        return jsonify({"error": "Service restart failed."}), 500
    return jsonify({"success": True})


# ── Error handlers ─────────────────────────────────────────────────────────────

@app.errorhandler(413)
def too_large(_):
    return jsonify({"error": "File too large. Maximum size is 50 MB."}), 413


@app.errorhandler(429)
def rate_limited(_):
    return jsonify({"error": "Too many requests. Please slow down."}), 429


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
