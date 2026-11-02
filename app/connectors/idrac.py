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
FIRMWARE = "/redfish/v1/UpdateService/FirmwareInventory"
DELL_SYSTEM = f"{SYSTEM}/Oem/Dell/DellSystem/System.Embedded.1"
NIC_ADAPTERS = ("/redfish/v1/Chassis/System.Embedded.1/NetworkAdapters", f"{SYSTEM}/NetworkAdapters")
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

    def _members(self, host: str, path: str) -> list[dict]:
        """A Redfish collection's members in full. Asks iDRAC to expand them in one request,
        and fetches them one by one if it doesn't."""
        sep = "&" if "?" in path else "?"
        members = self._get(host, f"{path}{sep}$expand=*($levels=1)").get("Members") or []
        return [m if len(m) > 1 else self._get(host, m["@odata.id"]) for m in members if m.get("@odata.id")]

    def inventory(self, host: str) -> dict:
        """Every part of one server: CPUs, memory, storage, network, power supplies and firmware.

        Each section is read on its own, so one that the iDRAC can't answer shows an error
        instead of hiding the rest.
        """
        if host not in self.hosts:
            raise ConnectorError(f"{host} isn't one of this connection's iDRACs")
        out = {"server": self.server(host), "errors": {}, "memory_slots": None, "nic_adapters": []}
        sections = {
            "processors": self._processors, "memory": self._memory, "storage": self._storage,
            "network": self._network, "power_supplies": self._power_supplies, "firmware": self._firmware,
        }
        for name, read in sections.items():
            try:
                result = read(host)
            except ConnectorError as e:
                out[name] = []
                out["errors"][name] = str(e)
                continue
            # A section may fill in more than its own list (memory also counts slots, network lists the cards).
            if isinstance(result, dict):
                out.update(result)
            else:
                out[name] = result
        out["boot_device"] = _boot_device(out["storage"], out["firmware"])
        return out

    def _processors(self, host: str) -> list[dict]:
        return [{
            "slot": p.get("Socket") or p.get("Id"),
            "model": p.get("Model"),
            "cores": p.get("TotalCores"),
            "threads": p.get("TotalThreads"),
            "speed_mhz": p.get("MaxSpeedMHz") or p.get("OperatingSpeedMHz"),
            "health": (p.get("Status") or {}).get("Health"),
        } for p in self._members(host, f"{SYSTEM}/Processors") if _present(p)]

    def _memory(self, host: str) -> dict:
        members = self._members(host, f"{SYSTEM}/Memory")
        dimms = [{
            "slot": m.get("DeviceLocator") or m.get("Id"),
            "size_mib": m.get("CapacityMiB"),
            "type": m.get("MemoryDeviceType"),
            "speed_mhz": m.get("OperatingSpeedMhz"),
            "manufacturer": m.get("Manufacturer"),
            "part_number": (m.get("PartNumber") or "").strip() or None,
            "serial": m.get("SerialNumber"),
            "health": (m.get("Status") or {}).get("Health"),
        } for m in members if _present(m)]
        # Dell's own system record knows how many DIMM slots the board has; the Memory list may
        # only include the filled ones.
        total = None
        try:
            dell = (self._get(host, SYSTEM).get("Oem") or {}).get("Dell", {}).get("DellSystem") or self._get(host, DELL_SYSTEM)
            total = dell.get("MaxDIMMSlots")
        except ConnectorError:
            pass
        if not total and len(members) > len(dimms):
            total = len(members)
        return {"memory": sorted(dimms, key=lambda d: _natural(d["slot"])),
                "memory_slots": {"total": total, "used": len(dimms)} if total else None}

    def _storage(self, host: str) -> list[dict]:
        controllers = []
        for st in self._members(host, f"{SYSTEM}/Storage"):
            ctrl = (st.get("StorageControllers") or [{}])[0]
            drives = []
            for link in st.get("Drives") or []:
                d = self._get(host, link["@odata.id"])
                if not _present(d):
                    continue
                dell_disk = (d.get("Oem") or {}).get("Dell", {}).get("DellPhysicalDisk") or {}
                drives.append({
                    "name": d.get("Name") or d.get("Id"),
                    "model": d.get("Model"),
                    "manufacturer": (d.get("Manufacturer") or "").strip() or None,
                    "capacity_bytes": d.get("CapacityBytes"),
                    "media": d.get("MediaType"),
                    "protocol": d.get("Protocol"),
                    "serial": d.get("SerialNumber"),
                    "life_left_pct": d.get("PredictedMediaLifeLeftPercent"),
                    # "Online" = part of a RAID virtual disk, "NonRAID" = passed straight through to the OS.
                    "raid_status": dell_disk.get("RaidStatus"),
                    "health": (d.get("Status") or {}).get("Health"),
                })
            if not drives and not ctrl.get("Model"):
                continue  # empty placeholder controllers (e.g. CPU-attached slots with nothing in them)
            dell_ctrl = (st.get("Oem") or {}).get("Dell", {}).get("DellController") or {}
            controllers.append({
                "name": ctrl.get("Name") or st.get("Name") or st.get("Id"),
                "model": ctrl.get("Model"),
                "kind": _controller_kind(ctrl.get("Model") or st.get("Name") or "", dell_ctrl.get("PersonalityMode")),
                "mode": dell_ctrl.get("PersonalityMode"),
                "firmware": ctrl.get("FirmwareVersion"),
                "health": (st.get("Status") or {}).get("HealthRollup") or (st.get("Status") or {}).get("Health"),
                "drives": drives,
            })
        return controllers

    def _network(self, host: str) -> dict:
        ports = [{
            "name": n.get("Description") or n.get("Name") or n.get("Id"),
            "id": n.get("Id"),
            "mac": n.get("MACAddress") or n.get("PermanentMACAddress"),
            "speed_mbps": n.get("SpeedMbps"),
            "link": n.get("LinkStatus"),
            "health": (n.get("Status") or {}).get("Health"),
        } for n in self._members(host, f"{SYSTEM}/EthernetInterfaces") if _present(n)]
        try:
            adapters = self._nic_adapters(host)
        except ConnectorError:
            adapters = []  # older firmware: the ports above still say what's there
        return {"network": sorted(ports, key=lambda p: _natural(p["id"])), "nic_adapters": adapters}

    def _nic_adapters(self, host: str) -> list[dict]:
        """Each network card: model, number of ports and the fastest speed its ports support."""
        for path in NIC_ADAPTERS:
            try:
                members = self._members(host, path)
            except ConnectorError:
                continue
            out = []
            for a in members:
                ctrl = (a.get("Controllers") or [{}])[0]
                speeds = []
                for link in ("Ports", "NetworkPorts"):
                    ref = (a.get(link) or {}).get("@odata.id")
                    if not ref:
                        continue
                    try:
                        for port in self._members(host, ref):
                            if port.get("MaxSpeedGbps"):
                                speeds.append(port["MaxSpeedGbps"] * 1000)
                            speeds += [c.get("LinkSpeedMbps") or 0 for c in port.get("SupportedLinkCapabilities") or []]
                    except ConnectorError:
                        continue
                    if speeds:
                        break
                out.append({
                    "id": a.get("Id"),
                    "model": a.get("Model") or a.get("Name"),
                    "manufacturer": a.get("Manufacturer"),
                    "ports": (ctrl.get("ControllerCapabilities") or {}).get("NetworkPortCount"),
                    "max_speed_mbps": max(speeds) if speeds else None,
                    "firmware": ctrl.get("FirmwarePackageVersion"),
                })
            return sorted(out, key=lambda a: _natural(a["id"]))
        return []

    def _power_supplies(self, host: str) -> list[dict]:
        out = []
        for p in self._get(host, POWER).get("PowerSupplies") or []:
            out.append({**_psu(p), "model": p.get("Model"), "serial": p.get("SerialNumber"),
                        "part_number": p.get("PartNumber"), "firmware": p.get("FirmwareVersion")})
        return out

    def _firmware(self, host: str) -> list[dict]:
        # iDRAC also lists the previous version of each part ("Previous-..."); only what's running counts.
        items = [{"name": f.get("Name"), "version": f.get("Version")}
                 for f in self._members(host, FIRMWARE) if (f.get("Id") or "").startswith("Installed-")]
        return sorted(items, key=lambda f: (f["name"] or "").lower())

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


def _present(item: dict) -> bool:
    return (item.get("Status") or {}).get("State") != "Absent"


def _controller_kind(model: str, personality: str | None) -> str:
    """Plain-words type of a storage controller. vSAN needs an HBA or a controller in HBA/pass-through mode."""
    m, mode = model.upper(), (personality or "").upper()
    if "BOSS" in m:
        return "Boot card (BOSS)"
    if "HBA" in m:
        return "HBA (pass-through)"
    if "PERC" in m:
        if "HBA" in mode:
            return f"PERC in {personality} (pass-through)"
        return f"PERC RAID controller{f' ({personality})' if personality else ''}"
    if any(k in m for k in ("S150", "S160", "AHCI", "SATA")):
        return "Onboard SATA / software RAID"
    return "Storage controller"


def _boot_device(storage: list[dict], firmware: list[dict]) -> str | None:
    if any("BOSS" in (c.get("model") or c.get("name") or "").upper() for c in storage):
        return "BOSS card"
    if any(k in (f.get("name") or "").upper() for f in firmware for k in ("IDSDM", "DUAL SD")):
        return "Internal SD cards (IDSDM)"
    return None


def _natural(text) -> list:
    """Sort key that puts A2 before A10."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text or "")]


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
