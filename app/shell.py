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

    def run(self, command: str, timeout: float = 300, log_as: str | None = None) -> dict:
        """Run a command. log_as replaces the command in the history log (used for scripts carrying passwords)."""
        with self._lock:
            if self.history:
                stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                with self.history.open("a", encoding="utf-8") as f:
                    f.write(f"{stamp}\t{log_as or repr(command)}\n")
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


def ps_quote(value) -> str:
    """A PowerShell single-quoted string literal."""
    return "'" + str(value).replace("'", "''") + "'"


AD_MISSING = """The ActiveDirectory PowerShell module isn't installed on this computer.
Install it once from an Administrator PowerShell window:
    Add-WindowsCapability -Online -Name Rsat.ActiveDirectory.DS-LDS.Tools~~~~0.0.1.0
(On Windows Server: Install-WindowsFeature RSAT-AD-PowerShell)
Then click Connect again. The saved credential is already available as $KasTec[{name}].Credential."""

VC_MISSING = """VMware PowerCLI isn't installed on this computer.
Install it once (no Administrator needed):
    Install-Module VCF.PowerCLI -Scope CurrentUser
(Older name: VMware.PowerCLI.) Then click Connect again.
The saved credential is already available as $KasTec[{name}].Credential."""


# Plain-LDAP commands set up by Connect for Active Directory. The AD cmdlets (Get-ADUser, ...) talk to
# AD Web Services on TCP 9389, which firewalls between networks often block; these only need LDAP (389).
# They use $global:KasTecLdap (Server, Base, Credential), set by Connect to the DC that was picked.
LDAP_HELPERS = r"""
function global:ConvertTo-LdapFilterValue([string]$Value) {
  -join ($Value.ToCharArray() | ForEach-Object { if ('\*()'.IndexOf($_) -ge 0 -or [int]$_ -eq 0) { '\{0:x2}' -f [int]$_ } else { $_ } })
}
function global:Invoke-KasTecLdap([string]$Filter, [string[]]$Properties) {
  $l = $global:KasTecLdap
  if (-not $l) { throw 'Click Connect for an Active Directory connection first.' }
  $root = New-Object System.DirectoryServices.DirectoryEntry("LDAP://$($l.Server)/$($l.Base)", $l.Credential.UserName, $l.Credential.GetNetworkCredential().Password)
  $s = New-Object System.DirectoryServices.DirectorySearcher($root, $Filter)
  $s.PageSize = 1000
  if ($Properties) { $s.PropertiesToLoad.AddRange($Properties) }
  try { $s.FindAll() | ForEach-Object { $_.Properties } } finally { $s.Dispose(); $root.Dispose() }
}
function global:Get-KasTecLdapValue($p, [string]$n) { $v = $p[$n]; if ($null -ne $v -and @($v).Count) { @($v)[0] } }
function global:ConvertFrom-KasTecFileTime($v) { if ($v -and [int64]$v -gt 0 -and [int64]$v -lt [int64]::MaxValue) { [DateTime]::FromFileTime([int64]$v) } }
function global:Get-KasTecLdapMatch([string]$Name, [string[]]$Attributes) {
  if (-not $Name -or $Name -eq '*') { return '' }
  $v = ConvertTo-LdapFilterValue $Name
  '(|' + (-join ($Attributes | ForEach-Object { "($_=$v)($_=*$v*)" })) + ')'
}

function global:Find-LdapUser {
  <# Users whose username or display name contains -Name ('*' for everyone). Uses plain LDAP. #>
  param([Parameter(Mandatory, Position = 0)][string]$Name)
  $m = Get-KasTecLdapMatch $Name 'sAMAccountName', 'displayName'
  Invoke-KasTecLdap "(&(objectCategory=person)(objectClass=user)$m)" 'samaccountname', 'displayname', 'mail', 'useraccountcontrol', 'lockouttime', 'lastlogontimestamp', 'msds-user-account-control-computed', 'distinguishedname' |
    ForEach-Object { [pscustomobject]@{
      User = Get-KasTecLdapValue $_ 'samaccountname'; Name = Get-KasTecLdapValue $_ 'displayname'; Mail = Get-KasTecLdapValue $_ 'mail'
      Disabled = [bool]([int](Get-KasTecLdapValue $_ 'useraccountcontrol') -band 2)
      Locked = [bool]([int](Get-KasTecLdapValue $_ 'msds-user-account-control-computed') -band 0x10)
      LastLogon = ConvertFrom-KasTecFileTime (Get-KasTecLdapValue $_ 'lastlogontimestamp')
      Path = Get-KasTecLdapValue $_ 'distinguishedname' } }
}

function global:Get-LdapLockedUser {
  <# Accounts that are locked out right now. Uses plain LDAP. #>
  Invoke-KasTecLdap '(&(objectCategory=person)(objectClass=user)(lockoutTime>=1))' 'samaccountname', 'displayname', 'lockouttime', 'msds-user-account-control-computed', 'distinguishedname' |
    Where-Object { [int](Get-KasTecLdapValue $_ 'msds-user-account-control-computed') -band 0x10 } |
    ForEach-Object { [pscustomobject]@{
      User = Get-KasTecLdapValue $_ 'samaccountname'; Name = Get-KasTecLdapValue $_ 'displayname'
      LockedAt = ConvertFrom-KasTecFileTime (Get-KasTecLdapValue $_ 'lockouttime'); Path = Get-KasTecLdapValue $_ 'distinguishedname' } }
}

function global:Find-LdapGroup {
  <# Groups whose name contains -Name ('*' for all). Uses plain LDAP. #>
  param([Parameter(Mandatory, Position = 0)][string]$Name)
  $m = Get-KasTecLdapMatch $Name 'sAMAccountName', 'cn'
  Invoke-KasTecLdap "(&(objectCategory=group)$m)" 'samaccountname', 'description', 'member', 'distinguishedname' |
    ForEach-Object { [pscustomobject]@{
      Name = Get-KasTecLdapValue $_ 'samaccountname'; Description = Get-KasTecLdapValue $_ 'description'
      Members = @($_['member']).Where({ $_ }).Count; Path = Get-KasTecLdapValue $_ 'distinguishedname' } }
}

function global:Get-LdapGroupMember {
  <# Direct members of the group named -Group (its exact name). Uses plain LDAP. #>
  param([Parameter(Mandatory, Position = 0)][string]$Group)
  $v = ConvertTo-LdapFilterValue $Group
  $g = @(Invoke-KasTecLdap "(&(objectCategory=group)(|(sAMAccountName=$v)(cn=$v)))" 'distinguishedname')
  if (-not $g.Count) { throw "No group named '$Group'. Find-LdapGroup $Group lists similar names." }
  $dn = ConvertTo-LdapFilterValue (Get-KasTecLdapValue $g[0] 'distinguishedname')
  Invoke-KasTecLdap "(memberOf=$dn)" 'samaccountname', 'displayname', 'objectclass', 'useraccountcontrol', 'distinguishedname' |
    ForEach-Object { [pscustomobject]@{
      Name = Get-KasTecLdapValue $_ 'samaccountname'; DisplayName = Get-KasTecLdapValue $_ 'displayname'
      Type = @($_['objectclass'])[-1]; Disabled = [bool]([int](Get-KasTecLdapValue $_ 'useraccountcontrol') -band 2)
      Path = Get-KasTecLdapValue $_ 'distinguishedname' } } | Sort-Object Type, Name
}

function global:Find-LdapComputer {
  <# Computers whose name contains -Name ('*' for all). Uses plain LDAP. #>
  param([Parameter(Mandatory, Position = 0)][string]$Name)
  $m = Get-KasTecLdapMatch $Name 'cn', 'dNSHostName'
  Invoke-KasTecLdap "(&(objectCategory=computer)$m)" 'cn', 'dnshostname', 'operatingsystem', 'useraccountcontrol', 'lastlogontimestamp', 'distinguishedname' |
    ForEach-Object { [pscustomobject]@{
      Name = Get-KasTecLdapValue $_ 'cn'; DNSName = Get-KasTecLdapValue $_ 'dnshostname'; OS = Get-KasTecLdapValue $_ 'operatingsystem'
      Disabled = [bool]([int](Get-KasTecLdapValue $_ 'useraccountcontrol') -band 2)
      LastLogon = ConvertFrom-KasTecFileTime (Get-KasTecLdapValue $_ 'lastlogontimestamp')
      Path = Get-KasTecLdapValue $_ 'distinguishedname' } }
}
"""

LDAP_READY = ("Also ready over plain LDAP (works even where AD Web Services, port 9389, is blocked): "
              "Find-LdapUser <name>, Get-LdapLockedUser, Find-LdapGroup <name>, Get-LdapGroupMember <group>, "
              "Find-LdapComputer <name>. Use * as the name for all.")


def connect_script(conn, host: str | None = None) -> str:
    """PowerShell that signs this session in to a saved connection.

    The password only travels inside the base64-encoded command over the
    session's stdin, never on a command line, in the history log or in output.
    Every value from the connection is passed as a single-quoted literal.
    For Active Directory, `host` picks which domain controller the AD cmdlets talk to.
    """
    cfg = conn.config
    host = host or cfg["host"]
    name = ps_quote(conn.name)
    user = cfg.get("bind_user") if conn.kind == "ad" else cfg.get("username")
    if conn.kind == "ad":
        base = ",".join(p.strip() for p in str(cfg.get("base_dn", "")).split(",") if p.strip().lower().startswith("dc=")) \
            or cfg.get("base_dn", "")
        body = f"""
  $global:KasTecLdap = @{{ Server = {ps_quote(host)}; Base = {ps_quote(base)}; Credential = $__cred }}{LDAP_HELPERS}
  if (Get-Module -ListAvailable -Name ActiveDirectory) {{
    Import-Module ActiveDirectory -WarningAction SilentlyContinue
    $PSDefaultParameterValues['*-AD*:Server'] = {ps_quote(host)}
    $PSDefaultParameterValues['*-AD*:Credential'] = $__cred
    {ps_quote(f"Connected to {conn.name} ({host}) as {user}.")}
    'Active Directory commands such as Get-ADUser now use this domain automatically.'
  }} else {{
    {ps_quote(AD_MISSING.replace("{name}", name))}
  }}
  ''
  {ps_quote(LDAP_READY)}"""
    else:
        cert = "Ignore" if cfg.get("verify_ssl") is False else "Fail"
        body = f"""
  if (Get-Module -ListAvailable -Name VMware.VimAutomation.Core) {{
    Import-Module VMware.VimAutomation.Core -WarningAction SilentlyContinue
    Set-PowerCLIConfiguration -Scope Session -InvalidCertificateAction {cert} -Confirm:$false | Out-Null
    Connect-VIServer -Server {ps_quote(host)} -Port {int(cfg.get("port") or 443)} -Credential $__cred -ErrorAction Stop | Out-Null
    {ps_quote(f"Connected to vCenter {conn.name} ({host}) as {user}.")}
    'PowerCLI commands such as Get-VM and Get-VMHost now work.'
  }} else {{
    {ps_quote(VC_MISSING.replace("{name}", name))}
  }}"""
    return f"""if (-not $global:KasTec) {{ $global:KasTec = @{{}} }}
try {{
  $__cred = New-Object System.Management.Automation.PSCredential({ps_quote(user)}, (ConvertTo-SecureString {ps_quote(conn.secret)} -AsPlainText -Force))
  $global:KasTec[{name}] = @{{ Server = {ps_quote(host)}; Credential = $__cred }}{body}
}} finally {{
  Remove-Variable __cred -ErrorAction SilentlyContinue
}}"""


OK_MARK, ERROR_MARK = "KASTEC-OK", "KASTEC-ERROR:"


def run_script(exe: str, script: str, timeout: float = 120) -> tuple[bool, str]:
    """Run a one-off script in a fresh PowerShell and report whether it finished cleanly.

    Like the interactive session, the script travels base64-encoded over stdin
    (never on the command line, where other processes could read it).
    """
    encoded = base64.b64encode(f"try {{ {script}\n'{OK_MARK}' }} catch {{ '{ERROR_MARK} ' + $_.Exception.Message }}"
                               .encode("utf-8")).decode("ascii")
    line = (f"$__c = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}')); "
            "& ([scriptblock]::Create($__c)) 2>&1 | Out-String -Width 200\n")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        r = subprocess.run([exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "-"], input=line,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=flags)
    except subprocess.TimeoutExpired:
        return False, f"PowerShell didn't finish within {int(timeout)} seconds."
    out = ANSI.sub("", r.stdout + r.stderr).strip()
    if ERROR_MARK in out:
        return False, out.split(ERROR_MARK, 1)[1].strip()
    if out.endswith(OK_MARK):
        return True, out[: -len(OK_MARK)].strip()
    return False, out or f"PowerShell exited with code {r.returncode}."


def restart_computer_script(conn, computer: str) -> str:
    """Restart-Computer for one machine, signed in with an AD connection's saved account."""
    user = conn.config.get("bind_user")
    return (f"$__cred = New-Object System.Management.Automation.PSCredential({ps_quote(user)}, "
            f"(ConvertTo-SecureString {ps_quote(conn.secret)} -AsPlainText -Force))\n"
            f"Restart-Computer -ComputerName {ps_quote(computer)} -Credential $__cred -Force -ErrorAction Stop")


def restart_domain_controller(exe: str, conn, computer: str) -> tuple[bool, str]:
    return run_script(exe, restart_computer_script(conn, computer), timeout=120)


def replication_script(conn, computer: str) -> str:
    """One line per replication partner of `computer`: partner|last success (ISO)|last result|consecutive failures."""
    user = conn.config.get("bind_user")
    return (f"$__cred = New-Object System.Management.Automation.PSCredential({ps_quote(user)}, "
            f"(ConvertTo-SecureString {ps_quote(conn.secret)} -AsPlainText -Force))\n"
            "Import-Module ActiveDirectory -ErrorAction Stop -WarningAction SilentlyContinue\n"
            f"Get-ADReplicationPartnerMetadata -Target {ps_quote(computer)} -Scope Server -Credential $__cred -ErrorAction Stop | "
            "ForEach-Object { 'PARTNER|{0}|{1}|{2}|{3}' -f $_.Partner, "
            "$(if ($_.LastReplicationSuccess) { $_.LastReplicationSuccess.ToUniversalTime().ToString('o') }), "
            "$_.LastReplicationResult, $_.ConsecutiveReplicationFailures }")


def replication_status(exe: str, conn, computer: str) -> tuple[bool, list[dict] | str]:
    """(True, partners) or (False, why it couldn't be checked)."""
    ok, out = run_script(exe, replication_script(conn, computer), timeout=90)
    if not ok:
        return False, out
    partners = []
    for line in out.splitlines():
        if not line.startswith("PARTNER|"):
            continue
        _, dn, last, result, failures = (line.split("|") + [""] * 5)[:5]
        # CN=NTDS Settings,CN=BTCPRODDC2,CN=Servers,CN=poinciana,... -> BTCPRODDC2
        parts = [p.split("=", 1)[-1] for p in dn.split(",")]
        partners.append({"partner": parts[1] if len(parts) > 1 else dn, "last_success": last or None,
                         "last_result": int(result or 0), "failures": int(failures or 0)})
    return True, partners
