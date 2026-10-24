"""The PowerShell page. Session tests need PowerShell (pwsh or powershell) on PATH."""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.shell import PowerShellSession, find_powershell

PWSH = find_powershell()
needs_pwsh = pytest.mark.skipif(PWSH is None, reason="PowerShell is not installed")
PASSWORD = "correct-horse"


def make_client(tmp_path, shell):
    settings = Settings(data_dir=tmp_path, secret_key=None, admin_password=PASSWORD,
                        secure_cookies=False, session_hours=1, shell=shell)
    c = TestClient(create_app(settings))
    c.post("/api/login", json={"password": PASSWORD})
    return c


@pytest.fixture
def session(tmp_path):
    s = PowerShellSession(PWSH, tmp_path, tmp_path / "history.log")
    yield s
    s.close()


@needs_pwsh
def test_state_carries_over_between_commands(session, tmp_path):
    assert session.run("$x = 41")["output"] == ""
    assert session.run("$x + 1")["output"] == "42"
    session.run("function Hi { 'hi there' }")
    assert session.run("Hi")["output"] == "hi there"
    (tmp_path / "sub").mkdir()
    assert session.run("Set-Location sub")["cwd"].endswith("sub")


@needs_pwsh
def test_errors_multiline_and_unicode(session):
    r = session.run("Get-Item ./does-not-exist")
    assert "does not exist" in r["output"]
    assert "boom" in session.run("throw 'boom'")["output"]
    assert session.run('foreach ($i in 1..3) {\n  "line $i"\n}')["output"].splitlines() == ["line 1", "line 2", "line 3"]
    assert session.run("'ünïcode ✓'")["output"] == "ünïcode ✓"
    assert "\x1b" not in session.run("Get-Date")["output"]


@needs_pwsh
def test_timeout_restarts_session(session):
    session.run("$keep = 1")
    r = session.run("Start-Sleep 5", timeout=1)
    assert r["error"] and "longer than 1 seconds" in r["output"]
    assert session.run("'alive'")["output"] == "alive"
    assert session.run("$keep")["output"] == ""   # fresh session


@needs_pwsh
def test_history_is_logged(session, tmp_path):
    session.run("Get-Date")
    assert "Get-Date" in (tmp_path / "history.log").read_text()


def test_local_mode_refuses_remote_browsers(tmp_path):
    # TestClient's client address is "testclient", i.e. not this computer.
    with make_client(tmp_path, "local") as c:
        s = c.get("/api/shell").json()
        assert s["available"] is False and "localhost" in s["reason"]
        assert c.post("/api/shell/run", json={"command": "Get-Date"}).status_code == 403


def test_off_mode_refuses(tmp_path):
    with make_client(tmp_path, "off") as c:
        assert c.post("/api/shell/run", json={"command": "Get-Date"}).status_code == 403


def test_shell_requires_login(tmp_path):
    settings = Settings(data_dir=tmp_path, secret_key=None, admin_password=PASSWORD,
                        secure_cookies=False, session_hours=1, shell="on")
    with TestClient(create_app(settings)) as c:
        assert c.post("/api/shell/run", json={"command": "Get-Date"}).status_code == 401


@needs_pwsh
def test_run_through_api(tmp_path):
    with make_client(tmp_path, "on") as c:
        assert c.get("/api/shell").json()["available"] is True
        r = c.post("/api/shell/run", json={"command": "1 + 1"}).json()
        assert r["output"] == "2" and r["cwd"]
        assert c.post("/api/shell/reset").json()["ok"] is True
