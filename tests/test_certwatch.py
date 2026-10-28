"""Certificate expiry alerts: the 30/21-day windows, what's watched, email and the API."""

import datetime as dt
import json
import socket
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from app import certwatch, tls
from app.config import Settings
from app.main import create_app

NOW = dt.datetime(2026, 10, 27, 12, 0, tzinfo=dt.timezone.utc)
PASSWORD = "correct-horse"


def _cert(name="host.example.com", days=60.0, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=400))
            .not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .sign(key, hashes.SHA256()))
    return key, cert


def _settings(tmp_path, **kw):
    return Settings(data_dir=tmp_path, secret_key=None, admin_password=PASSWORD, secure_cookies=False,
                    session_hours=1, cert_check_hours=0, **kw)


# --- the alert window ----------------------------------------------------------

@pytest.mark.parametrize("days, level, threshold", [
    (90, "ok", None),
    (31, "ok", None),
    (30.5, "warning", 30),     # 30 whole days left: the first alert
    (30, "warning", 30),
    (22, "warning", 30),
    (21.9, "critical", 21),    # the second alert
    (1, "critical", 21),
    (0.2, "critical", 21),
    (-0.1, "expired", 0),
    (-30, "expired", 0),
])
def test_alert_level(days, level, threshold):
    r = certwatch.alert_level(NOW + dt.timedelta(days=days), NOW)
    assert (r["level"], r["threshold"]) == (level, threshold)


def test_alert_level_custom_windows():
    assert certwatch.alert_level(NOW + dt.timedelta(days=50), NOW, windows=(60, 14))["threshold"] == 60
    assert certwatch.alert_level(NOW + dt.timedelta(days=10), NOW, windows=(60, 14))["level"] == "critical"


def test_own_certificate_days_left(tmp_path):
    assert certwatch.days_left(tmp_path) is None
    assert certwatch.needs_renewal(tmp_path) is False
    tls.make_own_ca_cert(tmp_path, ["pc01"], ["127.0.0.1"], days=25)
    assert certwatch.days_left(tmp_path) == 24       # minus the few minutes of back-dating
    assert certwatch.needs_renewal(tmp_path) is True
    assert certwatch.needs_renewal(tmp_path, days=20) is False
    assert certwatch.RENEW_DAYS == 30

    results = certwatch.check_all(tmp_path)
    assert results[0]["source"] == "cloudtec" and results[0]["level"] == "warning"
    assert results[0]["subject"] == "CN=pc01"


# --- what to watch ---------------------------------------------------------------

@pytest.mark.parametrize("target, expected", [
    ("btcbahamas.com", ("btcbahamas.com", 443)),
    ("jfkvcs.btcbahamas.com:8443", ("jfkvcs.btcbahamas.com", 8443)),
    ("10.255.215.121", ("10.255.215.121", 443)),
    ("10.0.16.124:443", ("10.0.16.124", 443)),
    ("[::1]:9443", ("::1", 9443)),
    ("[::1]", ("::1", 443)),
])
def test_split_host(target, expected):
    assert certwatch.split_host(target) == expected


def test_config_round_trip_and_validation(tmp_path):
    assert certwatch.read_config(tmp_path) == {"targets": [], "email_to": []}
    certwatch.save_config(tmp_path, [
        {"target": "btcme1.btcbahamas.com", "owners": ["javaughn.davis@btcbahamas.com"], "note": "mail"},
        "10.255.215.121",
        {"target": r"C:\certs\trailmail.cer"},
    ], ["sysops@example.com", " "])
    config = certwatch.read_config(tmp_path)
    assert [t["target"] for t in config["targets"]] == ["btcme1.btcbahamas.com", "10.255.215.121",
                                                         r"C:\certs\trailmail.cer"]
    assert config["targets"][0]["owners"] == ["javaughn.davis@btcbahamas.com"]
    assert config["email_to"] == ["sysops@example.com"]
    assert certwatch.is_file_target(r"C:\certs\trailmail.cer") and not certwatch.is_file_target("10.0.0.1")

    for bad in ({"target": ""}, {"target": "host:99999"}, {"target": "host:abc"},
                {"target": "host", "owners": ["not-an-email"]}):
        with pytest.raises(certwatch.WatchError):
            certwatch.save_config(tmp_path, [bad], [])
    with pytest.raises(certwatch.WatchError):
        certwatch.save_config(tmp_path, [], ["nobody"])


def test_file_target_uses_soonest_expiry(tmp_path):
    _, leaf = _cert("trailmail.example.com", days=20, now=NOW)
    _, intermediate = _cert("Issuing CA", days=900, now=NOW)
    path = tmp_path / "chain.pem"
    path.write_bytes(b"".join(c.public_bytes(serialization.Encoding.PEM) for c in (intermediate, leaf)))
    r = certwatch.check_target(certwatch.clean_target(str(path)), NOW)
    assert r["source"] == "file" and r["level"] == "critical" and r["days_left"] == 20
    assert r["subject"] == "CN=trailmail.example.com"

    der = tmp_path / "leaf.cer"
    der.write_bytes(leaf.public_bytes(serialization.Encoding.DER))
    assert certwatch.check_target({"target": str(der), "owners": [], "note": ""}, NOW)["days_left"] == 20

    missing = certwatch.check_target({"target": str(tmp_path / "gone.cer"), "owners": [], "note": ""}, NOW)
    assert missing["level"] == "error" and missing["error"]


def _tls_server(tmp_path, days):
    """A TLS server with an untrusted certificate, like an internal box or ESXi host."""
    key, cert = _cert("localhost", days=days)
    (tmp_path / "srv.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "srv.key").write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                                         serialization.PrivateFormat.PKCS8,
                                                         serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "srv.pem", tmp_path / "srv.key")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()

    def serve():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            try:
                with ctx.wrap_socket(conn, server_side=True) as s:
                    s.recv(1)
            except (OSError, ssl.SSLError):
                pass

    threading.Thread(target=serve, daemon=True).start()
    return sock, cert


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    """Tests reach their local servers directly, whatever proxy the test machine has."""
    monkeypatch.setenv("CLOUDTEC_CERT_PROXY", "off")


def _connect_proxy(log):
    """A minimal HTTP CONNECT proxy, like a company web proxy."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()

    def pipe(a, b):
        try:
            while data := a.recv(65536):
                b.sendall(data)
        except OSError:
            pass
        finally:
            for s in (a, b):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def serve():
        while True:
            try:
                client, _ = sock.accept()
            except OSError:
                return
            request = b""
            while b"\r\n\r\n" not in request:
                request += client.recv(4096)
            line = request.split(b"\r\n", 1)[0].decode()
            log.append(line)
            host, port = line.split()[1].rsplit(":", 1)
            if host == "blocked.example.com":
                client.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                client.close()
                continue
            upstream = socket.create_connection(("127.0.0.1", int(port)))
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
            threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    return sock


def test_host_target_through_proxy(tmp_path, monkeypatch):
    server, cert = _tls_server(tmp_path, days=25)
    log = []
    proxy = _connect_proxy(log)
    try:
        port = server.getsockname()[1]
        monkeypatch.setenv("CLOUDTEC_CERT_PROXY", f"127.0.0.1:{proxy.getsockname()[1]}")
        # The proxy resolves the name, as a company proxy does for internet sites.
        r = certwatch.check_target(certwatch.clean_target(f"btcme1.example.com:{port}"), timeout=3)
        assert r["error"] is None and r["level"] == "warning" and r["days_left"] == 24
        assert log == [f"CONNECT btcme1.example.com:{port} HTTP/1.1"]

        # A refusal from the proxy is reported along with the direct attempt.
        r = certwatch.check_target(certwatch.clean_target("blocked.example.com"), timeout=3)
        assert r["level"] == "error" and "via proxy" in r["error"] and "403" in r["error"] and "direct" in r["error"]
    finally:
        proxy.close()
        server.close()


def test_proxy_settings(monkeypatch):
    for name in ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLOUDTEC_CERT_PROXY", "proxy.corp:3128")
    assert certwatch.proxy_for("btcbahamas.com") == ("proxy.corp", 3128)
    monkeypatch.setenv("CLOUDTEC_CERT_PROXY", "http://proxy.corp")
    assert certwatch.proxy_for("btcbahamas.com") == ("proxy.corp", 8080)
    monkeypatch.setenv("CLOUDTEC_CERT_PROXY", "off")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:80")
    assert certwatch.proxy_for("btcbahamas.com") is None
    monkeypatch.delenv("CLOUDTEC_CERT_PROXY")
    monkeypatch.setenv("NO_PROXY", "10.0.0.1,.internal.example")
    assert certwatch.proxy_for("btcbahamas.com") == ("proxy.corp", 80)
    assert certwatch.proxy_for("10.0.0.1") is None
    assert certwatch.proxy_for("host.internal.example") is None


def test_host_target_reads_untrusted_certificate(tmp_path):
    sock, cert = _tls_server(tmp_path, days=10)
    try:
        port = sock.getsockname()[1]
        r = certwatch.check_target(certwatch.clean_target(f"127.0.0.1:{port}"))
        assert r["source"] == "host" and r["error"] is None
        assert r["level"] == "critical" and r["days_left"] == 9
        assert r["fingerprint"] == cert.fingerprint(hashes.SHA256()).hex()
    finally:
        sock.close()


def test_unreachable_host_is_reported_not_raised(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]           # nothing listening once closed
    r = certwatch.check_target(certwatch.clean_target(f"127.0.0.1:{port}"), timeout=1)
    assert r["level"] == "error" and r["error"]
    assert certwatch.alerts([r]) == []


# --- email -----------------------------------------------------------------------

class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, password):
        self.user = user

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(certwatch.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP.sent


def _result(target, days, cert=None, owners=()):
    cert = cert or _cert(target, days=days, now=NOW)[1]
    return {"target": target, "owners": list(owners), "note": "", "source": "host", "error": None,
            **certwatch._describe(cert, NOW)}


def test_notify_once_per_stage(tmp_path, smtp):
    settings = _settings(tmp_path, smtp_host="mail.example.com", alert_from="cloudtec@example.com")
    certwatch.save_config(tmp_path, [{"target": "btcme1.btcbahamas.com",
                                      "owners": ["javaughn.davis@btcbahamas.com"]}], ["sysops@example.com"])
    _, cert = _cert("btcme1.btcbahamas.com", days=29, now=NOW)
    healthy = _result("other.example.com", 200)

    first = certwatch.notify(settings, [_result("btcme1.btcbahamas.com", 29, cert, ["javaughn.davis@btcbahamas.com"]),
                                        healthy], NOW)
    assert [r["target"] for r in first] == ["btcme1.btcbahamas.com"]
    assert len(smtp) == 1
    msg = smtp[0]
    assert msg["To"] == "sysops@example.com, javaughn.davis@btcbahamas.com"
    assert msg["Subject"] == "Certificate expiring in 29 days: btcme1.btcbahamas.com"
    assert "btcme1.btcbahamas.com" in msg.get_content() and "29 days left" in msg.get_content()

    # Same stage on the next check: no second email.
    assert certwatch.notify(settings, [_result("btcme1.btcbahamas.com", 25, cert)], NOW) == []
    assert len(smtp) == 1

    # Crossing into the 21-day window, and then expiring, each email once more.
    later = NOW + dt.timedelta(days=9)
    r21 = {**_result("btcme1.btcbahamas.com", 0, cert), **certwatch.alert_level(cert.not_valid_after_utc, later)}
    assert r21["threshold"] == 21
    assert len(certwatch.notify(settings, [r21], later)) == 1
    gone = NOW + dt.timedelta(days=30)
    r0 = {**r21, **certwatch.alert_level(cert.not_valid_after_utc, gone)}
    assert len(certwatch.notify(settings, [r0], gone)) == 1
    assert smtp[-1]["Subject"] == "Certificate expired: btcme1.btcbahamas.com"
    assert len(smtp) == 3

    # A renewed certificate that's again close to expiry starts over.
    assert len(certwatch.notify(settings, [_result("btcme1.btcbahamas.com", 28)], NOW)) == 1


def test_notify_needs_mail_server_and_recipients(tmp_path, smtp):
    r = _result("a.example.com", 5)
    assert certwatch.notify(_settings(tmp_path), [r], NOW) == []          # no SMTP host
    assert certwatch.notify(_settings(tmp_path, smtp_host="mail"), [r], NOW) == []   # nobody to tell
    assert smtp == []


def test_notify_groups_several_certificates(tmp_path, smtp):
    settings = _settings(tmp_path, smtp_host="mail", smtp_port=587, smtp_starttls=True, smtp_user="svc")
    certwatch.save_config(tmp_path, [], ["sysops@example.com"])
    certwatch.notify(settings, [_result("a.example.com", 25), _result("b.example.com", 3)], NOW)
    assert len(smtp) == 1 and smtp[0]["Subject"] == "2 certificates need renewing (soonest in 3 days)"


# --- API and dashboard -----------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as c:
        assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
        yield c


def test_api_lists_alerts(tmp_path, client):
    r = client.get("/api/certificates").json()
    assert r["certificates"] == [] and r["alerts"] == [] and r["alert_days"] == [30, 21]
    assert r["config"] == {"targets": [], "email_to": []} and r["email"] is False

    tls.make_own_ca_cert(tmp_path, ["pc01"], ["127.0.0.1"], days=20)
    _, soon = _cert("trailmail.example.com", days=28)
    (tmp_path / "trail.pem").write_bytes(soon.public_bytes(serialization.Encoding.PEM))
    r = client.put("/api/certificates/config", json={
        "targets": [{"target": str(tmp_path / "trail.pem"), "owners": ["owner@example.com"], "note": "trail mail"}],
        "email_to": ["sysops@example.com"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [(a["target"], a["level"]) for a in body["alerts"]] == [
        ("CloudTec (this server)", "critical"), (str(tmp_path / "trail.pem"), "warning")]
    assert body["checked"]
    assert json.loads((tmp_path / "cert_watch.json").read_text())["email_to"] == ["sysops@example.com"]
    assert "certificates.config" in (tmp_path / "audit.log").read_text()

    assert client.put("/api/certificates/config", json={"targets": [{"target": "x:0"}]}).status_code == 400
    assert client.post("/api/certificates/check").status_code == 200
    # No mail server configured, so the test email is refused with a clear message.
    r = client.post("/api/certificates/test-email")
    assert r.status_code == 502 and "CLOUDTEC_SMTP_HOST" in r.json()["detail"]


def test_operators_see_alerts_but_not_settings(tmp_path, client):
    tls.make_own_ca_cert(tmp_path, ["pc01"], ["127.0.0.1"], days=10)
    assert client.post("/api/users", json={"username": "op1", "password": "operator-pass"}).status_code == 201
    client.post("/api/logout")
    assert client.post("/api/login", json={"username": "op1", "password": "operator-pass"}).status_code == 200
    r = client.get("/api/certificates").json()
    assert len(r["alerts"]) == 1 and "config" not in r
    assert client.put("/api/certificates/config", json={"targets": []}).status_code == 403
    assert client.post("/api/certificates/check").status_code == 403


def test_requires_sign_in(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as c:
        assert c.get("/api/certificates").status_code == 401


def test_command_line(tmp_path, capsys):
    assert certwatch.main(["--data-dir", str(tmp_path)]) == 0
    tls.make_own_ca_cert(tmp_path, ["pc01"], ["127.0.0.1"], days=15)
    assert certwatch.main(["--data-dir", str(tmp_path)]) == 2
    assert "CloudTec (this server): expires" in capsys.readouterr().out


# --- public certificate logs -----------------------------------------------------

GOOGLE = "C=US, O=Google Trust Services, CN=WE1"
GODADDY = ('C=US, ST=Arizona, L=Scottsdale, O="GoDaddy.com, Inc.", OU=http://certs.godaddy.com/repository/, '
           "CN=Go Daddy Secure Certificate Authority - G2")

# What crt.sh listed for btcme1.btcbahamas.com on 28 Oct 2026: Cloudflare's 90-day edge certificates
# (Google Trust Services) and one GoDaddy certificate on the origin.
CRTSH = {
    "btcme1.btcbahamas.com": [
        {"id": 29387057278, "serial_number": "a1", "issuer_name": GOOGLE, "common_name": "btcme1.btcbahamas.com",
         "name_value": "btcme1.btcbahamas.com", "not_before": "2026-09-12T00:00:00", "not_after": "2026-12-11T00:00:00"},
        {"id": 27966123403, "serial_number": "a2", "issuer_name": GOOGLE, "common_name": "btcme1.btcbahamas.com",
         "name_value": "btcme1.btcbahamas.com", "not_before": "2026-07-15T00:00:00", "not_after": "2026-10-13T00:00:00"},
        {"id": 26324174928, "serial_number": "gd1", "issuer_name": GODADDY, "common_name": "btcme1.btcbahamas.com",
         "name_value": "btcme1.btcbahamas.com\nwww.btcme1.btcbahamas.com",
         "not_before": "2026-05-13T00:00:00", "not_after": "2026-11-27T23:59:59"},
        {"id": 1, "serial_number": "old", "issuer_name": "C=US, O=Old CA, CN=Old", "common_name": "btcme1.btcbahamas.com",
         "name_value": "btcme1.btcbahamas.com", "not_before": "2024-01-01T00:00:00", "not_after": "2025-01-01T00:00:00"},
    ],
    "*.btcbahamas.com": [
        {"id": 5, "serial_number": "w1", "issuer_name": GOOGLE, "common_name": "*.btcbahamas.com",
         "name_value": "*.btcbahamas.com\nbtcbahamas.com", "not_before": "2026-10-01T00:00:00",
         "not_after": "2026-12-30T00:00:00"},
        {"id": 6, "serial_number": "x1", "issuer_name": GODADDY, "common_name": "other.btcbahamas.com",
         "name_value": "other.btcbahamas.com", "not_before": "2026-01-01T00:00:00", "not_after": "2026-11-01T00:00:00"},
    ],
    "btcbahamas.com": [
        {"id": 7, "serial_number": "b1", "issuer_name": GODADDY, "common_name": "btcbahamas.com",
         "name_value": "btcbahamas.com\nwww.btcbahamas.com", "not_before": "2025-10-13T00:00:00",
         "not_after": "2026-10-23T23:59:59"},
        {"id": 8, "serial_number": "b2", "issuer_name": GODADDY, "common_name": "btcbahamas.com",
         "name_value": "btcbahamas.com\nwww.btcbahamas.com", "not_before": "2026-10-13T00:00:00",
         "not_after": "2027-04-29T23:59:59"},
    ],
}


CT_NOW = dt.datetime(2026, 10, 28, 13, 0, tzinfo=dt.timezone.utc)   # when joey checked


@pytest.fixture
def crtsh(monkeypatch):
    queries = []

    def fake(query, timeout):
        queries.append(query)
        return CRTSH.get(query, [])

    monkeypatch.setattr(certwatch, "_ct_entries", fake)
    return queries


def test_ct_reports_the_certificate_that_lapses_first(crtsh):
    r = certwatch.ct_lookup("btcme1.btcbahamas.com", CT_NOW)
    # Cloudflare's newest runs to 11 Dec, the GoDaddy one only to 27 Nov: that's the one to renew.
    assert r["expires"].startswith("2026-11-27") and "GoDaddy" in r["issuer"]
    assert r["level"] == "warning" and r["days_left"] == 30
    assert r["fingerprint"] == "ct:gd1" and r["via"] == "certificate log" and r["ct_source"] == "crt.sh"
    # The wildcard for the parent domain is checked too; the unrelated name it returned is ignored.
    assert crtsh == ["btcme1.btcbahamas.com", "*.btcbahamas.com"]


def test_ct_ignores_replaced_and_retired_certificates(crtsh):
    # The October GoDaddy certificate was replaced by the April 2027 one.
    r = certwatch.ct_lookup("btcbahamas.com", NOW)
    assert r["expires"].startswith("2027-04-29") and r["level"] == "ok"
    with pytest.raises(certwatch.WatchError, match="No current certificate"):
        certwatch.ct_lookup("nothing.example.com", NOW)


def test_ct_target_and_fallback(crtsh, monkeypatch):
    t = certwatch.clean_target("CT:BTCME1.btcbahamas.com")
    assert t["target"] == "ct:btcme1.btcbahamas.com"
    r = certwatch.check_target(t, CT_NOW)
    assert r["source"] == "ct" and r["level"] == "warning"
    for bad in ("ct:10.255.215.121", "ct:server01", "ct:host.local"):
        with pytest.raises(certwatch.WatchError, match="public names"):
            certwatch.clean_target(bad)

    # When the server can't be reached (as from the BTC office), public names fall back to the logs.
    def blocked(*a, **kw):
        raise ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host")

    monkeypatch.setattr(certwatch, "fetch_host_cert", blocked)
    r = certwatch.check_target(certwatch.clean_target("btcme1.btcbahamas.com"), CT_NOW)
    assert r["source"] == "host" and r["via"] == "certificate log" and r["error"] is None
    assert r["expires"].startswith("2026-11-27") and "forcibly closed" in r["direct_error"]
    # Internal addresses aren't in the logs, so the connection error is reported.
    r = certwatch.check_target(certwatch.clean_target("10.255.215.121"), NOW)
    assert r["level"] == "error" and "forcibly closed" in r["error"]


def test_ct_falls_back_to_certspotter_when_crtsh_times_out(monkeypatch):
    def slow(query, timeout):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(certwatch, "_ct_entries", slow)
    issued = [  # what Cert Spotter returns: unexpired issuances only
        {"id": "1", "cert_sha256": "cf", "dns_names": ["btcme1.btcbahamas.com"], "issuer": {"name": GOOGLE},
         "not_after": "2026-12-11T00:00:00Z"},
        {"id": "2", "cert_sha256": "gd", "dns_names": ["btcme1.btcbahamas.com", "www.btcme1.btcbahamas.com"],
         "issuer": {"name": GODADDY}, "not_after": "2026-11-27T23:59:59Z"},
    ]

    class Resp:
        def __init__(self, url):
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return json.dumps(issued).encode()

    urls = []
    monkeypatch.setattr(certwatch.urllib.request, "urlopen", lambda req, timeout: urls.append(req.full_url) or Resp(req.full_url))
    r = certwatch.ct_lookup("btcme1.btcbahamas.com", CT_NOW)
    assert r["ct_source"] == "Cert Spotter" and "GoDaddy" in r["issuer"] and r["days_left"] == 30
    assert "domain=btcme1.btcbahamas.com" in urls[0] and "match_wildcards=true" in urls[0]

    monkeypatch.setattr(certwatch, "_certspotter_entries", lambda name, timeout: (_ for _ in ()).throw(OSError("blocked")))
    with pytest.raises(OSError, match="crt.sh: The read operation timed out; Cert Spotter: blocked"):
        certwatch.ct_lookup("btcme1.btcbahamas.com", CT_NOW)
