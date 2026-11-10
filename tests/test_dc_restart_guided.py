"""The guided domain controller restart: checks before, a guarded restart, checks after."""

import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import dcrestart, shell
from app.config import Settings
from app.shell import find_powershell
from app.store import Connection

ADMIN_PW = "correct-horse"
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
DCS = [
    {"name": "BTCPRODDC1", "dns_name": "btcproddc1.btcbahamas.com", "site": "poinciana", "global_catalog": True,
     "read_only": False, "roles": ["PDC", "RID", "Infrastructure", "Domain naming", "Schema"]},
    {"name": "BTCPRODDC2", "dns_name": "btcproddc2.btcbahamas.com", "site": "poinciana", "global_catalog": True,
     "read_only": False, "roles": []},
    {"name": "CWPDDCSBTCW01", "dns_name": "cwpddcsbtcw01.btcbahamas.com", "site": "curacao", "global_catalog": True,
     "read_only": False, "roles": []},
]


# --- the checks themselves -------------------------------------------------------------

def test_partner_check():
    up = lambda host, port: True          # noqa: E731
    down = lambda host, port: False       # noqa: E731
    assert dcrestart.partner_check(DCS[0], DCS, up, False)["status"] == "ok"
    fail = dcrestart.partner_check(DCS[0], DCS, down, False)
    assert fail["status"] == "fail" and "BTCPRODDC2" in fail["detail"] and "no working DC" in fail["detail"]
    alone = dcrestart.partner_check(DCS[2], DCS, up, False)            # only DC in curacao
    assert alone["status"] == "warn" and "only DC in curacao" in alone["detail"]


def test_replication_check():
    fresh = (NOW - timedelta(minutes=20)).isoformat()
    ok = [{"partner": "BTCPRODDC2", "last_success": fresh, "last_result": 0, "failures": 0}]
    assert dcrestart.replication_check(ok, "BTCPRODDC1", NOW)["status"] == "ok"
    failing = [{"partner": "BTCPRODDC2", "last_success": fresh, "last_result": 8453, "failures": 4}]
    r = dcrestart.replication_check(failing, "BTCPRODDC1", NOW)
    assert r["status"] == "fail" and "4 failures in a row" in r["detail"] and "repadmin" in r["command"]
    stale = [{"partner": "CWPDDCSBTCW01", "last_success": (NOW - timedelta(hours=5)).isoformat(), "last_result": 0, "failures": 0}]
    assert dcrestart.replication_check(stale, "BTCPRODDC1", NOW)["status"] == "warn"
    assert dcrestart.replication_check("ADWS not reachable", "BTCPRODDC1")["status"] == "unknown"
    assert dcrestart.replication_check(None, "BTCPRODDC1")["status"] == "unknown"


def test_fsmo_check_gives_move_and_move_back_commands():
    r = dcrestart.fsmo_check(DCS[0], DCS)
    assert r["status"] == "warn"
    move, back = r["command"].split("\n")
    assert move == ("Move-ADDirectoryServerOperationMasterRole -Identity BTCPRODDC2 -OperationMasterRole "
                    "PDCEmulator, RIDMaster, InfrastructureMaster, DomainNamingMaster, SchemaMaster")
    assert back.startswith("Move-ADDirectoryServerOperationMasterRole -Identity BTCPRODDC1 ")
    assert dcrestart.fsmo_check(DCS[1], DCS)["status"] == "ok"


def test_back_checks():
    ports_up = {"389": True, "636": True}
    dse = {"is_synchronized": True, "gc_ready": True, "current_time": NOW.isoformat(), "dns_name": "dc1"}
    assert all(c["status"] == "ok" for c in dcrestart.back_checks(ports_up, dse, None, NOW))
    syncing = dcrestart.back_checks(ports_up, {**dse, "is_synchronized": False}, None, NOW)
    assert {c["id"]: c["status"] for c in syncing}["synchronized"] == "fail"
    skewed = dcrestart.back_checks(ports_up, {**dse, "current_time": (NOW + timedelta(minutes=3)).isoformat()}, None, NOW)
    assert {c["id"]: c["status"] for c in skewed}["time"] == "warn"
    down = dcrestart.back_checks({"389": False, "636": False}, None, "timed out", NOW)
    assert [c["status"] for c in down] == ["fail", "fail"]


@pytest.mark.skipif(find_powershell() is None, reason="PowerShell is not installed")
def test_replication_status_through_powershell(tmp_path, monkeypatch):
    """The real script, with a stand-in ActiveDirectory module that records what it was asked."""
    mod = tmp_path / "ActiveDirectory"
    mod.mkdir()
    (mod / "ActiveDirectory.psm1").write_text(r"""
function Get-ADReplicationPartnerMetadata { param($Target, $Scope, $Credential, $ErrorAction)
  if ($Credential.GetNetworkCredential().Password -ne "it's `$ecret") { throw 'Access is denied.' }
  [pscustomobject]@{ Partner = "CN=NTDS Settings,CN=BTCPRODDC2,CN=Servers,CN=poinciana,CN=Sites,CN=Configuration,DC=btc,DC=com";
    LastReplicationSuccess = [datetime]'2026-11-10T11:40:00Z'; LastReplicationResult = 0; ConsecutiveReplicationFailures = 0 }
  [pscustomobject]@{ Partner = "CN=NTDS Settings,CN=CWPDDCSBTCW01,CN=Servers,CN=curacao,CN=Sites,CN=Configuration,DC=btc,DC=com";
    LastReplicationSuccess = $null; LastReplicationResult = 1722; ConsecutiveReplicationFailures = 6 }
}""")
    monkeypatch.setenv("PSModulePath", str(tmp_path))
    conn = Connection(id="c", kind="ad", name="btc", config={"bind_user": "svc@btc"}, secret="it's $ecret",
                      demo=False, created_at="")
    ok, partners = shell.replication_status(find_powershell(), conn, "btcproddc1.btc.com")
    assert ok, partners
    assert [(p["partner"], p["failures"], p["last_result"]) for p in partners] == [("BTCPRODDC2", 0, 0), ("CWPDDCSBTCW01", 6, 1722)]
    assert partners[0]["last_success"].startswith("2026-11-10T11:40:00") and partners[1]["last_success"] is None
    wrong = Connection(id="c", kind="ad", name="btc", config={"bind_user": "svc@btc"}, secret="nope", demo=False, created_at="")
    assert shell.replication_status(find_powershell(), wrong, "dc") == (False, "Access is denied.")


# --- through the API -----------------------------------------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch):
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
    calls = []
    monkeypatch.setattr(shell, "restart_domain_controller", lambda exe, conn, host: calls.append(host) or (True, ""))
    monkeypatch.setattr(shell, "replication_status", lambda exe, conn, host: (True, [
        {"partner": "BTCPRODDC2", "last_success": datetime.now(timezone.utc).isoformat(), "last_result": 0, "failures": 0}]))
    app = main.create_app(Settings(data_dir=tmp_path, secret_key=None, admin_password=ADMIN_PW, secure_cookies=False,
                                   session_hours=1, powershell=sys.executable))
    c = TestClient(app)
    c.post("/api/login", json={"username": "admin", "password": ADMIN_PW})
    real = c.post("/api/connections", json={"kind": "ad", "name": "btcbahamas.com", "password": "S3cret!",
                                            "config": {"host": "btcproddc1.btcbahamas.com", "base_dn": "DC=btcbahamas,DC=com",
                                                       "bind_user": "svc@btcbahamas.com"}}).json()["id"]
    return c, real, calls, monkeypatch


def test_precheck(api):
    c, real, _, monkeypatch = api
    monkeypatch.setattr(main, "port_open", lambda host, port, timeout=1.5: True)
    r = c.post(f"/api/ad/{real}/domain-controllers/precheck", json={"name": "BTCPRODDC1"}).json()
    assert {x["id"]: x["status"] for x in r["checks"]} == {"partner": "ok", "replication": "ok", "fsmo": "warn", "kastec": "warn"}
    assert c.post(f"/api/ad/{real}/domain-controllers/precheck", json={"name": "FILESERVER"}).status_code == 404


def test_restart_refused_when_the_site_would_have_no_working_dc(api, tmp_path):
    c, real, calls, monkeypatch = api
    monkeypatch.setattr(main, "port_open", lambda host, port, timeout=1.5: "btcproddc2" not in host)   # partner down
    r = c.post(f"/api/ad/{real}/domain-controllers/restart", json={"name": "BTCPRODDC1", "confirm": "BTCPRODDC1"})
    assert r.status_code == 409 and "Give a reason" in r.json()["detail"] and calls == []
    r = c.post(f"/api/ad/{real}/domain-controllers/restart",
               json={"name": "BTCPRODDC1", "confirm": "BTCPRODDC1", "override_reason": "DC2 is in for repair"})
    assert r.status_code == 200 and calls == ["btcproddc1.btcbahamas.com"]
    assert c.get("/api/audit").json()[0]["action"] == "restarted domain controller (pre-check overridden: DC2 is in for repair)"


def test_health_only_talks_to_known_dcs(api):
    c, real, _, monkeypatch = api
    monkeypatch.setattr(main, "port_open", lambda host, port, timeout=1.5: True)
    seen = []
    monkeypatch.setattr(dcrestart, "read_dse", lambda conn, host: seen.append(host) or (
        {"is_synchronized": True, "gc_ready": True, "current_time": datetime.now(timezone.utc).isoformat(), "dns_name": host}, None))
    assert c.get(f"/api/ad/{real}/domain-controllers/evil.example.com/health").status_code == 404   # never contacted
    c.post(f"/api/ad/{real}/domain-controllers/precheck", json={"name": "BTCPRODDC2"})              # learns the DCs
    r = c.get(f"/api/ad/{real}/domain-controllers/BTCPRODDC2/health?replication=true").json()
    assert r["ready"] is True and r["host"] == "btcproddc2.btcbahamas.com" and seen == ["btcproddc2.btcbahamas.com"]
    assert [x["id"] for x in r["checks"]] == ["ports", "ldap", "synchronized", "gc", "time", "replication"]


def test_demo_restart_goes_down_and_comes_back(api, monkeypatch):
    c, _, _, _ = api
    monkeypatch.setattr(dcrestart, "DEMO_PHASES", (0.2, 0.5, 0.8))
    demo = c.post("/api/connections", json={"kind": "ad", "name": "Demo", "demo": True}).json()["id"]
    dc = c.get(f"/api/ad/{demo}/domain-controllers").json()["dcs"][0]["name"]
    assert c.post(f"/api/ad/{demo}/domain-controllers/precheck", json={"name": dc}).status_code == 200
    assert c.post(f"/api/ad/{demo}/domain-controllers/restart", json={"name": dc, "confirm": dc}).status_code == 200
    health = lambda: c.get(f"/api/ad/{demo}/domain-controllers/{dc}/health").json()   # noqa: E731
    assert health()["ready"] is True              # not gone down yet
    time.sleep(0.3)
    assert health()["ready"] is False             # down
    time.sleep(0.3)
    syncing = {x["id"]: x["status"] for x in health()["checks"]}
    assert syncing["ports"] == "ok" and syncing["synchronized"] == "fail"
    time.sleep(0.3)
    assert health()["ready"] is True              # back


def test_outcome_is_recorded(api):
    c, real, _, _ = api
    r = c.post(f"/api/ad/{real}/domain-controllers/restart-complete",
               json={"name": "BTCPRODDC1", "outcome": "healthy", "notes": "CHG-1042"})
    assert r.status_code == 200
    entry = c.get("/api/audit").json()[0]
    assert entry["action"] == "DC restart checked: healthy: CHG-1042" and entry["target"] == "BTCPRODDC1"


def test_wording_for_a_one_dc_domain():
    only = [dict(DCS[0])]
    r = dcrestart.fsmo_check(only[0], only)
    assert "no other writable DC" in r["detail"] and "command" not in r
    assert "uses ," not in dcrestart.kastec_dc_check(only[0], "")["detail"]
