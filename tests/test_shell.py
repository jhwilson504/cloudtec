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
        assert s["available"] is False and "computer CloudTec runs on" in s["reason"]
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


# --- connecting the session to saved connections -----------------------------

from app.shell import connect_script  # noqa: E402
from app.store import Connection  # noqa: E402

TRICKY_PASSWORD = "it's $ecret \"quoted\" `tick"

FAKE_AD = """
function Get-ADUser { [CmdletBinding()] param($Identity, $Server, $Credential)
  "server=$Server user=$($Credential.UserName) pw=$($Credential.GetNetworkCredential().Password)" }
"""
FAKE_VC = """
function Set-PowerCLIConfiguration { param($Scope, $InvalidCertificateAction, [switch]$Confirm) $global:CertAction = $InvalidCertificateAction }
function Connect-VIServer { param($Server, $Port, $Credential, $ErrorAction)
  if ($Credential.GetNetworkCredential().Password -ne 'right') { throw 'Cannot complete login due to an incorrect user name or password.' }
  $global:VIConnected = "$Server`:$Port as $($Credential.UserName)" }
"""


def conn(kind, secret=TRICKY_PASSWORD, **config):
    base = {"host": "dc01.corp.example.com", "bind_user": "svc@corp.example.com"} if kind == "ad" else \
           {"host": "vc.example.com", "username": "administrator@vsphere.local"}
    return Connection(id="c1", kind=kind, name="Corp $prod", config={**base, **config},
                      secret=secret, demo=False, created_at="")


def fake_module(root, name, code):
    (root / name).mkdir(parents=True)
    (root / name / f"{name}.psm1").write_text(code)


@needs_pwsh
def test_connect_ad_without_module_keeps_credential(session, tmp_path):
    r = session.run(connect_script(conn("ad")), log_as="[connect] Corp")
    assert "ActiveDirectory PowerShell module isn't installed" in r["output"]
    assert TRICKY_PASSWORD not in r["output"]
    assert session.run("$CloudTec['Corp $prod'].Credential.GetNetworkCredential().Password")["output"] == TRICKY_PASSWORD
    assert session.run("$CloudTec['Corp $prod'].Server")["output"] == "dc01.corp.example.com"
    assert session.run("Test-Path variable:__cred")["output"] == "False"
    history = (tmp_path / "history.log").read_text()
    assert "[connect] Corp" in history and "ecret" not in history


@needs_pwsh
def test_connect_ad_sets_defaults_for_ad_commands(session, tmp_path):
    mods = tmp_path / "modules"
    fake_module(mods, "ActiveDirectory", FAKE_AD)
    session.run(f"$env:PSModulePath = '{mods}' + [IO.Path]::PathSeparator + $env:PSModulePath")
    r = session.run(connect_script(conn("ad")))
    assert "Connected to Corp $prod (dc01.corp.example.com) as svc@corp.example.com." in r["output"]
    out = session.run("Get-ADUser jdoe")["output"]
    assert out == f"server=dc01.corp.example.com user=svc@corp.example.com pw={TRICKY_PASSWORD}"


@needs_pwsh
def test_connect_vcenter(session, tmp_path):
    mods = tmp_path / "modules"
    fake_module(mods, "VMware.VimAutomation.Core", FAKE_VC)
    session.run(f"$env:PSModulePath = '{mods}' + [IO.Path]::PathSeparator + $env:PSModulePath")

    r = session.run(connect_script(conn("vsphere", secret="right", verify_ssl=False)))
    assert "Connected to vCenter Corp $prod (vc.example.com)" in r["output"]
    assert session.run("$VIConnected")["output"] == "vc.example.com:443 as administrator@vsphere.local"
    assert session.run("$CertAction")["output"] == "Ignore"

    r = session.run(connect_script(conn("vsphere", secret="wrong")))
    assert "incorrect user name or password" in r["output"]
    assert session.run("Test-Path variable:__cred")["output"] == "False"


@needs_pwsh
def test_connect_through_api(tmp_path):
    with make_client(tmp_path, "on") as c:
        demo = c.post("/api/connections", json={"kind": "ad", "name": "Demo", "demo": True}).json()
        assert c.post("/api/shell/connect", json={"connection_id": demo["id"]}).status_code == 422
        real = c.post("/api/connections", json={"kind": "vsphere", "name": "VC", "password": "pw",
                                                "config": {"host": "vc", "username": "u"}}).json()
        r = c.post("/api/shell/connect", json={"connection_id": real["id"]})
        assert r.status_code == 200 and "PowerCLI isn't installed" in r.json()["output"]
        assert c.post("/api/shell/connect", json={"connection_id": "nope"}).status_code == 404


def test_connect_refused_from_remote_browser(tmp_path):
    with make_client(tmp_path, "local") as c:
        assert c.post("/api/shell/connect", json={"connection_id": "x"}).status_code == 403
