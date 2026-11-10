"""The checks behind KasTec's guided domain controller restart.

Before: is another DC in the same site answering, is replication healthy, does this DC hold FSMO
roles, is it the DC KasTec itself uses. After: is it back on its ports, answering LDAP, has AD
finished its start-up sync (isSynchronized), is its Global Catalog ready, is its clock in step,
and is replication healthy again.

Each check is {"id", "label", "status": ok|warn|fail|unknown, "detail", "command"?}; "command" is
the manual PowerShell for whoever wants to check by hand (or when KasTec couldn't).
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import shell as powershell
from .connectors import ConnectorError
from .connectors.ad import ADConnector

PARTNER_PORTS = (389, 636)
STALE_REPLICATION = timedelta(hours=3)   # inter-site replication runs every 3 hours by default
MAX_SKEW_SECONDS = 60                    # Kerberos tolerates 5 minutes; anything near a minute needs a look

FSMO_ORDER = ["PDC", "RID", "Infrastructure", "Domain naming", "Schema"]
FSMO_NAMES = {"PDC": "PDCEmulator", "RID": "RIDMaster", "Infrastructure": "InfrastructureMaster",
              "Domain naming": "DomainNamingMaster", "Schema": "SchemaMaster"}


def _check(cid, label, status, detail, command=None) -> dict:
    out = {"id": cid, "label": label, "status": status, "detail": detail}
    if command:
        out["command"] = command
    return out


def host_of(dc: dict) -> str:
    return dc.get("dns_name") or dc["name"]


def move_roles_command(roles: list[str], to: str) -> str:
    names = ", ".join(FSMO_NAMES[r] for r in sorted(roles, key=FSMO_ORDER.index) if r in FSMO_NAMES)
    return f"Move-ADDirectoryServerOperationMasterRole -Identity {to} -OperationMasterRole {names}"


# --- before -----------------------------------------------------------------------

def partner_check(dc: dict, dcs: list[dict], port_open, demo: bool) -> dict:
    """Another DC in the same site must be answering, or sign-ins there go across the WAN (or fail)."""
    partners = [d for d in dcs if d["name"] != dc["name"] and (d.get("site") == dc.get("site") or not dc.get("site"))]
    site = dc.get("site") or "its site"
    if not partners:
        return _check("partner", "Another DC in the same site is up", "warn",
                      f"{dc['name']} is the only DC in {site}: users there will sign in through another site's DCs "
                      "until it's back.")
    if demo:
        answering = [d["name"] for d in partners if all((d.get("ports") or {}).get(str(p), True) for p in PARTNER_PORTS)]
    else:
        with ThreadPoolExecutor(max_workers=8) as pool:
            up = list(pool.map(lambda d: all(port_open(host_of(d), p) for p in PARTNER_PORTS), partners))
        answering = [d["name"] for d, ok in zip(partners, up) if ok]
    if answering:
        return _check("partner", "Another DC in the same site is up", "ok",
                      f"{', '.join(answering)} {'is' if len(answering) == 1 else 'are'} answering in {site}.")
    return _check("partner", "Another DC in the same site is up", "fail",
                  f"No other DC in {site} is answering ({', '.join(d['name'] for d in partners)}). "
                  f"Restarting {dc['name']} now would leave {site} with no working DC.")


def replication_check(partners: list[dict] | str | None, dc_name: str, now: datetime | None = None) -> dict:
    command = f"repadmin /showrepl {dc_name}\nrepadmin /replsummary"
    label = "Replication is healthy"
    if partners is None or isinstance(partners, str):
        why = partners or "PowerShell isn't available on the computer KasTec runs on"
        return _check("replication", label, "unknown", f"KasTec couldn't check ({why}). Check by hand:", command)
    if not partners:
        return _check("replication", label, "warn", f"{dc_name} reported no replication partners.", command)
    now = now or datetime.now(timezone.utc)
    failing = [p for p in partners if p["failures"] or p["last_result"]]
    if failing:
        return _check("replication", label, "fail", "Failing with " + "; ".join(
            f"{p['partner']} ({p['failures']} failure{'s' if p['failures'] != 1 else ''} in a row, last result {p['last_result']})"
            for p in failing) + ". Fix replication before restarting.", command)
    stale = []
    for p in partners:
        last = _parse_time(p["last_success"])
        if not last or now - last > STALE_REPLICATION:
            stale.append(p["partner"])
    if stale:
        return _check("replication", label, "warn",
                      f"No successful replication from {', '.join(stale)} in the last 3 hours.", command)
    return _check("replication", label, "ok",
                  f"Replicating with {', '.join(p['partner'] for p in partners)}, no failures.")


def fsmo_check(dc: dict, dcs: list[dict]) -> dict:
    roles = dc.get("roles") or []
    if not roles:
        return _check("fsmo", "FSMO roles", "ok", f"{dc['name']} holds no FSMO roles.")
    partner = next((d["name"] for d in dcs if d["name"] != dc["name"] and d.get("site") == dc.get("site")
                    and not d.get("read_only")), None) \
        or next((d["name"] for d in dcs if d["name"] != dc["name"] and not d.get("read_only")), None)
    if not partner:
        return _check("fsmo", "FSMO roles", "warn",
                      f"{dc['name']} holds {', '.join(roles)}, and there's no other writable DC to move them to, so "
                      "they're unavailable until it's back. Keep the restart short.")
    return _check("fsmo", "FSMO roles", "warn",
                  f"{dc['name']} holds {', '.join(roles)}. For a normal restart (back within about 15 minutes) "
                  f"nothing to do. For longer maintenance, move them to {partner} first, and back afterwards:",
                  move_roles_command(roles, partner) + "\n" + move_roles_command(roles, dc["name"]))


def kastec_dc_check(dc: dict, configured_host: str) -> dict:
    if not configured_host:
        return _check("kastec", "KasTec's own connection", "ok",
                      "If KasTec's connection uses this DC, it switches to another one automatically while it restarts.")
    if configured_host.lower() in (dc["name"].lower(), (dc.get("dns_name") or "").lower()):
        return _check("kastec", "KasTec's own connection", "warn",
                      f"KasTec normally talks to {dc['name']}; it will switch to another DC automatically while "
                      "this one restarts.")
    return _check("kastec", "KasTec's own connection", "ok", f"KasTec uses {configured_host}, not this DC.")


# --- after ------------------------------------------------------------------------

def back_checks(ports: dict, dse: dict | None, dse_error: str | None, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    up = all(ports.values())
    checks = [_check("ports", "Answering on its ports", "ok" if up else "fail",
                     ", ".join(f"{p} {'open' if ok else 'closed'}" for p, ok in ports.items()))]
    if dse is None:
        checks.append(_check("ldap", "Directory service answering", "fail", dse_error or "Not answering yet."))
        return checks
    checks.append(_check("ldap", "Directory service answering", "ok", "LDAP sign-in works."))
    checks.append(_check("synchronized", "Active Directory finished its start-up sync",
                         "ok" if dse["is_synchronized"] else "fail",
                         "isSynchronized is TRUE." if dse["is_synchronized"] else "Still synchronising after start-up."))
    checks.append(_check("gc", "Global Catalog ready", "ok" if dse["gc_ready"] else "fail",
                         "Ready." if dse["gc_ready"] else "Not ready yet."))
    their = _parse_time(dse.get("current_time"))
    if their:
        skew = abs((their - now).total_seconds())
        checks.append(_check("time", "Clock in step", "ok" if skew <= MAX_SKEW_SECONDS else "warn",
                             f"{skew:.0f} seconds from this computer's clock.",
                             None if skew <= MAX_SKEW_SECONDS else
                             f"w32tm /query /source /computer:{dse.get('dns_name') or ''}\nw32tm /monitor"))
    return checks


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# --- talking to one DC ---------------------------------------------------------------

def single_dc(conn, host: str) -> ADConnector:
    """A connection to exactly this DC: no switching to another one."""
    return ADConnector({**conn.config, "host": host, "known_dcs": []}, conn.secret, failover=False)


def read_dse(conn, host: str) -> tuple[dict | None, str | None]:
    try:
        with single_dc(conn, host) as c:
            return c.root_dse(), None
    except ConnectorError as e:
        return None, str(e)


# --- demo: a restart that takes about 40 seconds -----------------------------------------

_demo_restarts: dict[tuple[str, str], float] = {}
_demo_lock = threading.Lock()
DEMO_PHASES = (5, 20, 30)   # seconds: still up, then down, then up but syncing, then ready


def demo_restart(conn_id: str, name: str) -> None:
    with _demo_lock:
        _demo_restarts[(conn_id, name.lower())] = time.monotonic()


def demo_state(conn_id: str, name: str) -> tuple[dict, dict | None, str | None]:
    with _demo_lock:
        started = _demo_restarts.get((conn_id, name.lower()))
    t = time.monotonic() - started if started is not None else 1e9
    now = datetime.now(timezone.utc).isoformat()
    if DEMO_PHASES[0] <= t < DEMO_PHASES[1]:
        return {"389": False, "636": False}, None, f"{name} isn't answering on port 636: timed out"
    syncing = DEMO_PHASES[1] <= t < DEMO_PHASES[2]   # only just after it comes back up
    return ({"389": True, "636": True},
            {"is_synchronized": not syncing, "gc_ready": not syncing, "current_time": now, "dns_name": name.lower()}, None)


def demo_partners(dc: dict, dcs: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc)
    return [{"partner": d["name"], "last_success": (now - timedelta(minutes=7 + i * 11)).isoformat(),
             "last_result": 0, "failures": 0} for i, d in enumerate(x for x in dcs if x["name"] != dc["name"])]
