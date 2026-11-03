"""SolarWinds Orion (Platform / Hybrid Cloud Observability) via the SWIS REST API. Read-only.

Queries go to https://<host>:17774/SolarWinds/InformationService/v3/Json/Query as SWQL.

Config keys:
    host        the Orion server (the one running the SolarWinds Information Service)
    port        default 17774
    verify_ssl  verify its certificate; default true (Orion often has a self-signed one)
    username    an Orion web console account (read-only is enough)
The connection secret is the password.
"""

import httpx

from .base import ConnectorError

QUERY = "/SolarWinds/InformationService/v3/Json/Query"

# Orion.StatusInfo codes, grouped the way the Orion web console shows them.
STATUS = {0: "Unknown", 1: "Up", 2: "Down", 3: "Warning", 4: "Shutdown", 5: "Testing", 6: "Dormant",
          7: "Not present", 8: "Lower layer down", 9: "Unmanaged", 10: "Unplugged", 11: "External",
          12: "Unreachable", 14: "Critical", 15: "Partly available", 16: "Misconfigured",
          17: "Could not poll", 19: "Unconfirmed", 22: "Active", 24: "Inactive", 25: "Expired",
          26: "Monitoring disabled", 27: "Disabled", 28: "Not licensed", 29: "Other", 30: "Not running"}
DOWN = {2, 12}
WARNING = {3, 14, 15, 16, 17}
UNMANAGED = {9, 26, 27}
SEVERITY = {0: "Informational", 1: "Warning", 2: "Critical", 3: "Serious", 4: "Notice"}

NODES = ("SELECT NodeID, Caption, IPAddress, Status, StatusDescription, Vendor, MachineType, CPULoad, "
         "PercentMemoryUsed, ResponseTime, PercentLoss, LastBoot FROM Orion.Nodes ORDER BY Caption")
ALERTS = ("SELECT AA.AlertActiveID, AA.TriggeredDateTime, AA.Acknowledged, AA.AcknowledgedBy, "
          "AO.EntityCaption, AO.RelatedNodeCaption, AC.Name, AC.Severity "
          "FROM Orion.AlertActive AA "
          "JOIN Orion.AlertObjects AO ON AA.AlertObjectID = AO.AlertObjectID "
          "JOIN Orion.AlertConfigurations AC ON AO.AlertID = AC.AlertID "
          "ORDER BY AA.TriggeredDateTime DESC")
INTERFACES_DOWN = ("SELECT I.InterfaceID, I.Caption, I.Status, I.StatusDescription, N.Caption AS Node "
                   "FROM Orion.NPM.Interfaces I JOIN Orion.Nodes N ON I.NodeID = N.NodeID "
                   "WHERE I.Status IN (2, 12) ORDER BY N.Caption, I.Caption")


def status_group(code) -> str:
    code = int(code or 0)
    if code == 1:
        return "up"
    if code in DOWN:
        return "down"
    if code in WARNING:
        return "warning"
    if code in UNMANAGED:
        return "unmanaged"
    return "other"


def summarize(nodes: list[dict], alerts: list[dict]) -> dict:
    groups = [n["group"] for n in nodes]
    return {
        "nodes": len(nodes),
        "up": groups.count("up"),
        "down": groups.count("down"),
        "warning": groups.count("warning"),
        "unmanaged": groups.count("unmanaged"),
        "alerts": len(alerts),
        "critical_alerts": sum(1 for a in alerts if a["severity"] in ("Critical", "Serious")),
    }


class SolarWindsConnector:
    def __init__(self, config: dict, secret: str, transport: httpx.BaseTransport | None = None):
        host = config.get("host", "").strip()
        self.username = config.get("username", "").strip()
        if not (host and self.username):
            raise ConnectorError("host and username are required")
        self.host = host
        self.base_url = f"https://{host}:{int(config.get('port') or 17774)}"
        self.verify_ssl = config.get("verify_ssl", True)
        self.password = secret
        self._transport = transport  # tests inject a fake Orion server here
        self._client: httpx.Client | None = None

    def __enter__(self) -> "SolarWindsConnector":
        self._client = httpx.Client(base_url=self.base_url, verify=self.verify_ssl, timeout=30,
                                    auth=(self.username, self.password), transport=self._transport)
        return self

    def __exit__(self, *exc) -> None:
        if self._client:
            self._client.close()

    def query(self, swql: str) -> list[dict]:
        try:
            r = self._client.post(QUERY, json={"query": swql})
        except httpx.HTTPError as e:
            raise ConnectorError(f"Could not reach SolarWinds at {self.base_url}: {e}") from e
        if r.status_code in (401, 403):
            raise ConnectorError("SolarWinds rejected the username or password")
        if r.status_code >= 400:
            try:
                detail = r.json().get("Message") or r.text[:300]
            except ValueError:
                detail = r.text[:300]
            raise ConnectorError(f"SolarWinds returned HTTP {r.status_code}: {detail}")
        return r.json().get("results", [])

    # --- reads -------------------------------------------------------------

    def test(self) -> dict:
        count = self.query("SELECT COUNT(NodeID) AS N FROM Orion.Nodes")
        n = count[0].get("N", 0) if count else 0
        return {"ok": True, "message": f"Connected to SolarWinds on {self.host} ({n} monitored nodes)"}

    def nodes(self) -> list[dict]:
        out = []
        for n in self.query(NODES):
            status = int(n.get("Status") or 0)
            out.append({
                "id": n.get("NodeID"),
                "name": n.get("Caption"),
                "ip": n.get("IPAddress"),
                "status": n.get("StatusDescription") or STATUS.get(status, str(status)),
                "status_code": status,
                "group": status_group(status),
                "vendor": n.get("Vendor"),
                "machine_type": n.get("MachineType"),
                "cpu": _num(n.get("CPULoad")),
                "memory": _num(n.get("PercentMemoryUsed")),
                "response_ms": _num(n.get("ResponseTime")),
                "loss": _num(n.get("PercentLoss")),
                "last_boot": n.get("LastBoot"),
            })
        return out

    def alerts(self) -> list[dict]:
        return [{
            "id": a.get("AlertActiveID"),
            "name": a.get("Name"),
            "severity": SEVERITY.get(int(a.get("Severity") or 0), "Informational"),
            "object": a.get("EntityCaption"),
            "node": a.get("RelatedNodeCaption"),
            "triggered": a.get("TriggeredDateTime"),
            "acknowledged": bool(a.get("Acknowledged")),
            "acknowledged_by": a.get("AcknowledgedBy"),
        } for a in self.query(ALERTS)]

    def interfaces_down(self) -> list[dict] | None:
        """Down interfaces, or None when Network Performance Monitor (Orion.NPM) isn't installed."""
        try:
            rows = self.query(INTERFACES_DOWN)
        except ConnectorError as e:
            if "Orion.NPM" in str(e) or "HTTP 400" in str(e):
                return None
            raise
        return [{"id": i.get("InterfaceID"), "name": i.get("Caption"), "node": i.get("Node"),
                 "status": i.get("StatusDescription") or STATUS.get(int(i.get("Status") or 0), "")} for i in rows]

    def overview(self) -> dict:
        nodes, alerts = self.nodes(), self.alerts()
        return {"server": self.host, "summary": summarize(nodes, alerts), "nodes": nodes, "alerts": alerts,
                "interfaces_down": self.interfaces_down()}

    def summary(self) -> dict:
        return summarize(self.nodes(), self.alerts())


def _num(value):
    try:
        return round(float(value), 1) if value is not None and float(value) >= 0 else None
    except (TypeError, ValueError):
        return None
