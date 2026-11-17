"""Create a LUN on a VNX, present it to ESXi hosts, and optionally make it a VMFS datastore.

Everything is checked against a fresh read of the array and vCenter first (plan), and the plan lists the exact
commands. Creating needs an administrator, the plan's checks to pass, and the LUN name typed again. Nothing that
already exists is changed: a new LUN is created and added to storage groups, and a datastore is created on it.
"""

from __future__ import annotations

import re

from .connectors.vnx import LUN_NAME, sh_quote
from .shell import ps_quote

DATASTORE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
MAX_GB = 65536  # 64 TB, the largest VMFS-5/6 datastore


def _short(name: str) -> str:
    return (name or "").split(".")[0].lower()


def free_hlu(groups: list[dict]) -> int | None:
    used = {p["hlu"] for g in groups for p in g["luns"]}
    return next((n for n in range(0, 256) if n not in used), None)


def plan(req: dict, vnx: dict, vc: dict | None) -> dict:
    """req: name, pool, size_gb, thin, sp (auto|a|b), storage_groups, hlu (None = automatic), datastore (None or
    {cluster, name, vmfs}). vnx: the array as VNXConnector.read() returns it. vc: {"name", "clusters", "datastores",
    "powercli": True|False|None} for the chosen vCenter, or None."""
    errors, warnings = [], []
    name = (req.get("name") or "").strip()
    size = int(req.get("size_gb") or 0)
    thin = bool(req.get("thin", True))

    if not LUN_NAME.match(name):
        errors.append("The LUN name must start with a letter or digit and use only letters, digits, - _ and . (up to 63 characters).")
    elif any(lun["name"].lower() == name.lower() for lun in vnx["luns"]):
        errors.append(f"There's already a LUN called {name} on this array.")
    if not 1 <= size <= MAX_GB:
        errors.append(f"The size must be between 1 and {MAX_GB} GB.")

    pool = next((p for p in vnx["pools"] if p["name"] == req.get("pool")), None)
    if not pool:
        errors.append("Choose one of the array's storage pools.")
    else:
        free = pool["free_gb"] or 0
        if not (pool.get("status") or "OK").startswith("OK"):
            errors.append(f"{pool['name']} reports {pool['status']}; fix the pool before adding LUNs to it.")
        if not thin and size > free:
            errors.append(f"{pool['name']} has {free:,.0f} GB free, not enough for a {size:,} GB thick LUN.")
        elif thin and size > free:
            warnings.append(f"{pool['name']} has {free:,.0f} GB free. A {size:,} GB thin LUN starts small, but the pool "
                            "would run out if it filled up.")
        if pool.get("user_gb") and pool.get("subscribed_gb") is not None:
            after = (pool["subscribed_gb"] + size) / pool["user_gb"] * 100
            if thin and after > 100:
                warnings.append(f"{pool['name']} would be {after:.0f}% subscribed (thin LUNs promising more space than the pool has).")
        if pool.get("percent_full") and pool.get("threshold") and pool["percent_full"] >= pool["threshold"]:
            warnings.append(f"{pool['name']} is already {pool['percent_full']}% full (its alert threshold is {pool['threshold']}%).")
        try:
            sh_quote(pool["name"])
        except Exception:  # noqa: BLE001
            errors.append(f"KasTec can't use the pool name {pool['name']!r} in a command.")

    sp = req.get("sp") or "auto"
    if sp == "auto":
        owned = {"a": 0, "b": 0}
        for lun in vnx["luns"]:
            if lun.get("owner") in ("SP A", "SP B"):
                owned[lun["owner"][-1].lower()] += 1
        sp = "a" if owned["a"] <= owned["b"] else "b"
    elif sp not in ("a", "b"):
        errors.append("The SP must be A, B or automatic.")

    by_name = {g["name"]: g for g in vnx["groups"] if not g["special"]}
    chosen = []
    for g in req.get("storage_groups") or []:
        if g not in by_name:
            errors.append(f"There's no storage group called {g!r}.")
        else:
            chosen.append(by_name[g])
    hlu = req.get("hlu")
    if hlu in (None, ""):
        hlu = free_hlu(chosen) if chosen else None
        if chosen and hlu is None:
            errors.append("No free HLU (0-255) in all the chosen storage groups.")
    else:
        hlu = int(hlu)
        taken = [g["name"] for g in chosen if any(p["hlu"] == hlu for p in g["luns"])]
        if not 0 <= hlu <= 255:
            errors.append("The HLU must be between 0 and 255.")
        elif taken:
            errors.append(f"HLU {hlu} is already used in {', '.join(taken)}.")
    if not chosen:
        warnings.append("No storage group chosen: the LUN won't be visible to any host until you add it to one.")

    ds = req.get("datastore")
    ds_plan = None
    if ds:
        ds_name = (ds.get("name") or "").strip()
        vmfs = str(ds.get("vmfs") or "auto")
        if not vc:
            errors.append("Choose the vCenter to create the datastore in.")
        else:
            cluster = next((c for c in vc["clusters"] if c["name"] == ds.get("cluster")), None)
            if not cluster:
                errors.append("Choose the cluster whose hosts should use the datastore.")
            if not DATASTORE_NAME.match(ds_name):
                errors.append("The datastore name must start with a letter or digit and use only letters, digits, - _ and . .")
            elif any(n.lower() == ds_name.lower() for n in vc["datastores"]):
                errors.append(f"{vc['name']} already has a datastore called {ds_name}.")
            if vmfs not in ("auto", "5", "6"):
                errors.append("The VMFS version must be automatic, 5 or 6.")
            if vc.get("powercli") is False:
                errors.append("VMware PowerCLI isn't installed on the KasTec computer, so KasTec can't create the datastore. "
                              "In an Administrator PowerShell window there run: Install-Module VCF.PowerCLI -Scope AllUsers "
                              "(or untick the datastore and create it in vSphere yourself).")
            if not chosen:
                errors.append("Choose the storage group(s) holding this cluster's hosts, or the hosts can't see the LUN.")
            elif cluster:
                presented = {_short(h) for g in chosen for h in g["hosts"]}
                missing = [h for h in cluster["hosts"] if _short(h) not in presented]
                if missing and presented:
                    warnings.append(f"{', '.join(missing)} in {cluster['name']} isn't in the chosen storage group(s), so it "
                                    "won't see the new datastore.")
            ds_plan = {"cluster": ds.get("cluster"), "name": ds_name, "vmfs": vmfs, "vcenter": vc["name"]}

    steps = []
    if pool and LUN_NAME.match(name):
        steps.append(f"naviseccli lun -create -type {'Thin' if thin else 'NonThin'} -capacity {size} -sq gb "
                     f"-poolName '{pool['name']}' -name '{name}' -sp {sp}")
        steps += [f"naviseccli storagegroup -addhlu -gname '{g['name']}' -hlu {hlu} -alu <the new LUN's number>" for g in chosen]
    if ds_plan:
        steps.append(f"PowerCLI on {ds_plan['vcenter']}: rescan the storage of the hosts in {ds_plan['cluster']}, "
                     f"New-Datastore -Name '{ds_plan['name']}' -Path <the new LUN's naa ID> -Vmfs "
                     f"-FileSystemVersion {'(5 if any host runs ESXi older than 6.5, else 6)' if ds_plan['vmfs'] == 'auto' else ds_plan['vmfs']}"
                     " on the first host, then rescan the others")
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "lun": {"name": name, "pool": pool["name"] if pool else None, "size_gb": size, "thin": thin, "sp": sp},
            "storage_groups": [g["name"] for g in chosen], "hlu": hlu, "datastore": ds_plan, "steps": steps}


POWERCLI_CHECK = ("if (Get-Module -ListAvailable -Name VMware.VimAutomation.Core) { 'POWERCLI|yes' } "
                  "else { 'POWERCLI|no' }")


def datastore_script(vc_conn, cluster: str, naa: str, name: str, vmfs: str) -> str:
    """PowerCLI: rescan the cluster's hosts, create the VMFS datastore on the new LUN, rescan the rest.
    Prints DATASTORE|name|capacity GB|VMFS version|hosts that see it."""
    cfg = vc_conn.config
    cert = "Ignore" if cfg.get("verify_ssl") is False else "Fail"
    version = ("$v = if ($old) { '5' } else { '6' }" if vmfs == "auto" else
               f"$v = '{int(vmfs)}'; if ($v -eq '6' -and $old) {{ throw ('VMFS-6 needs ESXi 6.5 or newer; ' + ($old.Name -join ', ') + ' runs an older version. Choose VMFS-5.') }}")
    return f"""Import-Module VMware.VimAutomation.Core -ErrorAction Stop -WarningAction SilentlyContinue
Set-PowerCLIConfiguration -Scope Session -InvalidCertificateAction {cert} -ParticipateInCeip $false -Confirm:$false | Out-Null
$__cred = New-Object System.Management.Automation.PSCredential({ps_quote(cfg.get("username"))}, (ConvertTo-SecureString {ps_quote(vc_conn.secret)} -AsPlainText -Force))
$vi = Connect-VIServer -Server {ps_quote(cfg["host"])} -Port {int(cfg.get("port") or 443)} -Credential $__cred -ErrorAction Stop
try {{
  $hosts = @(Get-Cluster -Name {ps_quote(cluster)} -Server $vi -ErrorAction Stop | VMware.VimAutomation.Core\Get-VMHost | Where-Object {{ $_.ConnectionState -in 'Connected', 'Maintenance' }} | Sort-Object Name)
  if (-not $hosts) {{ throw 'No connected hosts in cluster {cluster.replace("'", "''")}.' }}
  $hosts | Get-VMHostStorage -RescanAllHba | Out-Null
  $first = $hosts[0]
  if (-not (Get-ScsiLun -VmHost $first -CanonicalName {ps_quote(naa)} -ErrorAction SilentlyContinue)) {{
    throw ($first.Name + ' does not see {naa} after a rescan. Check that its storage group holds this host and that it is zoned to the VNX.')
  }}
  $old = @($hosts | Where-Object {{ [version]$_.Version -lt [version]'6.5' }})
  {version}
  $ds = New-Datastore -VMHost $first -Name {ps_quote(name)} -Path {ps_quote(naa)} -Vmfs -FileSystemVersion $v -ErrorAction Stop
  $hosts | Select-Object -Skip 1 | Get-VMHostStorage -RescanVmfs | Out-Null
  $seen = @($hosts | Where-Object {{ Get-Datastore -Name {ps_quote(name)} -RelatedObject $_ -ErrorAction SilentlyContinue }}).Count
  'DATASTORE|{{0}}|{{1}}|{{2}}|{{3}}|{{4}}' -f $ds.Name, [math]::Round($ds.CapacityGB), $v, $seen, $hosts.Count
}} finally {{
  Disconnect-VIServer -Server $vi -Confirm:$false -Force -ErrorAction SilentlyContinue
  Remove-Variable __cred -ErrorAction SilentlyContinue
}}"""


def datastore_result(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("DATASTORE|"):
            _, name, gb, v, seen, total = (line.split("|") + [""] * 6)[:6]
            return f"Created VMFS-{v} datastore {name} ({gb} GB); {seen} of {total} hosts in the cluster see it."
    return None
