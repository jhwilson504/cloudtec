"""The FortiGate connector against a fake FortiOS REST API."""

import httpx
import pytest

from app.connectors import ConnectorError
from app.connectors import fortigate
from app.connectors.fortigate import FortiGateConnector

CONFIG = {"host": "10.0.19.250", "vdom": "root"}


def fake_fortios(token="tok", counters=None, seen=None):
    counters = counters if counters is not None else {"wan1": [1_000_000, 500_000]}
    seen = seen if seen is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.url.params)))
        if request.headers.get("authorization") != f"Bearer {token}":
            return httpx.Response(401, json={"http_status": 401})
        p = request.url.path
        if p == "/api/v2/monitor/system/status":
            return httpx.Response(200, json={"serial": "FG6H0E0000001", "version": "v7.2.8",
                                             "results": {"hostname": "FGT-BTC-EDGE", "model_name": "FortiGate"}})
        if p == "/api/v2/cmdb/system/interface":
            return httpx.Response(200, json={"results": [
                {"name": "wan1", "vdom": "root", "role": "wan", "ip": "203.0.113.10 255.255.255.248", "alias": "Cable Bahamas", "type": "physical"},
                {"name": "port3", "vdom": "root", "role": "lan", "ip": "10.50.241.1 255.255.255.0", "type": "physical"},
                {"name": "vlan40", "vdom": "root", "role": "lan", "ip": "0.0.0.0 0.0.0.0", "interface": "port3", "vlanid": 40, "type": "vlan"},
                {"name": "other-vdom", "vdom": "dmz-vdom", "role": "lan", "ip": "10.9.9.1 255.255.255.0"}]})
        if p == "/api/v2/monitor/system/interface":
            assert dict(request.url.params).get("include_vlan") == "true"
            return httpx.Response(200, json={"results": {
                "wan1": {"name": "wan1", "link": True, "speed": 1000, "rx_bytes": counters["wan1"][0], "tx_bytes": counters["wan1"][1]},
                "port3": {"name": "port3", "link": True, "speed": 10000, "rx_bytes": 10, "tx_bytes": 20},
                "vlan40": {"name": "vlan40", "link": False, "ip": "10.40.0.1", "mask": 24, "rx_bytes": 0, "tx_bytes": 0}}})
        if p == "/api/v2/monitor/router/ipv4":
            return httpx.Response(200, json={"results": [{"ip_mask": "0.0.0.0/0", "gateway": "203.0.113.9", "interface": "wan1", "type": "static", "distance": 10}]})
        if p == "/api/v2/monitor/vpn/ipsec":
            return httpx.Response(200, json={"results": [{"name": "to-Curacao", "rgwy": "192.0.2.50", "incoming_bytes": 100, "outgoing_bytes": 200,
                                                          "proxyid": [{"p2name": "p2", "status": "up", "proxy_src": [{"subnet": "10.50.241.0/24"}],
                                                                       "proxy_dst": [{"subnet": "10.60.0.0/16"}]}]}]})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_network_and_live_rates(monkeypatch):
    fortigate._counters.clear()
    clock = {"now": 1000.0}
    monkeypatch.setattr(fortigate.time, "time", lambda: clock["now"])
    counters = {"wan1": [1_000_000, 500_000]}
    seen = []
    with FortiGateConnector(CONFIG, "tok", transport=fake_fortios(counters=counters, seen=seen)) as c:
        n = c.network()
        assert (n["hostname"], n["serial"], n["version"]) == ("FGT-BTC-EDGE", "FG6H0E0000001", "v7.2.8")
        ifs = {i["name"]: i for i in n["interfaces"]}
        assert set(ifs) == {"wan1", "port3", "vlan40"}                        # other VDOMs left out
        assert ifs["wan1"]["ip"] == "203.0.113.10/29" and ifs["wan1"]["network"] == "203.0.113.8/29" and ifs["wan1"]["role"] == "wan"
        assert ifs["vlan40"]["ip"] == "10.40.0.1/24" and ifs["vlan40"]["parent"] == "port3" and ifs["vlan40"]["vlan"] == 40
        assert ifs["wan1"]["rate"] is None                                    # first read: no rate yet
        assert n["routes"][0]["gateway"] == "203.0.113.9"
        assert n["tunnels"][0]["up"] and n["tunnels"][0]["peer"] == "192.0.2.50"
        counters["wan1"] = [1_000_000 + 12_500_000, 500_000 + 1_250_000]     # +12.5 MB in, +1.25 MB out in 10 s
        clock["now"] = 1010.0
        n2 = c.network()
        r = {i["name"]: i for i in n2["interfaces"]}["wan1"]["rate"]
        assert r == {"rx": 10_000_000.0, "tx": 1_000_000.0}                  # 10 Mbps in, 1 Mbps out
    assert all(params.get("vdom") == "root" for _, params in seen)


def test_wrong_token():
    with pytest.raises(ConnectorError, match="refused the API token"):
        with FortiGateConnector(CONFIG, "nope", transport=fake_fortios()) as c:
            c.test()


def test_counter_reset_gives_no_rate():
    fortigate._counters.clear()
    fortigate.rates("k", {"x": (1000, 1000)}, now=1.0)
    assert fortigate.rates("k", {"x": (10, 10)}, now=11.0) == {}             # the FortiGate restarted
