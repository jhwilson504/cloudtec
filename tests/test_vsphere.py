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
