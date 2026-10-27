"""Automatic certificate renewal with certreq (certreq itself is faked)."""

import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app import renew, tls

NAMES, IPS = ["cloudtec.corp.example.com", "pc01", "localhost"], ["127.0.0.1", "10.0.0.5"]
CA = r"ca01.corp.example.com\Corp Issuing CA"


@pytest.fixture(scope="module")
def company_ca():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Corp Issuing CA")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return key, cert


def _sign(csr, company_ca, days):
    ca_key, ca_cert = company_ca
    now = dt.datetime.now(dt.timezone.utc)
    b = (x509.CertificateBuilder().subject_name(csr.subject).issuer_name(ca_cert.subject)
         .public_key(csr.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(now - dt.timedelta(days=30)).not_valid_after(now + dt.timedelta(days=days)))
    for ext in csr.extensions:
        b = b.add_extension(ext.value, ext.critical)
    return b.sign(ca_key, hashes.SHA256())


def _installed(data_dir, company_ca, days_left):
    """A company-CA certificate in use that expires in <days_left> days."""
    csr = x509.load_pem_x509_csr(tls.make_csr(data_dir, NAMES, IPS).read_bytes())
    issued = data_dir / "issued.cer"
    issued.write_bytes(_sign(csr, company_ca, days_left).public_bytes(serialization.Encoding.DER))
    tls.install_cert(data_dir, issued)
    return x509.load_pem_x509_certificates((data_dir / "tls" / "cert.pem").read_bytes())[0]


class FakeCertreq:
    """Stands in for subprocess.run(["certreq", ...]): signs the request like an AD CS issuing CA would,
    or holds it for approval when pending=True."""

    def __init__(self, company_ca, pending=False, days=365):
        self.company_ca, self.pending, self.days, self.calls = company_ca, pending, days, []
        self.requests: dict[str, Path] = {}
        self.approved, self.denied = False, False

    def __call__(self, cmd, **kwargs):
        assert cmd[0] == "certreq"
        self.calls.append(cmd[1:])
        verb, out = cmd[1], Path(cmd[-1])
        if verb == "-submit":
            csr_path = Path(cmd[-2])
            if self.pending:
                self.requests["42"] = csr_path
                return self._result("RequestId: 42\nRequestId: \"42\"\n"
                                    "Certificate request is pending: Taken Under Submission (0)")
            self._issue(csr_path, out)
            return self._result("RequestId: 7\nRequestId: \"7\"\nCertificate retrieved(Issued) Issued")
        if verb == "-retrieve":
            if self.denied:
                return self._result("Certificate not issued (Denied) Denied by Policy Module")
            if not self.approved:
                return self._result("Certificate request is pending: Taken Under Submission (0)")
            self._issue(self.requests[cmd[-2]], out)
            return self._result("Certificate retrieved(Issued) Issued")
        raise AssertionError(cmd)

    def _issue(self, csr_path, out):
        csr = x509.load_pem_x509_csr(csr_path.read_bytes())
        out.write_bytes(_sign(csr, self.company_ca, self.days).public_bytes(serialization.Encoding.DER))

    @staticmethod
    def _result(stdout, code=0):
        return subprocess.CompletedProcess([], code, stdout, "")


@pytest.fixture
def certreq(monkeypatch, company_ca):
    fake = FakeCertreq(company_ca)
    monkeypatch.setattr(renew.subprocess, "run", fake)
    return fake


def _sans(cert):
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return san.get_values_for_type(x509.DNSName), [str(i) for i in san.get_values_for_type(x509.IPAddress)]


def _key(data_dir):
    return (data_dir / "tls" / "key.pem").read_bytes()


def test_not_due(tmp_path, company_ca, certreq):
    _installed(tmp_path, company_ca, 60)
    status, msg = renew.renew(tmp_path, CA)
    assert status == "not-due" and "59 days" in msg
    assert certreq.calls == []


def test_renews_within_window(tmp_path, company_ca, certreq):
    old = _installed(tmp_path, company_ca, 20)
    old_key = _key(tmp_path)
    status, msg = renew.renew(tmp_path, CA)
    assert status == "issued" and "Restart CloudTec" in msg

    d = tmp_path / "tls"
    assert certreq.calls == [["-submit", "-q", "-f", "-config", CA, "-attrib", "CertificateTemplate:WebServer",
                              str(d / "request.csr"), str(d / "renewed.cer")]]
    new = x509.load_pem_x509_certificates((d / "cert.pem").read_bytes())[0]
    assert new.serial_number != old.serial_number
    assert new.not_valid_after_utc > old.not_valid_after_utc + dt.timedelta(days=300)
    assert new.subject == old.subject and _sans(new) == (NAMES, IPS)    # same names as before
    assert _key(tmp_path) != old_key                                     # with a fresh key
    assert x509.load_pem_x509_certificates((d / "cert-previous.pem").read_bytes())[0] == old
    assert not (d / "pending-key.pem").exists() and not (d / "renewed.cer").exists()
    assert "Installed the renewed certificate" in (d / "renewal.log").read_text()
    # Once renewed, it isn't due any more.
    assert renew.renew(tmp_path, CA)[0] == "not-due"
    assert len(certreq.calls) == 1


def test_expired_and_forced(tmp_path, company_ca, certreq):
    _installed(tmp_path, company_ca, -3)
    assert renew.renew(tmp_path, CA)[0] == "issued"
    assert renew.renew(tmp_path, CA, days=10)[0] == "not-due"
    assert renew.renew(tmp_path, CA, days=10, force=True)[0] == "issued"
    assert len(certreq.calls) == 2


def test_custom_window_and_template(tmp_path, company_ca, certreq):
    _installed(tmp_path, company_ca, 40)
    assert renew.renew(tmp_path, CA)[0] == "not-due"
    assert renew.renew(tmp_path, CA, template="CloudTecWeb", days=45)[0] == "issued"
    assert "CertificateTemplate:CloudTecWeb" in certreq.calls[0]


def test_dry_run_changes_nothing(tmp_path, company_ca, certreq):
    _installed(tmp_path, company_ca, 10)
    d = tmp_path / "tls"
    before = {p.name: p.read_bytes() for p in d.iterdir()}
    status, msg = renew.renew(tmp_path, CA, dry_run=True)
    assert status == "dry-run"
    assert "expires in 9 days" in msg and "cloudtec.corp.example.com" in msg and "certreq -submit" in msg
    assert certreq.calls == []
    assert {p.name: p.read_bytes() for p in d.iterdir()} == before


def test_pending_then_retrieved(tmp_path, company_ca, certreq):
    certreq.pending = True
    old = _installed(tmp_path, company_ca, 15)
    d = tmp_path / "tls"

    status, msg = renew.renew(tmp_path, CA)
    assert status == "pending" and "42" in msg
    assert json.loads((d / "renewal.json").read_text())["request_id"] == "42"
    assert (d / "pending-key.pem").exists()
    assert x509.load_pem_x509_certificates((d / "cert.pem").read_bytes())[0] == old   # still the old one

    # Not approved yet: asks again, keeps waiting, doesn't make a new request.
    assert renew.renew(tmp_path, CA)[0] == "pending"
    assert certreq.calls[-1] == ["-retrieve", "-q", "-f", "-config", CA, "42", str(d / "renewed.cer")]
    assert "certreq -retrieve" in renew.renew(tmp_path, CA, dry_run=True)[1]

    certreq.approved = True
    assert renew.renew(tmp_path, CA)[0] == "issued"
    new = x509.load_pem_x509_certificates((d / "cert.pem").read_bytes())[0]
    assert new != old and _sans(new) == (NAMES, IPS)
    assert not (d / "renewal.json").exists() and not (d / "pending-key.pem").exists()
    assert [c[0] for c in certreq.calls] == ["-submit", "-retrieve", "-retrieve"]


def test_pending_denied_starts_over(tmp_path, company_ca, certreq):
    certreq.pending = True
    _installed(tmp_path, company_ca, 15)
    renew.renew(tmp_path, CA)
    certreq.denied = True
    with pytest.raises(tls.TLSError, match="denied"):
        renew.renew(tmp_path, CA)
    d = tmp_path / "tls"
    assert not (d / "renewal.json").exists() and not (d / "pending-key.pem").exists()
    certreq.pending = certreq.denied = False
    assert renew.renew(tmp_path, CA)[0] == "issued"


def test_certreq_failures(tmp_path, company_ca, monkeypatch):
    old = _installed(tmp_path, company_ca, 5)
    d = tmp_path / "tls"

    monkeypatch.setattr(renew.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0x80094012, "", "Certificate Request Processor: The requested certificate template is not supported"))
    with pytest.raises(tls.TLSError, match="template is not supported"):
        renew.renew(tmp_path, CA)

    monkeypatch.setattr(renew.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, "Certificate not issued (Denied) Denied by Policy Module", ""))
    with pytest.raises(tls.TLSError, match="didn't issue"):
        renew.renew(tmp_path, CA)

    def missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(renew.subprocess, "run", missing)
    with pytest.raises(tls.TLSError, match="comes with Windows"):
        renew.renew(tmp_path, CA)

    # Each failure leaves the certificate in use alone and lets the next run start over.
    assert x509.load_pem_x509_certificates((d / "cert.pem").read_bytes())[0] == old
    assert not (d / "pending-key.pem").exists()


def test_refuses(tmp_path, company_ca, certreq):
    with pytest.raises(tls.TLSError, match="no certificate"):
        renew.renew(tmp_path, CA)

    tls.make_own_ca_cert(tmp_path, NAMES, IPS, days=5)
    with pytest.raises(tls.TLSError, match="own CA"):
        renew.renew(tmp_path, CA)

    other = tmp_path / "other"
    _installed(other, company_ca, 5)
    tls.make_csr(other, NAMES, IPS)     # someone started a request by hand
    with pytest.raises(tls.TLSError, match="made by hand"):
        renew.renew(other, CA)
    assert certreq.calls == []


def test_cli(tmp_path, company_ca, certreq, capsys):
    _installed(tmp_path, company_ca, 10)
    assert renew.main(["--data-dir", str(tmp_path), "--ca", CA, "--dry-run"]) == 0
    assert "Would request" in capsys.readouterr().out and certreq.calls == []
    assert renew.main(["--data-dir", str(tmp_path), "--ca", CA]) == 0
    assert "Installed" in capsys.readouterr().out
    assert renew.main(["--data-dir", str(tmp_path / "empty"), "--ca", CA]) == 1
    assert "no certificate" in capsys.readouterr().err
