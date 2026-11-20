"""Fortinet FortiGate through the FortiOS REST API (6.4 and later). Read-only.

Config keys:
    host        the FortiGate's management address
    port        default 443
    verify_ssl  verify its certificate; default false (FortiGates usually have a self-signed one)
    vdom        default root
The connection secret is a REST API token (System > Administrators > Create New > REST API Admin, with a
read-only profile and the KasTec server as a trusted host).

Live traffic per interface comes from the byte counters: KasTec reads them each time and divides the change
by the time since the last read, so the first read after starting KasTec has no rates yet.
"""

from __future__ import annotations

import ipaddress
import threading
import time

import httpx

from .base import ConnectorError

_counters: dict[str, tuple[float, dict]] = {}   # conn key -> (time, {interface: (rx_bytes, tx_bytes)})
_lock = threading.Lock()


def rates(key: str, counters: dict[str, tuple[int, int]], now: float | None = None) -> dict[str, dict]:
    """Bits per second per interface from two reads of the byte counters (counters that went down = reset)."""
    now = now or time.time()
    with _lock:
        prev = _counters.get(key)
        _counters[key] = (now, counters)
    if not prev or now - prev[0] < 1:
        return {}
    dt, out = now - prev[0], {}
    for name, (rx, tx) in counters.items():
        old = prev[1].get(name)
        if old and rx >= old[0] and tx >= old[1]:
            out[name] = {"rx": (rx - old[0]) * 8 / dt, "tx": (tx - old[1]) * 8 / dt}
    return out


def _cidr(ip: str) -> str | None:
    """'10.50.241.1 255.255.255.0' -> '10.50.241.1/24'; FortiOS shows unset addresses as 0.0.0.0."""
    parts = (ip or "").split()
    if len(parts) != 2 or parts[0] == "0.0.0.0":
        return None
    try:
        return str(ipaddress.ip_interface(f"{parts[0]}/{parts[1]}"))
    except ValueError:
        return None


def parse_interfaces(config: list[dict], live: dict) -> list[dict]:
    out = []
    for c in config:
        name = c.get("name")
        lv = live.get(name, {})
        cidr = _cidr(c.get("ip", ""))
        if not cidr and lv.get("ip") and lv.get("ip") != "0.0.0.0" and lv.get("mask") is not None:
            try:
                cidr = str(ipaddress.ip_interface(f"{lv['ip']}/{lv['mask']}"))
            except ValueError:
                cidr = None
        out.append({
            "name": name, "alias": c.get("alias") or lv.get("alias") or "", "role": c.get("role") or "undefined",
            "type": c.get("type"), "parent": c.get("interface") or None, "vlan": c.get("vlanid") or None,
            "ip": cidr, "network": str(ipaddress.ip_interface(cidr).network) if cidr else None,
            "description": c.get("description") or "", "status": c.get("status", "up"),
            "snmp_index": c.get("snmp-index"),   # NetFlow records name interfaces by this number
            "link": lv.get("link"), "speed": lv.get("speed"),
            "rx_bytes": lv.get("rx_bytes", 0), "tx_bytes": lv.get("tx_bytes", 0),
            "rx_errors": lv.get("rx_errors", 0), "tx_errors": lv.get("tx_errors", 0),
        })
    return out


def parse_routes(results: list[dict]) -> list[dict]:
    return [{"dest": r.get("ip_mask"), "gateway": r.get("gateway"), "interface": r.get("interface"),
             "type": r.get("type"), "distance": r.get("distance")} for r in results or []]


def parse_tunnels(results: list[dict]) -> list[dict]:
    out = []
    for t in results or []:
        p2 = t.get("proxyid") or []
        out.append({"name": t.get("name"), "peer": t.get("rgwy"), "type": t.get("wizard-type") or t.get("type"),
                    "up": any(p.get("status") == "up" for p in p2) if p2 else bool(t.get("connection_count")),
                    "rx_bytes": t.get("incoming_bytes", 0), "tx_bytes": t.get("outgoing_bytes", 0),
                    "subnets": [f"{p.get('p2name')}: {(p.get('proxy_src') or [{}])[0].get('subnet', '')} ↔ "
                                f"{(p.get('proxy_dst') or [{}])[0].get('subnet', '')}" for p in p2]})
    return out


class FortiGateConnector:
    def __init__(self, config: dict, secret: str, transport: httpx.BaseTransport | None = None):
        self.host = (config.get("host") or "").strip()
        if not self.host or not secret:
            raise ConnectorError("The FortiGate address and an API token are required")
        self.base_url = f"https://{self.host}:{int(config.get('port') or 443)}"
        self.verify_ssl = config.get("verify_ssl", False)
        self.vdom = (config.get("vdom") or "root").strip()
        self.token = secret
        self.key = f"{self.host}|{self.vdom}"
        self._transport = transport
        self._client: httpx.Client | None = None

    def __enter__(self) -> "FortiGateConnector":
        self._client = httpx.Client(base_url=self.base_url, verify=self.verify_ssl, timeout=20, transport=self._transport,
                                    headers={"Authorization": f"Bearer {self.token}"})
        return self

    def __exit__(self, *exc) -> None:
        if self._client:
            self._client.close()

    def _get(self, path: str, **params) -> dict:
        try:
            r = self._client.get(path, params={"vdom": self.vdom, **params})
        except httpx.HTTPError as e:
            raise ConnectorError(f"Could not reach the FortiGate at {self.base_url}: {e}") from e
        if r.status_code in (401, 403):
            raise ConnectorError("The FortiGate refused the API token (check the token, its profile, and that "
                                 "the KasTec server is one of the REST API admin's trusted hosts)")
        if r.status_code >= 400:
            raise ConnectorError(f"The FortiGate returned HTTP {r.status_code} for {path}")
        try:
            return r.json()
        except ValueError as e:
            raise ConnectorError(f"The FortiGate sent something that isn't JSON for {path}") from e

    def status(self) -> dict:
        data = self._get("/api/v2/monitor/system/status")
        res = data.get("results") or {}
        return {"hostname": res.get("hostname") or self.host, "model": res.get("model_name") or res.get("model") or "FortiGate",
                "serial": data.get("serial") or res.get("serial"), "version": data.get("version") or res.get("version")}

    def test(self) -> dict:
        s = self.status()
        return {"ok": True, "message": f"Connected to {s['hostname']} ({s['model']}, FortiOS {s['version']}, {s['serial']})"}

    def network(self) -> dict:
        """Status, interfaces with live rates, routes and IPsec tunnels."""
        st = self.status()
        config = self._get("/api/v2/cmdb/system/interface").get("results") or []
        live = self._get("/api/v2/monitor/system/interface", include_vlan="true").get("results") or {}
        ifaces = parse_interfaces([c for c in config if c.get("vdom", self.vdom) == self.vdom], live)
        r = rates(self.key, {i["name"]: (int(i["rx_bytes"] or 0), int(i["tx_bytes"] or 0)) for i in ifaces})
        for i in ifaces:
            i["rate"] = r.get(i["name"])
        try:
            routes = parse_routes(self._get("/api/v2/monitor/router/ipv4").get("results"))
        except ConnectorError:
            routes = []
        try:
            tunnels = parse_tunnels(self._get("/api/v2/monitor/vpn/ipsec").get("results"))
        except ConnectorError:
            tunnels = []
        tr = rates(self.key + "|vpn", {t["name"]: (int(t["rx_bytes"] or 0), int(t["tx_bytes"] or 0)) for t in tunnels})
        for t in tunnels:
            t["rate"] = tr.get(t["name"])
        return {**st, "interfaces": ifaces, "routes": routes, "tunnels": tunnels}

    def summary(self) -> dict:
        n = self.network()
        wan = [i for i in n["interfaces"] if i["role"] == "wan"]
        return {"model": n["model"], "hostname": n["hostname"], "version": n["version"],
                "interfaces": len(n["interfaces"]), "wan_links": len(wan),
                "wan_down": sum(1 for i in wan if i["link"] is False),
                "tunnels": len(n["tunnels"]), "tunnels_down": sum(1 for t in n["tunnels"] if not t["up"])}
