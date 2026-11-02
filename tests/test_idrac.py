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

S = "/redfish/v1/Systems/System.Embedded.1"
PARTS = {
    # Expanded in one go, as iDRAC9 does for $expand.
    f"{S}/Processors": {"Members": [
        {"@odata.id": f"{S}/Processors/CPU.Socket.1", "Socket": "CPU.Socket.1", "Model": "Xeon Gold 6338", "TotalCores": 32,
         "TotalThreads": 64, "MaxSpeedMHz": 4000, "Status": {"Health": "OK", "State": "Enabled"}},
        {"@odata.id": f"{S}/Processors/CPU.Socket.2", "Socket": "CPU.Socket.2", "Status": {"State": "Absent"}}]},
    # Not expanded: the connector has to fetch each member.
    f"{S}/Memory": {"Members": [{"@odata.id": f"{S}/Memory/DIMM.Socket.A10"}, {"@odata.id": f"{S}/Memory/DIMM.Socket.A2"}]},
    f"{S}/Memory/DIMM.Socket.A10": {"DeviceLocator": "DIMM.Socket.A10", "CapacityMiB": 32768, "Status": {"Health": "OK", "State": "Enabled"}},
    f"{S}/Memory/DIMM.Socket.A2": {"DeviceLocator": "DIMM.Socket.A2", "CapacityMiB": 32768, "Status": {"Health": "OK", "State": "Enabled"}},
    f"{S}/Oem/Dell/DellSystem/System.Embedded.1": {"MaxDIMMSlots": 32},
    f"{S}/Storage": {"Members": [
        {"@odata.id": f"{S}/Storage/RAID.SL.3-1", "Id": "RAID.SL.3-1", "Status": {"HealthRollup": "OK"},
         "StorageControllers": [{"Name": "PERC H755 Front", "Model": "PERC H755 Front", "FirmwareVersion": "52.21"}],
         "Oem": {"Dell": {"DellController": {"PersonalityMode": "RAID-Mode"}}},
         "Drives": [{"@odata.id": f"{S}/Storage/RAID.SL.3-1/Drives/Disk.0"}]},
        {"@odata.id": f"{S}/Storage/BOSS.SL.5-1", "Id": "BOSS.SL.5-1",
         "StorageControllers": [{"Model": "BOSS-S2"}], "Drives": []},
        {"@odata.id": f"{S}/Storage/CPU.1", "Id": "CPU.1", "StorageControllers": [{}], "Drives": []}]},
    f"{S}/Storage/RAID.SL.3-1/Drives/Disk.0": {"Name": "SSD 0", "Model": "MZ7", "CapacityBytes": 960197124096, "MediaType": "SSD",
                                              "Oem": {"Dell": {"DellPhysicalDisk": {"RaidStatus": "Online"}}},
                                              "Status": {"Health": "OK", "State": "Enabled"}},
    f"{S}/EthernetInterfaces": {"Members": [
        {"@odata.id": "x", "Id": "NIC.Integrated.1-2-1", "MACAddress": "B4:45:06:00:00:02", "SpeedMbps": 25000, "LinkStatus": "LinkUp"},
        {"@odata.id": "y", "Id": "NIC.Integrated.1-1-1", "MACAddress": "B4:45:06:00:00:01", "SpeedMbps": 0, "LinkStatus": "LinkDown"}]},
    "/redfish/v1/Chassis/System.Embedded.1/NetworkAdapters": {"Members": [
        {"@odata.id": "/a", "Id": "NIC.Integrated.1", "Model": "Broadcom 57414 25Gb", "NetworkPorts": {"@odata.id": "/a/ports"},
         "Controllers": [{"ControllerCapabilities": {"NetworkPortCount": 2}, "FirmwarePackageVersion": "22.31"}]}]},
    "/a/ports": {"Members": [{"@odata.id": "/a/ports/1", "SupportedLinkCapabilities": [{"LinkSpeedMbps": 25000}]}]},
    "/redfish/v1/UpdateService/FirmwareInventory": {"Members": [
        {"@odata.id": "f1", "Id": "Installed-159-1.13.2", "Name": "BIOS", "Version": "1.13.2"},
        {"@odata.id": "f2", "Id": "Previous-159-1.12.0", "Name": "BIOS", "Version": "1.12.0"}]},
}


def fake_idrac(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.host, request.url.path))
        if request.url.host == "10.0.0.3":
            raise httpx.ConnectTimeout("timed out")
        if request.headers.get("authorization") != httpx.BasicAuth("root", "pw")._auth_header:
            return httpx.Response(401)
        if request.url.path in PARTS:
            return httpx.Response(200, json=PARTS[request.url.path])
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


def test_full_hardware_inventory():
    calls = []
    with IDRACConnector(CONFIG, "pw", transport=fake_idrac(calls)) as c:
        inv = c.inventory("10.0.0.1")
        with pytest.raises(ConnectorError, match="isn't one of"):
            c.inventory("10.9.9.9")
    assert inv["errors"] == {}
    assert [p["model"] for p in inv["processors"]] == ["Xeon Gold 6338"]  # empty socket left out
    assert [m["slot"] for m in inv["memory"]] == ["DIMM.Socket.A2", "DIMM.Socket.A10"]
    assert inv["memory_slots"] == {"total": 32, "used": 2}
    perc, boss = inv["storage"]  # the empty CPU-attached controller is left out
    assert perc["kind"] == "PERC RAID controller (RAID-Mode)" and perc["drives"][0]["raid_status"] == "Online"
    assert boss["kind"] == "Boot card (BOSS)" and inv["boot_device"] == "BOSS card"
    assert [n["id"] for n in inv["network"]] == ["NIC.Integrated.1-1-1", "NIC.Integrated.1-2-1"]
    assert inv["nic_adapters"] == [{"id": "NIC.Integrated.1", "model": "Broadcom 57414 25Gb", "manufacturer": None,
                                    "ports": 2, "max_speed_mbps": 25000, "firmware": "22.31"}]
    assert inv["power_supplies"][1]["no_input"]
    assert inv["firmware"] == [{"name": "BIOS", "version": "1.13.2"}]
    assert {m for m, *_ in calls} == {"GET"}


def test_controller_kinds():
    from app.connectors.idrac import _controller_kind
    assert _controller_kind("HBA355i Front", None) == "HBA (pass-through)"
    assert _controller_kind("PERC H755 Front", "eHBA") == "PERC in eHBA (pass-through)"
    assert _controller_kind("PERC H755 Front", "HBA-Mode") == "PERC in HBA-Mode (pass-through)"
