"""In-memory stand-ins for AD and vCenter, for trying the console without a lab.

Data is generated deterministically per connection, and actions (disable,
unlock, power off, ...) change it for as long as the process runs. Some VM
names deliberately match AD computer names and some don't, so the cross-check
view has something to show.
"""

import random
import threading
from datetime import datetime, timedelta, timezone

from .base import ConnectorError
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
