"""Employee activity: time & attendance, and the apps and websites in use, reported by the KasTec agent.

The agent (agent/KasTecAgent.ps1) runs as the signed-in user on each monitored PC. Every minute it knows which
application was in front, the website domain when that's a browser, and how many seconds there was input
(active) or none (idle). It sends those minutes to KasTec in batches. It never records keystrokes, window
titles, page addresses beyond the domain, or screenshots.

Only members of one AD group are monitored (settings: ad_connection_id + group). Times are shown in this
computer's local time zone, and the work schedule (late starts, early departures) uses it too.

Productivity comes from labels: each app ("app:outlook") or website ("web:youtube.com") is productive,
unproductive or neutral; anything unlabelled is undefined. Admins change labels on the Apps & websites tab.
"""

from __future__ import annotations

import json
import random
import re
import secrets
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

LABELS = ("productive", "neutral", "unproductive", "undefined")
ONLINE_SECONDS = 6 * 60          # an agent that reported within this is "online"
MAX_BATCH = 5000
APP = re.compile(r"^[a-z0-9][a-z0-9 ._+-]{0,79}$")
DOMAIN = re.compile(r"^(?=.{1,253}$)[a-z0-9-]+(\.[a-z0-9-]+)+$")
USER = re.compile(r"^[a-z0-9][a-z0-9._$-]{0,63}$")
COMPUTER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
BROWSERS = {"chrome", "msedge", "firefox", "iexplore", "brave", "opera"}
AI_DOMAINS = ("chatgpt.com", "chat.openai.com", "openai.com", "claude.ai", "gemini.google.com", "copilot.microsoft.com",
              "perplexity.ai", "poe.com", "deepseek.com", "grok.com", "meta.ai")
AI_APPS = {"chatgpt", "claude", "copilot"}

DEFAULT_LABELS = {
    **{f"app:{a}": "productive" for a in ("outlook", "excel", "winword", "powerpnt", "onenote", "teams", "ms-teams",
                                          "mstsc", "putty", "powershell", "pwsh", "code", "devenv", "notepad++",
                                          "vmware-view", "vmrc", "acrord32", "acrobat", "mmc", "ssms", "winscp", "kitty")},
    **{f"app:{a}": "neutral" for a in ("explorer", "notepad", "calc", "snippingtool", "msedge", "chrome", "firefox",
                                       "lockapp", "searchhost", "systemsettings")},
    **{f"web:{d}": "productive" for d in ("office.com", "microsoft365.com", "sharepoint.com", "outlook.office.com",
                                          "teams.microsoft.com", "servicenow.com", "github.com", "learn.microsoft.com",
                                          "dell.com", "vmware.com", "broadcom.com", "solarwinds.com")},
    **{f"web:{d}": "unproductive" for d in ("youtube.com", "facebook.com", "instagram.com", "tiktok.com", "netflix.com",
                                            "x.com", "twitter.com", "reddit.com", "twitch.tv", "pinterest.com",
                                            "snapchat.com", "spotify.com", "9gag.com", "primevideo.com")},
    **{f"web:{d}": "neutral" for d in ("google.com", "bing.com", "duckduckgo.com", "linkedin.com", "wikipedia.org")},
}
DEFAULT_NOTICE = ("This computer reports which applications and websites (site names only) are in use, and when it is "
                  "active or idle, to the IT department's KasTec console under company policy. Keystrokes, window "
                  "contents and screenshots are not recorded. Questions: contact IT.")


def default_settings() -> dict:
    return {"enabled": False, "ad_connection_id": "", "group": "KasTec Monitored", "agent_key": secrets.token_urlsafe(32),
            "schedule": {"start": "08:30", "end": "17:00", "days": [0, 1, 2, 3, 4], "grace_minutes": 10},
            "retention_days": 90, "notice": DEFAULT_NOTICE, "labels": {}}


class ActivityError(ValueError):
    pass


# --- what a sample means ----------------------------------------------------------------------

def label_for(app: str, domain: str, labels: dict) -> str:
    """A website's label wins over its browser's; a domain matches its parent domains too (mail.google.com -> google.com)."""
    merged = {**DEFAULT_LABELS, **labels}
    if domain:
        parts = domain.split(".")
        for i in range(len(parts) - 1):
            hit = merged.get("web:" + ".".join(parts[i:]))
            if hit:
                return hit
        if app in BROWSERS:
            return "undefined"
    return merged.get(f"app:{app}", "undefined")


def is_ai(app: str, domain: str) -> bool:
    return app in AI_APPS or any(domain == d or domain.endswith("." + d) for d in AI_DOMAINS)


def _hm(text: str) -> int:
    h, m = (text or "0:0").split(":")
    return int(h) * 60 + int(m)


def clean_settings(new: dict, old: dict) -> dict:
    out = {**old}
    for key in ("enabled",):
        if key in new:
            out[key] = bool(new[key])
    for key in ("ad_connection_id", "group"):
        if key in new:
            out[key] = str(new[key] or "").strip()[:200]
    if "notice" in new:
        out["notice"] = str(new["notice"] or "").strip()[:2000] or DEFAULT_NOTICE
    if "retention_days" in new:
        out["retention_days"] = max(7, min(730, int(new["retention_days"])))
    if "schedule" in new:
        s = new["schedule"] or {}
        start, end = str(s.get("start", "08:30")), str(s.get("end", "17:00"))
        if not (re.match(r"^\d{1,2}:\d{2}$", start) and re.match(r"^\d{1,2}:\d{2}$", end)) or _hm(start) >= _hm(end):
            raise ActivityError("The working day needs a start time before its end time (HH:MM).")
        days = sorted({int(d) for d in s.get("days", []) if 0 <= int(d) <= 6})
        out["schedule"] = {"start": start, "end": end, "days": days,
                           "grace_minutes": max(0, min(120, int(s.get("grace_minutes", 10))))}
    if "labels" in new:
        labels = {}
        for key, value in (new["labels"] or {}).items():
            if value not in ("productive", "neutral", "unproductive", "undefined"):
                raise ActivityError(f"Unknown label {value!r}")
            kind, _, name = key.partition(":")
            if kind not in ("app", "web") or not (APP.match(name) if kind == "app" else DOMAIN.match(name)):
                raise ActivityError(f"{key!r} isn't an app or website name")
            labels[key] = value
        out["labels"] = labels
    return out


# --- storage ----------------------------------------------------------------------------------

class ActivityStore:
    def __init__(self, data_dir: Path):
        self.settings_path = data_dir / "activity.json"
        self._lock = threading.Lock()
        self._db = sqlite3.connect(data_dir / "activity.db", check_same_thread=False)
        self._db.execute("""CREATE TABLE IF NOT EXISTS samples (
            minute INTEGER NOT NULL, user TEXT NOT NULL, computer TEXT NOT NULL, app TEXT NOT NULL,
            domain TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL, idle INTEGER NOT NULL, demo INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (minute, user, computer, app, domain))""")
        self._db.execute("CREATE INDEX IF NOT EXISTS samples_user ON samples (user, minute)")
        self._db.execute("CREATE TABLE IF NOT EXISTS people (user TEXT PRIMARY KEY, name TEXT, last_seen INTEGER, computer TEXT)")
        self._db.commit()
        self._last_prune = 0.0

    # settings
    def settings(self) -> dict:
        try:
            data = json.loads(self.settings_path.read_text())
        except (OSError, ValueError):
            data = default_settings()
            self.save_settings(data)
        return {**default_settings(), **data}

    def save_settings(self, data: dict) -> None:
        tmp = self.settings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.settings_path)

    def new_agent_key(self) -> str:
        s = self.settings()
        s["agent_key"] = secrets.token_urlsafe(32)
        self.save_settings(s)
        return s["agent_key"]

    # ingest
    def ingest(self, user: str, computer: str, samples: list[dict], demo: bool = False, name: str = "") -> int:
        rows = []
        for s in samples[:MAX_BATCH]:
            try:
                minute = int(datetime.fromisoformat(str(s["t"]).replace("Z", "+00:00")).timestamp()) // 60
            except (KeyError, ValueError, TypeError):
                continue
            app = str(s.get("app") or "").strip().lower().removesuffix(".exe")[:80]
            domain = str(s.get("domain") or "").strip().lower().removeprefix("www.")[:253]
            if not APP.match(app):
                app = "unknown"
            if domain and not DOMAIN.match(domain):
                domain = ""
            active = max(0, min(60, int(s.get("active") or 0)))
            idle = max(0, min(60 - active, int(s.get("idle") or 0)))
            if active or idle:
                rows.append((minute, user, computer, app, domain, active, idle, int(demo)))
        if not rows:
            return 0
        last = max(r[0] for r in rows) * 60
        with self._lock:
            self._db.executemany("INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?,?)", rows)
            self._db.execute("""INSERT INTO people (user, name, last_seen, computer) VALUES (?,?,?,?)
                ON CONFLICT(user) DO UPDATE SET last_seen = MAX(last_seen, excluded.last_seen),
                computer = CASE WHEN excluded.last_seen >= last_seen THEN excluded.computer ELSE computer END,
                name = CASE WHEN excluded.name <> '' THEN excluded.name ELSE name END""", (user, name, last, computer))
            self._db.commit()
        self._prune()
        return len(rows)

    def set_names(self, names: dict[str, str]) -> None:
        with self._lock:
            self._db.executemany("UPDATE people SET name = ? WHERE user = ? AND ? <> ''",
                                 [(n, u, n) for u, n in names.items()])
            self._db.commit()

    def _prune(self) -> None:
        if time.time() - self._last_prune < 3600:
            return
        self._last_prune = time.time()
        cutoff = int(time.time()) // 60 - self.settings()["retention_days"] * 1440
        with self._lock:
            self._db.execute("DELETE FROM samples WHERE minute < ?", (cutoff,))
            self._db.commit()

    def clear_demo(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM samples WHERE demo = 1")
            self._db.execute("DELETE FROM people WHERE user NOT IN (SELECT DISTINCT user FROM samples)")
            self._db.commit()

    def has_demo(self) -> bool:
        return self._db.execute("SELECT 1 FROM samples WHERE demo = 1 LIMIT 1").fetchone() is not None

    def _rows(self, start: datetime, end: datetime, user: str | None = None) -> list[tuple]:
        q = "SELECT minute, user, computer, app, domain, active, idle FROM samples WHERE minute >= ? AND minute < ?"
        args: list = [int(start.timestamp()) // 60, int(end.timestamp()) // 60]
        if user:
            q += " AND user = ?"
            args.append(user)
        with self._lock:
            return self._db.execute(q, args).fetchall()

    def people(self) -> dict[str, dict]:
        with self._lock:
            return {u: {"user": u, "name": n or u, "last_seen": ls, "computer": c}
                    for u, n, ls, c in self._db.execute("SELECT user, name, last_seen, computer FROM people")}


# --- periods ----------------------------------------------------------------------------------

def period_bounds(period: str, day: date) -> tuple[datetime, datetime, list[date]]:
    if period == "day":
        days = [day]
    elif period == "week":
        monday = day - timedelta(days=day.weekday())
        days = [monday + timedelta(days=i) for i in range(7)]
    elif period == "month":
        first = day.replace(day=1)
        nxt = (first + timedelta(days=32)).replace(day=1)
        days = [first + timedelta(days=i) for i in range((nxt - first).days)]
    else:
        raise ActivityError("The period must be day, week or month.")
    start = datetime.combine(days[0], datetime.min.time())
    end = datetime.combine(days[-1] + timedelta(days=1), datetime.min.time())
    return start, end, days


def previous_day(period: str, day: date) -> date:
    if period == "day":
        return day - timedelta(days=1)
    if period == "week":
        return day - timedelta(days=7)
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


def _local(minute: int) -> datetime:
    return datetime.fromtimestamp(minute * 60)


def _in_schedule(t: datetime, sched: dict) -> bool:
    m = t.hour * 60 + t.minute
    return t.weekday() in sched["days"] and _hm(sched["start"]) <= m < _hm(sched["end"])


# --- the numbers on the pages -----------------------------------------------------------------

def _totals(rows, labels, sched, scheduled_only):
    by_label = {k: 0 for k in LABELS}
    ai = idle = 0
    for minute, _u, _c, app, domain, active, idl in rows:
        t = _local(minute)
        if scheduled_only and not _in_schedule(t, sched):
            continue
        by_label[label_for(app, domain, labels)] += active
        idle += idl
        if is_ai(app, domain):
            ai += active
    active = sum(by_label.values())
    pct = lambda n: round(n / active * 100, 1) if active else None  # noqa: E731
    return {"active": active, "idle": idle, "by_label": by_label, "ai": ai,
            "work_related_pct": pct(by_label["productive"] + by_label["neutral"]),
            "productive_pct": pct(by_label["productive"]), "unproductive_pct": pct(by_label["unproductive"]),
            "ai_pct": pct(ai)}


def attendance(rows, sched, days: list[date], today: date, now_minutes: int) -> list[dict]:
    """One row per person per day they were active: first/last activity, active and idle time, late/early."""
    per: dict[tuple, dict] = {}
    for minute, user, computer, _app, _domain, active, idle in rows:
        t = _local(minute)
        d = per.setdefault((user, t.date()), {"user": user, "day": t.date().isoformat(), "first": t, "last": t,
                                              "active": 0, "idle": 0, "computers": set()})
        if active:
            d["first"], d["last"] = min(d["first"], t), max(d["last"], t)
        d["active"] += active
        d["idle"] += idle
        d["computers"].add(computer)
    out = []
    start, end, grace = _hm(sched["start"]), _hm(sched["end"]), sched["grace_minutes"]
    for (user, day), d in sorted(per.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        if not d["active"]:
            continue
        workday = day.weekday() in sched["days"]
        first = d["first"].hour * 60 + d["first"].minute
        last = d["last"].hour * 60 + d["last"].minute + 1
        day_over = day < today or now_minutes >= end
        out.append({"user": user, "day": d["day"], "first": d["first"].strftime("%H:%M"), "last": d["last"].strftime("%H:%M"),
                    "active": d["active"], "idle": d["idle"], "computers": sorted(d["computers"]),
                    "late": workday and first > start + grace, "early": workday and day_over and last < end - grace})
    return out


def summary(store: ActivityStore, period: str, day: date, user: str | None, scheduled_only: bool,
            now: datetime | None = None) -> dict:
    s = store.settings()
    labels, sched = s["labels"], s["schedule"]
    now = now or datetime.now()
    start, end, days = period_bounds(period, day)
    rows = store._rows(start, end, user)
    pstart, pend, pdays = period_bounds(period, previous_day(period, day))
    prev_rows = store._rows(pstart, pend, user)
    cur, prev = _totals(rows, labels, sched, scheduled_only), _totals(prev_rows, labels, sched, scheduled_only)
    att = attendance(rows, sched, days, now.date(), now.hour * 60 + now.minute)
    patt = attendance(prev_rows, sched, pdays, now.date(), now.hour * 60 + now.minute)

    def delta(key):
        a, b = cur[key], prev[key]
        return None if a is None or b is None else round(a - b, 1)

    # Timeline: share of active time per label, per hour for a day, per day otherwise.
    buckets: dict[str, dict] = {}
    keys = [f"{h:02d}:00" for h in range(24)] if period == "day" else [d.isoformat() for d in days]
    for k in keys:
        buckets[k] = {k2: 0 for k2 in LABELS}
    for minute, _u, _c, app, domain, active, _i in rows:
        t = _local(minute)
        if scheduled_only and not _in_schedule(t, sched):
            continue
        k = f"{t.hour:02d}:00" if period == "day" else t.date().isoformat()
        if k in buckets:
            buckets[k][label_for(app, domain, labels)] += active
    timeline = [{"key": k, "active": sum(v.values()), **v} for k, v in buckets.items()]

    # Rankings.
    per_user: dict[str, dict] = {}
    per_thing: dict[str, dict] = {}
    for minute, u, _c, app, domain, active, _i in rows:
        if scheduled_only and not _in_schedule(_local(minute), sched):
            continue
        lab = label_for(app, domain, labels)
        pu = per_user.setdefault(u, {"active": 0, "productive": 0})
        pu["active"] += active
        pu["productive"] += active if lab == "productive" else 0
        key = f"web:{domain}" if domain else f"app:{app}"
        pt = per_thing.setdefault(key, {"key": key, "name": domain or app, "kind": "website" if domain else "app",
                                        "label": lab, "active": 0})
        pt["active"] += active
    people = store.people()
    name = lambda u: people.get(u, {}).get("name") or u  # noqa: E731
    productive_rank = sorted(({"user": u, "name": name(u), "active": v["active"],
                               "pct": round(v["productive"] / v["active"] * 100, 1) if v["active"] else 0}
                              for u, v in per_user.items() if v["active"] >= 600), key=lambda r: -r["pct"])[:10]
    total = cur["active"] or 1
    unproductive_rank = sorted(({**t, "pct": round(t["active"] / total * 100, 1)} for t in per_thing.values()
                                if t["label"] == "unproductive"), key=lambda r: -r["active"])[:10]
    events: dict[str, dict] = {}
    for a in att:
        e = events.setdefault(a["user"], {"user": a["user"], "name": name(a["user"]), "late": 0, "early": 0})
        e["late"] += a["late"]
        e["early"] += a["early"]
    events_rank = sorted((e for e in events.values() if e["late"] or e["early"]),
                         key=lambda e: -(e["late"] + e["early"]))[:10]

    late, early = sum(a["late"] for a in att), sum(a["early"] for a in att)
    return {
        "period": period, "from": days[0].isoformat(), "to": days[-1].isoformat(), "scheduled_only": scheduled_only,
        "people": len(per_user),
        "kpis": {"work_related_pct": cur["work_related_pct"], "productive_pct": cur["productive_pct"],
                 "unproductive_pct": cur["unproductive_pct"], "ai_pct": cur["ai_pct"],
                 "late_starts": late, "early_departures": early, "active": cur["active"], "idle": cur["idle"]},
        "deltas": {"work_related_pct": delta("work_related_pct"), "productive_pct": delta("productive_pct"),
                   "unproductive_pct": delta("unproductive_pct"), "ai_pct": delta("ai_pct"),
                   "late_starts": late - sum(a["late"] for a in patt), "early_departures": early - sum(a["early"] for a in patt)},
        "by_label": cur["by_label"], "timeline": timeline,
        "rankings": {"productive": productive_rank, "unproductive": unproductive_rank, "events": events_rank},
    }


def live(store: ActivityStore, now: datetime | None = None) -> list[dict]:
    """Everyone seen in the last day: online or not, what's in front of them now, and today's totals."""
    s = store.settings()
    now = now or datetime.now()
    start = datetime.combine(now.date(), datetime.min.time())
    rows = store._rows(min(start, now - timedelta(days=1)), now + timedelta(minutes=2))
    today: dict[str, dict] = {}
    latest: dict[str, tuple] = {}
    for minute, u, c, app, domain, active, idle in rows:
        if minute >= int(start.timestamp()) // 60:
            t = today.setdefault(u, {"active": 0, "idle": 0, "productive": 0})
            t["active"] += active
            t["idle"] += idle
            t["productive"] += active if label_for(app, domain, s["labels"]) == "productive" else 0
        if active and (u not in latest or minute >= latest[u][0]):
            if u in latest and minute == latest[u][0] and active < latest[u][3]:
                continue
            latest[u] = (minute, app, domain, active, c)
    out = []
    for u, p in store.people().items():
        if not p["last_seen"] or p["last_seen"] < now.timestamp() - 86400:
            continue
        t = today.get(u, {"active": 0, "idle": 0, "productive": 0})
        lt = latest.get(u)
        out.append({"user": u, "name": p["name"], "computer": p["computer"],
                    "online": now.timestamp() - p["last_seen"] <= ONLINE_SECONDS,
                    "last_seen": datetime.fromtimestamp(p["last_seen"]).isoformat(timespec="minutes"),
                    "now_app": lt[1] if lt else None, "now_domain": lt[2] if lt else None,
                    "now_label": label_for(lt[1], lt[2], s["labels"]) if lt else None,
                    "today_active": t["active"], "today_idle": t["idle"],
                    "today_productive_pct": round(t["productive"] / t["active"] * 100, 1) if t["active"] else None})
    return sorted(out, key=lambda r: (not r["online"], r["name"].lower()))


def apps(store: ActivityStore, period: str, day: date) -> list[dict]:
    s = store.settings()
    start, end, _ = period_bounds(period, day)
    things: dict[str, dict] = {}
    for _m, u, _c, app, domain, active, _i in store._rows(start, end):
        key = f"web:{domain}" if domain else f"app:{app}"
        t = things.setdefault(key, {"key": key, "name": domain or app, "kind": "website" if domain else "app",
                                    "via": app if domain else None, "active": 0, "users": set()})
        t["active"] += active
        t["users"].add(u)
    out = []
    for t in things.values():
        app, domain = (t["via"] or "", t["name"]) if t["kind"] == "website" else (t["name"], "")
        out.append({**t, "users": len(t["users"]), "label": label_for(app, domain, s["labels"]),
                    "custom": t["key"] in s["labels"], "ai": is_ai(app, domain)})
    return sorted(out, key=lambda t: -t["active"])


# --- demo data, so the pages can be tried before any agent is deployed --------------------------

def load_demo(store: ActivityStore, names: dict[str, str], days: int = 35, now: datetime | None = None) -> int:
    now = now or datetime.now()
    work = [("outlook", ""), ("excel", ""), ("teams", ""), ("winword", ""), ("mstsc", ""), ("putty", ""),
            ("msedge", "servicenow.com"), ("chrome", "office.com"), ("msedge", "learn.microsoft.com"), ("powershell", "")]
    neutral = [("explorer", ""), ("chrome", "google.com"), ("msedge", "bing.com"), ("chrome", "linkedin.com")]
    fun = [("chrome", "youtube.com"), ("msedge", "facebook.com"), ("chrome", "instagram.com"), ("chrome", "netflix.com")]
    other = [("chrome", "chatgpt.com"), ("msedge", "copilot.microsoft.com"), ("spotify", ""), ("chrome", "weather.com")]
    total = 0
    for i, (user, name) in enumerate(sorted(names.items())):
        rng = random.Random(f"activity-{user}")
        focus = rng.uniform(0.62, 0.9)
        computer = f"BTC-WS-{100 + i:03d}"
        for back in range(days, -1, -1):
            day = now.date() - timedelta(days=back)
            if day.weekday() >= 5 or rng.random() < 0.05:
                continue
            start = 8 * 60 + rng.randint(2, 38) + (rng.randint(15, 70) if rng.random() < 0.06 else 0)
            end = 17 * 60 + rng.randint(-5, 50) - (rng.randint(30, 90) if rng.random() < 0.05 else 0)
            samples, current = [], rng.choice(work)
            for m in range(start, end):
                t = datetime.combine(day, datetime.min.time()) + timedelta(minutes=m)
                if t > now:
                    break
                if 12 * 60 + 15 <= m < 12 * 60 + 55 and rng.random() < 0.9:
                    continue  # lunch
                if rng.random() < 0.12:
                    r = rng.random()
                    current = (rng.choice(work) if r < focus else rng.choice(neutral) if r < focus + 0.08
                               else rng.choice(other) if r < focus + 0.12 else rng.choice(fun))
                active = rng.randint(30, 60) if rng.random() > 0.1 else rng.randint(0, 20)
                samples.append({"t": t.astimezone().isoformat(), "app": current[0], "domain": current[1],
                                "active": active, "idle": 60 - active})
            total += store.ingest(user, computer, samples, demo=True, name=name)
    return total
