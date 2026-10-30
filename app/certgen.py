"""Certificate requests: make a key and CSR to send to a CA, then turn the signed certificate into a PFX.

Nothing is installed anywhere. Each request lives in <data dir>/cert-requests/<id>/:

    meta.json          the names and details it was made for
    request.csr        the certificate signing request, to give GoDaddy or another CA
    private.key        the private key that goes with it (kept here, never sent to the CA)
    certificate.pem    the signed certificate plus its chain, once it has been added
    certificate.pfx    the same with the key, password protected, to import on Windows / IIS / Exchange
"""

from __future__ import annotations

import base64
import datetime as dt
import ipaddress
import json
import re
import shutil
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs7, pkcs12
from cryptography.x509.oid import NameOID

from .tls import _key_pem, _pem, _san, _server_extensions, _write

DIR = "cert-requests"
META, CSR, KEY, CERT, PFX = "meta.json", "request.csr", "private.key", "certificate.pem", "certificate.pfx"
KEY_SIZES = (2048, 4096)
DNS_NAME = re.compile(r"^(\*\.)?([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
ID = re.compile(r"^[a-z0-9.-]{1,80}-\d{8}-\d{6}$")
# Subject fields a CA may ask for, in the order they appear in the request.
SUBJECT = (("organization", NameOID.ORGANIZATION_NAME), ("unit", NameOID.ORGANIZATIONAL_UNIT_NAME),
           ("locality", NameOID.LOCALITY_NAME), ("state", NameOID.STATE_OR_PROVINCE_NAME),
           ("country", NameOID.COUNTRY_NAME))


class CertGenError(Exception):
    pass


def _root(data_dir: Path) -> Path:
    d = data_dir / DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _folder(data_dir: Path, req_id: str) -> Path:
    if not ID.match(req_id or "") or not (data_dir / DIR / req_id / META).is_file():
        raise CertGenError("That certificate request wasn't found.")
    return data_dir / DIR / req_id


def file_stem(common_name: str) -> str:
    """A file name for the certificate: *.btcbahamas.com -> wildcard.btcbahamas.com."""
    return re.sub(r"[^a-z0-9.-]", "", common_name.lower().replace("*", "wildcard")) or "certificate"


def split_names(names: list[str]) -> tuple[list[str], list[str]]:
    """Check host names and IPs, returning (dns names, ips) without duplicates, in order."""
    dns: list[str] = []
    ips: list[str] = []
    for raw in names:
        name = raw.strip().lower().rstrip(".")
        if not name:
            continue
        try:
            ip = str(ipaddress.ip_address(name))
            if ip not in ips:
                ips.append(ip)
            continue
        except ValueError:
            pass
        if len(name) > 253 or not DNS_NAME.match(name):
            raise CertGenError(f"'{raw.strip()}' isn't a valid host name or IP address.")
        if name not in dns:
            dns.append(name)
    if not dns and not ips:
        raise CertGenError("Enter at least one host name, for example btcme1.btcbahamas.com.")
    return dns, ips


def create_request(data_dir: Path, names: list[str], details: dict | None = None, key_size: int = 2048,
                   now: dt.datetime | None = None) -> dict:
    """Make a new private key and a CSR for the names. The first name is the common name."""
    if key_size not in KEY_SIZES:
        raise CertGenError("The key size must be 2048 or 4096.")
    dns, ips = split_names(names)
    details = {k: str(v).strip() for k, v in (details or {}).items() if k in dict(SUBJECT) and str(v).strip()}
    if "country" in details:
        details["country"] = details["country"].upper()
        if not re.fullmatch(r"[A-Z]{2}", details["country"]):
            raise CertGenError("The country must be a two-letter code, for example BS.")
    for key, value in details.items():
        if len(value) > 64:
            raise CertGenError(f"The {key} is too long (64 characters at most).")
    common_name = (dns or ips)[0]
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, common_name)]
    attrs += [x509.NameAttribute(oid, details[field]) for field, oid in SUBJECT if field in details]

    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    builder = x509.CertificateSigningRequestBuilder().subject_name(x509.Name(attrs))
    csr = _server_extensions(builder, dns, ips).sign(key, hashes.SHA256())

    now = now or dt.datetime.now(dt.timezone.utc)
    req_id = f"{file_stem(common_name)}-{now:%Y%m%d-%H%M%S}"
    folder = _root(data_dir) / req_id
    if folder.exists():
        raise CertGenError("A request for this name was made a moment ago. Try again in a second.")
    folder.mkdir()
    meta = {"id": req_id, "common_name": common_name, "names": dns + ips, "details": details,
            "key_size": key_size, "created": now.isoformat(timespec="seconds")}
    _write(folder / KEY, _key_pem(key), private=True)
    _write(folder / CSR, csr.public_bytes(serialization.Encoding.PEM))
    _write(folder / META, json.dumps(meta, indent=2).encode())
    return describe(folder)


def describe(folder: Path) -> dict:
    meta = json.loads((folder / META).read_text(encoding="utf-8"))
    out = {**meta, "csr": (folder / CSR).read_text(encoding="ascii"), "file_stem": file_stem(meta["common_name"]),
           "has_certificate": (folder / CERT).is_file(), "has_pfx": (folder / PFX).is_file()}
    if out["has_certificate"]:
        cert = x509.load_pem_x509_certificate((folder / CERT).read_bytes())
        out["expires"] = cert.not_valid_after_utc.isoformat()
        out["issuer"] = _cn(cert.issuer)
    return out


def list_requests(data_dir: Path) -> list[dict]:
    root = data_dir / DIR
    if not root.is_dir():
        return []
    found = [describe(d) for d in root.iterdir() if d.is_dir() and ID.match(d.name) and (d / META).is_file()]
    return sorted(found, key=lambda r: r["created"], reverse=True)


def get_request(data_dir: Path, req_id: str) -> dict:
    return describe(_folder(data_dir, req_id))


def delete_request(data_dir: Path, req_id: str) -> None:
    shutil.rmtree(_folder(data_dir, req_id))


def _cn(name: x509.Name) -> str:
    attrs = name.get_attributes_for_oid(NameOID.COMMON_NAME) or name.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
    return str(attrs[0].value) if attrs else name.rfc4514_string()


def parse_certificates(data: bytes) -> list[x509.Certificate]:
    """Certificates from what a CA hands back: PEM (one or many), DER .cer/.crt, or a .p7b / .p7c bundle."""
    text = data.lstrip()
    if b"-----BEGIN" in text:
        certs = []
        if b"-----BEGIN PKCS7-----" in text:
            certs += pkcs7.load_pem_pkcs7_certificates(text[text.index(b"-----BEGIN PKCS7-----"):])
        if b"-----BEGIN CERTIFICATE-----" in text:
            certs += x509.load_pem_x509_certificates(text[text.index(b"-----BEGIN CERTIFICATE-----"):])
        return certs
    for load in (lambda b: [x509.load_der_x509_certificate(b)], pkcs7.load_der_pkcs7_certificates):
        try:
            return load(data)
        except ValueError:
            continue
    return []


def add_certificate(data_dir: Path, req_id: str, files: list[bytes], pfx_password: str) -> dict:
    """Store the signed certificate the CA sent back (plus any chain) and make a PFX with the key."""
    folder = _folder(data_dir, req_id)
    if len(pfx_password) < 4:
        raise CertGenError("Choose a PFX password of at least 4 characters. You'll need it when importing.")
    certs: list[x509.Certificate] = []
    for data in files:
        try:
            certs += parse_certificates(data)
        except ValueError as e:
            raise CertGenError(f"One of the files isn't a certificate KasTec can read ({e}).") from e
    if not certs:
        raise CertGenError("No certificate was found. Add the .crt, .cer, .pem or .p7b file the CA sent.")

    key = serialization.load_pem_private_key((folder / KEY).read_bytes(), password=None)
    public = key.public_key().public_numbers()
    leaf = next((c for c in certs if c.public_key().public_numbers() == public), None)
    if leaf is None:
        raise CertGenError("None of these certificates was made from this request (the key doesn't match). "
                           "Check that the CA was given this request's CSR.")
    chain, seen = [], {leaf.fingerprint(hashes.SHA256())}
    for c in certs:
        fp = c.fingerprint(hashes.SHA256())
        if fp not in seen:
            seen.add(fp)
            chain.append(c)

    name = file_stem(json.loads((folder / META).read_text(encoding="utf-8"))["common_name"])
    pfx = pkcs12.serialize_key_and_certificates(
        name.encode(), key, leaf, chain or None,
        # AES-256 with SHA-256 works on Windows Server 2019 / Windows 10 1709 and newer.
        serialization.PrivateFormat.PKCS12.encryption_builder().kdf_rounds(50000)
        .key_cert_algorithm(pkcs12.PBES.PBESv2SHA256AndAES256CBC).hmac_hash(hashes.SHA256())
        .build(pfx_password.encode()))
    _write(folder / CERT, b"".join(_pem(c) for c in [leaf, *chain]))
    _write(folder / PFX, pfx, private=True)
    return describe(folder)


def download(data_dir: Path, req_id: str, kind: str) -> tuple[bytes, str, str]:
    """(content, file name, media type) for kind csr / key / crt / pfx."""
    folder = _folder(data_dir, req_id)
    stem = file_stem(json.loads((folder / META).read_text(encoding="utf-8"))["common_name"])
    files = {"csr": (CSR, ".csr", "application/pkcs10"), "key": (KEY, ".key", "application/x-pem-file"),
             "crt": (CERT, ".crt", "application/x-x509-ca-cert"), "pfx": (PFX, ".pfx", "application/x-pkcs12")}
    if kind not in files:
        raise CertGenError("Unknown file.")
    source, ext, media = files[kind]
    if not (folder / source).is_file():
        raise CertGenError("Add the signed certificate first.")
    return (folder / source).read_bytes(), stem + ext, media


def decode_upload(value: str) -> bytes:
    """Files arrive from the browser as base64."""
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as e:
        raise CertGenError("A file couldn't be read.") from e
