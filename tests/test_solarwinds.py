"""The SolarWinds connector, against a fake SWIS (SolarWinds Information Service) API."""

import httpx
import pytest

from app.connectors import ConnectorError
from app.connectors.solarwinds import SolarWindsConnector

CONFIG = {"host": "btc-sw-app.btcbahamas.com", "username": "kastec"}


def fake_swis(npm=True, password="pw"):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/SolarWinds/InformationService/v3/Json/Query"
        assert request.url.port == 17774
        if request.headers.get("authorization") != httpx.BasicAuth("kastec", password)._auth_header:
            return httpx.Response(401)
        q = __import__("json").loads(request.content)["query"]
        seen.append(q)
        if "COUNT(NodeID)" in q:
            return httpx.Response(200, json={"results": [{"N": 3}]})
        if "FROM Orion.Nodes ORDER BY" in q:
            return httpx.Response(200, json={"results": [
                {"NodeID": 1, "Caption": "BTCPRODDC1", "IPAddress": "10.50.241.81", "Status": 1, "StatusDescription": "Node status is Up.",
                 "Vendor": "Windows", "MachineType": "Windows 2016 Server", "CPULoad": 12, "PercentMemoryUsed": 61.234,
                 "ResponseTime": 2, "PercentLoss": 0},
                {"NodeID": 2, "Caption": "BSPDRPCFDNS02", "IPAddress": "10.50.241.201", "Status": 2, "StatusDescription": "Node status is Down.",
                 "Vendor": "Windows", "CPULoad": -2, "PercentMemoryUsed": -2, "ResponseTime": -1, "PercentLoss": 100},
                {"NodeID": 3, "Caption": "SMTP1", "Status": 9, "Vendor": "Windows"},
            ]})
        if "Orion.AlertActive" in q:
            return httpx.Response(200, json={"results": [
                {"AlertActiveID": 7, "Name": "Node is down", "Severity": 2, "EntityCaption": "BSPDRPCFDNS02",
                 "RelatedNodeCaption": "BSPDRPCFDNS02", "TriggeredDateTime": "2026-11-03T08:12:00", "Acknowledged": False},
                {"AlertActiveID": 8, "Name": "High CPU", "Severity": 1, "EntityCaption": "jfkvh12", "Acknowledged": True,
                 "AcknowledgedBy": "BTC\\\\twells"},
            ]})
        if "Orion.NPM.Interfaces" in q:
            if not npm:
                return httpx.Response(400, json={"Message": "Source entity [Orion.NPM.Interfaces] not found in catalog"})
            return httpx.Response(200, json={"results": [{"InterfaceID": 5, "Caption": "Gi2/0/2", "Status": 2, "Node": "SW-POINCIANA"}]})
        return httpx.Response(400, json={"Message": f"unexpected query {q}"})
    return httpx.MockTransport(handler), seen


def test_overview():
    transport, _ = fake_swis()
    with SolarWindsConnector(CONFIG, "pw", transport=transport) as sw:
        assert "3 monitored nodes" in sw.test()["message"]
        o = sw.overview()
    nodes = {n["name"]: n for n in o["nodes"]}
    assert nodes["BTCPRODDC1"]["group"] == "up" and nodes["BTCPRODDC1"]["memory"] == 61.2
    assert nodes["BSPDRPCFDNS02"]["group"] == "down"
    assert nodes["BSPDRPCFDNS02"]["cpu"] is None and nodes["BSPDRPCFDNS02"]["response_ms"] is None   # -1/-2 mean "no data"
    assert nodes["SMTP1"]["group"] == "unmanaged" and nodes["SMTP1"]["status"] == "Unmanaged"
    assert o["summary"] == {"nodes": 3, "up": 1, "down": 1, "warning": 0, "unmanaged": 1, "alerts": 2, "critical_alerts": 1,
                            "down_nodes": ["BSPDRPCFDNS02"],
                            "critical": [{"name": "Node is down", "object": "BSPDRPCFDNS02"}]}   # the acknowledged one isn't listed
    assert [a["severity"] for a in o["alerts"]] == ["Critical", "Warning"]
    assert o["alerts"][1]["acknowledged"] is True
    assert o["interfaces_down"] == [{"id": 5, "name": "Gi2/0/2", "node": "SW-POINCIANA", "status": "Down"}]


def test_without_network_performance_monitor_interfaces_are_skipped():
    transport, _ = fake_swis(npm=False)
    with SolarWindsConnector(CONFIG, "pw", transport=transport) as sw:
        assert sw.overview()["interfaces_down"] is None


def test_wrong_password():
    transport, _ = fake_swis(password="right")
    with pytest.raises(ConnectorError, match="rejected the username or password"):
        with SolarWindsConnector(CONFIG, "wrong", transport=transport) as sw:
            sw.test()


def test_unreachable_server_message():
    with pytest.raises(ConnectorError, match="Could not reach SolarWinds at https://127.0.0.1:1"):
        with SolarWindsConnector({"host": "127.0.0.1", "port": 1, "username": "u"}, "pw") as sw:
            sw.nodes()


def test_network_topology_and_interface_traffic():
    def handler(request: httpx.Request) -> httpx.Response:
        q = __import__("json").loads(request.content)["query"]
        if "Orion.Nodes" in q and "Category" in q:
            return httpx.Response(200, json={"results": [
                {"NodeID": 1, "Caption": "SW-CORE-01", "IPAddress": "10.0.19.2", "Vendor": "Cisco", "Category": 1, "Status": 1},
                {"NodeID": 2, "Caption": "FGT-EDGE-01", "IPAddress": "10.0.19.250", "Vendor": "Fortinet", "Category": 1, "Status": 2},
                {"NodeID": 3, "Caption": "BTCPRODDC1", "Category": 2, "Status": 1}]})
        if "Orion.NPM.Interfaces" in q:
            return httpx.Response(200, json={"results": [
                {"InterfaceID": 10, "NodeID": 1, "Name": "Te1/0/1", "Speed": 1e10, "InBps": 5e6, "OutBps": 2e6, "Status": 1},
                {"InterfaceID": 20, "NodeID": 2, "Name": "port3", "Speed": 1e10, "InBps": 2e6, "OutBps": 5e6, "Status": 1, "InPercentUtil": -2}]})
        if "TopologyConnections" in q:
            return httpx.Response(200, json={"results": [
                {"SrcNodeID": 1, "SrcInterfaceID": 10, "DestNodeID": 2, "DestInterfaceID": 20, "LayerType": "L2"},
                {"SrcNodeID": 2, "SrcInterfaceID": 20, "DestNodeID": 1, "DestInterfaceID": 10, "LayerType": "L2"}]})
        return httpx.Response(400, json={"Message": "no such entity"})

    with SolarWindsConnector(CONFIG, "pw", transport=httpx.MockTransport(handler)) as c:
        n = c.network()
    assert [x["category"] for x in n["nodes"]] == ["network", "network", "server"]
    assert n["nodes"][1]["status"] == "down"
    assert n["interfaces"][10]["rx"] == 5e6 and n["interfaces"][20]["in_util"] is None
    assert n["links"] == [{"a_node": 1, "a_if": 10, "b_node": 2, "b_if": 20, "layer": "L2"}]   # reported both ways, kept once
    assert n["npm"] is True


def test_network_without_npm():
    def handler(request):
        q = __import__("json").loads(request.content)["query"]
        if "Orion.Nodes" in q:
            return httpx.Response(200, json={"results": [{"NodeID": 1, "Caption": "SW1", "Category": 1, "Status": 1}]})
        return httpx.Response(400, json={"Message": "Source entity [Orion.NPM.Interfaces] not found"})
    with SolarWindsConnector(CONFIG, "pw", transport=httpx.MockTransport(handler)) as c:
        n = c.network()
    assert n["npm"] is False and n["links"] == [] and n["interfaces"] == {}
