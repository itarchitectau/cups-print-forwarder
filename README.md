# CUPS Print Forwarder

A lightweight web application that lets users upload documents (PDF, DOCX, TIFF) through a browser and send them directly to a CUPS print queue. Access is protected by HTTP Digest Authentication.

## Features

- Upload PDF, DOCX, and TIFF/TIF files via a drag-and-drop interface
- Forwards print jobs to any printer registered with a local or remote CUPS server
- HTTP Digest Authentication — credentials never travel in plaintext
- DOCX-to-PDF conversion via LibreOffice (headless)
- Printer selection, copy count, page range, and duplex mode from the UI
- Uploaded files are deleted from disk immediately after queuing
- **Print Queue tab** — view active/completed jobs; cancel or release held jobs
- **Wake Printers tab** — wake sleeping network printers via TCP probe (standby) or Wake-on-LAN (fully off)
- **Services tab** — restart the `cups-browsed` network printer discovery service

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
| `RESTART_CUPS_BROWSED_CMD` | `systemctl restart cups-browsed` | Shell command (space-split) used by the Services tab to restart cups-browsed |

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

## Print options

The upload form exposes four CUPS options:

| Field | CUPS option | Notes |
|---|---|---|
| Printer | — | Selects the destination queue; falls back to the CUPS default |
| Copies | `copies` | 1–99 |
| Page Range | `page-ranges` | Optional. Accepts standard CUPS notation: a single page (`5`), a range (`1-5`), or a comma-separated mix (`1-3,5,7-10`). Leave blank to print all pages. |
| Sides | `sides` | `one-sided` (default), `two-sided-long-edge` (portrait / book binding), or `two-sided-short-edge` (landscape / calendar binding) |

The page range is validated on the server before submission; an invalid format returns a 400 error rather than being silently ignored by CUPS.

## Docker

The image bundles Python, pycups, and LibreOffice so there is nothing extra to install on the host beyond Docker itself.

### Quick start with Docker Compose

```bash
# Build and start (first build takes a few minutes — LibreOffice is large)
docker compose up -d --build

# Tail logs
docker compose logs -f

# Stop
docker compose down
```

The app is then available at `http://localhost:5000`.

CUPS must be running on the **host machine**. The container reaches it via `host.docker.internal` (resolved automatically on Docker Desktop for Mac/Windows; on Linux the `extra_hosts` entry in `docker-compose.yml` wires this up automatically).

### Build and run manually

```bash
docker build -t cups-print-forwarder .

docker run -d \
  --name cups-print-forwarder \
  --add-host host.docker.internal:host-gateway \
  -p 5000:5000 \
  -e SECRET_KEY=change-me \
  -e CUPS_HOST=host.docker.internal \
  -e CUPS_PRINTER=MyPrinter \
  cups-print-forwarder
```

### Passing configuration at runtime

All settings from the [Configuration](#configuration) table can be passed as `-e` flags or in an `.env` file:

```bash
docker run -d \
  --name cups-print-forwarder \
  --add-host host.docker.internal:host-gateway \
  -p 5000:5000 \
  --env-file .env \
  cups-print-forwarder
```

### Changing credentials in a container

Mount a custom `config.py` over the one baked into the image:

```bash
docker run -d \
  --name cups-print-forwarder \
  --add-host host.docker.internal:host-gateway \
  -p 5000:5000 \
  -v $(pwd)/config.py:/app/config.py:ro \
  cups-print-forwarder
```

## Wake Printers

The **Wake Printers** tab lets you maintain a list of network printer targets and wake them before printing.

### How it works

| Method | When it applies | What happens |
|---|---|---|
| **TCP probe** | Always (standby / sleep mode) | Opens a connection to ports 9100 (JetDirect), 631 (IPP), 80 (HTTP), 443 (HTTPS) — the connection attempt itself wakes most modern printers from sleep |
| **Wake-on-LAN** | When a MAC address is configured | Broadcasts a magic packet over UDP so the printer's NIC powers the device on from a fully-off state |

Both methods run on every **Wake** action. **Probe** only checks reachability without sending a WOL packet.

### Adding a printer

Click **+ Add Printer** and fill in:

| Field | Required | Notes |
|---|---|---|
| Name | Yes | Display label (e.g. `HP LaserJet 4015`) |
| IP / Hostname | Yes | e.g. `192.168.1.100` or `printer.local` |
| MAC Address | No | e.g. `AA:BB:CC:DD:EE:FF` — enables WOL |

Targets are persisted to `wake_targets.json` in the app directory (survives restarts).

### Wake-on-LAN requirements

- The printer NIC must support WOL (most network-connected printers do).
- Each Wake action sends the magic packet **twice**: as a UDP broadcast (reaches same-subnet devices) and as a unicast directly to the printer's IP (routable across subnets/routers). No router configuration is required for cross-subnet WOL.
- In Docker, broadcasts do not leave the bridge network by default. Use `--network host` (Linux) or rely on the unicast delivery, which works without host-networking.

## Project structure

```
cups-print-forwarder/
├── app.py              # Flask routes, auth, CUPS & wake logic
├── config.py           # Settings and defaults
├── run.py              # Entry point
├── wake_targets.json   # Persisted wake targets (auto-created)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── .env.example
└── templates/
    └── index.html      # Four-tab UI: Upload, Print Queue, Wake Printers, Services
```

## Troubleshooting

**Restarting cups-browsed in a container**
The Services tab runs `RESTART_CUPS_BROWSED_CMD` inside the container. To reach the host's systemd, set `pid: host` and `privileged: true` in `docker-compose.yml` (commented out by default), then set:
```
RESTART_CUPS_BROWSED_CMD=nsenter -t 1 -m -u -i -n -- systemctl restart cups-browsed
```

**Service restart returns "Command not found: systemctl"**
The host is not using systemd. Try `service cups-browsed restart` or set `RESTART_CUPS_BROWSED_CMD` to the correct init command for your distro.

**Probe always shows Offline even though the printer is reachable**
The probe checks ports 9100, 631, 80, and 443 in parallel with a 2-second timeout. If the printer uses a non-standard port or has a host firewall, add a direct ping test: `ping <printer-ip>` from the server to confirm basic connectivity.

**WOL packet sent but printer stays off**
Confirm WOL is enabled in the printer's network settings (some models call it "Wake from Sleep" or "EWS sleep settings"). The app sends the magic packet both as a broadcast and as a unicast to the printer's IP, so router boundaries are not the cause. Check that the printer's MAC address is entered correctly in the Wake Printers tab.

**WOL unicast is sent but still no response**
Some managed switches drop unsolicited UDP traffic to powered-off hosts (the ARP entry expires while the printer is off). Enable "WOL forwarding" or "directed-broadcast" on the switch port, or use a VLAN that permits these frames.

**"No default CUPS printer configured"**
Set `CUPS_PRINTER` in your environment or `config.py`, or configure a default printer in CUPS (`lpadmin -d <printer-name>`).

**DOCX conversion fails**
Ensure LibreOffice is installed (`libreoffice --version`) and that the `LIBREOFFICE_BIN` setting points to the correct binary (often `/usr/bin/soffice` on Debian/Ubuntu).

**401 loop in browser**
Some browsers cache Digest auth state across tabs. Open a private window or clear the browser's auth cache for the site.

**pycups import error on non-Linux**
`pycups` requires libcups. On Debian/Ubuntu: `sudo apt install libcups2-dev`. The app is not designed to run on Windows or macOS — deploy it on the Linux host running CUPS, or use the Docker image.

**Container cannot reach CUPS on the host**
Confirm CUPS is listening on all interfaces (not just `127.0.0.1`). Edit `/etc/cups/cupsd.conf` and ensure `Listen *:631` is set (or at minimum `Listen 0.0.0.0:631`), then restart CUPS. Also check that port 631 is not blocked by a host firewall for the Docker bridge network.

**LibreOffice crashes inside the container**
LibreOffice needs a writable user profile directory. The image runs as root so this should not be an issue by default, but if you run as a non-root user add `-e HOME=/tmp` to give LibreOffice a writable home.

## License

MIT
