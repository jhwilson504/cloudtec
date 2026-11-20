"""The Ecosystem map: joining the firewall, switches, hosts and outside traffic."""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.ecosystem import norm
from app.main import create_app

PASSWORD = "correct horse battery"


@pytest.fixture
def client(tmp_path):
    c = TestClient(create_app(Settings(data_dir=tmp_path, secret_key=None, admin_password=PASSWORD,
                                       secure_cookies=False, session_hours=1, shell="off")))
    c.post("/api/login", json={"password": PASSWORD})
    return c


def test_switch_names_from_different_sources_match():
    assert norm("SW-CORE-01.btc.local(FOC2034X0AB)") == norm("sw-core-01") == "sw-core-01"
    assert norm("10.0.19.2") == "10.0.19.2"


def test_demo_ecosystem_joins_everything(client):
    for kind, name in (("fortigate", "Demo FortiGate"), ("solarwinds", "Demo SolarWinds"), ("vsphere", "Demo vCenter")):
        assert client.post("/api/connections", json={"kind": kind, "name": name, "demo": True}).status_code == 201
    m = client.get("/api/ecosystem").json()
    nodes = {n["id"]: n for n in m["nodes"]}
    links = {(l["a"], l["b"]): l for l in m["links"]}
    by_label = {n["label"]: n for n in m["nodes"]}

    # Firewall, its internet links with public addresses, and its inside networks by IP.
    assert nodes["fw:Demo FortiGate"]["label"] == "FGT-EDGE-01" and nodes["fw:Demo FortiGate"]["col"] == 2
    wan1 = nodes["isp:Demo FortiGate|wan1"]
    assert wan1["col"] == 1 and wan1["sub"] == "203.0.113.10/29 → 203.0.113.9"
    assert nodes["net:Demo FortiGate|port3"]["sub"] == "10.50.241.0/24"
    assert links[("isp:Demo FortiGate|wan1", "fw:Demo FortiGate")]["rate"]["in"] > 0
    # SolarWinds: the firewall's port3 goes to SW-CORE-01; core and access switches in their columns.
    core = [n for n in m["nodes"] if n["label"] == "SW-CORE-01"][0]
    assert core["col"] == 4 and by_label["DATA_RECOVERY"]["col"] == 5
    assert ("net:Demo FortiGate|port3", core["id"]) in links
    # vCenter: ESXi hosts hang off the switches their uplinks report over CDP.
    assert any(a == core["id"] and b.startswith("host:esx0") for a, b in links)
    assert nodes["host:esx01.lab.local"]["col"] == 6
    # Outside: destinations grouped by provider, through the busiest internet link; Zscaler separately.
    dests = [n for n in m["nodes"] if n["kind"] == "destination"]
    assert {"Microsoft 365", "Google", "Amazon AWS"} <= {d["label"] for d in dests} and all(d["col"] == 0 for d in dests)
    assert all(l["b"] == "isp:Demo FortiGate|wan1" for l in m["links"] if l["a"].startswith("dest:"))
    assert nodes["zscaler"]["col"] == 0 and ("zscaler", "isp:Demo FortiGate|wan1") in links
    assert nodes["zpa:ZSCALERAPP001"]["sub"] == "ZPA connector (not found in vCenter)"
    assert m["summary"]["firewalls"] == 1 and m["summary"]["internet_links"] == 2 and m["flows"]["demo"]
    assert not [n for n in m["notes"] if "Add" in n]


def test_empty_ecosystem_says_what_to_add(client):
    m = client.get("/api/ecosystem").json()
    assert [n["id"] for n in m["nodes"]] == ["isp:internet"]
    assert any("FortiGate" in n for n in m["notes"]) and any("SolarWinds" in n for n in m["notes"])


def test_netflow_settings(client):
    s = client.get("/api/ecosystem/settings").json()
    assert s["enabled"] is False and s["port"] == 2055 and s["zpa_connectors"] == ["ZSCALERAPP001", "BSPDRPZPACON02"]
    assert client.put("/api/ecosystem/settings", json={"internal": ["not a network"]}).status_code == 422
    s = client.put("/api/ecosystem/settings", json={"internal": ["196.32.0.0/16"], "zpa_connectors": ["ZSCALERAPP001", " "]}).json()
    assert s["internal"] == ["196.32.0.0/16"] and s["zpa_connectors"] == ["ZSCALERAPP001"]


def test_vcenter_only_shows_networks_and_gateways(client):
    """No FortiGate or SolarWinds: networks by IP and their gateways come from VMware Tools on the VMs."""
    assert client.post("/api/connections", json={"kind": "vsphere", "name": "Demo vCenter", "demo": True}).status_code == 201
    m = client.get("/api/ecosystem").json()
    nodes = {n["id"]: n for n in m["nodes"]}
    links = {(l["a"], l["b"]): l for l in m["links"]}
    gw = nodes["fw:gateways"]
    assert gw["col"] == 2 and "10.50.241.1 for 10.50.241.0/24" in gw["details"]["subnets"]
    net = nodes["net:vc|10.50.241.0/24"]
    assert net["col"] == 3 and net["label"] == "Servers-VLAN20 (VLAN 20)" and "VM(s)" in net["sub"]
    assert links[("fw:gateways", "net:vc|10.50.241.0/24")]["rate"]["in"] > 0
    assert ("net:vc|10.50.241.0/24", "cdp:sw-core-01") in links and nodes["cdp:sw-core-01"]["col"] == 4
    assert ("isp:internet", "fw:gateways") in links
    assert m["summary"]["firewalls"] == 0 and m["summary"]["networks"] == 3
    assert any("VMware Tools" in n for n in m["notes"]) and not any(n.startswith("Add SolarWinds") for n in m["notes"])


def test_firewall_from_solarwinds_when_no_fortigate(client):
    for kind, name in (("solarwinds", "Demo SolarWinds"), ("vsphere", "Demo vCenter")):
        assert client.post("/api/connections", json={"kind": kind, "name": name, "demo": True}).status_code == 201
    m = client.get("/api/ecosystem").json()
    nodes = {n["id"]: n for n in m["nodes"]}
    links = {(l["a"], l["b"]): l for l in m["links"]}
    fw = nodes["fw:sw0|201"]
    assert fw["label"] == "FGT-EDGE-01" and "via SolarWinds" in fw["sub"]
    assert nodes["isp:sw0|201|wan1"]["label"] == "ISP-A Fibre" and nodes["isp:sw0|201|wan1"]["sub"] == "203.0.113.10/29"
    assert nodes["isp:sw0|201|wan2"]["status"] == "down"
    assert nodes["net:sw0|201|port3"]["sub"] == "10.50.241.0/24"
    assert links[("isp:sw0|201|wan1", "fw:sw0|201")]["rate"]["in"] > 0
    core = [n for n in m["nodes"] if n["label"] == "SW-CORE-01" and n["kind"] == "switch"][0]
    assert ("net:sw0|201|port3", core["id"]) in links
    assert "fw:gateways" not in nodes and m["summary"]["firewalls"] == 1 and m["summary"]["internet_links"] == 2
