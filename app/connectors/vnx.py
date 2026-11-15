"""Dell EMC VNX (Unified) through its Control Station, over SSH: the same as signing in with PuTTY. Read-only.

KasTec runs PuTTY's command-line tool, plink, so nothing else needs installing on the KasTec computer.
One SSH session per page load runs read-only commands on the Control Station:

    /nas/sbin/naviseccli -h spa getagent | faults -list | storagepool -list | lun -list | storagegroup -list -host
    /nas/sbin/naviseccli -h spa getlun -name -uid -capacity -state -rg   (RAID-group LUNs, overview only)
    /nas/sbin/getreason                                                  (Control Station / Data Mover status)

Config keys:
    host        the Control Station (the address you open in PuTTY)
    port        default 22
    username    e.g. nasadmin
    sp_a, sp_b  the storage processors as the Control Station knows them; default spa / spb
    host_key    the Control Station's SSH key fingerprint (SHA256:...). Learned on the first connection
                and checked every time after, so a different machine answering at that address is refused.
The connection secret is the password. It's handed to plink in a short-lived file (plink -pwfile),
never on a command line.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..config import env
from .base import ConnectorError

NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/=]{20,}")
PLINK_PLACES = (r"C:\Program Files\PuTTY\plink.exe", r"C:\Program Files (x86)\PuTTY\plink.exe")
OK_SLOTS = {5, 10, 11}  # getreason: 5 = Data Mover contacted, 10/11 = primary/secondary Control Station
SLOT_TEXT = {0: "reset", 1: "DOS booted", 2: "POST passed", 3: "loaded", 4: "configured", 5: "contacted",
             7: "panicked", 9: "reset pending", 10: "primary Control Station", 11: "secondary Control Station",
             13: "dump complete", 14: "being flashed", 15: "booted"}
SECURITY_HINT = ("If it mentions security, credentials or a security file, sign in to the Control Station with PuTTY "
                 "once and run: /nas/sbin/naviseccli -AddUserSecurity -scope 0 -user <a Unisphere admin> "
                 "(it asks for that account's password). Then try again.")


def find_plink() -> str | None:
    configured = env("PLINK")
    if configured:
        return configured
    return shutil.which("plink") or next((p for p in PLINK_PLACES if os.path.exists(p)), None)


# --- parsing naviseccli output ------------------------------------------------------------

def _kv(line: str) -> tuple[str, str] | None:
    """'Key:  value' (naviseccli's usual form) or 'Key      value' (getlun's columns)."""
    m = re.match(r"^\s*([^:]+?):\s*(.*?)\s*$", line)
    if m:
        return m.group(1).strip(), m.group(2)
    m = re.match(r"^(\S.*?)\s{2,}(\S.*?)\s*$", line)
    return (m.group(1).strip(), m.group(2)) if m else None


def _records(text: str, start: re.Pattern) -> list[list[str]]:
    out: list[list[str]] = []
    for line in text.splitlines():
        if start.match(line):
            out.append([line])
        elif out:
            out[-1].append(line)
    return out


def _fields(lines: list[str]) -> dict[str, str]:
    out = {}
    for line in lines:
        kv = _kv(line)
        if kv and kv[0] not in out:
            out[kv[0]] = kv[1]
    return out


def _num(value) -> float | None:
    try:
        return round(float(str(value).replace(",", "").split()[0]), 1)
    except (TypeError, ValueError, IndexError):
        return None


def naa(uid: str | None) -> str | None:
    """'60:06:01:60:78:00:3A:00:...' as vSphere shows it: 'naa.6006016078003a00...'."""
    hexed = re.sub(r"[^0-9A-Fa-f]", "", uid or "")
    return f"naa.{hexed.lower()}" if len(hexed) == 32 else None


def parse_agent(text: str) -> dict:
    f = _fields(text.splitlines())
    return {"name": f.get("Name") or "", "model": f.get("Model") or "", "serial": f.get("Serial No") or "",
            "revision": f.get("Revision") or "", "agent": f.get("Agent Rev") or ""}


def parse_faults(text: str) -> dict:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    ok = any("operating normally" in ln.lower() for ln in lines)
    items = [] if ok else [ln for ln in lines if not ln.lower().startswith("faulted subsystem")]
    return {"ok": ok, "items": items}


def parse_pools(text: str) -> list[dict]:
    pools = []
    for rec in _records(text, re.compile(r"^Pool Name:")):
        f = _fields(rec)
        luns = [int(x) for x in re.findall(r"\d+", f.get("LUNs", ""))]
        pools.append({
            "name": f.get("Pool Name", ""), "id": f.get("Pool ID"), "raid": f.get("Raid Type"),
            "state": f.get("State"), "status": f.get("Status"), "disk_type": f.get("Disk Type"),
            "user_gb": _num(f.get("User Capacity (GBs)")), "consumed_gb": _num(f.get("Consumed Capacity (GBs)")),
            "free_gb": _num(f.get("Available Capacity (GBs)")), "percent_full": _num(f.get("Percent Full")),
            "subscribed_gb": _num(f.get("Total Subscribed Capacity (GBs)")),
            "percent_subscribed": _num(f.get("Percent Subscribed")),
            "threshold": _num(f.get("Percent Full Threshold")), "luns": luns,
        })
    return pools


def parse_luns(text: str) -> list[dict]:
    luns = []
    for rec in _records(text, re.compile(r"^LOGICAL UNIT NUMBER\s+\d+")):
        f = _fields(rec[1:])
        number = int(re.search(r"\d+", rec[0]).group())
        user = _num(f.get("User Capacity (GBs)"))
        if user is None and f.get("LUN Capacity(Megabytes)"):
            user = round((_num(f["LUN Capacity(Megabytes)"]) or 0) / 1024, 1)
        luns.append({
            "number": number, "name": f.get("Name", ""), "uid": f.get("UID", ""), "naa": naa(f.get("UID")),
            "user_gb": user, "consumed_gb": _num(f.get("Consumed Capacity (GBs)")),
            "pool": f.get("Pool Name") or (f"RAID group {f['RAIDGroup ID']}" if f.get("RAIDGroup ID") else ""),
            "state": f.get("Current State") or f.get("State") or "", "status": f.get("Status", ""),
            "owner": f.get("Current Owner", ""), "thin": (f.get("Is Thin LUN") or "").lower() == "yes",
            "private": (f.get("Is Private") or "").lower() == "yes",
            "faulted": (f.get("Is Faulted") or "").lower() == "true",
        })
    return luns


def parse_groups(text: str) -> list[dict]:
    groups = []
    for rec in _records(text, re.compile(r"^Storage Group Name:")):
        name = _kv(rec[0])[1]
        hosts, pairs, in_pairs = [], [], False
        for line in rec[1:]:
            if line.strip().startswith("HLU/ALU Pairs"):
                in_pairs = True
                continue
            kv = _kv(line)
            if kv and kv[0] == "Host name" and kv[1] and kv[1] not in hosts:
                hosts.append(kv[1])
            m = re.match(r"^\s*(\d+)\s+(\d+)\s*$", line)
            if in_pairs and m:
                pairs.append({"hlu": int(m.group(1)), "alu": int(m.group(2))})
            elif in_pairs and kv and not m:
                in_pairs = False
        groups.append({"name": name, "hosts": hosts, "luns": pairs, "special": name.startswith("~")})
    return groups


def parse_slots(text: str) -> list[dict]:
    slots = []
    for line in text.splitlines():
        m = re.match(r"^\s*(\d+)\s*-\s*(\S.*?)\s*$", line)
        if m:
            code = int(m.group(1))
            n = re.search(r"slot_(\d+)", m.group(2))
            if code in (10, 11):
                label = f"Control Station {n.group(1) if n else ''}".strip()
                state = "primary" if code == 10 else "standby"
            else:
                label = f"Data Mover server_{n.group(1)}" if n else m.group(2)
                state = SLOT_TEXT.get(code, f"code {code}")
            slots.append({"code": code, "slot": m.group(2), "label": label, "state": state, "ok": code in OK_SLOTS})
    return slots


# --- what each LUN is used for --------------------------------------------------------------

def classify(luns: list[dict], groups: list[dict]) -> None:
    """Add storage groups, hosts and a 'use' to every LUN (in place)."""
    by_alu: dict[int, list[dict]] = {}
    for g in groups:
        for p in g["luns"]:
            by_alu.setdefault(p["alu"], []).append(g)
    for lun in luns:
        gs = by_alu.get(lun["number"], [])
        lun["storage_groups"] = [g["name"] for g in gs]
        lun["hosts"] = sorted({h for g in gs if not g["special"] for h in g["hosts"]}, key=str.lower)
        if lun["private"]:
            lun["use"] = "private"           # reserved/system LUN; never a cleanup candidate
        elif any(g["name"] == "~filestorage" for g in gs):
            lun["use"] = "file"              # used by the VNX's own Data Movers (file systems / CIFS / NFS)
        elif not [g for g in gs if not g["special"]]:
            lun["use"] = "not_presented"     # in no host storage group: nothing can be using it
        else:
            lun["use"] = "presented"         # refined by link_datastores()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def link_datastores(luns: list[dict], datastores: list[dict]) -> None:
    """Match presented LUNs to vSphere datastores by name (vCenter's REST API doesn't say which LUN backs
    which datastore). An exact name match wins; otherwise one datastore whose name ends with the LUN's
    name (or the other way round), so LUN 'ESB' isn't matched when both 'JFK_VNX_ESB' and 'PD_VNX_ESB' exist."""
    vmfs = [d for d in datastores if (d.get("type") or "VMFS").upper() == "VMFS"]
    for lun in luns:
        if lun.get("use") != "presented":
            continue
        n = _norm(lun["name"])
        found = [d for d in vmfs if _norm(d["name"]) == n]
        how = "name"
        if not found and len(n) >= 4:
            found = [d for d in vmfs if _norm(d["name"]).endswith(n) or n.endswith(_norm(d["name"]))]
            how = "similar name"
        if len(found) == 1:
            d = found[0]
            lun["datastore"] = {"name": d["name"], "vms": d.get("vms"), "connection": d.get("connection"),
                                "capacity": d.get("capacity"), "free_space": d.get("free_space"), "matched_by": how}
            lun["use"] = "datastore_empty" if d.get("vms") == [] else "datastore"
        else:
            lun["use"] = "unmatched"


def summarize(agent: dict, faults: dict, pools: list[dict], luns: list[dict], slots: list[dict]) -> dict:
    return {
        "model": agent.get("model"), "serial": agent.get("serial"),
        "faults_ok": faults["ok"], "faults": len(faults["items"]),
        "pools": len(pools),
        "capacity_gb": round(sum(p["user_gb"] or 0 for p in pools), 1),
        "free_gb": round(sum(p["free_gb"] or 0 for p in pools), 1),
        "luns": len([lun for lun in luns if not lun["private"]]),
        "not_presented": sum(1 for lun in luns if lun.get("use") == "not_presented"),
        "slots_bad": sum(1 for s in slots if not s["ok"]),
    }


# --- the connector ----------------------------------------------------------------------------

def _prefix(sp_a: str, sp_b: str) -> list[str]:
    """Use SP A, or SP B when A doesn't answer. Every naviseccli call reads /dev/null, so a command that
    unexpectedly asks a question gets no answer (and fails) instead of reading the next line of this script."""
    return ["export NAS_DB=/nas", "N=/nas/sbin/naviseccli",
            f"SP={sp_a}; $N -h $SP getagent </dev/null >/dev/null 2>&1 || SP={sp_b}", 'echo "@@KASTEC sp $SP"']


def _script(sp_a: str, sp_b: str, full: bool) -> str:
    navi = [("getagent", "getagent"), ("faults", "faults -list"), ("pools", "storagepool -list"),
            ("luns", "lun -list"), ("groups", "storagegroup -list -host")]
    if full:
        navi.append(("rglun", "getlun -name -uid -capacity -state -rg"))
    lines = _prefix(sp_a, sp_b)
    lines += [f'echo "@@KASTEC {key}"; $N -h $SP {cmd} </dev/null 2>&1' for key, cmd in navi]
    lines += ['echo "@@KASTEC slots"; /nas/sbin/getreason </dev/null 2>&1', "exit 0"]
    return "\n".join(lines) + "\n"


def sh_quote(value: str) -> str:
    """A single-quoted shell word. Callers have already refused quotes and line breaks."""
    if not value or "'" in value or "\n" in value or "\r" in value:
        raise ConnectorError(f"{value!r} can't be used in a VNX command")
    return f"'{value}'"


LUN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


def split_sections(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("@@KASTEC "):
            current, _, rest = line[len("@@KASTEC "):].partition(" ")
            out[current] = rest + "\n" if rest else ""
        elif current:
            out[current] += line + "\n"
    return out


class VNXConnector:
    def __init__(self, config: dict, secret: str, runner=None):
        self.host = (config.get("host") or "").strip()
        self.username = (config.get("username") or "").strip()
        if not (self.host and self.username):
            raise ConnectorError("The Control Station address and username are required")
        self.port = int(config.get("port") or 22)
        self.sp_a = (config.get("sp_a") or "spa").strip()
        self.sp_b = (config.get("sp_b") or "spb").strip()
        for value in (self.host, self.username, self.sp_a, self.sp_b):
            if not NAME.match(value):
                raise ConnectorError(f"{value!r} isn't a valid host or user name")
        self.host_key = (config.get("host_key") or "").strip() or None
        self.password = secret
        self.learned_config: dict = {}
        self._runner = runner  # tests and the demo inject a fake Control Station here

    def __enter__(self) -> "VNXConnector":
        return self

    def __exit__(self, *exc) -> None:
        pass

    # --- SSH through plink ---------------------------------------------------------------

    def _plink(self, script: str, host_key: str | None) -> tuple[int, str, str]:
        plink = find_plink()
        if not plink:
            raise ConnectorError("PuTTY's plink.exe wasn't found on the KasTec computer. Install PuTTY "
                                 "(it includes plink), or set KASTEC_PLINK to plink.exe's full path.")
        tmp_dir = Path(env("DATA_DIR", "data")) / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, pwfile = tempfile.mkstemp(dir=tmp_dir, prefix="vnx-", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.password + "\n")
            args = [plink, "-ssh", "-batch", "-P", str(self.port), "-l", self.username, "-pwfile", pwfile]
            if host_key:
                args += ["-hostkey", host_key]
            args += [self.host, "bash -s"]
            try:
                # Bytes, not text: in text mode Windows would turn every \n into \r\n, and the Control
                # Station's bash would then see "exit 0\r" ("numeric argument required") and "/nas\r".
                p = subprocess.run(args, input=script.replace("\r", "").encode("utf-8"), capture_output=True,
                                   timeout=300, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except subprocess.TimeoutExpired as e:
                raise ConnectorError(f"The Control Station {self.host} didn't finish answering within 5 minutes") from e
            except OSError as e:
                raise ConnectorError(f"Couldn't run plink ({plink}): {e}") from e
            return (p.returncode, p.stdout.decode("utf-8", "replace").replace("\r\n", "\n"),
                    p.stderr.decode("utf-8", "replace").replace("\r\n", "\n"))
        finally:
            try:
                os.remove(pwfile)
            except OSError:
                pass

    def _ssh(self, script: str) -> str:
        run = self._runner or self._plink
        code, out, err = run(script, self.host_key)
        if code and not self.host_key and ("not cached" in err or "not in the cache" in err or "host key" in err.lower()):
            # First connection: trust this Control Station's key, remember it, and check it from now on.
            found = FINGERPRINT.findall(err)
            if found:
                self.host_key = found[-1]
                self.learned_config["host_key"] = self.host_key
                code, out, err = run(script, self.host_key)
        if code:
            text = (err or out).strip()
            low = text.lower()
            if "security breach" in low or "did not appear in manually configured list" in low or "host key" in low:
                raise ConnectorError(f"The Control Station at {self.host} has a different SSH key than the one KasTec "
                                     "saved. If it was rebuilt or replaced, remove and re-add this connection; "
                                     "otherwise something else may be answering at that address.")
            if "access denied" in low or "authentication" in low or "password" in low:
                raise ConnectorError(f"The Control Station refused the username or password ({self.username}).")
            if "unknown option" in low and "pwfile" in low:
                raise ConnectorError("This PuTTY is too old for KasTec (it needs PuTTY 0.77 or newer). Update PuTTY.")
            raise ConnectorError(f"Couldn't sign in to the Control Station {self.host}: {text.splitlines()[-1] if text else f'plink exit code {code}'}")
        return out

    def _read(self, full: bool) -> dict:
        sec = split_sections(self._ssh(_script(self.sp_a, self.sp_b, full)))
        agent = parse_agent(sec.get("getagent", ""))
        if not agent["serial"]:
            detail = " ".join(ln.strip() for ln in sec.get("getagent", "").splitlines() if ln.strip())[:300]
            raise ConnectorError(f"Signed in to the Control Station, but naviseccli couldn't read the array "
                                 f"through {self.sp_a} or {self.sp_b}: {detail or 'no answer'}. {SECURITY_HINT}")
        luns = parse_luns(sec.get("luns", ""))
        if full:
            known = {lun["uid"] for lun in luns}
            luns += [lun for lun in parse_luns(sec.get("rglun", "")) if lun["uid"] and lun["uid"] not in known]
        luns.sort(key=lambda lun: lun["number"])
        groups = parse_groups(sec.get("groups", ""))
        classify(luns, groups)
        return {"agent": agent, "sp": sec.get("sp", "").strip(), "faults": parse_faults(sec.get("faults", "")),
                "pools": parse_pools(sec.get("pools", "")), "luns": luns, "groups": groups,
                "slots": parse_slots(sec.get("slots", ""))}

    def read(self) -> dict:
        """Pools, LUNs and storage groups, without the slower RAID-group listing (for planning changes)."""
        return self._read(False)

    # --- changes (admin only, from the "Create LUN" form) -----------------------------------

    def create_lun(self, name: str, pool: str, size_gb: int, thin: bool, sp: str) -> dict:
        """lun -create in a storage pool, then read the new LUN back by name. Returns the parsed LUN."""
        if not LUN_NAME.match(name) or sp not in ("a", "b") or not 1 <= int(size_gb) <= 65536:
            raise ConnectorError("Invalid LUN settings")
        create = (f"lun -create -type {'Thin' if thin else 'NonThin'} -capacity {int(size_gb)} -sq gb "
                  f"-poolName {sh_quote(pool)} -name {sh_quote(name)} -sp {sp}")
        lines = _prefix(self.sp_a, self.sp_b) + [
            f'echo "@@KASTEC create"; $N -h $SP {create} </dev/null 2>&1; echo "@@KASTEC rc $?"',
            f'echo "@@KASTEC luns"; $N -h $SP lun -list -name {sh_quote(name)} </dev/null 2>&1', "exit 0"]
        sec = split_sections(self._ssh("\n".join(lines) + "\n"))
        found = [lun for lun in parse_luns(sec.get("luns", "")) if lun["name"] == name]
        if not found:
            said = " ".join(sec.get("create", "").split())[:400]
            raise ConnectorError(f"The VNX didn't create LUN {name}: {said or 'no answer'}")
        return found[0]

    def add_to_group(self, group: str, hlu: int, alu: int) -> None:
        """storagegroup -addhlu, then check the group really lists the LUN at that HLU."""
        if not 0 <= int(hlu) <= 255 or int(alu) < 0:
            raise ConnectorError("Invalid HLU or LUN number")
        lines = _prefix(self.sp_a, self.sp_b) + [
            f'echo "@@KASTEC add"; $N -h $SP storagegroup -addhlu -gname {sh_quote(group)} -hlu {int(hlu)} -alu {int(alu)} </dev/null 2>&1',
            f'echo "@@KASTEC groups"; $N -h $SP storagegroup -list -gname {sh_quote(group)} -host </dev/null 2>&1', "exit 0"]
        sec = split_sections(self._ssh("\n".join(lines) + "\n"))
        groups = [g for g in parse_groups(sec.get("groups", "")) if g["name"] == group]
        if not groups or {"hlu": int(hlu), "alu": int(alu)} not in groups[0]["luns"]:
            said = " ".join(sec.get("add", "").split())[:400]
            raise ConnectorError(f"The VNX didn't add LUN {alu} to {group}: {said or 'no answer'}")

    def run_command(self, command: str) -> dict:
        """Run an administrator's own command on the Control Station, as they would in PuTTY. Each command
        is a fresh sign-in (like opening PuTTY again): there's no 'cd' or variables kept between commands,
        and it can't answer questions, so it reads /dev/null."""
        command = command.replace("\r", "")
        script = ("export NAS_DB=/nas; export PATH=$PATH:/nas/bin:/nas/sbin\n"
                  f'echo "@@KASTEC out"\n( {command}\n) </dev/null 2>&1\necho "@@KASTEC rc $?"\nexit 0\n')
        sec = split_sections(self._ssh(script))
        out = sec.get("out", "")
        return {"output": out[:200_000] + ("\n… (output cut at 200,000 characters)" if len(out) > 200_000 else ""),
                "exit_code": int((sec.get("rc") or "0").strip() or 0)}

    # --- reads ---------------------------------------------------------------------------

    def test(self) -> dict:
        a = self._read(False)["agent"]
        return {"ok": True, "message": f"Connected to {a['model'] or 'the VNX'} {a['serial']} "
                                       f"(revision {a['revision']}) through the Control Station {self.host}"}

    def overview(self) -> dict:
        d = self._read(True)
        d["control_station"] = self.host
        return d

    def summary(self) -> dict:
        d = self._read(False)
        return summarize(d["agent"], d["faults"], d["pools"], d["luns"], d["slots"])
