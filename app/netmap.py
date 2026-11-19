"""The vCenter network map: how hosts, virtual switches, port groups and VMs connect, and the traffic on each link.

vCenter's REST API doesn't describe virtual switches, physical NICs or traffic, so this reads them with
VMware PowerCLI on the KasTec computer, in a PowerShell session kept open per vCenter (signing in once,
not on every refresh):

  topology (every few minutes): each host's physical NICs (vmnic) with link speed and the physical switch
      and port behind them (CDP/LLDP), standard vSwitches and distributed switches with their uplinks,
      port groups with VLANs, VMkernel ports with their services, and which port group each VM NIC uses.
  traffic (every 20 seconds, vCenter's real-time interval): received/transmitted rate of each physical NIC
      and each powered-on VM (Get-Stat net.received.average / net.transmitted.average, KBps).

Read-only: nothing on vCenter or the hosts is changed.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from pathlib import Path

from .shell import PowerShellSession, ps_quote

TOPOLOGY_TTL = 300
TRAFFIC_TTL = 15


class NetMapError(Exception):
    pass


def _connect(conn) -> str:
    cfg = conn.config
    cert = "Ignore" if cfg.get("verify_ssl") is False else "Fail"
    return f"""$ErrorActionPreference = 'Stop'
if (-not (Get-Module -ListAvailable -Name VMware.VimAutomation.Core)) {{
  throw 'VMware PowerCLI is not installed on the KasTec computer. In an Administrator PowerShell window there run: Install-Module VCF.PowerCLI -Scope AllUsers'
}}
if (-not (Get-Module VMware.VimAutomation.Core)) {{ Import-Module VMware.VimAutomation.Core -WarningAction SilentlyContinue }}
if (-not $global:KasTecNet -or -not $global:KasTecNet.IsConnected) {{
  Set-PowerCLIConfiguration -Scope Session -InvalidCertificateAction {cert} -ParticipateInCeip $false -Confirm:$false | Out-Null
  $__cred = New-Object System.Management.Automation.PSCredential({ps_quote(cfg.get("username"))}, (ConvertTo-SecureString {ps_quote(conn.secret)} -AsPlainText -Force))
  $global:KasTecNet = Connect-VIServer -Server {ps_quote(cfg["host"])} -Port {int(cfg.get("port") or 443)} -Credential $__cred
  Remove-Variable __cred
}}
$vi = $global:KasTecNet
"""


TOPOLOGY = r"""
# Real inventories have gaps: vSwitches with no uplinks, VMs with no host, hosts with no distributed switch.
# K looks a key up safely (a missing key gives nothing), and L turns a value that may be missing into a list.
function K($table, $key) { if ($null -ne $key -and $table.ContainsKey($key)) { $table[$key] } }
function L($items) { @($items | Where-Object { $null -ne $_ }) }

$clusters = @{}; foreach ($c in L (Get-View -Server $vi -ViewType ClusterComputeResource -Property Name)) { $clusters[$c.MoRef.Value] = $c.Name }
$dvsNames = @{}
foreach ($d in L (Get-View -Server $vi -ViewType DistributedVirtualSwitch -Property Name, Uuid)) {
  $dvsNames[$d.MoRef.Value] = $d.Name
  if ($d.Uuid) { $dvsNames[$d.Uuid] = $d.Name }
}
$dvpgs = @{}
foreach ($p in L (Get-View -Server $vi -ViewType DistributedVirtualPortgroup -Property Name, Key, Config)) {
  if (-not $p.Key) { continue }
  $vlan = $p.Config.DefaultPortConfig.Vlan
  $vid = if ($vlan.VlanId -is [array]) { (L $vlan.VlanId | ForEach-Object { "$($_.Start)-$($_.End)" }) -join ',' } else { "$($vlan.VlanId)" }
  $dvpgs[$p.Key] = [ordered]@{ key = $p.Key; name = $p.Name; switch = (K $dvsNames $p.Config.DistributedVirtualSwitch.Value); vlan = $vid; uplink = [bool]$p.Config.Uplink }
}
$hostNames = @{}; $hosts = @()
foreach ($h in L (Get-View -Server $vi -ViewType HostSystem -Property Name, Parent, Runtime.ConnectionState, Config.Product.Version, Config.Network, Config.VirtualNicManagerInfo, ConfigManager.NetworkSystem)) {
  $hostNames[$h.MoRef.Value] = $h.Name
  $net = $h.Config.Network
  $pnicByKey = @{}; foreach ($p in L $net.Pnic) { if ($p.Key) { $pnicByKey[$p.Key] = $p.Device } }
  $hints = @{}
  if ("$($h.Runtime.ConnectionState)" -eq 'connected' -and $h.ConfigManager.NetworkSystem) {
    try { foreach ($hint in L (Get-View -Server $vi $h.ConfigManager.NetworkSystem).QueryNetworkHint($null)) { if ($hint.Device) { $hints[$hint.Device] = $hint } } } catch {}
  }
  $services = @{}
  foreach ($nc in L $h.Config.VirtualNicManagerInfo.NetConfig) {
    foreach ($sel in L $nc.SelectedVnic) {
      $v = L $nc.CandidateVnic | Where-Object { $_.Key -eq $sel } | Select-Object -First 1
      if ($v.Device) { $services[$v.Device] = @(L (K $services $v.Device)) + @($nc.NicType) }
    }
  }
  $pnics = foreach ($p in L $net.Pnic) {
    $hint = K $hints $p.Device; $sw = $null; $port = $null
    if ($hint.ConnectedSwitchPort) { $sw = $hint.ConnectedSwitchPort.DevId; $port = $hint.ConnectedSwitchPort.PortId }
    elseif ($hint.LldpInfo) { $sw = (L $hint.LldpInfo.Parameter | Where-Object { $_.Key -eq 'System Name' } | Select-Object -First 1).Value; $port = $hint.LldpInfo.PortId }
    [ordered]@{ name = $p.Device; speed = $(if ($p.LinkSpeed) { $p.LinkSpeed.SpeedMb } else { 0 }); mac = $p.Mac; switch = $sw; port = $port }
  }
  $hosts += [ordered]@{
    name = $h.Name; cluster = (K $clusters $h.Parent.Value); state = "$($h.Runtime.ConnectionState)"; version = $h.Config.Product.Version
    pnics = @($pnics)
    vswitches = @(foreach ($sw in L $net.Vswitch) { [ordered]@{ name = $sw.Name; pnics = @(foreach ($k in L $sw.Pnic) { K $pnicByKey $k }) } })
    portgroups = @(foreach ($pg in L $net.Portgroup) { [ordered]@{ name = $pg.Spec.Name; vlan = "$($pg.Spec.VlanId)"; vswitch = $pg.Spec.VswitchName } })
    proxies = @(foreach ($px in L $net.ProxySwitch) {
      $dn = K $dvsNames $px.DvsUuid
      [ordered]@{ name = $(if ($dn) { $dn } else { $px.DvsName }); pnics = @(foreach ($k in L $px.Pnic) { K $pnicByKey $k }) } })
    vmks = @(foreach ($v in L $net.Vnic) { [ordered]@{ name = $v.Device; ip = $v.Spec.Ip.IpAddress; portgroup = $v.Portgroup
      dvportgroup = $v.Spec.DistributedVirtualPort.PortgroupKey; services = @(L (K $services $v.Device)) } })
  }
}
$vms = @()
foreach ($v in L (Get-View -Server $vi -ViewType VirtualMachine -Property Name, Runtime.Host, Runtime.PowerState, Config.Template, Config.Hardware.Device)) {
  if ($v.Config.Template) { continue }
  $vms += [ordered]@{ name = $v.Name; host = (K $hostNames $v.Runtime.Host.Value); power = "$($v.Runtime.PowerState)"
    nics = @(foreach ($n in L $v.Config.Hardware.Device) { if ($n -is [VMware.Vim.VirtualEthernetCard]) {
      [ordered]@{ network = $n.Backing.DeviceName; dvportgroup = $n.Backing.Port.PortgroupKey; connected = [bool]$n.Connectable.Connected; mac = $n.MacAddress } } }) }
}
$json = [ordered]@{ vcenter = $vi.Name; hosts = $hosts; dvportgroups = @($dvpgs.Values); vms = $vms } | ConvertTo-Json -Depth 8 -Compress
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
for ($i = 0; $i -lt $b64.Length; $i += 150) { 'NETMAP|' + $b64.Substring($i, [Math]::Min(150, $b64.Length - $i)) }
"""

# Get-VM and Get-VMHost are named with their module: a computer with Hyper-V's PowerShell tools has its own.
TRAFFIC = r"""
$hs = @(VMware.VimAutomation.Core\Get-VMHost -Server $vi | Where-Object { $_.ConnectionState -in 'Connected', 'Maintenance' })
if ($hs) {
  foreach ($s in Get-Stat -Server $vi -Entity $hs -Stat 'net.received.average', 'net.transmitted.average' -Realtime -MaxSamples 1 -ErrorAction SilentlyContinue) {
    if ($s.Instance) { 'P|{0}|{1}|{2}|{3}' -f $s.Entity.Name, $s.Instance, $s.MetricId, $s.Value }
  }
}
$vs = @(VMware.VimAutomation.Core\Get-VM -Server $vi | Where-Object { $_.PowerState -eq 'PoweredOn' })
if ($vs) {
  foreach ($s in Get-Stat -Server $vi -Entity $vs -Stat 'net.received.average', 'net.transmitted.average' -Realtime -MaxSamples 1 -ErrorAction SilentlyContinue) {
    if (-not $s.Instance) { 'V|{0}|{1}|{2}' -f $s.Entity.Name, $s.MetricId, $s.Value }
  }
}
'TRAFFIC-DONE'
"""


GUEST_TYPES = ("Bash", "PowerShell", "Bat")


def guest_script(vm_id: str, command: str, user: str, password: str, script_type: str) -> str:
    """Invoke-VMScript: run a command inside a VM through VMware Tools (no network path to the VM needed)."""
    if not re.fullmatch(r"vm-\d+", vm_id) or script_type not in GUEST_TYPES:
        raise NetMapError("Invalid VM or script type")
    return f"""
$vm = VMware.VimAutomation.Core\\Get-VM -Server $vi -Id {ps_quote('VirtualMachine-' + vm_id)}
$r = Invoke-VMScript -Server $vi -VM $vm -ScriptText {ps_quote(command)} -GuestUser {ps_quote(user)} -GuestPassword {ps_quote(password)} -ScriptType {script_type} -ErrorAction Stop
'GUEST-EXIT|' + $r.ExitCode
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]$r.ScriptOutput))
for ($i = 0; $i -lt $b64.Length; $i += 150) {{ 'GUEST-OUT|' + $b64.Substring($i, [Math]::Min(150, $b64.Length - $i)) }}
'GUEST-DONE'
"""


def parse_guest(output: str) -> dict:
    _check(output)
    if "GUEST-DONE" not in output:
        raise NetMapError(f"The command didn't finish: {output.strip()[-300:] or 'no output'}")
    code, chunks = None, []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("GUEST-EXIT|"):
            code = int(line.split("|", 1)[1] or 0)
        elif line.startswith("GUEST-OUT|"):
            chunks.append(line[len("GUEST-OUT|"):])
    return {"output": base64.b64decode("".join(chunks)).decode("utf-8", "replace") if chunks else "", "exit_code": code}


def _wrap(conn, body: str) -> str:
    # The error names the script line it came from (its number only: one line carries the password).
    return (f"try {{\n{_connect(conn)}{body}\n}} catch {{ 'NETMAP-ERROR|' + $_.Exception.Message + "
            "' (network map script line ' + $_.InvocationInfo.ScriptLineNumber + ')' }")


def _check(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("NETMAP-ERROR|"):
            raise NetMapError(line.split("|", 1)[1].strip())
    return output


def decode_topology(output: str) -> dict:
    chunks = [line.strip()[len("NETMAP|"):] for line in _check(output).splitlines() if line.strip().startswith("NETMAP|")]
    if not chunks:
        raise NetMapError(f"vCenter didn't return the network layout: {output.strip()[-300:] or 'no output'}")
    return json.loads(base64.b64decode("".join(chunks)).decode("utf-8"))


def _list(value) -> list:
    """PowerShell's ConvertTo-Json turns a one-item array into the item itself, and an empty one into null."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def build(raw: dict) -> dict:
    """The diagram's nodes: physical switches, uplinks, virtual switches, port groups, VMs and VMkernel ports.

    Standard vSwitches and their port groups with the same name on each host of a cluster are drawn once
    ("vSwitch0 on 4 hosts"); distributed switches and their port groups are one node each."""
    dvpg = {p["key"]: p for p in _list(raw.get("dvportgroups"))}
    hosts, switches, portgroups, uplinks, physical, vms, vmks = [], {}, {}, [], {}, [], []
    host_cluster = {}
    for h in _list(raw.get("hosts")):
        cluster = h.get("cluster") or "Standalone hosts"
        host_cluster[h["name"]] = cluster
        hosts.append({"name": h["name"], "cluster": cluster, "state": h.get("state"), "version": h.get("version")})
        owner = {}
        for sw in _list(h.get("vswitches")):
            sid = f"std:{cluster}|{sw['name']}"
            node = switches.setdefault(sid, {"id": sid, "name": sw["name"], "kind": "standard", "cluster": cluster, "hosts": []})
            node["hosts"].append(h["name"])
            for p in _list(sw.get("pnics")):
                owner[p] = sid
        for px in _list(h.get("proxies")):
            sid = f"dvs:{px['name']}"
            node = switches.setdefault(sid, {"id": sid, "name": px["name"], "kind": "distributed", "cluster": None, "hosts": []})
            node["hosts"].append(h["name"])
            for p in _list(px.get("pnics")):
                owner[p] = sid
        for pg in _list(h.get("portgroups")):
            pid = f"std:{cluster}|{pg['vswitch']}|{pg['name']}"
            portgroups.setdefault(pid, {"id": pid, "name": pg["name"], "switch": f"std:{cluster}|{pg['vswitch']}",
                                        "vlan": pg.get("vlan") or "0", "hosts": []})["hosts"].append(h["name"])
        for p in _list(h.get("pnics")):
            phys = p.get("switch") or None
            uid = f"{h['name']}|{p['name']}"
            uplinks.append({"id": uid, "host": h["name"], "cluster": cluster, "name": p["name"], "speed": p.get("speed") or 0,
                            "mac": p.get("mac"), "switch": owner.get(p["name"]), "physical": phys, "port": p.get("port")})
            pid = f"phys:{phys or 'unknown'}"
            physical.setdefault(pid, {"id": pid, "name": phys or "Upstream switch (not reported)", "known": bool(phys), "ports": []})
            physical[pid]["ports"].append(uid)
        for v in _list(h.get("vmks")):
            pg = (f"dvpg:{v['dvportgroup']}" if v.get("dvportgroup")
                  else next((pid for pid in portgroups if pid.startswith(f"std:{cluster}|") and pid.endswith(f"|{v.get('portgroup')}")), None))
            vmks.append({"id": f"{h['name']}|{v['name']}", "name": v["name"], "host": h["name"], "cluster": cluster, "ip": v.get("ip"),
                         "portgroup": pg, "services": [s for s in _list(v.get("services")) if s]})
    for p in dvpg.values():
        if p.get("uplink"):
            continue
        portgroups[f"dvpg:{p['key']}"] = {"id": f"dvpg:{p['key']}", "name": p["name"], "switch": f"dvs:{p['switch']}",
                                          "vlan": p.get("vlan") or "0", "hosts": []}
    for v in _list(raw.get("vms")):
        cluster = host_cluster.get(v.get("host"), "Standalone hosts")
        nics = []
        for n in _list(v.get("nics")):
            if n.get("dvportgroup"):
                pg = f"dvpg:{n['dvportgroup']}"
            else:
                pg = next((pid for pid in portgroups if pid.startswith(f"std:{cluster}|") and pid.endswith(f"|{n.get('network')}")), None)
            nics.append({"portgroup": pg, "connected": bool(n.get("connected")), "mac": n.get("mac")})
        vms.append({"id": f"vm:{v['name']}", "name": v["name"], "host": v.get("host"), "cluster": cluster,
                    "power": v.get("power"), "nics": nics})
    return {"vcenter": raw.get("vcenter"), "hosts": hosts, "uplinks": uplinks, "physical": list(physical.values()),
            "switches": list(switches.values()), "portgroups": list(portgroups.values()), "vms": vms, "vmks": vmks,
            "clusters": sorted({h["cluster"] for h in hosts})}


def parse_traffic(output: str) -> dict:
    """{'uplinks': {'host|vmnic0': {'rx': bps, 'tx': bps}}, 'vms': {'name': {...}}}: KBps from vCenter as bits/s."""
    out = {"uplinks": {}, "vms": {}}
    for line in _check(output).splitlines():
        parts = line.strip().split("|")
        try:
            if parts[0] == "P" and len(parts) == 5:
                key, metric, value = f"{parts[1]}|{parts[2]}", parts[3], float(parts[4])
                bucket = out["uplinks"].setdefault(key, {"rx": 0.0, "tx": 0.0})
            elif parts[0] == "V" and len(parts) == 4:
                key, metric, value = parts[1], parts[2], float(parts[3])
                bucket = out["vms"].setdefault(key, {"rx": 0.0, "tx": 0.0})
            else:
                continue
        except ValueError:
            continue
        bucket["rx" if "received" in metric else "tx"] = value * 8000  # KBps -> bit/s
    return out


class NetMaps:
    """One long-lived PowerShell session per vCenter connection, plus short caches."""

    def __init__(self, exe: str | None):
        self.exe = exe
        self._sessions: dict[str, PowerShellSession] = {}
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def _session(self, conn) -> PowerShellSession:
        if not self.exe:
            raise NetMapError("PowerShell isn't available on the computer KasTec runs on, so the network map can't be read.")
        with self._lock:
            key = (conn.id, conn.config.get("host"), conn.secret)
            for k in [k for k in self._sessions if k[0] == conn.id and k != key]:
                self._sessions.pop(k).close()   # the connection was changed: sign in again
            if key not in self._sessions:
                self._sessions[key] = PowerShellSession(self.exe, Path.home(), None)
            return self._sessions[key]

    def _cached(self, kind: str, conn, ttl: float, force: bool, make):
        k = (kind, conn.id)
        hit = self._cache.get(k)
        if hit and not force and time.time() - hit[0] < ttl:
            return hit[1]
        value = make()
        self._cache[k] = (time.time(), value)
        return value

    def topology(self, conn, force: bool = False) -> dict:
        def make():
            r = self._session(conn).run(_wrap(conn, TOPOLOGY), timeout=600, log_as="[network map] topology")
            return build(decode_topology(r["output"]))
        return self._cached("topology", conn, TOPOLOGY_TTL, force, make)

    def traffic(self, conn) -> dict:
        def make():
            r = self._session(conn).run(_wrap(conn, TRAFFIC), timeout=120, log_as="[network map] traffic")
            if "TRAFFIC-DONE" not in r["output"]:
                _check(r["output"])
                raise NetMapError(f"vCenter didn't return traffic figures: {r['output'].strip()[-300:] or 'no output'}")
            return {**parse_traffic(r["output"]), "at": time.time()}
        return self._cached("traffic", conn, TRAFFIC_TTL, False, make)

    def guest_run(self, conn, vm_id: str, command: str, user: str, password: str, script_type: str) -> dict:
        script = guest_script(vm_id, command, user, password, script_type)
        r = self._session(conn).run(_wrap(conn, script), timeout=330, log_as=f"[guest command] {vm_id}")
        return parse_guest(r["output"])

    def close(self) -> None:
        with self._lock:
            for s in self._sessions.values():
                s.close()
            self._sessions.clear()
