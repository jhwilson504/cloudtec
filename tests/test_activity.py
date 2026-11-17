"""Employee activity: labels, storing what agents send, the numbers on the pages, and the agent API."""

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import activity
from app.activity import ActivityStore, label_for
from app.config import Settings
from app.main import create_app

DAY = date(2026, 11, 10)   # a Tuesday


def at(d: date, hhmm: str) -> str:
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(d, datetime.min.time()).replace(hour=h, minute=m).astimezone().isoformat()


def minutes(d, start, count, app, domain="", active=60):
    t0 = datetime.combine(d, datetime.min.time()).replace(hour=int(start[:2]), minute=int(start[3:]))
    return [{"t": (t0 + timedelta(minutes=i)).astimezone().isoformat(), "app": app, "domain": domain,
             "active": active, "idle": 60 - active} for i in range(count)]


def test_labels():
    assert label_for("outlook", "", {}) == "productive"
    assert label_for("chrome", "www.youtube.com".removeprefix("www."), {}) == "unproductive"
    assert label_for("chrome", "mail.google.com", {}) == "neutral"          # parent domain google.com
    assert label_for("chrome", "intranet.example", {}) == "undefined"       # a browser page nobody labelled
    assert label_for("msedge", "", {}) == "neutral"                          # the browser itself, no page
    assert label_for("chrome", "youtube.com", {"web:youtube.com": "productive"}) == "productive"
    assert activity.is_ai("chrome", "chatgpt.com") and activity.is_ai("msedge", "x.copilot.microsoft.com")


def test_ingest_cleans_and_never_double_counts(tmp_path):
    store = ActivityStore(tmp_path)
    batch = minutes(DAY, "09:00", 3, "OUTLOOK.EXE") + [
        {"t": at(DAY, "09:10"), "app": "chrome", "domain": "WWW.YouTube.com", "active": 90, "idle": 30},
        {"t": "not a time", "app": "x", "active": 10},
        {"t": at(DAY, "09:11"), "app": "bad;app|name", "domain": "not a domain!", "active": 20, "idle": 0}]
    assert store.ingest("jdoe", "pc1", batch) == 5
    assert store.ingest("jdoe", "pc1", batch) == 5   # the agent resending after a failed upload
    rows = sorted(store._rows(datetime(2026, 11, 10), datetime(2026, 11, 11)))
    assert len(rows) == 5
    assert rows[0][3:] == ("outlook", "", 60, 0)
    assert rows[3][3:] == ("chrome", "youtube.com", 60, 0)    # clamped to one minute
    assert rows[4][3:] == ("unknown", "", 20, 0)


def build(tmp_path):
    store = ActivityStore(tmp_path)
    s = store.settings()
    s["schedule"] = {"start": "08:30", "end": "17:00", "days": [0, 1, 2, 3, 4], "grace_minutes": 10}
    store.save_settings(s)
    # Ann: in at 08:35, 6 h of Excel, 1 h of YouTube, 1 h of Chrome on google.com, out at 16:35 (early).
    store.ingest("ann", "pc1", minutes(DAY, "08:35", 360, "excel") + minutes(DAY, "14:35", 60, "chrome", "youtube.com")
                 + minutes(DAY, "15:35", 60, "chrome", "google.com"), name="Ann Smith")
    # Bob: in at 09:05 (late), Outlook until 17:05, plus 30 min of ChatGPT; and 1 h after hours (not scheduled).
    store.ingest("bob", "pc2", minutes(DAY, "09:05", 450, "outlook") + minutes(DAY, "16:35", 30, "msedge", "chatgpt.com")
                 + minutes(DAY, "18:00", 60, "excel"), name="Bob Jones")
    # The week before, for the deltas: Ann only, all Excel.
    store.ingest("ann", "pc1", minutes(DAY - timedelta(days=7), "08:30", 120, "excel"))
    return store


def test_summary(tmp_path):
    store = build(tmp_path)
    now = datetime(2026, 11, 12, 12, 0)
    s = activity.summary(store, "week", DAY, None, True, now=now)
    k = s["kpis"]
    # Scheduled hours only: Bob's 18:00 hour is left out; his 16:35-17:05 ChatGPT overlaps Outlook minutes.
    assert k["late_starts"] == 1 and k["early_departures"] == 1
    assert k["productive_pct"] is not None and 0 < k["unproductive_pct"] < 10
    assert k["ai_pct"] > 0
    assert s["deltas"]["late_starts"] == 1
    assert s["rankings"]["unproductive"][0]["name"] == "youtube.com"
    assert [r["name"] for r in s["rankings"]["events"]] == ["Ann Smith", "Bob Jones"]
    assert len(s["timeline"]) == 7 and s["timeline"][1]["key"] == "2026-11-10" and s["timeline"][1]["active"] > 0
    everything = activity.summary(store, "week", DAY, None, False, now=now)
    # Bob's hour after work, plus his 16:35-17:05 ChatGPT minutes from 17:00 on.
    assert everything["kpis"]["active"] - k["active"] == 3600 + 5 * 60
    just_ann = activity.summary(store, "day", DAY, "ann", True, now=now)
    assert just_ann["people"] == 1 and len(just_ann["timeline"]) == 24


def test_attendance_and_apps(tmp_path):
    store = build(tmp_path)
    now = datetime(2026, 11, 12, 12, 0)
    start, end, days = activity.period_bounds("day", DAY)
    att = {a["user"]: a for a in activity.attendance(store._rows(start, end), store.settings()["schedule"], days, now.date(), 720)}
    assert (att["ann"]["first"], att["ann"]["last"], att["ann"]["late"], att["ann"]["early"]) == ("08:35", "16:34", False, True)
    assert (att["bob"]["first"], att["bob"]["late"], att["bob"]["early"]) == ("09:05", True, False)
    things = {t["key"]: t for t in activity.apps(store, "day", DAY)}
    assert things["web:youtube.com"]["label"] == "unproductive" and things["web:youtube.com"]["via"] == "chrome"
    assert things["web:chatgpt.com"]["ai"] and things["app:excel"]["users"] == 2


def test_live(tmp_path):
    store = ActivityStore(tmp_path)
    now = datetime.now().replace(second=0, microsecond=0)
    store.ingest("ann", "pc1", [{"t": (now - timedelta(minutes=2)).astimezone().isoformat(), "app": "excel", "active": 50, "idle": 10}],
                 name="Ann Smith")
    store.ingest("bob", "pc2", [{"t": (now - timedelta(hours=3)).astimezone().isoformat(), "app": "outlook", "active": 60, "idle": 0}])
    rows = {r["user"]: r for r in activity.live(store, now=now)}
    assert rows["ann"]["online"] and rows["ann"]["now_app"] == "excel" and rows["ann"]["name"] == "Ann Smith"
    assert not rows["bob"]["online"]


def test_settings_validation():
    old = activity.default_settings()
    new = activity.clean_settings({"schedule": {"start": "09:00", "end": "17:30", "days": [0, 4, 9], "grace_minutes": 500},
                                   "labels": {"web:youtube.com": "neutral", "app:excel": "productive"}, "retention_days": 3}, old)
    assert new["schedule"] == {"start": "09:00", "end": "17:30", "days": [0, 4], "grace_minutes": 120}
    assert new["retention_days"] == 7 and new["labels"]["web:youtube.com"] == "neutral"
    for bad in ({"schedule": {"start": "17:00", "end": "09:00"}}, {"labels": {"web:bad domain": "neutral"}},
                {"labels": {"app:excel": "great"}}):
        with pytest.raises(activity.ActivityError):
            activity.clean_settings(bad, old)


# --- the API -------------------------------------------------------------------------------------

PASSWORD = "correct horse battery"


@pytest.fixture
def api(tmp_path):
    c = TestClient(create_app(Settings(data_dir=tmp_path, secret_key=None, admin_password=PASSWORD,
                                       secure_cookies=False, session_hours=1, shell="off")))
    c.post("/api/login", json={"password": PASSWORD})
    return c


def test_agent_api(api, tmp_path):
    ad = api.post("/api/connections", json={"kind": "ad", "name": "Demo AD", "demo": True,
                                            "config": {"base_dn": "DC=btcbahamas,DC=com"}}).json()
    key = api.get("/api/activity/settings").json()["agent_key"]
    h = {"X-KasTec-Agent-Key": key}
    assert api.get("/api/activity/agent/config", params={"user": "BTC\\x"}).status_code == 401
    assert api.get("/api/activity/agent/config", params={"user": "BTC\\x"}, headers={"X-KasTec-Agent-Key": "nope"}).status_code == 401
    # Off by default: nobody is monitored.
    assert api.get("/api/activity/agent/config", params={"user": "BTC\\x"}, headers=h).json()["monitor"] is False

    s = api.put("/api/activity/settings", json={"enabled": True, "ad_connection_id": ad["id"], "group": "KasTec Monitored"}).json()
    assert s["members"] == 24 and s["membership_error"] is None
    everyone = api.get(f"/api/ad/{ad['id']}/users").json()
    group = {u["username"] for u in [u for u in everyone if u["enabled"]][:24]}  # the demo group: first 24 enabled users
    inside = sorted(group)[0]
    outside = next(u["username"] for u in everyone if u["username"] not in group)
    cfg = api.get("/api/activity/agent/config", params={"user": f"BTCBAHAMAS\\{inside.upper()}"}, headers=h).json()
    assert cfg["monitor"] is True and "Keystrokes" in cfg["notice"]
    assert api.get("/api/activity/agent/config", params={"user": f"BTC\\{outside}"}, headers=h).json()["monitor"] is False

    now = datetime.now().replace(second=0, microsecond=0)
    body = {"user": f"BTCBAHAMAS\\{inside}", "computer": "BTC-WS-101", "agent": "1.0",
            "samples": [{"t": (now - timedelta(minutes=i)).astimezone().isoformat(), "app": "excel", "domain": "", "active": 55, "idle": 5}
                        for i in range(1, 6)]}
    assert api.post("/api/activity/agent/report", json=body, headers=h).json() == {"accepted": 5, "monitor": True}
    assert api.post("/api/activity/agent/report", json={**body, "user": f"BTC\\{outside}"}, headers=h).json()["accepted"] == 0
    assert api.post("/api/activity/agent/report", json={**body, "computer": "bad name!"}, headers=h).status_code == 422
    live = api.get("/api/activity/live").json()
    assert live[0]["user"] == inside and live[0]["online"] and live[0]["name"] != inside   # the AD display name

    # A new key locks out agents still using the old one.
    api.post("/api/activity/settings/new-key")
    assert api.get("/api/activity/agent/config", params={"user": "BTC\\x"}, headers=h).status_code == 401


def test_pages_demo_data_and_agent_download(api, tmp_path):
    api.post("/api/connections", json={"kind": "ad", "name": "Demo AD", "demo": True})
    r = api.post("/api/activity/demo").json()
    assert r["people"] == 24 and r["minutes"] > 1000
    s = api.get("/api/activity/summary", params={"period": "month"}).json()
    assert s["people"] > 0 and s["kpis"]["productive_pct"] > 40 and len(s["rankings"]["productive"]) == 10
    assert api.get("/api/activity/summary", params={"period": "year"}).status_code == 422
    assert api.get("/api/activity/attendance", params={"period": "week"}).status_code == 200
    apps = api.get("/api/activity/apps", params={"period": "month"}).json()
    assert any(a["key"] == "web:youtube.com" and a["label"] == "unproductive" for a in apps)
    assert api.get("/api/activity/settings").json()["demo_data"] is True
    api.delete("/api/activity/demo")
    assert api.get("/api/activity/settings").json()["demo_data"] is False

    agent = api.get("/api/activity/agent.ps1", params={"server": "https://kastec.btcbahamas.com:8443"})
    key = api.get("/api/activity/settings").json()["agent_key"]
    assert agent.status_code == 200 and "https://kastec.btcbahamas.com:8443" in agent.text and key in agent.text
    assert "__KASTEC" not in agent.text
    assert api.get("/api/activity/agent.ps1", params={"server": "javascript:alert(1)"}).status_code == 422
    assert "downloaded the agent" in (tmp_path / "audit.log").read_text()


def test_only_admins_see_activity(api):
    api.post("/api/users", json={"username": "op", "password": "operator-pass-1", "role": "operator"})
    api.post("/api/logout")
    api.post("/api/login", json={"username": "op", "password": "operator-pass-1"})
    for path in ("/api/activity/summary", "/api/activity/live", "/api/activity/settings", "/api/activity/agent.ps1"):
        assert api.get(path).status_code == 403


def test_agent_file_is_the_one_in_the_repo():
    text = (Path(__file__).parent.parent / "agent" / "KasTecAgent.ps1").read_text()
    assert "__KASTEC_SERVER__" in text and "__KASTEC_AGENT_KEY__" in text
    for never in ("SetWindowsHookEx", "GetAsyncKeyState", "CopyFromScreen", "GetKeyboardState"):
        assert never not in text   # no keylogging, no screenshots
