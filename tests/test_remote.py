"""Getting into servers: RDP files, SSH terminals (against a fake plink), and guest commands (fake PowerCLI)."""

import base64
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import netmap, remote
from app.config import Settings
from app.main import create_app
from app.shell import find_powershell
from app.store import Connection

FAKE_PLINK = str(Path(__file__).parent / "fakes" / "fake_plink.py")
KEY = "SHA256:Zm9vYmFyYmF6cXV4cXV1eGNvcmdlZ3JhdWx0Z2FycGx5"
PASSWORD = "correct horse battery"
PWSH = find_powershell()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, "find_plink", lambda: FAKE_PLINK)
    c = TestClient(create_app(Settings(data_dir=tmp_path, secret_key=None, admin_password=PASSWORD,
                                       secure_cookies=False, session_hours=1, shell="off")))
    c.post("/api/login", json={"password": PASSWORD})
    return c


def test_rdp_file(client, tmp_path):
    r = client.get("/api/connect/rdp", params={"host": "BTCPRODDC2.btcbahamas.com", "name": "BTCPRODDC2"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-rdp")
    assert 'filename="BTCPRODDC2.rdp"' in r.headers["content-disposition"]
    assert "full address:s:BTCPRODDC2.btcbahamas.com\r\n" in r.text and "prompt for credentials:i:1" in r.text
    assert client.get("/api/connect/rdp", params={"host": "bad host; calc.exe"}).status_code == 422
    assert "RDP: downloaded a connection file" in (tmp_path / "audit.log").read_text()


def read_until(client, sid, text, since=0, tries=40):
    seen = b""
    for _ in range(tries):
        r = client.get(f"/api/ssh/{sid}/read", params={"since": since}).json()
        seen += base64.b64decode(r["data"])
        since = r["next"]
        if text.encode() in seen or r["closed"]:
            return seen, since, r["closed"]
    raise AssertionError(f"never saw {text!r}: {seen!r}")


def test_ssh_trust_sign_in_type_and_close(client, tmp_path):
    body = {"host": "newhost.lab", "username": "jovan", "password": "secret"}
    first = client.post("/api/ssh/open", json=body)
    assert first.status_code == 409 and first.json()["fingerprint"] == KEY and first.json()["changed"] is False
    opened = client.post("/api/ssh/open", json={**body, "trust_fingerprint": KEY})
    assert opened.status_code == 200, opened.text
    sid = opened.json()["sid"]
    seen, since, _ = read_until(client, sid, "jovan@newhost.lab")
    assert b"Welcome to Ubuntu" in seen
    assert client.post(f"/api/ssh/{sid}/write", json={"data": "uptime\r"}).json() == {"ok": True}
    seen, since, _ = read_until(client, sid, "you typed: uptime", since)
    # The key is remembered, and the password file is gone.
    assert '"newhost.lab": "' + KEY in (tmp_path / "ssh_hosts.json").read_text()
    assert not list((tmp_path / "tmp").glob("ssh-*"))
    # A second session to the same server goes straight in.
    again = client.post("/api/ssh/open", json=body)
    assert again.status_code == 200
    client.delete(f"/api/ssh/{again.json()['sid']}")
    # Typing exit ends the session.
    client.post(f"/api/ssh/{sid}/write", json={"data": "exit\r"})
    _, since, closed = read_until(client, sid, "logout", since)
    for _ in range(20):          # the shell prints "logout", then plink exits
        if closed:
            break
        r = client.get(f"/api/ssh/{sid}/read", params={"since": since}).json()
        since, closed = r["next"], r["closed"]
    assert closed
    client.delete(f"/api/ssh/{sid}")
    assert client.get(f"/api/ssh/{sid}/read").status_code == 410
    log = (tmp_path / "audit.log").read_text()
    assert "SSH: trusted a server key" in log and "SSH: opened a session" in log and "secret" not in log


def test_ssh_wrong_password_and_changed_key(client, tmp_path):
    (tmp_path / "ssh_hosts.json").write_text('{"newhost.lab": "' + KEY + '"}')
    r = client.post("/api/ssh/open", json={"host": "newhost.lab", "username": "jovan", "password": "nope"})
    assert r.status_code == 502 and "refused the username or password" in r.json()["detail"]
    (tmp_path / "ssh_hosts.json").write_text('{"newhost.lab": "SHA256:b2xkb2xkb2xkb2xkb2xkb2xkb2xkb2xkb2xkb2xkb2xk"}')
    r = client.post("/api/ssh/open", json={"host": "newhost.lab", "username": "jovan", "password": "secret"})
    assert r.status_code == 409 and r.json()["changed"] is True
    assert client.post("/api/ssh/open", json={"host": "bad host!", "username": "x", "password": "y"}).status_code == 502


def test_sessions_belong_to_who_opened_them(client):
    sid = client.post("/api/ssh/open", json={"host": "newhost.lab", "username": "jovan", "password": "secret",
                                             "trust_fingerprint": KEY}).json()["sid"]
    client.post("/api/users", json={"username": "op", "password": "operator-pass-1", "role": "admin"})
    client.post("/api/logout")
    client.post("/api/login", json={"username": "op", "password": "operator-pass-1"})
    assert client.get(f"/api/ssh/{sid}/read").status_code == 410            # someone else's session
    assert client.post(f"/api/ssh/{sid}/write", json={"data": "x"}).status_code == 410


def test_operators_cannot_ssh_or_run_guest_commands(client):
    client.post("/api/users", json={"username": "op", "password": "operator-pass-1", "role": "operator"})
    client.post("/api/logout")
    client.post("/api/login", json={"username": "op", "password": "operator-pass-1"})
    assert client.post("/api/ssh/open", json={"host": "h", "username": "u", "password": "p"}).status_code == 403
    assert client.post("/api/vsphere/x/vms/vm-1/guest/run", json={"command": "id", "username": "u", "password": "p"}).status_code == 403
    assert client.get("/api/connect/rdp", params={"host": "srv1"}).status_code == 200   # an RDP file is just an address


def test_demo_guest_command(client):
    v = client.post("/api/connections", json={"kind": "vsphere", "name": "Demo vCenter", "demo": True}).json()
    vm = next(x for x in client.get(f"/api/vsphere/{v['id']}/vms").json() if x["power_state"] == "POWERED_ON")
    r = client.post(f"/api/vsphere/{v['id']}/vms/{vm['id']}/guest/run",
                    json={"command": "hostname", "username": "root", "password": "pw", "script_type": "Bash"}).json()
    assert r == {"output": vm["name"].lower() + "\n", "exit_code": 0}


@pytest.mark.skipif(not PWSH, reason="PowerShell isn't installed here")
def test_guest_command_through_fake_powercli(monkeypatch):
    monkeypatch.setenv("PSModulePath", f"{Path(__file__).parent / 'fake_powercli'}{os.pathsep}{os.environ.get('PSModulePath', '')}")
    vc = Connection(id="vc1", kind="vsphere", name="vc", demo=False, created_at="", secret="right",
                    config={"host": "jfkvcs.btcbahamas.com", "username": "u", "verify_ssl": False})
    maps = netmap.NetMaps(PWSH)
    try:
        r = maps.guest_run(vc, "vm-1", "df -h\nuptime", "root", "guestpw", "Bash")
        assert r == {"output": "ran on BTCPRODDC1 as root with Bash:\ndf -h\nuptime\nlast line ünïcode", "exit_code": 0}
        assert maps.guest_run(vc, "vm-1", "will fail", "root", "guestpw", "Bash")["exit_code"] == 3
        with pytest.raises(netmap.NetMapError, match="Failed to authenticate"):
            maps.guest_run(vc, "vm-1", "id", "root", "wrong", "Bash")
        with pytest.raises(netmap.NetMapError, match="Invalid VM"):
            maps.guest_run(vc, "vm-1; Remove-VM", "id", "root", "guestpw", "Bash")
    finally:
        maps.close()
