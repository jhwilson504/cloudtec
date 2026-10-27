"""Automatic renewal of CloudTec's HTTPS certificate from a company CA, with certreq.

When the certificate in use expires within the renewal window (30 days by default), renew():

    1. makes a new key and request for the same names (tls.make_csr),
    2. submits it to the company CA with "certreq -submit",
    3. installs the issued certificate (tls.install_cert), keeping the old one as cert-previous.pem.

If the CA holds the request for a CA manager's approval, the request id is kept in
<data dir>/tls/renewal.json and the next run fetches it with "certreq -retrieve".
Every run is noted in <data dir>/tls/renewal.log. With --dry-run nothing is written and certreq isn't run.

Run it daily from Task Scheduler, e.g.:

    python -m app.renew --ca "ca01.corp.example.com\\Corp Issuing CA"

CloudTec reads the certificate when it starts, so restart it after a renewal.
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import NameOID

from . import tls

DEFAULT_TEMPLATE = "WebServer"
STATE, LOG, ISSUED, PREVIOUS = "renewal.json", "renewal.log", "renewed.cer", "cert-previous.pem"

# Stand-ins for app.certwatch (the expiry alerts), same names and shape; switch to it once it's merged.
RENEW_DAYS = 30


def days_left(data_dir: Path, now: dt.datetime | None = None) -> int | None:
    path = data_dir / "tls" / tls.CERT
    if not path.exists():
        return None
    cert = x509.load_pem_x509_certificates(path.read_bytes())[0]
    return (cert.not_valid_after_utc - (now or dt.datetime.now(dt.timezone.utc))).days


# --- certreq ----------------------------------------------------------------------

def _certreq(args: list[str]) -> str:
    try:
        r = subprocess.run(["certreq", *args], capture_output=True, text=True)
    except FileNotFoundError as e:
        raise tls.TLSError("certreq isn't available here (it comes with Windows).") from e
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        raise tls.TLSError(f"certreq failed: {out or f'exit code {r.returncode}'}")
    return out


def submit_args(ca: str, template: str, csr: Path, out: Path) -> list[str]:
    # -q: never show a dialog (it runs unattended), -f: overwrite the output file.
    return ["-submit", "-q", "-f", "-config", ca, "-attrib", f"CertificateTemplate:{template}", str(csr), str(out)]


def retrieve_args(ca: str, request_id: str, out: Path) -> list[str]:
    return ["-retrieve", "-q", "-f", "-config", ca, request_id, str(out)]


def _request_id(output: str) -> str | None:
    m = re.search(r'RequestId:\s*"?(\d+)', output)
    return m.group(1) if m else None


# --- state ------------------------------------------------------------------------

def _read_state(d: Path) -> dict | None:
    try:
        return json.loads((d / STATE).read_text())
    except (FileNotFoundError, ValueError):
        return None


def _log(d: Path, message: str) -> None:
    with open(d / LOG, "a", encoding="utf-8") as f:
        f.write(f"{dt.datetime.now().isoformat(timespec='seconds')}  {message}\n")


def _current_names(data_dir: Path) -> tuple[list[str], list[str]]:
    """The names the certificate in use covers, its common name first (make_csr uses names[0] as CN)."""
    cert = x509.load_pem_x509_certificates((data_dir / "tls" / tls.CERT).read_bytes())[0]
    names, ips = [], []
    for n in tls.cert_summary(data_dir)["names"]:
        try:
            ips.append(str(ipaddress.ip_address(n)))
        except ValueError:
            names.append(n)
    cn = [a.value for a in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)]
    if cn and not _is_ip(cn[0]):
        names = [cn[0].lower()] + [n for n in names if n != cn[0].lower()]
    if not names:
        raise tls.TLSError("The current certificate has no DNS names to renew.")
    return names, ips


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _install(data_dir: Path, d: Path, issued: Path) -> str:
    previous = (d / tls.CERT).read_bytes()
    cert = tls.install_cert(data_dir, issued)
    (d / PREVIOUS).write_bytes(previous)
    (d / STATE).unlink(missing_ok=True)
    issued.unlink(missing_ok=True)
    return (f"Installed the renewed certificate for {cert.subject.rfc4514_string()}, valid until "
            f"{cert.not_valid_after_utc:%Y-%m-%d}. Restart CloudTec to start using it.")


# --- renewal ----------------------------------------------------------------------

def renew(data_dir: Path, ca: str, template: str = DEFAULT_TEMPLATE, days: int = RENEW_DAYS,
          dry_run: bool = False, force: bool = False) -> tuple[str, str]:
    """Renew the certificate if it's due. Returns (status, message); status is one of
    "not-due", "issued", "pending" or "dry-run"."""
    d = tls.tls_dir(data_dir)
    state = _read_state(d)

    if state:   # a request the CA was holding for approval
        if dry_run:
            return "dry-run", (f"Would fetch request {state['request_id']} from {state['ca']}:\n"
                               f"    certreq {' '.join(retrieve_args(state['ca'], state['request_id'], d / ISSUED))}")
        out = _certreq(retrieve_args(state["ca"], state["request_id"], d / ISSUED))
        if not (d / ISSUED).exists():
            if "denied" in out.lower():
                (d / STATE).unlink()
                (d / tls.PENDING_KEY).unlink(missing_ok=True)
                raise tls.TLSError(f"The CA denied renewal request {state['request_id']}: {out}")
            msg = f"Request {state['request_id']} is still waiting for approval at the CA."
            _log(d, msg)
            return "pending", msg
        msg = _install(data_dir, d, d / ISSUED)
        _log(d, msg)
        return "issued", msg

    summary = tls.cert_summary(data_dir)
    if summary is None:
        raise tls.TLSError("There's no certificate to renew. Set one up first (python -m app.tls).")
    if summary["own_ca"]:
        raise tls.TLSError("This certificate comes from CloudTec's own CA, not a company CA. "
                           "Issue a new one with: python -m app.tls own-ca")
    left = days_left(data_dir)
    if left > days and not force:
        return "not-due", f"The certificate expires in {left} days; renewal starts {days} days before."
    if (d / tls.PENDING_KEY).exists():
        raise tls.TLSError("A certificate request made by hand is still waiting (pending-key.pem). Install its "
                           "certificate with option 2 of the setup, or delete pending-key.pem to let renewal start over.")

    names, ips = _current_names(data_dir)
    if dry_run:
        return "dry-run", (f"The certificate expires in {left} days. Would request a new one for "
                           f"{', '.join(names + ips)} and submit it with:\n"
                           f"    certreq {' '.join(submit_args(ca, template, d / tls.CSR, d / ISSUED))}")

    csr = tls.make_csr(data_dir, names, ips)
    (d / ISSUED).unlink(missing_ok=True)
    try:
        out = _certreq(submit_args(ca, template, csr, d / ISSUED))
    except tls.TLSError:
        (d / tls.PENDING_KEY).unlink(missing_ok=True)
        raise
    if (d / ISSUED).exists():
        msg = _install(data_dir, d, d / ISSUED)
        _log(d, msg)
        return "issued", msg
    request_id = _request_id(out)
    if request_id is None or "denied" in out.lower():
        (d / tls.PENDING_KEY).unlink(missing_ok=True)
        raise tls.TLSError(f"The CA didn't issue a certificate: {out}")
    tls._write(d / STATE, json.dumps({"request_id": request_id, "ca": ca,
                                      "submitted": dt.datetime.now(dt.timezone.utc).isoformat()}, indent=2).encode())
    msg = (f"Submitted renewal request {request_id}; the CA is holding it for approval. "
           f"The next run will pick up the certificate once it's approved.")
    _log(d, msg)
    return "pending", msg


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.renew",
                                description="Renew CloudTec's HTTPS certificate from a company CA with certreq.")
    p.add_argument("--data-dir", default=os.environ.get("CLOUDTEC_DATA_DIR", "data"))
    p.add_argument("--ca", required=True, help=r'the CA, as certreq -config expects it: "server\CA name"')
    p.add_argument("--template", default=DEFAULT_TEMPLATE, help=f"certificate template (default {DEFAULT_TEMPLATE})")
    p.add_argument("--days", type=int, default=RENEW_DAYS,
                   help=f"renew when the certificate expires within this many days (default {RENEW_DAYS})")
    p.add_argument("--dry-run", action="store_true", help="only say what would happen")
    p.add_argument("--force", action="store_true", help="renew now even if it isn't due")
    args = p.parse_args(argv)
    data_dir = Path(args.data_dir).resolve()
    try:
        _, msg = renew(data_dir, args.ca, args.template, args.days, args.dry_run, args.force)
    except (tls.TLSError, OSError, ValueError) as e:
        if not args.dry_run:
            _log(tls.tls_dir(data_dir), f"Error: {e}")
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
