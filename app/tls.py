"""HTTPS certificates and network access for CloudTec.

Everything lives in <data dir>/tls:

    key.pem, cert.pem       the server's private key and certificate (plus chain) in use
    request.csr             a certificate request to give a company CA (with pending-key.pem)
    ca.pem, ca-key.pem      CloudTec's own small CA, only if that option was chosen
    CloudTec-CA.cer         the same CA certificate, ready to double-click / deploy by GPO

<data dir>/network.json switches CloudTec from "this PC only" (http://localhost:8000)
to listening on the network over HTTPS (port 8443 by default).

Run "python -m app.tls" for an interactive setup, or see "python -m app.tls --help".
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DEFAULT_PORT = 8443
KEY, CERT, CSR, PENDING_KEY = "key.pem", "cert.pem", "request.csr", "pending-key.pem"
CA_CERT, CA_KEY, CA_CER = "ca.pem", "ca-key.pem", "CloudTec-CA.cer"
FIREWALL_RULE = "CloudTec Center (HTTPS)"


class TLSError(Exception):
    pass


# --- files ------------------------------------------------------------------

def tls_dir(data_dir: Path) -> Path:
    d = data_dir / "tls"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(path: Path, data: bytes, private: bool = False) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if private else 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _key_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _new_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


# --- names ------------------------------------------------------------------

def default_names() -> tuple[list[str], list[str]]:
    """DNS names and IP addresses this PC is reachable at, for the certificate."""
    host = socket.gethostname()
    names = [host.lower()]
    fqdn = socket.getfqdn().lower()
    if fqdn and fqdn != names[0] and "." in fqdn:
        names.insert(0, fqdn)
    names.append("localhost")
    ips = ["127.0.0.1"]
    try:
        ips += [ip for ip in socket.gethostbyname_ex(host)[2] if not ip.startswith("127.")]
    except OSError:
        pass
    return list(dict.fromkeys(names)), list(dict.fromkeys(ips))


def _san(names: list[str], ips: list[str]) -> x509.SubjectAlternativeName:
    entries: list[x509.GeneralName] = [x509.DNSName(n) for n in names]
    entries += [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    return x509.SubjectAlternativeName(entries)


def _server_extensions(builder, names, ips):
    return (builder
            .add_extension(_san(names, ips), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=True, content_commitment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False))


# --- option 1: company CA -------------------------------------------------------

def make_csr(data_dir: Path, names: list[str], ips: list[str]) -> Path:
    """Create a key and a certificate request to give the company CA."""
    d = tls_dir(data_dir)
    key = _new_key()
    builder = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
    csr = _server_extensions(builder, names, ips).sign(key, hashes.SHA256())
    _write(d / PENDING_KEY, _key_pem(key), private=True)
    _write(d / CSR, csr.public_bytes(serialization.Encoding.PEM))
    return d / CSR


def _load_certs(data: bytes) -> list[x509.Certificate]:
    """Certificates from a .cer/.crt (PEM or DER) or .p7b file, in file order."""
    from cryptography.hazmat.primitives.serialization import pkcs7
    if b"-----BEGIN CERTIFICATE-----" in data:
        return x509.load_pem_x509_certificates(data)
    if b"-----BEGIN PKCS7-----" in data:
        return pkcs7.load_pem_pkcs7_certificates(data)
    try:
        return [x509.load_der_x509_certificate(data)]
    except ValueError:
        try:
            return pkcs7.load_der_pkcs7_certificates(data)
        except ValueError as e:
            raise TLSError("That file isn't a certificate CloudTec can read (.cer, .crt, .pem or .p7b).") from e


def _same_key(cert: x509.Certificate, key) -> bool:
    fmt = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return cert.public_key().public_bytes(*fmt) == key.public_key().public_bytes(*fmt)


def install_cert(data_dir: Path, cert_file: Path) -> x509.Certificate:
    """Install the certificate the company CA issued for our request."""
    d = tls_dir(data_dir)
    if not (d / PENDING_KEY).exists():
        raise TLSError("There's no pending certificate request. Create one first (option 1).")
    key = serialization.load_pem_private_key((d / PENDING_KEY).read_bytes(), password=None)
    certs = _load_certs(Path(cert_file).read_bytes())
    leaf = next((c for c in certs if _same_key(c, key)), None)
    if leaf is None:
        raise TLSError("That certificate wasn't issued for CloudTec's request (the keys don't match).")
    chain = [leaf] + [c for c in certs if c is not leaf]
    _write(d / CERT, b"".join(_pem(c) for c in chain))
    _write(d / KEY, _key_pem(key), private=True)
    (d / PENDING_KEY).unlink()
    return leaf


# --- option 2: existing .pfx ----------------------------------------------------

def import_pfx(data_dir: Path, pfx_file: Path, password: str | None) -> x509.Certificate:
    try:
        key, cert, extra = pkcs12.load_key_and_certificates(
            Path(pfx_file).read_bytes(), password.encode() if password else None)
    except ValueError as e:
        raise TLSError(f"Couldn't open the .pfx file (wrong password?): {e}") from e
    if key is None or cert is None:
        raise TLSError("The .pfx file must contain the certificate and its private key.")
    d = tls_dir(data_dir)
    _write(d / CERT, b"".join(_pem(c) for c in [cert, *(extra or [])]))
    _write(d / KEY, _key_pem(key), private=True)
    return cert


# --- option 3: CloudTec's own CA ------------------------------------------------

def make_own_ca_cert(data_dir: Path, names: list[str], ips: list[str], days: int = 825) -> x509.Certificate:
    """Create (or reuse) a small CloudTec CA, and issue this PC's certificate from it."""
    d = tls_dir(data_dir)
    now = dt.datetime.now(dt.timezone.utc)
    if (d / CA_CERT).exists() and (d / CA_KEY).exists():
        ca_key = serialization.load_pem_private_key((d / CA_KEY).read_bytes(), password=None)
        ca = x509.load_pem_x509_certificate((d / CA_CERT).read_bytes())
    else:
        ca_key = _new_key()
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"CloudTec CA ({names[0]})")])
        ca = (x509.CertificateBuilder()
              .subject_name(ca_name).issuer_name(ca_name)
              .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=3650))
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .add_extension(x509.KeyUsage(digital_signature=False, key_encipherment=False, content_commitment=False,
                                           data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                           crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
              .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
              .sign(ca_key, hashes.SHA256()))
        _write(d / CA_KEY, _key_pem(ca_key), private=True)
        _write(d / CA_CERT, _pem(ca))
        _write(d / CA_CER, ca.public_bytes(serialization.Encoding.DER))

    key = _new_key()
    builder = (x509.CertificateBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
               .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=days))
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False))
    cert = _server_extensions(builder, names, ips).sign(ca_key, hashes.SHA256())
    _write(d / CERT, _pem(cert) + _pem(ca))
    _write(d / KEY, _key_pem(key), private=True)
    return cert


# --- network switch ---------------------------------------------------------------

def read_network(data_dir: Path) -> dict:
    try:
        return json.loads((data_dir / "network.json").read_text())
    except (FileNotFoundError, ValueError):
        return {}


def set_network(data_dir: Path, enabled: bool, port: int = DEFAULT_PORT) -> None:
    _write(data_dir / "network.json", json.dumps({"enabled": enabled, "port": port}, indent=2).encode())


def cert_summary(data_dir: Path) -> dict | None:
    path = data_dir / "tls" / CERT
    if not path.exists():
        return None
    cert = x509.load_pem_x509_certificates(path.read_bytes())[0]
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = san.get_values_for_type(x509.DNSName) + [str(i) for i in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        names = []
    return {
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "names": names,
        "expires": cert.not_valid_after_utc.isoformat(),
        "own_ca": (data_dir / "tls" / CA_CERT).exists()
                  and x509.load_pem_x509_certificate((data_dir / "tls" / CA_CERT).read_bytes()).subject == cert.issuer,
    }


def open_firewall(port: int) -> tuple[bool, str]:
    """Allow inbound TCP <port> in Windows Firewall (needs Administrator)."""
    if os.name != "nt":
        return False, "Not Windows: open the port in this machine's firewall yourself."
    script = (f"Remove-NetFirewallRule -DisplayName '{FIREWALL_RULE}' -ErrorAction SilentlyContinue; "
              f"New-NetFirewallRule -DisplayName '{FIREWALL_RULE}' -Direction Inbound -Protocol TCP "
              f"-LocalPort {port} -Action Allow -Profile Domain,Private | Out-Null")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True)
    if r.returncode == 0:
        return True, f"Windows Firewall now allows TCP {port} (Domain and Private networks)."
    return False, ("Couldn't add the firewall rule (run this as Administrator). Command to run in an "
                   f"Administrator PowerShell:\n  New-NetFirewallRule -DisplayName '{FIREWALL_RULE}' "
                   f"-Direction Inbound -Protocol TCP -LocalPort {port} -Action Allow -Profile Domain,Private")


# --- interactive setup ------------------------------------------------------------

def _ask(prompt: str, default: str = "") -> str:
    answer = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return answer or default


def _finish(data_dir: Path, port: int) -> None:
    set_network(data_dir, True, port)
    ok, msg = open_firewall(port)
    print(("  OK  " if ok else "  !!  ") + msg)
    s = cert_summary(data_dir)
    print("\nDone. Start CloudTec with 'Start CloudTec', then colleagues can open:")
    for n in s["names"]:
        if n not in ("localhost", "127.0.0.1"):
            print(f"    https://{n}:{port}")
    if s["own_ca"]:
        print(f"\nBecause this uses CloudTec's own CA, each colleague's PC must trust it once:\n"
              f"  - deploy {data_dir / 'tls' / CA_CER} by Group Policy (Computer Configuration > Policies >\n"
              f"    Windows Settings > Security Settings > Public Key Policies > Trusted Root Certification Authorities), or\n"
              f"  - on each PC, double-click {CA_CER} > Install Certificate > Local Machine >\n"
              f"    'Trusted Root Certification Authorities'.")


def wizard(data_dir: Path) -> None:
    names, ips = default_names()
    print("CloudTec Center - network access over HTTPS\n")
    s = cert_summary(data_dir)
    if s:
        print(f"Current certificate: {s['subject']}, issued by {s['issuer']}, expires {s['expires'][:10]}")
        print(f"  for: {', '.join(s['names'])}\n")
    if (tls_dir(data_dir) / PENDING_KEY).exists():
        print("A certificate request is waiting for your CA's certificate.\n")
    print("How should CloudTec get its certificate?")
    print("  1  From my company's certificate authority (recommended - no browser warnings on domain PCs)")
    print("  2  Install the certificate my CA issued for the request from option 1")
    print("  3  Import an existing .pfx file (certificate with private key)")
    print("  4  Use CloudTec's own CA (quick; colleagues' PCs must trust it once)")
    print("  5  Turn network access off again (back to this PC only)")
    choice = _ask("\nChoose 1-5")

    if choice == "5":
        set_network(data_dir, False)
        print("Network access is off. CloudTec will only be reachable at http://localhost:8000 on this PC.")
        return
    if choice in ("1", "4"):
        print(f"\nThe certificate will be valid for: {', '.join(names + ips)}")
        extra = _ask("Extra names it should also be valid for, comma-separated (e.g. cloudtec.btcbahamas.com)")
        for n in (x.strip().lower() for x in extra.split(",") if x.strip()):
            try:
                ips.append(str(ipaddress.ip_address(n)))
            except ValueError:
                names.insert(0, n)
        names, ips = list(dict.fromkeys(names)), list(dict.fromkeys(ips))
    port = int(_ask("Port", str(read_network(data_dir).get("port", DEFAULT_PORT))) or DEFAULT_PORT)

    if choice == "1":
        csr = make_csr(data_dir, names, ips)
        print(f"\nCreated the request: {csr}")
        print("Give it to whoever runs your CA. With Active Directory Certificate Services you can submit it with:")
        print(f'    certreq -submit -attrib "CertificateTemplate:WebServer" "{csr}" "%USERPROFILE%\\Desktop\\cloudtec.cer"')
        print("When you have the issued certificate, run this again and choose 2.")
    elif choice == "2":
        path = _ask("Path to the issued certificate (.cer/.crt/.pem/.p7b)").strip('"')
        cert = install_cert(data_dir, Path(path))
        print(f"\nInstalled: {cert.subject.rfc4514_string()}, issued by {cert.issuer.rfc4514_string()}")
        _finish(data_dir, port)
    elif choice == "3":
        path = _ask("Path to the .pfx file").strip('"')
        import getpass
        cert = import_pfx(data_dir, Path(path), getpass.getpass(".pfx password (leave empty if none): ") or None)
        print(f"\nImported: {cert.subject.rfc4514_string()}")
        _finish(data_dir, port)
    elif choice == "4":
        make_own_ca_cert(data_dir, names, ips)
        _finish(data_dir, port)
    else:
        print("Nothing changed.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.tls", description="HTTPS and network access for CloudTec.")
    p.add_argument("--data-dir", default=os.environ.get("CLOUDTEC_DATA_DIR", "data"))
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("status", help="show the certificate and network setting")
    for name in ("csr", "own-ca"):
        sp = sub.add_parser(name)
        sp.add_argument("--name", action="append", default=[], help="extra DNS name or IP (repeatable)")
    sp = sub.add_parser("install", help="install the certificate issued for request.csr")
    sp.add_argument("cert")
    sp = sub.add_parser("pfx", help="import a .pfx")
    sp.add_argument("pfx")
    sp.add_argument("--password")
    sp = sub.add_parser("enable", help="listen on the network over HTTPS")
    sp.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub.add_parser("disable", help="back to this PC only")
    args = p.parse_args(argv)
    data_dir = Path(args.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    try:
        if args.cmd is None:
            wizard(data_dir)
        elif args.cmd == "status":
            print(json.dumps({"network": read_network(data_dir), "certificate": cert_summary(data_dir)}, indent=2))
        elif args.cmd in ("csr", "own-ca"):
            names, ips = default_names()
            for n in args.name:
                try:
                    ips.append(str(ipaddress.ip_address(n)))
                except ValueError:
                    names.insert(0, n.lower())
            if args.cmd == "csr":
                print(make_csr(data_dir, names, ips))
            else:
                print(make_own_ca_cert(data_dir, names, ips).subject.rfc4514_string())
        elif args.cmd == "install":
            print(install_cert(data_dir, Path(args.cert)).subject.rfc4514_string())
        elif args.cmd == "pfx":
            print(import_pfx(data_dir, Path(args.pfx), args.password).subject.rfc4514_string())
        elif args.cmd == "enable":
            if not cert_summary(data_dir):
                raise TLSError("Set up a certificate first.")
            set_network(data_dir, True, args.port)
            print(open_firewall(args.port)[1])
        elif args.cmd == "disable":
            set_network(data_dir, False)
    except (TLSError, OSError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
