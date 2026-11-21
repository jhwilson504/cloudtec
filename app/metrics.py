"""Daily snapshots of the dashboard's totals, so the dashboard can show trends and change since yesterday.

Every time the dashboard loads, today's values are stored (the last load of the day wins). Nothing is
recorded for days KasTec wasn't opened; the charts simply skip them.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, timedelta
from pathlib import Path

KEYS = ("users", "disabled", "locked", "computers", "vms", "vms_on", "hosts", "hosts_connected", "storage_capacity",
        "storage_free", "servers", "servers_attention", "sw_nodes", "sw_down", "sw_alerts", "vnx_capacity_gb",
        "vnx_free_gb", "vnx_faults", "connections", "connections_down", "problems", "health_pct", "storage_used_pct")


def totals(items: list[dict]) -> dict:
    """Add up the dashboard's per-connection summaries into one set of numbers, plus a health score."""
    ok = [i for i in items if i.get("summary")]

    def sum_of(kind, key):
        return sum((i["summary"].get(key) or 0) for i in ok if i["kind"] == kind)

    t = {
        "users": sum_of("ad", "users"), "disabled": sum_of("ad", "disabled"), "locked": sum_of("ad", "locked"),
        "computers": sum_of("ad", "computers"),
        "vms": sum_of("vsphere", "vms"), "vms_on": sum_of("vsphere", "powered_on"), "hosts": sum_of("vsphere", "hosts"),
        "hosts_connected": sum_of("vsphere", "hosts_connected"),
        "storage_capacity": sum_of("vsphere", "capacity"), "storage_free": sum_of("vsphere", "free_space"),
        "servers": sum_of("idrac", "servers"), "servers_attention": sum_of("idrac", "attention"),
        "sw_nodes": sum_of("solarwinds", "nodes"), "sw_down": sum_of("solarwinds", "down"), "sw_alerts": sum_of("solarwinds", "alerts"),
        "vnx_capacity_gb": sum_of("vnx", "capacity_gb"), "vnx_free_gb": sum_of("vnx", "free_gb"), "vnx_faults": sum_of("vnx", "faults"),
        "connections": len(items), "connections_down": len(items) - len(ok),
    }
    t["storage_used_pct"] = (round((1 - t["storage_free"] / t["storage_capacity"]) * 100, 1)
                             if t["storage_capacity"] else None)
    t["problems"] = (t["connections_down"] + t["sw_down"] + t["vnx_faults"] + t["servers_attention"]
                     + (t["hosts"] - t["hosts_connected"]))
    # Health: the average of each area's share that's fine. Each part says why it isn't 100% and where to look.
    parts = []

    def part(label, good, total, detail, link):
        parts.append({"label": label, "pct": round(good / total * 100, 1), "detail": detail, "link": link, "_v": good / total})

    if t["connections"]:
        part("Connections answering", t["connections"] - t["connections_down"], t["connections"],
             f"{t['connections_down']} of {t['connections']} not answering" if t["connections_down"] else f"all {t['connections']} answering",
             "#/connections")
    if t["hosts"]:
        down = t["hosts"] - t["hosts_connected"]
        part("ESXi hosts connected", t["hosts_connected"], t["hosts"],
             f"{down} of {t['hosts']} hosts not connected" if down else f"all {t['hosts']} hosts connected", "#/vsphere")
    if t["sw_nodes"]:
        part("SolarWinds nodes up", t["sw_nodes"] - t["sw_down"], t["sw_nodes"],
             f"{t['sw_down']} of {t['sw_nodes']} nodes down" if t["sw_down"] else f"all {t['sw_nodes']} nodes up", "#/solarwinds")
    if t["servers"]:
        part("Servers healthy (iDRAC)", t["servers"] - t["servers_attention"], t["servers"],
             f"{t['servers_attention']} of {t['servers']} servers need attention" if t["servers_attention"]
             else f"all {t['servers']} servers healthy", "#/servers")
    arrays = [i for i in ok if i["kind"] == "vnx"]
    if arrays:
        good = sum(1 for i in arrays if i["summary"].get("faults_ok"))
        part("VNX arrays without faults", good, len(arrays),
             f"{t['vnx_faults']} fault(s) on {len(arrays) - good} array(s)" if good < len(arrays) else "no faults", "#/vnx")
    t["health_pct"] = round(sum(p["_v"] for p in parts) / len(parts) * 100, 1) if parts else None
    t["health_parts"] = [{k: v for k, v in p.items() if k != "_v"} for p in parts]
    return t


class MetricsStore:
    def __init__(self, data_dir: Path):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(data_dir / "metrics.db", check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS daily (day TEXT NOT NULL, key TEXT NOT NULL, value REAL, "
                         "PRIMARY KEY (day, key))")
        self._db.commit()

    def record(self, values: dict, day: date | None = None) -> None:
        day = (day or date.today()).isoformat()
        rows = [(day, k, values.get(k)) for k in KEYS if k in values]
        with self._lock:
            self._db.executemany("INSERT OR REPLACE INTO daily VALUES (?,?,?)", rows)
            self._db.commit()

    def history(self, days: int, today: date | None = None) -> dict:
        today = today or date.today()
        start = (today - timedelta(days=days - 1)).isoformat()
        with self._lock:
            rows = self._db.execute("SELECT day, key, value FROM daily WHERE day >= ? ORDER BY day", (start,)).fetchall()
        by_day: dict[str, dict] = {}
        for d, k, v in rows:
            by_day.setdefault(d, {})[k] = v
        dates = sorted(by_day)
        yesterday = None
        with self._lock:
            prev = self._db.execute("SELECT MAX(day) FROM daily WHERE day < ?", (today.isoformat(),)).fetchone()[0]
            if prev:
                yesterday = {k: v for k, v in self._db.execute("SELECT key, value FROM daily WHERE day = ?", (prev,))}
        return {"days": dates, "series": {k: [by_day[d].get(k) for d in dates] for k in KEYS},
                "previous": yesterday, "previous_day": prev}
