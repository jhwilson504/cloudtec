"""The vCenter network map: the PowerCLI scripts (against a fake PowerCLI), parsing, and the API."""

import base64
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import netmap
from app.config import Settings
from app.main import create_app
from app.shell import find_powershell
from app.store import Connection

PWSH = find_powershell()
needs_pwsh = pytest.mark.skipif(not PWSH, reason="PowerShell isn't installed here")
FAKE = Path(__file__).parent / "fake_powercli"


def vc(password="right"):
    return Connection(id="vc1", kind="vsphere", name="JFK vCenter", demo=False, created_at="", secret=password,
                      config={"host": "jfkvcs.btcbahamas.com", "username": "administrator@vsphere.local", "verify_ssl": False})


def test_build_handles_powershell_json_quirks():
    # ConvertTo-Json makes a one-item list a plain object and an empty list null.
    raw = {"vcenter": "vc", "dvportgroups": None, "hosts": {
        "name": "h1", "cluster": None, "state": "connected",
        "pnics": {"name": "vmnic0", "speed": 1000, "switch": "SW1", "port": "Gi1/0/1"},
        "vswitches": {"name": "vSwitch0", "pnics": "vmnic0"},
        "portgroups": {"name": "VM Network", "vlan": "0", "vswitch": "vSwitch0"},
        "proxies": None, "vmks": None},
        "vms": {"name": "vm1", "host": "h1", "power": "poweredOn", "nics": {"network": "VM Network", "connected": True}}}
    m = netmap.build(raw)
    assert m["clusters"] == ["Standalone hosts"]
    assert m["uplinks"][0]["switch"] == "std:Standalone hosts|vSwitch0" and m["uplinks"][0]["physical"] == "SW1"
    assert m["vms"][0]["nics"][0]["portgroup"] == "std:Standalone hosts|vSwitch0|VM Network"
    assert m["physical"][0]["name"] == "SW1"


def test_traffic_and_errors():
    t = netmap.parse_traffic("P|h1|vmnic0|net.received.average|125\nP|h1|vmnic0|net.transmitted.average|25\n"
                             "V|vm1|net.received.average|10\nnoise\nV|vm1|net.transmitted.average|x\nTRAFFIC-DONE")
    assert t == {"uplinks": {"h1|vmnic0": {"rx": 1_000_000.0, "tx": 200_000.0}}, "vms": {"vm1": {"rx": 80_000.0, "tx": 0.0}}}
    with pytest.raises(netmap.NetMapError, match="PowerCLI is not installed"):
        netmap.decode_topology("NETMAP-ERROR|VMware PowerCLI is not installed on the KasTec computer.")
    with pytest.raises(netmap.NetMapError, match="didn't return"):
        netmap.decode_topology("something else")
    blob = base64.b64encode(json.dumps({"vcenter": "x"}).encode()).decode()
    assert netmap.decode_topology("\n".join("NETMAP|" + blob[i:i + 10] for i in range(0, len(blob), 10))) == {"vcenter": "x"}


def test_the_script_never_changes_anything():
    for word in ("Set-VM", "New-", "Remove-", "Set-VirtualSwitch", "Set-VMHost", "Restart", "Stop-"):
        assert word not in netmap.TOPOLOGY and word not in netmap.TRAFFIC
    assert "right" not in netmap.TOPOLOGY


@needs_pwsh
def test_scripts_against_fake_powercli(monkeypatch):
    monkeypatch.setenv("PSModulePath", f"{FAKE}{os.pathsep}{os.environ.get('PSModulePath', '')}")
    maps = netmap.NetMaps(PWSH)
    try:
        m = maps.topology(vc())
        assert m["vcenter"] == "jfkvcs.btcbahamas.com" and m["clusters"] == ["JFK"]
        up = {u["name"]: u for u in m["uplinks"]}
        assert (up["vmnic0"]["physical"], up["vmnic0"]["port"], up["vmnic0"]["speed"]) == ("SW-JFK-CORE", "Gi1/0/7", 1000)
        assert up["vmnic1"]["speed"] == 0 and up["vmnic1"]["switch"] == "std:JFK|vSwitch0"      # link down
        assert (up["vmnic2"]["physical"], up["vmnic2"]["switch"]) == ("NX-JFK-01", "dvs:dvs-JFK")  # LLDP, distributed
        # An internal-only vSwitch (no uplinks) is shown too; it caused "array index evaluated to null" before.
        assert {s["id"] for s in m["switches"]} == {"std:JFK|vSwitch0", "std:JFK|vSwitch-Internal", "dvs:dvs-JFK"}
        pgs = {p["id"]: p for p in m["portgroups"]}
        assert pgs["dvpg:dvportgroup-5"]["vlan"] == "20" and "dvpg:dvportgroup-4" not in pgs    # uplink port group hidden
        vms = {v["name"]: v for v in m["vms"]}
        assert "TEMPLATE-W2022" not in vms
        orphan = vms["%2fvmfs%2fvolumes%2f5793c116%2fPELATROTEST_03"]       # a VM vCenter has no host for
        assert orphan["host"] is None and orphan["nics"] == []
        assert vms["BTCPRODDC1"]["nics"] == [{"portgroup": "std:JFK|vSwitch0|VCMS-Only", "connected": True, "mac": "00:50:56:aa:00:01",
                                              "ips": ["10.0.16.20/22"]}]
        assert vms["BTCPRODDC1"]["gateway"] == "10.0.16.1"
        assert vms["APP02"]["nics"][0]["portgroup"] == "dvpg:dvportgroup-5" and not vms["APP02"]["nics"][0]["connected"]
        vmk = {v["name"]: v for v in m["vmks"]}
        assert vmk["vmk0"]["services"] == ["management"] and vmk["vmk1"]["portgroup"] == "dvpg:dvportgroup-5"
        assert vmk["vmk1"]["services"] == ["vmotion"] and vmk["vmk0"]["portgroup"] == "std:JFK|vSwitch0|Management Network"

        t = maps.traffic(vc())
        assert t["uplinks"] == {"jfkvh10.btcbahamas.com|vmnic0": {"rx": 10_000_000.0, "tx": 2_000_000.0}}
        assert t["vms"] == {"BTCPRODDC1": {"rx": 2_000_000.0, "tx": 400_000.0}}
        assert maps.topology(vc()) is m                      # cached
        session = next(iter(maps._sessions.values()))
        assert session.run("$global:FakeConnects")["output"] == "1"   # signed in once, reused
    finally:
        maps.close()


@needs_pwsh
def test_wrong_password_and_missing_powercli(monkeypatch, tmp_path):
    monkeypatch.setenv("PSModulePath", f"{FAKE}{os.pathsep}{os.environ.get('PSModulePath', '')}")
    maps = netmap.NetMaps(PWSH)
    try:
        with pytest.raises(netmap.NetMapError, match=r"incorrect user name or password.*script line \d+"):
            maps.topology(vc("wrong"))
    finally:
        maps.close()
    monkeypatch.setenv("PSModulePath", str(tmp_path))
    maps = netmap.NetMaps(PWSH)
    try:
        with pytest.raises(netmap.NetMapError, match="Install-Module VCF.PowerCLI"):
            maps.traffic(vc())
    finally:
        maps.close()
    with pytest.raises(netmap.NetMapError, match="PowerShell isn't available"):
        netmap.NetMaps(None).topology(vc())


def test_demo_network_through_the_api(tmp_path):
    c = TestClient(create_app(Settings(data_dir=tmp_path, secret_key=None, admin_password="correct horse battery",
                                       secure_cookies=False, session_hours=1, shell="off")))
    c.post("/api/login", json={"password": "correct horse battery"})
    v = c.post("/api/connections", json={"kind": "vsphere", "name": "Demo vCenter", "demo": True}).json()
    m = c.get(f"/api/vsphere/{v['id']}/network").json()
    assert m["clusters"] == ["Lab-Cluster", "Prod-Cluster", "Standalone hosts"]
    assert any(s["kind"] == "distributed" for s in m["switches"]) and len(m["uplinks"]) == 12
    t = c.get(f"/api/vsphere/{v['id']}/network/traffic").json()
    assert t["vms"] and all(x["rx"] > 0 for x in t["vms"].values())
    assert set(t["uplinks"]) <= {u["id"] for u in m["uplinks"]}
    assert c.get("/api/vsphere/nope/network").status_code == 404


@needs_pwsh
def test_hyper_v_commands_with_the_same_names_dont_interfere(monkeypatch):
    """A PC with Hyper-V's PowerShell tools has its own Get-VM and Get-VMHost; KasTec must call PowerCLI's."""
    monkeypatch.setenv("PSModulePath", f"{FAKE}{os.pathsep}{os.environ.get('PSModulePath', '')}")
    maps = netmap.NetMaps(PWSH)
    try:
        session = maps._session(vc())
        session.run("function global:Get-VM { 'HYPER-V' }; function global:Get-VMHost { 'HYPER-V' }")
        t = maps.traffic(vc())
        assert t["vms"] == {"BTCPRODDC1": {"rx": 2_000_000.0, "tx": 400_000.0}}
        assert "jfkvh10.btcbahamas.com|vmnic0" in t["uplinks"]
    finally:
        maps.close()
