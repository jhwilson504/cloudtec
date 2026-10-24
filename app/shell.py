"""A persistent PowerShell session behind the console's PowerShell page.

One PowerShell process is kept running, so variables, the current folder and
imported modules carry over between commands. Each command is sent as a single
base64-encoded line (no quoting or encoding problems), dot-sourced so it runs
in the session's own scope, and followed by a unique marker line that tells us
where its output ends.
"""

import base64
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

MAX_OUTPUT = 1_000_000
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
WIDTH = 200

INIT = (
    "if ($PSStyle) { $PSStyle.OutputRendering = 'PlainText' }; "
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
    "$ProgressPreference = 'SilentlyContinue'"
)


def find_powershell(configured: str | None = None) -> str | None:
    if configured:
        return configured if Path(configured).exists() or shutil.which(configured) else None
    return shutil.which("pwsh") or shutil.which("powershell")


class PowerShellSession:
    def __init__(self, exe: str, start_dir: Path, history: Path | None = None):
        self.exe = exe
        self.start_dir = start_dir
        self.history = history
        self.cwd = str(start_dir)
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue = queue.Queue()
        self._lock = threading.Lock()

    def _start(self) -> None:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self._proc = subprocess.Popen(
            [self.exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=self.start_dir, text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=flags,
        )
        self._lines = queue.Queue()
        threading.Thread(target=self._pump, args=(self._proc, self._lines), daemon=True).start()
        self._send(INIT, timeout=60)

    @staticmethod
    def _pump(proc: subprocess.Popen, lines: queue.Queue) -> None:
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    def _send(self, command: str, timeout: float) -> dict:
        marker = uuid.uuid4().hex
        encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
        self._proc.stdin.write(
            f"$__c = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}')); "
            f"try {{ . ([scriptblock]::Create($__c)) 2>&1 | Out-String -Width {WIDTH} }} "
            f"catch {{ $_ | Out-String -Width {WIDTH} }}; "
            f"Write-Output \"{marker}$((Get-Location).Path)\"\n"
        )
        self._proc.stdin.flush()

        out, size, deadline = [], 0, time.monotonic() + timeout
        while True:
            try:
                line = self._lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                self.close()
                return self._result(out, size, note=f"Stopped: the command ran longer than {int(timeout)} seconds. "
                                                    "The session was restarted.")
            if line is None:
                self.close()
                return self._result(out, size, note="PowerShell exited. A new session starts with your next command.")
            line = ANSI.sub("", line)
            if line.startswith(marker):
                self.cwd = line[len(marker):].strip() or self.cwd
                return self._result(out, size)
            if size < MAX_OUTPUT:
                out.append(line)
            size += len(line)

    @staticmethod
    def _result(out: list[str], size: int, note: str | None = None) -> dict:
        text = "".join(out).strip("\r\n")
        if size > MAX_OUTPUT:
            text += f"\n… output cut off after {MAX_OUTPUT:,} characters"
        if note:
            text = (text + "\n\n" if text else "") + note
        return {"output": text, "error": note is not None}

    def run(self, command: str, timeout: float = 300) -> dict:
        with self._lock:
            if self.history:
                stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                with self.history.open("a", encoding="utf-8") as f:
                    f.write(f"{stamp}\t{command!r}\n")
            if not self._proc or self._proc.poll() is not None:
                self.cwd = str(self.start_dir)
                self._start()
            result = self._send(command, timeout)
            return {**result, "cwd": self.cwd}

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            try:
                proc.stdin.close()
                proc.wait(timeout=3)
            except Exception:  # noqa: BLE001 - make sure it's gone either way
                proc.kill()

    def reset(self) -> None:
        with self._lock:
            self.close()
            self.cwd = str(self.start_dir)
