import os

# HTTP Digest Auth credentials (username -> password)
DIGEST_USERS = {
    "admin": "changeme",
}

# CUPS settings
CUPS_HOST = os.environ.get("CUPS_HOST", "localhost")
CUPS_PORT = int(os.environ.get("CUPS_PORT", 631))
CUPS_PRINTER = os.environ.get("CUPS_PRINTER", "")  # empty = default printer

# LibreOffice binary for DOCX->PDF conversion
LIBREOFFICE_BIN = os.environ.get(
    "LIBREOFFICE_BIN",
    "soffice",  # override with full path if not on PATH
)

# Command used to restart cups-browsed (space-separated).
# In Docker with --privileged + --pid=host you can use:
#   nsenter -t 1 -m -u -i -n -- systemctl restart cups-browsed
RESTART_CUPS_BROWSED_CMD = os.environ.get(
    "RESTART_CUPS_BROWSED_CMD",
    "systemctl restart cups-browsed",
)

# Upload limits
MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50 MB
ALLOWED_EXTENSIONS = {"pdf", "docx", "tiff", "tif"}
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
