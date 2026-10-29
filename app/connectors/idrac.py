"""Dell iDRAC (iDRAC9 and later) via the Redfish API. Read-only: nothing here changes a server.

One connection covers several iDRACs that share an account, e.g. every host in a rack.

Config keys:
    hosts       iDRAC addresses (a list, or one string with one per line / comma-separated)
    verify_ssl  verify each iDRAC certificate; default true (iDRACs ship with a self-signed one)
    username    e.g. root or a read-only iDRAC user
The connection secret is the password.
"""

import re
from concurrent.futures import ThreadPoolExecutor

import httpx

from .base import ConnectorError

SYSTEM = "/redfish/v1/Systems/System.Embedded.1"
MANAGER = "/redfish/v1/Managers/iDRAC.Embedded.1"
POWER = "/redfish/v1/Chassis/System.Embedded.1/Power"
HOST = re.compile(r"^[A-Za-z0-9.:\[\]_-]{1,255}$")
MAX_HOSTS = 64


def parse_hosts(value) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[\s,;]+", value)
    hosts = list(dict.fromkeys(h.strip() for h in value or [] if h and h.strip()))
    bad = [h for h in hosts if not HOST.match(h)]
    if bad:
        raise ConnectorError(f"Not an iDRAC address: {bad[0]}")
    if len(hosts) > MAX_HOSTS:
        raise ConnectorError(f"At most {MAX_HOSTS} iDRACs per connection")
    return hosts


def _health(*values) -> str:
    """The worst of Redfish health values: OK < Warning < Critical. None if none are known."""
    order = {"OK": 0, "Warning": 1, "Critical": 2}
    known = [v for v in values if v in order]
    return max(known, key=order.get) if known else None


def _psu(p: dict) -> dict:
    status = p.get("Status") or {}
    slot = re.search(r"(\d+)$", p.get("MemberId") or "")
    name = f"PS{slot.group(1)}" if slot else (p.get("Name") or "PSU").replace(" Status", "")
    volts = p.get("LineInputVoltage")
    health = status.get("Health")
    return {
        "name": name,
        "health": health,
        "state": status.get("State"),
        "input_volts": volts,
        "capacity_watts": p.get("PowerCapacityWatts"),
        # Dell reports a cabled-but-unpowered PSU as unhealthy with 0 V (or no reading) on its input.
        "no_input": status.get("State") != "Absent" and health not in (None, "OK") and not volts,
    }


class IDRACConnector:
    def __init__(self, config: dict, secret: str, transport: httpx.BaseTransport | None = None):
        self.hosts = parse_hosts(config.get("hosts"))
        self.username = config.get("username", "").strip()
        self.password = secret
        self.verify_ssl = config.get("verify_ssl", True)
        if not (self.hosts and self.username):
            raise ConnectorError("At least one iDRAC address and a username are required")
        self._transport = transport  # tests inject a mock iDRAC here
        self._client: httpx.Client | None = None

    def __enter__(self) -> "IDRACConnector":
        self._client = httpx.Client(verify=self.verify_ssl, timeout=20, transport=self._transport,
                                    auth=(self.username, self.password), headers={"Accept": "application/json"})
        return self

    def __exit__(self, *exc) -> None:
        if self._client:
            self._client.close()

    def _get(self, host: str, path: str) -> dict:
        try:
            r = self._client.get(f"https://{host}{path}")
        except httpx.HTTPError as e:
            if "CERTIFICATE_VERIFY_FAILED" in str(e):
                raise ConnectorError(f"{host}: the iDRAC's certificate isn't trusted. "
                                     "Untick Verify certificate if it still has its self-signed one.") from e
            raise ConnectorError(f"Could not reach iDRAC {host}: {e}") from e
        if r.status_code == 401:
            raise ConnectorError(f"{host}: the iDRAC rejected the username or password")
        if r.status_code >= 400:
            raise ConnectorError(f"{host}: iDRAC returned HTTP {r.status_code} for {path}")
        return r.json()

    def server(self, host: str) -> dict:
        system = self._get(host, SYSTEM)
        manager = self._get(host, MANAGER)
        try:
            psus = [_psu(p) for p in self._get(host, POWER).get("PowerSupplies") or []]
        except ConnectorError:
            psus = []  # some models or firmware don't expose it; the rest is still useful
        status = system.get("Status") or {}
        procs = system.get("ProcessorSummary") or {}
        problems = [f"{p['name']} has no input power" if p["no_input"] else f"{p['name']} is {p['health']}"
                    for p in psus if p["health"] not in (None, "OK") and p["state"] != "Absent"]
        return {
            "host": host,
            "name": system.get("HostName") or host,
            "model": system.get("Model"),
            "service_tag": system.get("SKU") or system.get("SerialNumber"),
            "bios": system.get("BiosVersion"),
            "idrac_firmware": manager.get("FirmwareVersion"),
            "power_state": system.get("PowerState"),
            "health": _health(status.get("HealthRollup"), status.get("Health"),
                              *(p["health"] for p in psus if p["state"] != "Absent")),
            "cpus": procs.get("Count"),
            "cpu_model": procs.get("Model"),
            "memory_gib": (system.get("MemorySummary") or {}).get("TotalSystemMemoryGiB"),
            "psus": psus,
            "problems": problems,
            "error": None,
        }

    # --- reads -------------------------------------------------------------

    def servers(self) -> list[dict]:
        """Every iDRAC in the connection; one that can't be read gets an error instead of sinking the rest."""
        def one(host):
            try:
                return self.server(host)
            except ConnectorError as e:
                return {"host": host, "name": host, "error": str(e), "psus": [], "problems": []}

        with ThreadPoolExecutor(max_workers=min(8, len(self.hosts))) as pool:
            return list(pool.map(one, self.hosts))

    def test(self) -> dict:
        servers = self.servers()
        ok = [s for s in servers if not s["error"]]
        if not ok:
            raise ConnectorError(servers[0]["error"])
        msg = f"Reached {len(ok)} of {len(servers)} iDRAC{'s' if len(servers) != 1 else ''}"
        failed = [s for s in servers if s["error"]]
        return {"ok": True, "message": msg + (f". {failed[0]['error']}" if failed else "")}

    def summary(self) -> dict:
        return summarize(self.servers())


def summarize(servers: list[dict]) -> dict:
    ok = [s for s in servers if not s["error"]]
    return {
        "servers": len(servers),
        "reachable": len(ok),
        "healthy": sum(1 for s in ok if s["health"] == "OK"),
        "attention": sum(1 for s in ok if s["health"] in ("Warning", "Critical")),
        "powered_on": sum(1 for s in ok if s["power_state"] == "On"),
        "psu_problems": sum(1 for s in ok if any(p["health"] not in (None, "OK") and p["state"] != "Absent"
                                                 for p in s["psus"])),
    }
