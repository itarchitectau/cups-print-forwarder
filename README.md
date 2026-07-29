# CUPS Print Forwarder

A lightweight web application that lets users upload documents (PDF, DOCX, TIFF) through a browser and send them directly to a CUPS print queue. Access is protected by HTTP Digest Authentication.

## Features

- Upload PDF, DOCX, and TIFF/TIF files via a drag-and-drop interface
- Forwards print jobs to any printer registered with a local or remote CUPS server
- HTTP Digest Authentication — credentials never travel in plaintext
- DOCX-to-PDF conversion via LibreOffice (headless)
- Printer selection and copy count from the UI
- Uploaded files are deleted from disk immediately after queuing

## Requirements

- Python 3.9+
- A reachable [CUPS](https://www.cups.org/) server (default: `localhost:631`)
- [LibreOffice](https://www.libreoffice.org/) installed and on `PATH` (only needed for DOCX files)

## Installation

```bash
git clone <repo-url>
cd cups-print-forwarder
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

> **Note:** `pycups` is Linux-only. The Flask server must run on a Linux host (typically the same machine as CUPS, or any Linux box that can reach CUPS over the network).

## Configuration

All settings live in [`config.py`](config.py) and can be overridden with environment variables.

| Environment variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | random (per process) | Flask session secret — set a stable value in production |
| `FLASK_HOST` | `0.0.0.0` | Interface to listen on |
| `FLASK_PORT` | `5000` | Port to listen on |
| `CUPS_HOST` | `localhost` | CUPS server hostname or IP |
| `CUPS_PORT` | `631` | CUPS server port |
| `CUPS_PRINTER` | *(empty)* | Printer name to use; empty = CUPS default |
| `LIBREOFFICE_BIN` | `soffice` | LibreOffice binary name or full path |

### Changing credentials

Edit `DIGEST_USERS` in [`config.py`](config.py):

```python
DIGEST_USERS = {
    "alice": "s3cr3t",
    "bob":   "hunter2",
}
```

Each key is a username; the value is the password sent through HTTP Digest (never transmitted in plaintext).

### Using an `.env` file

```bash
cp .env.example .env
# Edit .env with your values, then:
export $(grep -v '^#' .env | xargs)
python run.py
```

## Running

```bash
python run.py
```

The server starts on `http://0.0.0.0:5000`. Open it in a browser — you will be prompted for credentials before the upload form is shown.

### Running as a systemd service

Create `/etc/systemd/system/print-forwarder.service`:

```ini
[Unit]
Description=CUPS Print Forwarder
After=network.target cups.service

[Service]
User=www-data
WorkingDirectory=/opt/cups-print-forwarder
EnvironmentFile=/opt/cups-print-forwarder/.env
ExecStart=/opt/cups-print-forwarder/.venv/bin/python run.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now print-forwarder
```

### Running behind a reverse proxy (nginx)

```nginx
server {
    listen 443 ssl;
    server_name print.example.com;

    # SSL certificates here ...

    client_max_body_size 50M;

    location / {
        proxy_pass         http://127.0.0.1:5000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
```

## Supported file types

| Format | Sent to CUPS as |
|---|---|
| PDF | PDF (direct) |
| TIFF / TIF | TIFF (direct) |
| DOCX | Converted to PDF via LibreOffice, then sent |

## Project structure

```
cups-print-forwarder/
├── app.py              # Flask routes, auth, CUPS submission logic
├── config.py           # Settings and defaults
├── run.py              # Entry point
├── requirements.txt
├── .env.example
└── templates/
    └── index.html      # Drag-and-drop upload UI
```

## Troubleshooting

**"No default CUPS printer configured"**
Set `CUPS_PRINTER` in your environment or `config.py`, or configure a default printer in CUPS (`lpadmin -d <printer-name>`).

**DOCX conversion fails**
Ensure LibreOffice is installed (`libreoffice --version`) and that the `LIBREOFFICE_BIN` setting points to the correct binary (often `/usr/bin/soffice` on Debian/Ubuntu).

**401 loop in browser**
Some browsers cache Digest auth state across tabs. Open a private window or clear the browser's auth cache for the site.

**pycups import error on non-Linux**
`pycups` requires libcups. On Debian/Ubuntu: `sudo apt install libcups2-dev`. The app is not designed to run on Windows or macOS — deploy it on the Linux host running CUPS.

## License

MIT
