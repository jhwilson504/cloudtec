"""Creating a LUN (and a datastore on it): the checks, the exact commands, and the whole flow on the demo array."""

import pytest
from fastapi.testclient import TestClient

from app import provision
from app.config import Settings
from app.connectors import ConnectorError
from app.connectors.demo import DemoVNX, _vnx_output
from app.connectors.vnx import VNXConnector, sh_quote
from app.main import create_app
from app.store import Connection

KEY = "SHA256:Zm9vYmFyYmF6cXV4cXV1eGNvcmdlZ3JhdWx0Z2FycGx5"
VC = {"name": "vCenter", "clusters": [{"name": "Prod-Cluster", "hosts": ["esx01.lab.local", "esx02.lab.local", "esx05.lab.local"]}],
      "datastores": ["ds-vmfs-01"], "powercli": True}


def array():
    return DemoVNX("plan-tests", {}).read()


def req(**kw):
    base = {"name": "NEW_APP", "pool": "Pool 0", "size_gb": 500, "thin": True, "sp": "auto",
            "storage_groups": ["ESX-Prod-Cluster"], "hlu": None, "datastore": None}
    return {**base, **kw}


def test_a_good_plan_lists_the_exact_commands():
    p = provision.plan(req(datastore={"cluster": "Prod-Cluster", "name": "NEW_APP", "vmfs": "auto"}), array(), VC)
    assert p["ok"] and p["hlu"] == 4  # HLUs 0-3 are taken in ESX-Prod-Cluster
    assert p["steps"][0] == "naviseccli lun -create -type Thin -capacity 500 -sq gb -poolName 'Pool 0' -name 'NEW_APP' -sp b"
    assert p["steps"][1] == "naviseccli storagegroup -addhlu -gname 'ESX-Prod-Cluster' -hlu 4 -alu <the new LUN's number>"
    assert "New-Datastore -Name 'NEW_APP'" in p["steps"][2]
    assert any("esx05.lab.local" in w for w in p["warnings"])  # a cluster host that isn't in the storage group


def test_the_checks():
    a = array()
    assert any("already a LUN" in e for e in provision.plan(req(name="ds-vmfs-01"), a, None)["errors"])
    assert any("not enough" in e for e in provision.plan(req(thin=False, size_gb=5000), a, None)["errors"])
    thin = provision.plan(req(size_gb=5000), a, None)
    assert thin["ok"] and any("run out" in w for w in thin["warnings"]) and any("subscribed" in w for w in thin["warnings"])
    assert any("HLU 2 is already used" in e for e in provision.plan(req(hlu=2), a, None)["errors"])
    assert any("no storage group called" in e for e in provision.plan(req(storage_groups=["~filestorage"]), a, None)["errors"])
    assert any("letters, digits" in e for e in provision.plan(req(name="bad name'; rm"), a, None)["errors"])
    assert provision.plan(req(sp="a"), a, None)["lun"]["sp"] == "a"
    nogroup = provision.plan(req(storage_groups=[]), a, None)
    assert nogroup["ok"] and nogroup["hlu"] is None and any("won't be visible" in w for w in nogroup["warnings"])
    ds = {"cluster": "Prod-Cluster", "name": "ds-vmfs-01", "vmfs": "auto"}
    assert any("already has a datastore" in e for e in provision.plan(req(datastore=ds), a, VC)["errors"])
    assert any("PowerCLI isn't installed" in e for e in provision.plan(req(datastore={**ds, "name": "x1"}), a, {**VC, "powercli": False})["errors"])
    assert any("Choose the storage group" in e for e in provision.plan(req(storage_groups=[], datastore={**ds, "name": "x1"}), a, VC)["errors"])


def test_create_lun_and_present_it_send_these_commands():
    seen = []

    def run(script, host_key):
        seen.append(script)
        if "lun -create" in script:
            return 0, "@@KASTEC sp spa\n@@KASTEC create\n@@KASTEC rc 0\n@@KASTEC luns\n" + (
                "LOGICAL UNIT NUMBER 42\nName:  NEW_APP\nUID:  60:06:01:60:AB:CD:12:00:11:22:33:44:55:66:77:42\n"), ""
        return 0, ("@@KASTEC sp spa\n@@KASTEC add\n@@KASTEC groups\nStorage Group Name:    ESX Prod\n"
                   "HLU/ALU Pairs:\n\n  HLU Number     ALU Number\n  ----------     ----------\n    4               42\n"), ""

    c = VNXConnector({"host": "10.0.16.124", "username": "nasadmin", "host_key": KEY}, "pw", runner=run)
    lun = c.create_lun("NEW_APP", "Pool 0", 500, True, "b")
    assert lun["number"] == 42 and lun["naa"] == "naa.60060160abcd12001122334455667742"
    assert "lun -create -type Thin -capacity 500 -sq gb -poolName 'Pool 0' -name 'NEW_APP' -sp b </dev/null" in seen[0]
    c.add_to_group("ESX Prod", 4, 42)
    assert "storagegroup -addhlu -gname 'ESX Prod' -hlu 4 -alu 42 </dev/null" in seen[1]
    with pytest.raises(ConnectorError, match="didn't add LUN 43"):
        c.add_to_group("ESX Prod", 5, 43)


def test_a_failed_create_says_what_the_vnx_answered():
    out = "@@KASTEC sp spa\n@@KASTEC create\nError: The specified LUN name is already in use.\n@@KASTEC rc 1\n@@KASTEC luns\n"
    c = VNXConnector({"host": "cs", "username": "nasadmin", "host_key": KEY}, "pw", runner=lambda s, k: (0, out, ""))
    with pytest.raises(ConnectorError, match="already in use"):
        c.create_lun("NEW_APP", "Pool 0", 10, True, "a")


def test_values_that_could_break_out_of_a_command_are_refused():
    for bad in ("Pool' ; rm -rf / '", "two\nlines", ""):
        with pytest.raises(ConnectorError):
            sh_quote(bad)
    c = VNXConnector({"host": "cs", "username": "nasadmin", "host_key": KEY}, "pw", runner=lambda s, k: (0, "", ""))
    with pytest.raises(ConnectorError):
        c.create_lun("bad name", "Pool 0", 10, True, "a")


def test_datastore_script_and_its_result():
    vc = Connection(id="v", kind="vsphere", name="vc", config={"host": "jfkvcs.btcbahamas.com", "username": "administrator@vsphere.local",
                                                                "verify_ssl": False}, secret="p'w", demo=False, created_at="")
    script = provision.datastore_script(vc, "JFK", "naa.6006016078003a00aa", "NEW_APP", "auto")
    assert "Get-Cluster -Name 'JFK'" in script and "-RescanAllHba" in script and "-CanonicalName 'naa.6006016078003a00aa'" in script
    assert "New-Datastore -VMHost $first -Name 'NEW_APP' -Path 'naa.6006016078003a00aa' -Vmfs" in script
    assert "'p''w'" in script and "InvalidCertificateAction Ignore" in script and "Disconnect-VIServer" in script
    assert "'6.5'" in script and "VMFS-6 needs" in provision.datastore_script(vc, "JFK", "naa.x", "N", "6")
    assert provision.datastore_result("noise\nDATASTORE|NEW_APP|500|5|3|3\n") == \
        "Created VMFS-5 datastore NEW_APP (500 GB); 3 of 3 hosts in the cluster see it."
    assert provision.datastore_result("nothing") is None


def test_run_command_parses_the_exit_code():
    seen = []

    def run(script, host_key):
        seen.append(script)
        return 0, "@@KASTEC out\nserver_2 : running\n@@KASTEC rc 3\n", ""

    r = VNXConnector({"host": "cs", "username": "nasadmin", "host_key": KEY}, "pw", runner=run).run_command("nas_server -list\r")
    assert r == {"output": "server_2 : running\n", "exit_code": 3}
    assert "( nas_server -list\n) </dev/null 2>&1" in seen[0] and "\r" not in seen[0]


# --- the whole flow through the API, on the demo array and demo vCenter --------------------------

PASSWORD = "correct horse battery"


def test_create_a_lun_and_datastore_on_the_demo(tmp_path):
    c = TestClient(create_app(Settings(data_dir=tmp_path, secret_key=None, admin_password=PASSWORD,
                                       secure_cookies=False, session_hours=1, shell="off")))
    c.post("/api/login", json={"password": PASSWORD})
    v = c.post("/api/connections", json={"kind": "vnx", "name": "Demo VNX", "demo": True}).json()
    vc = c.post("/api/connections", json={"kind": "vsphere", "name": "Demo vCenter", "demo": True}).json()

    o = c.get(f"/api/vnx/{v['id']}/provision/options").json()
    assert [g["name"] for g in o["storage_groups"]] == ["ESX-Prod-Cluster"]
    prod = next(cl for cl in o["vcenters"][0]["clusters"] if cl["name"] == "Prod-Cluster")
    assert prod["hosts"] == ["esx01.lab.local", "esx02.lab.local"]

    body = {"name": "KASTEC_NEW_01", "pool": "Pool 1", "size_gb": 300, "thin": True, "sp": "auto",
            "storage_groups": ["ESX-Prod-Cluster"],
            "datastore": {"connection_id": vc["id"], "cluster": "Prod-Cluster", "name": "KASTEC_NEW_01", "vmfs": "auto"}}
    assert c.post(f"/api/vnx/{v['id']}/provision/plan", json=body).json()["ok"]
    assert c.post(f"/api/vnx/{v['id']}/provision", json=body).status_code == 422          # not confirmed
    r = c.post(f"/api/vnx/{v['id']}/provision", json={**body, "confirm": "KASTEC_NEW_01"}).json()
    assert r["ok"] and [s["ok"] for s in r["steps"]] == [True, True, True], r
    assert "HLU 4" in r["steps"][1]["detail"]

    lun = next(x for x in c.get(f"/api/vnx/{v['id']}/overview").json()["luns"] if x["name"] == "KASTEC_NEW_01")
    assert lun["use"] == "datastore_empty" and lun["datastore"]["name"] == "KASTEC_NEW_01"
    again = c.post(f"/api/vnx/{v['id']}/provision", json={**body, "confirm": "KASTEC_NEW_01"})
    assert again.status_code == 422 and "already a LUN" in again.json()["detail"]

    out = c.post(f"/api/vnx/{v['id']}/run", json={"command": "naviseccli -h spa getagent"}).json()
    assert "VNX5400" in out["output"]
    log = (tmp_path / "audit.log").read_text()
    assert "VNX: Created LUN KASTEC_NEW_01" in log and "VNX: ran a Control Station command" in log


def test_operators_cannot_create_or_run_commands(tmp_path):
    c = TestClient(create_app(Settings(data_dir=tmp_path, secret_key=None, admin_password=PASSWORD,
                                       secure_cookies=False, session_hours=1, shell="off")))
    c.post("/api/login", json={"password": PASSWORD})
    v = c.post("/api/connections", json={"kind": "vnx", "name": "Demo VNX", "demo": True}).json()
    c.post("/api/users", json={"username": "op", "password": "operator-pass-1", "role": "operator"})
    c.post("/api/logout")
    c.post("/api/login", json={"username": "op", "password": "operator-pass-1"})
    assert c.get(f"/api/vnx/{v['id']}/overview").status_code == 200
    assert c.get(f"/api/vnx/{v['id']}/provision/options").status_code == 403
    assert c.post(f"/api/vnx/{v['id']}/run", json={"command": "id"}).status_code == 403
