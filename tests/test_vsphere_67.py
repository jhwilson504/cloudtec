"""vCenter 6.5/6.7, which only has the older /rest form of the API (like BTC's jfkvcs)."""

import base64

import httpx
import pytest

from app.connectors import ConnectorError
from app.connectors.vsphere import VSphereConnector

CONFIG = {"host": "jfkvcs.btcbahamas.com", "username": "BTCBAHAMAS.COM\\A-Jovan.Wilson", "verify_ssl": False}
HOSTS = [{"host": "host-10", "name": "jfkvh10.btcbahamas.com", "connection_state": "CONNECTED", "power_state": "POWERED_ON"},
         {"host": "host-12", "name": "jfkvh12.btcbahamas.com", "connection_state": "CONNECTED", "power_state": "POWERED_ON"}]
VMS = {"host-10": [{"vm": "vm-1", "name": "BTCPRODDC1", "power_state": "POWERED_ON", "cpu_count": 4, "memory_size_MiB": 8192}],
       "host-12": [{"vm": "vm-2", "name": "CCURE2K8", "power_state": "POWERED_OFF", "cpu_count": 1, "memory_size_MiB": 4096}]}


def fake_vcenter_67(password="pw", seen=None):
    seen = seen if seen is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, dict(request.url.params)
        seen.append((request.method, path, params))
        if path == "/api/session":
            return httpx.Response(500, json={"type": "com.vmware.vapi.std.errors.internal_server_error"})
        if path == "/rest/com/vmware/cis/session":
            if request.method == "DELETE":
                return httpx.Response(200)
            user, _, pw = base64.b64decode(request.headers["authorization"].split()[1]).decode().partition(":")
            if (user, pw) != (CONFIG["username"], password):
                return httpx.Response(401, json={"type": "com.vmware.vapi.std.errors.unauthenticated"})
            return httpx.Response(200, json={"value": "token-67"})
        assert request.headers.get("vmware-api-session-id") == "token-67"
        assert not any(k in params for k in ("hosts", "clusters", "datacenters", "datastores")), "6.7 wants filter.*"
        value = None
        if path == "/rest/appliance/system/version":
            value = {"version": "6.7.0", "build": "19299595"}
        elif path == "/rest/vcenter/host":
            value = [h for h in HOSTS if params.get("filter.clusters") in (None, "domain-c1")]
        elif path == "/rest/vcenter/vm":
            host = params.get("filter.hosts")
            value = VMS.get(host, []) if host else VMS["host-10"] + VMS["host-12"]
        elif path == "/rest/vcenter/vm/vm-1":
            value = {"name": "BTCPRODDC1", "power_state": "POWERED_ON", "guest_OS": "WINDOWS_9_SERVER_64",
                     "cpu": {"count": 4}, "memory": {"size_MiB": 8192},
                     "disks": [{"key": "2000", "value": {"label": "Hard disk 1", "capacity": 85899345920}}],
                     "nics": [{"key": "4000", "value": {"label": "Network adapter 1", "mac_address": "00:50:56:a1:00:01", "state": "CONNECTED"}}]}
        elif path == "/rest/vcenter/vm/vm-1/guest/identity":
            value = {"host_name": "BTCPRODDC1.btcbahamas.com", "ip_address": "10.50.241.81"}
        elif path == "/rest/vcenter/datastore":
            value = [{"datastore": "datastore-1", "name": "JFK_VNX_BTCPRODDC1", "type": "VMFS", "capacity": 100, "free_space": 30}]
        elif path == "/rest/vcenter/cluster":
            value = [{"cluster": "domain-c1", "name": "JFK", "ha_enabled": True, "drs_enabled": False}]
        elif path == "/rest/vcenter/datacenter":
            value = [{"datacenter": "datacenter-2", "name": "BTC"}]
        elif path == "/rest/vcenter/network":
            value = [{"network": "network-5", "name": "VCMS-New", "type": "STANDARD_PORTGROUP"}]
        elif path == "/rest/vcenter/vm/vm-2/power/start" and request.method == "POST":
            return httpx.Response(200)
        if value is None:
            return httpx.Response(404, json={"type": "com.vmware.vapi.std.errors.not_found"})
        return httpx.Response(200, json={"value": value})

    return httpx.MockTransport(handler)


def test_signs_in_with_rest_and_reads_everything():
    seen = []
    with VSphereConnector(CONFIG, "pw", transport=fake_vcenter_67(seen=seen)) as c:
        assert c.legacy
        assert c.test()["message"] == "Connected to vCenter 6.7.0 build 19299595"
        s = c.summary()
        assert (s["vms"], s["powered_on"], s["hosts"], s["datastores"], s["free_space"]) == (2, 1, 2, 1, 30)
        vm = c.vm("vm-1")
        assert vm["disks"] == [{"label": "Hard disk 1", "capacity": 85899345920}]
        assert vm["nics"][0]["mac"] == "00:50:56:a1:00:01" and vm["ip_address"] == "10.50.241.81"
        t = c.topology()
        hosts = t["datacenters"][0]["clusters"][0]["hosts"]
        assert [h["name"] for h in hosts] == ["jfkvh10.btcbahamas.com", "jfkvh12.btcbahamas.com"]
        assert [v["name"] for v in hosts[1]["vms"]] == ["CCURE2K8"]          # filter.hosts worked
        assert c.clusters_with_hosts()[0]["hosts"] == ["jfkvh10.btcbahamas.com", "jfkvh12.btcbahamas.com"]
        c.power("vm-2", "start")
    assert ("POST", "/rest/vcenter/vm/vm-2/power/start", {}) in seen
    assert ("DELETE", "/rest/com/vmware/cis/session", {}) in seen


def test_wrong_password_on_67():
    with pytest.raises(ConnectorError, match="rejected the username or password"):
        with VSphereConnector(CONFIG, "nope", transport=fake_vcenter_67()):
            pass
