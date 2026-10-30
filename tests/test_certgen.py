import base64
import datetime as dt

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs7, pkcs12
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from app import certgen
from app.config import Settings
from app.main import create_app

PASSWORD = "correct-horse"


def make_ca():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Issuing CA")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return key, cert


def sign(csr_pem: str, ca, days=397):
    ca_key, ca_cert = ca
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    now = dt.datetime.now(dt.timezone.utc)
    return (x509.CertificateBuilder().subject_name(csr.subject).issuer_name(ca_cert.subject)
            .public_key(csr.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + dt.timedelta(days=days))
            .sign(ca_key, hashes.SHA256()))


def pem(cert):
    return cert.public_bytes(serialization.Encoding.PEM)


def test_request_has_names_details_and_a_private_key(tmp_path):
    r = certgen.create_request(tmp_path, ["BTCME1.btcbahamas.com", "autodiscover.btcbahamas.com", "10.0.0.5",
                                          "btcme1.btcbahamas.com"],
                               {"organization": "BTC", "locality": "Nassau", "country": "bs", "bogus": "x"})
    csr = x509.load_pem_x509_csr(r["csr"].encode())
    assert csr.is_signature_valid
    assert csr.subject.rfc4514_string() == "C=BS,L=Nassau,O=BTC,CN=btcme1.btcbahamas.com"
    san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["btcme1.btcbahamas.com", "autodiscover.btcbahamas.com"]
    assert [str(i) for i in san.get_values_for_type(x509.IPAddress)] == ["10.0.0.5"]
    assert r["names"] == ["btcme1.btcbahamas.com", "autodiscover.btcbahamas.com", "10.0.0.5"]
    assert r["has_certificate"] is False and r["file_stem"] == "btcme1.btcbahamas.com"
    key = serialization.load_pem_private_key((tmp_path / "cert-requests" / r["id"] / "private.key").read_bytes(), None)
    assert key.public_key().public_numbers() == csr.public_key().public_numbers()
    assert [x["id"] for x in certgen.list_requests(tmp_path)] == [r["id"]]


def test_wildcard_file_name(tmp_path):
    r = certgen.create_request(tmp_path, ["*.btcbahamas.com"])
    assert r["common_name"] == "*.btcbahamas.com" and r["id"].startswith("wildcard.btcbahamas.com-")


@pytest.mark.parametrize("names,details,size,msg", [
    ([], {}, 2048, "at least one"),
    (["bad name.com"], {}, 2048, "isn't a valid"),
    (["a.com"], {"country": "Bahamas"}, 2048, "two-letter"),
    (["a.com"], {}, 1024, "2048 or 4096"),
])
def test_bad_requests(tmp_path, names, details, size, msg):
    with pytest.raises(certgen.CertGenError, match=msg):
        certgen.create_request(tmp_path, names, details, size)


@pytest.mark.parametrize("encode", ["pem_chain", "der", "p7b_pem", "p7b_der"])
def test_signed_certificate_becomes_a_pfx(tmp_path, encode):
    ca = make_ca()
    r = certgen.create_request(tmp_path, ["esb.btcbahamas.com"])
    leaf = sign(r["csr"], ca)
    files = {
        "pem_chain": [pem(leaf), pem(ca[1])],
        "der": [leaf.public_bytes(serialization.Encoding.DER), pem(ca[1])],
        "p7b_pem": [pkcs7.serialize_certificates([ca[1], leaf], serialization.Encoding.PEM)],
        "p7b_der": [pkcs7.serialize_certificates([leaf, ca[1]], serialization.Encoding.DER)],
    }[encode]
    out = certgen.add_certificate(tmp_path, r["id"], files, "s3cret")
    assert out["has_certificate"] and out["has_pfx"] and out["issuer"] == "Test Issuing CA"
    data, name, _ = certgen.download(tmp_path, r["id"], "pfx")
    assert name == "esb.btcbahamas.com.pfx"
    key, cert, extra = pkcs12.load_key_and_certificates(data, b"s3cret")
    assert cert == leaf and extra == [ca[1]]
    assert key.public_key().public_numbers() == leaf.public_key().public_numbers()
    crt, crt_name, _ = certgen.download(tmp_path, r["id"], "crt")
    assert crt_name == "esb.btcbahamas.com.crt" and x509.load_pem_x509_certificates(crt) == [leaf, ca[1]]


def test_certificate_for_another_key_is_refused(tmp_path):
    ca = make_ca()
    mine = certgen.create_request(tmp_path, ["a.btcbahamas.com"])
    other = certgen.create_request(tmp_path, ["b.btcbahamas.com"])
    with pytest.raises(certgen.CertGenError, match="key doesn't match"):
        certgen.add_certificate(tmp_path, mine["id"], [pem(sign(other["csr"], ca))], "s3cret")
    with pytest.raises(certgen.CertGenError, match="No certificate"):
        certgen.add_certificate(tmp_path, mine["id"], [b"hello"], "s3cret")
    with pytest.raises(certgen.CertGenError, match="password"):
        certgen.add_certificate(tmp_path, mine["id"], [pem(sign(mine["csr"], ca))], "")
    with pytest.raises(certgen.CertGenError, match="Add the signed"):
        certgen.download(tmp_path, mine["id"], "pfx")


def test_unknown_or_unsafe_ids(tmp_path):
    for bad in ["../../etc", "nope-20260101-000000", ""]:
        with pytest.raises(certgen.CertGenError, match="wasn't found"):
            certgen.get_request(tmp_path, bad)


@pytest.fixture
def client(tmp_path):
    settings = Settings(data_dir=tmp_path, secret_key=None, admin_password=PASSWORD,
                        secure_cookies=False, session_hours=1)
    with TestClient(create_app(settings)) as c:
        assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
        yield c


def test_api_flow(client, tmp_path):
    assert client.get("/api/cert-requests").json() == []
    r = client.post("/api/cert-requests", json={"names": ["btcme1.btcbahamas.com"], "details": {"country": "BS"}})
    assert r.status_code == 201, r.text
    req = r.json()
    assert client.post("/api/cert-requests", json={"names": ["bad name"]}).status_code == 400

    csr = client.get(f"/api/cert-requests/{req['id']}/csr")
    assert csr.status_code == 200 and csr.content.decode() == req["csr"]
    assert 'filename="btcme1.btcbahamas.com.csr"' in csr.headers["content-disposition"]
    assert client.get(f"/api/cert-requests/{req['id']}/pfx").status_code == 400
    assert client.get(f"/api/cert-requests/{req['id']}/other").status_code == 422

    leaf = sign(req["csr"], make_ca())
    r = client.post(f"/api/cert-requests/{req['id']}/certificate",
                    json={"files": [base64.b64encode(pem(leaf)).decode()], "pfx_password": "s3cret"})
    assert r.status_code == 200, r.text
    assert r.json()["has_pfx"]
    pfx = client.get(f"/api/cert-requests/{req['id']}/pfx")
    assert pkcs12.load_key_and_certificates(pfx.content, b"s3cret")[1] == leaf
    audit = (tmp_path / "audit.log").read_text()
    assert "cert-request.create" in audit and "cert-request.download-pfx" in audit
    assert "BEGIN" not in audit

    assert client.delete(f"/api/cert-requests/{req['id']}").status_code == 200
    assert client.get("/api/cert-requests").json() == []
    assert client.delete(f"/api/cert-requests/{req['id']}").status_code == 400


def test_operators_cannot_make_requests(client):
    assert client.post("/api/users", json={"username": "op", "password": "operator-pass-1",
                                           "role": "operator"}).status_code in (200, 201)
    client.post("/api/logout")
    assert client.post("/api/login", json={"username": "op", "password": "operator-pass-1"}).status_code == 200
    assert client.get("/api/cert-requests").status_code == 403
    assert client.post("/api/cert-requests", json={"names": ["a.com"]}).status_code == 403
