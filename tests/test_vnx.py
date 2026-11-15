"""The VNX connector: naviseccli output parsing, LUN-to-datastore matching, signing in through plink."""

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.connectors import ConnectorError
from app.connectors import vnx as vnx_mod
from app.connectors.demo import _vnx_output
from app.connectors.vnx import VNXConnector, classify, link_datastores, naa, parse_groups, parse_luns, split_sections
from app.main import create_app

CONFIG = {"host": "10.0.16.50", "username": "nasadmin"}
KEY = "SHA256:Zm9vYmFyYmF6cXV4cXV1eGNvcmdlZ3JhdWx0Z2FycGx5"


def ok_runner(seen=None):
    def run(script, host_key):
        if seen is not None:
            seen.append((script, host_key))
        return 0, _vnx_output(), ""
    return run


def test_overview_parses_the_control_station_output():
    seen = []
    o = VNXConnector({**CONFIG, "host_key": KEY}, "pw", runner=ok_runner(seen)).overview()
    assert o["agent"]["model"] == "VNX5400" and o["agent"]["serial"] == "CKM00DEMO0001"
    assert o["sp"] == "spa" and o["control_station"] == "10.0.16.50"
    assert o["faults"] == {"ok": False, "items": ["Bus 0 Enclosure 1 Disk 7 : Faulted"]}
    assert [p["name"] for p in o["pools"]] == ["Pool 0", "Pool 1"]
    assert o["pools"][0]["percent_full"] == 74.4 and o["pools"][0]["free_gb"] == 3069.8 and o["pools"][0]["luns"] == [0, 1, 3, 8]
    use = {lun["name"]: lun["use"] for lun in o["luns"]}
    assert use["LUN_TEST_07"] == "not_presented" and use["FILE_LUN_0"] == "file"
    assert use["Reserved LUN Pool 1"] == "private" and use["ds-vmfs-01"] == "presented"
    lun0 = o["luns"][0]
    assert lun0["naa"] == "naa.60060160abcd12001122334455667701" and lun0["hosts"] == ["esx01.lab.local", "esx02.lab.local"]
    assert [s["ok"] for s in o["slots"]] == [True, True, True]
    # One SSH session, read-only commands, both SPs tried, the RAID-group LUN listing included.
    script = seen[0][0]
    assert seen[0][1] == KEY and len(seen) == 1
    assert "SP=spa" in script and "|| SP=spb" in script and "getlun" in script and "getreason" in script
    assert not any(w in script for w in ("-destroy", "-unbind", "-remove", "-create", "-modify", "-expand", "-trespass"))


def test_summary_skips_the_slow_raid_group_listing():
    seen = []
    s = VNXConnector({**CONFIG, "host_key": KEY}, "pw", runner=ok_runner(seen)).summary()
    assert s == {"model": "VNX5400", "serial": "CKM00DEMO0001", "faults_ok": False, "faults": 1, "pools": 2,
                 "capacity_gb": 32000.0, "free_gb": 13869.8, "luns": 8, "not_presented": 2, "slots_bad": 0}
    assert "getlun" not in seen[0][0]


def test_raid_group_luns_and_uids():
    text = ("LOGICAL UNIT NUMBER 20\nName                        RG_LUN_20\nUID:                        60:06:01:60:00:11:22:33:44:55:66:77:88:99:AA:BB\n"
            "LUN Capacity(Megabytes):    10240\nState:                      Bound\nRAIDGroup ID:               3\n")
    lun = parse_luns(text)[0]
    assert (lun["number"], lun["name"], lun["user_gb"], lun["pool"], lun["state"]) == (20, "RG_LUN_20", 10.0, "RAID group 3", "Bound")
    assert naa("60:06:01:60:00:11:22:33:44:55:66:77:88:99:AA:BB") == "naa.6006016000112233445566778899aabb"
    assert naa("garbage") is None


def test_matching_luns_to_datastores():
    luns = parse_luns(split_sections(_vnx_output())["luns"])
    classify(luns, parse_groups(split_sections(_vnx_output())["groups"]))
    link_datastores(luns, [
        {"name": "ds-vmfs-01", "type": "VMFS", "vms": ["SRV-DC01"], "connection": "vc"},
        {"name": "DS_VMFS_02", "type": "VMFS", "vms": [], "connection": "vc"},   # same name, different spelling
        {"name": "rdm_sql_logs", "type": "NFS", "vms": []},                    # NFS stores never back a LUN
    ])
    by = {lun["name"]: lun for lun in luns}
    assert by["ds-vmfs-01"]["use"] == "datastore" and by["ds-vmfs-01"]["datastore"]["vms"] == ["SRV-DC01"]
    assert by["ds-vmfs-02"]["use"] == "datastore_empty"
    assert by["RDM_SQL_LOGS"]["use"] == "unmatched"


def test_an_ambiguous_short_name_is_not_matched():
    luns = [{"name": "ESB", "use": "presented"}, {"name": "COGNOSSRV", "use": "presented"}]
    link_datastores(luns, [{"name": "JFK_VNX_ESB", "vms": ["a"]}, {"name": "PD_VNX_ESB", "vms": ["b"]},
                           {"name": "JFK_VNX_COGNOSSRV", "vms": ["COGNOSSRV"]}])
    assert luns[0]["use"] == "unmatched"
    assert luns[1]["use"] == "datastore" and luns[1]["datastore"]["matched_by"] == "similar name"


def test_first_connection_learns_the_ssh_key_and_uses_it():
    calls = []

    def run(script, host_key):
        calls.append(host_key)
        if not host_key:
            return 1, "", ("The host key is not cached for this server:\n  10.0.16.50 (port 22)\n"
                           f"The server's ssh-ed25519 key fingerprint is:\n  ssh-ed25519 255 {KEY}\n")
        return 0, _vnx_output(), ""

    c = VNXConnector(CONFIG, "pw", runner=run)
    assert "VNX5400 CKM00DEMO0001" in c.test()["message"]
    assert calls == [None, KEY] and c.learned_config == {"host_key": KEY}


def test_a_changed_ssh_key_is_refused():
    def run(script, host_key):
        return 1, "", "WARNING - POTENTIAL SECURITY BREACH!\nThe host key does not match the one PuTTY has cached"
    with pytest.raises(ConnectorError, match="different SSH key"):
        VNXConnector({**CONFIG, "host_key": KEY}, "pw", runner=run).test()


def test_wrong_password_and_naviseccli_security_problems():
    with pytest.raises(ConnectorError, match="refused the username or password"):
        VNXConnector({**CONFIG, "host_key": KEY}, "pw", runner=lambda s, k: (1, "", "Access denied\nFATAL ERROR: Remote side unexpectedly closed")).test()

    out = "@@KASTEC sp spb\n@@KASTEC getagent\nSecurity file not found. Already removed or check -secfilepath option.\n"
    with pytest.raises(ConnectorError, match="AddUserSecurity"):
        VNXConnector({**CONFIG, "host_key": KEY}, "pw", runner=lambda s, k: (0, out, "")).test()


def test_names_are_checked_before_they_reach_a_shell():
    for bad in ({"sp_a": "spa; rm -rf /"}, {"host": "cs `id`"}, {"username": "nas admin"}):
        with pytest.raises(ConnectorError):
            VNXConnector({**CONFIG, **bad}, "pw")


def test_plink_gets_the_password_in_a_file_that_is_then_deleted(tmp_path, monkeypatch):
    monkeypatch.setenv("KASTEC_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(vnx_mod, "find_plink", lambda: "plink.exe")
    seen = {}

    def fake_run(args, input, capture_output, timeout, creationflags):
        pwfile = args[args.index("-pwfile") + 1]
        seen.update(args=args, script=input, password=Path(pwfile).read_text(), pwfile=pwfile)
        # Windows-style line endings coming back are fine too.
        return subprocess.CompletedProcess(args, 0, _vnx_output().replace("\n", "\r\n").encode(), b"")

    monkeypatch.setattr(vnx_mod.subprocess, "run", fake_run)
    VNXConnector({**CONFIG, "port": 2222, "host_key": KEY}, "s3cret pw!", runner=None).test()
    args = seen["args"]
    assert args[:6] == ["plink.exe", "-ssh", "-batch", "-P", "2222", "-l"] and "nasadmin" in args
    assert args[args.index("-hostkey") + 1] == KEY and args[-2:] == ["10.0.16.50", "bash -s"]
    assert "s3cret pw!" not in " ".join(args) and seen["password"] == "s3cret pw!\n"
    # The script reaches the Control Station's bash as bytes with Unix line endings only.
    assert isinstance(seen["script"], bytes) and b"\r" not in seen["script"] and seen["script"].endswith(b"exit 0\n")
    assert b"naviseccli" in seen["script"] and not Path(seen["pwfile"]).exists()


def test_plink_missing(monkeypatch):
    monkeypatch.setattr(vnx_mod, "find_plink", lambda: None)
    with pytest.raises(ConnectorError, match="plink.exe wasn't found"):
        VNXConnector({**CONFIG, "host_key": KEY}, "pw").test()


# --- through the API ----------------------------------------------------------------------

PASSWORD = "correct horse battery"


def client(tmp_path):
    c = TestClient(create_app(Settings(data_dir=tmp_path, secret_key=None, admin_password=PASSWORD,
                                       secure_cookies=False, session_hours=1, shell="off")))
    c.post("/api/login", json={"password": PASSWORD})
    return c


def test_demo_vnx_overview_matches_the_demo_vcenter(tmp_path):
    with client(tmp_path) as c:
        v = c.post("/api/connections", json={"kind": "vnx", "name": "Demo VNX", "demo": True}).json()
        c.post("/api/connections", json={"kind": "vsphere", "name": "Demo vCenter", "demo": True})
        o = c.get(f"/api/vnx/{v['id']}/overview").json()
        by = {lun["name"]: lun for lun in o["luns"]}
        assert by["ds-vmfs-01"]["use"] == "datastore" and by["ds-vmfs-01"]["datastore"]["connection"] == "Demo vCenter"
        assert by["ds-vmfs-02"]["use"] == "datastore_empty" and by["LUN_TEST_07"]["use"] == "not_presented"
        assert o["vsphere"] == {"connections": 1, "datastores": 3, "errors": []}
        dash = {i["kind"]: i for i in c.get("/api/dashboard").json()}
        assert dash["vnx"]["summary"]["not_presented"] == 2
        assert c.get("/api/vnx/nope/overview").status_code == 404


def test_the_learned_ssh_key_is_saved_with_the_connection(tmp_path, monkeypatch):
    def fake_plink(self, script, host_key):
        if not host_key:
            return 1, "", f"The host key is not cached for this server\nssh-ed25519 255 {KEY}\n"
        return 0, _vnx_output(), ""

    monkeypatch.setattr(VNXConnector, "_plink", fake_plink)
    with client(tmp_path) as c:
        v = c.post("/api/connections", json={"kind": "vnx", "name": "JFK VNX", "password": "pw",
                                             "config": {**CONFIG, "sp_a": "spa", "verify_ssl": False}}).json()
        assert "verify_ssl" not in v["config"]
        assert c.post(f"/api/connections/{v['id']}/test").status_code == 200
        saved = next(x for x in c.get("/api/connections").json() if x["id"] == v["id"])
        assert saved["config"]["host_key"] == KEY
