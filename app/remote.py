"""Getting into servers from KasTec: Remote Desktop files for Windows, and SSH terminals for Linux.

RDP: KasTec hands the browser a small .rdp file; Windows opens it in Remote Desktop (mstsc), already pointed at
the server, and asks for the password as usual. The connection runs from the viewer's own PC.

SSH: KasTec runs PuTTY's plink on its own computer with a terminal requested on the server (-t), and relays the
keystrokes and screen output to a terminal in the browser. The password is handed to plink in a file that is
deleted once the session has started; it is never stored or logged. A server's host key is trusted the first
time an administrator confirms it (data/ssh_hosts.json) and checked on every connection after that.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .connectors.vnx import FINGERPRINT, find_plink

HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
USER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@\\-]{0,63}$")
IDLE_SECONDS = 30 * 60
MAX_SESSIONS = 10
KEEP_BYTES = 2_000_000      # scrollback kept per session for the browser to fetch


class RemoteError(Exception):
    pass


class HostKeyNeeded(RemoteError):
    def __init__(self, host: str, fingerprint: str, changed: bool = False):
        super().__init__(f"{host}'s SSH key {'has changed' if changed else 'is not trusted yet'}: {fingerprint}")
        self.host, self.fingerprint, self.changed = host, fingerprint, changed


def rdp_file(host: str, username: str = "") -> str:
    """A Remote Desktop connection file for mstsc."""
    if not HOST.match(host or ""):
        raise RemoteError("That isn't a valid server name or address")
    lines = [f"full address:s:{host}", "prompt for credentials:i:1", "authentication level:i:2",
             "screen mode id:i:2", "use multimon:i:0", "redirectclipboard:i:1", "audiomode:i:2",
             "autoreconnection enabled:i:1"]
    if username and USER.match(username):
        lines.append(f"username:s:{username}")
    return "\r\n".join(lines) + "\r\n"


class _Session:
    def __init__(self, sid, proc, host, user, owner):
        self.sid, self.proc, self.host, self.user, self.owner = sid, proc, host, user, owner
        self.chunks: list[bytes] = []
        self.start = 0               # offset of chunks[0] in the whole output
        self.size = 0                # total bytes ever received
        self.cond = threading.Condition()
        self.last = time.time()
        self.closed = False

    def pump(self):
        out = self.proc.stdout
        while True:
            data = os.read(out.fileno(), 65536) if hasattr(out, "fileno") else out.read(65536)
            if not data:
                break
            with self.cond:
                self.chunks.append(data)
                self.size += len(data)
                while self.size - self.start > KEEP_BYTES and len(self.chunks) > 1:
                    self.start += len(self.chunks.pop(0))
                self.cond.notify_all()
        with self.cond:
            self.closed = True
            self.cond.notify_all()


class SSHSessions:
    def __init__(self, data_dir: Path, plink: str | None = None):
        self.keys_path = data_dir / "ssh_hosts.json"
        self.tmp_dir = data_dir / "tmp"
        self._plink = plink
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()

    # trusted host keys
    def keys(self) -> dict:
        try:
            return json.loads(self.keys_path.read_text())
        except (OSError, ValueError):
            return {}

    def trust(self, host: str, fingerprint: str) -> None:
        if not FINGERPRINT.fullmatch(fingerprint):
            raise RemoteError("That isn't an SSH key fingerprint")
        keys = self.keys()
        keys[host.lower()] = fingerprint
        tmp = self.keys_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(keys, indent=2))
        tmp.replace(self.keys_path)

    def open(self, host: str, port: int, user: str, password: str, owner: str, wait: float = 8.0) -> _Session:
        if not HOST.match(host or "") or not USER.match(user or "") or not 1 <= int(port) <= 65535:
            raise RemoteError("Check the server address, port and username")
        self._reap()
        if len(self._sessions) >= MAX_SESSIONS:
            raise RemoteError(f"There are already {MAX_SESSIONS} SSH sessions open in KasTec. Close one first.")
        plink = self._plink or find_plink()
        if not plink:
            raise RemoteError("PuTTY's plink.exe wasn't found on the KasTec computer. Install PuTTY, or set KASTEC_PLINK.")
        known = self.keys().get(host.lower())
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, pwfile = tempfile.mkstemp(dir=self.tmp_dir, prefix="ssh-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(password + "\n")
        args = [plink, "-ssh", "-t", "-batch", "-P", str(int(port)), "-l", user, "-pwfile", pwfile]
        if known:
            args += ["-hostkey", known]
        args.append(host)
        try:
            proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    bufsize=0, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            os.remove(pwfile)
            raise RemoteError(f"Couldn't run plink ({plink}): {e}") from e
        session = _Session(secrets.token_urlsafe(16), proc, host, user, owner)
        threading.Thread(target=session.pump, daemon=True).start()
        try:
            # Give plink time to check the host key and sign in; if it gives up, say why.
            started, deadline = time.time(), time.time() + wait
            while time.time() < deadline and proc.poll() is None:
                if session.size and time.time() - started > 1.5:   # still running and the server has said something
                    break
                time.sleep(0.1)
            if proc.poll() is not None:
                time.sleep(0.2)
                text = b"".join(session.chunks).decode("utf-8", "replace")
                self._failed(host, user, text, known)
        finally:
            try:
                os.remove(pwfile)
            except OSError:
                pass
        with self._lock:
            self._sessions[session.sid] = session
        return session

    def _failed(self, host, user, text, known):
        low = text.lower()
        prints = FINGERPRINT.findall(text)
        if ("not cached" in low or "not in the cache" in low) and prints:
            raise HostKeyNeeded(host, prints[-1])
        if ("security breach" in low or "manually configured list" in low or "does not match" in low) and known:
            raise HostKeyNeeded(host, prints[-1] if prints else "unknown", changed=True)
        if "access denied" in low or "authentication" in low or "password" in low:
            raise RemoteError(f"{host} refused the username or password ({user}).")
        last = [ln for ln in text.strip().splitlines() if ln.strip()][-1:] or ["no answer"]
        raise RemoteError(f"Couldn't connect to {host}: {last[0]}")

    def get(self, sid: str, owner: str) -> _Session:
        s = self._sessions.get(sid)
        if not s or s.owner != owner:
            raise RemoteError("That SSH session has ended")
        s.last = time.time()
        return s

    def read(self, sid: str, owner: str, since: int, wait: float = 20.0) -> dict:
        s = self.get(sid, owner)
        with s.cond:
            if s.size <= since and not s.closed:
                s.cond.wait(timeout=wait)
            since = max(since, s.start)
            data, pos = b"", s.start
            for chunk in s.chunks:
                end = pos + len(chunk)
                if end > since:
                    data += chunk[max(0, since - pos):]
                pos = end
            return {"data": base64.b64encode(data).decode("ascii"), "next": s.size, "closed": s.closed and s.size <= since + len(data)}

    def write(self, sid: str, owner: str, text: str) -> None:
        s = self.get(sid, owner)
        if s.closed or s.proc.poll() is not None:
            raise RemoteError("The SSH session has ended")
        try:
            s.proc.stdin.write(text.encode("utf-8"))
            s.proc.stdin.flush()
        except OSError as e:
            raise RemoteError("The SSH session has ended") from e

    def close(self, sid: str, owner: str | None = None) -> _Session | None:
        with self._lock:
            s = self._sessions.get(sid)
            if not s or (owner is not None and s.owner != owner):
                return None
            self._sessions.pop(sid)
        try:
            s.proc.stdin.close()
        except OSError:
            pass
        try:
            s.proc.terminate()
            s.proc.wait(timeout=3)
        except Exception:  # noqa: BLE001 - make sure it's gone
            s.proc.kill()
        return s

    def _reap(self) -> None:
        now = time.time()
        for sid, s in list(self._sessions.items()):
            if s.closed or now - s.last > IDLE_SECONDS:
                self.close(sid)

    def close_all(self) -> None:
        for sid in list(self._sessions):
            self.close(sid)
