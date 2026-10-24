"""HTTPS certificates, network mode and a real TLS server."""

import datetime as dt
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from app import tls
from app.run import server_options

NAMES, IPS = ["cloudtec.corp.example.com", "pc01", "localhost"], ["127.0.0.1", "10.0.0.5"]


def _verify(cert, issuer):
    issuer.public_key().verify(cert.signature, cert.tbs_certificate_bytes, padding.PKCS1v15(),
                               cert.signature_hash_algorithm)


def _sans(cert):
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return san.get_values_for_type(x509.DNSName), [str(i) for i in san.get_values_for_type(x509.IPAddress)]


def test_own_ca(tmp_path):
    cert = tls.make_own_ca_cert(tmp_path, NAMES, IPS)
    d = tmp_path / "tls"
    ca = x509.load_pem_x509_certificate((d / "ca.pem").read_bytes())
    _verify(cert, ca)
    assert _sans(cert) == (NAMES, IPS)
    assert cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value == \
        x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH])
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is True
    assert x509.load_der_x509_certificate((d / "CloudTec-CA.cer").read_bytes()) == ca
    if os.name != "nt":
        assert oct((d / "key.pem").stat().st_mode)[-3:] == "600"
        assert oct((d / "ca-key.pem").stat().st_mode)[-3:] == "600"
    # Re-issuing keeps the same CA, so colleagues don't have to trust a new one.
    again = tls.make_own_ca_cert(tmp_path, NAMES + ["new-name"], IPS)
    _verify(again, ca)
    s = tls.cert_summary(tmp_path)
    assert s["own_ca"] is True and "new-name" in s["names"]


def _company_ca():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Corp Issuing CA")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return key, cert


def _sign(csr, ca_key, ca_cert):
    now = dt.datetime.now(dt.timezone.utc)
    b = (x509.CertificateBuilder().subject_name(csr.subject).issuer_name(ca_cert.subject)
         .public_key(csr.public_key()).serial_number(2).not_valid_before(now)
         .not_valid_after(now + dt.timedelta(days=30)))
    for ext in csr.extensions:
        b = b.add_extension(ext.value, ext.critical)
    return b.sign(ca_key, hashes.SHA256())


def test_company_ca_request_and_install(tmp_path):
    csr_path = tls.make_csr(tmp_path, NAMES, IPS)
    csr = x509.load_pem_x509_csr(csr_path.read_bytes())
    assert csr.is_signature_valid and csr.subject.rfc4514_string() == "CN=cloudtec.corp.example.com"
    assert _sans(csr) == (NAMES, IPS)

    ca_key, ca_cert = _company_ca()
    issued = _sign(csr, ca_key, ca_cert)

    # A certificate for some other key is refused.
    other = tmp_path / "other.cer"
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.timezone.utc)
    other.write_bytes(x509.CertificateBuilder().subject_name(csr.subject).issuer_name(ca_cert.subject)
                      .public_key(other_key.public_key()).serial_number(3).not_valid_before(now)
                      .not_valid_after(now + dt.timedelta(days=1)).sign(ca_key, hashes.SHA256())
                      .public_bytes(serialization.Encoding.DER))
    with pytest.raises(tls.TLSError, match="keys don't match"):
        tls.install_cert(tmp_path, other)

    # The real one, as DER (what certreq saves), installs with its chain.
    issued_file = tmp_path / "cloudtec.cer"
    issued_file.write_bytes(issued.public_bytes(serialization.Encoding.DER))
    tls.install_cert(tmp_path, issued_file)
    chain = x509.load_pem_x509_certificates((tmp_path / "tls" / "cert.pem").read_bytes())
    assert chain == [issued]
    assert not (tmp_path / "tls" / "pending-key.pem").exists()
    assert tls.cert_summary(tmp_path)["own_ca"] is False
    with pytest.raises(tls.TLSError, match="no pending"):
        tls.install_cert(tmp_path, issued_file)


def test_import_pfx(tmp_path):
    ca_key, ca_cert = _company_ca()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pc01")])).sign(key, hashes.SHA256()))
    cert = _sign(csr, ca_key, ca_cert)
    pfx = tmp_path / "pc01.pfx"
    pfx.write_bytes(pkcs12.serialize_key_and_certificates(
        b"pc01", key, cert, [ca_cert], serialization.BestAvailableEncryption(b"pfx-pass")))

    with pytest.raises(tls.TLSError, match="wrong password"):
        tls.import_pfx(tmp_path, pfx, "nope")
    tls.import_pfx(tmp_path, pfx, "pfx-pass")
    assert x509.load_pem_x509_certificates((tmp_path / "tls" / "cert.pem").read_bytes()) == [cert, ca_cert]


def test_server_options(tmp_path):
    assert server_options(tmp_path) == {"host": "127.0.0.1", "port": 8000,
                                         "url": "http://localhost:8000", "tls": False}
    tls.set_network(tmp_path, True, 9443)
    with pytest.raises(SystemExit, match="no certificate"):
        server_options(tmp_path)
    tls.make_own_ca_cert(tmp_path, NAMES, IPS)
    opts = server_options(tmp_path)
    assert opts["host"] == "0.0.0.0" and opts["port"] == 9443 and opts["tls"] is True
    assert opts["url"] == "https://cloudtec.corp.example.com:9443" and Path(opts["ssl_certfile"]).exists()
    tls.set_network(tmp_path, False)
    assert server_options(tmp_path)["tls"] is False


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_https_server_end_to_end(tmp_path):
    tls.make_own_ca_cert(tmp_path, ["localhost"], ["127.0.0.1"])
    port = _free_port()
    tls.set_network(tmp_path, True, port)
    env = {**os.environ, "CLOUDTEC_DATA_DIR": str(tmp_path), "CLOUDTEC_ADMIN_PASSWORD": "Adm1n-pass"}
    proc = subprocess.Popen([sys.executable, "-m", "app.run"], cwd=Path(__file__).parent.parent, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        ca = str(tmp_path / "tls" / "ca.pem")
        with httpx.Client(base_url=f"https://localhost:{port}", verify=ca) as c:
            for _ in range(50):
                try:
                    c.get("/api/me")
                    break
                except httpx.ConnectError:
                    time.sleep(0.2)
            r = c.post("/api/login", json={"username": "admin", "password": "Adm1n-pass"})
            assert r.status_code == 200
            assert "secure" in r.headers["set-cookie"].lower()      # cookie only sent over HTTPS
            assert c.get("/api/me").json()["signed_in"] is True
        # Without trusting the CA, the certificate is rejected (as a browser would warn).
        with pytest.raises(httpx.ConnectError):
            httpx.get(f"https://localhost:{port}/api/me")
    finally:
        proc.terminate()
        out = proc.communicate(timeout=10)[0].decode()
    assert f"https://localhost:{port}" in out
