"""Dashboard totals, the health score, and the daily snapshots behind the trends."""

from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.metrics import MetricsStore, totals

ITEMS = [
    {"kind": "ad", "summary": {"users": 100, "disabled": 5, "locked": 3, "computers": 40}},
    {"kind": "vsphere", "summary": {"vms": 20, "powered_on": 16, "hosts": 4, "hosts_connected": 3,
                                    "capacity": 1000, "free_space": 250}},
    {"kind": "solarwinds", "summary": {"nodes": 10, "down": 1, "alerts": 4}},
    {"kind": "vnx", "summary": {"capacity_gb": 100, "free_gb": 40, "faults": 1, "faults_ok": False}},
    {"kind": "idrac", "summary": None, "error": "unreachable"},
]


def test_totals_and_health():
    t = totals(ITEMS)
    assert (t["users"], t["locked"], t["vms_on"], t["hosts_connected"], t["storage_used_pct"]) == (100, 3, 16, 3, 75.0)
    assert (t["connections"], t["connections_down"], t["vnx_faults"]) == (5, 1, 1)
    assert t["problems"] == 1 + 1 + 1 + 0 + 1          # unreachable + SW down + VNX fault + host not connected
    # (4/5 connections + 3/4 hosts + 9/10 nodes + 0/1 arrays) / 4
    assert t["health_pct"] == round((0.8 + 0.75 + 0.9 + 0) / 4 * 100, 1)
    assert [p["label"] for p in t["health_parts"]][0] == "Connections answering"
    assert all(p["detail"] and p["link"].startswith("#/") for p in t["health_parts"])
    assert totals([])["health_pct"] is None


def test_history(tmp_path):
    m = MetricsStore(tmp_path)
    today = date(2026, 11, 17)
    for i, locked in enumerate([5, 4, 7]):
        m.record({"locked": locked, "users": 100 + i}, day=today - timedelta(days=2 - i))
    m.record({"locked": 2, "users": 103}, day=today)          # a later load the same day replaces it
    h = m.history(7, today=today)
    assert h["days"] == ["2026-11-15", "2026-11-16", "2026-11-17"]
    assert h["series"]["locked"] == [5, 4, 2] and h["series"]["vms"] == [None, None, None]
    assert h["previous_day"] == "2026-11-16" and h["previous"]["locked"] == 4
    assert m.history(2, today=today)["days"] == ["2026-11-16", "2026-11-17"]


def test_dashboard_records_today(tmp_path):
    c = TestClient(create_app(Settings(data_dir=tmp_path, secret_key=None, admin_password="correct horse battery",
                                       secure_cookies=False, session_hours=1, shell="off")))
    c.post("/api/login", json={"password": "correct horse battery"})
    c.post("/api/connections", json={"kind": "ad", "name": "Demo AD", "demo": True})
    c.post("/api/connections", json={"kind": "vsphere", "name": "Demo vCenter", "demo": True})
    assert c.get("/api/dashboard").status_code == 200
    h = c.get("/api/dashboard/history", params={"days": 30}).json()
    assert h["days"] == [date.today().isoformat()] and h["series"]["users"] == [48]
    assert h["today"]["vms"] == 20 and h["today"]["health_pct"] is not None and h["previous"] is None
