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
    assert o["summary"] == {"nodes": 3, "up": 1, "down": 1, "warning": 0, "unmanaged": 1, "alerts": 2, "critical_alerts": 1}
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
