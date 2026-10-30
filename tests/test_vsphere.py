"""The real vSphere connector, against a mocked vCenter REST API."""

import httpx
import pytest

from app.connectors import ConnectorError
from app.connectors.vsphere import VSphereConnector

TOKEN = "abc123"
CONFIG = {"host": "vc.example.com", "username": "administrator@vsphere.local"}


def fake_vcenter(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, dict(request.url.params)))
        path = request.url.path
        if path == "/api/session" and request.method == "POST":
            if request.headers.get("authorization") != httpx.BasicAuth("administrator@vsphere.local", "pw")._auth_header:
                return httpx.Response(401)
            return httpx.Response(201, json=TOKEN)
        if request.headers.get("vmware-api-session-id") != TOKEN:
            return httpx.Response(401)
        if path == "/api/session":
            return httpx.Response(204)
        if path == "/api/vcenter/vm":
            return httpx.Response(200, json=[
                {"vm": "vm-2", "name": "web01", "power_state": "POWERED_ON", "cpu_count": 2, "memory_size_MiB": 4096},
                {"vm": "vm-1", "name": "App02", "power_state": "POWERED_OFF", "cpu_count": 4, "memory_size_MiB": 8192},
            ])
        if path == "/api/vcenter/host":
            return httpx.Response(200, json=[{"host": "host-1", "name": "esx1", "connection_state": "CONNECTED", "power_state": "POWERED_ON"}])
        if path == "/api/vcenter/datastore":
            return httpx.Response(200, json=[{"datastore": "ds-1", "name": "ds1", "type": "VMFS", "capacity": 100, "free_space": 40}])
        if path == "/api/vcenter/vm/vm-2/power":
            return httpx.Response(204)
        if path == "/api/vcenter/vm/vm-2/guest/power":
            return httpx.Response(400, json={"messages": [{"default_message": "VMware Tools is not running"}]})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_login_list_and_logout():
    calls = []
    with VSphereConnector(CONFIG, "pw", transport=fake_vcenter(calls)) as vc:
        vms = vc.vms()
        summary = vc.summary()
    assert [v["name"] for v in vms] == ["App02", "web01"]
    assert vms[1] == {"id": "vm-2", "name": "web01", "power_state": "POWERED_ON", "cpus": 2, "memory_mb": 4096}
    assert summary["powered_on"] == 1 and summary["free_space"] == 40
    assert calls[-1][:2] == ("DELETE", "/api/session")


def test_bad_password():
    with pytest.raises(ConnectorError, match="rejected"):
        with VSphereConnector(CONFIG, "wrong", transport=fake_vcenter([])):
            pass


def test_power_actions_route_correctly():
    calls = []
    with VSphereConnector(CONFIG, "pw", transport=fake_vcenter(calls)) as vc:
        vc.power("vm-2", "stop")
        with pytest.raises(ConnectorError, match="VMware Tools is not running"):
            vc.power("vm-2", "shutdown")
        with pytest.raises(ConnectorError, match="Invalid VM id"):
            vc.power("../vm-2", "stop")
    assert ("POST", "/api/vcenter/vm/vm-2/power", {"action": "stop"}) in calls
    assert ("POST", "/api/vcenter/vm/vm-2/guest/power", {"action": "shutdown"}) in calls


def topology_vcenter():
    vms = {"vm-1": ("web01", "host-1"), "vm-2": ("sql01", "host-2"), "vm-3": ("edge01", "host-3"),
           "vm-9": ("orphan01", None)}

    def vm(v):
        return {"vm": v, "name": vms[v][0], "power_state": "POWERED_ON", "cpu_count": 2, "memory_size_MiB": 4096}

    def handler(request: httpx.Request) -> httpx.Response:
        path, q = request.url.path, request.url.params
        if path == "/api/session":
            return httpx.Response(201 if request.method == "POST" else 204, json=TOKEN)
        if request.headers.get("vmware-api-session-id") != TOKEN:
            return httpx.Response(401)
        if path == "/api/vcenter/datacenter":
            return httpx.Response(200, json=[{"datacenter": "datacenter-1", "name": "Nassau"}])
        if path == "/api/vcenter/cluster" and q.get("datacenters") == "datacenter-1":
            return httpx.Response(200, json=[{"cluster": "domain-c1", "name": "Prod", "ha_enabled": True, "drs_enabled": False}])
        if path == "/api/vcenter/host" and q.get("clusters") == "domain-c1":
            return httpx.Response(200, json=[{"host": "host-2", "name": "esx02"}, {"host": "host-1", "name": "esx01"}])
        if path == "/api/vcenter/host" and q.get("datacenters") == "datacenter-1":
            return httpx.Response(200, json=[{"host": h, "name": n, "connection_state": "CONNECTED"}
                                             for h, n in (("host-1", "esx01"), ("host-2", "esx02"), ("host-3", "esx03"))])
        if path == "/api/vcenter/vm" and "hosts" in q:
            return httpx.Response(200, json=[vm(v) for v, (_, h) in vms.items() if h == q["hosts"]])
        if path == "/api/vcenter/vm" and q.get("datacenters") == "datacenter-1":
            return httpx.Response(200, json=[vm(v) for v in vms])
        if path == "/api/vcenter/datastore":
            return httpx.Response(200, json=[{"datastore": "ds-1", "name": "vsan", "type": "VSAN", "capacity": 10, "free_space": 4}])
        if path == "/api/vcenter/network":
            return httpx.Response(200, json=[{"network": "dvportgroup-1", "name": "VLAN20", "type": "DISTRIBUTED_PORTGROUP"}])
        return httpx.Response(404, json={"path": path, "params": dict(q)})
    return httpx.MockTransport(handler)


def test_topology():
    with VSphereConnector(CONFIG, "pw", transport=topology_vcenter()) as vc:
        t = vc.topology()
    assert t["vcenter"] == "vc.example.com"
    [dc] = t["datacenters"]
    [prod] = dc["clusters"]
    assert prod["name"] == "Prod" and prod["ha_enabled"] is True and prod["drs_enabled"] is False
    assert [h["name"] for h in prod["hosts"]] == ["esx01", "esx02"]                 # sorted
    assert [v["name"] for v in prod["hosts"][0]["vms"]] == ["web01"]
    assert [h["name"] for h in dc["standalone_hosts"]] == ["esx03"]                 # not in any cluster
    assert [v["name"] for v in dc["standalone_hosts"][0]["vms"]] == ["edge01"]
    assert [v["name"] for v in dc["other_vms"]] == ["orphan01"]                     # on no host
    assert dc["datastores"][0]["name"] == "vsan" and dc["networks"][0]["type"] == "DISTRIBUTED_PORTGROUP"
