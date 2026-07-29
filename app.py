import os
import subprocess
import tempfile
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


def docx_to_pdf(docx_path: str) -> str:
    """Convert DOCX to PDF using LibreOffice; returns path to generated PDF."""
    out_dir = os.path.dirname(docx_path)
    subprocess.run(
        [
            config.LIBREOFFICE_BIN,
            "--headless",
            "--convert-to", "pdf",
            "--outdir", out_dir,
            docx_path,
        ],
        check=True,
        timeout=60,
    )
    base = os.path.splitext(os.path.basename(docx_path))[0]
    return os.path.join(out_dir, base + ".pdf")


def send_to_cups(file_path: str, job_title: str, copies: int = 1) -> int:
    """Submit file to CUPS and return job id."""
    conn = cups.Connection(host=config.CUPS_HOST, port=config.CUPS_PORT)
    printer = config.CUPS_PRINTER or conn.getDefault()
    if not printer:
        raise RuntimeError("No default CUPS printer configured.")
    options = {"copies": str(copies)}
    job_id = conn.printFile(printer, file_path, job_title, options)
    return job_id


@app.route("/")
@auth.login_required
def index():
    return render_template("index.html")


@app.route("/printers")
@auth.login_required
def list_printers():
    try:
        conn = cups.Connection(host=config.CUPS_HOST, port=config.CUPS_PORT)
        printers = list(conn.getPrinters().keys())
        default = conn.getDefault()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify({"printers": printers, "default": default})


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
    unique_name = f"{uuid.uuid4().hex}_{safe_name}"
    save_path = os.path.join(app.config["UPLOAD_FOLDER"], unique_name)
    f.save(save_path)

    try:
        ext = safe_name.rsplit(".", 1)[-1].lower()
        print_path = save_path

        if ext == "docx":
            print_path = docx_to_pdf(save_path)

        # Temporarily override printer if caller specified one
        orig_printer = config.CUPS_PRINTER
        if printer_name:
            config.CUPS_PRINTER = printer_name

        job_id = send_to_cups(print_path, job_title=safe_name, copies=copies)
        config.CUPS_PRINTER = orig_printer

    except subprocess.CalledProcessError:
        return jsonify({"error": "DOCX conversion failed. Is LibreOffice installed?"}), 500
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:
        return jsonify({"error": f"Print error: {exc}"}), 500
    finally:
        # Clean up uploaded file (and converted PDF if different)
        for p in {save_path, print_path if "print_path" in dir() else save_path}:
            try:
                os.remove(p)
            except OSError:
                pass

    return jsonify({"success": True, "job_id": job_id, "printer": printer_name or "default"})


@app.errorhandler(413)
def too_large(_):
    return jsonify({"error": "File too large. Maximum size is 50 MB."}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
