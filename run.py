"""Entry point — sets a stable secret key and starts the server."""
import os
import secrets

os.environ.setdefault("SECRET_KEY", secrets.token_hex(32))

from app import app  # noqa: E402

if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true")
    app.run(host=host, port=port, debug=debug)
