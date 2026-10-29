"""The iDRAC connector, against a mocked Redfish API."""

import httpx
import pytest

from app.connectors import ConnectorError
from app.connectors.idrac import IDRACConnector, parse_hosts

CONFIG = {"hosts": ["10.0.0.1", "10.0.0.2", "10.0.0.3"], "username": "root"}

SYSTEM = {"Model": "PowerEdge R750", "SKU": "ABC1234", "HostName": "esx01.lab.local", "BiosVersion": "1.13.2",
          "PowerState": "On", "Status": {"Health": "OK", "HealthRollup": "Critical", "State": "Enabled"},
          "ProcessorSummary": {"Count": 2, "Model": "Xeon"}, "MemorySummary": {"TotalSystemMemoryGiB": 512}}
POWER = {"PowerSupplies": [
    {"MemberId": "PSU.Slot.1", "Name": "PS1 Status", "LineInputVoltage": 230, "Status": {"Health": "OK", "State": "Enabled"}},
    {"MemberId": "PSU.Slot.2", "Name": "PS2 Status", "LineInputVoltage": 0, "Status": {"Health": "Critical", "State": "Enabled"}},
]}


def fake_idrac(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.host, request.url.path))
        if request.url.host == "10.0.0.3":
            raise httpx.ConnectTimeout("timed out")
        if request.headers.get("authorization") != httpx.BasicAuth("root", "pw")._auth_header:
            return httpx.Response(401)
        return {
            "/redfish/v1/Systems/System.Embedded.1": lambda: httpx.Response(200, json=SYSTEM),
            "/redfish/v1/Managers/iDRAC.Embedded.1": lambda: httpx.Response(200, json={"FirmwareVersion": "7.10.30.00"}),
            "/redfish/v1/Chassis/System.Embedded.1/Power": lambda: httpx.Response(200, json=POWER),
        }.get(request.url.path, lambda: httpx.Response(404))()
    return httpx.MockTransport(handler)


def test_inventory_and_power_supply_fault():
    calls = []
    with IDRACConnector(CONFIG, "pw", transport=fake_idrac(calls)) as c:
        servers = c.servers()
        summary = c.summary()
    s = servers[0]
    assert (s["name"], s["model"], s["service_tag"], s["idrac_firmware"]) == (
        "esx01.lab.local", "PowerEdge R750", "ABC1234", "7.10.30.00")
    assert s["health"] == "Critical"
    assert [p["name"] for p in s["psus"]] == ["PS1", "PS2"]
    assert s["psus"][1]["no_input"] and not s["psus"][0]["no_input"]
    assert s["problems"] == ["PS2 has no input power"]
    assert "Could not reach" in servers[2]["error"]
    assert summary == {"servers": 3, "reachable": 2, "healthy": 0, "attention": 2, "powered_on": 2, "psu_problems": 2}
    assert {m for m, *_ in calls} == {"GET"}  # read-only


def test_test_reports_partial_and_bad_password():
    with IDRACConnector(CONFIG, "pw", transport=fake_idrac([])) as c:
        assert c.test()["message"].startswith("Reached 2 of 3 iDRACs")
    with pytest.raises(ConnectorError, match="rejected the username or password"):
        with IDRACConnector({**CONFIG, "hosts": ["10.0.0.1"]}, "wrong", transport=fake_idrac([])) as c:
            c.test()


def test_parse_hosts():
    assert parse_hosts("10.0.0.1, 10.0.0.2\n10.0.0.1\n") == ["10.0.0.1", "10.0.0.2"]
    with pytest.raises(ConnectorError):
        parse_hosts(["10.0.0.1/evil path"])
    with pytest.raises(ConnectorError, match="required"):
        IDRACConnector({"hosts": [], "username": "root"}, "pw")
