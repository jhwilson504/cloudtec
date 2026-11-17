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

    def group_member_usernames(self, group: str) -> set[str]:
        """The demo directory's 'monitored' group: its first 24 enabled users, whatever the group is called."""
        members = [u for u in self.data["users"] if u["enabled"]][:24]
        self.member_names = {u["username"]: u["display_name"] for u in members}
        return set(self.member_names)

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

    def datastores_with_vms(self) -> list[dict]:
        """ds-vmfs-01 holds the servers, ds-vmfs-02 is empty, the NFS store holds the lab VMs."""
        names = sorted(v["name"] for v in self.data["vms"])
        where = {"ds-vmfs-01": [n for n in names if n.startswith("SRV")], "ds-vmfs-02": [],
                 "ds-nfs-03": [n for n in names if not n.startswith("SRV")]}
        return [{**d, "vms": where.get(d["name"], [])} for d in self.data["datastores"]]

    def clusters(self) -> list[dict]:
        return [dict(c) for c in self.data["clusters"]]

    def clusters_with_hosts(self) -> list[dict]:
        hosts = {"Prod-Cluster": ["esx01.lab.local", "esx02.lab.local"], "Lab-Cluster": ["esx03.lab.local"]}
        return [{**c, "hosts": hosts.get(c["name"], [])} for c in self.data["clusters"]]

    def create_datastore(self, cluster: str, name: str, naa: str, size_gb: int, vmfs: str) -> str:
        if cluster != "Prod-Cluster":
            raise ConnectorError(f"The hosts in {cluster} don't see {naa}: in the demo only Prod-Cluster is in the storage group")
        cap = int(size_gb) * 1024 ** 3
        with _lock:
            self.data["datastores"].append({"id": f"datastore-{100 + len(self.data['datastores'])}", "name": name,
                                            "type": "VMFS", "capacity": cap, "free_space": int(cap * 0.99)})
        return f"Created VMFS-{'5' if vmfs == 'auto' else vmfs} datastore {name} ({size_gb} GB) on esx01.lab.local; esx02.lab.local sees it too."

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
                 ("BTCLOADBALANCER", "Citrix", "NetScaler VPX"), ("JFKNAS", "Synology", "RS3617xs"),
                 # Also some of the demo vCenter's machines, so its Topology tab shows SolarWinds status too.
                 ("SRV-DC01", "Windows", "VMware Virtual Platform"), ("SRV-SQL01", "Windows", "VMware Virtual Platform"),
                 ("SRV-WEB02", "Windows", "VMware Virtual Platform"), ("esx01.lab.local", "VMware", "PowerEdge R750")]
        status = {"BSPDRPCFDNS02": 2, "jfkvh12": 3, "SMTP1": 9, "SRV-WEB02": 2, "SRV-SQL01": 3}
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


# --- VNX: a fake Control Station, so the real parser runs on realistic naviseccli output -----------

def _vnx_output(extra: dict | None = None) -> str:
    """naviseccli output for the demo array, plus LUNs created on it through KasTec (extra)."""
    extra = extra or {"luns": [], "pairs": {}}
    def lun(n, name, uid_tail, gb, pool, thin="Yes", private="No", state="Ready", faulted="false"):
        return (f"LOGICAL UNIT NUMBER {n}\nName:  {name}\nUID:  60:06:01:60:AB:CD:12:00:{uid_tail}\n"
                f"Current Owner:  SP {'AB'[n % 2]}\nUser Capacity (GBs):  {gb:.3f}\n"
                f"Consumed Capacity (GBs):  {gb * (0.35 if thin == 'Yes' else 1.0):.3f}\nPool Name:  {pool}\n"
                f"Current State:  {state}\nStatus:  OK(0x0)\nIs Faulted:  {faulted}\nIs Thin LUN:  {thin}\n"
                f"Is Private:  {private}\n\n")
    luns = [
        (0, "ds-vmfs-01", "11:22:33:44:55:66:77:01", 2048, "Pool 0"),
        (1, "ds-vmfs-02", "11:22:33:44:55:66:77:02", 4096, "Pool 0"),
        (2, "ds-vmfs-retired", "11:22:33:44:55:66:77:03", 1024, "Pool 1"),
        (3, "RDM_SQL_LOGS", "11:22:33:44:55:66:77:04", 500, "Pool 0"),
        (4, "LUN_TEST_07", "11:22:33:44:55:66:77:05", 750, "Pool 1"),
        (5, "OLD_EXCHANGE_DB", "11:22:33:44:55:66:77:06", 1500, "Pool 1"),
        (6, "FILE_LUN_0", "11:22:33:44:55:66:77:07", 2000, "Pool 1"),
        (7, "FILE_LUN_1", "11:22:33:44:55:66:77:08", 2000, "Pool 1"),
    ]
    lun_text = "".join(lun(*x) for x in luns) + lun(8, "Reserved LUN Pool 1", "11:22:33:44:55:66:77:09", 100, "Pool 0", private="Yes")
    lun_text += "".join(lun(n, name, f"11:22:33:44:55:66:{n:02X}:{n:02X}", gb, pool, thin="Yes" if thin else "No")
                        for n, name, gb, pool, thin in extra["luns"])
    pair = lambda group: "".join(f"    {h}               {a}\n" for h, a in extra["pairs"].get(group, []))  # noqa: E731
    pools = (
        "Pool Name:  Pool 0\nPool ID:  0\nRaid Type:  r_5\nPercent Full Threshold:  70\nDisk Type:  Mixed\nState:  Ready\n"
        "Status:  OK(0x0)\nUser Capacity (GBs):  12000.000\nConsumed Capacity (GBs):  8930.250\n"
        "Available Capacity (GBs):  3069.750\nPercent Full:  74.419\nTotal Subscribed Capacity (GBs):  13200.000\n"
        "Percent Subscribed:  110.000\nLUNs:  0, 1, 3, 8\n\n"
        "Pool Name:  Pool 1\nPool ID:  1\nRaid Type:  r_6\nPercent Full Threshold:  70\nDisk Type:  NL SAS\nState:  Ready\n"
        "Status:  OK(0x0)\nUser Capacity (GBs):  20000.000\nConsumed Capacity (GBs):  9200.000\n"
        "Available Capacity (GBs):  10800.000\nPercent Full:  46.000\nTotal Subscribed Capacity (GBs):  12500.000\n"
        "Percent Subscribed:  62.500\nLUNs:  2, 4, 5, 6, 7\n\n")
    groups = (
        "Storage Group Name:    ESX-Prod-Cluster\nStorage Group UID:     AA:BB:CC:01\nHBA/SP Pairs:\n\n"
        "  HBA UID                                          SP Name     SPPort\n"
        "  -------                                          -------     ------\n"
        "  20:00:00:25:B5:00:00:01:21:00:00:25:B5:00:00:01   SP A         0\nHost name:             esx01.lab.local\n"
        "  20:00:00:25:B5:00:00:02:21:00:00:25:B5:00:00:02   SP B         0\nHost name:             esx02.lab.local\n\n"
        "HLU/ALU Pairs:\n\n  HLU Number     ALU Number\n  ----------     ----------\n"
        "    0               0\n    1               1\n    2               2\n    3               3\n" + pair("ESX-Prod-Cluster") +
        "Shareable:             YES\n\n"
        "Storage Group Name:    ~filestorage\nStorage Group UID:     AA:BB:CC:02\nHBA/SP Pairs:\n\n"
        "HLU/ALU Pairs:\n\n  HLU Number     ALU Number\n  ----------     ----------\n"
        "    16              6\n    17              7\nShareable:             YES\n\n")
    return (
        "@@KASTEC sp spa\n"
        "@@KASTEC getagent\nAgent Rev:           7.33.9 (1.89)\nName:                VNX-DEMO\nModel:               VNX5400\n"
        "Serial No:           CKM00DEMO0001\nRevision:            05.33.009.5.236\n"
        "@@KASTEC faults\nFaulted Subsystem: CKM00DEMO0001\nBus 0 Enclosure 1 Disk 7 : Faulted\n"
        f"@@KASTEC pools\n{pools}@@KASTEC luns\n{lun_text}@@KASTEC rglun\n@@KASTEC groups\n{groups}"
        "@@KASTEC slots\n 10 - slot_0 primary control station\n 5 - slot_2 contacted\n 5 - slot_3 contacted\n")


class DemoVNX:
    def __init__(self, conn_id: str, config: dict):
        from .vnx import VNXConnector
        with _lock:
            self.extra = _state.setdefault(f"vnx-{conn_id}", {"luns": [], "pairs": {}})
        self.inner = VNXConnector({"host": "vnx-cs.demo", "username": "nasadmin", "host_key": "SHA256:demo"}, "",
                                  runner=lambda script, key: (0, _vnx_output(self.extra), ""))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def test(self) -> dict:
        return {"ok": True, "message": "Demo VNX5400 (no real Control Station contacted)"}

    def overview(self) -> dict:
        return self.inner.overview()

    def summary(self) -> dict:
        return self.inner.summary()

    def read(self) -> dict:
        return self.inner.read()

    def create_lun(self, name: str, pool: str, size_gb: int, thin: bool, sp: str) -> dict:
        with _lock:
            number = 9 + len(self.extra["luns"])
            self.extra["luns"].append((number, name, int(size_gb), pool, thin))
        return next(lun for lun in self.read()["luns"] if lun["name"] == name)

    def run_command(self, command: str) -> dict:
        from .vnx import split_sections
        sec = split_sections(_vnx_output(self.extra))
        if "getagent" in command:
            return {"output": sec["getagent"], "exit_code": 0}
        if "lun -list" in command:
            return {"output": sec["luns"], "exit_code": 0}
        if "getreason" in command:
            return {"output": sec["slots"], "exit_code": 0}
        return {"output": f"(demo) There's no real Control Station, so this wasn't run: {command}\n"
                          "Try naviseccli -h spa getagent, naviseccli -h spa lun -list or getreason.\n", "exit_code": 0}

    def add_to_group(self, group: str, hlu: int, alu: int) -> None:
        if group != "ESX-Prod-Cluster":
            raise ConnectorError(f"The demo array has no storage group {group}")
        with _lock:
            self.extra["pairs"].setdefault(group, []).append((int(hlu), int(alu)))
