"""Certificate expiry alerts.

Watches CloudTec's own HTTPS certificate (<data dir>/tls/cert.pem) plus a list of other
certificates, and warns 30 and then 21 days before each one expires: as a banner on the
dashboard, and by email when an SMTP server is configured.

<data dir>/cert_watch.json holds what to watch and who to tell:

    {"targets": [{"target": "btcbahamas.com", "owners": ["web-team@example.com"], "note": "public site"},
                 {"target": "10.255.215.121:443"},
                 {"target": "C:\\certs\\trailmail.cer"}],
     "email_to": ["sysops@example.com"]}

A target is a host name or IP address with an optional port (443 by default), or the path
of a certificate file (.cer, .crt, .pem, .der or .p7b). Each alert is emailed once per
stage (30 days, 21 days, expired) to email_to plus that certificate's owners.

Run "python -m app.certwatch" to check now from the command line (e.g. in Task Scheduler).
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import smtplib
import socket
import ssl
import sys
import threading
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes

from .config import Settings
from .tls import CERT, _load_certs, _write

ALERT_DAYS = (30, 21)            # alert stages, in days before expiry
RENEW_DAYS = ALERT_DAYS[0]       # the first alert: time to renew
CONFIG, STATE = "cert_watch.json", "cert_alerts.json"
CERT_EXTENSIONS = (".pem", ".cer", ".crt", ".der", ".p7b")


class WatchError(Exception):
    pass


def _now(now: dt.datetime | None) -> dt.datetime:
    return now or dt.datetime.now(dt.timezone.utc)


# --- the alert window ----------------------------------------------------------

def alert_level(not_after: dt.datetime, now: dt.datetime | None = None,
                windows: tuple[int, ...] = ALERT_DAYS) -> dict:
    """How close a certificate is to expiring.

    level is "expired", "critical" (inside the last window, 21 days), "warning" (inside the
    first, 30 days) or "ok"; threshold is the window it's inside (0 once expired, else None).
    """
    now = _now(now)
    days_left = (not_after - now).days                   # whole days, rounded down
    if not_after <= now:
        return {"days_left": days_left, "level": "expired", "threshold": 0}
    inside = [w for w in sorted(windows) if days_left <= w]
    if not inside:
        return {"days_left": days_left, "level": "ok", "threshold": None}
    threshold = inside[0]
    return {"days_left": days_left, "threshold": threshold,
            "level": "critical" if threshold == min(windows) else "warning"}


def own_cert(data_dir: Path) -> x509.Certificate | None:
    path = data_dir / "tls" / CERT
    if not path.exists():
        return None
    return x509.load_pem_x509_certificates(path.read_bytes())[0]


def days_left(data_dir: Path, now: dt.datetime | None = None) -> int | None:
    """Days until CloudTec's own HTTPS certificate expires, or None if there isn't one."""
    cert = own_cert(data_dir)
    return None if cert is None else alert_level(cert.not_valid_after_utc, now)["days_left"]


def needs_renewal(data_dir: Path, days: int = RENEW_DAYS, now: dt.datetime | None = None) -> bool:
    left = days_left(data_dir, now)
    return left is not None and left <= days


# --- what to watch ---------------------------------------------------------------

def is_file_target(target: str) -> bool:
    return "/" in target or "\\" in target or target.lower().endswith(CERT_EXTENSIONS)


def split_host(target: str) -> tuple[str, int]:
    """'host', 'host:8443', '10.0.0.5:443' or '[::1]:443' -> (host, port)."""
    target = target.strip()
    if target.startswith("["):
        host, _, rest = target[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else 443
    if target.count(":") == 1:
        host, port = target.split(":")
        return host, int(port)
    return target, 443


def clean_target(entry: dict | str) -> dict:
    if isinstance(entry, str):
        entry = {"target": entry}
    target = str(entry.get("target", "")).strip().strip('"')
    if not target:
        raise WatchError("Each certificate to watch needs a host name, address or file path.")
    if not is_file_target(target):
        try:
            host, port = split_host(target)
        except ValueError:
            raise WatchError(f"'{target}' isn't a host, host:port or certificate file.") from None
        if not host or not 0 < port < 65536:
            raise WatchError(f"'{target}' isn't a host, host:port or certificate file.")
    owners = [o.strip() for o in entry.get("owners") or [] if o.strip()]
    for o in owners:
        if "@" not in o:
            raise WatchError(f"'{o}' isn't an email address.")
    return {"target": target, "owners": owners, "note": str(entry.get("note") or "").strip()}


def read_config(data_dir: Path) -> dict:
    try:
        raw = json.loads((data_dir / CONFIG).read_text())
    except (FileNotFoundError, ValueError):
        raw = {}
    return {"targets": [clean_target(t) for t in raw.get("targets", [])],
            "email_to": list(raw.get("email_to", []))}


def save_config(data_dir: Path, targets: list, email_to: list[str]) -> dict:
    email_to = [e.strip() for e in email_to if e.strip()]
    for e in email_to:
        if "@" not in e:
            raise WatchError(f"'{e}' isn't an email address.")
    config = {"targets": [clean_target(t) for t in targets], "email_to": email_to}
    _write(data_dir / CONFIG, json.dumps(config, indent=2).encode())
    return config


# --- reading certificates --------------------------------------------------------

def proxy_for(host: str) -> tuple[str, int] | None:
    """The HTTPS proxy to reach a host through, if this PC uses one.

    CLOUDTEC_CERT_PROXY (host:port, or "off") wins; otherwise HTTPS_PROXY or, on Windows, the
    proxy set in Internet Options, skipping the hosts listed there as bypassed.
    """
    setting = os.environ.get("CLOUDTEC_CERT_PROXY", "").strip()
    if setting.lower() in ("off", "none", "direct"):
        return None
    if not setting:
        if urllib.request.proxy_bypass(host):
            return None
        setting = urllib.request.getproxies().get("https", "")
    if not setting:
        return None
    url = urllib.parse.urlsplit(setting if "://" in setting else f"http://{setting}")
    return (url.hostname, url.port or 8080) if url.hostname else None


def _tunnel(proxy: tuple[str, int], host: str, port: int, timeout: float) -> socket.socket:
    sock = socket.create_connection(proxy, timeout=timeout)
    try:
        sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
        reply = b""
        while b"\r\n\r\n" not in reply:
            chunk = sock.recv(4096)
            if not chunk or len(reply) > 65536:
                raise OSError(f"proxy {proxy[0]}:{proxy[1]} closed the connection")
            reply += chunk
        status = reply.split(b"\r\n", 1)[0].decode(errors="replace")
        if status.split()[1:2] != ["200"]:
            raise OSError(f"proxy {proxy[0]}:{proxy[1]} refused: {status}")
        return sock
    except BaseException:
        sock.close()
        raise


def fetch_host_cert(host: str, port: int = 443, timeout: float = 10) -> x509.Certificate:
    """The certificate a server presents, even if it has expired or isn't trusted here.

    Goes through this PC's proxy when it has one (as a browser would), then tries directly.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    proxy = proxy_for(host)
    routes = ([("via proxy", lambda: _tunnel(proxy, host, port, timeout))] if proxy else []) + \
             [("direct", lambda: socket.create_connection((host, port), timeout=timeout))]
    errors = []
    for name, connect in routes:
        try:
            with connect() as sock, ctx.wrap_socket(sock, server_hostname=host) as tls_sock:
                der = tls_sock.getpeercert(binary_form=True)
            return x509.load_der_x509_certificate(der)
        except (OSError, ssl.SSLError) as e:
            errors.append(f"{name}: {e or e.__class__.__name__}" if proxy else str(e) or e.__class__.__name__)
    raise OSError("; ".join(errors))


def _describe(cert: x509.Certificate, now: dt.datetime) -> dict:
    return {"subject": cert.subject.rfc4514_string(), "issuer": cert.issuer.rfc4514_string(),
            "expires": cert.not_valid_after_utc.isoformat(),
            "fingerprint": cert.fingerprint(hashes.SHA256()).hex(),
            **alert_level(cert.not_valid_after_utc, now)}


def check_target(entry: dict, now: dt.datetime | None = None, timeout: float = 10) -> dict:
    now = _now(now)
    target = entry["target"]
    out = {**entry, "source": "file" if is_file_target(target) else "host", "error": None}
    try:
        if out["source"] == "file":
            certs = _load_certs(Path(target).read_bytes())
            # The soonest to expire is the one that will break things first.
            cert = min(certs, key=lambda c: c.not_valid_after_utc)
        else:
            cert = fetch_host_cert(*split_host(target), timeout=timeout)
        return {**out, **_describe(cert, now)}
    except (OSError, ValueError, ssl.SSLError) as e:
        return {**out, "level": "error", "error": str(e) or e.__class__.__name__}
    except Exception as e:  # e.g. TLSError from an unreadable file
        return {**out, "level": "error", "error": str(e)}


def check_all(data_dir: Path, now: dt.datetime | None = None, timeout: float = 10) -> list[dict]:
    """CloudTec's own certificate first, then every configured target."""
    now = _now(now)
    results = []
    cert = own_cert(data_dir)
    if cert is not None:
        results.append({"target": "CloudTec (this server)", "owners": [], "note": "", "source": "cloudtec",
                        "error": None, **_describe(cert, now)})
    results += [check_target(t, now, timeout) for t in read_config(data_dir)["targets"]]
    return results


def alerts(results: list[dict]) -> list[dict]:
    return [r for r in results if r["level"] in ("warning", "critical", "expired")]


# --- email -----------------------------------------------------------------------

def email_enabled(settings: Settings) -> bool:
    return bool(settings.smtp_host)


def send_email(settings: Settings, to: list[str], subject: str, body: str) -> None:
    if not settings.smtp_host:
        raise WatchError("No mail server is set up (CLOUDTEC_SMTP_HOST).")
    msg = EmailMessage()
    msg["Subject"], msg["To"] = subject, ", ".join(to)
    msg["From"] = settings.alert_from or settings.smtp_user or f"cloudtec@{socket.getfqdn()}"
    msg.set_content(body)
    cls = smtplib.SMTP_SSL if settings.smtp_port == 465 else smtplib.SMTP
    with cls(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        if settings.smtp_starttls and cls is smtplib.SMTP:
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password or "")
        smtp.send_message(msg)


def _line(r: dict) -> str:
    when = r["expires"][:10]
    state = (f"EXPIRED on {when}" if r["level"] == "expired"
             else f"expires {when} ({r['days_left']} day{'s' if r['days_left'] != 1 else ''} left)")
    extra = "".join(f"\n    {k}: {v}" for k, v in (("subject", r["subject"]), ("issuer", r["issuer"]),
                                                     ("note", r["note"]), ("owners", ", ".join(r["owners"])))
                    if v)
    return f"- {r['target']}: {state}{extra}"


def _read_state(data_dir: Path) -> dict:
    try:
        return json.loads((data_dir / STATE).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def notify(settings: Settings, results: list[dict], now: dt.datetime | None = None) -> list[dict]:
    """Email each certificate that reached a new alert stage since the last email. Returns those sent."""
    if not email_enabled(settings):
        return []
    data_dir = settings.data_dir
    config = read_config(data_dir)
    state = _read_state(data_dir)
    # A stage is "new" when it's closer to expiry than the last one emailed for this exact certificate,
    # so a renewed certificate (new fingerprint) starts over.
    due = [r for r in alerts(results)
           if state.get(r["fingerprint"], {}).get("threshold", 10**6) > r["threshold"]]
    if not due:
        return []
    by_recipients: dict[tuple, list[dict]] = {}
    for r in due:
        to = tuple(dict.fromkeys(config["email_to"] + r["owners"]))
        if to:
            by_recipients.setdefault(to, []).append(r)
    sent = []
    for to, items in by_recipients.items():
        worst = min(r["days_left"] for r in items)
        subject = (f"Certificate expired: {items[0]['target']}" if worst < 0 and len(items) == 1
                   else f"Certificate expiring in {worst} days: {items[0]['target']}" if len(items) == 1
                   else f"{len(items)} certificates need renewing (soonest in {max(worst, 0)} days)")
        body = ("These certificates are expiring soon and need renewing:\n\n"
                + "\n\n".join(_line(r) for r in items)
                + f"\n\nCloudTec warns {' and '.join(map(str, ALERT_DAYS))} days before a certificate expires, "
                  "and again once it has expired.\n")
        send_email(settings, list(to), subject, body)
        stamp = _now(now).isoformat(timespec="seconds")
        for r in items:
            state[r["fingerprint"]] = {"target": r["target"], "threshold": r["threshold"], "sent": stamp}
        sent += items
    _write(data_dir / STATE, json.dumps(state, indent=2).encode())
    return sent


# --- running in the background ------------------------------------------------------

class Watcher:
    """Re-checks every few hours in a background thread and keeps the latest results."""

    def __init__(self, settings: Settings, hours: float | None = None):
        self.settings = settings
        self.hours = hours if hours is not None else settings.cert_check_hours
        self.results: list[dict] | None = None
        self.checked: str | None = None
        self.last_error: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def check(self, send: bool = True) -> list[dict]:
        with self._lock:
            results = check_all(self.settings.data_dir)
            self.results, self.checked = results, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            if send:
                try:
                    notify(self.settings, results)
                    self.last_error = None
                except (OSError, smtplib.SMTPException, WatchError) as e:
                    self.last_error = f"Couldn't send the alert email: {e}"
            return results

    def latest(self) -> list[dict]:
        return self.results if self.results is not None else self.check(send=False)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check()
            except Exception as e:  # never let the checker die quietly
                self.last_error = f"Certificate check failed: {e}"
            self._stop.wait(self.hours * 3600)

    def start(self) -> None:
        if self.hours > 0 and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="certwatch", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()


# --- command line --------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.certwatch",
                                description="Check certificate expiry. Exit code 2 if any need attention.")
    p.add_argument("--data-dir", default=os.environ.get("CLOUDTEC_DATA_DIR", "data"))
    p.add_argument("--email", action="store_true", help="email new alerts (needs CLOUDTEC_SMTP_HOST)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    settings = dataclasses.replace(Settings.from_env(), data_dir=Path(args.data_dir))
    results = check_all(settings.data_dir)
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            if r["error"]:
                print(f"  ??  {r['target']}: couldn't check ({r['error']})")
            else:
                mark = {"ok": "  OK", "warning": "  !!", "critical": " !!!", "expired": "XXXX"}[r["level"]]
                print(f"{mark}  {r['target']}: expires {r['expires'][:10]} ({r['days_left']} days)")
    if args.email:
        try:
            for r in notify(settings, results):
                print(f"Emailed an alert for {r['target']}")
        except (OSError, smtplib.SMTPException, WatchError) as e:
            print(f"Error: couldn't send the alert email: {e}", file=sys.stderr)
            return 1
    return 2 if alerts(results) else 0


if __name__ == "__main__":
    sys.exit(main())
