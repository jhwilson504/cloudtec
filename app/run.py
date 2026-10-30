"""Start KasTec: on this PC only (http://localhost:8000), or on the network over
HTTPS once "Set up network access" has been run (see tls.py).

    python -m app.run [--open-browser]
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from pathlib import Path

from .tls import CERT, KEY, cert_summary, read_network
from .config import env

LOCAL_PORT = 8000


def server_options(data_dir: Path) -> dict:
    """uvicorn settings, plus the address to open in the browser on this PC."""
    net = read_network(data_dir)
    if not net.get("enabled"):
        return {"host": "127.0.0.1", "port": LOCAL_PORT, "url": f"http://localhost:{LOCAL_PORT}", "tls": False}
    cert, key = data_dir / "tls" / CERT, data_dir / "tls" / KEY
    if not (cert.exists() and key.exists()):
        raise SystemExit("Network access is turned on but there's no certificate. "
                         "Run 'Set up network access' again, or choose option 5 there to turn it off.")
    port = int(net.get("port") or 8443)
    # Open the name the certificate is for (a company CA may not include "localhost").
    names = [n for n in (cert_summary(data_dir) or {}).get("names", []) if not n[0].isdigit()] or ["localhost"]
    return {"host": "0.0.0.0", "port": port, "url": f"https://{names[0]}:{port}", "tls": True,
            "ssl_certfile": str(cert), "ssl_keyfile": str(key)}


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m app.run")
    p.add_argument("--open-browser", action="store_true")
    args = p.parse_args(argv)

    data_dir = Path(env("DATA_DIR", "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    opts = server_options(data_dir)
    if opts["tls"]:
        os.environ["KASTEC_SECURE_COOKIES"] = "true"  # sign-in cookies only ever travel encrypted

    print(f"\n  KasTec Center is starting at {opts['url']}")
    if opts["tls"]:
        print(f"  Colleagues can open https://<this PC's name>:{opts['port']}")
    print("\n  Keep this window open while you use KasTec. Close it to stop KasTec.\n")
    if args.open_browser:
        threading.Timer(3, webbrowser.open, args=[opts["url"]]).start()

    import uvicorn
    tls = {k: opts[k] for k in ("ssl_certfile", "ssl_keyfile") if k in opts}
    uvicorn.run("app.main:create_app", factory=True, host=opts["host"], port=opts["port"], **tls)


if __name__ == "__main__":
    sys.exit(main())
