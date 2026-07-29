import os
import subprocess
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
