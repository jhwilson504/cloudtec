"""Restarting a domain controller from the Domain controllers page."""

import sys
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import shell
from app.config import Settings
from app.store import Connection

ADMIN_PW = "correct-horse"
DCS = [{"name": "BTCPRODDC1", "dns_name": "btcproddc1.btcbahamas.com", "roles": ["PDC"], "site": "poinciana",
        "global_catalog": True, "read_only": False, "os": "", "os_version": "", "last_logon": None, "dn": ""}]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    # Any existing file works as "PowerShell" here: the restart itself is replaced below.
    settings = Settings(data_dir=tmp_path, secret_key=None, admin_password=ADMIN_PW, secure_cookies=False,
                        session_hours=1, powershell=sys.executable)
    calls = []

    class FakeAD:
        def domain_controllers(self):
            return [dict(d) for d in DCS]

    real_open = main.open_connector

    @contextmanager
    def fake_open(conn):
        if conn.demo:
            with real_open(conn) as c:
                yield c
        else:
            yield FakeAD()

    monkeypatch.setattr(main, "open_connector", fake_open)
    app = main.create_app(settings)
    admin = TestClient(app)
    admin.post("/api/login", json={"username": "admin", "password": ADMIN_PW})
    real = admin.post("/api/connections", json={"kind": "ad", "name": "btcbahamas.com", "password": "S3cret!",
                                                "config": {"host": "btcproddc1.btcbahamas.com", "base_dn": "DC=btcbahamas,DC=com",
                                                           "bind_user": "svc-kastec@btcbahamas.com"}}).json()["id"]
    return app, admin, real, calls, tmp_path


def restart(client, conn_id, name="BTCPRODDC1", confirm=None):
    return client.post(f"/api/ad/{conn_id}/domain-controllers/restart",
                       json={"name": name, "confirm": name if confirm is None else confirm})


def test_restart_runs_restart_computer_with_saved_account(setup, monkeypatch):
    app, admin, real, calls, tmp_path = setup
    monkeypatch.setattr(shell, "restart_domain_controller",
                        lambda exe, conn, host: calls.append((conn.config["bind_user"], conn.secret, host)) or (True, ""))
    r = restart(admin, real, confirm="btcproddc1")          # confirmation isn't case-sensitive
    assert r.status_code == 200 and "Restart sent to BTCPRODDC1" in r.json()["message"]
    assert calls == [("svc-kastec@btcbahamas.com", "S3cret!", "btcproddc1.btcbahamas.com")]
    entry = admin.get("/api/audit").json()[0]
    assert entry["action"] == "restarted domain controller" and entry["target"] == "BTCPRODDC1"
    assert "S3cret!" not in (tmp_path / "audit.log").read_text()


def test_failed_restart_is_reported_and_logged(setup, monkeypatch):
    app, admin, real, calls, _ = setup
    monkeypatch.setattr(shell, "restart_domain_controller", lambda exe, conn, host: (False, "Access is denied."))
    r = restart(admin, real)
    assert r.status_code == 502 and "Access is denied." in r.json()["detail"]
    assert admin.get("/api/audit").json()[0]["action"] == "domain controller restart failed"


def test_guards(setup, monkeypatch):
    app, admin, real, calls, _ = setup
    monkeypatch.setattr(shell, "restart_domain_controller", lambda *a: calls.append(a) or (True, ""))
    assert restart(admin, real, confirm="BTCPRODDC2").status_code == 422        # wrong confirmation
    assert restart(admin, real, name="FILESERVER01").status_code == 404         # not a DC: never restarted
    admin.post("/api/users", json={"username": "op", "role": "operator", "password": "Op3rator-pw"})
    op = TestClient(app)
    op.post("/api/login", json={"username": "op", "password": "Op3rator-pw"})
    assert restart(op, real).status_code == 403                                 # administrators only
    assert calls == []


def test_demo_dc_is_not_really_restarted(setup, monkeypatch):
    app, admin, real, calls, _ = setup
    monkeypatch.setattr(shell, "restart_domain_controller", lambda *a: calls.append(a) or (True, ""))
    demo = admin.post("/api/connections", json={"kind": "ad", "name": "Demo", "demo": True}).json()["id"]
    dc = admin.get(f"/api/ad/{demo}/domain-controllers").json()["dcs"][0]["name"]
    r = restart(admin, demo, name=dc)
    assert r.status_code == 200 and "demo" in r.json()["message"] and calls == []


def test_restart_script_keeps_password_off_the_command_line(monkeypatch):
    seen = {}

    def fake_run(args, **kw):
        seen["args"], seen["input"] = args, kw["input"]

        class R:
            stdout, stderr, returncode = "KASTEC-OK", "", 0
        return R()

    monkeypatch.setattr(shell.subprocess, "run", fake_run)
    conn = Connection(id="c", kind="ad", name="btc", config={"bind_user": "svc@btc"}, secret="it's $ecret",
                      demo=False, created_at="")
    assert shell.restart_domain_controller("powershell", conn, "dc1.btc") == (True, "")
    assert "ecret" not in " ".join(seen["args"])          # not visible in the process list
    assert "ecret" not in seen["input"]                   # and only base64-encoded on stdin
    script = shell.restart_computer_script(conn, "dc1.btc")
    assert "Restart-Computer -ComputerName 'dc1.btc'" in script and "'it''s $ecret'" in script
