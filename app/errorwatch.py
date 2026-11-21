"""Error watch: KasTec notes its own unexpected errors, so they can be reported and fixed.

What is recorded: unexpected server errors (a bug, not "vCenter is down") and pages that crash in the browser.
Each distinct error is kept once, with how often and when it happened.

What can leave this computer: only after an admin turns it on (or presses Send), a report goes to a GitHub
issue so the KasTec maintenance agent can look at it. The repository may be public, so a report holds only
the kind of error, where in KasTec's own code it happened, and KasTec's version. Addresses, host names,
user names, directory paths and anything in quotes are taken out first (see redact), and the admin sees
the exact text before anything is sent.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import traceback
from pathlib import Path

import httpx
from cryptography.fernet import Fernet, InvalidToken

MAX_ERRORS = 200
LABEL = "kastec-auto"
APP_DIR = Path(__file__).resolve().parent

_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?(?::\d+)?\b")
_IPV6 = re.compile(r"\b(?:[0-9a-f]{1,4}:){2,7}[0-9a-f]{0,4}\b", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s'\"<>)]+", re.I)
_DN = re.compile(r"\b(?:CN|OU|DC|O|L)=[^,;'\"\n]+(?:,\s*(?:CN|OU|DC|O|L)=[^,;'\"\n]+)*", re.I)
_UNC = re.compile(r"(?:\\\\[^\s'\"]+|\b[A-Za-z]:\\[^\s'\"]*)")
_DOMAIN_USER = re.compile(r"\b[\w-]+\\[\w.$-]+")
_FQDN = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.I)
_QUOTED = re.compile(r"(['\"])(.*?)\1")
_HOSTISH = re.compile(r"\b(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9][A-Za-z0-9_-]{2,}\b")
# Code words with digits that say nothing about the network.
_SAFE_WORDS = {"utf8", "utf-8", "utf16", "base64", "sha1", "sha256", "md5", "x509", "http2", "int32", "int64", "float64",
               "ipv4", "ipv6", "tls1", "tls12", "tls13", "ssl3", "es6", "h2", "e2e", "2xx", "4xx", "5xx"}
_IDENT = re.compile(r"^[A-Za-z_$][A-Za-z_$]{0,30}$")   # a code word like innerHTML: safe to keep


def redact(text: str, extra: list[str] | None = None) -> str:
    """Take out anything that could identify the network or its people; keep what describes the bug."""
    s = str(text or "")
    for term in sorted({t for t in extra or [] if t and len(t) >= 3}, key=len, reverse=True):
        s = re.sub(re.escape(term), "‹name›", s, flags=re.I)
    s = _URL.sub("‹url›", s)
    s = _EMAIL.sub("‹email›", s)
    s = _DN.sub("‹directory path›", s)
    s = _UNC.sub("‹path›", s)
    s = _DOMAIN_USER.sub("‹account›", s)
    s = _IPV4.sub("‹ip›", s)
    s = _IPV6.sub("‹ip›", s)
    s = _FQDN.sub(lambda m: m.group(0) if m.group(0).endswith((".js", ".py", ".css", ".html")) else "‹host›", s)
    s = _QUOTED.sub(lambda m: m.group(0) if _IDENT.match(m.group(2)) else f"{m.group(1)}…{m.group(1)}", s)
    s = _HOSTISH.sub(lambda m: m.group(0) if m.group(0).lower() in _SAFE_WORDS else "‹name›", s)
    return s[:400]


def python_frames(exc: BaseException) -> list[str]:
    """Where in KasTec's own code the error happened (file, function, line), innermost last."""
    out = []
    for f in traceback.extract_tb(exc.__traceback__):
        try:
            rel = Path(f.filename).resolve().relative_to(APP_DIR.parent)
        except ValueError:
            continue   # a library's code: not ours to fix, and its paths say where Python is installed
        out.append(f"{rel.as_posix()}:{f.lineno} in {f.name}")
    return out[-8:]


def js_frames(stack: str) -> list[str]:
    """KasTec's own frames from a browser stack trace: 'app.js:123 in renderX'."""
    out = []
    for line in (stack or "").splitlines():
        m = re.search(r"at (?:async )?(?:(\S+) \()?\S*?/static/([\w./-]+\.js):(\d+)", line) or re.search(r"(\w*)@\S*?/static/([\w./-]+\.js):(\d+)", line)
        if m:
            out.append(f"static/{m.group(2)}:{m.group(3)} in {m.group(1) or '(anonymous)'}")
    return out[:8]


def fingerprint(kind: str, etype: str, frames: list[str], message: str) -> str:
    # Line numbers move with every edit; the same bug should keep the same fingerprint.
    where = [re.sub(r":\d+ in", " in", f) for f in frames[-3:]]
    what = re.sub(r"\d+", "#", message)[:120]
    return hashlib.sha1(json.dumps([kind, etype, where, what]).encode()).hexdigest()[:12]


class ErrorWatch:
    def __init__(self, data_dir: Path, key: bytes, version: str, transport: httpx.BaseTransport | None = None):
        self.path = data_dir / "errors.json"
        self.settings_path = data_dir / "errorwatch.json"
        self._fernet = Fernet(key)
        self.version = version
        self._lock = threading.Lock()
        self._transport = transport
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.extra_terms: list[str] = []   # connection names, hosts, domain: always taken out of reports

    # --- storage -------------------------------------------------------------------------------------

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.path)

    def settings(self) -> dict:
        try:
            s = json.loads(self.settings_path.read_text())
        except (OSError, ValueError):
            s = {}
        return {"auto_send": bool(s.get("auto_send")), "repo": s.get("repo") or "jhwilson504/cloudtec",
                "token": s.get("token") or ""}

    def public_settings(self) -> dict:
        s = self.settings()
        return {"auto_send": s["auto_send"], "repo": s["repo"], "has_token": bool(s["token"])}

    def save_settings(self, auto_send: bool | None = None, repo: str | None = None, token: str | None = None) -> dict:
        s = self.settings()
        if repo is not None:
            if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo.strip()):
                raise ValueError("The repository should look like owner/name")
            s["repo"] = repo.strip()
        if token is not None:
            s["token"] = self._fernet.encrypt(token.strip().encode()).decode() if token.strip() else ""
        if auto_send is not None:
            s["auto_send"] = bool(auto_send)
        if s["auto_send"] and not s["token"]:
            raise ValueError("Add a GitHub token before turning on automatic reports")
        tmp = self.settings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(s))
        tmp.replace(self.settings_path)
        return self.public_settings()

    def _token(self) -> str:
        t = self.settings()["token"]
        try:
            return self._fernet.decrypt(t.encode()).decode() if t else ""
        except InvalidToken:
            return ""

    # --- recording -----------------------------------------------------------------------------------

    def record(self, kind: str, etype: str, message: str, frames: list[str], where: str = "") -> str:
        message = redact(message, self.extra_terms)
        where = redact(re.sub(r"/[0-9a-f-]{8,}|/vm-\d+|/[^/]*%[0-9A-F]{2}[^/]*", "/…", where), self.extra_terms)
        fp = fingerprint(kind, etype, frames, message)
        now = time.time()
        with self._lock:
            data = self._load()
            e = data.get(fp) or {"kind": kind, "type": etype, "message": message, "frames": frames, "where": where,
                                 "count": 0, "first": now, "issue": None, "reported_count": 0, "dismissed": False}
            e.update(count=e["count"] + 1, last=now, version=self.version, frames=frames or e["frames"])
            data[fp] = e
            if len(data) > MAX_ERRORS:   # forget the oldest
                for old in sorted(data, key=lambda k: data[k]["last"])[: len(data) - MAX_ERRORS]:
                    data.pop(old)
            self._save(data)
        return fp

    def record_exception(self, exc: BaseException, where: str) -> str:
        return self.record("server", type(exc).__name__, str(exc), python_frames(exc), where)

    def list(self) -> list[dict]:
        data = self._load()
        return sorted(({"id": k, **v} for k, v in data.items() if not v.get("dismissed")), key=lambda e: -e["last"])

    def dismiss(self, fp: str) -> bool:
        with self._lock:
            data = self._load()
            if fp not in data:
                return False
            data[fp]["dismissed"] = True
            self._save(data)
        return True

    # --- reporting -----------------------------------------------------------------------------------

    def report_text(self, fp: str) -> tuple[str, str]:
        """The exact title and body a report would have (what the admin previews)."""
        e = self._load()[fp]
        when = lambda t: time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t))  # noqa: E731
        title = f"[{LABEL} {fp}] {e['type']} in {('the browser' if e['kind'] == 'browser' else 'the server')}" \
                f"{' on ' + e['where'] if e['where'] else ''}"
        frames = "\n".join(e["frames"]) or "(no KasTec frames)"
        body = (f"KasTec reported this error automatically. Addresses, host names, user names and quoted values were "
                f"removed before sending.\n\n"
                f"- **Kind:** {e['kind']}\n- **Error:** `{e['type']}`\n- **Message:** `{e['message']}`\n"
                f"- **Where:** `{e['where'] or '–'}`\n- **KasTec version:** {e.get('version', self.version)}\n"
                f"- **Seen:** {e['count']} time(s), first {when(e['first'])}, last {when(e['last'])}\n\n"
                f"**Where in KasTec's code** (innermost last):\n```\n{frames}\n```\n\n"
                f"<!-- fingerprint: {fp} -->\n"
                f"_The KasTec maintenance agent will post a diagnosis and a suggested fix here, and will only make the "
                f"change after the repository owner replies **fix it**._")
        return title, body

    def send(self, fp: str) -> dict:
        """Open a GitHub issue for this error, or add a comment to its issue if it keeps happening."""
        token, repo = self._token(), self.settings()["repo"]
        if not token:
            raise ValueError("Add a GitHub token first (Settings > Error reports)")
        with self._lock:
            e = self._load().get(fp)
        if not e:
            raise KeyError(fp)
        title, body = self.report_text(fp)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "KasTec-error-watch"}
        with httpx.Client(base_url="https://api.github.com", headers=headers, timeout=20, transport=self._transport) as c:
            if e.get("issue"):
                r = c.post(f"/repos/{repo}/issues/{e['issue']}/comments",
                           json={"body": f"Seen again: now {e['count']} time(s) in total, last at "
                                         f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(e['last']))} (KasTec {e.get('version')})."})
                number = e["issue"]
            else:
                r = c.post(f"/repos/{repo}/issues", json={"title": title, "body": body, "labels": [LABEL]})
                number = r.json().get("number") if r.status_code < 300 else None
        if r.status_code >= 300:
            raise ValueError(f"GitHub refused the report (HTTP {r.status_code}). Check the token can write issues on {repo}.")
        with self._lock:
            data = self._load()
            data[fp].update(issue=number, reported_count=e["count"])
            self._save(data)
        return {"issue": number, "url": f"https://github.com/{repo}/issues/{number}"}

    def send_pending(self) -> int:
        """Automatic mode: report new errors, and errors that happened again since the last report (at most daily)."""
        sent = 0
        for e in self.list():
            again = e.get("issue") and e["count"] > e.get("reported_count", 0) and time.time() - e.get("sent_at", 0) > 86400
            if not e.get("issue") or again:
                try:
                    self.send(e["id"])
                    with self._lock:
                        data = self._load()
                        data[e["id"]]["sent_at"] = time.time()
                        self._save(data)
                    sent += 1
                except (ValueError, KeyError, httpx.HTTPError):
                    break
        return sent

    def start(self) -> None:
        def loop():
            while not self._stop.wait(600):
                if self.settings()["auto_send"]:
                    self.send_pending()
        if not self._thread:
            self._thread = threading.Thread(target=loop, name="error-watch", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
