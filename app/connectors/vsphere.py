"""vCenter via the vSphere Automation REST API (vCenter 7.0 U2 and later).

Config keys:
    host        vCenter hostname or IP
    port        default 443
    verify_ssl  verify the vCenter certificate; default true
    username    e.g. administrator@vsphere.local
The connection secret is the password.
"""

import re

import httpx

from .base import ConnectorError

VM_ID = re.compile(r"^vm-\d+$")

# Hard power actions go to /power; guest shutdown/reboot need VMware Tools.
POWER_ACTIONS = {"start", "stop", "reset", "suspend"}
GUEST_ACTIONS = {"shutdown", "reboot"}


class VSphereConnector:
    def __init__(self, config: dict, secret: str, transport: httpx.BaseTransport | None = None):
        host = config.get("host", "").strip()
        self.username = config.get("username", "").strip()
        self.password = secret
        self.verify_ssl = config.get("verify_ssl", True)
        if not (host and self.username):
            raise ConnectorError("host and username are required")
        self.base_url = f"https://{host}:{int(config.get('port') or 443)}"
        self._transport = transport  # tests inject a mock vCenter here
        self._client: httpx.Client | None = None

    def __enter__(self) -> "VSphereConnector":
        self._client = httpx.Client(base_url=self.base_url, verify=self.verify_ssl, timeout=30,
                                    transport=self._transport)
        try:
            r = self._client.post("/api/session", auth=(self.username, self.password))
        except httpx.HTTPError as e:
            self._client.close()
            raise ConnectorError(f"Could not reach vCenter at {self.base_url}: {e}") from e
        if r.status_code == 401:
            self._client.close()
            raise ConnectorError("vCenter rejected the username or password")
        if r.status_code >= 400:
            self._client.close()
            raise ConnectorError(f"vCenter login failed (HTTP {r.status_code})")
        self._client.headers["vmware-api-session-id"] = r.json()
        return self

    def __exit__(self, *exc) -> None:
        if self._client:
            try:
                self._client.delete("/api/session")
            except httpx.HTTPError:
                pass
            self._client.close()

    def _get(self, path: str, **params):
        try:
            r = self._client.get(path, params=params or None)
        except httpx.HTTPError as e:
            raise ConnectorError(f"vCenter request failed: {e}") from e
        if r.status_code >= 400:
            raise ConnectorError(f"vCenter returned HTTP {r.status_code} for {path}: {_error_text(r)}")
        return r.json()

    def _post(self, path: str, **params) -> None:
        try:
            r = self._client.post(path, params=params or None)
        except httpx.HTTPError as e:
            raise ConnectorError(f"vCenter request failed: {e}") from e
        if r.status_code >= 400:
            raise ConnectorError(f"vCenter refused the action: {_error_text(r)}")

    # --- reads -------------------------------------------------------------

    def test(self) -> dict:
        about = self._get("/api/appliance/system/version")
        return {"ok": True, "message": f"Connected to vCenter {about.get('version', '')} build {about.get('build', '')}".strip()}

    def summary(self) -> dict:
        vms = self.vms()
        hosts = self.hosts()
        stores = self.datastores()
        return {
            "vms": len(vms),
            "powered_on": sum(1 for v in vms if v["power_state"] == "POWERED_ON"),
            "hosts": len(hosts),
            "hosts_connected": sum(1 for h in hosts if h["connection_state"] == "CONNECTED"),
            "datastores": len(stores),
            "capacity": sum(s["capacity"] or 0 for s in stores),
            "free_space": sum(s["free_space"] or 0 for s in stores),
        }

    def vms(self, q: str = "") -> list[dict]:
        out = [
            {
                "id": v["vm"],
                "name": v["name"],
                "power_state": v.get("power_state"),
                "cpus": v.get("cpu_count"),
                "memory_mb": v.get("memory_size_MiB"),
            }
            for v in self._get("/api/vcenter/vm")
        ]
        if q:
            out = [v for v in out if q.lower() in v["name"].lower()]
        return sorted(out, key=lambda v: v["name"].lower())

    def vm(self, vm_id: str) -> dict:
        _check_vm(vm_id)
        info = self._get(f"/api/vcenter/vm/{vm_id}")
        detail = {
            "id": vm_id,
            "name": info.get("name"),
            "power_state": info.get("power_state"),
            "guest_os": info.get("guest_OS"),
            "cpus": (info.get("cpu") or {}).get("count"),
            "memory_mb": (info.get("memory") or {}).get("size_MiB"),
            "disks": [
                {"label": d.get("label"), "capacity": d.get("capacity")}
                for d in (info.get("disks") or {}).values()
            ],
            "nics": [
                {"label": n.get("label"), "mac": n.get("mac_address"), "state": n.get("state")}
                for n in (info.get("nics") or {}).values()
            ],
            "hostname": None,
            "ip_address": None,
        }
        # Guest identity only answers when VMware Tools is running.
        try:
            ident = self._get(f"/api/vcenter/vm/{vm_id}/guest/identity")
            detail["hostname"] = ident.get("host_name")
            detail["ip_address"] = ident.get("ip_address")
        except ConnectorError:
            pass
        return detail

    def hosts(self) -> list[dict]:
        return [
            {
                "id": h["host"],
                "name": h["name"],
                "connection_state": h.get("connection_state"),
                "power_state": h.get("power_state"),
            }
            for h in self._get("/api/vcenter/host")
        ]

    def datastores(self) -> list[dict]:
        return [
            {
                "id": d["datastore"],
                "name": d["name"],
                "type": d.get("type"),
                "capacity": d.get("capacity"),
                "free_space": d.get("free_space"),
            }
            for d in self._get("/api/vcenter/datastore")
        ]

    def clusters(self) -> list[dict]:
        return [
            {
                "id": c["cluster"],
                "name": c["name"],
                "ha_enabled": c.get("ha_enabled"),
                "drs_enabled": c.get("drs_enabled"),
            }
            for c in self._get("/api/vcenter/cluster")
        ]

    def topology(self) -> dict:
        """vCenter > datacenters > clusters > hosts > VMs, plus each datacenter's datastores and networks."""
        def vm(v):
            return {"id": v["vm"], "name": v["name"], "power_state": v.get("power_state"),
                    "cpus": v.get("cpu_count"), "memory_mb": v.get("memory_size_MiB")}

        placed: set[str] = set()

        def host(h):
            vms = [vm(v) for v in self._get("/api/vcenter/vm", hosts=h["host"])]
            placed.update(v["id"] for v in vms)
            return {"id": h["host"], "name": h["name"], "connection_state": h.get("connection_state"),
                    "power_state": h.get("power_state"), "vms": sorted(vms, key=lambda v: v["name"].lower())}

        datacenters = []
        for dc in sorted(self._get("/api/vcenter/datacenter"), key=lambda d: d["name"].lower()):
            dc_id = dc["datacenter"]
            clustered: set[str] = set()
            clusters = []
            for c in sorted(self._get("/api/vcenter/cluster", datacenters=dc_id), key=lambda c: c["name"].lower()):
                hosts = sorted(self._get("/api/vcenter/host", clusters=c["cluster"]), key=lambda h: h["name"].lower())
                clustered.update(h["host"] for h in hosts)
                clusters.append({"id": c["cluster"], "name": c["name"], "ha_enabled": c.get("ha_enabled"),
                                 "drs_enabled": c.get("drs_enabled"), "hosts": [host(h) for h in hosts]})
            standalone = [host(h) for h in sorted(self._get("/api/vcenter/host", datacenters=dc_id),
                                                  key=lambda h: h["name"].lower()) if h["host"] not in clustered]
            # VMs vCenter doesn't report on any host (e.g. orphaned or inaccessible)
            other = [vm(v) for v in self._get("/api/vcenter/vm", datacenters=dc_id) if v["vm"] not in placed]
            datacenters.append({
                "id": dc_id, "name": dc["name"], "clusters": clusters, "standalone_hosts": standalone,
                "other_vms": sorted(other, key=lambda v: v["name"].lower()),
                "datastores": sorted(({"id": d["datastore"], "name": d["name"], "type": d.get("type"),
                                       "capacity": d.get("capacity"), "free_space": d.get("free_space")}
                                      for d in self._get("/api/vcenter/datastore", datacenters=dc_id)),
                                     key=lambda d: d["name"].lower()),
                "networks": sorted(({"id": n["network"], "name": n["name"], "type": n.get("type")}
                                    for n in self._get("/api/vcenter/network", datacenters=dc_id)),
                                   key=lambda n: n["name"].lower()),
            })
        return {"vcenter": self.base_url.split("://", 1)[1].rsplit(":", 1)[0], "datacenters": datacenters}

    # --- actions -----------------------------------------------------------

    def power(self, vm_id: str, action: str) -> None:
        _check_vm(vm_id)
        if action in POWER_ACTIONS:
            self._post(f"/api/vcenter/vm/{vm_id}/power", action=action)
        elif action in GUEST_ACTIONS:
            self._post(f"/api/vcenter/vm/{vm_id}/guest/power", action=action)
        else:
            raise ConnectorError(f"Unknown power action {action!r}")


def _check_vm(vm_id: str) -> None:
    if not VM_ID.match(vm_id):
        raise ConnectorError("Invalid VM id")


def _error_text(r: httpx.Response) -> str:
    try:
        body = r.json()
        messages = body.get("messages") or []
        if messages:
            return messages[0].get("default_message", str(body))
        return str(body)[:300]
    except ValueError:
        return r.text[:300]
