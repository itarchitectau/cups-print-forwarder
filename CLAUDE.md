# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
# Native (Linux only — pycups requires libcups)
pip install -r requirements.txt
python run.py

# Debug mode
FLASK_DEBUG=1 python run.py

# Docker (primary deployment path; first build is slow — LibreOffice is large)
docker compose up -d --build
docker compose logs -f
```

The server listens on `http://0.0.0.0:5000`. All routes require HTTP Digest Auth (default: `admin` / `changeme` in `config.py`).

There is no test suite and no linter configuration.

## Architecture

All application logic lives in two files — there are no submodules or packages.

**`config.py`** is the single source of truth for every tuneable value. Every setting has an `os.environ.get()` override, so environment variables always win over the Python defaults. Credentials (`DIGEST_USERS`) are the only setting that cannot be overridden by env var — they must be edited directly.

**`app.py`** contains all Flask routes and all business logic. It imports `config` directly (not as a package). Route groups:

| URL prefix | Purpose |
|---|---|
| `GET /` | Serves the single-page UI |
| `/printers` | Lists CUPS printers via pycups |
| `/print` | Accepts multipart upload, optionally converts DOCX→PDF via LibreOffice, queues to CUPS, deletes the file |
| `/jobs`, `/jobs/<id>/cancel`, `/jobs/<id>/release` | CUPS job queue management |
| `/wake/targets` (CRUD) + `/wake/targets/<id>/probe`, `/wake/targets/<id>/wake`, `/wake/all` | Wake-on-LAN / TCP probe management |
| `/service/cups-browsed/restart` | Runs `RESTART_CUPS_BROWSED_CMD` in a subprocess |

**`run.py`** is a thin entry point that generates a stable `SECRET_KEY` before importing `app`. Use it instead of invoking `app.py` directly.

**`templates/index.html`** is a single self-contained file: all CSS and JavaScript are inline, no build step or bundler. Tabs (Upload / Print Queue / Wake Printers / Services) are driven by vanilla JS toggling `display` on `.panel` divs.

**`wake_targets.json`** is auto-created on the first `POST /wake/targets`. It is a flat JSON array; the file is fully read and fully rewritten on every mutating request (no locking — single-process only).

## Key implementation details

- **pycups connection**: `cups_conn()` opens a new `cups.Connection` on every call. There is no connection pool.
- **Port probing**: `_probe_host()` opens TCP connections to ports 9100/631/80/443 using one daemon thread per port so all four run in parallel, bounding the probe to ~2 s regardless of target count.
- **WOL**: `_send_wol()` broadcasts the magic packet on UDP ports 9 and 7. WOL does not cross routers; the server must be on the same L2 broadcast domain as the printer.
- **File lifecycle**: uploaded files are saved to `uploads/` with a UUID prefix, then always deleted in a `finally` block — even when printing fails. If DOCX conversion produces a second file, that is also deleted.
- **Auth**: `@auth.login_required` is on every route. Flask-HTTPAuth handles the Digest challenge/response cycle automatically; the browser stores credentials after the first 401 handshake and replays them on all subsequent fetch() calls from the SPA.

## Configuration reference

All values can be set as environment variables:

| Variable | Default | Notes |
|---|---|---|
| `SECRET_KEY` | random per process | Set a stable value in production |
| `CUPS_HOST` / `CUPS_PORT` | `localhost` / `631` | |
| `CUPS_PRINTER` | `""` | Empty = CUPS default printer |
| `LIBREOFFICE_BIN` | `soffice` | Full path if not on PATH |
| `RESTART_CUPS_BROWSED_CMD` | `systemctl restart cups-browsed` | Space-split before exec; use `nsenter` variant in Docker |
| `FLASK_HOST` / `FLASK_PORT` | `0.0.0.0` / `5000` | |
| `FLASK_DEBUG` | `""` | Set to `1` for debug mode |

## Docker notes

- The image installs `libcups2-dev` (required by pycups) and `libreoffice-writer` at build time.
- CUPS on the host is reached via `host.docker.internal` (auto-resolved on Docker Desktop; on Linux Docker Engine the `extra_hosts: host.docker.internal:host-gateway` line in `docker-compose.yml` handles this).
- To allow the Services tab to restart `cups-browsed` on the host, uncomment `pid: host` and `privileged: true` in `docker-compose.yml` and set `RESTART_CUPS_BROWSED_CMD=nsenter -t 1 -m -u -i -n -- systemctl restart cups-browsed`.
- WOL broadcasts do not traverse the Docker bridge by default; use `--network host` (Linux) or configure a directed-broadcast relay for WOL to reach the printer subnet.
