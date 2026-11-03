"""In-memory stand-ins for AD, vCenter and iDRAC, for trying the console without a lab.

Data is generated deterministically per connection, and actions (disable,
unlock, power off, ...) change it for as long as the process runs. Some VM
names deliberately match AD computer names and some don't, so the cross-check
view has something to show.
"""

import random
import threading
from datetime import datetime, timedelta, timezone

from .base import ConnectorError
from .idrac import summarize
from .vsphere import GUEST_ACTIONS, POWER_ACTIONS, _check_vm

FIRST = ["Ava", "Ben", "Chloe", "Dev", "Elena", "Femi", "Grace", "Hiro", "Isla", "Jon",
         "Kira", "Liam", "Maya", "Noah", "Omar", "Priya", "Quinn", "Rosa", "Sam", "Tara"]
LAST = ["Adams", "Brooks", "Chen", "Diaz", "Evans", "Fischer", "Garcia", "Hughes",
        "Ito", "Johnson", "Khan", "Lopez", "Murphy", "Nguyen", "Okafor", "Patel"]
DEPTS = ["IT", "Finance", "HR", "Sales", "Engineering", "Operations"]
ROLES = ["web", "sql", "app", "file", "dc", "print", "build", "mon"]
OSES = [("Windows Server 2022 Standard", "10.0 (20348)"),
        ("Windows Server 2019 Standard", "10.0 (17763)"),
        ("Windows 11 Enterprise", "10.0 (22631)")]

_state: dict[str, dict] = {}
_lock = threading.Lock()


def _ago(rng: random.Random, max_days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=rng.randint(0, max_days),
                                                   minutes=rng.randint(0, 1440))).isoformat()


def _domain_parts(base_dn: str) -> list[str]:
    return [p.split("=", 1)[1] for p in base_dn.split(",") if p.strip().lower().startswith("dc=")]


def _ad_state(key: str, base_dn: str) -> dict:
    with _lock:
        if key in _state:
            return _state[key]
        rng = random.Random(f"ad-{key}")
        domain = ".".join(_domain_parts(base_dn)) or "corp.example.com"
        users = []
        names = rng.sample([(f, l) for f in FIRST for l in LAST], 48)
        for i, (first, last) in enumerate(names):
            username = f"{first[0]}{last}{i}".lower()
            users.append({
                "dn": f"CN={first} {last},OU=Staff,{base_dn}",
                "username": username,
                "display_name": f"{first} {last}",
                "email": f"{username}@{domain}",
                "title": rng.choice(["Analyst", "Engineer", "Manager", "Specialist"]),
                "department": rng.choice(DEPTS),
                "enabled": rng.random() > 0.1,
                "locked": rng.random() < 0.08,
                "last_logon": _ago(rng, 90),
                "created": _ago(rng, 1500),
            })
        groups = [{
            "dn": f"CN={name},OU=Groups,{base_dn}",
            "name": name,
            "description": f"{name} access",
            "members": rng.randint(2, 40),
            "type": "Security" if rng.random() > 0.2 else "Distribution",
        } for name in ["Domain Admins", "VM Operators", "Helpdesk"] + [f"{d} Team" for d in DEPTS]]
        computers = []
        for role in ROLES:
            for n in range(1, rng.randint(2, 4)):
                name = f"SRV-{role.upper()}{n:02d}"
                os_name, os_ver = rng.choice(OSES[:2])
                computers.append(_computer(rng, base_dn, domain, name, os_name, os_ver, "Servers"))
        for n in range(1, 7):
            os_name, os_ver = OSES[2]
            computers.append(_computer(rng, base_dn, domain, f"WS-{n:03d}", os_name, os_ver, "Workstations"))
        _state[key] = {"users": users, "groups": groups, "computers": computers, "domain": domain}
        return _state[key]


def _computer(rng, base_dn, domain, name, os_name, os_ver, ou) -> dict:
    return {
        "dn": f"CN={name},OU={ou},{base_dn}",
        "name": name,
        "dns_name": f"{name.lower()}.{domain}",
        "os": os_name,
        "os_version": os_ver,
        "enabled": True,
        "last_logon": _ago(rng, 30),
    }


def _match(q: str, *fields) -> bool:
    return not q or any(q.lower() in (f or "").lower() for f in fields)


class DemoAD:
    def __init__(self, conn_id: str, config: dict):
        self.data = _ad_state(conn_id, config.get("base_dn") or "DC=corp,DC=example,DC=com")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def test(self) -> dict:
        return {"ok": True, "message": f"Demo directory {self.data['domain']} (no real server contacted)"}

    def summary(self) -> dict:
        users = self.data["users"]
        return {
            "users": len(users),
            "disabled": sum(1 for u in users if not u["enabled"]),
            "locked": sum(1 for u in users if u["locked"]),
            "computers": len(self.data["computers"]),
            "groups": len(self.data["groups"]),
            "capped": False,
        }

    def users(self, q: str = "", limit: int = 500) -> list[dict]:
        return [dict(u) for u in self.data["users"]
                if _match(q, u["username"], u["display_name"], u["email"])][:limit]

    def groups(self, q: str = "", limit: int = 500) -> list[dict]:
        return [dict(g) for g in self.data["groups"] if _match(q, g["name"], g["description"])][:limit]

    def computers(self, q: str = "", limit: int = 2000) -> list[dict]:
        return [dict(c) for c in self.data["computers"]
                if _match(q, c["name"], c["dns_name"], c["os"])][:limit]

    def domain_controllers(self) -> list[dict]:
        sites = ["Nassau-HQ", "Nassau-HQ", "Freeport"]
        out = []
        for i, c in enumerate([c for c in self.data["computers"] if c["name"].startswith("SRV-DC")]):
            out.append({
                "dn": c["dn"], "name": c["name"], "dns_name": c["dns_name"], "os": c["os"],
                "os_version": c["os_version"], "read_only": i == 2, "site": sites[i % len(sites)],
                "global_catalog": i != 2, "last_logon": c["last_logon"],
                "roles": ["PDC", "RID", "Infrastructure", "Domain naming", "Schema"] if i == 0 else [],
                # Sample reachability: the branch DC has AD Web Services blocked.
                "ports": {"389": True, "636": True, "3268": i != 2, "9389": i != 2},
            })
        return out

    def _user(self, dn: str) -> dict:
        for u in self.data["users"]:
            if u["dn"] == dn:
                return u
        raise ConnectorError("Object not found")

    def set_enabled(self, dn: str, enabled: bool) -> None:
        self._user(dn)["enabled"] = enabled

    def unlock(self, dn: str) -> None:
        self._user(dn)["locked"] = False

    def reset_password(self, dn: str, new_password: str, unlock: bool = False, must_change: bool = False) -> None:
        user = self._user(dn)
        if unlock:
            user["locked"] = False
        user["must_change"] = must_change


def _vs_state(key: str) -> dict:
    with _lock:
        if key in _state:
            return _state[key]
        rng = random.Random(f"vs-{key}")
        vms, n = [], 1000
        names = [f"SRV-{r.upper()}{i:02d}" for r in ROLES for i in (1, 2)]
        names += ["template-win2022", "lab-k8s-node1", "lab-k8s-node2", "appliance-backup"]
        for name in names:
            n += rng.randint(1, 9)
            vms.append({
                "id": f"vm-{n}",
                "name": name,
                "power_state": rng.choice(["POWERED_ON"] * 6 + ["POWERED_OFF", "SUSPENDED"]),
                "cpus": rng.choice([2, 4, 8]),
                "memory_mb": rng.choice([4096, 8192, 16384, 32768]),
                "guest_os": "WINDOWS_SERVER_2021" if name.startswith("SRV") else "OTHER_LINUX_64",
            })
        hosts = [{
            "id": f"host-{10 + i}",
            "name": f"esx{i:02d}.lab.local",
            "connection_state": "CONNECTED" if i != 4 else "DISCONNECTED",
            "power_state": "POWERED_ON" if i != 4 else "POWERED_OFF",
        } for i in range(1, 5)]
        stores = []
        for i, kind in enumerate(["VMFS", "VMFS", "NFS"], 1):
            cap = rng.choice([2, 4, 8]) * 1024 ** 4
            stores.append({"id": f"datastore-{20 + i}", "name": f"ds-{kind.lower()}-{i:02d}",
                           "type": kind, "capacity": cap, "free_space": int(cap * rng.uniform(0.08, 0.6))})
        clusters = [{"id": "domain-c8", "name": "Prod-Cluster", "ha_enabled": True, "drs_enabled": True},
                    {"id": "domain-c9", "name": "Lab-Cluster", "ha_enabled": False, "drs_enabled": True}]
        _state[key] = {"vms": vms, "hosts": hosts, "datastores": stores, "clusters": clusters}
        return _state[key]


class DemoVSphere:
    def __init__(self, conn_id: str, config: dict):
        self.data = _vs_state(conn_id)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def test(self) -> dict:
        return {"ok": True, "message": "Demo vCenter 8.0.3 (no real server contacted)"}

    def summary(self) -> dict:
        vms, hosts, stores = self.data["vms"], self.data["hosts"], self.data["datastores"]
        return {
            "vms": len(vms),
            "powered_on": sum(1 for v in vms if v["power_state"] == "POWERED_ON"),
            "hosts": len(hosts),
            "hosts_connected": sum(1 for h in hosts if h["connection_state"] == "CONNECTED"),
            "datastores": len(stores),
            "capacity": sum(s["capacity"] for s in stores),
            "free_space": sum(s["free_space"] for s in stores),
        }

    def vms(self, q: str = "") -> list[dict]:
        keys = ("id", "name", "power_state", "cpus", "memory_mb")
        return sorted(({k: v[k] for k in keys} for v in self.data["vms"] if _match(q, v["name"])),
                      key=lambda v: v["name"].lower())

    def _vm(self, vm_id: str) -> dict:
        _check_vm(vm_id)
        for v in self.data["vms"]:
            if v["id"] == vm_id:
                return v
        raise ConnectorError("VM not found")

    def vm(self, vm_id: str) -> dict:
        v = self._vm(vm_id)
        on = v["power_state"] == "POWERED_ON"
        octet = int(vm_id.split("-")[1]) % 250
        return {
            "id": v["id"], "name": v["name"], "power_state": v["power_state"],
            "guest_os": v["guest_os"], "cpus": v["cpus"], "memory_mb": v["memory_mb"],
            "disks": [{"label": "Hard disk 1", "capacity": 100 * 1024 ** 3}],
            "nics": [{"label": "Network adapter 1", "mac": f"00:50:56:00:{octet:02x}:01",
                      "state": "CONNECTED"}],
            "hostname": v["name"].lower() if on else None,
            "ip_address": f"10.20.0.{octet}" if on else None,
        }

    def hosts(self) -> list[dict]:
        return [dict(h) for h in self.data["hosts"]]

    def datastores(self) -> list[dict]:
        return [dict(d) for d in self.data["datastores"]]

    def clusters(self) -> list[dict]:
        return [dict(c) for c in self.data["clusters"]]

    def topology(self) -> dict:
        """Prod-Cluster (esx01-02), Lab-Cluster (esx03), and esx04 as a disconnected standalone host."""
        hosts = {h["name"]: dict(h) for h in self.data["hosts"]}
        running = ["esx01.lab.local", "esx02.lab.local", "esx03.lab.local"]
        placement = {name: [] for name in hosts}
        keys = ("id", "name", "power_state", "cpus", "memory_mb")
        for i, v in enumerate(sorted(self.data["vms"], key=lambda v: v["name"].lower())):
            target = "esx03.lab.local" if v["name"].startswith(("lab-", "template")) else running[i % 2]
            placement[target].append({k: v[k] for k in keys})
        node = lambda name: {**hosts[name], "vms": placement[name]}  # noqa: E731
        clusters = {c["name"]: dict(c) for c in self.data["clusters"]}
        return {"vcenter": "vcenter.lab.local", "datacenters": [{
            "id": "datacenter-3", "name": "Nassau-DC",
            "clusters": [{**clusters["Lab-Cluster"], "hosts": [node("esx03.lab.local")]},
                         {**clusters["Prod-Cluster"], "hosts": [node("esx01.lab.local"), node("esx02.lab.local")]}],
            "standalone_hosts": [node("esx04.lab.local")],
            "other_vms": [],
            "datastores": [dict(d) for d in self.data["datastores"]],
            "networks": [{"id": "network-11", "name": "Management Network", "type": "STANDARD_PORTGROUP"},
                         {"id": "dvportgroup-21", "name": "Servers-VLAN20", "type": "DISTRIBUTED_PORTGROUP"},
                         {"id": "dvportgroup-22", "name": "Users-VLAN30", "type": "DISTRIBUTED_PORTGROUP"},
                         {"id": "network-12", "name": "VM Network", "type": "STANDARD_PORTGROUP"}],
        }]}

    def power(self, vm_id: str, action: str) -> None:
        v = self._vm(vm_id)
        if action not in POWER_ACTIONS | GUEST_ACTIONS:
            raise ConnectorError(f"Unknown power action {action!r}")
        state = v["power_state"]
        if action in ("stop", "shutdown", "suspend", "reset", "reboot") and state != "POWERED_ON":
            raise ConnectorError("The VM is not powered on")
        if action == "start" and state == "POWERED_ON":
            raise ConnectorError("The VM is already powered on")
        v["power_state"] = {"start": "POWERED_ON", "stop": "POWERED_OFF", "shutdown": "POWERED_OFF",
                            "suspend": "SUSPENDED"}.get(action, state)


def _idrac_servers(key: str) -> list[dict]:
    rng = random.Random(f"idrac-{key}")
    servers = []
    for i in range(1, 7):
        # Sample fault: two hosts have lost input power on one supply (an unplugged cord or tripped PDU).
        dead = {2: 2, 5: 1}.get(i)
        psus = [{"name": f"PS{n}", "health": "Critical" if n == dead else "OK", "state": "Enabled",
                 "input_volts": None if n == dead else 230, "capacity_watts": 1400, "no_input": n == dead}
                for n in (1, 2)]
        tag = "".join(rng.choice("BCDFGHJKLMNPQRSTVWXYZ0123456789") for _ in range(7))
        servers.append({
            "host": f"10.0.10.{10 + i}", "name": f"esx{i:02d}.lab.local", "model": "PowerEdge R750",
            "service_tag": tag, "bios": "1.13.2", "idrac_firmware": "7.10.30.00", "power_state": "On",
            "health": "Critical" if dead else "OK", "cpus": 2, "cpu_model": "Intel(R) Xeon(R) Gold 6338 CPU @ 2.00GHz",
            "memory_gib": rng.choice([512, 768, 1024]), "psus": psus,
            "problems": [f"PS{dead} has no input power"] if dead else [], "error": None,
        })
    return servers


class DemoIDRAC:
    def __init__(self, conn_id: str, config: dict):
        self.data = _idrac_servers(conn_id)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def test(self) -> dict:
        return {"ok": True, "message": f"Demo rack of {len(self.data)} iDRACs (no real server contacted)"}

    def servers(self) -> list[dict]:
        return [{**s, "psus": [dict(p) for p in s["psus"]]} for s in self.data]

    def summary(self) -> dict:
        return summarize(self.data)

    def inventory(self, host: str) -> dict:
        server = next((s for s in self.servers() if s["host"] == host), None)
        if not server:
            raise ConnectorError(f"{host} isn't one of this connection's iDRACs")
        rng = random.Random(f"inv-{host}")
        tag = server["service_tag"]
        dimm_gib = server["memory_gib"] // 16
        sn = lambda n=8: "".join(rng.choice("0123456789ABCDEF") for _ in range(n))  # noqa: E731
        return {
            "server": server,
            "errors": {},
            "processors": [{"slot": f"CPU.Socket.{i}", "model": server["cpu_model"], "cores": 32, "threads": 64,
                            "speed_mhz": 4000, "health": "OK"} for i in (1, 2)],
            "memory": [{"slot": f"DIMM.Socket.{bank}{n}", "size_mib": dimm_gib * 1024, "type": "DDR4",
                        "speed_mhz": 3200, "manufacturer": "Hynix Semiconductor", "part_number": "HMAA8GR7AJR4N-XN",
                        "serial": sn(), "health": "OK"} for bank in "AB" for n in range(1, 9)],
            "memory_slots": {"total": 32, "used": 16},
            "storage": [
                {"name": "BOSS-S2", "model": "BOSS-S2", "kind": "Boot card (BOSS)", "mode": None,
                 "firmware": "2.5.13.4008", "health": "OK", "drives": [
                    {"name": f"Disk {i}", "model": "MTFDDAV480TDS", "manufacturer": "MICRON", "capacity_bytes": 480103981056,
                     "media": "SSD", "protocol": "SATA", "serial": sn(12), "life_left_pct": 99, "raid_status": "Online",
                     "health": "OK"} for i in (0, 1)]},
                {"name": "PERC H755 Front", "model": "PERC H755 Front", "kind": "PERC RAID controller (RAID-Mode)",
                 "mode": "RAID-Mode", "firmware": "52.21.0-4606", "health": "OK",
                 "drives": [{"name": f"Solid State Disk 0:1:{i}", "model": "MZILT1T9HBJR0D3", "manufacturer": "SAMSUNG",
                             "capacity_bytes": 1920383410176, "media": "SSD", "protocol": "SAS", "serial": sn(12),
                             "life_left_pct": 100 - i, "raid_status": "Online", "health": "OK"} for i in range(4)]},
            ],
            "network": [{"name": f"Integrated NIC 1 Port {p} Partition 1", "id": f"NIC.Integrated.1-{p}-1",
                         "mac": "B4:45:06:" + ":".join(sn(2) for _ in range(3)),
                         "speed_mbps": 25000 if p <= 2 else 1000, "link": "LinkUp" if p != 1 else "LinkDown",
                         "health": "OK"} for p in range(1, 5)],
            "nic_adapters": [{"id": "NIC.Integrated.1", "model": "Broadcom Adv. Quad 25Gb Ethernet", "manufacturer": "Broadcom Inc.",
                              "ports": 4, "max_speed_mbps": 25000, "firmware": "22.31.6"}],
            "boot_device": "BOSS card",
            "power_supplies": [{**p, "model": "PWR SPLY,1400W,RDNT,LTON", "serial": f"CN{sn(10)}",
                                "part_number": "0Y1R9GA02", "firmware": "00.1D.9E"} for p in server["psus"]],
            "firmware": [{"name": n, "version": v} for n, v in [
                ("BIOS", server["bios"]), ("Integrated Dell Remote Access Controller", server["idrac_firmware"]),
                ("Broadcom Adv. Quad 25Gb Ethernet", "22.31.6"), ("PERC H755 Front", "52.21.0-4606"),
                ("BOSS-S2", "2.5.13.4008"), ("Power Supply.Slot.1", "00.1D.9E"), ("Power Supply.Slot.2", "00.1D.9E"),
                (f"Lifecycle Controller ({tag})", server["idrac_firmware"])]],
        }


def _solarwinds_state(key: str) -> dict:
    with _lock:
        if key in _state:
            return _state[key]
        rng = random.Random(f"sw-{key}")
        names = [("BTCPRODDC1", "Windows", "VMware Virtual Platform"), ("BTCPRODDC2", "Windows", "VMware Virtual Platform"),
                 ("jfkvh10", "VMware", "PowerEdge R720"), ("jfkvh11", "VMware", "PowerEdge R720"),
                 ("jfkvh12", "VMware", "PowerEdge R910"), ("pdvh10", "VMware", "PowerEdge R720"),
                 ("SW-POINCIANA-VM_ENVIRONMENT", "Cisco", "Catalyst 3850"), ("ESB_Network", "Cisco", "Catalyst 2960X"),
                 ("DATA_RECOVERY", "Cisco", "Catalyst 3850"), ("ESBSQL01", "Windows", "VMware Virtual Platform"),
                 ("BSPDRPCFDNS02", "Windows", "VMware Virtual Platform"), ("SMTP1", "Windows", "VMware Virtual Platform"),
                 ("BTCLOADBALANCER", "Citrix", "NetScaler VPX"), ("JFKNAS", "Synology", "RS3617xs")]
        status = {"BSPDRPCFDNS02": 2, "jfkvh12": 3, "SMTP1": 9}
        nodes = []
        for i, (name, vendor, model) in enumerate(names, 1):
            code = status.get(name, 1)
            nodes.append({
                "id": i, "name": name, "ip": f"10.50.241.{20 + i}" if vendor != "Cisco" else f"10.0.19.{200 + i}",
                "status": {1: "Up", 2: "Down", 3: "Warning", 9: "Unmanaged"}[code], "status_code": code,
                "group": {1: "up", 2: "down", 3: "warning", 9: "unmanaged"}[code],
                "vendor": vendor, "machine_type": model,
                "cpu": None if code == 2 else round(rng.uniform(3, 92 if code == 3 else 45), 1),
                "memory": None if code == 2 else round(rng.uniform(30, 88), 1),
                "response_ms": None if code == 2 else round(rng.uniform(1, 30), 1),
                "loss": 100.0 if code == 2 else 0.0,
                "last_boot": None,
            })
        alerts = [
            {"id": 1, "name": "Node is down", "severity": "Critical", "object": "BSPDRPCFDNS02", "node": "BSPDRPCFDNS02",
             "triggered": "2026-11-03T08:12:00", "acknowledged": False, "acknowledged_by": None},
            {"id": 2, "name": "High CPU utilization", "severity": "Warning", "object": "jfkvh12", "node": "jfkvh12",
             "triggered": "2026-11-03T09:40:00", "acknowledged": True, "acknowledged_by": "BTC\\twells"},
            {"id": 3, "name": "Interface is down", "severity": "Warning", "object": "GigabitEthernet2/0/2",
             "node": "SW-POINCIANA-VM_ENVIRONMENT", "triggered": "2026-11-02T22:05:00", "acknowledged": False,
             "acknowledged_by": None},
        ]
        interfaces = [{"id": 101, "name": "GigabitEthernet2/0/2", "node": "SW-POINCIANA-VM_ENVIRONMENT", "status": "Down"}]
        _state[key] = {"nodes": nodes, "alerts": alerts, "interfaces": interfaces}
        return _state[key]


class DemoSolarWinds:
    def __init__(self, conn_id: str, config: dict):
        self.data = _solarwinds_state(conn_id)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def test(self) -> dict:
        return {"ok": True, "message": f"Demo SolarWinds with {len(self.data['nodes'])} nodes (no real server contacted)"}

    def nodes(self) -> list[dict]:
        return [dict(n) for n in self.data["nodes"]]

    def alerts(self) -> list[dict]:
        return [dict(a) for a in self.data["alerts"]]

    def interfaces_down(self) -> list[dict]:
        return [dict(i) for i in self.data["interfaces"]]

    def overview(self) -> dict:
        from .solarwinds import summarize
        return {"server": "solarwinds.demo", "summary": summarize(self.nodes(), self.alerts()), "nodes": self.nodes(),
                "alerts": self.alerts(), "interfaces_down": self.interfaces_down()}

    def summary(self) -> dict:
        from .solarwinds import summarize
        return summarize(self.nodes(), self.alerts())
